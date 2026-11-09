"""URL detection helpers."""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path

URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
TIKTOK_PHOTO_URL_RE = re.compile(r"https?://(?:www\.)?tiktok\.com/@[^/\s]+/photo/(?P<id>\d+)", re.IGNORECASE)
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
MEDIA_PLATFORM_DOMAINS = {
    "b23.tv",
    "bandcamp.com",
    "bilibili.com",
    "clips.twitch.tv",
    "dai.ly",
    "dailymotion.com",
    "facebook.com",
    "fixupx.com",
    "fixvx.com",
    "fxtwitter.com",
    "fb.watch",
    "instagram.com",
    "kick.com",
    "nicovideo.jp",
    "niconico.jp",
    "ok.ru",
    "on.soundcloud.com",
    "pin.it",
    "pinterest.com",
    "reddit.com",
    "redd.it",
    "rumble.com",
    "soundcloud.com",
    "streamable.com",
    "threads.net",
    "tiktok.com",
    "twitch.tv",
    "twitter.com",
    "twittpr.com",
    "vimeo.com",
    "vxtwitter.com",
    "vk.com",
    "x.com",
    "youtu.be",
    "youtube.com",
}
DIRECT_FILE_EXTENSIONS = {
    ".7z", ".aac", ".apk", ".avi", ".bmp", ".csv", ".doc", ".docx", ".epub", ".exe", ".flac",
    ".gif", ".gz", ".heic", ".iso", ".jpeg", ".jpg", ".json", ".m3u8", ".m4a", ".m4v", ".mkv",
    ".mov", ".mp3", ".mp4", ".mpeg", ".mpg", ".ogg", ".opus", ".pdf", ".png", ".ppt", ".pptx",
    ".rar", ".tar", ".tgz", ".txt", ".wav", ".webm", ".webp", ".xls", ".xlsx", ".xml", ".zip",
}
# Query parameters that only carry tracking information; dropped when building cache keys.
TRACKING_PARAMS = {
    "si", "feature", "igsh", "igshid", "fbclid", "ref", "ref_src", "ref_url", "is_from_webapp",
    "sender_device", "sender_web_id", "share_app_id", "share_item_id", "share_link_id",
    "share_author_id", "_r", "_t", "u_code", "social_sharing", "source", "pp",
}
TRAILING_URL_CHARS = ".,;:!?)\"]'"


def is_youtube_url(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host in YOUTUBE_HOSTS or host.endswith(".youtube.com")


def is_tiktok_url(url: str) -> bool:
    return (urllib.parse.urlparse(url).hostname or "").lower() in TIKTOK_HOSTS


def is_tiktok_photo_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    path_parts = [part for part in parsed.path.split("/") if part]
    return (
        host in {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}
        and len(path_parts) >= 3
        and path_parts[0].startswith("@")
        and path_parts[1] == "photo"
        and path_parts[2].isdigit()
    ) or bool(TIKTOK_PHOTO_URL_RE.search(url))


def is_tiktok_redirect_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    path_parts = [part for part in parsed.path.split("/") if part]
    return host in TIKTOK_SHORT_HOSTS or (
        host in {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}
        and len(path_parts) >= 2
        and path_parts[0] == "t"
    )


def tiktok_photo_id(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) >= 3 and path_parts[1] == "photo" and path_parts[2].isdigit():
        return path_parts[2]
    match = TIKTOK_PHOTO_URL_RE.search(url)
    return match.group("id") if match else None


def is_downloadable_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or is_direct_file_url(parsed):
        return False
    return is_media_platform_host(host)


def is_media_platform_host(host: str) -> bool:
    return any(host == domain or host.endswith(f".{domain}") for domain in MEDIA_PLATFORM_DOMAINS)


def is_direct_file_url(parsed: urllib.parse.ParseResult) -> bool:
    suffix = Path(urllib.parse.unquote(parsed.path)).suffix.lower()
    return suffix in DIRECT_FILE_EXTENSIONS


def first_url(text: str) -> str | None:
    match = URL_RE.search(text)
    if not match:
        return None
    return match.group(0).rstrip(TRAILING_URL_CHARS)


def download_urls(text: str, limit: int | None = None) -> list[str]:
    """All distinct supported media-platform links in ``text``, in order of appearance."""
    result: list[str] = []
    seen: set[str] = set()
    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(TRAILING_URL_CHARS)
        if not is_downloadable_url(url):
            continue
        key = cache_url(url)
        if key in seen:
            continue
        seen.add(key)
        result.append(url)
        if limit is not None and len(result) >= limit:
            break
    return result


def first_download_url(text: str) -> str | None:
    urls = download_urls(text, limit=1)
    return urls[0] if urls else None


def host_label(url: str) -> str:
    host = (urllib.parse.urlparse(url).hostname or url).lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


def cache_url(url: str) -> str:
    """Normalise a link so equivalent shares map to the same cache key."""
    parsed = urllib.parse.urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if host in TIKTOK_HOSTS:
        query = ""
    else:
        pairs = [
            (name, value)
            for name, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            if name.lower() not in TRACKING_PARAMS and not name.lower().startswith("utm_")
        ]
        query = urllib.parse.urlencode(pairs)
    path = parsed.path.rstrip("/") or "/"
    return urllib.parse.urlunparse((parsed.scheme.lower(), host, path, "", query, ""))
