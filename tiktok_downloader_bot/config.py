"""Environment configuration."""

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PUBLIC_BOT_API_UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True)
class Config:
    token: str
    api_base: str
    work_dir: Path
    download_dir: Path
    cache_chat_id: str | None
    authorized_users: frozenset[int]
    admin_users: frozenset[int]
    max_upload_bytes: int
    ytdlp_format: str
    ytdlp_no_playlist: bool
    ytdlp_cookies: str | None
    ytdlp_cookies_from_browser: str | None
    poll_timeout: int
    max_workers: int = 4
    user_concurrency: int = 2
    max_links_per_message: int = 3

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()

        token = read_secret("BOT_TOKEN", "BOT_TOKEN_FILE")
        if not token:
            raise SystemExit("BOT_TOKEN or BOT_TOKEN_FILE is required")

        work_dir = Path(os.getenv("BOT_WORK_DIR", "/tmp/tiktok-downloader-bot")).expanduser()
        download_dir = Path(os.getenv("BOT_DOWNLOAD_DIR", str(work_dir / "downloads"))).expanduser()
        max_upload_mb = int(os.getenv("BOT_MAX_UPLOAD_MB", "50"))

        return cls(
            token=token.strip(),
            api_base=os.getenv("BOT_API_BASE", "https://api.telegram.org").rstrip("/"),
            work_dir=work_dir,
            download_dir=download_dir,
            cache_chat_id=empty_to_none(os.getenv("TELEGRAM_CACHE_CHAT_ID")),
            authorized_users=parse_user_ids(os.getenv("AUTHORIZED_USER", "")),
            admin_users=parse_user_ids(os.getenv("ADMIN_USER", "")),
            max_upload_bytes=max_upload_mb * 1024 * 1024,
            ytdlp_format=os.getenv("YTDLP_FORMAT", "bv*+ba/best"),
            ytdlp_no_playlist=parse_bool(os.getenv("YTDLP_NO_PLAYLIST", "true")),
            ytdlp_cookies=empty_to_none(os.getenv("YTDLP_COOKIES")),
            ytdlp_cookies_from_browser=empty_to_none(os.getenv("YTDLP_COOKIES_FROM_BROWSER")),
            poll_timeout=int(os.getenv("BOT_POLL_TIMEOUT", "50")),
            max_workers=max(1, int(os.getenv("BOT_MAX_WORKERS", "4"))),
            user_concurrency=max(1, int(os.getenv("BOT_USER_CONCURRENCY", "2"))),
            max_links_per_message=max(1, int(os.getenv("BOT_MAX_LINKS", "3"))),
        )


def read_secret(value_name: str, file_name: str) -> str:
    value = os.getenv(value_name)
    if value:
        return value
    path = os.getenv(file_name)
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    return ""


def empty_to_none(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value.strip()


def parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_user_ids(value: str) -> frozenset[int]:
    result: set[int] = set()
    for raw in value.split(","):
        raw = raw.strip()
        if not raw:
            continue
        result.add(int(raw))
    return frozenset(result)


def is_public_bot_api(api_base: str) -> bool:
    parsed = urllib.parse.urlparse(api_base)
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == "api.telegram.org"


def effective_upload_limit_bytes(config: Config) -> int:
    configured_limit = int(config.max_upload_bytes)
    if is_public_bot_api(config.api_base):
        return min(configured_limit, PUBLIC_BOT_API_UPLOAD_LIMIT_BYTES)
    return configured_limit
