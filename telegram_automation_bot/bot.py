from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import shutil
import signal
import tempfile
import time
import uuid
import urllib.parse
from html import unescape
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
import yt_dlp
from dotenv import load_dotenv


URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
TIKTOK_PHOTO_URL_RE = re.compile(r"https?://(?:www\.)?tiktok\.com/@[^/\s]+/photo/(?P<id>\d+)", re.IGNORECASE)
LOG = logging.getLogger("telegram_automation_bot")
TELEGRAM_MEDIA_GROUP_LIMIT = 10
PUBLIC_BOT_API_UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024
TIKTOK_HOSTS = {
    "tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
}
TIKTOK_SHORT_HOSTS = {"vm.tiktok.com", "vt.tiktok.com"}
YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}
YOUTUBE_QUALITY_HEIGHTS = (1080, 720, 480, 360)
PENDING_DOWNLOAD_TTL_SECONDS = 15 * 60
TIKTOK_WEB_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
        "Mobile/15E148 Safari/604.1"
    ),
}


class TelegramAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChatContext:
    chat_id: int | str
    reply_to_message_id: int | None = None
    requester_id: int | None = None
    business_connection_id: str | None = None
    direct_messages_topic_id: int | None = None
    guest_query_id: str | None = None

    @classmethod
    def from_message(cls, message: dict[str, Any]) -> "ChatContext":
        topic = message.get("direct_messages_topic")
        direct_topic_id = message.get("direct_messages_topic_id")
        if direct_topic_id is None and isinstance(topic, dict):
            direct_topic_id = topic.get("topic_id")
        return cls(
            chat_id=message["chat"]["id"],
            reply_to_message_id=message.get("message_id"),
            requester_id=message_user_id(message),
            business_connection_id=empty_to_none(message.get("business_connection_id")),
            direct_messages_topic_id=int(direct_topic_id) if direct_topic_id is not None else None,
            guest_query_id=empty_to_none(message.get("guest_query_id")),
        )

    def without_reply(self) -> "ChatContext":
        return ChatContext(
            chat_id=self.chat_id,
            requester_id=self.requester_id,
            business_connection_id=self.business_connection_id,
            direct_messages_topic_id=self.direct_messages_topic_id,
            guest_query_id=self.guest_query_id,
        )

    def with_reply(self, message_id: int | None) -> "ChatContext":
        return ChatContext(
            chat_id=self.chat_id,
            reply_to_message_id=message_id,
            requester_id=self.requester_id,
            business_connection_id=self.business_connection_id,
            direct_messages_topic_id=self.direct_messages_topic_id,
            guest_query_id=self.guest_query_id,
        )

    def payload(self, *, include_reply: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": self.chat_id}
        if self.business_connection_id:
            payload["business_connection_id"] = self.business_connection_id
        if self.direct_messages_topic_id is not None:
            payload["direct_messages_topic_id"] = self.direct_messages_topic_id
        if include_reply and self.reply_to_message_id is not None:
            payload["reply_parameters"] = {
                "message_id": self.reply_to_message_id,
                "allow_sending_without_reply": True,
            }
        return payload


@dataclass(frozen=True)
class Config:
    token: str
    api_base: str
    work_dir: Path
    download_dir: Path
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
        max_upload_mb = int(os.getenv("BOT_MAX_UPLOAD_MB", "50"))

        return cls(
            token=token.strip(),
            api_base=os.getenv("BOT_API_BASE", "https://api.telegram.org").rstrip("/"),
            work_dir=work_dir,
            download_dir=download_dir,
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
            "allowed_updates": ["message", "callback_query", "guest_message"],
        }
        if offset is not None:
            payload["offset"] = offset
        return self.call("getUpdates", payload=payload, timeout=self.config.poll_timeout + 10)

    def send_message(
        self,
        context: ChatContext,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {**context.payload(include_reply=True), "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self.call("sendMessage", payload=payload)

    def edit_message_text(
        self,
        context: ChatContext,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any] | bool:
        payload: dict[str, Any] = {
            **context.payload(),
            "message_id": message_id,
            "text": text,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self.call("editMessageText", payload=payload)

    def delete_message(self, context: ChatContext, message_id: int) -> bool:
        try:
            return self.call("deleteMessage", payload={**context.payload(), "message_id": message_id})
        except TelegramAPIError as exc:
            LOG.debug("deleteMessage failed: %s", exc)
            return False

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> bool:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:200]
        return self.call("answerCallbackQuery", payload=payload, timeout=20)

    def send_chat_action(self, context: ChatContext, action: str) -> None:
        try:
            self.call("sendChatAction", payload={**context.payload(), "action": action}, timeout=20)
        except TelegramAPIError as exc:
            LOG.debug("sendChatAction failed: %s", exc)

    def send_media(
        self,
        context: ChatContext,
        file_path: Path,
        *,
        caption: str = "",
    ) -> dict[str, Any]:
        media_kind = detect_media_kind(file_path)
        method, field, action = {
            "photo": ("sendPhoto", "photo", "upload_photo"),
            "audio": ("sendAudio", "audio", "upload_document"),
            "video": ("sendVideo", "video", "upload_video"),
            "animation": ("sendAnimation", "animation", "upload_document"),
            "document": ("sendDocument", "document", "upload_document"),
        }[media_kind]

        self.send_chat_action(context, action)
        data: dict[str, Any] = stringify_payload(context.payload(include_reply=True))
        if caption:
            data["caption"] = caption[:1024]

        with file_path.open("rb") as handle:
            files = {field: (file_path.name, handle)}
            return self.call(method, data=data, files=files, timeout=600)

    def send_photo_album(
        self,
        context: ChatContext,
        file_paths: tuple[Path, ...],
        *,
        caption: str = "",
    ) -> list[dict[str, Any]]:
        if not 2 <= len(file_paths) <= TELEGRAM_MEDIA_GROUP_LIMIT:
            raise ValueError("Telegram media groups must contain 2-10 photos")

        self.send_chat_action(context, "upload_photo")
        data: dict[str, Any] = {
            **stringify_payload(context.payload(include_reply=True)),
            "media": json.dumps(
                [
                    {
                        **({"caption": caption[:1024]} if index == 0 and caption else {}),
                        "type": "photo",
                        "media": f"attach://photo{index}",
                    }
                    for index, _path in enumerate(file_paths)
                ]
            ),
        }

        with ExitStack() as stack:
            files = {
                f"photo{index}": (path.name, stack.enter_context(path.open("rb")))
                for index, path in enumerate(file_paths)
            }
            return self.call("sendMediaGroup", data=data, files=files, timeout=600)

    def answer_guest_query(self, guest_query_id: str, result: dict[str, Any]) -> dict[str, Any]:
        return self.call(
            "answerGuestQuery",
            payload={"guest_query_id": guest_query_id, "result": result},
            timeout=60,
        )


@dataclass
class DownloadedMedia:
    paths: tuple[Path, ...]
    title: str

    @property
    def path(self) -> Path:
        return self.paths[0]

    @property
    def is_photo_album(self) -> bool:
        return len(self.paths) > 1 and all(detect_media_kind(path) == "photo" for path in self.paths)


@dataclass(frozen=True)
class QualityOption:
    key: str
    label: str
    format_selector: str | None
    approx_bytes: int | None = None


@dataclass
class DownloadSession:
    token: str
    url: str
    context: ChatContext
    requester_id: int | None
    created_at: float
    options: dict[str, QualityOption] = field(default_factory=dict)
    selected_quality: QualityOption | None = None
    status_message_id: int | None = None
    attempt_count: int = 0
    last_error: str | None = None

    def is_expired(self) -> bool:
        return time.time() - self.created_at > PENDING_DOWNLOAD_TTL_SECONDS

    def action_keyboard(self) -> dict[str, Any]:
        rows: list[list[dict[str, str]]] = [
            [{"text": "Retry", "callback_data": f"dls:{self.token}:retry"}],
        ]
        if is_youtube_url(self.url):
            rows.append([{"text": "Quality", "callback_data": f"dls:{self.token}:quality"}])
        rows.append([{"text": "Cancel", "callback_data": f"dls:{self.token}:cancel"}])
        return {"inline_keyboard": rows}

class MediaDownloader:
    def __init__(self, config: Config) -> None:
        self.config = config
        config.download_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()

    @contextmanager
    def download(self, url: str, *, format_selector: str | None = None) -> Any:
        job_dir = Path(tempfile.mkdtemp(prefix="job-", dir=self.config.download_dir))
        try:
            download_url = self.resolve_tiktok_url(url)
            if is_tiktok_photo_url(download_url):
                yield self.download_tiktok_photo_post(download_url, job_dir)
                return

            outtmpl = str(job_dir / "%(title).180B-%(id)s.%(ext)s")
            options = {
                "format": format_selector or self.config.ytdlp_format,
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
                try:
                    info = ydl.extract_info(download_url, download=True)
                except yt_dlp.utils.DownloadError:
                    if is_tiktok_url(download_url):
                        try:
                            yield self.download_tiktok_photo_post(download_url, job_dir)
                            return
                        except Exception as photo_exc:
                            LOG.debug("TikTok photo fallback failed for %s: %s", download_url, photo_exc)
                    raise

            downloaded_files = find_downloaded_files(job_dir)
            if not downloaded_files:
                raise RuntimeError("yt-dlp completed without a usable output file")
            media_paths = select_downloaded_media(downloaded_files)
            title = str(info.get("title") or media_paths[0].stem) if isinstance(info, dict) else media_paths[0].stem
            yield DownloadedMedia(paths=media_paths, title=title)
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    def youtube_quality_options(self, url: str) -> list[QualityOption]:
        with tempfile.TemporaryDirectory(prefix="ytdlp-quality-", dir=self.config.download_dir) as tmp_dir:
            tmp_path = Path(tmp_dir)
            options = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": self.config.ytdlp_no_playlist,
                "skip_download": True,
            }
            if self.config.ytdlp_cookies:
                cookie_copy = tmp_path / "cookies.txt"
                shutil.copyfile(self.config.ytdlp_cookies, cookie_copy)
                cookie_copy.chmod(0o600)
                options["cookiefile"] = str(cookie_copy)
            if self.config.ytdlp_cookies_from_browser:
                options["cookiesfrombrowser"] = tuple(self.config.ytdlp_cookies_from_browser.split(":"))

            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)

        formats = info.get("formats") if isinstance(info, dict) else None
        if not isinstance(formats, list):
            formats = []
        duration = float(info.get("duration") or 0) if isinstance(info, dict) else 0.0
        audio = best_audio_format(formats)

        choices: list[QualityOption] = [
            QualityOption("auto", "Auto", None),
        ]
        seen_heights: set[int] = set()
        for height in YOUTUBE_QUALITY_HEIGHTS:
            video = best_video_format(formats, max_height=height)
            if not video:
                continue
            selected_height = int(video.get("height") or height)
            if selected_height in seen_heights:
                continue
            seen_heights.add(selected_height)
            approx_bytes = combined_format_size(video, audio, duration)
            label = f"{selected_height}p"
            if approx_bytes:
                label = f"{label} ~{format_bytes(approx_bytes)}"
            choices.append(
                QualityOption(
                    key=f"h{selected_height}",
                    label=label,
                    format_selector=quality_format_selector(selected_height),
                    approx_bytes=approx_bytes,
                )
            )
        return choices

    def resolve_tiktok_url(self, url: str) -> str:
        if not is_tiktok_url(url) or not is_tiktok_redirect_url(url):
            return url

        try:
            with self.session.get(
                url,
                headers=TIKTOK_WEB_HEADERS,
                allow_redirects=True,
                stream=True,
                timeout=30,
            ) as response:
                response.raise_for_status()
                return response.url or url
        except requests.RequestException as exc:
            LOG.debug("Could not resolve TikTok redirect URL %s: %s", url, exc)
            return url

    def download_tiktok_photo_post(self, url: str, job_dir: Path) -> DownloadedMedia:
        response = self.session.get(url, headers=TIKTOK_WEB_HEADERS, timeout=30)
        response.raise_for_status()

        data = extract_tiktok_rehydration_data(response.text)
        if not data:
            raise RuntimeError("TikTok photo post did not expose readable page metadata")

        item = extract_tiktok_item_struct(data)
        image_urls = extract_tiktok_image_urls(item)
        if not image_urls:
            raise RuntimeError("TikTok photo post did not expose image URLs")

        title = str(item.get("desc") or f"TikTok photo post {tiktok_photo_id(url) or ''}").strip()
        paths = tuple(
            self.download_tiktok_image(image_url, job_dir, index=index, referer=url)
            for index, image_url in enumerate(image_urls, start=1)
        )
        return DownloadedMedia(paths=paths, title=title)

    def download_tiktok_image(self, url: str, job_dir: Path, *, index: int, referer: str) -> Path:
        response = self.session.get(
            normalize_url(url),
            headers={**TIKTOK_WEB_HEADERS, "Referer": referer},
            stream=True,
            timeout=60,
        )
        response.raise_for_status()

        extension = extension_from_response(url, response.headers.get("Content-Type"))
        path = job_dir / f"tiktok-photo-{index:03d}{extension}"
        with path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    handle.write(chunk)
        return path


def find_downloaded_files(directory: Path) -> list[Path]:
    return [
        path
        for path in directory.rglob("*")
        if path.is_file()
        and path.name != "cookies.txt"
        and not path.name.endswith((".part", ".ytdl", ".temp", ".tmp"))
    ]


def select_downloaded_media(paths: list[Path]) -> tuple[Path, ...]:
    if not paths:
        return ()

    sorted_paths = tuple(sorted(paths, key=natural_path_key))
    if len(sorted_paths) > 1 and all(detect_media_kind(path) == "photo" for path in sorted_paths):
        return sorted_paths
    return (max(paths, key=lambda path: path.stat().st_size),)


def natural_path_key(path: Path) -> list[int | str]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]


def chunk_paths(paths: tuple[Path, ...], size: int) -> list[tuple[Path, ...]]:
    return [paths[index : index + size] for index in range(0, len(paths), size)]


def photo_album_chunks(paths: tuple[Path, ...]) -> list[tuple[Path, ...]]:
    albums = chunk_paths(paths, TELEGRAM_MEDIA_GROUP_LIMIT)
    if len(albums) > 1 and len(albums[-1]) == 1:
        moved = albums[-2][-1:]
        albums[-2] = albums[-2][:-1]
        albums[-1] = moved + albums[-1]
    return albums


def quality_keyboard(token: str, options: list[QualityOption]) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = []
    for option in options:
        rows.append(
            [
                {
                    "text": option.label,
                    "callback_data": f"dlq:{token}:{option.key}",
                }
            ]
        )
    rows.append([{"text": "Cancel", "callback_data": f"dls:{token}:cancel"}])
    return {"inline_keyboard": rows}


def stringify_payload(payload: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            result[key] = json.dumps(value)
        else:
            result[key] = str(value)
    return result


def quality_format_selector(height: int) -> str:
    return f"bv*[height<={height}]+ba/b[height<={height}]/best"


def best_video_format(formats: list[Any], *, max_height: int) -> dict[str, Any] | None:
    videos = [
        fmt
        for fmt in formats
        if isinstance(fmt, dict)
        and fmt.get("vcodec") not in {None, "none"}
        and fmt.get("height")
        and int(fmt["height"]) <= max_height
        and not is_storyboard_format(fmt)
    ]
    if not videos:
        return None
    return max(videos, key=lambda fmt: (int(fmt.get("height") or 0), float(fmt.get("tbr") or 0)))


def best_audio_format(formats: list[Any]) -> dict[str, Any] | None:
    audios = [
        fmt
        for fmt in formats
        if isinstance(fmt, dict)
        and fmt.get("acodec") not in {None, "none"}
        and fmt.get("vcodec") in {None, "none"}
    ]
    if not audios:
        return None
    return max(audios, key=lambda fmt: float(fmt.get("abr") or fmt.get("tbr") or 0))


def is_storyboard_format(fmt: dict[str, Any]) -> bool:
    protocol = str(fmt.get("protocol") or "").lower()
    format_note = str(fmt.get("format_note") or "").lower()
    return "storyboard" in protocol or "storyboard" in format_note


def combined_format_size(
    video: dict[str, Any],
    audio: dict[str, Any] | None,
    duration_seconds: float,
) -> int | None:
    video_size = format_size_bytes(video, duration_seconds)
    audio_size = format_size_bytes(audio, duration_seconds) if audio else None
    if video_size and audio_size and video.get("acodec") in {None, "none"}:
        return video_size + audio_size
    return video_size or audio_size


def format_size_bytes(fmt: dict[str, Any] | None, duration_seconds: float) -> int | None:
    if not fmt:
        return None
    for key in ("filesize", "filesize_approx"):
        value = fmt.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    bitrate_kbps = fmt.get("tbr") or fmt.get("vbr") or fmt.get("abr")
    if isinstance(bitrate_kbps, (int, float)) and bitrate_kbps > 0 and duration_seconds > 0:
        return int(duration_seconds * bitrate_kbps * 1000 / 8)
    return None


def format_bytes(size: int) -> str:
    mb = size / 1024 / 1024
    if mb < 1024:
        return f"{mb:.0f} MB"
    return f"{mb / 1024:.1f} GB"


def is_youtube_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    return host in YOUTUBE_HOSTS or host.endswith(".youtube.com")


def is_tiktok_photo_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    path_parts = [part for part in parsed.path.split("/") if part]
    return (
        host in {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}
        and len(path_parts) >= 3
        and path_parts[0].startswith("@")
        and path_parts[1] == "photo"
        and path_parts[2].isdigit()
    ) or bool(TIKTOK_PHOTO_URL_RE.search(url))


def is_tiktok_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    return host in TIKTOK_HOSTS


def is_tiktok_redirect_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    path_parts = [part for part in parsed.path.split("/") if part]
    return host in TIKTOK_SHORT_HOSTS or (
        host in {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}
        and len(path_parts) >= 2
        and path_parts[0] == "t"
    )


def is_downloadable_url(url: str) -> bool:
    return is_tiktok_url(url) or is_youtube_url(url)


def tiktok_photo_id(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) >= 3 and path_parts[1] == "photo" and path_parts[2].isdigit():
        return path_parts[2]
    match = TIKTOK_PHOTO_URL_RE.search(url)
    return match.group("id") if match else None


def extract_tiktok_rehydration_data(webpage: str) -> dict[str, Any]:
    for script_id in ("__UNIVERSAL_DATA_FOR_REHYDRATION__", "SIGI_STATE"):
        match = re.search(
            rf'<script[^>]+\bid=["\']{re.escape(script_id)}["\'][^>]*>(?P<body>.*?)</script>',
            webpage,
            flags=re.DOTALL,
        )
        if not match:
            continue
        try:
            data = json.loads(unescape(match.group("body")).strip())
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            if script_id == "__UNIVERSAL_DATA_FOR_REHYDRATION__":
                scope = data.get("__DEFAULT_SCOPE__")
                return scope if isinstance(scope, dict) else data
            return data
    return {}


def extract_tiktok_item_struct(data: dict[str, Any]) -> dict[str, Any]:
    candidates: list[Any] = [
        data.get("webapp.video-detail"),
        data.get("ItemModule"),
        data,
    ]

    for candidate in candidates:
        item = item_struct_from_candidate(candidate)
        if item:
            return item

    for value in walk_json(data):
        item = item_struct_from_candidate(value)
        if item:
            return item

    return {}


def item_struct_from_candidate(candidate: Any) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        return {}

    item = candidate.get("itemInfo", {}).get("itemStruct") if isinstance(candidate.get("itemInfo"), dict) else None
    if isinstance(item, dict):
        return item

    item = candidate.get("itemStruct")
    if isinstance(item, dict):
        return item

    if "imagePost" in candidate or "image_post_info" in candidate:
        return candidate

    if candidate and all(isinstance(value, dict) for value in candidate.values()):
        for value in candidate.values():
            if "imagePost" in value or "image_post_info" in value:
                return value

    return {}


def extract_tiktok_image_urls(item: dict[str, Any]) -> tuple[str, ...]:
    image_post = item.get("imagePost") or item.get("image_post_info") or {}
    if not isinstance(image_post, dict):
        return ()

    raw_images = image_post.get("images") or image_post.get("image_list") or []
    urls: list[str] = []
    for image in raw_images if isinstance(raw_images, list) else []:
        if not isinstance(image, dict):
            continue
        url = first_tiktok_image_url(image)
        if url and url not in urls:
            urls.append(url)
    return tuple(urls)


def first_tiktok_image_url(image: dict[str, Any]) -> str | None:
    preferred_keys = (
        "imageURL",
        "image_url",
        "displayImage",
        "display_image",
        "originCover",
        "origin_cover",
    )
    for key in preferred_keys:
        url = first_url_from_json(image.get(key))
        if url:
            return url
    return first_url_from_json(image)


def first_url_from_json(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith(("http://", "https://", "//")):
        return value
    if isinstance(value, list):
        for item in value:
            url = first_url_from_json(item)
            if url:
                return url
    if isinstance(value, dict):
        for key in ("urlList", "url_list", "urls", "UrlList", "src", "url"):
            url = first_url_from_json(value.get(key))
            if url:
                return url
        for item in value.values():
            url = first_url_from_json(item)
            if url:
                return url
    return None


def walk_json(value: Any) -> Any:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk_json(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_json(item)


def normalize_url(url: str) -> str:
    return f"https:{url}" if url.startswith("//") else url


def extension_from_response(url: str, content_type: str | None) -> str:
    parsed_extension = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if parsed_extension in {".jpg", ".jpeg", ".png", ".webp"}:
        return parsed_extension

    if content_type:
        content_type = content_type.split(";", maxsplit=1)[0].strip().lower()
        if content_type == "image/jpeg":
            return ".jpg"
        extension = mimetypes.guess_extension(content_type)
        if extension in {".jpg", ".jpeg", ".png", ".webp"}:
            return extension

    return ".jpg"


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
        self.downloader = MediaDownloader(config)
        self.running = True
        self.bot_username = ""
        self.sessions: dict[str, DownloadSession] = {}

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
        elif "callback_query" in update:
            self.handle_callback_query(update["callback_query"])
        elif "message" in update:
            self.handle_message(update["message"])

    def handle_message(self, message: dict[str, Any]) -> None:
        text = message.get("text") or message.get("caption") or ""
<<<<<<< HEAD
        chat_id = message["chat"]["id"]
        message_id = message.get("message_id")
        url = first_download_url(strip_bot_mention(text, self.bot_username))
=======
        context = ChatContext.from_message(message)
        url = first_url(strip_bot_mention(text, self.bot_username))
>>>>>>> 2ec99ef (f)
        authorized = self.is_authorized(message)

        if not authorized:
            if url and is_group_chat(message) and not text.lstrip().startswith("/"):
                self.prepare_download_to_chat(message, url)
                return
            if text.startswith("/"):
                self.api.send_message(context, "You are not authorized to use this bot.")
            return

        if text.startswith("/start") or text.startswith("/help"):
            self.api.send_message(context, help_text())
            return

        if text.startswith("/"):
            self.api.send_message(context, "Unknown command. Send a media URL to download it.")
            return

        if url:
            self.prepare_download_to_chat(message, url)
            return

    def prepare_download_to_chat(self, message: dict[str, Any], url: str) -> None:
        if is_youtube_url(url):
            self.prompt_youtube_quality(message, url)
            return
        self.download_to_chat(message, url)

    def prompt_youtube_quality(self, message: dict[str, Any], url: str) -> None:
        try:
            options = self.downloader.youtube_quality_options(url)
            token = uuid.uuid4().hex[:12]
            session = DownloadSession(
                token=token,
                url=url,
                context=ChatContext.from_message(message),
                requester_id=message_user_id(message),
                created_at=time.time(),
                options={option.key: option for option in options},
            )
            self.sessions[token] = session
            self.prune_sessions()
            self.send_quality_prompt(session, options)
        except Exception as exc:
            LOG.exception("Could not prepare YouTube quality choices")
            self.api.send_message(ChatContext.from_message(message), friendly_error_message(exc))

    def handle_callback_query(self, callback_query: dict[str, Any]) -> None:
        callback_id = str(callback_query.get("id") or "")
        data = str(callback_query.get("data") or "")
        if not (data.startswith("dlq:") or data.startswith("dls:")):
            if callback_id:
                self.api.answer_callback_query(callback_id)
            return

        prefix, token, action = data.split(":", maxsplit=2) if data.count(":") >= 2 else ("", "", "")
        session = self.callback_session(token, callback_query, callback_id)
        if not session:
            return

        message = callback_query.get("message") or {}
        callback_message_id = message.get("message_id")

        if prefix == "dlq":
            option = session.options.get(action)
            if not option:
                if callback_id:
                    self.api.answer_callback_query(callback_id, "Unknown quality choice.")
                return
            session.selected_quality = option
            if callback_id:
                self.api.answer_callback_query(callback_id, f"Downloading {option.label}...")
            self.run_download_session(session, callback_message_id=int(callback_message_id) if callback_message_id else None)
            return

        if action == "retry":
            session.attempt_count += 1
            if callback_id:
                self.api.answer_callback_query(callback_id, "Retrying...")
            self.run_download_session(session, callback_message_id=int(callback_message_id) if callback_message_id else None)
            return

        if action == "quality" and is_youtube_url(session.url):
            try:
                options = self.downloader.youtube_quality_options(session.url)
                session.options = {option.key: option for option in options}
                if callback_id:
                    self.api.answer_callback_query(callback_id, "Choose quality.")
                self.send_quality_prompt(session, options)
            except Exception as exc:
                LOG.exception("Could not prepare YouTube quality choices")
                if callback_id:
                    self.api.answer_callback_query(callback_id, friendly_error_message(exc))
            return

        if action == "cancel":
            self.sessions.pop(session.token, None)
            if callback_id:
                self.api.answer_callback_query(callback_id, "Canceled.")
            if callback_message_id:
                self.safe_edit_message(session.context, int(callback_message_id), "Download canceled.")
            return

        if callback_id:
            self.api.answer_callback_query(callback_id, "Unknown download action.")

    def handle_guest_message(self, message: dict[str, Any]) -> None:
        guest_query_id = message.get("guest_query_id")
        if not guest_query_id:
            return

        text = message.get("text") or message.get("caption") or ""
        url = first_download_url(strip_bot_mention(text, self.bot_username))
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
                if media.is_photo_album:
                    self.api.answer_guest_query(
                        guest_query_id,
                        article_result(
                            "TikTok photo album",
                            (
                                f"This TikTok post contains {len(media.paths)} images. "
                                "Telegram guest replies cannot send albums; send the link in a normal chat with the bot."
                            ),
                        ),
                    )
                    return

                self.ensure_size(media.path)
                cached_message = self.api.send_media(
                    ChatContext(chat_id=self.config.cache_chat_id),
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
                article_result("Download failed", friendly_error_message(exc)),
            )

    def download_to_chat(self, message: dict[str, Any], url: str) -> None:
        token = uuid.uuid4().hex[:12]
        session = DownloadSession(
            token=token,
            url=url,
            context=ChatContext.from_message(message),
            requester_id=message_user_id(message),
            created_at=time.time(),
        )
        self.sessions[token] = session
        self.run_download_session(session)

    def run_download_session(
        self,
        session: DownloadSession,
        *,
        callback_message_id: int | None = None,
    ) -> None:
        status_context = session.context.without_reply()
        try:
            if callback_message_id:
                session.status_message_id = callback_message_id
                self.safe_edit_message(status_context, callback_message_id, "Downloading...")
            else:
                status_msg = self.api.send_message(session.context, "Downloading...")
                session.status_message_id = int(status_msg.get("message_id") or 0) or None

            format_selector = session.selected_quality.format_selector if session.selected_quality else None
            with self.downloader.download(session.url, format_selector=format_selector) as media:
                self.send_downloaded_media(session.context, media)

            if is_youtube_url(session.url):
                self.finish_session_status(session, "Download complete.")
            else:
                if session.status_message_id:
                    self.api.delete_message(status_context, session.status_message_id)
                self.sessions.pop(session.token, None)
        except Exception as exc:
            LOG.exception("Download failed")
            session.last_error = str(exc)
            self.finish_session_status(session, friendly_error_message(exc))

    def finish_session_status(self, session: DownloadSession, text: str) -> None:
        if session.status_message_id:
            self.safe_edit_message(
                session.context.without_reply(),
                session.status_message_id,
                text,
                reply_markup=session.action_keyboard(),
            )
        else:
            status_msg = self.api.send_message(
                session.context,
                text,
                reply_markup=session.action_keyboard(),
            )
            session.status_message_id = int(status_msg.get("message_id") or 0) or None

    def send_quality_prompt(self, session: DownloadSession, options: list[QualityOption]) -> None:
        self.api.send_message(
            session.context,
            "Choose YouTube quality:",
            reply_markup=quality_keyboard(session.token, options),
        )

    def callback_session(
        self,
        token: str,
        callback_query: dict[str, Any],
        callback_id: str,
    ) -> DownloadSession | None:
        session = self.sessions.get(token)
        callback_user_id = message_user_id(callback_query)
        if not session or session.is_expired():
            if callback_id:
                self.api.answer_callback_query(callback_id, "This download expired.")
            self.sessions.pop(token, None)
            return None
        if session.requester_id is not None and callback_user_id != session.requester_id:
            if callback_id:
                self.api.answer_callback_query(callback_id, "Only the requester can use this download.")
            return None
        return session

    def safe_edit_message(
        self,
        context: ChatContext,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        try:
            self.api.edit_message_text(context, message_id, text, reply_markup=reply_markup)
        except TelegramAPIError as exc:
            LOG.debug("Could not edit message: %s", exc)

    def prune_sessions(self) -> None:
        cutoff = time.time() - PENDING_DOWNLOAD_TTL_SECONDS
        for token, session in list(self.sessions.items()):
            if session.created_at < cutoff:
                self.sessions.pop(token, None)

    def send_downloaded_media(
        self,
        context: ChatContext,
        media: DownloadedMedia,
    ) -> None:
        if media.is_photo_album:
            for album in photo_album_chunks(media.paths):
                for path in album:
                    self.ensure_size(path)
                if len(album) == 1:
                    self.api.send_media(
                        context,
                        album[0],
                        caption=media.title,
                    )
                else:
                    self.send_album_chunk(context, album, media.title)
            return

        self.ensure_size(media.path)
        self.api.send_media(
            context,
            media.path,
            caption=media.title,
        )

    def send_album_chunk(self, context: ChatContext, album: tuple[Path, ...], title: str) -> None:
        try:
            self.api.send_photo_album(context, album, caption=title)
            return
        except TelegramAPIError:
            LOG.debug("Photo album upload failed; retrying once", exc_info=True)

        try:
            self.api.send_photo_album(context, album, caption=title)
            return
        except TelegramAPIError:
            LOG.debug("Photo album upload retry failed; falling back to individual photos", exc_info=True)

        for index, path in enumerate(album):
            self.api.send_media(context, path, caption=title if index == 0 else "")
        self.api.send_message(context.without_reply(), "Album upload failed; sent photos one by one.")

    def ensure_size(self, file_path: Path) -> None:
        size = file_path.stat().st_size
        max_upload_bytes = effective_upload_limit_bytes(self.config)
        if size > max_upload_bytes:
            mb = size / 1024 / 1024
            max_mb = max_upload_bytes / 1024 / 1024
            raise RuntimeError(f"file is {mb:.1f} MB, max upload is {max_mb:.0f} MB")

    def is_authorized(self, message: dict[str, Any]) -> bool:
        if not self.config.authorized_users:
            return True
        user_id = (message.get("from") or {}).get("id")
        return user_id in self.config.authorized_users


def is_group_chat(message: dict[str, Any]) -> bool:
    chat_type = (message.get("chat") or {}).get("type")
    return chat_type in {"group", "supergroup"}


def effective_upload_limit_bytes(config: Config) -> int:
    configured_limit = int(config.max_upload_bytes)
    if is_public_bot_api(config.api_base):
        return min(configured_limit, PUBLIC_BOT_API_UPLOAD_LIMIT_BYTES)
    return configured_limit


def friendly_error_message(exc: BaseException) -> str:
    message = str(exc)
    lower = message.lower()
    if "max upload is" in lower or "file is " in lower and " mb" in lower:
        return "The file is too large for Telegram upload."
    if isinstance(exc, TelegramAPIError):
        return "Telegram upload failed."
    if isinstance(exc, TimeoutError) or "timed out" in lower or "timeout" in lower:
        return "Network timeout while downloading media."
    if any(needle in lower for needle in ("private video", "unavailable", "not available", "login required")):
        return "Media is unavailable or private."
    if any(needle in lower for needle in ("without a usable output file", "metadata", "image urls")):
        return "Could not find downloadable media metadata."
    return "Download failed."


def is_public_bot_api(api_base: str) -> bool:
    parsed = urllib.parse.urlparse(api_base)
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == "api.telegram.org"


def first_url(text: str) -> str | None:
    match = URL_RE.search(text)
    if not match:
        return None
    return match.group(0).rstrip(".,;:!?)\"]'")


def first_download_url(text: str) -> str | None:
    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:!?)\"]'")
        if is_downloadable_url(url):
            return url
    return None


def strip_bot_mention(text: str, username: str) -> str:
    if not username:
        return text
    return re.sub(rf"@{re.escape(username)}\b", "", text, flags=re.IGNORECASE).strip()


def message_user_id(message: dict[str, Any]) -> int | None:
    user_id = (message.get("from") or {}).get("id")
    return int(user_id) if user_id is not None else None


def display_user(user: dict[str, Any] | None) -> str:
    if not user:
        return ""
    username = user.get("username")
    if username:
        return f"@{username}"
    parts = [user.get("first_name"), user.get("last_name")]
    return " ".join(part for part in parts if part) or str(user.get("id", ""))


def help_text() -> str:
    return "\n".join(
        [
<<<<<<< HEAD
            "Send a TikTok or YouTube URL and I will download it.",
            "",
            rp_help_text(),
            "",
            "Guest mode: mention the bot with a TikTok or YouTube URL. Guest mode only downloads media; RP commands are ignored there.",
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
            "Commands are saved globally for your Telegram user and can be used anywhere the bot can read your messages.",
            "Template placeholders: {actor}, {target}, and random choices like {warmly|softly| }.",
            "Trigger with the command word, for example: hug @user, or reply with hug.",
=======
            "Send any media URL and I will download it with yt-dlp.",
            "For YouTube links, choose a quality from the buttons.",
            "If a download fails, use Retry, Quality, or Cancel from the inline buttons.",
            "",
            "Guest mode: mention the bot with a media URL. Single files are returned from cache; albums need a normal chat with the bot.",
>>>>>>> 2ec99ef (f)
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
