"""Lexical classification of user-authored SQL.

This is a **policy and UX layer, never the security boundary**: read-only execution is enforced
by the database (ADR-0008). The classifier decides which permission a statement needs, whether
it is destructive enough to need confirmation, and which statements are not allowed at all
(transaction/session control would let a statement tamper with the execution context).

The scanner understands comments, quoted strings/identifiers, PostgreSQL dollar-quoting and
MySQL backslash escapes, so ``;`` or keywords inside literals are not mistaken for syntax.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Kind = Literal["read", "write", "ddl", "admin", "session", "unknown"]

READ_FIRST = {"SELECT", "VALUES", "TABLE", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"}
WRITE_FIRST = {
    "INSERT", "UPDATE", "DELETE", "MERGE", "REPLACE", "CALL", "DO", "EXECUTE", "COPY", "LOAD",
    "IMPORT",
}  # fmt: skip
DDL_FIRST = {
    "CREATE", "ALTER", "DROP", "TRUNCATE", "RENAME", "COMMENT", "REINDEX", "VACUUM", "ANALYZE",
    "CLUSTER", "REFRESH", "OPTIMIZE", "REPAIR",
}  # fmt: skip
ADMIN_FIRST = {"GRANT", "REVOKE", "SECURITY", "ATTACH", "DETACH", "INSTALL"}
SESSION_FIRST = {
    "BEGIN", "START", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE", "END", "ABORT",
    "SET", "RESET", "DISCARD", "USE", "LOCK", "UNLOCK", "PREPARE", "DEALLOCATE", "LISTEN",
    "UNLISTEN", "NOTIFY", "KILL", "FLUSH", "RESTART", "SHUTDOWN",
}  # fmt: skip
DML_WORDS = {"INSERT", "UPDATE", "DELETE", "MERGE"}
EXPLAIN_OPTIONS = {
    "ANALYZE", "ANALYSE", "VERBOSE", "COSTS", "BUFFERS", "FORMAT", "TIMING", "SUMMARY",
    "SETTINGS", "WAL", "JSON", "XML", "YAML", "TEXT", "TRUE", "FALSE", "ON", "OFF", "TREE",
}  # fmt: skip


@dataclass(frozen=True)
class Statement:
    text: str
    kind: Kind
    destructive: bool
    verb: str


def _scan(sql: str) -> list[tuple[str, str]]:
    """Tokenise into ``(type, text)``; types: space, comment, string, ident, word, number,
    semi, punct."""
    out: list[tuple[str, str]] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            j = i
            while j < n and sql[j].isspace():
                j += 1
            out.append(("space", sql[i:j]))
            i = j
        elif sql.startswith("--", i) or c == "#":
            j = sql.find("\n", i)
            j = n if j < 0 else j
            out.append(("comment", sql[i:j]))
            i = j
        elif sql.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif sql.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            out.append(("comment", sql[i:j]))
            i = j
        elif c in ("'", '"', "`"):
            j = i + 1
            while j < n:
                if sql[j] == "\\" and c != "`":
                    j += 2
                    continue
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            out.append(("string" if c == "'" else "ident", sql[i : j + 1]))
            i = j + 1
        elif c == "$" and (m := re.match(r"\$[A-Za-z_]*\$", sql[i:])):
            tag = m.group(0)
            j = sql.find(tag, i + len(tag))
            j = n if j < 0 else j + len(tag)
            out.append(("string", sql[i:j]))
            i = j
        elif c.isdigit():
            m2 = re.match(r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", sql[i:])
            assert m2
            out.append(("number", m2.group(0)))
            i += len(m2.group(0))
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (sql[j].isalnum() or sql[j] in "_$"):
                j += 1
            out.append(("word", sql[i:j]))
            i = j
        elif c == ";":
            out.append(("semi", c))
            i += 1
        else:
            out.append(("punct", c))
            i += 1
    return out


def split_statements(sql: str) -> list[str]:
    """Split on top-level semicolons. Empty statements and bare comments are dropped."""
    parts: list[list[tuple[str, str]]] = [[]]
    for tok in _scan(sql):
        if tok[0] == "semi":
            parts.append([])
        else:
            parts[-1].append(tok)
    out: list[str] = []
    for toks in parts:
        if any(t[0] not in ("space", "comment") for t in toks):
            out.append("".join(t[1] for t in toks).strip())
    return out


def _words(stmt: str) -> list[str]:
    return [t[1].upper() for t in _scan(stmt) if t[0] == "word"]


def classify_one(stmt: str) -> Statement:
    words = _words(stmt)
    if not words:
        return Statement(stmt, "unknown", False, "")
    verb = words[0]
    kind: Kind
    if verb == "WITH":
        kind = "write" if DML_WORDS & set(words) else "read"
    elif verb == "EXPLAIN":
        rest = words[1:]
        inner = next((w for w in rest if w not in EXPLAIN_OPTIONS), "SELECT")
        analyzes = bool({"ANALYZE", "ANALYSE"} & set(rest))
        kind = "read" if inner in READ_FIRST | {"WITH"} or not analyzes else "write"
    elif verb in READ_FIRST:
        # SELECT ... INTO creates a table (PostgreSQL) or writes a file/variable (MySQL)
        kind = "write" if verb == "SELECT" and "INTO" in words else "read"
    elif verb in SESSION_FIRST:
        kind = "session"
    elif verb in WRITE_FIRST:
        kind = "write"
    elif verb in DDL_FIRST:
        kind = "ddl"
    elif verb in ADMIN_FIRST:
        kind = "admin"
    else:
        kind = "unknown"
    return Statement(stmt, kind, _destructive(verb, words), verb)


def _destructive(verb: str, words: list[str]) -> bool:
    if verb in ("DROP", "TRUNCATE", "REVOKE"):
        return True
    if verb in ("DELETE", "UPDATE"):
        return "WHERE" not in words
    return verb == "ALTER" and "DROP" in words


def classify(sql: str) -> list[Statement]:
    return [classify_one(s) for s in split_statements(sql)]


def redact_literals(sql: str) -> str:
    """Replace string and numeric literals with ``?`` (keeps structure, drops values)."""
    return "".join("?" if typ in ("string", "number") else text for typ, text in _scan(sql))
