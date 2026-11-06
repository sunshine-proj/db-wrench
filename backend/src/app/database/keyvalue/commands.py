"""Redis command policy for the console (ADR-0019).

Redis has no read-only session or transaction, so the policy is two independent checks:

1. an **allowlist** of commands, split into read and write (anything else is refused:
   scripting, ``CONFIG``, ``DEBUG``, ``FLUSH*``, blocking commands, replication, ``ACL``...);
2. the **server's own command table**: a command on the read list is only run when the server
   (``COMMAND INFO``) does not flag it as ``write``/``admin``/``blocking``/``scripting``-capable.
   This catches version differences (e.g. a command that gained a ``STORE`` option).
"""

from __future__ import annotations

import re

from app.errors import StatementNotAllowed, ValidationFailed

READ_COMMANDS = frozenset(
    {
        "PING", "ECHO", "GET", "MGET", "STRLEN", "GETRANGE", "EXISTS", "TYPE", "TTL", "PTTL",
        "EXPIRETIME", "PEXPIRETIME", "DBSIZE", "SCAN", "SSCAN", "HSCAN", "ZSCAN", "HGET",
        "HMGET", "HGETALL", "HKEYS", "HVALS", "HLEN", "HEXISTS", "HSTRLEN", "HRANDFIELD",
        "LRANGE", "LLEN", "LINDEX", "LPOS", "SMEMBERS", "SCARD", "SISMEMBER", "SMISMEMBER",
        "SRANDMEMBER", "SINTER", "SUNION", "SDIFF", "ZRANGE", "ZRANGEBYSCORE", "ZREVRANGE",
        "ZREVRANGEBYSCORE", "ZRANGEBYLEX", "ZCARD", "ZSCORE", "ZMSCORE", "ZRANK", "ZREVRANK",
        "ZCOUNT", "ZLEXCOUNT", "ZRANDMEMBER", "XRANGE", "XREVRANGE", "XLEN", "XREAD", "BITCOUNT",
        "BITPOS", "GETBIT", "PFCOUNT", "GEOPOS", "GEODIST", "GEOHASH", "GEOSEARCH", "SORT_RO",
        "OBJECT", "MEMORY", "INFO", "TIME", "RANDOMKEY", "LOLWUT",
    }
)  # fmt: skip
WRITE_COMMANDS = frozenset(
    {
        "SET", "SETEX", "PSETEX", "SETNX", "MSET", "MSETNX", "APPEND", "SETRANGE", "GETSET",
        "GETDEL", "INCR", "DECR", "INCRBY", "DECRBY", "INCRBYFLOAT", "DEL", "UNLINK", "EXPIRE",
        "PEXPIRE", "EXPIREAT", "PEXPIREAT", "PERSIST", "RENAME", "RENAMENX", "HSET", "HSETNX",
        "HMSET", "HDEL", "HINCRBY", "HINCRBYFLOAT", "LPUSH", "RPUSH", "LPUSHX", "RPUSHX", "LPOP",
        "RPOP", "LSET", "LREM", "LTRIM", "LINSERT", "SADD", "SREM", "SPOP", "SMOVE", "ZADD",
        "ZREM", "ZINCRBY", "ZREMRANGEBYRANK", "ZREMRANGEBYSCORE", "XADD", "XDEL", "XTRIM",
        "SETBIT", "PFADD", "GEOADD", "TOUCH",
    }
)  # fmt: skip
# Writes that destroy or overwrite data: need an explicit confirmation.
DESTRUCTIVE = frozenset(
    {
        "DEL", "UNLINK", "GETDEL", "RENAME", "LTRIM", "LREM", "SPOP", "LPOP", "RPOP", "XTRIM",
        "ZREMRANGEBYRANK", "ZREMRANGEBYSCORE", "SET", "MSET", "GETSET", "SETRANGE", "HDEL",
        "SREM", "ZREM", "XDEL",
    }
)  # fmt: skip
# Sub-commands that are fine on otherwise allowed commands.
ALLOWED_SUB = {
    "OBJECT": {"ENCODING", "FREQ", "IDLETIME", "REFCOUNT"},
    "MEMORY": {"USAGE"},
    "INFO": None,  # any section, read-only
}
# Flags (from COMMAND INFO) that disqualify a command from the read path.
UNSAFE_FLAGS = frozenset({"write", "admin", "blocking"})
MAX_ARGS = 64
MAX_ARG_BYTES = 1 << 20
MAX_LINE = 64 * 1024

