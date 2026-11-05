"""Singleton dùng chung lúc chạy: StravaClient (api, bot, jobs đều lấy qua đây)."""
from __future__ import annotations

from app.strava.client import StravaClient

_client: StravaClient | None = None


def get_client() -> StravaClient:
    global _client
    if _client is None:
        _client = StravaClient()
    return _client


def set_client(client: StravaClient | None) -> None:
    global _client
    _client = client
