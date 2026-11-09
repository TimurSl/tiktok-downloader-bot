"""Turn download/upload exceptions into short, localized user-facing messages."""

from __future__ import annotations

from .i18n import tr
from .telegram_api import TelegramAPIError

# Failures caused by the link or the source site, not by a bug: no need to bother the admin.
EXPECTED_ERROR_KEYS = {
    "too_large", "timeout", "unavailable", "bot_check", "age", "geo", "rate_limit",
    "unsupported", "no_media", "no_sound",
}


class TooLargeError(RuntimeError):
    """The media would not fit into a Telegram upload."""


class DownloadCancelled(RuntimeError):
    """The user pressed Cancel while the download was running."""


class NoSoundError(RuntimeError):
    """The post has no separate sound to download."""


def error_key(exc: BaseException) -> str:
    if isinstance(exc, TooLargeError):
        return "too_large"
    if isinstance(exc, NoSoundError):
        return "no_sound"

    lower = str(exc).lower()
    if "max upload is" in lower or ("file is " in lower and " mb" in lower):
        return "too_large"
    if isinstance(exc, TelegramAPIError):
        if "too large" in lower:
            return "too_large"
        return "telegram"
    if any(needle in lower for needle in ("sign in to confirm", "not a bot", "confirm you’re not")):
        return "bot_check"
    if any(needle in lower for needle in ("age-restricted", "confirm your age", "inappropriate for some users")):
        return "age"
    if any(needle in lower for needle in ("in your country", "geo restrict", "geo-restrict", "region")):
        return "geo"
    if any(needle in lower for needle in ("http error 429", "too many requests", "rate limit", "rate-limit")):
        return "rate_limit"
    if "unsupported url" in lower:
        return "unsupported"
    if any(
        needle in lower
        for needle in (
            "private video", "this video is private", "unavailable", "not available", "login required",
            "requires login", "has been removed", "does not exist", "http error 404",
        )
    ):
        return "unavailable"
    if any(
        needle in lower
        for needle in ("without a usable output file", "without a usable media output file", "did not expose")
    ):
        return "no_media"
    if isinstance(exc, TimeoutError) or "timed out" in lower or "timeout" in lower:
        return "timeout"
    return "generic"


def friendly_error_message(exc: BaseException, lang: str = "en") -> str:
    return tr(lang, f"err_{error_key(exc)}")
