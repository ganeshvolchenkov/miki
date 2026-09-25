"""A small Telegram Bot API client over plain HTTPS (stdlib only).

The bot token is a password. It appears in every request URL, so it is scrubbed from all error text and
never logged.
"""

from __future__ import annotations

import json
import logging
import uuid
import urllib.error
import urllib.request
from typing import Any, Callable

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 4096
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # the Bot API's own limit for getFile


class TelegramError(Exception):
    """A failed Bot API call. ``status`` is the HTTP status (0 for network errors)."""

    def __init__(self, description: str, *, status: int = 0, retry_after: float = 0.0) -> None:
        super().__init__(description)
        self.description = description
        self.status = status
        self.retry_after = retry_after

    @property
    def is_auth_error(self) -> bool:
        return self.status in {401, 404}  # bad/revoked token

    @property
    def is_conflict(self) -> bool:
        return self.status == 409  # another process is polling this bot


Opener = Callable[[urllib.request.Request, float], Any]


def _default_opener(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310  (fixed https host)


class TelegramApi:
    def __init__(self, token: str, *, opener: Opener | None = None, base: str = API_BASE) -> None:
        if not token or ":" not in token:
            raise ValueError("A Telegram bot token looks like '123456:ABC...' (get one from @BotFather).")
        self._token = token
        self._opener = opener or _default_opener
        self._base = base.rstrip("/")

    # ---------------------------------------------------------------- transport
    def _scrub(self, text: str) -> str:
        return str(text).replace(self._token, "<token>")

    def call(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30) -> Any:
        request = urllib.request.Request(
            f"{self._base}/bot{self._token}/{method}",
            data=json.dumps(params or {}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(request, timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail, retry_after = "", 0.0
            try:
                body = json.loads(exc.read().decode("utf-8"))
                detail = str(body.get("description", ""))
                retry_after = float((body.get("parameters") or {}).get("retry_after", 0))
            except Exception:
                pass
            raise TelegramError(self._scrub(detail or f"HTTP {exc.code}"), status=exc.code, retry_after=retry_after) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise TelegramError(self._scrub(f"Network error: {exc}"), status=0) from None

        if not payload.get("ok"):
            raise TelegramError(self._scrub(str(payload.get("description", "Telegram returned an error"))), status=int(payload.get("error_code", 0)))
        return payload.get("result")

    # ---------------------------------------------------------------- methods
    def get_me(self) -> dict[str, Any]:
        return self.call("getMe")

    def get_updates(self, offset: int | None, *, timeout: int = 25) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", params, timeout=timeout + 15) or []

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        buttons: list[list[dict[str, str]]] | None = None,
        parse_mode: str | None = "HTML",
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``buttons`` = an inline keyboard under the message; ``reply_markup`` = anything else (the persistent
        bottom keyboard, force-reply...). Only one of them is used."""
        params: dict[str, Any] = {"chat_id": chat_id, "text": text[:MAX_MESSAGE_CHARS], "disable_web_page_preview": True}
        if parse_mode:
            params["parse_mode"] = parse_mode
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        elif reply_markup:
            params["reply_markup"] = reply_markup
        return self.call("sendMessage", params)

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        buttons: list[list[dict[str, str]]] | None = None,
        parse_mode: str | None = "HTML",
    ) -> None:
        """Replace a message's text and buttons in place (what makes navigation feel like an app)."""
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:MAX_MESSAGE_CHARS],
            "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": buttons or []},
        }
        if parse_mode:
            params["parse_mode"] = parse_mode
        self.call("editMessageText", params)

    def set_reaction(self, chat_id: int, message_id: int, emoji: str | None) -> None:
        """React to a message (e.g. 👀 = "seen"); ``None`` clears it. Purely cosmetic, never raises."""
        try:
            reaction = [{"type": "emoji", "emoji": emoji}] if emoji else []
            self.call("setMessageReaction", {"chat_id": chat_id, "message_id": message_id, "reaction": reaction}, timeout=10)
        except TelegramError:
            logger.debug("setMessageReaction failed", exc_info=True)

    def set_menu_button(self) -> None:
        """Show the command list as the bot's menu button."""
        try:
            self.call("setChatMenuButton", {"menu_button": {"type": "commands"}}, timeout=10)
        except TelegramError:
            logger.debug("setChatMenuButton failed", exc_info=True)

    def send_voice(self, chat_id: int, audio: bytes, *, caption: str = "", buttons: list[list[dict[str, str]]] | None = None) -> dict[str, Any]:
        """Upload an Ogg/Opus voice message (multipart, stdlib only)."""
        boundary = uuid.uuid4().hex
        fields: list[tuple[str, str]] = [("chat_id", str(chat_id))]
        if caption:
            fields.append(("caption", caption[:1000]))
        if buttons:
            fields.append(("reply_markup", json.dumps({"inline_keyboard": buttons})))
        body = b""
        for name, value in fields:
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="voice"; filename="miki.ogg"\r\n'
            "Content-Type: audio/ogg\r\n\r\n"
        ).encode("utf-8") + audio + f"\r\n--{boundary}--\r\n".encode("utf-8")
        request = urllib.request.Request(
            f"{self._base}/bot{self._token}/sendVoice",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with self._opener(request, 60) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise TelegramError(self._scrub(f"HTTP {exc.code}"), status=exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise TelegramError(self._scrub(f"Network error: {exc}"), status=0) from None
        if not payload.get("ok"):
            raise TelegramError(self._scrub(str(payload.get("description", "Telegram returned an error"))), status=int(payload.get("error_code", 0)))
        return payload.get("result") or {}

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        try:
            self.call("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=10)
        except TelegramError:
            logger.debug("sendChatAction failed", exc_info=True)  # cosmetic; never worth interrupting a reply

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:180]}, timeout=10)
        except TelegramError:
            logger.debug("answerCallbackQuery failed", exc_info=True)

    def remove_buttons(self, chat_id: int, message_id: int) -> None:
        try:
            self.call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}}, timeout=10)
        except TelegramError:
            logger.debug("editMessageReplyMarkup failed", exc_info=True)

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        self.call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in commands]}, timeout=15)

    def download_file(self, file_id: str, *, max_bytes: int = MAX_DOWNLOAD_BYTES) -> bytes:
        info = self.call("getFile", {"file_id": file_id}, timeout=15)
        if int(info.get("file_size", 0) or 0) > max_bytes:
            raise TelegramError("That file is too large.", status=413)
        request = urllib.request.Request(f"{self._base}/file/bot{self._token}/{info['file_path']}")
        try:
            with self._opener(request, 60) as response:
                data = response.read(max_bytes + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TelegramError(self._scrub(f"Download failed: {exc}"), status=0) from None
        if len(data) > max_bytes:
            raise TelegramError("That file is too large.", status=413)
        return data
