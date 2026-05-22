from __future__ import annotations

import json
import logging
import mimetypes
import os
import random
import re
import shutil
import signal
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import yt_dlp
from dotenv import load_dotenv


URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
COMMAND_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
LOG = logging.getLogger("telegram_automation_bot")


class TelegramAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    token: str
    api_base: str
    work_dir: Path
    download_dir: Path
    database_path: Path
    cache_chat_id: str | None
    authorized_users: frozenset[int]
    max_upload_bytes: int
    ytdlp_format: str
    ytdlp_no_playlist: bool
    ytdlp_cookies: str | None
    ytdlp_cookies_from_browser: str | None
    poll_timeout: int

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()

        token = read_secret("BOT_TOKEN", "BOT_TOKEN_FILE")
        if not token:
            raise SystemExit("BOT_TOKEN or BOT_TOKEN_FILE is required")

        work_dir = Path(os.getenv("BOT_WORK_DIR", "/tmp/telegram-automation-bot")).expanduser()
        download_dir = Path(os.getenv("BOT_DOWNLOAD_DIR", str(work_dir / "downloads"))).expanduser()
        database_path = Path(os.getenv("BOT_DATABASE", str(work_dir / "bot.sqlite3"))).expanduser()
        max_upload_mb = int(os.getenv("BOT_MAX_UPLOAD_MB", "50"))

        return cls(
            token=token.strip(),
            api_base=os.getenv("BOT_API_BASE", "https://api.telegram.org").rstrip("/"),
            work_dir=work_dir,
            download_dir=download_dir,
            database_path=database_path,
            cache_chat_id=empty_to_none(os.getenv("TELEGRAM_CACHE_CHAT_ID")),
            authorized_users=parse_user_ids(os.getenv("AUTHORIZED_USER", "")),
            max_upload_bytes=max_upload_mb * 1024 * 1024,
            ytdlp_format=os.getenv("YTDLP_FORMAT", "bv*+ba/best"),
            ytdlp_no_playlist=parse_bool(os.getenv("YTDLP_NO_PLAYLIST", "true")),
            ytdlp_cookies=empty_to_none(os.getenv("YTDLP_COOKIES")),
            ytdlp_cookies_from_browser=empty_to_none(os.getenv("YTDLP_COOKIES_FROM_BROWSER")),
            poll_timeout=int(os.getenv("BOT_POLL_TIMEOUT", "50")),
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


class TelegramAPI:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.session = requests.Session()
        self.base_url = f"{config.api_base}/bot{config.token}"

    def call(
        self,
        method: str,
        *,
        payload: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        timeout: int = 60,
    ) -> Any:
        url = f"{self.base_url}/{method}"
        if files:
            response = self.session.post(url, data=data or {}, files=files, timeout=timeout)
        else:
            response = self.session.post(url, json=payload or data or {}, timeout=timeout)

        try:
            body = response.json()
        except ValueError as exc:
            raise TelegramAPIError(f"{method} returned non-JSON HTTP {response.status_code}") from exc

        if not body.get("ok"):
            description = body.get("description", "unknown Telegram API error")
            raise TelegramAPIError(f"{method} failed: {description}")
        return body.get("result")

    def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": self.config.poll_timeout,
            "allowed_updates": ["message", "guest_message"],
        }
        if offset is not None:
            payload["offset"] = offset
        return self.call("getUpdates", payload=payload, timeout=self.config.poll_timeout + 10)

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": True,
            }
        return self.call("sendMessage", payload=payload)

    def send_chat_action(self, chat_id: int | str, action: str) -> None:
        try:
            self.call("sendChatAction", payload={"chat_id": chat_id, "action": action}, timeout=20)
        except TelegramAPIError as exc:
            LOG.debug("sendChatAction failed: %s", exc)

    def send_media(
        self,
        chat_id: int | str,
        file_path: Path,
        *,
        caption: str = "",
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        media_kind = detect_media_kind(file_path)
        method, field, action = {
            "photo": ("sendPhoto", "photo", "upload_photo"),
            "audio": ("sendAudio", "audio", "upload_document"),
            "video": ("sendVideo", "video", "upload_video"),
            "animation": ("sendAnimation", "animation", "upload_document"),
            "document": ("sendDocument", "document", "upload_document"),
        }[media_kind]

        self.send_chat_action(chat_id, action)
        data: dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption[:1024]
        if reply_to_message_id is not None:
            data["reply_parameters"] = json.dumps(
                {"message_id": reply_to_message_id, "allow_sending_without_reply": True}
            )

        with file_path.open("rb") as handle:
            files = {field: (file_path.name, handle)}
            return self.call(method, data=data, files=files, timeout=600)

    def answer_guest_query(self, guest_query_id: str, result: dict[str, Any]) -> dict[str, Any]:
        return self.call(
            "answerGuestQuery",
            payload={"guest_query_id": guest_query_id, "result": result},
            timeout=60,
        )


class RPStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS rp_commands (
                    chat_id TEXT NOT NULL,
                    command TEXT NOT NULL,
                    template TEXT NOT NULL,
                    created_by INTEGER,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (chat_id, command)
                )
                """
            )

    @contextmanager
    def connect(self) -> Any:
        db = sqlite3.connect(self.path)
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def add(self, chat_id: str, command: str, template: str, created_by: int | None) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO rp_commands (chat_id, command, template, created_by, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, command) DO UPDATE SET
                    template = excluded.template,
                    created_by = excluded.created_by,
                    created_at = excluded.created_at
                """,
                (chat_id, command, template, created_by, int(time.time())),
            )

    def remove(self, chat_id: str, command: str) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                "DELETE FROM rp_commands WHERE chat_id = ? AND command = ?",
                (chat_id, command),
            )
            return cursor.rowcount > 0

    def get(self, chat_id: str, command: str) -> str | None:
        with self.connect() as db:
            cursor = db.execute(
                "SELECT template FROM rp_commands WHERE chat_id = ? AND command = ?",
                (chat_id, command),
            )
            row = cursor.fetchone()
        return row[0] if row else None

    def list(self, chat_id: str) -> list[tuple[str, str]]:
        with self.connect() as db:
            cursor = db.execute(
                "SELECT command, template FROM rp_commands WHERE chat_id = ? ORDER BY command",
                (chat_id,),
            )
            return [(row[0], row[1]) for row in cursor.fetchall()]


