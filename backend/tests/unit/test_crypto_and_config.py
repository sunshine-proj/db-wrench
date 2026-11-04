import base64
import json

import pytest

from app.config import Settings
from app.connections.crypto import CryptoError, Envelope, StaticKeyProvider


def make_envelope(active: str = "k1") -> Envelope:
    keys = {"k1": b"a" * 32, "k2": b"b" * 32}
    return Envelope(StaticKeyProvider(keys, active))


def test_roundtrip_and_no_plaintext_in_blob():
    env = make_envelope()
    blob = env.encrypt_json({"password": "s3cr3t-value"}, "connection:1")
    assert "s3cr3t-value" not in blob
    assert env.decrypt_json(blob, "connection:1") == {"password": "s3cr3t-value"}


def test_ciphertext_is_bound_to_its_context():
    env = make_envelope()
    blob = env.encrypt(b"x", "connection:A")
    with pytest.raises(CryptoError):
        env.decrypt(blob, "connection:B")


def test_tampered_ciphertext_is_rejected():
    env = make_envelope()
    doc = json.loads(env.encrypt(b"hello", "c"))
    ct = bytearray(base64.b64decode(doc["ct"]))
    ct[0] ^= 1
    doc["ct"] = base64.b64encode(bytes(ct)).decode()
    with pytest.raises(CryptoError):
        env.decrypt(json.dumps(doc), "c")


def test_each_encryption_uses_fresh_data_key_and_nonce():
    env = make_envelope()
    a, b = json.loads(env.encrypt(b"same", "c")), json.loads(env.encrypt(b"same", "c"))
    assert a["wk"] != b["wk"] and a["ct"] != b["ct"] and a["n"] != b["n"]


def test_rewrap_moves_to_new_kek_without_touching_payload():
    old = make_envelope("k1")
    blob = old.encrypt(b"payload", "ctx")
    assert old.kek_id(blob) == "k1"
    new = old.rewrap(blob, "ctx", "k2")
    assert old.kek_id(new) == "k2"
    assert json.loads(new)["ct"] == json.loads(blob)["ct"]
    assert old.decrypt(new, "ctx") == b"payload"
    # a deployment that only holds k2 can still read it
    only_k2 = Envelope(StaticKeyProvider({"k2": b"b" * 32}, "k2"))
    assert only_k2.decrypt(new, "ctx") == b"payload"


def test_missing_kek_is_reported():
    blob = make_envelope("k1").encrypt(b"x", "c")
    lonely = Envelope(StaticKeyProvider({"k2": b"b" * 32}, "k2"))
    with pytest.raises(CryptoError):
        lonely.decrypt(blob, "c")


def _settings(**kw):
    base = {
        "database_url": "postgresql+psycopg://u:p@h/db",
        "encryption_keys": "k1:" + base64.b64encode(b"a" * 32).decode(),
        "active_kek_id": "k1",
    }
    return Settings(**{**base, **kw})


def test_production_refuses_insecure_cookies():
    with pytest.raises(ValueError, match="COOKIE_SECURE"):
        _settings(env="production", cookie_secure=False)


def test_production_refuses_loopback_flag():
    with pytest.raises(ValueError, match="LOOPBACK"):
        _settings(env="production", cookie_secure=True, network_allow_loopback=True)


def test_short_key_rejected():
    with pytest.raises(ValueError, match="32 bytes"):
        _settings(encryption_keys="k1:" + base64.b64encode(b"short").decode())


def test_active_kek_must_exist():
    with pytest.raises(ValueError, match="ACTIVE_KEK_ID"):
        _settings(active_kek_id="nope")


def test_secrets_not_in_repr():
    s = _settings()
    assert "u:p@h" not in repr(s) and "k1:" not in repr(s)
