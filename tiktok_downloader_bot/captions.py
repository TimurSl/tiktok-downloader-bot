"""Caption building: description, uploader and a link back to the original post."""

from __future__ import annotations

import html
import re

from .urls import host_label

CAPTION_LIMIT = 1024
EXPANDABLE_THRESHOLD = 180
HASHTAG_TAIL_RE = re.compile(r"(?:\s*[#＃][^\s#]+)+\s*$")


def clean_title(title: str) -> str:
    """Collapse whitespace and drop the trailing hashtag list common on TikTok."""
    text = re.sub(r"\s+", " ", title or "").strip()
    stripped = HASHTAG_TAIL_RE.sub("", text).strip()
    return stripped or text


def fit_escaped(text: str, budget: int) -> str:
    """HTML-escape ``text``, shortening it with an ellipsis so the result fits ``budget``."""
    if budget <= 0:
        return ""
    escaped = html.escape(text, quote=False)
    if len(escaped) <= budget:
        return escaped
    while text and len(html.escape(text.rstrip() + "…", quote=False)) > budget:
        text = text[: max(0, int(len(text) * 0.9) - 1)]
    return html.escape(text.rstrip() + "…", quote=False) if text else ""


def source_link(uploader: str, url: str) -> str:
    link = f'<a href="{html.escape(url, quote=True)}">{html.escape(host_label(url), quote=False)}</a>'
    uploader = re.sub(r"\s+", " ", uploader or "").strip()
    if uploader:
        return f"{html.escape(uploader[:80], quote=False)} · {link}"
    return link


def build_caption(
    title: str,
    uploader: str,
    url: str,
    *,
    enabled: bool = True,
    limit: int = CAPTION_LIMIT,
) -> tuple[str, str | None]:
    """Return ``(caption, parse_mode)``. Empty caption means "send without one"."""
    if not enabled:
        return "", None

    link = source_link(uploader, url)
    body = clean_title(title)
    if not body:
        return link, "HTML"

    expandable = len(body) > EXPANDABLE_THRESHOLD
    open_tag, close_tag = ("<blockquote expandable>", "</blockquote>") if expandable else ("", "")
    budget = limit - len(link) - 2 - len(open_tag) - len(close_tag)
    escaped = fit_escaped(body, budget)
    if not escaped:
        return link, "HTML"
    return f"{open_tag}{escaped}{close_tag}\n\n{link}", "HTML"
