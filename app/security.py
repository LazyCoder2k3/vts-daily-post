"""Mã hoá token Strava khi lưu DB (Fernet). Không có khoá -> lưu thô (chỉ nên dùng khi thử)."""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

_PREFIX = "enc:"


def _fernet() -> Fernet | None:
    key = get_settings().token_encryption_key
    return Fernet(key.encode()) if key else None


def encrypt(value: str) -> str:
    f = _fernet()
    return _PREFIX + f.encrypt(value.encode()).decode() if f else value


def decrypt(value: str) -> str:
    if not value.startswith(_PREFIX):
        return value
    f = _fernet()
    if f is None:
        raise RuntimeError("Token trong DB đã mã hoá nhưng thiếu TOKEN_ENCRYPTION_KEY")
    try:
        return f.decrypt(value[len(_PREFIX):].encode()).decode()
    except InvalidToken as e:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY không khớp với dữ liệu trong DB") from e


def generate_key() -> str:
    return Fernet.generate_key().decode()
