"""Small JSON-backed stores: per-chat settings and the Telegram file_id cache."""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

LOG = logging.getLogger("tiktok_downloader_bot")

QUALITY_CHOICES = ("ask", "auto", "1080", "720", "480", "audio")
SETTING_DEFAULTS: dict[str, Any] = {
    "captions": True,
    "sound": True,
    "gif": True,
    "auto": True,
    "delete_original": False,
    "reply": False,
    "quality": "ask",
}
BOOL_SETTINGS = ("captions", "sound", "gif", "auto", "delete_original", "reply")


def read_json(path: Path | None) -> Any:
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        LOG.warning("Could not read %s: %s", path, exc)
        return None


def write_json_atomic(path: Path | None, data: Any) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        LOG.warning("Could not write %s: %s", path, exc)


class SettingsStore:
    """Per-chat settings. ``path=None`` keeps everything in memory (tests)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.lock = threading.Lock()
        loaded = read_json(path)
        self.data: dict[str, dict[str, Any]] = loaded if isinstance(loaded, dict) else {}

    def get(self, chat_id: int | str) -> dict[str, Any]:
        with self.lock:
            stored = self.data.get(str(chat_id), {})
            result = dict(SETTING_DEFAULTS)
            for key, default in SETTING_DEFAULTS.items():
                value = stored.get(key, default)
                if key == "quality":
                    result[key] = value if value in QUALITY_CHOICES else default
                else:
                    result[key] = value if isinstance(value, bool) else default
            return result

    def toggle(self, chat_id: int | str, key: str) -> dict[str, Any]:
        """Flip a boolean setting or cycle through the quality choices."""
        current = self.get(chat_id)
        if key in BOOL_SETTINGS:
            new_value: Any = not current[key]
        elif key == "quality":
            index = QUALITY_CHOICES.index(current["quality"])
            new_value = QUALITY_CHOICES[(index + 1) % len(QUALITY_CHOICES)]
        else:
            raise KeyError(key)
        with self.lock:
            self.data.setdefault(str(chat_id), {})[key] = new_value
            write_json_atomic(self.path, self.data)
        return self.get(chat_id)

    def chat_count(self) -> int:
        with self.lock:
            return len(self.data)


class FileIdCache:
    """LRU cache: (variant, url) -> Telegram file_ids, so repeated links are instant."""

    def __init__(self, path: Path | None = None, max_entries: int = 2000) -> None:
        self.path = path
        self.max_entries = max_entries
        self.lock = threading.Lock()
        loaded = read_json(path)
        self.entries: OrderedDict[str, dict[str, Any]] = OrderedDict(
            loaded if isinstance(loaded, dict) else {}
        )

    def get(self, key: str) -> dict[str, Any] | None:
        with self.lock:
            entry = self.entries.get(key)
            if entry is not None:
                self.entries.move_to_end(key)
            return entry

    def put(self, key: str, entry: dict[str, Any]) -> None:
        with self.lock:
            self.entries[key] = entry
            self.entries.move_to_end(key)
            while len(self.entries) > self.max_entries:
                self.entries.popitem(last=False)
            write_json_atomic(self.path, self.entries)

    def discard(self, key: str) -> None:
        with self.lock:
            if self.entries.pop(key, None) is not None:
                write_json_atomic(self.path, self.entries)

    def __len__(self) -> int:
        with self.lock:
            return len(self.entries)
