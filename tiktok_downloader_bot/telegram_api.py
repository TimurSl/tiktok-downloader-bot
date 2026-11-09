"""Thin Telegram Bot API client."""

from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Sequence

import requests

from .i18n import DEFAULT_LANG, normalize_lang
from .media import TELEGRAM_MEDIA_GROUP_LIMIT, detect_media_kind

LOG = logging.getLogger("tiktok_downloader_bot")
MAX_FLOOD_WAIT_SECONDS = 30

# kind -> (API method, upload field, chat action)
SEND_METHODS = {
    "photo": ("sendPhoto", "photo", "upload_photo"),
    "audio": ("sendAudio", "audio", "upload_document"),
    "video": ("sendVideo", "video", "upload_video"),
    "animation": ("sendAnimation", "animation", "upload_document"),
    "document": ("sendDocument", "document", "upload_document"),
}


class TelegramAPIError(RuntimeError):
    def __init__(self, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def empty_to_none(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value.strip()


def message_user_id(message: dict[str, Any]) -> int | None:
    user_id = (message.get("from") or {}).get("id")
    return int(user_id) if user_id is not None else None


@dataclass(frozen=True)
class ChatContext:
    chat_id: int | str
    reply_to_message_id: int | None = None  # only set when the chat enabled replies
    source_message_id: int | None = None  # the user's message that contained the link
    requester_id: int | None = None
    business_connection_id: str | None = None
    direct_messages_topic_id: int | None = None
    lang: str = DEFAULT_LANG
    chat_type: str = "private"
    silent: bool = True

    @classmethod
    def from_message(cls, message: dict[str, Any], *, reply: bool = False) -> "ChatContext":
        topic = message.get("direct_messages_topic")
        direct_topic_id = message.get("direct_messages_topic_id")
        if direct_topic_id is None and isinstance(topic, dict):
            direct_topic_id = topic.get("topic_id")
        message_id = message.get("message_id")
        chat = message["chat"]
        return cls(
            chat_id=chat["id"],
            reply_to_message_id=message_id if reply else None,
            source_message_id=message_id,
            requester_id=message_user_id(message),
            business_connection_id=empty_to_none(message.get("business_connection_id")),
            direct_messages_topic_id=int(direct_topic_id) if direct_topic_id is not None else None,
            lang=normalize_lang((message.get("from") or {}).get("language_code")),
            chat_type=str(chat.get("type") or "private"),
        )

    def with_reply(self, message_id: int | None) -> "ChatContext":
        return replace(self, reply_to_message_id=message_id)

    def without_reply(self) -> "ChatContext":
        return replace(self, reply_to_message_id=None)

    def base_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": self.chat_id}
        if self.business_connection_id:
            payload["business_connection_id"] = self.business_connection_id
        if self.direct_messages_topic_id is not None:
            payload["direct_messages_topic_id"] = self.direct_messages_topic_id
        return payload

    def send_payload(self) -> dict[str, Any]:
        """Payload for messages the bot sends: silent, and replying only if enabled."""
        payload = self.base_payload()
        if self.silent:
            payload["disable_notification"] = True
        if self.reply_to_message_id is not None:
            payload["reply_parameters"] = {
                "message_id": self.reply_to_message_id,
                "allow_sending_without_reply": True,
            }
        return payload


@dataclass(frozen=True)
class MediaMeta:
    """Optional metadata that makes Telegram render uploads properly."""

    width: int | None = None
    height: int | None = None
    duration: int | None = None
    title: str | None = None
    performer: str | None = None
    thumbnail: Path | None = None


def stringify_payload(payload: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in payload.items():
        if isinstance(value, bool):
            result[key] = "true" if value else "false"
        elif isinstance(value, (dict, list)):
            result[key] = json.dumps(value)
        else:
            result[key] = str(value)
    return result


class TelegramAPI:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.base_url = f"{config.api_base}/bot{config.token}"
        self._local = threading.local()

    @property
    def session(self) -> requests.Session:
        # requests.Session is not guaranteed to be thread-safe: one per worker thread.
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._local.session = requests.Session()
        return session

    def call(
        self,
        method: str,
        *,
        payload: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        timeout: int = 60,
        retries: int = 0,
    ) -> Any:
        url = f"{self.base_url}/{method}"
        attempt = 0
        while True:
            if files:
                # Re-seek upload handles so a retry resends the whole file.
                for _name, value in files.items():
                    handle = value[1] if isinstance(value, tuple) else value
                    if hasattr(handle, "seek"):
                        handle.seek(0)
                response = self.session.post(url, data=data or {}, files=files, timeout=timeout)
            else:
                response = self.session.post(url, json=payload or data or {}, timeout=timeout)

            try:
                body = response.json()
            except ValueError as exc:
                raise TelegramAPIError(f"{method} returned non-JSON HTTP {response.status_code}") from exc

            if body.get("ok"):
                return body.get("result")

            description = body.get("description", "unknown Telegram API error")
            retry_after = (body.get("parameters") or {}).get("retry_after")
            error = TelegramAPIError(
                f"{method} failed: {description}",
                retry_after=int(retry_after) if retry_after is not None else None,
            )
            if error.retry_after is not None and attempt < retries:
                attempt += 1
                time.sleep(min(error.retry_after, MAX_FLOOD_WAIT_SECONDS) + 1)
                continue
            raise error

    def get_me(self) -> dict[str, Any]:
        return self.call("getMe")

    def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": self.config.poll_timeout,
            "allowed_updates": ["message", "callback_query", "inline_query", "chosen_inline_result"],
        }
        if offset is not None:
            payload["offset"] = offset
        return self.call("getUpdates", payload=payload, timeout=self.config.poll_timeout + 10)

    def set_my_commands(self, commands: list[dict[str, str]], language_code: str | None = None) -> bool:
        payload: dict[str, Any] = {"commands": commands}
        if language_code:
            payload["language_code"] = language_code
        return self.call("setMyCommands", payload=payload, timeout=30)

    def get_chat_member(self, chat_id: int | str, user_id: int) -> dict[str, Any]:
        return self.call("getChatMember", payload={"chat_id": chat_id, "user_id": user_id}, timeout=20)

    def send_message(
        self,
        context: ChatContext,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {**context.send_payload(), "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return self.call("sendMessage", payload=payload, retries=2)

    def edit_message_text(
        self,
        context: ChatContext,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any] | bool:
        payload: dict[str, Any] = {**context.base_payload(), "message_id": message_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self.call("editMessageText", payload=payload)

    def edit_message_reply_markup(
        self,
        context: ChatContext,
        message_id: int,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any] | bool:
        payload: dict[str, Any] = {**context.base_payload(), "message_id": message_id}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self.call("editMessageReplyMarkup", payload=payload)

    def delete_message(self, context: ChatContext, message_id: int) -> bool:
        try:
            return self.call("deleteMessage", payload={**context.base_payload(), "message_id": message_id})
        except TelegramAPIError as exc:
            LOG.debug("deleteMessage failed: %s", exc)
            return False

    def answer_callback_query(self, callback_query_id: str, text: str = "", *, alert: bool = False) -> bool:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:200]
        if alert:
            payload["show_alert"] = True
        return self.call("answerCallbackQuery", payload=payload, timeout=20)

    def send_chat_action(self, context: ChatContext, action: str) -> None:
        try:
            self.call("sendChatAction", payload={**context.base_payload(), "action": action}, timeout=20)
        except TelegramAPIError as exc:
            LOG.debug("sendChatAction failed: %s", exc)

    def send_media(
        self,
        context: ChatContext,
        source: Path | str,
        *,
        kind: str | None = None,
        caption: str = "",
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        meta: MediaMeta | None = None,
    ) -> dict[str, Any]:
        """Send a local file (``Path``) or an already uploaded one (``file_id`` string)."""
        is_upload = isinstance(source, Path)
        if kind is None:
            kind = detect_media_kind(source) if isinstance(source, Path) else "document"
        method, field, action = SEND_METHODS[kind]
        if is_upload:
            self.send_chat_action(context, action)

        payload: dict[str, Any] = context.send_payload()
        if caption:
            payload["caption"] = caption[:1024]
            if parse_mode:
                payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        meta = meta or MediaMeta()
        if kind in {"video", "animation"}:
            if kind == "video":
                payload["supports_streaming"] = True
            for name in ("width", "height", "duration"):
                value = getattr(meta, name)
                if value:
                    payload[name] = int(value)
        elif kind == "audio":
            if meta.duration:
                payload["duration"] = int(meta.duration)
            if meta.title:
                payload["title"] = meta.title[:64]
            if meta.performer:
                payload["performer"] = meta.performer[:64]

        with ExitStack() as stack:
            files: dict[str, Any] = {}
            if isinstance(source, Path):
                files[field] = (source.name, stack.enter_context(source.open("rb")))
            else:
                payload[field] = source
            if is_upload and kind in {"video", "audio"} and meta.thumbnail and meta.thumbnail.exists():
                files["thumb_file"] = (meta.thumbnail.name, stack.enter_context(meta.thumbnail.open("rb")))
                payload["thumbnail"] = "attach://thumb_file"

            if files:
                return self.call(method, data=stringify_payload(payload), files=files, timeout=600, retries=1)
            return self.call(method, payload=payload, retries=2)

    def send_media_group(
        self,
        context: ChatContext,
        items: Sequence[tuple[str, Path | str]],
        *,
        caption: str = "",
        parse_mode: str | None = None,
    ) -> list[dict[str, Any]]:
        """Send 2-10 photos/videos as one album. Items are ``(kind, Path | file_id)``."""
        if not 2 <= len(items) <= TELEGRAM_MEDIA_GROUP_LIMIT:
            raise ValueError("Telegram media groups must contain 2-10 items")

        has_upload = any(isinstance(source, Path) for _kind, source in items)
        if has_upload:
            self.send_chat_action(context, "upload_video" if any(k == "video" for k, _ in items) else "upload_photo")

        media: list[dict[str, Any]] = []
        for index, (kind, source) in enumerate(items):
            entry: dict[str, Any] = {
                "type": kind,
                "media": f"attach://item{index}" if isinstance(source, Path) else source,
            }
            if kind == "video":
                entry["supports_streaming"] = True
            if index == 0 and caption:
                entry["caption"] = caption[:1024]
                if parse_mode:
                    entry["parse_mode"] = parse_mode
            media.append(entry)

        payload: dict[str, Any] = {**context.send_payload(), "media": media}
        with ExitStack() as stack:
            files = {
                f"item{index}": (source.name, stack.enter_context(source.open("rb")))
                for index, (_kind, source) in enumerate(items)
                if isinstance(source, Path)
            }
            if files:
                return self.call(
                    "sendMediaGroup", data=stringify_payload(payload), files=files, timeout=600, retries=1
                )
            return self.call("sendMediaGroup", payload=payload, retries=2)

    def answer_inline_query(
        self,
        inline_query_id: str,
        results: list[dict[str, Any]],
        *,
        cache_time: int = 5,
    ) -> bool:
        return self.call(
            "answerInlineQuery",
            payload={
                "inline_query_id": inline_query_id,
                "results": results,
                "cache_time": cache_time,
                "is_personal": True,
            },
            timeout=20,
        )

    def edit_inline_message_media(self, inline_message_id: str, media: dict[str, Any]) -> bool:
        return self.call(
            "editMessageMedia",
            payload={"inline_message_id": inline_message_id, "media": media},
            retries=1,
        )

    def edit_inline_message_text(self, inline_message_id: str, text: str) -> bool:
        return self.call(
            "editMessageText",
            payload={"inline_message_id": inline_message_id, "text": text},
        )
