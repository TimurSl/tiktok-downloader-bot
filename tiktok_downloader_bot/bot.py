from __future__ import annotations

import hashlib
import logging
import os
import re
import signal
import threading
import time
import uuid
from collections import Counter, OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any, Callable

import requests

from .captions import build_caption
from .config import Config, effective_upload_limit_bytes
from .downloader import (
    DownloadedMedia,
    MediaDownloader,
    ProgressInfo,
    QualityOption,
    quality_format_selector,
)
from .errors import (
    EXPECTED_ERROR_KEYS,
    DownloadCancelled,
    TooLargeError,
    error_key,
    friendly_error_message,
)
from .i18n import normalize_lang, tr
from .media import album_chunks, detect_media_kind
from .scheduler import JobScheduler
from .storage import FileIdCache, SettingsStore
from .telegram_api import ChatContext, MediaMeta, TelegramAPI, TelegramAPIError, message_user_id
from .urls import cache_url, download_urls, first_download_url, is_tiktok_url, is_youtube_url

LOG = logging.getLogger("tiktok_downloader_bot")

PENDING_DOWNLOAD_TTL_SECONDS = 15 * 60
SOUND_REQUEST_TTL_SECONDS = 24 * 60 * 60
MAX_SOUND_REQUESTS = 2000
ADMIN_NOTICE_INTERVAL_SECONDS = 30
PRUNE_INTERVAL_SECONDS = 60
PROGRESS_EDIT_INTERVAL_SECONDS = 3.0
COMMAND_LANGUAGES = (None, "ru", "uk")
SETTING_KEYS_GROUP_ONLY = {"auto"}


class StopPolling(BaseException):
    """Raised from the signal handler to interrupt a blocking long poll."""


@dataclass(frozen=True)
class Command:
    name: str
    args: str
    addressed: bool  # written as /command@this_bot
    foreign: bool  # addressed to another bot


def parse_command(text: str, bot_username: str) -> Command | None:
    text = text.lstrip()
    if not text.startswith("/"):
        return None
    head, *rest = re.split(r"\s+", text, maxsplit=1)
    name, _, target = head[1:].partition("@")
    foreign = bool(target) and bool(bot_username) and target.lower() != bot_username.lower()
    return Command(
        name=name.lower(),
        args=rest[0].strip() if rest else "",
        addressed=bool(target) and not foreign,
        foreign=foreign,
    )


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
    audio_only: bool = False
    settings: dict[str, Any] = field(default_factory=dict)
    running: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event)

    @property
    def is_audio(self) -> bool:
        return self.audio_only or bool(self.selected_quality and self.selected_quality.audio_only)

    def is_expired(self) -> bool:
        return not self.running and time.time() - self.created_at > PENDING_DOWNLOAD_TTL_SECONDS

    def action_keyboard(self) -> dict[str, Any]:
        lang = self.context.lang
        rows: list[list[dict[str, str]]] = [
            [{"text": tr(lang, "btn_retry"), "callback_data": f"dls:{self.token}:retry"}],
        ]
        if is_youtube_url(self.url):
            rows.append([{"text": tr(lang, "btn_quality"), "callback_data": f"dls:{self.token}:quality"}])
        rows.append([{"text": tr(lang, "btn_cancel"), "callback_data": f"dls:{self.token}:cancel"}])
        return {"inline_keyboard": rows}

    def cancel_keyboard(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": tr(self.context.lang, "btn_cancel"), "callback_data": f"dls:{self.token}:cancel"}]
            ]
        }


@dataclass
class SoundRequest:
    """What the 🎵 button of a delivered post needs to fetch its sound later."""

    url: str
    context: ChatContext
    created_at: float
    is_helper: bool = False  # the button lives on a separate helper message (albums)
    busy: bool = False


