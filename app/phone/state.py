"""Who the bot may talk to, and what it has already told them.

The bot is locked to one Telegram account. Linking works like pairing a Bluetooth device: Miki's dashboard
shows a one-time code (or a one-tap link), and the account that sends it becomes the owner. After that,
everyone else is ignored without a word.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CODE_TTL_SECONDS = 10 * 60
MAX_WRONG_CODES = 5
LOCKOUT_SECONDS = 15 * 60
NOTIFIED_RETENTION_DAYS = 30


DEFAULT_PREFS: dict[str, Any] = {
    "urgent_push": True,  # push urgent mail
    "morning_brief": False,  # a daily summary at ``brief_time`` (opt-in)
    "brief_time": "08:30",
    "voice_replies": True,  # answer voice notes with a voice note too
}


class PhoneState:
    """Persistent, thread-safe state in ``data/phone.json`` (never contains the bot token)."""

    def __init__(self, path: str | Path = "data/phone.json") -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: dict[str, Any] = self._load()

    # ------------------------------------------------------------------ storage
    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(self._data), encoding="utf-8")
            temp.replace(self.path)
        except OSError:
            logger.warning("Could not save phone state", exc_info=True)

    # ------------------------------------------------------------------ owner
    @property
    def chat_id(self) -> int | None:
        with self._lock:
            value = self._data.get("chat_id")
            return int(value) if value is not None else None

    @property
    def is_paired(self) -> bool:
        return self.chat_id is not None

    @property
    def owner_name(self) -> str:
        with self._lock:
            return str(self._data.get("owner_name", ""))

    def is_owner(self, chat_id: int) -> bool:
        return self.chat_id is not None and self.chat_id == chat_id

    def unpair(self) -> None:
        with self._lock:
            for key in ("chat_id", "owner_name", "paired_at", "pending_code", "code_expires", "wrong_codes", "locked_until"):
                self._data.pop(key, None)
            self._save()

    # ------------------------------------------------------------------ pairing
    @staticmethod
    def _digest(code: str) -> str:
        return hashlib.sha256(code.encode("utf-8")).hexdigest()

    def new_pairing_code(self) -> str:
        """A fresh one-time 6-digit code (valid 10 minutes). Only its hash is stored."""
        code = f"{secrets.randbelow(10**6):06d}"
        with self._lock:
            self._data["pending_code"] = self._digest(code)
            self._data["code_expires"] = time.time() + CODE_TTL_SECONDS
            self._data["wrong_codes"] = 0
            self._save()
        return code

    def has_pending_code(self) -> bool:
        with self._lock:
            return bool(self._data.get("pending_code")) and time.time() < float(self._data.get("code_expires", 0))

    def try_pair(self, code: str, chat_id: int, owner_name: str) -> str:
        """Returns 'paired', 'wrong', 'expired', 'locked' or 'already'. Constant-time compare; brute-force lockout."""
        code = "".join(ch for ch in str(code) if ch.isdigit())
        with self._lock:
            if self.is_paired:
                return "already"
            if time.time() < float(self._data.get("locked_until", 0)):
                return "locked"
            if not self._data.get("pending_code") or time.time() >= float(self._data.get("code_expires", 0)):
                return "expired"
            if len(code) == 6 and hmac.compare_digest(self._digest(code), str(self._data["pending_code"])):
                self._data.update({"chat_id": int(chat_id), "owner_name": owner_name, "paired_at": time.time()})
                for key in ("pending_code", "code_expires", "wrong_codes", "locked_until"):
                    self._data.pop(key, None)
                self._save()
                return "paired"
            wrong = int(self._data.get("wrong_codes", 0)) + 1
            self._data["wrong_codes"] = wrong
            if wrong >= MAX_WRONG_CODES:
                self._data.update({"locked_until": time.time() + LOCKOUT_SECONDS, "pending_code": "", "wrong_codes": 0})
            self._save()
            return "locked" if wrong >= MAX_WRONG_CODES else "wrong"

    # ------------------------------------------------------------------ preferences (set from the phone's Settings)
    def pref(self, name: str, default: Any = None) -> Any:
        """A stored preference, else ``default``, else the built-in default."""
        with self._lock:
            stored = self._data.get("prefs", {})
            if name in stored:
                return stored[name]
        return default if default is not None else DEFAULT_PREFS.get(name)

    def set_pref(self, name: str, value: Any) -> None:
        with self._lock:
            self._data.setdefault("prefs", {})[name] = value
            self._save()

    # ------------------------------------------------------------------ notifications already sent
    def already_notified(self, key: str) -> bool:
        with self._lock:
            return key in self._data.get("notified", {})

    def mark_notified(self, key: str) -> None:
        with self._lock:
            notified = self._data.setdefault("notified", {})
            notified[key] = time.time()
            cutoff = time.time() - NOTIFIED_RETENTION_DAYS * 86400
            self._data["notified"] = {k: v for k, v in notified.items() if v >= cutoff}
            self._save()

    def flag(self, name: str) -> bool:
        with self._lock:
            return bool(self._data.get("flags", {}).get(name))

    def set_flag(self, name: str, value: bool = True) -> None:
        with self._lock:
            self._data.setdefault("flags", {})[name] = value
            self._save()
