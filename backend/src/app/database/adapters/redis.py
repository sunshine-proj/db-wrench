"""Redis / Valkey adapter (redis-py, one bounded pool per logical database). ADR-0019.

Standalone servers only (no Cluster, no Sentinel discovery): following server-advertised
addresses would bypass the network policy, like MongoDB replica-set discovery (ADR-0007).
Every new TCP connection re-resolves and re-validates the destination and connects to the
validated address.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import os
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import redis
import redis.exceptions

from app.database.core.base import Capabilities, TestResult, TlsMode
from app.database.core.serialization import binary_marker, to_json_value
from app.database.keyvalue import commands as cmd
from app.database.keyvalue.base import KeyValueAdapter
from app.database.keyvalue.models import (
    KEY_TYPES,
    CommandResult,
    KeyDetail,
    KeyInfo,
    KeyPage,
    KvDatabaseInfo,
    ValuePage,
)
from app.errors import (
    Conflict,
    ConnectionFailed,
    Forbidden,
    NotFound,
    QueryFailed,
    StatementTimeout,
    ValidationFailed,
)
from app.observability import logger, scrub

MAX_DATABASES = 64
SCAN_ROUNDS = 40  # bounded work per page: a sparse pattern cannot pin the server
SCAN_COUNT = 500
EDIT_MAX_ELEMENTS = 1000
EDIT_MAX_BYTES = 1 << 20
MAX_PATTERN = 256


def encode_key(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_key(key: str) -> bytes:
    if not key or len(key) > 4096:
        raise ValidationFailed("Invalid key.")
    try:
        std = key.replace("-", "+").replace("_", "/")
        return base64.b64decode(std + "=" * (-len(std) % 4), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationFailed("Invalid key.") from exc


def _text(raw: Any, limit: int) -> Any:
    """UTF-8 text, or a binary marker; long text is clipped with a marker."""
    if isinstance(raw, str):
        return to_json_value(raw, limit)
    if not isinstance(raw, bytes | bytearray):
        return to_json_value(raw, limit)
    try:
        return to_json_value(bytes(raw).decode("utf-8"), limit)
    except UnicodeDecodeError:
        return binary_marker(bytes(raw))


def _utf8(raw: bytes) -> bool:
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _version(dump: bytes | None) -> str:
    return hashlib.sha256(dump or b"").hexdigest()[:32]


class RedisAdapter(KeyValueAdapter):
    capabilities = Capabilities(
        family="keyvalue",
        engine="redis",
        supports_schemas=False,
        supports_views=False,
        supports_indexes=False,
        supports_read_only_transactions=False,
        supports_cancellation=False,
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pools: dict[int, redis.ConnectionPool] = {}
        self._lock = threading.Lock()
        self._ca_path: str | None = None
        self._flags: dict[str, frozenset[str] | None] = {}
        self._db_count: int | None = None

    # ---- connection handling ----------------------------------------------------------

    def _ca_file(self) -> str | None:
        if not self.spec.tls_ca:
            return None
        if self._ca_path is None:
            fd, path = tempfile.mkstemp(prefix="dbw-ca-", suffix=".pem")
            with os.fdopen(fd, "w") as fh:
                fh.write(self.spec.tls_ca)
            self._ca_path = path
        return self._ca_path

    def _pool_for(self, index: int) -> redis.ConnectionPool:
        with self._lock:
            pool = self._pools.get(index)
            if pool is None:
                pool = self._build_pool(index)
                self._pools[index] = pool
            return pool

    def _build_pool(self, index: int) -> redis.ConnectionPool:
        s = self.spec
        adapter = self
        verify_name = s.tls_mode == TlsMode.VERIFY_FULL
        use_tls = s.tls_mode != TlsMode.DISABLE

        class Pinned(redis.SSLConnection if use_tls else redis.Connection):  # type: ignore[misc]
            def _connect(self) -> Any:  # a new TCP socket: validate again, connect to the result
                pinned = adapter.policy.resolve(s.host, s.port)[0]
                if not verify_name:
                    self.host = pinned
                return super()._connect()

        kwargs: dict[str, Any] = {
            "host": s.host,
            "port": s.port,
            "db": index,
            "username": s.username or None,
            "password": s.password or None,
            "socket_timeout": self.limits.statement_timeout_s + 5,
            "socket_connect_timeout": s.connect_timeout_s,
            "max_connections": max(1, min(s.pool_max, 20)),
            "health_check_interval": 30,
            "client_name": "db-wrench",
            "decode_responses": False,
        }
        if use_tls:
            ca = self._ca_file()
            if s.tls_mode == TlsMode.REQUIRE:
                kwargs.update(ssl_cert_reqs="none", ssl_check_hostname=False)
            else:
                if not ca:
                    raise ConnectionFailed("TLS verification requires a CA certificate.")
                kwargs.update(
                    ssl_cert_reqs="required", ssl_ca_certs=ca, ssl_check_hostname=verify_name
                )
        return redis.ConnectionPool(connection_class=Pinned, **kwargs)

    @contextmanager
    def _client(self, index: int = 0) -> Iterator[Any]:
        client = redis.Redis(connection_pool=self._pool_for(index))
        with self._errors():
            yield client

    @contextmanager
    def _errors(self) -> Iterator[None]:
        try:
            yield
        except (
            ConnectionFailed,
            Conflict,
            Forbidden,
            NotFound,
            QueryFailed,
            StatementTimeout,
            ValidationFailed,
        ):
            raise
        except redis.exceptions.AuthenticationError as exc:
            raise ConnectionFailed(scrub(str(exc), self.spec.password)) from exc
        except redis.exceptions.TimeoutError as exc:
            raise StatementTimeout() from exc
        except redis.exceptions.ConnectionError as exc:
            raise ConnectionFailed(scrub(str(exc), self.spec.password)) from exc
        except redis.exceptions.ResponseError as exc:
            raise QueryFailed(scrub(str(exc), self.spec.password)) from exc
        except redis.exceptions.RedisError as exc:
            raise QueryFailed(scrub(str(exc), self.spec.password)) from exc
        except OSError as exc:
            raise ConnectionFailed(scrub(str(exc), self.spec.password)) from exc

    def close(self) -> None:
        with self._lock:
            pools, self._pools = list(self._pools.values()), {}
        for p in pools:
            with contextlib.suppress(Exception):
                p.disconnect()
        if self._ca_path:
            with contextlib.suppress(OSError):
                os.unlink(self._ca_path)
            self._ca_path = None

    def test(self) -> TestResult:
        started = time.perf_counter()
        try:
            with self._client(self._default_db()) as r:
                r.ping()
                info = r.info("server")
        except Exception as exc:
            code = getattr(exc, "code", "connection_failed")
            return TestResult(False, error=getattr(exc, "detail", str(exc)), error_code=code)
        version = info.get("valkey_version") or info.get("redis_version")
        name = "Valkey" if info.get("valkey_version") else "Redis"
        return TestResult(
            True, f"{name} {version}", round((time.perf_counter() - started) * 1000, 1)
        )

    def _default_db(self) -> int:
        try:
            return max(0, int(self.spec.database or 0))
        except ValueError:
            return 0

    # ---- metadata ---------------------------------------------------------------------

    def _database_count(self, r: Any) -> int:
        if self._db_count is None:
            try:
                reply = r.execute_command("CONFIG", "GET", "databases")
                n = int(reply.get(b"databases") or reply.get("databases") or 16)
            except Exception:  # CONFIG is commonly denied by ACLs or renamed: assume the default
                n = 16
            self._db_count = max(1, min(n, MAX_DATABASES))
        return self._db_count

    def _index(self, database: str) -> int:
        if not database.isdigit() or len(database) > 3:
            raise NotFound(f"Database {database!r} was not found.")
        index = int(database)
        with self._client(self._default_db()) as r:
            if index >= self._database_count(r):
                raise NotFound(f"Database {database!r} was not found.")
        return index

    def list_databases(self) -> list[KvDatabaseInfo]:
        with self._client(self._default_db()) as r:
            count = self._database_count(r)
            keyspace = r.info("keyspace")
        out: list[KvDatabaseInfo] = []
        for i in range(count):
            ks = keyspace.get(f"db{i}") or {}
            out.append(KvDatabaseInfo(i, str(i), int(ks.get("keys", 0)), int(ks.get("expires", 0))))
        return out

    # ---- keys ---------------------------------------------------------------------------

    def scan_keys(
        self,
        database: str,
        *,
        pattern: str | None,
        key_type: str | None,
        cursor: str | None,
        limit: int,
    ) -> KeyPage:
        index = self._index(database)
        if key_type is not None and key_type not in KEY_TYPES:
            raise ValidationFailed(f"Unknown key type {key_type!r}.")
        if pattern is not None and (len(pattern) > MAX_PATTERN or "\x00" in pattern):
            raise ValidationFailed("Invalid pattern.")
        if cursor is not None and not cursor.isdigit():
            raise ValidationFailed("Invalid cursor.")
        limit = max(1, min(limit, self.limits.max_page_size))
        cur = int(cursor or 0)
        found: list[bytes] = []
        rounds = 0
        truncated = False
        with self._client(index) as r:
            while True:
                kwargs: dict[str, Any] = {"count": SCAN_COUNT}
                if pattern:
                    kwargs["match"] = pattern
                if key_type:
                    kwargs["_type"] = key_type
                cur, batch = r.scan(cur, **kwargs)
                found.extend(batch)
                rounds += 1
                if cur == 0 or len(found) >= limit:
                    break
                if rounds >= SCAN_ROUNDS:
                    truncated = True
                    break
            # SCAN may return duplicates, and a batch can overshoot ``limit``: every key of the
            # batch is returned because the cursor has already moved past it.
            page_keys = list(dict.fromkeys(found))
            pipe = r.pipeline(transaction=False)
            for k in page_keys:
                pipe.pttl(k)
                if not key_type:
                    pipe.type(k)
            replies = pipe.execute(raise_on_error=False)
            total = r.dbsize()
        stride = 1 if key_type else 2
        infos: list[KeyInfo] = []
        for i, k in enumerate(page_keys):
            ttl = replies[i * stride]
            ktype = key_type or replies[i * stride + 1]
            if isinstance(ktype, bytes):
                ktype = ktype.decode()
            if (
                isinstance(ttl, Exception)
                or ktype in (None, "none")
                or isinstance(ktype, Exception)
            ):
                continue  # expired between SCAN and the lookup
            ok = _utf8(k)
            infos.append(
                KeyInfo(
                    encode_key(k),
                    k.decode("utf-8") if ok else k.decode("utf-8", "backslashreplace"),
                    str(ktype),
                    int(ttl) if isinstance(ttl, int) and ttl >= 0 else None,
                    binary=not ok,
                )
            )
        warnings = []
        if truncated:
            warnings.append(
                "The scan stopped early to protect the server; narrow the pattern or continue."
            )
        return KeyPage(
            keys=infos,
            limit=limit,
            next_cursor=str(cur) if cur else None,
            total_estimate=int(total),
            truncated=truncated,
            warnings=warnings,
        )

    # ---- one key ------------------------------------------------------------------------

    def _need(self, r: Any, raw: bytes) -> str:
        ktype = r.type(raw)
        ktype = ktype.decode() if isinstance(ktype, bytes) else str(ktype)
        if ktype == "none":
            raise NotFound("Key not found (it may have expired).")
        return ktype

    def _read_value(
        self, r: Any, raw: bytes, ktype: str, cursor: str | None, limit: int, clip: int
    ) -> ValuePage:
        if cursor is not None and ktype != "stream" and not cursor.isdigit():
            raise ValidationFailed("Invalid cursor.")
        start = int(cursor or 0) if ktype != "stream" else 0
        limit = max(1, min(limit, self.limits.max_page_size))
        if ktype == "string":
            length = int(r.strlen(raw))
            body = r.getrange(raw, 0, clip - 1) if length > clip else r.get(raw)
            return ValuePage(
                _text_clip(body or b"", clip, length > clip), None, length, length > clip
            )
        if ktype == "hash":
            length = int(r.hlen(raw))
            nxt, items = r.hscan(raw, start, count=limit)
            value = {_key_text(k): _text(v, clip) for k, v in items.items()}
            return ValuePage(value, str(nxt) if nxt else None, length, False)
        if ktype == "set":
            length = int(r.scard(raw))
            nxt, members = r.sscan(raw, start, count=limit)
            return ValuePage([_text(m, clip) for m in members], str(nxt) if nxt else None, length)
        if ktype == "list":
            length = int(r.llen(raw))
            items = r.lrange(raw, start, start + limit - 1)
            more = start + len(items) < length
            return ValuePage(
                [_text(i, clip) for i in items], str(start + len(items)) if more else None, length
            )
        if ktype == "zset":
            length = int(r.zcard(raw))
            pairs = r.zrange(raw, start, start + limit - 1, withscores=True)
            more = start + len(pairs) < length
            return ValuePage(
                [[_text(m, clip), to_json_value(float(sc))] for m, sc in pairs],
                str(start + len(pairs)) if more else None,
                length,
            )
        if ktype == "stream":
            length = int(r.xlen(raw))
            lo = start_id(cursor)
            entries = r.xrange(raw, min=lo, count=limit)
            stream_value = [
                [eid.decode(), {_key_text(k): _text(v, clip) for k, v in fields.items()}]
                for eid, fields in entries
            ]
            nxt_id = f"({entries[-1][0].decode()}" if len(entries) == limit else None
            return ValuePage(stream_value, nxt_id, length)
        raise ValidationFailed(f"Unsupported key type {ktype!r}.")

    def get_key(
        self, database: str, key: str, *, cursor: str | None, limit: int, full: bool = False
    ) -> KeyDetail:
        index = self._index(database)
        raw = decode_key(key)
        clip = self.limits.max_cell_fetch_chars if full else self.limits.max_cell_chars
        with self._client(index) as r:
            ktype = self._need(r, raw)
            page = self._read_value(r, raw, ktype, cursor, limit, clip)
            pipe = r.pipeline(transaction=False)
            pipe.pttl(raw)
            pipe.object("encoding", raw)
            pipe.memory_usage(raw)
            pipe.dump(raw)
            ttl, enc, size, dump = pipe.execute(raise_on_error=False)
        if isinstance(dump, Exception):
            dump = None
        encoding = enc.decode() if isinstance(enc, bytes) else None
        editable = (
            ktype in ("string", "hash", "list", "set", "zset")
            and (page.length or 0) <= (EDIT_MAX_BYTES if ktype == "string" else EDIT_MAX_ELEMENTS)
            and not _has_binary(page.value)
            and self.spec.allow_writes
        )
        ok = _utf8(raw)
        return KeyDetail(
            key=key,
            name=raw.decode("utf-8") if ok else raw.decode("utf-8", "backslashreplace"),
            type=ktype,
            ttl_ms=int(ttl) if isinstance(ttl, int) and ttl >= 0 else None,
            encoding=encoding,
            size_bytes=int(size) if isinstance(size, int) else None,
            version=_version(dump),
            editable=bool(editable),
            value=page,
            binary=not ok,
        )

    # ---- writes -------------------------------------------------------------------------

    def _assert_writable(self) -> None:
        if not self.spec.allow_writes:
            raise Forbidden("This connection is read-only.")

    @staticmethod
    def _members(ktype: str, value: Any) -> Any:
        """Validate and encode a value for a new/replaced key."""

        def s(x: Any, what: str) -> bytes:
            if not isinstance(x, str):
                raise ValidationFailed(f"{what} must be text.")
            b = x.encode()
            if len(b) > EDIT_MAX_BYTES or "\x00" in x:
                raise ValidationFailed(f"{what} is too large or contains a NUL character.")
            return b

        if ktype == "string":
            return s(value, "The value")
        if ktype == "hash":
            if not isinstance(value, dict) or not value:
                raise ValidationFailed("A hash needs at least one field.")
            if len(value) > EDIT_MAX_ELEMENTS:
                raise ValidationFailed(f"At most {EDIT_MAX_ELEMENTS} fields can be written.")
            return {s(k, "A field name"): s(v, "A field value") for k, v in value.items()}
        if ktype in ("list", "set"):
            if not isinstance(value, list) or not value:
                raise ValidationFailed(f"A {ktype} needs at least one element.")
            if len(value) > EDIT_MAX_ELEMENTS:
                raise ValidationFailed(f"At most {EDIT_MAX_ELEMENTS} elements can be written.")
            return [s(v, "An element") for v in value]
        if ktype == "zset":
            if not isinstance(value, list) or not value:
                raise ValidationFailed("A sorted set needs at least one member.")
            if len(value) > EDIT_MAX_ELEMENTS:
                raise ValidationFailed(f"At most {EDIT_MAX_ELEMENTS} members can be written.")
            out: dict[bytes, float] = {}
            for item in value:
                if not (isinstance(item, list | tuple) and len(item) == 2):
                    raise ValidationFailed("Each member is [member, score].")
                try:
                    score = float(item[1])
                except (TypeError, ValueError) as exc:
                    raise ValidationFailed("A score must be a number.") from exc
                if score != score:  # NaN
                    raise ValidationFailed("A score must be a number.")
                out[s(item[0], "A member")] = score
            return out
        raise ValidationFailed(f"Keys of type {ktype!r} cannot be written here.")

    def put_key(
        self,
        database: str,
        key: str | None,
        *,
        new_name: str | None,
        type: str,
        value: Any,
        ttl_ms: int | None,
        keep_ttl: bool,
        version: str | None,
    ) -> KeyDetail:
        self._assert_writable()
        index = self._index(database)
        if key is None:
            if not new_name or "\x00" in new_name or len(new_name.encode()) > 4096:
                raise ValidationFailed("A new key needs a name.")
            raw = new_name.encode()
        else:
            raw = decode_key(key)
            if version is None:
                raise ValidationFailed("Replacing a key needs the version from the last read.")
        if ttl_ms is not None and not (0 < ttl_ms <= 10 * 365 * 86_400_000):
            raise ValidationFailed("Invalid TTL.")
        data = self._members(type, value)
        with self._client(index) as r, r.pipeline() as pipe:
            try:
                pipe.watch(raw)
                exists = bool(pipe.exists(raw))
                if key is None:
                    if exists:
                        raise Conflict("A key with this name already exists.")
                    old_ttl = -1
                else:
                    if not exists:
                        raise NotFound("Key not found (it may have expired).")
                    current = pipe.type(raw)
                    current = current.decode() if isinstance(current, bytes) else str(current)
                    if current != type:
                        raise ValidationFailed(
                            f"The key is a {current}; its type cannot be changed here."
                        )
                    if _version(pipe.dump(raw)) != version:
                        raise Conflict("The key was changed by someone else. Reload and retry.")
                    old_ttl = int(pipe.pttl(raw))
                pipe.multi()
                pipe.delete(raw)
                if type == "string":
                    pipe.set(raw, data)
                elif type == "hash":
                    pipe.hset(raw, mapping=data)
                elif type == "list":
                    pipe.rpush(raw, *data)
                elif type == "set":
                    pipe.sadd(raw, *data)
                else:
                    pipe.zadd(raw, data)
                if ttl_ms is not None:
                    pipe.pexpire(raw, ttl_ms)
                elif keep_ttl and old_ttl > 0:
                    pipe.pexpire(raw, old_ttl)
                pipe.execute()
            except redis.exceptions.WatchError as exc:
                raise Conflict("The key was changed by someone else. Reload and retry.") from exc
        return self.get_key(database, encode_key(raw), cursor=None, limit=100)

    def delete_key(self, database: str, key: str, version: str | None) -> None:
        self._assert_writable()
        index = self._index(database)
        raw = decode_key(key)
        with self._client(index) as r, r.pipeline() as pipe:
            try:
                pipe.watch(raw)
                if not pipe.exists(raw):
                    raise NotFound("Key not found (it may have expired).")
                if version is not None and _version(pipe.dump(raw)) != version:
                    raise Conflict("The key was changed by someone else. Reload and retry.")
                pipe.multi()
                pipe.unlink(raw)
                pipe.execute()
            except redis.exceptions.WatchError as exc:
                raise Conflict("The key was changed by someone else. Reload and retry.") from exc

    def set_ttl(self, database: str, key: str, ttl_ms: int | None) -> None:
        self._assert_writable()
        index = self._index(database)
        raw = decode_key(key)
        if ttl_ms is not None and not (0 < ttl_ms <= 10 * 365 * 86_400_000):
            raise ValidationFailed("Invalid TTL.")
        with self._client(index) as r:
            self._need(r, raw)
            if ttl_ms is None:
                r.persist(raw)
            else:
                r.pexpire(raw, ttl_ms)

    # ---- console ------------------------------------------------------------------------

    def _server_flags(self, r: Any, name: str) -> frozenset[str] | None:
        if name not in self._flags:
            flags: frozenset[str] | None = None
            try:
                info = r.execute_command("COMMAND", "INFO", name.lower())
                entry = info.get(name.lower()) if isinstance(info, dict) else None
                if entry:
                    flags = frozenset(
                        (f.decode() if isinstance(f, bytes) else str(f)).lower()
                        for f in entry["flags"]
                    )
            except Exception:  # COMMAND may be denied by the account's ACL: allowlist only
                logger.info("redis_command_info_unavailable", extra={"command": name})
            self._flags[name] = flags
        return self._flags[name]

    def run_command(self, database: str, args: list[bytes], *, write: bool) -> CommandResult:
        name, kind = cmd.classify(args)  # allowlist again, whatever the caller did
        if (kind == "write") != write:
            raise ValidationFailed("The command does not match the requested access.")
        if kind == "write":
            self._assert_writable()
        index = self._index(database)
        started = time.perf_counter()
        with self._client(index) as r:
            flags = self._server_flags(r, name)
            if (
                kind == "read"
                and flags is not None
                and not cmd.server_says_safe_to_read(name, flags)
            ):
                raise Forbidden(f"The server flags {name} as a write or admin command.")
            if kind == "write" and flags is not None and "write" not in flags:
                raise Forbidden(f"The server does not flag {name} as a write command.")
            reply = r.execute_command(*args)
        limit = self.limits.max_result_rows
        counter = {"n": 0, "cut": False}
        out = _json_reply(reply, self.limits.max_cell_chars, limit, counter)
        return CommandResult(
            reply=out,
            truncated=bool(counter["cut"]),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            command=name,
            kind=kind,
        )


def start_id(cursor: str | None) -> str:
    if cursor is None:
        return "-"
    if cursor.startswith("(") and all(c.isdigit() or c == "-" for c in cursor[1:]):
        return cursor
    raise ValidationFailed("Invalid cursor.")


def _key_text(k: Any) -> str:
    if isinstance(k, bytes):
        return k.decode("utf-8", "backslashreplace")
    return str(k)


def _text_clip(raw: bytes, clip: int, cut: bool) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        if cut and exc.start >= len(raw) - 4:  # clipped in the middle of a character
            return to_json_value(raw[: exc.start].decode("utf-8"), clip)
        return binary_marker(raw)
    return to_json_value(text, clip)


def _has_binary(value: Any) -> bool:
    if isinstance(value, dict):
        if "$binary" in value or "$truncated" in value:
            return True
        return any(_has_binary(v) for v in value.values()) or any("$binary" in k for k in value)
    if isinstance(value, list):
        return any(_has_binary(v) for v in value)
    return False


def _json_reply(reply: Any, clip: int, limit: int, counter: dict[str, Any]) -> Any:
    counter["n"] += 1
    if counter["n"] > limit * 4:
        counter["cut"] = True
        return None
    if reply is None or isinstance(reply, bool | int):
        return reply
    if isinstance(reply, float):
        return to_json_value(reply)
    if isinstance(reply, bytes | bytearray | str):
        return _text(reply, clip)
    if isinstance(reply, dict):
        items = list(reply.items())
        if len(items) > limit:
            counter["cut"] = True
            items = items[:limit]
        return {_key_text(k): _json_reply(v, clip, limit, counter) for k, v in items}
    if isinstance(reply, list | tuple | set | frozenset):
        items = list(reply)
        if len(items) > limit:
            counter["cut"] = True
            items = items[:limit]
        return [_json_reply(v, clip, limit, counter) for v in items]
    return str(reply)
