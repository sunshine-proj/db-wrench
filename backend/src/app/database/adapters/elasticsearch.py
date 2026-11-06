"""Elasticsearch / OpenSearch adapter over the REST API (httpx). ADR-0022.

Only a fixed set of endpoints is ever called (cluster info, ``_cat``, mapping/settings reads,
single-document CRUD, ``_search`` and index create/delete). Index names always come from the
cluster's own listing (or a strict pattern for new indices), document ids travel as opaque ids,
and user-supplied search bodies are validated first (``search/dsl.py``). Every request validates
the destination and connects to the validated address.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import ssl
import tempfile
import threading
import time
from typing import Any
from urllib.parse import quote

import httpx

from app.database.core.base import Capabilities, TestResult, TlsMode
from app.database.core.serialization import decode_opaque, encode_opaque, to_json_value
from app.database.search import dsl
from app.database.search.base import SearchAdapter
from app.database.search.models import (
    DocHit,
    FieldInfo,
    SearchIndexDetail,
    SearchIndexInfo,
    SearchPage,
    SearchResult,
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
from app.observability import scrub

NEW_INDEX = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,254}$")
MAX_SOURCE_BYTES = 1 << 20
MAX_ID_BYTES = 512
SETTINGS_SHOWN = (
    "number_of_shards", "number_of_replicas", "refresh_interval", "creation_date", "uuid",
    "max_result_window", "analysis",
)  # fmt: skip
SORT_DIRS = ("asc", "desc")


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def flatten_mapping(props: dict[str, Any], prefix: str = "") -> list[FieldInfo]:
    out: list[FieldInfo] = []
    for name, spec in sorted(props.items()):
        if not isinstance(spec, dict):
            continue
        path = f"{prefix}{name}"
        if "properties" in spec:
            out.append(FieldInfo(path, spec.get("type", "object")))
            out.extend(flatten_mapping(spec["properties"], f"{path}."))
        else:
            out.append(FieldInfo(path, str(spec.get("type", "object"))))
        for sub, subspec in sorted((spec.get("fields") or {}).items()):
            out.append(FieldInfo(f"{path}.{sub}", str(subspec.get("type", "?"))))
    return out


class ElasticsearchAdapter(SearchAdapter):
    capabilities = Capabilities(
        family="search",
        engine="elasticsearch",
        supports_schemas=False,
        supports_views=False,
        supports_indexes=False,
        supports_read_only_transactions=False,
        supports_cancellation=False,
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._client_obj: httpx.Client | None = None
        self._lock = threading.Lock()
        self._ca_path: str | None = None
        self._cache: dict[str, tuple[float, Any]] = {}

    # ---- transport --------------------------------------------------------------------

    def _ssl_context(self) -> ssl.SSLContext | bool:
        s = self.spec
        if s.tls_mode == TlsMode.DISABLE:
            return False
        ctx = ssl.create_default_context()
        if s.tls_mode == TlsMode.REQUIRE:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        if s.tls_ca:
            if self._ca_path is None:
                fd, path = tempfile.mkstemp(prefix="dbw-ca-", suffix=".pem")
                with os.fdopen(fd, "w") as fh:
                    fh.write(s.tls_ca)
                self._ca_path = path
            ctx.load_verify_locations(self._ca_path)
        ctx.check_hostname = s.tls_mode == TlsMode.VERIFY_FULL
        return ctx

    def _client(self) -> httpx.Client:
        with self._lock:
            if self._client_obj is None:
                self._client_obj = httpx.Client(
                    verify=self._ssl_context(),
                    timeout=httpx.Timeout(
                        self.limits.statement_timeout_s + 10, connect=self.spec.connect_timeout_s
                    ),
                    limits=httpx.Limits(max_connections=max(1, self.spec.pool_max)),
                    follow_redirects=False,
                )
            return self._client_obj

    def _auth_header(self) -> dict[str, str]:
        s = self.spec
        if s.username:
            import base64

            raw = base64.b64encode(f"{s.username}:{s.password}".encode()).decode()
            return {"Authorization": f"Basic {raw}"}
        if s.password:  # no user name: the secret is an API key
            return {"Authorization": f"ApiKey {s.password}"}
        return {}

    def _fail(self, status: int, body: Any) -> Exception:
        err = body.get("error") if isinstance(body, dict) else None
        kind = err.get("type", "") if isinstance(err, dict) else ""
        reason = err.get("reason", "") if isinstance(err, dict) else str(err or "")
        msg = scrub(str(reason)[:500], self.spec.password) or f"HTTP {status}"
        if status == 401:
            return ConnectionFailed("Authentication failed.")
        if status == 404 or "index_not_found" in kind:
            return NotFound(msg if "index" in msg.lower() else "Not found.")
        if status == 409 or "version_conflict" in kind:
            return Conflict("The document was changed by someone else. Reload and retry.")
        if status == 408 or "timeout" in kind:
            return StatementTimeout()
        if status == 403 or "security_exception" in kind:
            return QueryFailed(f"Not permitted for this account: {msg}")
        return QueryFailed(msg)

    def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        timeout_s: int | None = None,
    ) -> Any:
        s = self.spec
        pinned = self.policy.resolve(s.host, s.port)[0]  # validated on every request
        host = f"[{pinned}]" if ":" in pinned else pinned
        scheme = "http" if s.tls_mode == TlsMode.DISABLE else "https"
        headers = {
            "Host": f"{s.host}:{s.port}",
            "Accept": "application/json",
            **self._auth_header(),
        }
        ext: dict[str, Any] = {}
        if scheme == "https" and s.tls_mode != TlsMode.REQUIRE:
            ext["sni_hostname"] = s.host
        timeout = timeout_s or self.limits.statement_timeout_s
        content = None
        if body is not None:
            content = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            r = self._client().request(
                method,
                f"{scheme}://{host}:{s.port}{path}",
                params={
                    k: str(v).lower() if isinstance(v, bool) else v
                    for k, v in (params or {}).items()
                },
                headers=headers,
                content=content,
                extensions=ext,
                timeout=httpx.Timeout(timeout + 5, connect=s.connect_timeout_s),
            )
        except httpx.ReadTimeout as exc:
            raise StatementTimeout() from exc
        except httpx.TimeoutException as exc:
            raise ConnectionFailed("Timed out connecting to the search cluster.") from exc
        except (httpx.TransportError, ssl.SSLError, OSError) as exc:
            raise ConnectionFailed(scrub(str(exc), s.password)) from exc
        try:
            data = r.json() if r.content else None
        except json.JSONDecodeError:
            data = None if r.is_success else {"error": r.text[:300]}
        if r.status_code >= 400:
            raise self._fail(r.status_code, data)
        return data

    def close(self) -> None:
        with self._lock:
            client, self._client_obj = self._client_obj, None
        if client:
            client.close()
        if self._ca_path:
            with contextlib.suppress(OSError):
                os.unlink(self._ca_path)
            self._ca_path = None

    def test(self) -> TestResult:
        started = time.perf_counter()
        try:
            info = self._call("GET", "/")
        except Exception as exc:
            code = getattr(exc, "code", "connection_failed")
            return TestResult(False, error=getattr(exc, "detail", str(exc)), error_code=code)
        v = (info or {}).get("version", {})
        name = "OpenSearch" if v.get("distribution") == "opensearch" else "Elasticsearch"
        return TestResult(
            True, f"{name} {v.get('number', '?')}", round((time.perf_counter() - started) * 1000, 1)
        )

    # ---- indices ----------------------------------------------------------------------

    def _cached(self, key: str, loader: Any) -> Any:
        hit = self._cache.get(key)
        now = time.monotonic()
        if hit and now - hit[0] < 30:
            return hit[1]
        value = loader()
        self._cache[key] = (now, value)
        return value

    def _forget(self) -> None:
        self._cache.clear()

    def list_indices(self) -> list[SearchIndexInfo]:
        def load() -> list[SearchIndexInfo]:
            rows = self._call(
                "GET",
                "/_cat/indices",
                params={
                    "format": "json",
                    "h": "index,health,status,docs.count,store.size,pri,rep",
                    "bytes": "b",
                    "expand_wildcards": "open",
                },
            )
            aliases: dict[str, list[str]] = {}
            for a in self._call(
                "GET", "/_cat/aliases", params={"format": "json", "h": "alias,index"}
            ):
                aliases.setdefault(a["index"], []).append(a["alias"])
            return sorted(
                (
                    SearchIndexInfo(
                        name=r["index"],
                        health=r.get("health"),
                        status=r.get("status"),
                        docs=_int(r.get("docs.count")),
                        size_bytes=_int(r.get("store.size")),
                        primaries=_int(r.get("pri")),
                        replicas=_int(r.get("rep")),
                        aliases=sorted(
                            a for a in aliases.get(r["index"], []) if not a.startswith(".")
                        ),
                    )
                    for r in rows
                    if not r["index"].startswith(".")  # system and hidden indices stay hidden
                ),
                key=lambda i: i.name,
            )

        return self._cached("indices", load)

    def _require_index(self, index: str) -> str:
        known = {i.name for i in self.list_indices()}
        if index not in known:
            raise NotFound(f"Index {index!r} was not found.")
        return index

    def get_index(self, index: str) -> SearchIndexDetail:
        self._require_index(index)
        q = quote(index, safe="")
        mapping = self._call("GET", f"/{q}/_mapping")
        settings = self._call("GET", f"/{q}/_settings")
        info = next(i for i in self.list_indices() if i.name == index)
        props = (mapping.get(index, {}).get("mappings", {}) or {}).get("properties", {})
        raw = settings.get(index, {}).get("settings", {}).get("index", {})
        shown = {k: raw[k] for k in SETTINGS_SHOWN if k in raw}
        return SearchIndexDetail(
            name=index,
            fields=flatten_mapping(props),
            settings=shown,
            aliases=info.aliases,
            docs=info.docs,
            size_bytes=info.size_bytes,
        )

    def create_index(
        self, name: str, *, shards: int, replicas: int, mappings: dict[str, Any] | None
    ) -> None:
        self._assert_writable()
        if not NEW_INDEX.fullmatch(name):
            raise ValidationFailed(
                "Index names are lower case: letters, digits, '_' or '-', not starting with a symbol."
            )
        if not 1 <= shards <= 20 or not 0 <= replicas <= 5:
            raise ValidationFailed("Use 1-20 shards and 0-5 replicas.")
        if name in {i.name for i in self.list_indices()}:
            raise Conflict(f"Index {name!r} already exists.")
        body: dict[str, Any] = {
            "settings": {"number_of_shards": shards, "number_of_replicas": replicas}
        }
        maps = dsl.validate_mappings(mappings)
        if maps:
            body["mappings"] = maps
        self._call("PUT", f"/{quote(name, safe='')}", body=body)
        self._forget()

    def delete_index(self, name: str) -> None:
        self._assert_writable()
        self._require_index(name)
        self._call(
            "DELETE",
            f"/{quote(name, safe='')}",
            params={"allow_no_indices": False, "expand_wildcards": "none"},
        )
        self._forget()

    # ---- documents --------------------------------------------------------------------

    def _assert_writable(self) -> None:
        if not self.spec.allow_writes:
            raise Forbidden("This connection is read-only.")

    def _hit(self, h: dict[str, Any]) -> DocHit:
        doc_id = str(h["_id"])
        src = h.get("_source")
        return DocHit(
            key=encode_opaque(doc_id.encode()),
            id=doc_id,
            source=to_json_value(src if isinstance(src, dict) else {}, self.limits.max_cell_chars),
            score=h.get("_score"),
            seq_no=h.get("_seq_no"),
            primary_term=h.get("_primary_term"),
            sort=to_json_value(h.get("sort")) if h.get("sort") is not None else None,
        )

    def search_documents(
        self,
        index: str,
        *,
        text: str | None,
        sort: list[tuple[str, str]],
        offset: int,
        size: int,
    ) -> SearchPage:
        self._require_index(index)
        size = max(1, min(size, self.limits.max_page_size))
        if offset < 0 or offset + size > dsl.MAX_WINDOW:
            raise ValidationFailed(
                f"Paging is limited to the first {dsl.MAX_WINDOW:,} matches; add a search term."
            )
        known = {f.path for f in self.get_index(index).fields}
        order: list[Any] = []
        for field, direction in sort:
            if field not in known or direction not in SORT_DIRS:
                raise ValidationFailed(f"Cannot sort by {field!r}.")
            order.append({field: {"order": direction, "unmapped_type": "keyword"}})
        body: dict[str, Any] = {
            "from": offset,
            "size": size + 1,  # one extra tells whether another page exists
            "seq_no_primary_term": True,
            "track_total_hits": dsl.MAX_WINDOW,
            "timeout": f"{self.limits.statement_timeout_s}s",
            "query": {"match_all": {}},
        }
        if text:
            if len(text) > 1000 or "\x00" in text:
                raise ValidationFailed("The search text is too long.")
            body["query"] = {
                "simple_query_string": {
                    "query": text,
                    "fields": ["*"],
                    "lenient": True,
                    "default_operator": "and",
                }  # fmt: skip
            }
        if order:
            body["sort"] = order
        res = self._call(
            "POST", f"/{quote(index, safe='')}/_search", body=body,
            params={"allow_no_indices": False, "expand_wildcards": "none"},
        )  # fmt: skip
        hits = res["hits"]["hits"]
        more = len(hits) > size
        total = res["hits"].get("total") or {}
        return SearchPage(
            hits=[self._hit(h) for h in hits[:size]],
            total=total.get("value") if isinstance(total, dict) else None,
            total_relation=total.get("relation", "eq") if isinstance(total, dict) else "eq",
            took_ms=int(res.get("took", 0)),
            offset=offset,
            size=size,
            next_cursor=str(offset + size) if more and offset + size < dsl.MAX_WINDOW else None,
            truncated=more and offset + size >= dsl.MAX_WINDOW,
            timed_out=bool(res.get("timed_out")),
        )

    def get_document(self, index: str, key: str) -> DocHit:
        self._require_index(index)
        doc_id = decode_opaque(key, max_len=1024).decode("utf-8", "strict")
        res = self._call(
            "GET", f"/{quote(index, safe='')}/_doc/{quote(doc_id, safe='')}",
        )  # fmt: skip
        if not res or not res.get("found", True):
            raise NotFound("Document not found.")
        return self._hit(res)

    def put_document(
        self,
        index: str,
        key: str | None,
        *,
        new_id: str | None,
        source: dict[str, Any],
        seq_no: int | None,
        primary_term: int | None,
    ) -> DocHit:
        self._assert_writable()
        self._require_index(index)
        if not isinstance(source, dict):
            raise ValidationFailed("A document must be a JSON object.")
        if len(json.dumps(source)) > MAX_SOURCE_BYTES:
            raise ValidationFailed("The document is too large (1 MiB maximum).")
        q = quote(index, safe="")
        params: dict[str, Any] = {"refresh": "wait_for"}
        if key is None:
            if new_id:
                if len(new_id.encode()) > MAX_ID_BYTES or "\x00" in new_id:
                    raise ValidationFailed("Invalid document id.")
                res = self._call(
                    "PUT", f"/{q}/_create/{quote(new_id, safe='')}", body=source, params=params
                )
            else:
                res = self._call("POST", f"/{q}/_doc", body=source, params=params)
        else:
            if seq_no is None or primary_term is None:
                raise ValidationFailed("Replacing a document needs seq_no and primary_term.")
            doc_id = decode_opaque(key, max_len=1024).decode()
            params.update(if_seq_no=seq_no, if_primary_term=primary_term)
            res = self._call(
                "PUT", f"/{q}/_doc/{quote(doc_id, safe='')}", body=source, params=params
            )
        return self.get_document(index, encode_opaque(str(res["_id"]).encode()))

    def delete_document(
        self, index: str, key: str, seq_no: int | None, primary_term: int | None
    ) -> None:
        self._assert_writable()
        self._require_index(index)
        doc_id = decode_opaque(key, max_len=1024).decode()
        params: dict[str, Any] = {"refresh": "wait_for"}
        if seq_no is not None and primary_term is not None:
            params.update(if_seq_no=seq_no, if_primary_term=primary_term)
        self._call(
            "DELETE", f"/{quote(index, safe='')}/_doc/{quote(doc_id, safe='')}", params=params
        )

    # ---- console ----------------------------------------------------------------------

    def run_search(self, index: str, body: dict[str, Any], *, timeout_s: int) -> SearchResult:
        self._require_index(index)
        safe = dsl.validate_search_body(body, max_size=self.limits.max_result_rows)
        safe.setdefault("timeout", f"{timeout_s}s")
        res = self._call(
            "POST", f"/{quote(index, safe='')}/_search", body=safe, timeout_s=timeout_s,
            params={"allow_no_indices": False, "expand_wildcards": "none"},
        )  # fmt: skip
        text = json.dumps(res)
        truncated = len(text) > self.limits.max_response_bytes
        if truncated:
            res["hits"]["hits"] = res["hits"]["hits"][: max(1, len(res["hits"]["hits"]) // 10)]
        return SearchResult(
            body=to_json_value(res, self.limits.max_cell_chars),
            took_ms=int(res.get("took", 0)),
            truncated=truncated,
        )
