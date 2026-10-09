"""Кошелёк и якорь контракта. Сервер не хранит и не запрашивает приватные ключи."""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

import hub_db
from contracts import format_client_context, format_client_context_from_text
from ledger import (
    ProofError,
    assess_transaction,
    contract_file,
    describe_proof,
    fetch_transaction,
    login_message,
    memo_for,
    sha256_bytes,
    verify_wallet_signature,
)

DB_PATH = Path(os.environ.get("HUB_DB", "hub_data/hub.db"))
DEFAULT_RPC = "https://api.devnet.solana.com"


class NonceRequest(BaseModel):
    address: str = Field(min_length=32, max_length=44)


class VerifyRequest(BaseModel):
    address: str
    nonce: str
    signature_b64: str


class ServiceRequest(BaseModel):
    service: str = "auth"


class ConfirmRequest(BaseModel):
    service: str = "auth"
    signature: str = Field(min_length=64, max_length=100)


class PassphraseRequest(BaseModel):
    passphrase: str = Field(min_length=8, max_length=200)


def rpc_url() -> str:
    configured = os.environ.get("HUB_SOLANA_RPC", DEFAULT_RPC).strip()
    allow_mainnet = os.environ.get("HUB_ALLOW_MAINNET", "").strip() == "1"
    if "mainnet" in configured and not allow_mainnet:
        return DEFAULT_RPC
    return configured


def cluster_name() -> str:
    return os.environ.get("HUB_SOLANA_CLUSTER", "devnet").strip() or "devnet"


def anchor_limit() -> int:
    raw = os.environ.get("HUB_ANCHOR_MONTHLY_LIMIT", "").strip()
    if raw.isdigit():
        return int(raw)
    return 200 if os.environ.get("HUB_PLAN", "free").strip().lower() == "team" else 5


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="session_required")
    token = header[7:].strip()
    wallet = hub_db.session_wallet(DB_PATH, token)
    if not wallet:
        raise HTTPException(status_code=401, detail="session_required")
    return wallet


def _month_start() -> str:
    now = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return now.isoformat()


def _contract_bytes(root: Path, service: str) -> tuple[Path, bytes, str]:
    path = contract_file(root, service)
    if path is None:
        raise HTTPException(status_code=404, detail="contract_not_found")
    data = path.read_bytes()
    return path, data, sha256_bytes(data)


def proof_payload(root: Path, service: str) -> dict:
    path = contract_file(root, service)
    if path is None:
        raise HTTPException(status_code=404, detail="contract_not_found")
    local_hash = sha256_bytes(path.read_bytes())
    anchor = hub_db.latest_anchor(DB_PATH, service)
    payload = describe_proof(service, local_hash, anchor)
    payload["contract"] = path.resolve().relative_to(root.resolve()).as_posix()
    payload["cluster"] = cluster_name()
    if payload.get("signature"):
        payload["explorer"] = (
            f"https://explorer.solana.com/tx/{payload['signature']}?cluster={cluster_name()}"
        )
    return payload


CLIENT_ROLES = {"frontend", "flutter"}


def compose_agent_view(
    *,
    role: str,
    current_slice: str,
    current_hash: str,
    release: dict | None,
    released_slice: str | None,
) -> str:
    """Фронтенд и Flutter после релиза читают снимок, а не черновик на диске."""
    normalized = (role or "frontend").strip().lower()
    if release is None:
        return f"No Solana release yet. This is the current role slice.\n\n{current_slice}"
    if normalized not in CLIENT_ROLES:
        return f"Working copy for {normalized}.\n\n{current_slice}"
    if not released_slice:
        return (
            f"Vault is locked. The release at slot {release.get('slot')} is encrypted, "
            "so the schema is not shown."
        )
    slot = release.get("slot")
    if current_hash != release.get("content_hash"):
        header = (
            f"Draft is newer than the release at slot {slot}. "
            "Fields below are the released contract, not the file on disk."
        )
    else:
        header = f"Released contract at slot {slot}."
    return f"{header}\n\n{released_slice}"


