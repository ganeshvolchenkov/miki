"""The Telegram bot: long-polls for updates and turns them into an app-like experience.

Two ways to use it, both always available:
* talk to it (text or voice notes): Miki answers like the assistant on your dashboard;
* use it as a bot: a home screen, buttons, cards that update in place, guided prompts, settings.

Security model (the important part, since this can read your mail and memory):
* Only *private* chats are considered; groups are ignored.
* Until paired, the only thing it will do is accept a valid one-time pairing code.
* After pairing, every other account is ignored completely: no reply, no hint that Miki is listening.
* Text messages older than 10 minutes are skipped (a backlog delivered after downtime must never replay
  commands like /forget), and the owner is told how many were skipped.
"""

from __future__ import annotations

import html
import itertools
import logging
import re
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from typing import Any, Callable

from app.core.mail_triage import gmail_url
from app.phone import ui
from app.phone.backend import PhoneHooks
from app.phone.format import chunk_text, to_telegram_html
from app.phone.state import PhoneState
from app.phone.telegram_api import TelegramApi, TelegramError

logger = logging.getLogger(__name__)

MAX_MESSAGE_AGE = 10 * 60
MAX_INPUT_CHARS = 4000
MAX_VOICE_SECONDS = 120
MAX_MESSAGES_PER_MINUTE = 30
MAX_SPOKEN_CHARS = 1500
MAX_MAIL_CARDS = 5
REMEMBERED_REPLIES = 40

COMMANDS = [
    ("home", "Your home screen"),
    ("mail", "Emails that need your attention"),
    ("today", "Your calendar"),
    ("brief", "Your day in one message"),
    ("memory", "What I remember about you"),
    ("profile", "Who I think you are"),
    ("interview", "Let me get to know you"),
    ("add", "Add an event or a memory"),
    ("settings", "Alerts, quiet hours, voice, model"),
    ("model", "Choose the AI model"),
    ("help", "What I can do"),
]

_SCREEN_COMMANDS = {"home", "mail", "today", "brief", "profile", "add", "settings", "model", "help"}
_MESSAGE_ID = re.compile(r"[0-9a-fA-F]{6,32}")
_NOT_MODIFIED = "not modified"
_HOME_BUTTON = [[{"text": "🏠 Home", "callback_data": "nav:home"}]]


