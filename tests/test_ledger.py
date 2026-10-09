import base64
from pathlib import Path

import base58
import pytest
from nacl.signing import SigningKey

from ledger import (
    MEMO_PROGRAM_ID,
    ProofError,
    assess_transaction,
    describe_proof,
    login_message,
    memo_for,
    sha256_bytes,
    verify_wallet_signature,
)


def test_hash_and_memo_are_stable():
    digest = sha256_bytes(b"openapi")
    assert digest == sha256_bytes(b"openapi")
    assert memo_for("auth", digest) == f"hub:v1:auth:{digest}"


def test_memo_rejects_injection():
    with pytest.raises(ProofError):
        memo_for("auth\nadmin", "a" * 64)


def test_wallet_signature_roundtrip():
    signing = SigningKey.generate()
    wallet = base58.b58encode(bytes(signing.verify_key)).decode()
    message = login_message("localhost", wallet, "abc", "2026-10-09T00:00:00+00:00")
    signature = base64.b64encode(signing.sign(message.encode()).signature).decode()
    assert verify_wallet_signature(wallet, message, signature)
    assert not verify_wallet_signature(wallet, message + "x", signature)


def _tx(wallet: str, memo: str, *, signer: bool = True, err=None, slot: int = 42) -> dict:
    return {
        "slot": slot,
        "blockTime": 1_700_000_000,
        "meta": {"err": err},
        "transaction": {
            "message": {
                "accountKeys": [{"pubkey": wallet, "signer": signer, "writable": True}],
                "instructions": [
                    {"program": "spl-memo", "programId": MEMO_PROGRAM_ID, "parsed": memo}
                ],
            }
        },
    }


def test_assess_accepts_matching_memo():
    proof = assess_transaction(_tx("Wallet111", "hub:v1:auth:aa"), wallet="Wallet111", memo="hub:v1:auth:aa")
    assert proof["slot"] == 42


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda tx: tx.update({"meta": {"err": {"InstructionError": [0, "Custom"]}}}) or tx, "transaction_failed"),
        (lambda tx: _tx("Other", "hub:v1:auth:aa"), "signer_mismatch"),
        (lambda tx: _tx("Wallet111", "hub:v1:auth:aa", signer=False), "signer_mismatch"),
        (lambda tx: _tx("Wallet111", "other"), "memo_mismatch"),
    ],
)
def test_assess_rejects_bad_transactions(mutate, code):
    tx = _tx("Wallet111", "hub:v1:auth:aa")
    tx = mutate(tx)
    with pytest.raises(ProofError) as caught:
        assess_transaction(tx, wallet="Wallet111", memo="hub:v1:auth:aa")
    assert caught.value.code == code


def test_missing_transaction():
    with pytest.raises(ProofError) as caught:
        assess_transaction(None, wallet="Wallet111", memo="memo")
    assert caught.value.code == "not_found"


def test_slice_stays_available_without_anchor():
    from ledger import present_client_context

    plain = present_client_context("username: string", {"service": "auth", "anchored": False})
    assert "username: string" in plain
    assert plain.startswith("No Solana anchor")
    verified = present_client_context(
        "username: string",
        {"service": "auth", "anchored": True, "matches": True, "slot": 3},
    )
    assert verified.startswith("Solana anchor matches")
    assert "username: string" in verified
    stale = present_client_context(
        "username: string",
        {"service": "auth", "anchored": True, "matches": False, "slot": 9},
    )
    assert "current contract" in stale
    assert "username: string" in stale
    fresh = describe_proof("auth", "abc", None)
    assert fresh["anchored"] is False
    current = describe_proof("auth", "abc", {"content_hash": "abc", "signature": "sig", "slot": 1, "wallet": "W"})
    assert current["matches"] is True
    stale = describe_proof("auth", "zzz", {"content_hash": "abc", "signature": "sig", "slot": 1, "wallet": "W"})
    assert stale["anchored"] is True
    assert stale["matches"] is False


def test_contract_file_stays_inside_project(tmp_path: Path):
    (tmp_path / "hub_manifest.json").write_text(
        '{"services":[{"name":"auth","contracts":["spec.json"]}]}',
        encoding="utf-8",
    )
    (tmp_path / "spec.json").write_text("{}", encoding="utf-8")
    from ledger import contract_file

    assert contract_file(tmp_path, "auth") == (tmp_path / "spec.json").resolve()
    (tmp_path / "hub_manifest.json").write_text(
        '{"services":[{"name":"auth","contracts":["../secret.json"]}]}',
        encoding="utf-8",
    )
    assert contract_file(tmp_path, "auth") is None
