"""Хеш контракта и проверка транзакции Solana Memo. Приватных ключей здесь нет."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

import base58
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

MEMO_PROGRAM_ID = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
DEFAULT_RPC = "https://api.devnet.solana.com"
_SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_B58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,88}$")


class ProofError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def memo_for(service: str, digest: str) -> str:
    if not _SERVICE_RE.fullmatch(service):
        raise ProofError("bad_service")
    if not _DIGEST_RE.fullmatch(digest):
        raise ProofError("bad_digest")
    return f"hub:v1:{service}:{digest}"


def login_message(domain: str, wallet: str, nonce: str, issued_at: str) -> str:
    return "\n".join(
        [
            "MCP Context Hub",
            f"Domain: {domain}",
            f"Wallet: {wallet}",
            f"Nonce: {nonce}",
            f"Issued At: {issued_at}",
            "Sign in to publish a contract proof. This is not a transaction.",
        ]
    )


def verify_wallet_signature(wallet: str, message: str, signature_b64: str) -> bool:
    if not _B58_RE.fullmatch(wallet):
        return False
    try:
        signature = _b64(signature_b64)
        if len(signature) != 64:
            return False
        verify_key = VerifyKey(base58.b58decode(wallet))
        verify_key.verify(message.encode("utf-8"), signature)
        return True
    except (BadSignatureError, ValueError):
        return False


def _b64(value: str) -> bytes:
    import base64

    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, validate=False)


def assess_transaction(tx: dict | None, *, wallet: str, memo: str) -> dict:
    """Проверяет уже полученный ответ getTransaction. Сеть здесь не вызывается."""
    if not isinstance(tx, dict):
        raise ProofError("not_found")
    meta = tx.get("meta") if isinstance(tx.get("meta"), dict) else {}
    if meta.get("err"):
        raise ProofError("transaction_failed")
    message = ((tx.get("transaction") or {}).get("message") or {})
    if wallet not in _signers(message):
        raise ProofError("signer_mismatch")
    if memo not in _memos(message):
        raise ProofError("memo_mismatch")
    slot = tx.get("slot")
    if not isinstance(slot, int):
        raise ProofError("missing_slot")
    return {"slot": slot, "block_time": tx.get("blockTime")}


def fetch_transaction(signature: str, rpc_url: str, timeout: float = 20.0) -> dict | None:
    if not _B58_RE.fullmatch(signature):
        raise ProofError("bad_signature")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            signature,
            {
                "encoding": "jsonParsed",
                "commitment": "confirmed",
                "maxSupportedTransactionVersion": 0,
            },
        ],
    }
    request = urllib.request.Request(
        rpc_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ProofError("rpc_unavailable") from exc
    if body.get("error"):
        raise ProofError("rpc_unavailable")
    result = body.get("result")
    return result if isinstance(result, dict) else None


def present_client_context(slice_text: str, proof: dict | None) -> str:
    """Срез для агента всегда отдаётся. Якорь Solana только помечает, совпал ли файл."""
    if not proof or not proof.get("anchored"):
        note = "No Solana anchor. This is the current role slice."
    elif proof.get("matches"):
        note = f"Solana anchor matches at slot {proof.get('slot')}."
    else:
        note = "Solana anchor does not match this file. The slice below is the current contract."
    return f"{note}\n\n{slice_text}"


def gate_client_context(slice_text: str, proof: dict) -> str:
    """Агент получает схему только если хеш файла совпал с проверенным якорем."""
    service = proof.get("service") or "unknown"
    local_hash = proof.get("local_sha256") or ""
    if not proof.get("anchored"):
        return "\n".join(
            [
                "REFUSED: contract is not anchored on Solana.",
                f"service: {service}",
                f"local_sha256: {local_hash}",
                "Do not invent request fields or generate a client.",
                "The publisher must anchor this exact file, then call this tool again.",
            ]
        )
    if not proof.get("matches"):
        return "\n".join(
            [
                "REFUSED: anchored hash does not match the current file.",
                f"service: {service}",
                f"local_sha256: {local_hash}",
                f"anchored_sha256: {proof.get('anchored_sha256')}",
                f"slot: {proof.get('slot')}",
                f"signature: {proof.get('signature')}",
                f"explorer: {proof.get('explorer')}",
                "Do not generate a client. The chain proof is for different bytes.",
            ]
        )
    return "\n".join(
        [
            "VERIFIED on Solana.",
            f"slot: {proof.get('slot')}",
            f"sha256: {local_hash}",
            f"signature: {proof.get('signature')}",
            f"explorer: {proof.get('explorer')}",
            "",
            slice_text,
        ]
    )


def describe_proof(service: str, local_hash: str, anchor: dict | None) -> dict:
    if not anchor:
        return {
            "service": service,
            "local_sha256": local_hash,
            "anchored": False,
            "matches": False,
            "signature": None,
            "slot": None,
            "wallet": None,
            "anchored_sha256": None,
        }
    anchored_hash = str(anchor.get("content_hash") or "")
    return {
        "service": service,
        "local_sha256": local_hash,
        "anchored": True,
        "matches": anchored_hash == local_hash,
        "signature": anchor.get("signature"),
        "slot": anchor.get("slot"),
        "wallet": anchor.get("wallet"),
        "anchored_sha256": anchored_hash,
    }


def contract_file(root: Path, service: str) -> Path | None:
    if not _SERVICE_RE.fullmatch(service):
        return None
    manifest_path = root / "hub_manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    root_resolved = root.resolve()
    for item in manifest.get("services") or []:
        if not isinstance(item, dict) or item.get("name") != service:
            continue
        contracts = item.get("contracts") or []
        if not contracts:
            return None
        candidate = (root_resolved / str(contracts[0])).resolve()
        try:
            candidate.relative_to(root_resolved)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None
    return None


def _signers(message: dict) -> set[str]:
    signers: set[str] = set()
    for key in message.get("accountKeys") or []:
        if isinstance(key, dict) and key.get("signer") and key.get("pubkey"):
            signers.add(str(key["pubkey"]))
        elif isinstance(key, str):
            # В нераспарсенном виде порядок ключей не доказывает подпись.
            continue
    return signers


def _memos(message: dict) -> set[str]:
    found: set[str] = set()
    for instruction in message.get("instructions") or []:
        if not isinstance(instruction, dict):
            continue
        program_id = str(instruction.get("programId") or "")
        program = str(instruction.get("program") or "")
        if program_id != MEMO_PROGRAM_ID and program != "spl-memo":
            continue
        parsed = instruction.get("parsed")
        if isinstance(parsed, str):
            found.add(parsed)
        elif isinstance(parsed, dict) and isinstance(parsed.get("info"), str):
            found.add(parsed["info"])
        data = instruction.get("data")
        if isinstance(data, str):
            try:
                found.add(base58.b58decode(data).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
    return found