def _plain(text: str) -> str:
    """HTML -> plain text (fallback when Telegram rejects the markup)."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


class PhoneBot:
    def __init__(
        self,
        api: TelegramApi,
        state: PhoneState,
        backend: Any,
        *,
        hooks: PhoneHooks | None = None,
        quiet_default: str = "23:00-08:00",
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api = api
        self.state = state
        self.backend = backend
        self.hooks = hooks or PhoneHooks()
        self.quiet_default = quiet_default
        self._clock = clock
        self._sleep = sleep
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset: int | None = None
        self._skipped = 0
        self._recent: list[float] = []
        self._warned_strangers: set[int] = set()
        self._unpaired_replied: dict[int, float] = {}
        self._awaiting: str | None = None  # a guided prompt ("event", "remember", "chat") waiting for the next message
        self._replies: "OrderedDict[str, str]" = OrderedDict()  # key -> text, for the 🔊 Listen button
        self._keys = itertools.count(1)
        self.username = ""
        self.last_error = ""

    # ------------------------------------------------------------------ lifecycle
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="miki-phone", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            me = self.api.get_me()
            self.username = str(me.get("username", ""))
            self.last_error = ""
            self.hooks.on_state()
            try:
                self.api.set_commands(COMMANDS)
                self.api.set_menu_button()
            except TelegramError:
                logger.debug("Could not register the command menu", exc_info=True)
        except TelegramError as exc:
            self.last_error = "The Telegram bot token was rejected." if exc.is_auth_error else f"Can't reach Telegram: {exc.description}"
            logger.warning("Phone bot not started: %s", self.last_error)
            self.hooks.on_state()
            return

        delay = 1.0
        while not self._stop.is_set():
            try:
                updates = self.api.get_updates(self._offset)
                delay = 1.0
                self.last_error = ""
            except TelegramError as exc:
                self._on_poll_error(exc)
                if exc.is_auth_error:
                    return
                self._sleep(max(delay, exc.retry_after))
                delay = min(delay * 2, 60)
                continue
            for update in updates:
                self._offset = int(update["update_id"]) + 1
                try:
                    self.handle_update(update)
                except Exception:
                    logger.exception("Phone update failed")

    def _on_poll_error(self, exc: TelegramError) -> None:
        if exc.is_auth_error:
            self.last_error = "The Telegram bot token was rejected."
        elif exc.is_conflict:
            self.last_error = "Another Miki is already running this bot."
        else:
            self.last_error = f"Can't reach Telegram: {exc.description}"
        self.hooks.on_state()

    # ------------------------------------------------------------------ routing
    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self._on_callback(update["callback_query"])
        elif "message" in update:
            self._on_message(update["message"])

    def _on_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None or chat.get("type") != "private":
            return  # groups, channels: never
        sender = message.get("from") or {}
        name = str(sender.get("first_name") or sender.get("username") or "you")
        text = str(message.get("text") or "")

        if not self.state.is_paired:
            self._handle_unpaired(chat_id, name, text)
            return
        if not self.state.is_owner(chat_id):
            if chat_id not in self._warned_strangers:  # log once, say nothing to them
                self._warned_strangers.add(chat_id)
                logger.warning("Ignored a message from an unlinked Telegram account")
            return

        if self._is_stale(message):
            self._skipped += 1
            return
        self._flush_skipped(chat_id)
        if self._rate_limited():
            self._send(chat_id, "Slow down a little, I'll catch up.", mode="plain")
            return

        message_id = message.get("message_id")
        if message.get("voice") or message.get("audio"):
            self._on_voice(chat_id, message.get("voice") or message.get("audio"), message_id)
        elif text in ui.KEYBOARD_LABELS:
            self._awaiting = None
            self._open(chat_id, ui.KEYBOARD_LABELS[text])
        elif text.startswith("/"):
            self._on_command(chat_id, text)
        elif self._awaiting:
            kind, self._awaiting = self._awaiting, None
            self._on_awaited(chat_id, kind, text[:MAX_INPUT_CHARS], message_id)
        elif self.backend.interview_active:
            self._interview_step(chat_id, lambda: self.backend.interview_reply(text[:MAX_INPUT_CHARS]))
        elif text.strip():
            self._on_chat(chat_id, text[:MAX_INPUT_CHARS], message_id)

    # ------------------------------------------------------------------ pairing
    def _handle_unpaired(self, chat_id: int, name: str, text: str) -> None:
        command, _, argument = text.strip().partition(" ")
        command = command.split("@")[0].lower()
        if command in {"/start", "/pair"} and argument.strip():
            outcome = self.state.try_pair(argument.strip(), chat_id, name)
            if outcome == "paired":
                self.hooks.on_state()
                self._send(chat_id, ui.welcome_text(name), mode="html", reply_markup=ui.REPLY_KEYBOARD)
                self._open(chat_id, "home")
                return
            self._send(chat_id, {
                "wrong": "That code isn't right.",
                "expired": "That code has expired. Type /phone in Miki on your computer for a new one.",
                "locked": "Too many wrong codes. Try again in a while.",
            }.get(outcome, "That didn't work."), mode="plain")
            return
        now = self._clock()
        if now - self._unpaired_replied.get(chat_id, 0) > 60:  # don't let strangers make the bot chatty
            self._unpaired_replied[chat_id] = now
            self._send(chat_id, "This is a private assistant. To link it, type /phone in Miki on your computer, then send /pair <code> here.", mode="plain")

    # ------------------------------------------------------------------ commands
    def _on_command(self, chat_id: int, text: str) -> None:
        command, _, argument = text[1:].partition(" ")
        command, argument = command.split("@")[0].lower(), argument.strip()
        self._awaiting = None
        if command in {"start", "menu"}:
            self._send(chat_id, ui.welcome_text(self.state.owner_name or "you"), mode="html", reply_markup=ui.REPLY_KEYBOARD)
            self._open(chat_id, "home")
        elif command == "unlink":
            self.state.unpair()
            self.hooks.on_state()
            self._send(chat_id, "Disconnected. Nobody can talk to Miki through this bot until you link a phone again.", mode="plain")
        elif command == "interview":
            self._interview_step(chat_id, self.backend.interview_start)
        elif command in _SCREEN_COMMANDS:
            self._open(chat_id, command)
        elif command == "memory":
            if argument:
                self._send(chat_id, self._backend_text("memory", argument), mode="plain")
            else:
                self._open(chat_id, "memory")
        elif command in {"remember", "forget", "brain"}:
            with self._typing(chat_id):
                self._send(chat_id, self._backend_text(command, argument), mode="plain")
        else:
            self._send(chat_id, f"I don't know /{command}. Try /help.", mode="plain")

    def _backend_text(self, command: str, argument: str) -> str:
        return self.backend.command(command, argument) or "I couldn't do that."

    # ------------------------------------------------------------------ screens
    def _open(self, chat_id: int, name: str, arg: str = "", edit_id: int | None = None) -> None:
        """Show a screen: as a new message (typed commands / bottom keyboard) or by editing the tapped one."""
        try:
            if name == "mail":
                self._send_mail(chat_id)
                return
            if edit_id is None:  # a fresh message: show "typing…" while slow services (Gmail, weather) answer
                with self._typing(chat_id):
                    screen = self._build_screen(name, arg)
            else:
                screen = self._build_screen(name, arg)
        except Exception as exc:
            logger.exception("Screen %s failed", name)
            screen = ui.Screen(f"That didn't work: {ui.esc(exc)}", _HOME_BUTTON)
        self._show(chat_id, screen, edit_id)

    def _build_screen(self, name: str, arg: str) -> ui.Screen:
        if name == "home":
            return ui.home_screen(self.backend.home_data(self.state.owner_name))
        if name == "today":
            offset = max(-30, min(30, int(arg or 0)))
            try:
                day, events = self.backend.day_events(offset)
            except RuntimeError as exc:
                return ui.Screen(f"📅 {ui.esc(exc)}", _HOME_BUTTON)
            return ui.today_screen(offset, day, events)
        if name == "memory":
            return ui.memory_list_screen(self.backend.memories(), int(arg or 0))
        if name == "profile":
            return ui.profile_screen(self.backend.profile(refresh=arg == "refresh"))
        if name == "settings":
            return ui.settings_screen(self._prefs(), self.backend.models()[0])
        if name == "model":
            current, choices = self.backend.models()
            return ui.model_screen(current, choices)
        if name == "add":
            return ui.add_screen()
        if name == "help":
            return ui.help_screen()
        if name == "brief":
            return self._brief_screen()
        raise ValueError(f"unknown screen {name}")

    def _prefs(self) -> dict[str, Any]:
        return {
            "urgent_push": bool(self.state.pref("urgent_push")),
            "quiet_hours": str(self.state.pref("quiet_hours", self.quiet_default)),
            "morning_brief": bool(self.state.pref("morning_brief")),
            "brief_time": str(self.state.pref("brief_time")),
            "voice_replies": bool(self.state.pref("voice_replies")),
        }

    def _brief_screen(self) -> ui.Screen:
        data = self.backend.home_data(self.state.owner_name)
        try:
            _, events = self.backend.day_events(0)
        except RuntimeError:
            events = []
        try:
            urgent = [i for i in self.backend.mail_items(force=False) if i.priority == "urgent"]
        except Exception:
            urgent = []
        return ui.brief_screen(data, events, urgent)

    def _send_mail(self, chat_id: int) -> None:
        with self._typing(chat_id):
            try:
                items = self.backend.mail_items(force=True)
            except Exception as exc:
                self._show(chat_id, ui.Screen(f"📬 {ui.esc(exc)}", _HOME_BUTTON))
                return
        shown = items[:MAX_MAIL_CARDS]
        self._show(chat_id, ui.mail_header(len(items), len(shown)))
        for item in shown:
            self._show(chat_id, ui.mail_card(item, gmail_url(item.thread_id, self.backend.mail_account)))

    def _show(self, chat_id: int, screen: ui.Screen, edit_id: int | None = None) -> int | None:
        """Edit message ``edit_id`` in place if given, else send a new one. Returns the message id."""
        if edit_id is not None:
            try:
                self.api.edit_message(chat_id, edit_id, screen.text, buttons=screen.buttons)
                return edit_id
            except TelegramError as exc:
                if _NOT_MODIFIED in exc.description.lower():
                    return edit_id  # tapped the same thing twice: nothing to change
                logger.debug("Edit failed (%s); sending a new message", exc.description)
        return self._send(chat_id, screen.text, mode="html", buttons=screen.buttons)

    # ------------------------------------------------------------------ conversation
    def _on_chat(self, chat_id: int, text: str, message_id: int | None, *, spoken: bool = False) -> None:
        self.hooks.on_message("user", text)
        self._react(chat_id, message_id, "👀")
        try:
            with self._typing(chat_id):
                reply = self.backend.chat(text)
        except Exception as exc:
            logger.exception("Phone chat failed")
            self._react(chat_id, message_id, None)
            self._send(chat_id, f"Something went wrong: {exc}", mode="plain")
            return
        self._react(chat_id, message_id, None)
        self.hooks.on_message("assistant", reply.text)

        buttons = ui.confirm_buttons() if reply.needs_confirmation else []
        can_speak = getattr(self.backend, "can_speak", False) and len(reply.text) <= MAX_SPOKEN_CHARS
        self._send(chat_id, reply.text, buttons=buttons or None, listen=can_speak)
        if reply.memories:
            lines = "\n".join(f"• {m['title']}" for m in reply.memories[:4])
            self._send(chat_id, f"🧠 Remembered:\n{lines}", mode="plain")
        if spoken and can_speak and self.state.pref("voice_replies"):
            self._speak(chat_id, reply.text)

    def _on_awaited(self, chat_id: int, kind: str, text: str, message_id: int | None) -> None:
        if kind == "remember":
            with self._typing(chat_id):
                result = self._backend_text("remember", text)
            self._send(chat_id, f"✅ {result}" if result.startswith("Remembered") else result, mode="plain")
        elif kind == "event":
            self._on_chat(chat_id, f"Add this to my calendar: {text}", message_id)
        else:
            self._on_chat(chat_id, text, message_id)

    def _interview_step(self, chat_id: int, action: Callable[[], Any]) -> None:
        with self._typing(chat_id):
            turn = action()
        if turn.memories:
            titles = "; ".join((m.title or m.content) for m in turn.memories[:3])
            self._send(chat_id, f"🧠 Noted: {titles}", mode="plain")
        self._show(chat_id, ui.interview_screen(turn.text, finished=turn.finished))

    # ------------------------------------------------------------------ voice
    def _on_voice(self, chat_id: int, voice: dict[str, Any], message_id: int | None) -> None:
        if not getattr(self.backend, "can_transcribe", False):
            self._send(chat_id, "I can't listen to voice notes right now.", mode="plain")
            return
        if int(voice.get("duration", 0) or 0) > MAX_VOICE_SECONDS:
            self._send(chat_id, "That voice note is long. Keep them under two minutes.", mode="plain")
            return
        self._react(chat_id, message_id, "👀")  # "seen" for the whole time we work on it, always cleared after
        try:
            try:
                with self._typing(chat_id):
                    text = self.backend.transcribe(self.api.download_file(voice["file_id"]), "voice.ogg")
            except Exception as exc:
                logger.warning("Voice note failed: %s", exc)
                self._send(chat_id, "I couldn't process that voice note.", mode="plain")
                return
            if not text:
                self._send(chat_id, "I couldn't make that out. Could you say it again?", mode="plain")
                return
            self._send(chat_id, f"🎙 Heard: “{text}”", mode="plain")
            if self._awaiting:
                kind, self._awaiting = self._awaiting, None
                self._on_awaited(chat_id, kind, text[:MAX_INPUT_CHARS], None)
            else:
                self._on_chat(chat_id, text[:MAX_INPUT_CHARS], None, spoken=True)
        finally:
            self._react(chat_id, message_id, None)

    def _speak(self, chat_id: int, text: str) -> bool:
        try:
            self.api.send_chat_action(chat_id, "record_voice")
            self.api.send_voice(chat_id, self.backend.speak(text))
            return True
        except Exception as exc:
            logger.warning("Could not send a voice reply: %s", exc)
            return False

    # ------------------------------------------------------------------ buttons
    def _on_callback(self, query: dict[str, Any]) -> None:
        message = query.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id is None or not self.state.is_owner(chat_id):
            return  # a button press from anyone else is ignored too
        callback_id = str(query.get("id", ""))
        kind, _, rest = str(query.get("data", "")).partition(":")
        toast = ""
        try:
            toast = self._dispatch_callback(chat_id, message.get("message_id"), message, kind, rest) or ""
        except Exception:
            logger.exception("Button %s failed", kind)
            toast = "Something went wrong"
        self.api.answer_callback(callback_id, toast)

    def _dispatch_callback(self, chat_id: int, message_id: int | None, message: dict[str, Any], kind: str, rest: str) -> str:
        if kind == "nav":
            name, _, arg = rest.partition(":")
            name = "memory" if name == "mem" else name
            if name == "mail":
                self._open(chat_id, "mail")
            elif name in {"home", "today", "memory", "profile", "settings", "model", "add"}:
                self._open(chat_id, name, arg, edit_id=message_id)
            return ""
        if kind in {"mem", "memf", "memy"}:
            return self._memory_action(chat_id, message_id, kind, rest)
        if kind in {"md", "mz"}:
            return self._mail_action(chat_id, message_id, message, kind, rest)
        if kind == "set":
            self._setting(rest)
            self._open(chat_id, "settings", edit_id=message_id)
            return "Saved"
        if kind == "model":
            problem = self.backend.set_model(int(rest)) if rest.isdigit() else "Unknown model."
            self._open(chat_id, "settings", edit_id=message_id)
            return problem or f"Switched to {self.backend.models()[0]}"
        if kind == "iv":
            actions = {"start": self.backend.interview_start, "skip": lambda: self.backend.interview_reply("skip"), "stop": self.backend.interview_stop}
            if rest in actions:
                self._interview_step(chat_id, actions[rest])
            return ""
        if kind == "cf" and rest in {"yes", "no"}:
            if message_id is not None:
                self.api.remove_buttons(chat_id, message_id)
            self._on_chat(chat_id, "Yes, go ahead." if rest == "yes" else "No, cancel that.", None)
            return ""
        if kind == "ask" and rest in ui.PROMPTS:
            self._awaiting = rest
            self._send(chat_id, ui.PROMPTS[rest][0], mode="html", reply_markup=ui.prompt_markup(rest))
            return ""
        if kind == "tts":
            text = self._replies.get(rest)
            if not text:
                return "I no longer have that message"
            if not getattr(self.backend, "can_speak", False):
                return "Voice isn't available"
            return "" if self._speak(chat_id, text) else "Couldn't generate the voice"
        return ""

    def _memory_action(self, chat_id: int, message_id: int | None, kind: str, rest: str) -> str:
        memory_id, _, page = rest.partition(":")
        page_no = int(page) if page.isdigit() else 0
        memory = self.backend.get_memory(memory_id)
        if memory is None:
            self._open(chat_id, "memory", str(page_no), edit_id=message_id)
            return "That memory is already gone"
        if kind == "mem":
            self._show(chat_id, ui.memory_detail_screen(memory, self.backend.related_memories(memory), page_no), message_id)
        elif kind == "memf":
            self._show(chat_id, ui.forget_confirm_screen(memory, page_no), message_id)
        else:
            self.backend.forget_memory(memory_id)
            self._open(chat_id, "memory", str(page_no), edit_id=message_id)
            return "Forgotten"
        return ""

    def _mail_action(self, chat_id: int, message_id: int | None, message: dict[str, Any], kind: str, mail_id: str) -> str:
        if not _MESSAGE_ID.fullmatch(mail_id):
            return "That doesn't look right"
        done = self.backend.dismiss_mail(mail_id) if kind == "md" else self.backend.snooze_mail(mail_id)
        if not done:
            return "Couldn't do that"
        if message_id is not None:
            verb = "✓ Dismissed" if kind == "md" else "💤 Snoozed until tomorrow"
            original = html.escape(str(message.get("text") or ""), quote=False)
            try:
                self.api.edit_message(chat_id, message_id, ui.mail_done_text(original, verb), buttons=None)
            except TelegramError:
                self.api.remove_buttons(chat_id, message_id)
        return "Dismissed" if kind == "md" else "Snoozed"

    def _setting(self, rest: str) -> None:
        name, _, value = rest.partition(":")
        if name == "urgent":
            enabled = not self.state.pref("urgent_push")
            self.state.set_pref("urgent_push", enabled)
            if enabled:
                self.state.set_flag("mail_baselined", False)  # don't flood with what piled up while it was off
        elif name == "brief":
            self.state.set_pref("morning_brief", not self.state.pref("morning_brief"))
        elif name == "voice":
            self.state.set_pref("voice_replies", not self.state.pref("voice_replies"))
        elif name == "quiet" and value in {preset for preset, _ in ui.QUIET_PRESETS}:
            self.state.set_pref("quiet_hours", value)
        elif name == "btime" and len(value) == 4 and f"{value[:2]}:{value[2:]}" in ui.BRIEF_TIMES:
            self.state.set_pref("brief_time", f"{value[:2]}:{value[2:]}")

    # ------------------------------------------------------------------ helpers
    def _is_stale(self, message: dict[str, Any]) -> bool:
        date = message.get("date")
        return bool(date) and self._clock() - float(date) > MAX_MESSAGE_AGE

    def _flush_skipped(self, chat_id: int) -> None:
        skipped = self._skipped
        if skipped:
            self._skipped = 0
            self._send(chat_id, f"I was offline, so I skipped {skipped} old message{'s' if skipped != 1 else ''}. Send them again if they still matter.", mode="plain")

    def _rate_limited(self) -> bool:
        now = self._clock()
        self._recent = [t for t in self._recent if now - t < 60]
        self._recent.append(now)
        return len(self._recent) > MAX_MESSAGES_PER_MINUTE

    def _react(self, chat_id: int, message_id: int | None, emoji: str | None) -> None:
        if message_id is not None:
            self.api.set_reaction(chat_id, message_id, emoji)

    @contextmanager
    def _typing(self, chat_id: int):
        """Show 'typing…' in Telegram while Miki thinks (it expires after ~5 s, so refresh it)."""
        done = threading.Event()
        self.api.send_chat_action(chat_id)  # immediate feedback, before any slow work starts

        def refresh() -> None:
            while not done.wait(4):
                self.api.send_chat_action(chat_id)

        thread = threading.Thread(target=refresh, daemon=True)
        thread.start()
        try:
            yield
        finally:
            done.set()

    def _send(
        self,
        chat_id: int,
        text: str,
        *,
        mode: str = "md",
        buttons: list[list[dict[str, str]]] | None = None,
        reply_markup: dict[str, Any] | None = None,
        listen: bool = False,
    ) -> int | None:
        """Send ``text``, split into chunks if long; buttons go on the last chunk. Returns that message's id.

        mode: "md" = Miki's own text (escaped, with light markdown), "html" = already Telegram HTML (screens),
        "plain" = no formatting at all.
        """
        chunks = chunk_text(text) or [""]
        if listen:  # give the reply a 🔊 Listen button that can find its text again later
            key = str(next(self._keys))
            self._remember_reply(key, text)
            buttons = list(buttons or []) + [[ui.listen_button(key)]]
        last_id: int | None = None
        for index, chunk in enumerate(chunks):
            final = index == len(chunks) - 1
            inline = buttons if final else None
            markup = reply_markup if final and not inline else None
            try:
                result = self._deliver(chat_id, chunk, mode, inline, markup)
            except TelegramError as exc:
                if mode != "plain" and exc.status == 400:  # malformed formatting: resend as plain text
                    try:
                        result = self.api.send_message(chat_id, _plain(chunk), buttons=inline, parse_mode=None, reply_markup=markup)
                    except TelegramError:
                        logger.warning("Could not send a message")
                        continue
                else:
                    logger.warning("Could not send a message: %s", exc.description)
                    continue
            last_id = (result or {}).get("message_id")
        return last_id

    def _deliver(self, chat_id: int, chunk: str, mode: str, inline: Any, markup: Any) -> Any:
        if mode == "plain":
            return self.api.send_message(chat_id, chunk, buttons=inline, parse_mode=None, reply_markup=markup)
        body = chunk if mode == "html" else to_telegram_html(chunk)
        return self.api.send_message(chat_id, body, buttons=inline, reply_markup=markup)

    def _remember_reply(self, key: str, text: str) -> None:
        self._replies[key] = text
        while len(self._replies) > REMEMBERED_REPLIES:
            self._replies.popitem(last=False)
