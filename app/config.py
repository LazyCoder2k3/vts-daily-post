"""Cấu hình ứng dụng — đọc từ biến môi trường / file .env.

Mọi module lấy cấu hình qua `get_settings()`; test có thể gọi `reset_settings()`.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ----- Strava API app (https://www.strava.com/settings/api)
    strava_client_id: str = ""
    strava_client_secret: str = ""
    strava_webhook_verify_token: str = "vts-verify"
    # "read,activity:read" bỏ qua hoạt động "Only You"; "read,activity:read_all" lấy cả private
    strava_scope: str = "read,activity:read"
    strava_api_base: str = "https://www.strava.com/api/v3"
    strava_oauth_base: str = "https://www.strava.com/oauth"
    # Nếu đặt, bot kiểm tra runner có trong club này khi kết nối (cần scope read)
    strava_club_id: int | None = None

    # ----- URL công khai của máy chủ (Strava gọi về đây), ví dụ https://run.example.com
    public_base_url: str = "http://localhost:8080"
    host: str = "0.0.0.0"
    port: int = 8080

    # ----- Telegram
    telegram_bot_token: str = ""
    admin_telegram_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    group_chat_id: int | None = None
    notify_rejected_dm: bool = True      # nhắn riêng runner khi hoạt động bị loại
    notify_valid_dm: bool = False        # nhắn riêng runner khi hoạt động hợp lệ
    notify_valid_group: bool = False     # báo vào nhóm mỗi hoạt động hợp lệ
    auto_post_group: bool = False        # bài sáng gửi thẳng vào nhóm (mặc định chỉ gửi admin)
    daily_post_time: str = "06:30"       # HH:MM theo timezone
    timezone: str = "Asia/Ho_Chi_Minh"

    # ----- Lưu trữ
    database_url: str = "sqlite+aiosqlite:///./data/vts.db"
    token_encryption_key: str | None = None   # Fernet key (python -m app.cli gen-key); trống = lưu thô
    backup_dir: str = "./data/backups"
    backup_keep: int = 14
    raw_retention_days: int = 7               # xoá raw_json của hoạt động sau N ngày (chính sách Strava)

    # ----- Jobs
    reconcile_interval_hours: int = 3
    reconcile_lookback_days: int = 2

    # ----- Soạn bài (composer)
    llm_provider: str = "none"                # none | anthropic | gemini
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    hashtags: str = "#VTSRunningClub"
    show_daily_km: bool = True
    founded_year: int | None = 2018           # để tính "N năm VTS" trong bài; None = không nhắc

    log_level: str = "INFO"

    @field_validator("admin_telegram_ids", mode="before")
    @classmethod
    def _split_ids(cls, v):
        if isinstance(v, int):
            return [v]
        if isinstance(v, str):
            return [int(x) for x in v.replace(";", ",").replace("[", "").replace("]", "").split(",") if x.strip()]
        return v

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/auth/callback"

    @property
    def webhook_url(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/webhook"

    def is_admin(self, telegram_id: int | None) -> bool:
        return telegram_id is not None and telegram_id in self.admin_telegram_ids


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    get_settings.cache_clear()
