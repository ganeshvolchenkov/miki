"""What the phone can ask Miki to do.

The bot itself knows nothing about Miki; it talks to this backend. That keeps every rule about *what the
phone may do* in one small place, and lets the dashboard and the headless service reuse the same code.
"""

from __future__ import annotations

import io
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from app.core.interview import Interview, InterviewTurn
from app.core.mail_attention import MailAttention, MailUnavailable
from app.core.model_choice import MODEL_CHOICES, switch_model
from app.core.timeutil import local_utc_offset
from app.interfaces import memory_commands as mc
from app.phone.ui import HomeData

logger = logging.getLogger(__name__)

_MESSAGE_ID = re.compile(r"[0-9a-fA-F]{6,32}")


@dataclass
class ChatReply:
    text: str
    memories: list[dict[str, Any]] = field(default_factory=list)
    needs_confirmation: bool = False  # Miki previewed a calendar change and is waiting for yes/no


@dataclass
class PhoneHooks:
    """Optional callbacks so a dashboard can mirror phone activity (all default to no-ops)."""

    on_message: Callable[[str, str], None] = lambda role, text: None
    on_busy: Callable[[bool], None] = lambda busy: None
    on_speak: Callable[[str], None] = lambda text: None
    on_state: Callable[[], None] = lambda: None


