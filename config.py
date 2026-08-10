"""Конфигурация бота из переменных окружения."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    bot_token: str
    max_file_size_mb: int
    cookies_file: str | None
    telegram_api_base: str | None
    auto_update_hours: int
    download_dir: str
    owner_id: int | None

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def is_local_server(self) -> bool:
        """True, если ходим через свой Bot API server (path-режим без аплоада)."""
        return bool(self.telegram_api_base)


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "BOT_TOKEN не задан. Скопируй .env.example в .env и впиши токен от @BotFather."
        )

    # Путь к cookies храним как задан в env (без проверки наличия) — файл может
    # появиться позже через owner-flow. Наличие проверяет downloader при скачивании.
    cookies = os.getenv("COOKIES_FILE", "").strip() or None

    api_base = os.getenv("TELEGRAM_API_BASE", "").strip() or None

    owner = os.getenv("OWNER_ID", "").strip()
    owner_id = int(owner) if owner.isdigit() else None

    return Config(
        bot_token=token,
        max_file_size_mb=int(os.getenv("MAX_FILE_SIZE_MB", "49")),
        cookies_file=cookies,
        telegram_api_base=api_base,
        auto_update_hours=int(os.getenv("YTDLP_AUTO_UPDATE_HOURS", "0")),
        download_dir=os.getenv("DOWNLOAD_DIR", "./downloads").strip() or "./downloads",
        owner_id=owner_id,
    )
