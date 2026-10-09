from pathlib import Path

from hub_db import init_db, insert_anchor, latest_release
from wallet_api import compose_agent_view
import vault_state


def test_frontend_reads_release_not_the_draft():
    text = compose_agent_view(
        role="frontend",
        current_slice="username: NEWFIELD",
        current_hash="new",
        release={"slot": 42, "content_hash": "old"},
        released_slice="username: string",
    )
    assert "slot 42" in text
    assert "username: string" in text
    assert "NEWFIELD" not in text


def test_before_release_the_current_slice_is_served():
    text = compose_agent_view(
        role="flutter",
        current_slice="username: string",
        current_hash="abc",
        release=None,
        released_slice=None,
    )
    assert text.startswith("No Solana release yet")
    assert "username: string" in text


def test_backend_keeps_the_working_copy():
    text = compose_agent_view(
        role="backend",
        current_slice="def login",
        current_hash="new",
        release={"slot": 7, "content_hash": "old"},
        released_slice="username: string",
    )
    assert "def login" in text
    assert "username: string" not in text


def test_release_snapshot_is_stored_encrypted(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault_state, "SALT_PATH", tmp_path / "vault.salt")
    vault_state.lock()
    vault_state.unlock("project-passphrase")
    sealed = vault_state.seal_document('{"password":"secret-field"}')
    database = tmp_path / "hub.db"
    init_db(database)
    assert insert_anchor(
        database,
        signature="sig",
        service="auth",
        contract_path="test_sample/openapi.json",
        content_hash="abc",
        wallet="Wallet",
        slot=3,
        snapshot=sealed,
    )
    stored = latest_release(database, "auth")
    assert stored is not None
    assert "secret-field" not in stored["snapshot"]
    assert vault_state.open_document(stored["snapshot"]) == '{"password":"secret-field"}'
    vault_state.lock()
