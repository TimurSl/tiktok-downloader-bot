"""Helpers for classifying and selecting downloaded files."""

from __future__ import annotations

import mimetypes
import re
import urllib.parse
from pathlib import Path
from typing import Sequence, TypeVar

T = TypeVar("T")

TELEGRAM_MEDIA_GROUP_LIMIT = 10
INCOMPLETE_DOWNLOAD_SUFFIXES = (".part", ".ytdl", ".temp", ".tmp")
SIDECAR_DOWNLOAD_EXTENSIONS = {
    ".ass", ".description", ".info.json", ".json", ".srt", ".ssa", ".srv1", ".srv2", ".srv3",
    ".ttml", ".txt", ".vtt", ".xml",
}
USABLE_DOWNLOAD_MEDIA_KINDS = {"animation", "audio", "photo", "video"}
ALBUM_MEDIA_KINDS = {"photo", "video"}
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav"}


def detect_media_kind(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    if suffix in PHOTO_EXTENSIONS:
        return "photo"
    if suffix in AUDIO_EXTENSIONS:
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


def find_downloaded_files(directory: Path) -> list[Path]:
    return [
        path
        for path in directory.rglob("*")
        if path.is_file()
        and path.name != "cookies.txt"
        and not path.name.endswith(INCOMPLETE_DOWNLOAD_SUFFIXES)
        and not is_sidecar_download_file(path)
    ]


def select_downloaded_media(paths: list[Path]) -> tuple[Path, ...]:
    """Pick what to upload: every photo/video of a multi-item post, else the largest file."""
    media_paths = [path for path in paths if is_usable_downloaded_media(path)]
    if not media_paths:
        return ()

    sorted_paths = tuple(sorted(media_paths, key=natural_path_key))
    if len(sorted_paths) > 1 and all(detect_media_kind(path) in ALBUM_MEDIA_KINDS for path in sorted_paths):
        return sorted_paths
    return (max(media_paths, key=lambda path: path.stat().st_size),)


def is_sidecar_download_file(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(extension) for extension in SIDECAR_DOWNLOAD_EXTENSIONS)


def is_usable_downloaded_media(path: Path) -> bool:
    return detect_media_kind(path) in USABLE_DOWNLOAD_MEDIA_KINDS


def natural_path_key(path: Path) -> list[int | str]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]


def chunk_paths(paths: Sequence[T], size: int) -> list[tuple[T, ...]]:
    return [tuple(paths[index : index + size]) for index in range(0, len(paths), size)]


def album_chunks(items: Sequence[T]) -> list[tuple[T, ...]]:
    """Split into Telegram albums of at most 10 items, never leaving a lone trailing item."""
    albums = chunk_paths(items, TELEGRAM_MEDIA_GROUP_LIMIT)
    if len(albums) > 1 and len(albums[-1]) == 1:
        moved = albums[-2][-1:]
        albums[-2] = albums[-2][:-1]
        albums[-1] = moved + albums[-1]
    return albums


photo_album_chunks = album_chunks


def normalize_url(url: str) -> str:
    return f"https:{url}" if url.startswith("//") else url


def extension_from_response(url: str, content_type: str | None, *, allowed: set[str] | None = None, default: str = ".jpg") -> str:
    allowed = allowed or PHOTO_EXTENSIONS
    parsed_extension = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if parsed_extension in allowed:
        return parsed_extension

    if content_type:
        content_type = content_type.split(";", maxsplit=1)[0].strip().lower()
        if content_type == "image/jpeg":
            return ".jpg"
        if content_type in {"audio/mpeg", "audio/mp3"}:
            return ".mp3" if ".mp3" in allowed else default
        extension = mimetypes.guess_extension(content_type)
        if extension in allowed:
            return extension

    return default