@dataclass
class DownloadedMedia:
    path: Path
    title: str


class MediaDownloader:
    def __init__(self, config: Config) -> None:
        self.config = config
        config.download_dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def download(self, url: str) -> Any:
        job_dir = Path(tempfile.mkdtemp(prefix="job-", dir=self.config.download_dir))
        try:
            outtmpl = str(job_dir / "%(title).180B-%(id)s.%(ext)s")
            options = {
                "format": self.config.ytdlp_format,
                "outtmpl": outtmpl,
                "merge_output_format": "mp4",
                "noplaylist": self.config.ytdlp_no_playlist,
                "quiet": True,
                "no_warnings": True,
                "restrictfilenames": True,
            }
            if self.config.ytdlp_cookies:
                cookie_copy = job_dir / "cookies.txt"
                shutil.copyfile(self.config.ytdlp_cookies, cookie_copy)
                cookie_copy.chmod(0o600)
                options["cookiefile"] = str(cookie_copy)
            if self.config.ytdlp_cookies_from_browser:
                options["cookiesfrombrowser"] = tuple(self.config.ytdlp_cookies_from_browser.split(":"))
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)

            file_path = find_downloaded_file(job_dir)
            if not file_path:
                raise RuntimeError("yt-dlp completed without a usable output file")
            title = str(info.get("title") or file_path.stem) if isinstance(info, dict) else file_path.stem
            yield DownloadedMedia(path=file_path, title=title)
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)


def find_downloaded_file(directory: Path) -> Path | None:
    candidates = [
        path
        for path in directory.rglob("*")
        if path.is_file() and not path.name.endswith((".part", ".ytdl", ".temp"))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_size)


