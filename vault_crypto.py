"""AES-GCM для текста чанков. Ключ в базу не пишется."""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

PREFIX = "enc1:"
PBKDF2_ITERATIONS = 200_000


def derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return kdf.derive(passphrase.encode("utf-8"))


def seal(text: str, key: bytes) -> str:
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, text.encode("utf-8"), None)
    return PREFIX + base64.b64encode(nonce + ciphertext).decode("ascii")


def open_sealed(stored: str, key: bytes) -> str:
    if not stored.startswith(PREFIX):
        return stored
    raw = base64.b64decode(stored[len(PREFIX):])
    nonce, ciphertext = raw[:12], raw[12:]
    return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8")


def is_sealed(stored: str | None) -> bool:
    return isinstance(stored, str) and stored.startswith(PREFIX)
