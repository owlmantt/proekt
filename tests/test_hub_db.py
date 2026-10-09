from pathlib import Path

from hub_db import consume_nonce, count_anchors_since, create_session, init_db, insert_anchor, issue_nonce, latest_anchor, session_wallet


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "hub.db"
    init_db(path)
    return path


def test_nonce_is_single_use(tmp_path: Path):
    path = _db(tmp_path)
    issue_nonce(path, "Wallet", "message-body", "nonce-1", ttl_seconds=60)
    assert consume_nonce(path, "Wallet", "nonce-1") == "message-body"
    assert consume_nonce(path, "Wallet", "nonce-1") is None
    assert consume_nonce(path, "Other", "nonce-1") is None


def test_session_roundtrip_and_anchor_replay(tmp_path: Path):
    path = _db(tmp_path)
    create_session(path, "secret-token", "Wallet", ttl_seconds=60)
    assert session_wallet(path, "secret-token") == "Wallet"
    assert session_wallet(path, "wrong") is None
    assert insert_anchor(
        path,
        signature="sig",
        service="auth",
        contract_path="test_sample/openapi.json",
        content_hash="abc",
        wallet="Wallet",
        slot=7,
    )
    assert insert_anchor(
        path,
        signature="sig",
        service="auth",
        contract_path="test_sample/openapi.json",
        content_hash="abc",
        wallet="Wallet",
        slot=7,
    ) is False
    assert latest_anchor(path, "auth")["slot"] == 7
    assert count_anchors_since(path, "Wallet", "2000-01-01T00:00:00+00:00") == 1