class CoreBackend:
    def __init__(
        self,
        core: Any,
        *,
        lock: threading.RLock | None = None,
        mail: MailAttention | None = None,
        openai_client: Any = None,
        transcribe_model: str = "whisper-1",
        tts_model: str = "gpt-4o-mini-tts",
        tts_voice: str = "nova",
        hooks: PhoneHooks | None = None,
    ) -> None:
        self.core = core
        self.lock = lock or threading.RLock()
        self.mail = mail
        self.openai_client = openai_client
        self.transcribe_model = transcribe_model
        self.tts_model = tts_model
        self.tts_voice = tts_voice
        self.hooks = hooks or PhoneHooks()
        self._interview: Interview | None = None

    # ------------------------------------------------------------------ chat
    def chat(self, text: str) -> ChatReply:
        """A normal conversation turn: same brain, memory and tools as the dashboard, one at a time."""
        with self.lock:
            self.hooks.on_busy(True)
            try:
                response, _created = self.core.process_user_input(text)
                learned = list(getattr(self.core, "last_memories", None) or [])
            finally:
                self.hooks.on_busy(False)
        self.hooks.on_speak(response)
        pending = getattr(self.core, "pending_tool_action", None) is not None
        return ChatReply(response or "…", mc.toast_payload(learned), needs_confirmation=pending)

    # ------------------------------------------------------------------ commands
    def command(self, name: str, argument: str) -> str | None:
        """Text for a known command, or None if it isn't one."""
        manager = getattr(self.core, "memory_manager", None)
        try:
            if name == "mail":
                return self._mail()
            if name == "today":
                return self._today()
            if manager is None and name in {"memory", "remember", "forget", "profile", "brain"}:
                return "Memory isn't set up."
            if name == "memory":
                return mc.memory_overview(manager, argument)
            if name == "remember":
                return self._remember(argument)
            if name == "forget":
                return mc.forget(self.core, argument)
            if name == "profile":
                return mc.profile_text(manager.get_profile())
            if name == "brain":
                return mc.brain_status(manager)
        except Exception as exc:
            logger.exception("Phone command /%s failed", name)
            return f"That didn't work: {exc}"
        return None

    def _remember(self, text: str) -> str:
        if not text.strip():
            return "Usage: /remember <something about you>"
        with self.lock:
            memories = self.core.remember(text)
        if not memories:
            return "I couldn't turn that into a memory."
        return "Remembered: " + "; ".join(m.title or m.content for m in memories)

    def _mail(self) -> str:
        if self.mail is None:
            return "Mail isn't set up."
        try:
            self.mail.refresh(force=True)
        except MailUnavailable as exc:
            return str(exc)
        return self.mail.summary()

    def _today(self) -> str:
        tool = self.core.get_tool("calendar") if hasattr(self.core, "get_tool") else None
        if tool is None or not tool.is_available():
            return "Your calendar isn't connected."
        today = datetime.now()
        offset = local_utc_offset()
        result = tool.execute("get_events", {"start": today.strftime("%Y-%m-%dT00:00:00") + offset, "end": today.strftime("%Y-%m-%dT23:59:59") + offset})
        events = (result.data or {}).get("events", []) if result.success else []
        if not events:
            return "Nothing on your calendar today."
        lines = [today.strftime("Today, %A %d %B"), ""]
        for event in events:
            when = "All day" if event.get("all_day") else str(event.get("start", ""))[11:16]
            lines.append(f"{when}  {event.get('title', '')}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ voice & buttons
    @property
    def can_transcribe(self) -> bool:
        return self.openai_client is not None

    def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str:
        if self.openai_client is None:
            raise RuntimeError("Voice isn't available.")
        buffer = io.BytesIO(audio)
        buffer.name = filename  # the API decides the format from the file name
        result = self.openai_client.audio.transcriptions.create(model=self.transcribe_model, file=buffer)
        return (getattr(result, "text", "") or "").strip()

    # ------------------------------------------------------------------ voice out
    @property
    def can_speak(self) -> bool:
        return self.openai_client is not None

    def speak(self, text: str) -> bytes:
        """Ogg/Opus speech for ``text`` (a voice message Telegram can play). Tries the configured model, then a fallback."""
        if self.openai_client is None:
            raise RuntimeError("Voice isn't available.")
        last: Exception | None = None
        for model in dict.fromkeys([self.tts_model, "tts-1"]):
            try:
                response = self.openai_client.audio.speech.create(model=model, voice=self.tts_voice, input=text[:1500], response_format="opus")
                data = response.read() if hasattr(response, "read") else response.content
                if data:
                    return data
            except Exception as exc:  # try the next model
                last = exc
        raise RuntimeError(f"Couldn't generate speech: {last}")

    # ------------------------------------------------------------------ home, mail, calendar, memory (screens' data)
    def _now(self) -> datetime:
        return datetime.now().astimezone()

    def home_data(self, name: str = "") -> HomeData:
        """Everything the home card shows. Each part is best-effort: a broken service just leaves its line out."""
        now = self._now()
        data = HomeData(now=now, name=name)
        try:
            data.model = str(getattr(getattr(self.core, "brain", None), "model", "") or "")
        except Exception:
            pass
        try:
            weather = self.core.get_tool("weather") if hasattr(self.core, "get_tool") else None
            result = weather.execute("get_current_weather", {}) if weather else None
            if result is not None and result.success:
                current = (result.data or {}).get("current") or {}
                place = (result.data or {}).get("location") or ""
                data.weather = f"{current.get('temperature')}°C, {current.get('condition') or ''}" + (f" · {place}" if place else "")
        except Exception:
            logger.debug("Home: weather failed", exc_info=True)
        try:
            _, events = self.day_events(0)
            data.events_today = len(events)
            data.next_event = self._next_event(events, now)
        except Exception:
            logger.debug("Home: calendar failed", exc_info=True)
        try:
            items = self.mail_items(force=False)
            data.mail_attention = len(items)
            data.mail_urgent = sum(1 for i in items if i.priority == "urgent")
        except Exception:
            logger.debug("Home: mail failed", exc_info=True)
        manager = getattr(self.core, "memory_manager", None)
        if manager is not None:
            try:
                data.memories = manager.count_memories(active_only=True)
                profile = manager.get_profile()
                data.coverage = profile.coverage if profile is not None else None
            except Exception:
                logger.debug("Home: memory failed", exc_info=True)
        return data

    @staticmethod
    def _next_event(events: list[dict[str, Any]], now: datetime) -> str:
        for event in events:
            if event.get("all_day"):
                continue
            try:
                start = datetime.fromisoformat(str(event["start"]))
            except (KeyError, ValueError):
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=now.tzinfo)
            if start >= now:
                minutes = int((start - now).total_seconds() // 60)
                hours, mins = divmod(minutes, 60)
                until = f"{hours}h {mins}m" if hours else f"{mins}m"
                return f"{event.get('title', '')} {start.strftime('%H:%M')} (in {until})"
        return ""

    def day_events(self, offset: int = 0) -> tuple[datetime, list[dict[str, Any]]]:
        """(the day, its events) for today + ``offset`` days. Raises MailUnavailable-style RuntimeError if no calendar."""
        day = self._now() + timedelta(days=offset)
        tool = self.core.get_tool("calendar") if hasattr(self.core, "get_tool") else None
        if tool is None or not tool.is_available():
            raise RuntimeError("Your calendar isn't connected.")
        tz = local_utc_offset()
        result = tool.execute("get_events", {"start": day.strftime("%Y-%m-%dT00:00:00") + tz, "end": day.strftime("%Y-%m-%dT23:59:59") + tz})
        if not result.success:
            raise RuntimeError(result.message or "I couldn't read your calendar.")
        return day, list((result.data or {}).get("events", []))

    def mail_items(self, *, force: bool = True) -> list[Any]:
        if self.mail is None:
            raise MailUnavailable("Mail isn't set up.")
        items = self.mail.refresh(force=force)
        return list(self.mail.items if items is None else items)

    @property
    def mail_account(self) -> str:
        return self.mail.account if self.mail is not None else ""

    def snooze_mail(self, message_id: str, hours: float = 20.0) -> bool:
        if self.mail is None or not _MESSAGE_ID.fullmatch(str(message_id or "")):
            return False
        self.mail.snooze(str(message_id), hours)
        return True

    def memories(self) -> list[Any]:
        manager = getattr(self.core, "memory_manager", None)
        return manager.list_memories(active_only=True) if manager is not None else []

    def get_memory(self, memory_id: str) -> Any | None:
        manager = getattr(self.core, "memory_manager", None)
        return manager.get_memory(memory_id) if manager is not None and re.fullmatch(r"[0-9a-fA-F]{6,32}", str(memory_id or "")) else None

    def related_memories(self, memory: Any) -> list[Any]:
        from app.memory.brain import MemoryBrain

        manager = getattr(self.core, "memory_manager", None)
        if manager is None:
            return []
        return [c.memory for c in MemoryBrain(manager.list_memories(active_only=True)).neighbors(memory.memory_id, limit=3)]

    def forget_memory(self, memory_id: str) -> Any | None:
        memory = self.get_memory(memory_id)
        if memory is None:
            return None
        with self.lock:
            return self.core.delete_memory(memory.memory_id)

    def profile(self, *, refresh: bool = False) -> Any | None:
        manager = getattr(self.core, "memory_manager", None)
        if manager is None:
            return None
        return manager.refresh_profile() if refresh else manager.get_profile()

    # ------------------------------------------------------------------ interview
    @property
    def interview_active(self) -> bool:
        return self._interview is not None and self._interview.active

    def interview_start(self) -> InterviewTurn:
        manager = getattr(self.core, "memory_manager", None)
        if manager is None:
            return InterviewTurn("Memory isn't set up.", finished=True)
        self._interview = Interview(manager)
        with self.lock:
            return self._interview.start()

    def interview_reply(self, text: str) -> InterviewTurn:
        if self._interview is None:
            return InterviewTurn("There's no interview running.", finished=True)
        with self.lock:
            return self._interview.respond(text)

    def interview_stop(self) -> InterviewTurn:
        if self._interview is None:
            return InterviewTurn("There's no interview running.", finished=True)
        with self.lock:
            return self._interview.stop()

    # ------------------------------------------------------------------ model
    def models(self) -> tuple[str, list[tuple[str, str, str]]]:
        return str(getattr(getattr(self.core, "brain", None), "model", "")), MODEL_CHOICES

    def set_model(self, index: int) -> str | None:
        """None on success, else why it failed."""
        if not 0 <= index < len(MODEL_CHOICES):
            return "Unknown model."
        return switch_model(self.core.brain, MODEL_CHOICES[index][0])

    def dismiss_mail(self, message_id: str) -> bool:
        if self.mail is None or not _MESSAGE_ID.fullmatch(str(message_id or "")):
            return False
        self.mail.dismiss(str(message_id))
        return True
