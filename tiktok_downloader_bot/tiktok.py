"""Parsing of TikTok web pages (photo posts and their music)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import unescape
from typing import Any

TIKTOK_WEB_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
        "Mobile/15E148 Safari/604.1"
    ),
}


@dataclass(frozen=True)
class TikTokMusic:
    url: str
    title: str
    author: str


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


def extract_tiktok_music(item: dict[str, Any]) -> TikTokMusic | None:
    """The sound attached to a post (photo posts expose it as ``music.playUrl``)."""
    music = item.get("music") or item.get("music_info")
    if not isinstance(music, dict):
        return None
    url = None
    for key in ("playUrl", "play_url", "playurl"):
        url = first_url_from_json(music.get(key))
        if url:
            break
    if not url:
        return None
    author = music.get("authorName") or music.get("author") or ""
    if not author and isinstance(item.get("author"), dict):
        author = item["author"].get("nickname") or item["author"].get("uniqueId") or ""
    return TikTokMusic(
        url=url,
        title=str(music.get("title") or "TikTok sound"),
        author=str(author),
    )


def extract_tiktok_author(item: dict[str, Any]) -> str:
    author = item.get("author")
    if isinstance(author, dict):
        return str(author.get("nickname") or author.get("uniqueId") or "")
    return str(author or "")


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
