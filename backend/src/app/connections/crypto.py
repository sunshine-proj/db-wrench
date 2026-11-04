"""Envelope encryption: per-secret data key (DEK) wrapped by a key-encryption key (KEK).

Ciphertext layout (JSON, stored as text)::

    {"v": 1, "kek": "<kek id>", "wn": b64 nonce, "wk": b64 wrapped DEK,
     "n": b64 nonce, "ct": b64 ciphertext}

Both layers use AES-256-GCM with the caller-supplied ``context`` as associated data,
so a ciphertext cannot be moved between records (e.g. connection A -> connection B).
"""

from __future__ import annotations

import base64
import json
import os
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import Settings


class CryptoError(Exception):
    pass


class KeyProvider(Protocol):
    """Pluggable KEK source (environment/file today; Vault or cloud KMS later)."""

    @property
    def active_id(self) -> str: ...

    def get(self, kek_id: str) -> bytes: ...


class StaticKeyProvider:
    def __init__(self, keys: dict[str, bytes], active_id: str):
        if active_id not in keys:
            raise CryptoError("active KEK is not configured")
        self._keys = keys
        self._active = active_id

    @classmethod
    def from_settings(cls, settings: Settings) -> StaticKeyProvider:
        return cls(settings.parsed_keys(), settings.active_kek_id)

    @property
    def active_id(self) -> str:
        return self._active

    def get(self, kek_id: str) -> bytes:
        try:
            return self._keys[kek_id]
        except KeyError as exc:
            raise CryptoError(f"KEK {kek_id!r} is not available") from exc


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"))


class Envelope:
    def __init__(self, provider: KeyProvider):
        self._provider = provider

    def encrypt(self, plaintext: bytes, context: str, kek_id: str | None = None) -> str:
        kid = kek_id or self._provider.active_id
        aad = context.encode()
        dek = AESGCM.generate_key(bit_length=256)
        wn, n = os.urandom(12), os.urandom(12)
        wrapped = AESGCM(self._provider.get(kid)).encrypt(wn, dek, aad)
        ct = AESGCM(dek).encrypt(n, plaintext, aad)
        return json.dumps(
            {"v": 1, "kek": kid, "wn": _b64(wn), "wk": _b64(wrapped), "n": _b64(n), "ct": _b64(ct)}
        )

    def decrypt(self, blob: str, context: str) -> bytes:
        try:
            doc = json.loads(blob)
            aad = context.encode()
            dek = AESGCM(self._provider.get(doc["kek"])).decrypt(
                _unb64(doc["wn"]), _unb64(doc["wk"]), aad
            )
            return AESGCM(dek).decrypt(_unb64(doc["n"]), _unb64(doc["ct"]), aad)
        except (InvalidTag, KeyError, ValueError) as exc:
            raise CryptoError("unable to decrypt secret") from exc

    def kek_id(self, blob: str) -> str:
        return str(json.loads(blob)["kek"])

    def rewrap(self, blob: str, context: str, new_kek_id: str | None = None) -> str:
        """Re-encrypt only the DEK under another KEK (rotation); payload is untouched."""
        kid = new_kek_id or self._provider.active_id
        doc = json.loads(blob)
        aad = context.encode()
        dek = AESGCM(self._provider.get(doc["kek"])).decrypt(
            _unb64(doc["wn"]), _unb64(doc["wk"]), aad
        )
        wn = os.urandom(12)
        doc["wk"] = _b64(AESGCM(self._provider.get(kid)).encrypt(wn, dek, aad))
        doc["wn"] = _b64(wn)
        doc["kek"] = kid
        return json.dumps(doc)

    def derive_key(self, purpose: str) -> bytes:
        """A purpose-bound key derived from the active KEK, e.g. to sign short-lived tokens."""
        import hashlib
        import hmac

        kek = self._provider.get(self._provider.active_id)
        return hmac.new(kek, f"dbwrench:{purpose}".encode(), hashlib.sha256).digest()

    def encrypt_json(self, value: dict[str, object], context: str) -> str:
        return self.encrypt(json.dumps(value).encode(), context)

    def decrypt_json(self, blob: str, context: str) -> dict[str, object]:
        result: dict[str, object] = json.loads(self.decrypt(blob, context))
        return result
