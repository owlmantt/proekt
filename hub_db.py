"""SQLite для одноразовых nonce, сессий и якорей контрактов. Chroma не заменяет."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def init_db(path: Path) -> None:
    with _connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS nonces (
                nonce TEXT PRIMARY KEY,
                wallet TEXT NOT NULL,
                message TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                wallet TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS anchors (
                signature TEXT PRIMARY KEY,
                service TEXT NOT NULL,
                contract_path TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                wallet TEXT NOT NULL,
                slot INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS anchors_service_created
                ON anchors (service, created_at DESC);
            CREATE INDEX IF NOT EXISTS anchors_wallet_created
                ON anchors (wallet, created_at DESC);
            CREATE TABLE IF NOT EXISTS releases (
                signature TEXT PRIMARY KEY,
                service TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                slot INTEGER NOT NULL,
                snapshot TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS releases_service_created
                ON releases (service, created_at DESC);
            """
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def issue_nonce(path: Path, wallet: str, message: str, nonce: str, ttl_seconds: int = 600) -> str:
    expires = _iso(_now() + timedelta(seconds=ttl_seconds))
    with _connect(path) as connection:
        connection.execute(
            "INSERT INTO nonces (nonce, wallet, message, expires_at, used) VALUES (?, ?, ?, ?, 0)",
            (nonce, wallet, message, expires),
        )
    return expires


def peek_nonce(path: Path, wallet: str, nonce: str) -> str | None:
    now = _iso(_now())
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT message, expires_at, used, wallet FROM nonces WHERE nonce = ?",
            (nonce,),
        ).fetchone()
    if row is None or row["used"] or row["wallet"] != wallet or row["expires_at"] < now:
        return None
    return str(row["message"])


def consume_nonce(path: Path, wallet: str, nonce: str) -> str | None:
    now = _iso(_now())
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT message, expires_at, used, wallet FROM nonces WHERE nonce = ?",
            (nonce,),
        ).fetchone()
        if row is None or row["used"] or row["wallet"] != wallet or row["expires_at"] < now:
            return None
        connection.execute("UPDATE nonces SET used = 1 WHERE nonce = ?", (nonce,))
    return str(row["message"])


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(path: Path, token: str, wallet: str, ttl_seconds: int = 7 * 24 * 3600) -> str:
    expires = _iso(_now() + timedelta(seconds=ttl_seconds))
    with _connect(path) as connection:
        connection.execute(
            "INSERT INTO sessions (token_hash, wallet, expires_at) VALUES (?, ?, ?)",
            (token_hash(token), wallet, expires),
        )
    return expires


def session_wallet(path: Path, token: str) -> str | None:
    now = _iso(_now())
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT wallet, expires_at FROM sessions WHERE token_hash = ?",
            (token_hash(token),),
        ).fetchone()
    if row is None or row["expires_at"] < now:
        return None
    return str(row["wallet"])


def delete_session(path: Path, token: str) -> None:
    with _connect(path) as connection:
        connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))


def count_distinct_wallets(path: Path) -> int:
    now = _iso(_now())
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT COUNT(DISTINCT wallet) AS n FROM sessions WHERE expires_at >= ?",
            (now,),
        ).fetchone()
    return int(row["n"])


def wallet_has_session(path: Path, wallet: str) -> bool:
    now = _iso(_now())
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT 1 FROM sessions WHERE wallet = ? AND expires_at >= ? LIMIT 1",
            (wallet, now),
        ).fetchone()
    return row is not None


def count_anchors_since(path: Path, wallet: str, since_iso: str) -> int:
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS n FROM anchors WHERE wallet = ? AND created_at >= ?",
            (wallet, since_iso),
        ).fetchone()
    return int(row["n"])


def insert_anchor(
    path: Path,
    *,
    signature: str,
    service: str,
    contract_path: str,
    content_hash: str,
    wallet: str,
    slot: int,
    snapshot: str = "",
) -> bool:
    created = _iso(_now())
    with _connect(path) as connection:
        exists = connection.execute(
            "SELECT 1 FROM anchors WHERE signature = ?",
            (signature,),
        ).fetchone()
        if exists:
            return False
        connection.execute(
            """
            INSERT INTO anchors
                (signature, service, contract_path, content_hash, wallet, slot, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (signature, service, contract_path, content_hash, wallet, slot, created),
        )
        if snapshot:
            connection.execute(
                """
                INSERT INTO releases (signature, service, content_hash, slot, snapshot, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (signature, service, content_hash, slot, snapshot, created),
            )
    return True


def latest_anchor(path: Path, service: str) -> dict | None:
    with _connect(path) as connection:
        row = connection.execute(
            """
            SELECT signature, service, contract_path, content_hash, wallet, slot, created_at
            FROM anchors
            WHERE service = ?
            ORDER BY created_at DESC, slot DESC
            LIMIT 1
            """,
            (service,),
        ).fetchone()
    return dict(row) if row else None


def latest_release(path: Path, service: str) -> dict | None:
    with _connect(path) as connection:
        row = connection.execute(
            """
            SELECT signature, service, content_hash, slot, snapshot, created_at
            FROM releases
            WHERE service = ?
            ORDER BY created_at DESC, slot DESC
            LIMIT 1
            """,
            (service,),
        ).fetchone()
    return dict(row) if row else None