_ESC = {"n": b"\n", "r": b"\r", "t": b"\t", "b": b"\b", "a": b"\a", "\\": b"\\", '"': b'"'}


def tokenize(line: str) -> list[bytes]:
    """Split a console line like ``redis-cli`` does (quotes, ``\\n``, ``\\xHH``)."""
    if len(line) > MAX_LINE or "\x00" in line:
        raise ValidationFailed("The command is too long or contains a NUL character.")
    out: list[bytes] = []
    i, n = 0, len(line)
    while i < n:
        if line[i].isspace():
            i += 1
            continue
        buf = bytearray()
        if line[i] == '"':
            i += 1
            while True:
                if i >= n:
                    raise ValidationFailed("Unterminated double quote.")
                c = line[i]
                if c == "\\" and i + 1 < n:
                    nxt = line[i + 1]
                    if nxt == "x" and re.fullmatch(r"[0-9a-fA-F]{2}", line[i + 2 : i + 4]):
                        buf.append(int(line[i + 2 : i + 4], 16))
                        i += 4
                        continue
                    buf += _ESC.get(nxt, nxt.encode())
                    i += 2
                    continue
                if c == '"':
                    i += 1
                    break
                buf += c.encode()
                i += 1
        elif line[i] == "'":
            i += 1
            while True:
                if i >= n:
                    raise ValidationFailed("Unterminated single quote.")
                if line[i] == "\\" and line[i + 1 : i + 2] == "'":
                    buf += b"'"
                    i += 2
                    continue
                if line[i] == "'":
                    i += 1
                    break
                buf += line[i].encode()
                i += 1
        else:
            while i < n and not line[i].isspace():
                buf += line[i].encode()
                i += 1
        out.append(bytes(buf))
        if len(out) > MAX_ARGS:
            raise ValidationFailed(f"At most {MAX_ARGS} arguments are allowed.")
    if not out:
        raise ValidationFailed("Enter a command.")
    if any(len(a) > MAX_ARG_BYTES for a in out):
        raise ValidationFailed("An argument is too large.")
    return out


def classify(args: list[bytes]) -> tuple[str, str]:
    """``(COMMAND, kind)`` for an allowed command, else :class:`StatementNotAllowed`."""
    name = args[0].decode("ascii", "replace").upper()
    if name in READ_COMMANDS:
        sub = ALLOWED_SUB.get(name)
        if name in ALLOWED_SUB and sub is not None:
            word = args[1].decode("ascii", "replace").upper() if len(args) > 1 else ""
            if word not in sub:
                raise StatementNotAllowed(f"{name} {word or '(no sub-command)'} is not allowed.")
        if name == "XREAD" and any(a.upper() in (b"BLOCK",) for a in args[1:]):
            raise StatementNotAllowed("Blocking reads are not allowed.")
        if name == "ZRANGE" and any(a.upper() == b"STORE" for a in args[1:]):
            raise StatementNotAllowed("STORE is not allowed.")
        if name == "GEOSEARCH" and any(a.upper() == b"STORE" for a in args[1:]):
            raise StatementNotAllowed("STORE is not allowed.")
        return name, "read"
    if name in WRITE_COMMANDS:
        return name, "write"
    raise StatementNotAllowed(f"The command {name!r} is not allowed in the console.")


def server_says_safe_to_read(name: str, flags: frozenset[str]) -> bool:
    return not (flags & UNSAFE_FLAGS)
