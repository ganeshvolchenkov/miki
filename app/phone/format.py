"""Turn Miki's replies into safe Telegram messages."""

from __future__ import annotations

import html
import re

from app.phone.telegram_api import MAX_MESSAGE_CHARS

_CHUNK = MAX_MESSAGE_CHARS - 200  # headroom for the HTML tags added after splitting


def to_telegram_html(text: str) -> str:
    """Escape everything, then re-add the little formatting Miki uses (``code`` blocks, `inline`, **bold**)."""
    parts = html.escape(text or "", quote=False).split("```")
    out = []
    for index, part in enumerate(parts):
        if index % 2 == 1:  # inside a fenced block: drop an optional language tag on the first line
            body = part.split("\n", 1)[1] if re.match(r"^[\w+-]*\n", part) else part
            out.append(f"<pre>{body.strip(chr(10))}</pre>")
        else:
            part = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", part)
            part = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", part)
            out.append(part)
    return "".join(out)


def chunk_text(text: str, limit: int = _CHUNK) -> list[str]:
    """Split on paragraph/line boundaries so no message exceeds Telegram's limit."""
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n"):
        while len(paragraph) > limit:  # a single monster line
            cut = paragraph.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            if current:
                chunks.append(current)
                current = ""
            chunks.append(paragraph[:cut])
            paragraph = paragraph[cut:].lstrip()
        candidate = f"{current}\n{paragraph}" if current else paragraph
        if len(candidate) > limit:
            chunks.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
