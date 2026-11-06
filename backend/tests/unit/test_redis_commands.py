"""Redis console parsing and the command allowlist (no server needed)."""

from __future__ import annotations

import pytest

from app.database.adapters.redis import decode_key, encode_key
from app.database.keyvalue import commands as cmd
from app.errors import StatementNotAllowed, ValidationFailed


def test_tokenizer_matches_redis_cli_quoting():
    assert cmd.tokenize("SET a b") == [b"SET", b"a", b"b"]
    assert cmd.tokenize('SET "a b" "x\\ny\\x41"') == [b"SET", b"a b", b"x\nyA"]
    assert cmd.tokenize("SET 'it\\'s' v") == [b"SET", b"it's", b"v"]
    assert cmd.tokenize("  GET   k  ") == [b"GET", b"k"]


@pytest.mark.parametrize("bad", ["", "   ", 'SET "a', "SET 'a", "GET a\x00b"])
def test_tokenizer_rejects_garbage(bad):
    with pytest.raises(ValidationFailed):
        cmd.tokenize(bad)


def test_tokenizer_limits():
    with pytest.raises(ValidationFailed):
        cmd.tokenize("DEL " + " ".join(["k"] * 100))
    with pytest.raises(ValidationFailed):
        cmd.tokenize("SET k " + "x" * (cmd.MAX_LINE + 1))


@pytest.mark.parametrize("line", ["get k", "HGETALL h", "SCAN 0 MATCH a* COUNT 10", "ttl k",
                                  "OBJECT ENCODING k", "MEMORY USAGE k", "INFO keyspace"])  # fmt: skip
def test_read_commands(line):
    assert cmd.classify(cmd.tokenize(line))[1] == "read"


@pytest.mark.parametrize("line", ["set k v", "DEL a b", "hset h f v", "Expire k 10", "ZADD z 1 m"])
def test_write_commands(line):
    assert cmd.classify(cmd.tokenize(line))[1] == "write"


@pytest.mark.security
@pytest.mark.parametrize(
    "line",
    [
        "EVAL 'return 1' 0", "EVALSHA abc 0", "FUNCTION LOAD x", "SCRIPT FLUSH", "FLUSHALL",
        "FLUSHDB", "CONFIG SET dir /tmp", "CONFIG GET *", "DEBUG SLEEP 5", "SHUTDOWN", "KEYS *",
        "MONITOR", "SUBSCRIBE c", "BLPOP l 0", "REPLICAOF host 6379", "SLAVEOF NO ONE",
        "ACL SETUSER x on", "AUTH x", "SELECT 3", "MODULE LOAD x", "MIGRATE h 1 k 0 1", "SAVE",
        "BGSAVE", "CLIENT KILL ID 1", "SORT l STORE d", "OBJECT HELP", "MEMORY PURGE",
        "XREAD BLOCK 0 STREAMS s 0", "ZRANGE a 0 -1 STORE d", "MULTI", "WATCH k", "COPY a b",
        "RESTORE k 0 x", "SWAPDB 0 1", "WAIT 1 0", "FAILOVER", "PSYNC x 1", "LATENCY RESET",
    ],
)  # fmt: skip
def test_dangerous_commands_are_refused(line):
    with pytest.raises(StatementNotAllowed):
        cmd.classify(cmd.tokenize(line))


def test_key_ids_round_trip_binary_keys():
    raw = b"\x00\xff/path with spaces/\xde\xad"
    assert decode_key(encode_key(raw)) == raw
    assert "/" not in encode_key(raw) and "=" not in encode_key(raw)


@pytest.mark.parametrize("bad", ["", "!!!", "a" * 5000])
def test_bad_key_ids(bad):
    with pytest.raises(ValidationFailed):
        decode_key(bad)


def test_unsafe_server_flags_disqualify_a_read():
    assert cmd.server_says_safe_to_read("GET", frozenset({"readonly", "fast"}))
    assert not cmd.server_says_safe_to_read("X", frozenset({"write"}))
    assert not cmd.server_says_safe_to_read("X", frozenset({"admin"}))
    assert not cmd.server_says_safe_to_read("X", frozenset({"blocking"}))
