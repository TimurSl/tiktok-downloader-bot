"""X/Twitter photo tweets.

yt-dlp only handles tweets that contain a video or GIF; for photo tweets it fails with
"No video could be found in this tweet". This module reads the photos from the tweet
data instead: first from Twitter's own syndication endpoint (the one used by embedded
tweets), then, if that has nothing, from the public FxTwitter API.
"""

from __future__ import annotations

import html
import math
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

TWITTER_HOSTS = {"twitter.com", "x.com", "mobile.twitter.com"}
# Link-preview mirrors people paste often; yt-dlp does not know them, X does.
MIRROR_HOSTS = {"fxtwitter.com", "vxtwitter.com", "fixupx.com", "fixvx.com", "twittpr.com"}
STATUS_RE = re.compile(r"/(?:(?P<user>[^/]+)|i/web)/status(?:es)?/(?P<id>\d+)", re.IGNORECASE)
TRAILING_TCO_RE = re.compile(r"(?:\s*https?://t\.co/\w+)+\s*$")
BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"
# Telegram refuses photos over 10 MB; stay below that.
TELEGRAM_PHOTO_MAX_BYTES = 9_500_000
SYNDICATION_URL = "https://cdn.syndication.twimg.com/tweet-result"
FXTWITTER_API = "https://api.fxtwitter.com"
API_HEADERS = {"User-Agent": "Googlebot", "Accept": "application/json"}
IMAGE_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"}


@dataclass(frozen=True)
class TweetPost:
    photo_urls: tuple[str, ...]
    text: str = ""
    author: str = ""


def twitter_host(url: str) -> str:
    host = (urllib.parse.urlparse(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def is_twitter_url(url: str) -> bool:
    host = twitter_host(url)
    return host in TWITTER_HOSTS or host in MIRROR_HOSTS


def twitter_status_id(url: str) -> str | None:
    match = STATUS_RE.search(urllib.parse.urlparse(url).path)
    return match.group("id") if match else None


def twitter_screen_name(url: str) -> str | None:
    match = STATUS_RE.search(urllib.parse.urlparse(url).path)
    user = match.group("user") if match else None
    return user if user and user.lower() not in {"i", "web"} else None


def normalize_twitter_url(url: str) -> str:
    """Point link-preview mirrors (fxtwitter.com, vxtwitter.com...) back at x.com."""
    parsed = urllib.parse.urlparse(url)
    if twitter_host(url) in MIRROR_HOSTS:
        return urllib.parse.urlunparse(parsed._replace(netloc="x.com", query="", fragment=""))
    return url


def is_no_video_error(error: BaseException | str) -> bool:
    return "no video could be found in this tweet" in str(error).lower()


def syndication_token(twid: str) -> str:
    """Port of ``((Number(id) / 1e15) * Math.PI).toString(36).replace(/(0+|\\.)/g, '')``."""
    value = (int(twid) / 1e15) * math.pi
    whole = int(value)
    fraction = value - whole
    digits = ""
    while whole:
        whole, rest = divmod(whole, 36)
        digits = BASE36[rest] + digits
    digits = digits or "0"
    fraction_digits = ""
    for _ in range(12):
        if not fraction:
            break
        fraction *= 36
        index = int(fraction)
        fraction_digits += BASE36[index]
        fraction -= index
    return (digits + fraction_digits).replace("0", "")


def clean_tweet_text(text: str) -> str:
    """Unescape HTML entities and drop the trailing t.co link Twitter adds for media."""
    text = html.unescape(text or "")
    return TRAILING_TCO_RE.sub("", text).strip()


def original_photo_url(url: str, *, size: str = "orig") -> str:
    """The full-size version of a pbs.twimg.com photo."""
    parsed = urllib.parse.urlparse(url if not url.startswith("//") else f"https:{url}")
    if parsed.hostname == "pbs.twimg.com" and parsed.path.startswith("/media/"):
        return urllib.parse.urlunparse(parsed._replace(query=f"name={size}", fragment=""))
    return parsed.geturl()


def parse_syndication(data: Any) -> TweetPost | None:
    """Photos of a tweet from the syndication JSON (None for tombstones/empty answers)."""
    if not isinstance(data, dict) or not data or data.get("__typename") == "TweetTombstone":
        return None
    urls: list[str] = []
    details = data.get("mediaDetails")
    for detail in details if isinstance(details, list) else []:
        if isinstance(detail, dict) and detail.get("type") == "photo" and detail.get("media_url_https"):
            urls.append(str(detail["media_url_https"]))
    if not urls:
        photos = data.get("photos")
        for photo in photos if isinstance(photos, list) else []:
            if isinstance(photo, dict) and photo.get("url"):
                urls.append(str(photo["url"]))
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    return TweetPost(
        photo_urls=tuple(dict.fromkeys(urls)),
        text=clean_tweet_text(str(data.get("text") or "")),
        author=str(user.get("name") or user.get("screen_name") or ""),
    )


def parse_fxtwitter(data: Any) -> TweetPost | None:
    """Photos of a tweet from an FxTwitter API answer."""
    tweet = data.get("tweet") if isinstance(data, dict) else None
    if not isinstance(tweet, dict):
        return None
    media = tweet.get("media") if isinstance(tweet.get("media"), dict) else {}
    candidates = media.get("photos")
    if not isinstance(candidates, list) or not candidates:
        everything = media.get("all")
        candidates = [m for m in everything if isinstance(m, dict) and m.get("type") == "photo"] if isinstance(everything, list) else []
    urls = [str(m["url"]) for m in candidates if isinstance(m, dict) and m.get("url")]
    author = tweet.get("author") if isinstance(tweet.get("author"), dict) else {}
    return TweetPost(
        photo_urls=tuple(dict.fromkeys(urls)),
        text=clean_tweet_text(str(tweet.get("text") or "")),
        author=str(author.get("name") or author.get("screen_name") or ""),
    )
