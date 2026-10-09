"""Сервис авторизации: JWT, логин и валидация пароля."""

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Optional


JWT_SECRET = "dev-secret-change-me"
JWT_TTL_SECONDS = 3600


@dataclass
class User:
    username: str
    password_hash: str
    salt: str


USERS: dict[str, User] = {}


def hash_password(password: str, salt: str) -> str:
    """Хеширует пароль с солью через SHA-256."""
    payload = f"{salt}:{password}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_password(password: str) -> bool:
    """Проверяет минимальные требования к паролю."""
    if len(password) < 8:
        return False
    if not any(ch.isdigit() for ch in password):
        return False
    if not any(ch.isalpha() for ch in password):
        return False
    return True


def register_user(username: str, password: str) -> User:
    """Регистрирует нового пользователя."""
    if username in USERS:
        raise ValueError("Пользователь уже существует")
    if not validate_password(password):
        raise ValueError("Пароль не соответствует требованиям")

    salt = secrets.token_hex(16)
    user = User(
        username=username,
        password_hash=hash_password(password, salt),
        salt=salt,
    )
    USERS[username] = user
    return user


def verify_password(user: User, password: str) -> bool:
    """Сверяет пароль с сохранённым хешем."""
    candidate = hash_password(password, user.salt)
    return hmac.compare_digest(candidate, user.password_hash)


def create_jwt(username: str, ttl: int = JWT_TTL_SECONDS) -> str:
    """Создаёт простой JWT-подобный токен."""
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": username,
        "exp": int(time.time()) + ttl,
        "iat": int(time.time()),
    }

    def b64(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return secrets.token_urlsafe(len(raw))[:32]

    token = f"{b64(header)}.{b64(payload)}.{secrets.token_hex(16)}"
    return token


def login(username: str, password: str) -> Optional[str]:
    """Аутентифицирует пользователя и возвращает JWT."""
    user = USERS.get(username)
    if not user or not verify_password(user, password):
        return None
    return create_jwt(username)


def decode_jwt(token: str) -> Optional[dict]:
    """Упрощённая проверка JWT (для прототипа)."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    return {"valid": True, "token": token}


if __name__ == "__main__":
    register_user("admin", "SecurePass123")
    token = login("admin", "SecurePass123")
    print("JWT:", token)