class ProgressReporter:
    """Turns yt-dlp progress callbacks into throttled status-message edits."""

    def __init__(
        self,
        edit: Callable[[str], None],
        lang: str,
        interval: float = PROGRESS_EDIT_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.edit = edit
        self.lang = lang
        self.interval = interval
        self.clock = clock
        self.last_text: str | None = None
        self.next_at = 0.0

    def __call__(self, info: ProgressInfo) -> None:
        force = info.stage == "processing"
        if force:
            text = tr(self.lang, "processing")
        else:
            detail = format_progress_detail(info)
            if not detail:
                return
            text = tr(self.lang, "progress", detail=detail)
        if text == self.last_text:
            return
        now = self.clock()
        if not force and now < self.next_at:
            return
        try:
            self.edit(text)
        except TelegramAPIError as exc:
            self.next_at = now + max(self.interval, float(exc.retry_after or 0))
            return
        except Exception:
            LOG.debug("Progress update failed", exc_info=True)
            return
        self.last_text = text
        self.next_at = now + self.interval


def format_progress_detail(info: ProgressInfo) -> str:
    parts: list[str] = []
    if info.percent is not None:
        parts.append(f"{max(0.0, min(100.0, info.percent)):.0f}%")
    if info.speed:
        parts.append(format_speed(info.speed))
    return " · ".join(parts)


def format_speed(bytes_per_second: float) -> str:
    mb = bytes_per_second / 1024 / 1024
    if mb >= 1:
        return f"{mb:.1f} MB/s"
    return f"{bytes_per_second / 1024:.0f} KB/s"


def option_label(option: QualityOption, lang: str) -> str:
    if option.key == "audio":
        return tr(lang, "quality_audio")
    if option.key == "auto":
        label = tr(lang, "quality_auto")
        return f"{label} ({option.max_height}p)" if option.max_height else label
    return option.label


def quality_keyboard(token: str, options: list[QualityOption], lang: str = "en") -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = [
        [{"text": option_label(option, lang), "callback_data": f"dlq:{token}:{option.key}"}]
        for option in options
    ]
    rows.append([{"text": tr(lang, "btn_cancel"), "callback_data": f"dls:{token}:cancel"}])
    return {"inline_keyboard": rows}


def extract_file_id(message: dict[str, Any], kind: str) -> tuple[str, str]:
    """The (kind, file_id) Telegram assigned to something we just sent."""
    if kind == "photo":
        photos = message.get("photo") or []
        if photos:
            return "photo", photos[-1]["file_id"]
    if isinstance(message.get(kind), dict) and message[kind].get("file_id"):
        return kind, message[kind]["file_id"]
    if isinstance(message.get("document"), dict):
        return "document", message["document"]["file_id"]
    raise RuntimeError("Telegram did not return a reusable file_id")


def cache_key(variant: str, url: str) -> str:
    return f"{variant}|{cache_url(url)}"


def cached_inline_result(
    kind: str,
    file_id: str,
    title: str,
    caption: str = "",
    parse_mode: str | None = None,
) -> dict[str, Any]:
    result_id = uuid.uuid4().hex
    extra: dict[str, Any] = {}
    if caption:
        extra["caption"] = caption[:1024]
        if parse_mode:
            extra["parse_mode"] = parse_mode
    title = title[:64] or "Downloaded media"
    if kind == "photo":
        return {"type": "photo", "id": result_id, "photo_file_id": file_id, **extra}
    if kind == "audio":
        return {"type": "audio", "id": result_id, "audio_file_id": file_id, **extra}
    if kind == "video":
        return {"type": "video", "id": result_id, "video_file_id": file_id, "title": title, **extra}
    if kind == "animation":
        return {"type": "gif", "id": result_id, "gif_file_id": file_id, **extra}
    return {"type": "document", "id": result_id, "document_file_id": file_id, "title": title, **extra}


def inline_media(kind: str, file_id: str, caption: str, parse_mode: str | None) -> dict[str, Any]:
    media: dict[str, Any] = {
        "type": kind if kind in {"photo", "video", "audio", "animation", "document"} else "document",
        "media": file_id,
    }
    if caption:
        media["caption"] = caption[:1024]
        if parse_mode:
            media["parse_mode"] = parse_mode
    return media


def article_result(title: str, message: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "article",
        "id": uuid.uuid4().hex,
        "title": title[:64],
        "input_message_content": {"message_text": message[:4096]},
        **extra,
    }


class DownloaderBot:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.api = TelegramAPI(config)
        self.downloader = MediaDownloader(config)
        token_digest = hashlib.sha256(config.token.encode()).hexdigest()[:10]
        self.settings = SettingsStore(config.work_dir / "settings.json")
        # file_ids are only valid for the bot that received them.
        self.file_cache = FileIdCache(config.work_dir / f"file-cache-{token_digest}.json")
        self.scheduler = JobScheduler(config.max_workers, config.user_concurrency)
        self.running = True
        self.polling = False
        self.bot_username = ""
        self.sessions: dict[str, DownloadSession] = {}
        self.sound_requests: OrderedDict[str, SoundRequest] = OrderedDict()
        self.state_lock = threading.Lock()
        self.started_at = time.time()
        self.stats: Counter[str] = Counter()
        self.last_admin_notice = float("-inf")

    # ------------------------------------------------------------------ lifecycle

    def run(self) -> None:
        me = self.api.get_me()
        self.bot_username = str(me.get("username") or "")
        LOG.info(
            "Started as @%s; inline mode enabled in BotFather: %s; cache chat configured: %s",
            self.bot_username,
            "yes" if me.get("supports_inline_queries") else "no",
            "yes" if self.config.cache_chat_id else "no",
        )
        self.api.call("deleteWebhook", payload={"drop_pending_updates": False}, timeout=30)
        self.register_commands()
        self.downloader.cleanup_stale_jobs()

        offset: int | None = None
        last_prune = time.monotonic()
        try:
            while self.running:
                try:
                    self.polling = True
                    updates = self.api.get_updates(offset)
                    self.polling = False
                    for update in updates:
                        offset = int(update["update_id"]) + 1
                        self.safe_handle_update(update)
                except StopPolling:
                    break
                except requests.RequestException as exc:
                    LOG.warning("Network error while polling: %s", exc)
                    time.sleep(3)
                except TelegramAPIError as exc:
                    LOG.warning("Telegram API error while polling: %s", exc)
                    time.sleep(3)
                except Exception:
                    LOG.exception("Unexpected error in the polling loop")
                    time.sleep(3)
                finally:
                    self.polling = False

                if time.monotonic() - last_prune >= PRUNE_INTERVAL_SECONDS:
                    self.prune()
                    last_prune = time.monotonic()
        finally:
            LOG.info("Shutting down; waiting for running downloads to finish")
            self.scheduler.shutdown()

    def stop(self, *_args: Any) -> None:
        if not self.running:
            os._exit(130)  # second signal: stop waiting for running downloads
        self.running = False
        if self.polling:
            raise StopPolling

    def register_commands(self) -> None:
        for language in COMMAND_LANGUAGES:
            lang = normalize_lang(language)
            commands = [
                {"command": "start", "description": tr(lang, "cmd_start")},
                {"command": "help", "description": tr(lang, "cmd_help")},
                {"command": "settings", "description": tr(lang, "cmd_settings")},
                {"command": "audio", "description": tr(lang, "cmd_audio")},
            ]
            try:
                self.api.set_my_commands(commands, language_code=language)
            except Exception as exc:
                LOG.warning("Could not register bot commands (%s): %s", language or "default", exc)

    def prune(self) -> None:
        now = time.time()
        with self.state_lock:
            for token, session in list(self.sessions.items()):
                if not session.running and session.created_at < now - PENDING_DOWNLOAD_TTL_SECONDS:
                    self.sessions.pop(token, None)
            for token, request in list(self.sound_requests.items()):
                if request.created_at < now - SOUND_REQUEST_TTL_SECONDS and not request.busy:
                    self.sound_requests.pop(token, None)

    def prune_sessions(self) -> None:
        self.prune()

    # ------------------------------------------------------------------ updates

    def safe_handle_update(self, update: dict[str, Any]) -> None:
        try:
            self.handle_update(update)
        except Exception as exc:
            LOG.exception("Error while handling update %s", update.get("update_id"))
            self.notify_admins(f"Unhandled error in update handler: {exc!r}")

    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self.handle_callback_query(update["callback_query"])
        elif "message" in update:
            self.handle_message(update["message"])
        elif "inline_query" in update:
            self.handle_inline_query(update["inline_query"])
        elif "chosen_inline_result" in update:
            self.handle_chosen_inline_result(update["chosen_inline_result"])

    def handle_message(self, message: dict[str, Any]) -> None:
        text = message.get("text") or message.get("caption") or ""
        command = parse_command(text, self.bot_username)
        if command and command.foreign:
            return

        settings = self.settings.get(message["chat"]["id"])
        context = ChatContext.from_message(message, reply=settings["reply"])
        lang = context.lang
        group = is_group_chat(message)
        mentioned = bool(self.bot_username) and f"@{self.bot_username}".lower() in text.lower()

        body = strip_bot_mention(command.args if command else text, self.bot_username)
        urls = download_urls(body, self.config.max_links_per_message)
        if command and command.name in {"dl", "audio"} and not urls:
            replied = message.get("reply_to_message") or {}
            urls = download_urls(replied.get("text") or replied.get("caption") or "", self.config.max_links_per_message)

        if not self.is_authorized(message):
            if group:
                # Supported media links in groups are served to everyone, as before.
                if command is None and urls and (settings["auto"] or mentioned):
                    self.queue_downloads(context, urls, settings, audio=False)
                elif command and command.addressed:
                    self.api.send_message(context, tr(lang, "not_authorized"))
            elif command or urls:
                self.api.send_message(context, tr(lang, "not_authorized"))
            return

        if command:
            self.handle_command(command, message, context, settings, urls)
            return

        if urls and (not group or settings["auto"] or mentioned):
            self.queue_downloads(context, urls, settings, audio=False)

    def handle_command(
        self,
        command: Command,
        message: dict[str, Any],
        context: ChatContext,
        settings: dict[str, Any],
        urls: list[str],
    ) -> None:
        lang = context.lang
        name = command.name
        if name in {"start", "help"}:
            self.api.send_message(context, help_text(lang, self.bot_username, bool(self.config.cache_chat_id)))
        elif name == "settings":
            self.send_settings(context, message["chat"], message_user_id(message))
        elif name == "stats" and self.is_admin(context.requester_id):
            self.api.send_message(context, self.stats_text())
        elif name in {"dl", "audio"}:
            if urls:
                self.queue_downloads(context, urls, settings, audio=name == "audio")
            else:
                self.api.send_message(context, tr(lang, "usage_audio" if name == "audio" else "usage_dl"))
        else:
            self.api.send_message(context, tr(lang, "unknown_command"))

    # ------------------------------------------------------------------ job queue

    def queue_job(self, context: ChatContext, job: Callable[[], None]) -> bool:
        key = context.requester_id if context.requester_id is not None else context.chat_id
        if self.scheduler.submit(key, job):
            return True
        try:
            self.api.send_message(context, tr(context.lang, "busy"))
        except TelegramAPIError:
            LOG.debug("Could not send busy notice", exc_info=True)
        return False

    def queue_downloads(self, context: ChatContext, urls: list[str], settings: dict[str, Any], *, audio: bool) -> None:
        for url in urls:
            queued = self.queue_job(
                context,
                lambda url=url: self.prepare_download(context, url, settings, audio=audio),
            )
            if not queued:
                break

    def new_session(self, context: ChatContext, url: str, settings: dict[str, Any]) -> DownloadSession:
        session = DownloadSession(
            token=uuid.uuid4().hex[:12],
            url=url,
            context=context,
            requester_id=context.requester_id,
            created_at=time.time(),
            settings=settings,
        )
        with self.state_lock:
            self.sessions[session.token] = session
        return session

    def prepare_download(self, context: ChatContext, url: str, settings: dict[str, Any], *, audio: bool = False) -> None:
        """Pick the quality (asking the user if needed), then download."""
        preference = "audio" if audio else str(settings.get("quality", "ask"))
        session = self.new_session(context, url, settings)
        try:
            if preference == "audio":
                session.audio_only = True
            elif preference.isdigit():
                height = int(preference)
                session.selected_quality = QualityOption(
                    f"h{height}", f"{height}p", quality_format_selector(height), max_height=height
                )
            elif is_youtube_url(url):
                options = self.downloader.youtube_quality_options(url)
                session.options = {option.key: option for option in options}
                if preference == "ask":
                    self.send_quality_prompt(session, options)
                    return
                session.selected_quality = options[0]
        except Exception as exc:
            LOG.exception("Could not prepare download")
            session.last_error = str(exc)
            self.finish_session_status(session, friendly_error_message(exc, context.lang))
            return
        self.run_download_session(session)

    # ------------------------------------------------------------------ downloading

    def run_download_session(
        self,
        session: DownloadSession,
        *,
        callback_message_id: int | None = None,
    ) -> None:
        context = session.context
        lang = context.lang
        if session.cancel_event.is_set():
            return
        session.running = True
        try:
            self.show_status(session, tr(lang, "downloading"), callback_message_id)

            if not self.deliver_from_cache(session):
                quality = session.selected_quality
                reporter = ProgressReporter(lambda text: self.edit_status(session, text), lang)
                with self.downloader.download(
                    session.url,
                    format_selector=quality.format_selector if quality else None,
                    max_height=quality.max_height if quality else None,
                    audio_only=session.is_audio,
                    progress=reporter,
                    cancel=session.cancel_event.is_set,
                ) as media:
                    self.deliver(session, media)

            self.stats["downloads_ok"] += 1
            self.finish_success(session)
        except DownloadCancelled:
            LOG.info("Download of %s was canceled", session.url)
        except Exception as exc:
            if session.cancel_event.is_set():
                return
            LOG.exception("Download failed")
            self.stats["downloads_failed"] += 1
            session.last_error = str(exc)
            self.finish_session_status(session, friendly_error_message(exc, lang))
            if error_key(exc) not in EXPECTED_ERROR_KEYS:
                self.notify_admins(f"Download failed\nURL: {session.url}\nChat: {context.chat_id}\n{exc!r}"[:3500])
        finally:
            session.running = False

    def show_status(self, session: DownloadSession, text: str, callback_message_id: int | None = None) -> None:
        if callback_message_id:
            session.status_message_id = callback_message_id
            self.safe_edit_message(session.context, callback_message_id, text, reply_markup=session.cancel_keyboard())
        else:
            status = self.api.send_message(session.context, text, reply_markup=session.cancel_keyboard())
            session.status_message_id = int(status.get("message_id") or 0) or None

    def edit_status(self, session: DownloadSession, text: str) -> None:
        """Edit the status message; errors propagate so the reporter can back off."""
        if session.status_message_id:
            self.api.edit_message_text(
                session.context, session.status_message_id, text, reply_markup=session.cancel_keyboard()
            )

    def finish_success(self, session: DownloadSession) -> None:
        context = session.context
        if session.status_message_id:
            self.api.delete_message(context, session.status_message_id)
        with self.state_lock:
            self.sessions.pop(session.token, None)
        if session.settings.get("delete_original") and context.source_message_id:
            self.api.delete_message(context, context.source_message_id)

    def finish_session_status(self, session: DownloadSession, text: str) -> None:
        if session.status_message_id:
            self.safe_edit_message(
                session.context,
                session.status_message_id,
                text,
                reply_markup=session.action_keyboard(),
            )
        else:
            status = self.api.send_message(session.context, text, reply_markup=session.action_keyboard())
            session.status_message_id = int(status.get("message_id") or 0) or None

    def session_cache_key(self, session: DownloadSession) -> str:
        if session.is_audio:
            variant = "audio"
        elif session.selected_quality:
            variant = session.selected_quality.key
        else:
            variant = "default"
        return cache_key(variant, session.url)

    def deliver_from_cache(self, session: DownloadSession) -> bool:
        key = self.session_cache_key(session)
        entry = self.file_cache.get(key)
        if not entry:
            return False
        try:
            items = [(str(kind), str(file_id)) for kind, file_id in entry["items"]]
            caption, parse_mode = self.caption_for(session, entry, items)
            markup = self.sound_markup(session, [kind for kind, _ in items])
            self.send_items(session, items, caption=caption, parse_mode=parse_mode, markup=markup)
            self.send_sound_helper(session, [kind for kind, _ in items])
        except (TelegramAPIError, KeyError, ValueError, TypeError) as exc:
            LOG.info("Cached file_ids for %s were rejected (%s); downloading again", session.url, exc)
            self.file_cache.discard(key)
            return False
        self.stats["cache_hits"] += 1
        return True

    def caption_for(
        self,
        session: DownloadSession,
        entry: dict[str, Any],
        items: list[tuple[str, str]],
    ) -> tuple[str, str | None]:
        title = "" if items and items[0][0] == "audio" else str(entry.get("title") or "")
        return build_caption(
            title,
            str(entry.get("uploader") or ""),
            str(entry.get("url") or session.url),
            enabled=bool(session.settings.get("captions", True)),
        )

    def deliver(self, session: DownloadSession, media: DownloadedMedia) -> None:
        if session.cancel_event.is_set():
            raise DownloadCancelled("canceled")
        for path in media.paths:
            self.ensure_size(path)

        kinds = [detect_media_kind(path) for path in media.paths]
        items: list[tuple[str, Any]] = list(zip(kinds, media.paths))
        entry = {
            "title": media.title,
            "uploader": media.uploader,
            "url": media.webpage_url or session.url,
        }
        caption, parse_mode = self.caption_for(session, entry, [(kinds[0], "")])

        meta: MediaMeta | None = None
        if len(items) == 1 and kinds[0] == "video":
            meta = MediaMeta(
                width=media.width, height=media.height, duration=media.duration, thumbnail=media.thumbnail
            )
        elif len(items) == 1 and kinds[0] == "audio":
            meta = MediaMeta(
                duration=media.duration, title=media.title, performer=media.performer or media.uploader
            )

        markup = self.sound_markup(session, kinds)
        uploaded = self.send_items(session, items, caption=caption, parse_mode=parse_mode, meta=meta, markup=markup)
        self.send_sound_helper(session, kinds)
        self.file_cache.put(
            self.session_cache_key(session),
            {"items": [[kind, file_id] for kind, file_id in uploaded], **entry},
        )

    def send_items(
        self,
        session: DownloadSession,
        items: list[tuple[str, Any]],
        *,
        caption: str,
        parse_mode: str | None,
        meta: MediaMeta | None = None,
        markup: dict[str, Any] | None = None,
    ) -> list[tuple[str, str]]:
        """Send one file, or an album; returns the (kind, file_id) of everything sent."""
        context = session.context
        if len(items) == 1:
            kind, source = items[0]
            message = self.api.send_media(
                context, source, kind=kind, caption=caption, parse_mode=parse_mode, reply_markup=markup, meta=meta
            )
            return [extract_file_id(message, kind)]

        uploaded: list[tuple[str, str]] = []
        for index, chunk in enumerate(album_chunks(items)):
            uploaded.extend(
                self.send_album_chunk(context, chunk, caption if index == 0 else "", parse_mode)
            )
        return uploaded

    def send_album_chunk(
        self,
        context: ChatContext,
        chunk: tuple[tuple[str, Any], ...],
        caption: str,
        parse_mode: str | None,
    ) -> list[tuple[str, str]]:
        for attempt in range(2):
            try:
                messages = self.api.send_media_group(context, list(chunk), caption=caption, parse_mode=parse_mode)
                return [extract_file_id(message, kind) for message, (kind, _source) in zip(messages, chunk)]
            except TelegramAPIError:
                LOG.debug("Album upload attempt %d failed", attempt + 1, exc_info=True)

        uploaded: list[tuple[str, str]] = []
        for index, (kind, source) in enumerate(chunk):
            message = self.api.send_media(
                context, source, kind=kind, caption=caption if index == 0 else "", parse_mode=parse_mode
            )
            uploaded.append(extract_file_id(message, kind))
        self.api.send_message(context.without_reply(), tr(context.lang, "album_fallback"))
        return uploaded

    def ensure_size(self, file_path: Any) -> None:
        size = file_path.stat().st_size
        max_upload_bytes = effective_upload_limit_bytes(self.config)
        if size > max_upload_bytes:
            mb = size / 1024 / 1024
            max_mb = max_upload_bytes / 1024 / 1024
            raise TooLargeError(f"file is {mb:.1f} MB, max upload is {max_mb:.0f} MB")

    # ------------------------------------------------------------------ sound button

    def wants_sound(self, session: DownloadSession, kinds: list[str]) -> bool:
        """The 🎵 button is offered for single videos and TikTok photo posts, never sent unasked."""
        if not session.settings.get("sound", True) or session.is_audio or not kinds:
            return False
        if "audio" in kinds:
            return False
        if len(kinds) == 1 and kinds[0] == "video":
            return True
        return is_tiktok_url(session.url) and all(kind == "photo" for kind in kinds)

    def register_sound(self, session: DownloadSession, *, is_helper: bool) -> dict[str, Any]:
        token = uuid.uuid4().hex[:12]
        with self.state_lock:
            self.sound_requests[token] = SoundRequest(
                url=session.url, context=session.context, created_at=time.time(), is_helper=is_helper
            )
            while len(self.sound_requests) > MAX_SOUND_REQUESTS:
                self.sound_requests.popitem(last=False)
        return {
            "inline_keyboard": [
                [{"text": tr(session.context.lang, "btn_sound"), "callback_data": f"snd:{token}"}]
            ]
        }

    def sound_markup(self, session: DownloadSession, kinds: list[str]) -> dict[str, Any] | None:
        """Button attached directly to the media message (single files only)."""
        if len(kinds) == 1 and self.wants_sound(session, kinds):
            return self.register_sound(session, is_helper=False)
        return None

    def send_sound_helper(self, session: DownloadSession, kinds: list[str]) -> None:
        """Albums cannot carry a button, so offer the sound in a small silent follow-up."""
        if len(kinds) > 1 and self.wants_sound(session, kinds):
            markup = self.register_sound(session, is_helper=True)
            self.api.send_message(session.context, tr(session.context.lang, "sound_prompt"), reply_markup=markup)

    def handle_sound_callback(self, callback_query: dict[str, Any], callback_id: str, token: str, lang: str) -> None:
        request = self.sound_requests.get(token)
        if not request or time.time() - request.created_at > SOUND_REQUEST_TTL_SECONDS:
            self.sound_requests.pop(token, None)
            self.answer_callback(callback_id, tr(lang, "cb_expired"))
            return

        message = callback_query.get("message") or {}
        chat_type = (message.get("chat") or {}).get("type")
        user_id = message_user_id(callback_query)
        if chat_type == "private" and self.config.authorized_users and user_id not in self.config.authorized_users:
            self.answer_callback(callback_id, tr(lang, "not_authorized"))
            return
        if request.busy:
            self.answer_callback(callback_id, tr(lang, "sound_busy"))
            return

        request.busy = True
        self.answer_callback(callback_id, tr(lang, "sound_sending"))
        message_id = message.get("message_id")
        context = replace(request.context, lang=lang, requester_id=user_id)
        queued = self.queue_job(
            context,
            lambda: self.run_sound_job(token, request, context, int(message_id) if message_id else None),
        )
        if not queued:
            request.busy = False

    def run_sound_job(
        self,
        token: str,
        request: SoundRequest,
        context: ChatContext,
        message_id: int | None,
    ) -> None:
        settings = self.settings.get(context.chat_id)
        send_context = context.with_reply(message_id if settings["reply"] else None)
        key = cache_key("audio", request.url)
        try:
            self.send_sound(send_context, request.url, key, captions=bool(settings["captions"]))
            self.stats["sounds_sent"] += 1
            if message_id:
                if request.is_helper:
                    self.api.delete_message(context, message_id)
                else:
                    try:
                        self.api.edit_message_reply_markup(context, message_id)
                    except TelegramAPIError:
                        LOG.debug("Could not remove the sound button", exc_info=True)
            with self.state_lock:
                self.sound_requests.pop(token, None)
        except Exception as exc:
            LOG.exception("Sound download failed")
            reason = friendly_error_message(exc, context.lang)
            try:
                self.api.send_message(context, tr(context.lang, "sound_failed", reason=reason))
            except TelegramAPIError:
                LOG.debug("Could not report the sound failure", exc_info=True)
            if error_key(exc) not in EXPECTED_ERROR_KEYS:
                self.notify_admins(f"Sound download failed\nURL: {request.url}\n{exc!r}"[:3500])
        finally:
            request.busy = False

    def send_sound(self, context: ChatContext, url: str, key: str, *, captions: bool) -> None:
        entry = self.file_cache.get(key)
        if entry:
            try:
                _kind, file_id = entry["items"][0]
                caption, parse_mode = build_caption("", str(entry.get("uploader") or ""), str(entry.get("url") or url), enabled=captions)
                self.api.send_media(context, str(file_id), kind="audio", caption=caption, parse_mode=parse_mode)
                return
            except (TelegramAPIError, KeyError, IndexError, ValueError, TypeError):
                LOG.info("Cached sound for %s was rejected; downloading again", url)
                self.file_cache.discard(key)

        with self.downloader.download(url, audio_only=True) as media:
            self.ensure_size(media.path)
            caption, parse_mode = build_caption("", media.uploader, media.webpage_url or url, enabled=captions)
            message = self.api.send_media(
                context,
                media.path,
                kind="audio",
                caption=caption,
                parse_mode=parse_mode,
                meta=MediaMeta(
                    duration=media.duration, title=media.title, performer=media.performer or media.uploader
                ),
            )
            kind, file_id = extract_file_id(message, "audio")
            self.file_cache.put(
                key,
                {
                    "items": [[kind, file_id]],
                    "title": media.title,
                    "uploader": media.uploader,
                    "url": media.webpage_url or url,
                },
            )

    # ------------------------------------------------------------------ callbacks

    def answer_callback(self, callback_id: str, text: str = "", *, alert: bool = False) -> None:
        if not callback_id:
            return
        try:
            self.api.answer_callback_query(callback_id, text, alert=alert)
        except TelegramAPIError:
            LOG.debug("answerCallbackQuery failed", exc_info=True)

    def handle_callback_query(self, callback_query: dict[str, Any]) -> None:
        callback_id = str(callback_query.get("id") or "")
        data = str(callback_query.get("data") or "")
        lang = normalize_lang((callback_query.get("from") or {}).get("language_code"))

        if data.startswith("snd:"):
            self.handle_sound_callback(callback_query, callback_id, data[4:], lang)
            return
        if data.startswith("cfg:"):
            self.handle_settings_callback(callback_query, callback_id, data[4:], lang)
            return
        if not (data.startswith("dlq:") or data.startswith("dls:")):
            self.answer_callback(callback_id)
            return

        prefix, token, action = data.split(":", maxsplit=2) if data.count(":") >= 2 else ("", "", "")
        session = self.callback_session(token, callback_query, callback_id, lang)
        if not session:
            return

        message = callback_query.get("message") or {}
        callback_message_id = int(message["message_id"]) if message.get("message_id") else None

        if prefix == "dlq":
            option = session.options.get(action)
            if not option:
                self.answer_callback(callback_id, tr(lang, "cb_unknown_quality"))
                return
            if session.running:
                self.answer_callback(callback_id, tr(lang, "downloading"))
                return
            session.selected_quality = option
            self.answer_callback(callback_id, tr(lang, "cb_downloading_quality", label=option_label(option, lang)))
            self.queue_session_run(session, callback_message_id)
            return

        if action == "retry":
            if session.running:
                self.answer_callback(callback_id, tr(lang, "downloading"))
                return
            session.attempt_count += 1
            self.answer_callback(callback_id, tr(lang, "cb_retrying"))
            self.queue_session_run(session, callback_message_id)
            return

        if action == "quality" and is_youtube_url(session.url):
            self.answer_callback(callback_id, tr(lang, "cb_choose_quality"))
            self.queue_job(
                session.context,
                lambda: self.show_quality_options(session, callback_message_id),
            )
            return

        if action == "cancel":
            session.cancel_event.set()
            with self.state_lock:
                self.sessions.pop(session.token, None)
            self.answer_callback(callback_id, tr(lang, "cb_canceled"))
            if callback_message_id:
                self.safe_edit_message(session.context, callback_message_id, tr(lang, "canceled"))
            return

        self.answer_callback(callback_id, tr(lang, "cb_unknown_action"))

    def queue_session_run(self, session: DownloadSession, callback_message_id: int | None) -> None:
        self.queue_job(
            session.context,
            lambda: self.run_download_session(session, callback_message_id=callback_message_id),
        )

    def show_quality_options(self, session: DownloadSession, message_id: int | None) -> None:
        """Turn the status message back into the quality picker."""
        lang = session.context.lang
        try:
            options = self.downloader.youtube_quality_options(session.url)
        except Exception as exc:
            LOG.exception("Could not prepare YouTube quality choices")
            if message_id:
                self.safe_edit_message(
                    session.context, message_id, friendly_error_message(exc, lang), reply_markup=session.action_keyboard()
                )
            return
        session.options = {option.key: option for option in options}
        markup = quality_keyboard(session.token, options, lang)
        if message_id:
            session.status_message_id = message_id
            self.safe_edit_message(session.context, message_id, tr(lang, "choose_quality"), reply_markup=markup)
        else:
            self.send_quality_prompt(session, options)

    def send_quality_prompt(self, session: DownloadSession, options: list[QualityOption]) -> None:
        lang = session.context.lang
        status = self.api.send_message(
            session.context,
            tr(lang, "choose_quality"),
            reply_markup=quality_keyboard(session.token, options, lang),
        )
        session.status_message_id = int(status.get("message_id") or 0) or None

    def callback_session(
        self,
        token: str,
        callback_query: dict[str, Any],
        callback_id: str,
        lang: str = "en",
    ) -> DownloadSession | None:
        session = self.sessions.get(token)
        callback_user_id = message_user_id(callback_query)
        if not session or session.is_expired():
            self.answer_callback(callback_id, tr(lang, "cb_expired"))
            with self.state_lock:
                self.sessions.pop(token, None)
            return None
        if session.requester_id is not None and callback_user_id != session.requester_id:
            self.answer_callback(callback_id, tr(lang, "cb_only_requester"))
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

    # ------------------------------------------------------------------ settings

    def can_manage_settings(self, chat: dict[str, Any], user_id: int | None) -> bool:
        if user_id is None:
            return False
        if self.config.authorized_users:
            return user_id in self.config.authorized_users
        if chat.get("type") in {"group", "supergroup"}:
            try:
                member = self.api.get_chat_member(chat["id"], user_id)
            except TelegramAPIError:
                return False
            return member.get("status") in {"creator", "administrator"}
        return True

    def send_settings(self, context: ChatContext, chat: dict[str, Any], user_id: int | None) -> None:
        lang = context.lang
        if not self.can_manage_settings(chat, user_id):
            self.api.send_message(context, tr(lang, "settings_admin_only"))
            return
        values = self.settings.get(context.chat_id)
        group = chat.get("type") in {"group", "supergroup"}
        self.api.send_message(
            context,
            settings_text(lang),
            reply_markup=settings_keyboard(lang, values, group=group),
        )

    def handle_settings_callback(self, callback_query: dict[str, Any], callback_id: str, key: str, lang: str) -> None:
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        if not self.can_manage_settings(chat, message_user_id(callback_query)):
            self.answer_callback(callback_id, tr(lang, "settings_admin_only"), alert=True)
            return
        try:
            values = self.settings.toggle(chat["id"], key)
        except KeyError:
            self.answer_callback(callback_id, tr(lang, "cb_unknown_action"))
            return
        group = chat.get("type") in {"group", "supergroup"}
        if message.get("message_id"):
            self.safe_edit_message(
                ChatContext(chat_id=chat["id"], lang=lang),
                int(message["message_id"]),
                settings_text(lang),
                reply_markup=settings_keyboard(lang, values, group=group),
            )
        self.answer_callback(callback_id, tr(lang, "cb_saved"))

    # ------------------------------------------------------------------ inline mode

    @staticmethod
    def inline_variant(url: str) -> str:
        return "auto" if is_youtube_url(url) else "default"

    def handle_inline_query(self, query: dict[str, Any]) -> None:
        query_id = str(query.get("id") or "")
        user_id = message_user_id(query)
        lang = normalize_lang((query.get("from") or {}).get("language_code"))
        text = query.get("query") or ""

        results: list[dict[str, Any]] = []
        if self.config.authorized_users and user_id not in self.config.authorized_users:
            results = [article_result(tr(lang, "inline_unauthorized_title"), tr(lang, "not_authorized"))]
        else:
            url = first_download_url(text)
            if url is None:
                results = []
            elif not self.config.cache_chat_id:
                results = [article_result(tr(lang, "inline_unconfigured_title"), tr(lang, "inline_unconfigured"))]
            else:
                entry = self.file_cache.get(cache_key(self.inline_variant(url), url))
                if entry and len(entry.get("items") or []) == 1:
                    kind, file_id = entry["items"][0]
                    caption, parse_mode = build_caption(
                        "" if kind == "audio" else str(entry.get("title") or ""),
                        str(entry.get("uploader") or ""),
                        str(entry.get("url") or url),
                    )
                    results = [cached_inline_result(kind, file_id, str(entry.get("title") or ""), caption, parse_mode)]
                else:
                    results = [
                        {
                            "type": "article",
                            "id": f"dl:{uuid.uuid4().hex}",
                            "title": tr(lang, "inline_download_title"),
                            "description": url[:100],
                            "input_message_content": {
                                "message_text": f"⏳ {tr(lang, 'inline_downloading')}\n{url}",
                                "link_preview_options": {"is_disabled": True},
                            },
                            # A keyboard is required for Telegram to report inline_message_id.
                            "reply_markup": {"inline_keyboard": [[{"text": "⏳", "callback_data": "noop"}]]},
                        }
                    ]
        try:
            self.api.answer_inline_query(query_id, results)
        except TelegramAPIError as exc:
            LOG.debug("answerInlineQuery failed: %s", exc)

    def handle_chosen_inline_result(self, chosen: dict[str, Any]) -> None:
        inline_message_id = chosen.get("inline_message_id")
        if not str(chosen.get("result_id") or "").startswith("dl:") or not inline_message_id:
            return
        url = first_download_url(chosen.get("query") or "")
        if not url or not self.config.cache_chat_id:
            return
        lang = normalize_lang((chosen.get("from") or {}).get("language_code"))
        user_id = message_user_id(chosen)
        queued = self.scheduler.submit(
            user_id if user_id is not None else "inline",
            lambda: self.run_inline_download(str(inline_message_id), url, lang),
        )
        if not queued:
            self.edit_inline_text(str(inline_message_id), tr(lang, "busy"))

    def edit_inline_text(self, inline_message_id: str, text: str) -> None:
        try:
            self.api.edit_inline_message_text(inline_message_id, text)
        except TelegramAPIError:
            LOG.debug("Could not edit inline message", exc_info=True)

    def run_inline_download(self, inline_message_id: str, url: str, lang: str) -> None:
        """Download, upload once to the cache chat to obtain a file_id, then swap it into the inline message."""
        try:
            quality = None
            if is_youtube_url(url):
                quality = self.downloader.youtube_quality_options(url)[0]
            with self.downloader.download(
                url,
                format_selector=quality.format_selector if quality else None,
                max_height=quality.max_height if quality else None,
            ) as media:
                if media.is_album:
                    self.edit_inline_text(inline_message_id, tr(lang, "inline_album", count=len(media.paths)))
                    return
                self.ensure_size(media.path)
                kind = detect_media_kind(media.path)
                source_url = media.webpage_url or url
                caption, parse_mode = build_caption("" if kind == "audio" else media.title, media.uploader, source_url)
                meta = MediaMeta(
                    width=media.width, height=media.height, duration=media.duration,
                    title=media.title, performer=media.performer or media.uploader,
                    thumbnail=media.thumbnail,
                )
                message = self.api.send_media(
                    ChatContext(chat_id=str(self.config.cache_chat_id), lang=lang),
                    media.path,
                    kind=kind,
                    caption=f"Cache: {media.title}"[:1024],
                    meta=meta,
                )
                kind, file_id = extract_file_id(message, kind)
                self.file_cache.put(
                    cache_key(self.inline_variant(url), url),
                    {"items": [[kind, file_id]], "title": media.title, "uploader": media.uploader, "url": source_url},
                )
                self.api.edit_inline_message_media(inline_message_id, inline_media(kind, file_id, caption, parse_mode))
                self.stats["inline_ok"] += 1
        except Exception as exc:
            LOG.exception("Inline download failed")
            self.edit_inline_text(
                inline_message_id, tr(lang, "inline_failed", reason=friendly_error_message(exc, lang))
            )
            if error_key(exc) not in EXPECTED_ERROR_KEYS:
                self.notify_admins(f"Inline download failed\nURL: {url}\n{exc!r}"[:3500])

    # ------------------------------------------------------------------ admin

    def is_authorized(self, message: dict[str, Any]) -> bool:
        if not self.config.authorized_users:
            return True
        return message_user_id(message) in self.config.authorized_users

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.config.admin_users

    def notify_admins(self, text: str) -> None:
        """Forward unexpected errors to the admins, at most one notice per interval."""
        if not self.config.admin_users:
            return
        now = time.monotonic()
        with self.state_lock:
            if now - self.last_admin_notice < ADMIN_NOTICE_INTERVAL_SECONDS:
                return
            self.last_admin_notice = now
        for admin_id in self.config.admin_users:
            try:
                self.api.send_message(ChatContext(chat_id=admin_id, silent=False), f"⚠️ {text}")
            except Exception:
                LOG.debug("Could not notify admin %s", admin_id, exc_info=True)

    def stats_text(self) -> str:
        running, queued = self.scheduler.counts()
        uptime = int(time.time() - self.started_at)
        days, rest = divmod(uptime, 86400)
        hours, rest = divmod(rest, 3600)
        lines = [
            f"Uptime: {days}d {hours}h {rest // 60}m",
            f"Jobs: {running} running, {queued} queued",
            f"Downloads: {self.stats['downloads_ok']} ok, {self.stats['downloads_failed']} failed",
            f"Cache hits: {self.stats['cache_hits']} (entries: {len(self.file_cache)})",
            f"Sounds sent: {self.stats['sounds_sent']}",
            f"Inline downloads: {self.stats['inline_ok']}",
            f"Open sessions: {len(self.sessions)}, sound buttons: {len(self.sound_requests)}",
            f"Chats with custom settings: {self.settings.chat_count()}",
        ]
        return "\n".join(lines)


def settings_text(lang: str) -> str:
    return f"{tr(lang, 'settings_title')}\n{tr(lang, 'settings_hint')}"


def quality_setting_label(lang: str, value: str) -> str:
    if value == "ask":
        return tr(lang, "quality_ask")
    if value == "auto":
        return tr(lang, "quality_best")
    if value == "audio":
        return tr(lang, "quality_audio_only")
    return f"{value}p"


def settings_keyboard(lang: str, values: dict[str, Any], *, group: bool) -> dict[str, Any]:
    def onoff(key: str) -> str:
        return tr(lang, "on" if values[key] else "off")

    rows = [
        [(f"{tr(lang, 'set_quality')}: {quality_setting_label(lang, values['quality'])}", "cfg:quality")],
        [(f"{tr(lang, 'set_captions')}: {onoff('captions')}", "cfg:captions")],
        [(f"{tr(lang, 'set_sound')}: {onoff('sound')}", "cfg:sound")],
    ]
    if group:
        rows.append([(f"{tr(lang, 'set_auto')}: {onoff('auto')}", "cfg:auto")])
    rows.append([(f"{tr(lang, 'set_delete_original')}: {onoff('delete_original')}", "cfg:delete_original")])
    rows.append([(f"{tr(lang, 'set_reply')}: {onoff('reply')}", "cfg:reply")])
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row] for row in rows
        ]
    }


def is_group_chat(message: dict[str, Any]) -> bool:
    chat_type = (message.get("chat") or {}).get("type")
    return chat_type in {"group", "supergroup"}


def strip_bot_mention(text: str, username: str) -> str:
    if not username:
        return text
    return re.sub(rf"@{re.escape(username)}\b", "", text, flags=re.IGNORECASE).strip()


def help_text(lang: str, bot_username: str = "", inline_enabled: bool = False) -> str:
    text = tr(lang, "help")
    if inline_enabled and bot_username:
        text += "\n" + tr(lang, "help_inline", bot=bot_username)
    return text


def configure_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> None:
    configure_logging()
    config = Config.from_env()
    config.work_dir.mkdir(parents=True, exist_ok=True)
    bot = DownloaderBot(config)
    signal.signal(signal.SIGTERM, bot.stop)
    signal.signal(signal.SIGINT, bot.stop)
    bot.run()