def detect_media_kind(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return "photo"
    if suffix in {".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav"}:
        return "audio"
    if suffix == ".gif":
        return "animation"
    mime, _ = mimetypes.guess_type(file_path.name)
    if mime:
        if mime.startswith("image/"):
            return "photo"
        if mime.startswith("audio/"):
            return "audio"
        if mime.startswith("video/"):
            return "video"
    if suffix in {".mp4", ".mov", ".mkv", ".webm", ".avi"}:
        return "video"
    return "document"


def extract_file_id(message: dict[str, Any], file_path: Path) -> tuple[str, str]:
    kind = detect_media_kind(file_path)
    if kind == "photo":
        photos = message.get("photo") or []
        if photos:
            return "photo", photos[-1]["file_id"]
    if kind in message and isinstance(message[kind], dict):
        return kind, message[kind]["file_id"]
    if message.get("document"):
        return "document", message["document"]["file_id"]
    raise RuntimeError("Telegram did not return a reusable file_id")


def cached_inline_result(kind: str, file_id: str, title: str) -> dict[str, Any]:
    result_id = uuid.uuid4().hex
    caption = title[:1024]
    if kind == "photo":
        return {
            "type": "photo",
            "id": result_id,
            "photo_file_id": file_id,
            "caption": caption,
        }
    if kind == "audio":
        return {
            "type": "audio",
            "id": result_id,
            "audio_file_id": file_id,
            "caption": caption,
        }
    if kind == "video":
        return {
            "type": "video",
            "id": result_id,
            "video_file_id": file_id,
            "title": title[:64] or "Downloaded media",
            "caption": caption,
        }
    if kind == "animation":
        return {
            "type": "gif",
            "id": result_id,
            "gif_file_id": file_id,
            "caption": caption,
        }
    return {
        "type": "document",
        "id": result_id,
        "document_file_id": file_id,
        "title": title[:64] or "Downloaded media",
        "caption": caption,
    }


def article_result(title: str, message: str) -> dict[str, Any]:
    return {
        "type": "article",
        "id": uuid.uuid4().hex,
        "title": title[:64],
        "input_message_content": {"message_text": message[:4096]},
    }


class AutomationBot:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.api = TelegramAPI(config)
        self.rp = RPStore(config.database_path)
        self.downloader = MediaDownloader(config)
        self.running = True
        self.bot_username = ""

    def run(self) -> None:
        me = self.api.call("getMe")
        self.bot_username = str(me.get("username") or "")
        guest_supported = "yes" if me.get("supports_guest_queries") else "no"
        LOG.info("Started as @%s; guest mode enabled in BotFather: %s", self.bot_username, guest_supported)
        self.api.call("deleteWebhook", payload={"drop_pending_updates": False}, timeout=30)

        offset: int | None = None
        while self.running:
            try:
                for update in self.api.get_updates(offset):
                    offset = int(update["update_id"]) + 1
                    self.handle_update(update)
            except requests.RequestException as exc:
                LOG.warning("Network error while polling: %s", exc)
                time.sleep(3)
            except TelegramAPIError as exc:
                LOG.warning("Telegram API error while polling: %s", exc)
                time.sleep(3)

    def stop(self, *_args: Any) -> None:
        self.running = False

    def handle_update(self, update: dict[str, Any]) -> None:
        if "guest_message" in update:
            self.handle_guest_message(update["guest_message"])
        elif "message" in update:
            self.handle_message(update["message"])

    def handle_message(self, message: dict[str, Any]) -> None:
        text = message.get("text") or message.get("caption") or ""
        chat_id = message["chat"]["id"]
        message_id = message.get("message_id")

        if not self.is_authorized(message):
            if text.startswith("/"):
                self.api.send_message(chat_id, "You are not authorized to use this bot.", reply_to_message_id=message_id)
            return

        if text.startswith("/start") or text.startswith("/help"):
            self.api.send_message(chat_id, help_text(), reply_to_message_id=message_id)
            return

        if text.startswith("/rp"):
            self.handle_rp_command(message, text)
            return

        url = first_url(text)
        if url:
            self.download_to_chat(message, url)
            return

        self.handle_rp_trigger(message, text)

    def handle_guest_message(self, message: dict[str, Any]) -> None:
        guest_query_id = message.get("guest_query_id")
        if not guest_query_id:
            return

        text = message.get("text") or message.get("caption") or ""
        url = first_url(strip_bot_mention(text, self.bot_username))
        if not url:
            return

        if not self.config.cache_chat_id:
            self.api.answer_guest_query(
                guest_query_id,
                article_result(
                    "Cache chat missing",
                    "Guest downloads need TELEGRAM_CACHE_CHAT_ID configured so the bot can cache files before replying.",
                ),
            )
            return

        try:
            with self.downloader.download(url) as media:
                self.ensure_size(media.path)
                cached_message = self.api.send_media(
                    self.config.cache_chat_id,
                    media.path,
                    caption=f"Guest cache: {media.title}",
                )
                kind, file_id = extract_file_id(cached_message, media.path)
                self.api.answer_guest_query(
                    guest_query_id,
                    cached_inline_result(kind, file_id, media.title),
                )
        except Exception as exc:
            LOG.exception("Guest download failed")
            self.api.answer_guest_query(
                guest_query_id,
                article_result("Download failed", f"Download failed: {exc}"),
            )

    def download_to_chat(self, message: dict[str, Any], url: str) -> None:
        chat_id = message["chat"]["id"]
        message_id = message.get("message_id")
        try:
            self.api.send_message(chat_id, "Downloading...", reply_to_message_id=message_id)
            with self.downloader.download(url) as media:
                self.ensure_size(media.path)
                self.api.send_media(
                    chat_id,
                    media.path,
                    caption=media.title,
                    reply_to_message_id=message_id,
                )
        except Exception as exc:
            LOG.exception("Download failed")
            self.api.send_message(chat_id, f"Download failed: {exc}", reply_to_message_id=message_id)

    def ensure_size(self, file_path: Path) -> None:
        size = file_path.stat().st_size
        if size > self.config.max_upload_bytes:
            mb = size / 1024 / 1024
            max_mb = self.config.max_upload_bytes / 1024 / 1024
            raise RuntimeError(f"file is {mb:.1f} MB, max upload is {max_mb:.0f} MB")

    def handle_rp_command(self, message: dict[str, Any], text: str) -> None:
        chat_id = str(message["chat"]["id"])
        reply_to = message.get("message_id")
        parts = text.split(maxsplit=3)
        action = parts[1].lower() if len(parts) > 1 else "help"

        if action == "add":
            if len(parts) < 4:
                self.api.send_message(
                    message["chat"]["id"],
                    "Usage: /rp add <command> <template>",
                    reply_to_message_id=reply_to,
                )
                return
            command = parts[2].lower()
            template = parts[3]
            if command.startswith("/") or not COMMAND_RE.match(command):
                self.api.send_message(
                    message["chat"]["id"],
                    "Command must be 1-64 letters, numbers, underscores, or dashes, without slash.",
                    reply_to_message_id=reply_to,
                )
                return
            actor_id = (message.get("from") or {}).get("id")
            self.rp.add(chat_id, command, template, actor_id)
            self.api.send_message(message["chat"]["id"], f"Added RP command: {command}", reply_to_message_id=reply_to)
            return

        if action in {"remove", "delete", "del"}:
            if len(parts) < 3:
                self.api.send_message(message["chat"]["id"], "Usage: /rp remove <command>", reply_to_message_id=reply_to)
                return
            command = parts[2].lower()
            removed = self.rp.remove(chat_id, command)
            text_out = f"Removed RP command: {command}" if removed else f"No RP command named {command}"
            self.api.send_message(message["chat"]["id"], text_out, reply_to_message_id=reply_to)
            return

        if action == "list":
            commands = self.rp.list(chat_id)
            if not commands:
                self.api.send_message(message["chat"]["id"], "No RP commands in this chat.", reply_to_message_id=reply_to)
                return
            lines = ["RP commands:"] + [f"{command}: {template}" for command, template in commands]
            self.api.send_message(message["chat"]["id"], "\n".join(lines), reply_to_message_id=reply_to)
            return

        self.api.send_message(message["chat"]["id"], rp_help_text(), reply_to_message_id=reply_to)

    def handle_rp_trigger(self, message: dict[str, Any], text: str) -> None:
        if not text.strip():
            return
        command, _, target_text = text.strip().partition(" ")
        template = self.rp.get(str(message["chat"]["id"]), command.lower())
        if template is None:
            return

        actor = display_user(message.get("from"))
        target = target_text.strip()
        reply = message.get("reply_to_message")
        if reply and reply.get("from"):
            target = display_user(reply.get("from"))

        rendered = render_template(template, actor=actor, target=target)
        if rendered.strip():
            self.api.send_message(
                message["chat"]["id"],
                rendered,
                reply_to_message_id=message.get("message_id"),
            )

    def is_authorized(self, message: dict[str, Any]) -> bool:
        if not self.config.authorized_users:
            return True
        user_id = (message.get("from") or {}).get("id")
        return user_id in self.config.authorized_users


def first_url(text: str) -> str | None:
    match = URL_RE.search(text)
    if not match:
        return None
    return match.group(0).rstrip(".,;:!?)\"]'")


def strip_bot_mention(text: str, username: str) -> str:
    if not username:
        return text
    return re.sub(rf"@{re.escape(username)}\b", "", text, flags=re.IGNORECASE).strip()


def display_user(user: dict[str, Any] | None) -> str:
    if not user:
        return ""
    username = user.get("username")
    if username:
        return f"@{username}"
    parts = [user.get("first_name"), user.get("last_name")]
    return " ".join(part for part in parts if part) or str(user.get("id", ""))


def render_template(template: str, *, actor: str, target: str) -> str:
    def replace(match: re.Match[str]) -> str:
        body = match.group(1)
        if body == "actor":
            return actor
        if body == "target":
            return target
        if "|" in body:
            choices = body.split("|")
            selected = random.choice(choices)
            return "" if selected.isspace() else selected.strip()
        return match.group(0)

    return PLACEHOLDER_RE.sub(replace, template)


def help_text() -> str:
    return "\n".join(
        [
            "Send any media URL and I will download it with yt-dlp.",
            "",
            rp_help_text(),
            "",
            "Guest mode: mention the bot with a media URL. Guest mode only downloads media; RP commands are ignored there.",
        ]
    )


def rp_help_text() -> str:
    return "\n".join(
        [
            "RP commands:",
            "/rp add <command> <template>",
            "/rp remove <command>",
            "/rp list",
            "",
            "Template placeholders: {actor}, {target}, and random choices like {warmly|softly| }.",
            "Trigger with the command word, for example: hug @user, or reply with hug.",
        ]
    )


def configure_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> None:
    configure_logging()
    config = Config.from_env()
    config.work_dir.mkdir(parents=True, exist_ok=True)
    bot = AutomationBot(config)
    signal.signal(signal.SIGTERM, bot.stop)
    signal.signal(signal.SIGINT, bot.stop)
    bot.run()
