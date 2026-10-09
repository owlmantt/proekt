"""Ключ проекта живёт в памяти процесса. На диск попадает только соль и шифротекст."""

from __future__ import annotations

import os
from pathlib import Path

from vault_crypto import derive_key, is_sealed, open_sealed, seal

SALT_PATH = Path(os.environ.get("HUB_VAULT_SALT", "hub_data/vault.salt"))
_key: bytes | None = None


class VaultLocked(Exception):
    pass


def locked() -> bool:
    return _key is None


def current_key() -> bytes | None:
    return _key


def unlock(passphrase: str) -> None:
    global _key
    phrase = (passphrase or "").strip()
    if len(phrase) < 8:
        raise ValueError("short_passphrase")
    SALT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not SALT_PATH.is_file():
        SALT_PATH.write_bytes(os.urandom(16))
    _key = derive_key(phrase, SALT_PATH.read_bytes())


def lock() -> None:
    global _key
    _key = None


def salt_b64() -> str:
    import base64

    if not SALT_PATH.is_file():
        return ""
    return base64.b64encode(SALT_PATH.read_bytes()).decode("ascii")


def open_document(stored: str | None) -> str:
    text = stored or ""
    if not is_sealed(text):
        return text
    key = current_key()
    if key is None:
        raise VaultLocked()
    return open_sealed(text, key)


def seal_document(text: str) -> str:
    key = current_key()
    if key is None or is_sealed(text):
        return text
    return seal(text, key)


def protect_collection(collection) -> int:
    key = current_key()
    if key is None:
        return 0
    raw = collection.get(include=["documents"])
    ids = raw.get("ids") or []
    documents = raw.get("documents") or []
    sealed_ids: list[str] = []
    sealed_docs: list[str] = []
    for doc_id, document in zip(ids, documents):
        if document and not is_sealed(document):
            sealed_ids.append(doc_id)
            sealed_docs.append(seal(document, key))
    if sealed_ids:
        collection.update(ids=sealed_ids, documents=sealed_docs)
    return len(sealed_ids)


def protect_text_file(path: Path) -> None:
    key = current_key()
    if key is None or not path.is_file():
        return
    raw = path.read_text(encoding="utf-8")
    if raw.startswith("enc1:"):
        return
    path.write_text(seal(raw, key), encoding="utf-8")


def autoload_from_env() -> None:
    phrase = os.environ.get("HUB_PASSPHRASE", "").strip()
    if phrase:
        unlock(phrase)


autoload_from_env()