def agent_context(root: Path, method: str, api_path: str, role: str = "frontend", service: str = "auth") -> str:
    from vault_state import VaultLocked, open_document

    current_slice = format_client_context(root, method, api_path, role=role)
    current_hash = ""
    contract = contract_file(root, service)
    if contract is not None:
        current_hash = sha256_bytes(contract.read_bytes())
    release = hub_db.latest_release(DB_PATH, service)
    released_slice = None
    if release and (role or "").strip().lower() in CLIENT_ROLES:
        try:
            raw = open_document(str(release.get("snapshot") or ""))
        except VaultLocked:
            raw = ""
        if raw:
            released_slice = format_client_context_from_text(
                raw,
                str(contract.relative_to(root.resolve()).as_posix() if contract else service),
                method,
                api_path,
                role,
            )
    return compose_agent_view(
        role=role,
        current_slice=current_slice,
        current_hash=current_hash,
        release=release,
        released_slice=released_slice,
    )


def proof_text(root: Path, service: str) -> str:
    try:
        payload = proof_payload(root, service)
    except HTTPException as exc:
        return f"Контракт не найден: {exc.detail}"
    if not payload["anchored"]:
        return (
            f"{payload['service']}: локальный sha256 {payload['local_sha256']}\n"
            "В Solana якоря ещё нет. Хеш нельзя считать подтверждённым сетью."
        )
    state = "совпадает с файлом" if payload["matches"] else "НЕ совпадает с текущим файлом"
    return "\n".join(
        [
            f"Сервис: {payload['service']}",
            f"Файл: {payload['contract']}",
            f"Локальный sha256: {payload['local_sha256']}",
            f"Якорь sha256: {payload['anchored_sha256']}",
            f"Статус: {state}",
            f"Слот: {payload['slot']}",
            f"Кошелёк: {payload['wallet']}",
            f"Подпись: {payload['signature']}",
            f"Explorer: {payload.get('explorer')}",
        ]
    )


def seat_limit() -> int:
    raw = os.environ.get("HUB_SEAT_LIMIT", "").strip()
    if raw.isdigit():
        return int(raw)
    return 25 if os.environ.get("HUB_PLAN", "free").strip().lower() == "team" else 5


def storage_limit() -> int:
    raw = os.environ.get("HUB_STORAGE_LIMIT_BYTES", "").strip()
    if raw.isdigit():
        return int(raw)
    if os.environ.get("HUB_PLAN", "free").strip().lower() == "team":
        return 5 * 1024 * 1024 * 1024
    return 50 * 1024 * 1024


