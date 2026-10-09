from pathlib import Path

import pytest

import vault_state
from vault_state import VaultLocked


def test_sealed_text_hides_the_source(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault_state, "SALT_PATH", tmp_path / "vault.salt")
    vault_state.lock()
    vault_state.unlock("project-passphrase")
    sealed = vault_state.seal_document("username secret field")
    assert "username" not in sealed
    assert sealed.startswith("enc1:")
    assert vault_state.open_document(sealed) == "username secret field"
    vault_state.lock()
    with pytest.raises(VaultLocked):
        vault_state.open_document(sealed)


def test_collection_on_disk_does_not_keep_plaintext(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault_state, "SALT_PATH", tmp_path / "vault.salt")
    vault_state.lock()
    vault_state.unlock("project-passphrase")

    class Store:
        def __init__(self):
            self.rows = {"ids": ["a"], "documents": ["POST /login username"]}

        def get(self, include=None):
            return self.rows

        def update(self, ids, documents):
            self.rows = {"ids": ids, "documents": documents}

    store = Store()
    assert vault_state.protect_collection(store) == 1
    assert "username" not in store.rows["documents"][0]
    assert vault_state.open_document(store.rows["documents"][0]) == "POST /login username"
    vault_state.lock()