def mount_wallet_api(app: FastAPI, root: Path, collection=None) -> None:
    hub_db.init_db(DB_PATH)

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "cluster": cluster_name(), "checkout": "not_connected"}

    @app.get("/api/network")
    def network() -> dict:
        return {"cluster": cluster_name(), "rpc": rpc_url(), "memo_program": "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"}

    @app.get("/api/plans")
    def plans() -> dict:
        plan = os.environ.get("HUB_PLAN", "free").strip().lower()
        return {
            "hypothetical": True,
            "note": "Prices are product hypotheses, not observed demand or revenue. Checkout is not connected.",
            "current_plan": plan,
            "anchor_limit_per_wallet_month": anchor_limit(),
            "seat_limit": seat_limit(),
            "storage_limit_bytes": storage_limit(),
            "checkout": "not_connected",
            "plans": [
                {
                    "id": "free",
                    "payer": "individual developer",
                    "pays_for": "search and up to 5 devnet anchors a month",
                    "price_hypothesis_usd": 0,
                },
                {
                    "id": "team",
                    "payer": "team lead",
                    "pays_for": "seats for developers whose agents read the shared contract",
                    "price_hypothesis_usd_per_seat_month": 29,
                    "formula": "revenue_hypothesis = paying_seats * 29",
                },
            ],
        }

    @app.post("/api/wallet/nonce")
    def wallet_nonce(body: NonceRequest, request: Request) -> dict:
        nonce = secrets.token_hex(16)
        issued = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        domain = request.headers.get("host") or "localhost"
        message = login_message(domain, body.address, nonce, issued)
        try:
            expires = hub_db.issue_nonce(DB_PATH, body.address, message, nonce)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="bad_wallet") from exc
        return {"nonce": nonce, "message": message, "expires_at": expires}

    @app.post("/api/wallet/verify")
    def wallet_verify(body: VerifyRequest) -> dict:
        message = hub_db.peek_nonce(DB_PATH, body.address, body.nonce)
        if message is None or not verify_wallet_signature(body.address, message, body.signature_b64):
            raise HTTPException(status_code=401, detail="nonce_rejected" if message is None else "bad_signature")
        if hub_db.consume_nonce(DB_PATH, body.address, body.nonce) is None:
            raise HTTPException(status_code=401, detail="nonce_rejected")
        if (
            not hub_db.wallet_has_session(DB_PATH, body.address)
            and hub_db.count_distinct_wallets(DB_PATH) >= seat_limit()
        ):
            raise HTTPException(status_code=402, detail="seat_limit")
        token = secrets.token_urlsafe(32)
        expires = hub_db.create_session(DB_PATH, token, body.address)
        return {"token": token, "wallet": body.address, "expires_at": expires}

    @app.get("/api/wallet/me")
    def wallet_me(request: Request) -> dict:
        return {"wallet": _bearer(request)}

    @app.post("/api/wallet/logout")
    def wallet_logout(request: Request) -> dict:
        header = request.headers.get("authorization") or ""
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if token:
            hub_db.delete_session(DB_PATH, token)
        return {"ok": True}

    @app.get("/api/vault")
    def vault_status() -> dict:
        storage = 0
        if collection is not None:
            raw = collection.get(include=["documents"])
            storage = sum(len(doc or "") for doc in (raw.get("documents") or []))
        from vault_state import locked, salt_b64

        return {
            "locked": locked(),
            "salt_b64": salt_b64(),
            "seats_used": hub_db.count_distinct_wallets(DB_PATH),
            "seat_limit": seat_limit(),
            "storage_bytes": storage,
            "storage_limit_bytes": storage_limit(),
            "pbkdf2_iterations": 200_000,
        }

    @app.post("/api/vault/unlock")
    def vault_unlock(payload: PassphraseRequest) -> dict:
        from vault_state import unlock

        try:
            unlock(payload.passphrase)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="short_passphrase") from exc
        return {"locked": False, "sealed_chunks": 0}

    @app.post("/api/vault/lock")
    def vault_lock() -> dict:
        from vault_state import lock

        lock()
        return {"locked": True}

    @app.get("/api/contracts/{service}/proof")
    def contract_proof(service: str) -> dict:
        return proof_payload(root, service)

    @app.get("/api/contracts/{service}/agent")
    def contract_agent(
        service: str,
        method: str = "POST",
        path: str = "/api/v1/auth/login",
        role: str = "frontend",
    ) -> dict:
        text = agent_context(root, method, path, role=role, service=service)
        return {"allowed": True, "text": text}

    @app.post("/api/contracts/prepare")
    def prepare(body: ServiceRequest, request: Request) -> dict:
        wallet = _bearer(request)
        used = hub_db.count_anchors_since(DB_PATH, wallet, _month_start())
        limit = anchor_limit()
        if used >= limit:
            raise HTTPException(status_code=402, detail="anchor_limit")
        path, _data, digest = _contract_bytes(root, body.service)
        memo = memo_for(body.service, digest)
        return {
            "service": body.service,
            "contract": path.resolve().relative_to(root.resolve()).as_posix(),
            "sha256": digest,
            "memo": memo,
            "cluster": cluster_name(),
            "rpc": rpc_url(),
            "anchors_used": used,
            "anchor_limit": limit,
            "checkout": "not_connected",
        }

    @app.post("/api/contracts/confirm")
    def confirm(body: ConfirmRequest, request: Request) -> dict:
        wallet = _bearer(request)
        used = hub_db.count_anchors_since(DB_PATH, wallet, _month_start())
        if used >= anchor_limit():
            raise HTTPException(status_code=402, detail="anchor_limit")
        path, data, digest = _contract_bytes(root, body.service)
        memo = memo_for(body.service, digest)
        try:
            transaction = fetch_transaction(body.signature, rpc_url())
            checked = assess_transaction(transaction, wallet=wallet, memo=memo)
        except ProofError as exc:
            status = 502 if exc.code == "rpc_unavailable" else 400
            raise HTTPException(status_code=status, detail=exc.code) from exc
        from vault_state import current_key, seal_document

        if current_key() is None:
            raise HTTPException(status_code=423, detail="vault_locked")
        snapshot = seal_document(data.decode("utf-8"))
        stored = hub_db.insert_anchor(
            DB_PATH,
            signature=body.signature,
            service=body.service,
            contract_path=path.resolve().relative_to(root.resolve()).as_posix(),
            content_hash=digest,
            wallet=wallet,
            slot=checked["slot"],
            snapshot=snapshot,
        )
        if not stored:
            raise HTTPException(status_code=409, detail="signature_replayed")
        return {
            "ok": True,
            "confirmed_by_rpc": True,
            "slot": checked["slot"],
            "sha256": digest,
            "explorer": f"https://explorer.solana.com/tx/{body.signature}?cluster={cluster_name()}",
        }
