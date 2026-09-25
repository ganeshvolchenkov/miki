from __future__ import annotations

import html
import logging
import json
import os
import re
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any
from datetime import datetime

import webview

from app.core.bootstrap import build_runtime
from app.core.model_choice import MODEL_CHOICES, load_saved_model, save_model_preference, switch_model
from app.core.timeutil import local_utc_offset
from app.core.interview import Interview
from app.core.mail_attention import MailAttention, MailUnavailable
from app.phone.backend import PhoneHooks
from app.phone.service import PhoneService, build_phone_service
from app.core.mail_triage import MailTriage, gmail_url
from app.interfaces import memory_commands as mc
from app.memory.brain import layout_graph
from app.interfaces.face import _FACE_SEQUENCES, _FACE_STATE_BOUNCE, _SUPER_IDLE_SEQUENCE, _compose_face_frame, _random_speaking_frame

logger = logging.getLogger(__name__)

# Face animation is slowed by this factor (frame durations are multiplied by it).
FACE_TIME_SCALE = 2.4

# Face palette for the futuristic UI (the original amber palette lives in gui.py for the Tk app).
FACE_BODY_COLOR = "#1c1b17"
FACE_HIGHLIGHT_COLOR = "#33312a"
FACE_EYE_COLORS = {
    "idle": "#ff5b2e",
    "listening": "#ffd2bf",
    "thinking": "#f5b83d",
    "speaking": "#ff5b2e",
}


class WebApi:
    def __init__(self, core, brain=None):
        self._core = core
        self._brain = brain
        self._window = None
        self._busy = False
        self._face_state = "idle"
        self._face_wake = threading.Event()
        manager = getattr(core, "memory_manager", None)
        self._interview = Interview(manager) if manager is not None else None
        self._profile_refreshing = False
        self._triage: MailTriage | None = None
        self._core_lock = threading.RLock()  # the core handles one conversation turn at a time (dashboard or phone)
        self._phone: PhoneService | None = None
        self._mail = MailAttention(lambda name: self._core.get_tool(name), lambda: self._get_triage())

    def start_loops(self):
        threading.Thread(target=self._face_loop, daemon=True).start()
        threading.Thread(target=self._update_widgets_loop, daemon=True).start()
        threading.Thread(target=self._startup_maintenance, daemon=True).start()
        self._push_model()
        self._start_phone()

    # ---- JS helpers -------------------------------------------------
    def _js(self, code: str) -> None:
        if not self._window:
            return
        try:
            self._window.evaluate_js(code)
        except Exception:
            logger.debug("evaluate_js failed", exc_info=True)

    def _system(self, text: str) -> None:
        self._js(f"appendMessage('system', {json.dumps(text)})")

    def _push_model(self) -> None:
        if self._brain is not None:
            self._js(f"setModel({json.dumps(self._brain.model)})")

    # ---- chat entry point (called from JS) --------------------------
    def process_message(self, text: str) -> None:
        text = (text or "").strip()
        if not text or self._busy:
            return
        self._js(f"appendMessage('user', {json.dumps(text)})")
        if text.startswith("/"):
            self._handle_command(text)
            return
        if self._interview is not None and self._interview.active:
            self._run_background(self._interview_turn, text)
            return
        self._set_busy(True)
        threading.Thread(target=self._run_core, args=(text,), daemon=True).start()

    @property
    def _mail_items(self):
        return self._mail.items

    @_mail_items.setter
    def _mail_items(self, value) -> None:
        self._mail.items = value

    @property
    def _mail_account(self) -> str:
        return self._mail.account

    # ---- phone -------------------------------------------------------------------------
    def phone_hooks(self) -> PhoneHooks:
        """Callbacks that let the phone service show up in the dashboard (messages, face, header pill)."""
        return PhoneHooks(
            on_message=self._phone_message,
            on_busy=self._phone_busy,
            on_speak=lambda text: self._speak_for(min(6.0, max(1.5, len(text) * 0.03))),
            on_state=self._push_phone_state,
        )

    def _phone_message(self, role: str, text: str) -> None:
        self._js(f"appendPhoneMessage({json.dumps(role)}, {json.dumps(text)})")

    def _phone_busy(self, busy: bool) -> None:
        if self._busy:  # the dashboard is already animating its own turn
            return
        self._face_state = "thinking" if busy else "idle"
        self._face_wake.set()
        self._js(f"setVoiceState({json.dumps('thinking' if busy else 'idle')})")

    def _start_phone(self) -> None:
        if self._phone is None:
            return
        try:
            self._phone.start()
        except Exception:
            logger.exception("Phone service failed to start")
        self._push_phone_state()

    def _push_phone_state(self) -> None:
        if self._phone is None:
            return
        status = self._phone.status()
        if status.error:
            self._js(f"setPill('phone', false, {json.dumps('Phone: ' + status.error[:40])})")
        elif not status.owns_bot:
            self._js("setPill('phone', false, 'Phone: other Miki')")
        elif status.paired:
            self._js(f"setPill('phone', true, {json.dumps('Phone: ' + (status.owner or 'linked'))})")
        else:
            self._js("setPill('phone', false, 'Phone: not linked')")

    def _command_phone(self, argument: str) -> None:
        phone = self._phone
        if phone is None:
            self._system(
                "Phone isn't set up yet.\n\n"
                "1. In Telegram, open @BotFather and send /newbot. Pick a name, then a username ending in 'bot'.\n"
                "2. Copy the token it gives you into your .env file as TELEGRAM_BOT_TOKEN=...\n"
                "3. Restart Miki, then type /phone again.\n\n"
                "The bot only ever talks to the account you link. Nobody else gets an answer."
            )
            return
        status = phone.status()
        action = argument.lower().strip()
        if status.error:
            self._system(f"Phone problem: {status.error}")
            return
        if not status.owns_bot:
            self._system("The phone bot is running in another Miki process (the headless one), so it can't be managed here.")
            return
        if action == "unlink":
            phone.unlink()
            self._push_phone_state()
            self._system("Phone unlinked. The bot now ignores everyone.")
        elif action == "test":
            self._system("Sent a test message to your phone." if phone.send_test() else "Couldn't send a test (is a phone linked, and is it quiet hours?).")
        elif status.paired and action != "new":
            self._system(
                f"Linked to {status.owner or 'your phone'} on @{status.username}.\n"
                "  /phone test     send a test message\n"
                "  /phone unlink   disconnect it\n"
                "Urgent emails are pushed to your phone (quiet hours 23:00-08:00 by default)."
            )
        else:
            code, link = phone.new_pairing()
            self._system(
                f"To link your phone (valid 10 minutes):\n"
                f"  1. Open Telegram and find @{status.username}\n"
                f"  2. Send:  /pair {code}\n"
                + (f"  or just tap: {link}\n" if link else "")
                + "Only the account that sends this code will be able to use Miki."
            )

    # ---- mail (called from the dashboard) ------------------------------------------
    def open_mail(self, thread_id: str) -> bool:
        """Open a conversation in Gmail. The id is validated and the URL built here, never taken from the page."""
        if not re.fullmatch(r"[0-9a-fA-F]{6,32}", str(thread_id or "")):
            return False
        return bool(webbrowser.open(gmail_url(str(thread_id), self._mail_account)))

    def dismiss_mail(self, message_id: str) -> None:
        """Hide an item in Miki only; the mailbox itself is never modified."""
        if re.fullmatch(r"[0-9a-fA-F]{6,32}", str(message_id or "")):
            self._mail.dismiss(str(message_id))
            self._js(f"setMailCount({len(self._mail_items)})")

    def _get_triage(self) -> MailTriage:
        if self._triage is None:
            manager = getattr(self._core, "memory_manager", None)
            use_ai = os.getenv("MIKI_MAIL_TRIAGE", "1").strip().lower() not in {"0", "false", "no"}
            self._triage = MailTriage(
                getattr(self._core, "brain", None) if use_ai else None,
                context=manager.mail_context if manager is not None else None,
            )
        return self._triage

    # ---- assistant-style output --------------------------------------------
    def _say(self, text: str) -> None:
        """Show ``text`` as one of Miki's own chat messages (not a system line)."""
        bubble_id = str(datetime.now().timestamp())
        self._js(f"createAssistantBubble({json.dumps(bubble_id)})")
        self._js(f"updateAssistantBubble({json.dumps(bubble_id)}, {json.dumps(text)})")

    def _toast(self, memories) -> None:
        if memories:
            self._js(f"appendMemoryToast({json.dumps(mc.toast_payload(memories))})")

    # ---- interview ------------------------------------------------------------
    def _interview_turn(self, text: str) -> None:
        turn = self._interview.respond(text)
        self._say(turn.text)
        self._toast(turn.memories)

    def _start_interview(self) -> None:
        self._say(self._interview.start().text)

    # ---- slash commands ---------------------------------------------
    def _handle_command(self, text: str) -> None:
        command, _, argument = text[1:].partition(" ")
        command, argument = command.lower(), argument.strip()
        manager = getattr(self._core, "memory_manager", None)
        if command == "model":
            self._command_model(argument)
        elif command == "memory" and manager is not None:
            self._system(mc.memory_overview(manager, argument))
        elif command == "remember" and manager is not None:
            self._run_background(self._remember, argument)
        elif command == "profile" and manager is not None:
            if argument.lower() == "refresh":
                self._run_background(self._refresh_profile)
            else:
                self._system(mc.profile_text(manager.get_profile()))
        elif command == "interview" and self._interview is not None:
            if argument.lower() in {"stop", "end", "quit"}:
                self._say(self._interview.stop().text)
            else:
                self._run_background(self._start_interview)
        elif command == "mail":
            self._run_background(self._mail_command)
        elif command == "phone":
            self._command_phone(argument)
        elif command == "forget" and manager is not None:
            self._system(mc.forget(self._core, argument))
            self._update_widgets()
        elif command == "brain" and manager is not None:
            if argument.lower() == "rebuild":
                self._run_background(self._rebuild_brain)
            elif argument.lower() == "learn":
                self._run_background(self._learn)
            elif argument.lower() == "sleep":
                self._run_background(self._consolidate)
            elif argument.lower() == "open":
                self._system(mc.open_vault(manager))
            else:
                self._system(mc.brain_status(manager))
        elif command in {"help", "commands", "?"}:
            self._system(
                "Commands\n"
                "/remember <fact>    store something on purpose\n"
                "/memory [word]      what I remember (or search it)\n"
                "/forget <word|id>   delete a memory\n"
                "/mail               the emails that need your attention, and why\n"
                "/phone              link Miki to your phone (Telegram)\n"
                "/profile [refresh]  who I think you are, and what I don't know yet\n"
                "/interview          I ask you questions to get to know you (skip / stop anytime)\n"
                "/brain              how connected my memory is\n"
                "/brain learn        learn from your earlier chats\n"
                "/brain sleep        refresh the profile and write today's journal\n"
                "/brain rebuild      re-link everything (backs up your vault first)\n"
                "/brain open         open the Obsidian vault\n"
                "/model [name]       pick the OpenAI model (cheaper models cost less)\n"
                "/help               show this list"
            )
        else:
            self._system(f"Unknown command /{command}. Type /help.")

    def _run_background(self, target, *args) -> None:
        """Run a slow command off the UI thread with the busy indicator on."""
        def work() -> None:
            try:
                target(*args)
            except Exception as exc:
                logger.exception("Command failed")
                self._system(f"Error: {exc}")
            finally:
                self._set_busy(False)
                self._update_widgets()

        self._set_busy(True)
        threading.Thread(target=work, daemon=True).start()

    def _remember(self, text: str) -> None:
        if not text:
            self._system("Usage: /remember <something about you>")
            return
        memories = self._core.remember(text)
        if memories:
            self._js(f"appendMemoryToast({json.dumps(mc.toast_payload(memories))})")
        else:
            self._system("I couldn't turn that into a memory.")

    def _mail_command(self) -> None:
        self._widget_mail(force=True)
        text = self._mail.summary()
        self._system(text + ("\n\nClick one in the MAIL panel to open it in Gmail." if self._mail_items else ""))

    def _refresh_profile(self) -> None:
        self._system("Re-reading everything I know about you…")
        self._system(mc.profile_text(self._core.memory_manager.refresh_profile()))

    def _learn(self) -> None:
        manager = self._core.memory_manager
        pending = manager.count_unlearned_messages()
        if not pending:
            self._system(mc.learn_summary({"messages_read": 0}))
            return
        self._system(f"Reading {pending} things you said in earlier chats… this takes a minute.")
        self._system(mc.learn_summary(manager.learn_from_conversations()))

    def _consolidate(self) -> None:
        self._system("Sleeping on it: refreshing my picture of you and writing the journal…")
        self._system(mc.consolidate_summary(self._core.memory_manager.consolidate()))

    def _maybe_refresh_profile_async(self) -> None:
        """After a few new memories, quietly refresh the portrait (at most one refresh at a time)."""
        manager = getattr(self._core, "memory_manager", None)
        if manager is None or self._profile_refreshing or not manager.profile_is_stale():
            return
        self._profile_refreshing = True

        def work() -> None:
            try:
                manager.maybe_refresh_profile()
                self._widget_brain()
            finally:
                self._profile_refreshing = False

        threading.Thread(target=work, daemon=True).start()

    def _rebuild_brain(self) -> None:
        self._system("Rebuilding my brain… (your vault is backed up first)")
        self._system(mc.describe_rebuild(self._core.memory_manager.rebuild_brain()))

    def _startup_maintenance(self) -> None:
        """Runs once after the window loads: migrate older memories into the graph, refresh the
        search index, then draw the brain. Failures never block the app."""
        manager = getattr(self._core, "memory_manager", None)
        try:
            if manager is not None and manager.needs_graph_migration():
                self._system("Organising your older memories into a connected brain… (vault backed up first)")
                self._system(mc.describe_rebuild(manager.rebuild_brain()))
        except Exception:
            logger.exception("Brain migration failed")
        try:
            rebuild = getattr(getattr(manager, "memory_store", None), "rebuild_graph", None)
            if callable(rebuild):
                rebuild()  # idempotent and free (no model calls): heals renamed/edited notes and stale links
        except Exception:
            logger.exception("Vault graph refresh failed")
        try:
            if manager is not None:
                manager.catch_up()
        except Exception:
            logger.exception("Startup catch-up failed")
        try:
            if manager is not None and manager.count_unlearned_messages() >= 5:
                self._system(
                    f"I can learn from {manager.count_unlearned_messages()} things you told me in earlier chats. "
                    "Type /brain learn, or /interview to let me ask you questions."
                )
        except Exception:
            logger.debug("Could not count unlearned messages", exc_info=True)
        try:
            rag = getattr(self._core, "rag_service", None)
            if rag is not None:
                rag.rebuild()
        except Exception:
            logger.exception("Search index refresh failed")
        self._widget_brain()

    def _command_model(self, argument: str) -> None:
        if self._brain is None:
            self._system("Model switching is unavailable.")
            return
        if not argument:
            models = [{"id": m, "tier": tier, "note": note} for m, tier, note in MODEL_CHOICES]
            self._js(f"appendModelPicker({json.dumps(self._brain.model)}, {json.dumps(models)})")
            return
        if argument.isdigit() and 1 <= int(argument) <= len(MODEL_CHOICES):
            argument = MODEL_CHOICES[int(argument) - 1][0]
        self._set_busy(True)
        threading.Thread(target=self._switch_model, args=(argument,), daemon=True).start()

    def _switch_model(self, name: str) -> None:
        try:
            problem = switch_model(self._brain, name)
            if problem:
                self._system(problem)
                return
            self._push_model()
            self._system(f"Model switched to {name}.")
        finally:
            self._set_busy(False)

    # ---- normal chat ------------------------------------------------
    def _run_core(self, text: str):
        speaking_seconds = 0.0
        try:
            bubble_id = str(datetime.now().timestamp())
            self._js(f"createAssistantBubble({json.dumps(bubble_id)})")
            with self._core_lock:
                response, _memory_created = self._core.process_user_input(text)
            self._js(f"updateAssistantBubble({json.dumps(bubble_id)}, {json.dumps(response)})")
            saved = list(getattr(self._core, "last_memories", None) or [])
            if saved:
                self._toast(saved)
                self._maybe_refresh_profile_async()
            speaking_seconds = min(6.0, max(1.5, len(response) * 0.03))
        except Exception as exc:
            logger.exception("Miki failed to process message")
            self._js(f"removeTyping(); appendMessage('system', {json.dumps('Error: ' + str(exc))})")
        finally:
            self._set_busy(False)
            if speaking_seconds:
                self._speak_for(speaking_seconds)
            self._update_widgets()

    def _speak_for(self, seconds: float) -> None:
        self._face_state = "speaking"
        self._face_wake.set()
        self._js("setVoiceState('speaking')")

        def stop() -> None:
            if not self._busy:
                self._face_state = "idle"
                self._face_wake.set()
                self._js("setVoiceState('idle')")

        threading.Timer(seconds, stop).start()

    def _set_busy(self, busy: bool):
        self._busy = busy
        self._face_state = "thinking" if busy else "idle"
        self._face_wake.set()
        self._js(f"setBusy({json.dumps(busy)})")

    # ---- face -------------------------------------------------------
    def _face_loop(self):
        """Drives the mascot's frames. Event-driven: sleeps until the next frame is actually due (a
        blink is seconds apart) and is woken immediately when the state changes, instead of waking
        ~16 times a second forever."""
        import time

        frame_idx = 0
        frame_elapsed_ms = 0.0
        idle_elapsed_ms = 0.0
        playing_super_idle = False
        current_frame = _FACE_SEQUENCES["idle"][0]
        last_sent: tuple[Any, str] | None = None
        last_state = "idle"
        last_tick = time.monotonic()

        while True:
            if last_sent is not None:  # the very first frame is drawn immediately
                due_ms = current_frame[3] * FACE_TIME_SCALE - frame_elapsed_ms
                if last_state == "idle" and not playing_super_idle:
                    due_ms = min(due_ms, 14000 - idle_elapsed_ms)
                self._face_wake.wait(max(0.03, due_ms / 1000))
                self._face_wake.clear()

            now = time.monotonic()
            dt_ms = (now - last_tick) * 1000
            last_tick = now
            state = self._face_state

            if state != last_state:  # react to state changes at once
                last_state = state
                frame_idx = 0
                frame_elapsed_ms = 0.0
                playing_super_idle = False
                idle_elapsed_ms = 0.0
                current_frame = _random_speaking_frame() if state == "speaking" else _FACE_SEQUENCES.get(state, _FACE_SEQUENCES["idle"])[0]
            else:
                frame_elapsed_ms += dt_ms
                if state == "idle" and not playing_super_idle:
                    idle_elapsed_ms += dt_ms
                    if idle_elapsed_ms >= 14000:
                        playing_super_idle = True
                        frame_idx = 0
                        frame_elapsed_ms = 0.0
                        current_frame = _SUPER_IDLE_SEQUENCE[0]

                if frame_elapsed_ms >= current_frame[3] * FACE_TIME_SCALE:
                    frame_elapsed_ms = 0.0
                    if state == "speaking":
                        current_frame = _random_speaking_frame()
                    else:
                        seq = _SUPER_IDLE_SEQUENCE if playing_super_idle else _FACE_SEQUENCES.get(state, _FACE_SEQUENCES["idle"])
                        frame_idx += 1
                        if frame_idx >= len(seq):
                            frame_idx = 0
                            if playing_super_idle:
                                playing_super_idle = False
                                idle_elapsed_ms = 0.0
                                seq = _FACE_SEQUENCES.get(state, _FACE_SEQUENCES["idle"])
                        current_frame = seq[frame_idx]

            # Only push to the page when something actually changed; the bob is animated in JS.
            if last_sent is not None and last_sent[0] is current_frame and last_sent[1] == state:
                continue
            last_sent = (current_frame, state)

            eye_l, eye_r, mouth, _ = current_frame
            grid = _compose_face_frame(eye_l, eye_r, mouth)
            amplitude, speed = _FACE_STATE_BOUNCE.get(state, (0.06, 2.0))
            eye_color = FACE_EYE_COLORS.get(state, FACE_EYE_COLORS["idle"])
            colors = {"B": FACE_BODY_COLOR, "H": FACE_HIGHLIGHT_COLOR, "E": eye_color, "M": eye_color, ".": "transparent"}
            colored_grid = [[colors.get(c, "transparent") for c in row] for row in grid]
            self._js(f"renderFace({json.dumps(colored_grid)}, {amplitude}, {speed}, {json.dumps(eye_color)})")

    def _update_widgets_loop(self):
        import time
        while True:
            self._update_widgets()
            time.sleep(300)  # also refreshed after every chat message

    def _set_widget(self, element_id: str, content: str) -> None:
        self._js(f"document.getElementById({json.dumps(element_id)}).innerHTML = {json.dumps(content)};")

    def _set_pill(self, name: str, ok: bool, text: str) -> None:
        self._js(f"setPill({json.dumps(name)}, {json.dumps(ok)}, {json.dumps(text)})")

    @staticmethod
    def _rows(items: list[tuple[str, str]]) -> str:
        return "".join(
            f"<div class='row'><div class='row-t'>{html.escape(title)}</div><div class='row-s'>{html.escape(sub)}</div></div>"
            for title, sub in items
        )

    @staticmethod
    def _note(text: str) -> str:
        return f"<div class='note'>{html.escape(text)}</div>"

    def _update_widgets(self):
        # Each widget runs in its own thread so one slow/failing service can't leave the rest on "Loading...".
        for name in ("memory", "weather", "calendar", "mail", "drive", "maps", "brain"):
            threading.Thread(target=self._refresh_widget, args=(name,), daemon=True).start()

    def _refresh_widget(self, name: str) -> None:
        try:
            getattr(self, f"_widget_{name}")()
        except Exception as exc:
            logger.warning("Widget %s failed: %s", name, exc)
            if name == "memory":
                return
            message = "Sign in to Google required" if "authentication is required" in str(exc) else "Unavailable"
            self._set_widget(f"{name}-content", self._note(message))
            if name == "calendar":
                self._set_pill("calendar", False, "Calendar offline")

    def _widget_memory(self) -> None:
        count = len(self._core.list_memories(active_only=True))
        self._set_pill("memory", True, f"{count} memories")

    def _widget_weather(self) -> None:
        tool = self._core.get_tool("weather")
        res = tool.execute("get_current_weather", {"location": "Amsterdam"}) if tool else None
        if res is None or not res.success:
            self._set_widget("weather-content", self._note("Unavailable"))
            return
        data = res.data or {}
        current = data.get("current") or {}
        self._set_widget(
            "weather-content",
            f"<div class='wx'><span class='wx-temp'>{html.escape(str(current.get('temperature')))}°</span>"
            f"<span class='wx-sym'>{html.escape(current.get('condition_symbol') or '')}</span></div>"
            f"<div class='row-s'>{html.escape(current.get('condition') or '')} · {html.escape(str(data.get('location') or 'Amsterdam'))}</div>",
        )

    def _widget_calendar(self) -> None:
        tool = self._core.get_tool("calendar")
        if tool is None or not tool.is_available():
            self._set_pill("calendar", False, "Calendar offline")
            self._set_widget("calendar-content", self._note("Not connected"))
            return
        self._set_pill("calendar", True, "Calendar live")
        today = datetime.now()
        offset = local_utc_offset()
        res = tool.execute("get_events", {
            "start": today.strftime("%Y-%m-%dT00:00:00") + offset,
            "end": today.strftime("%Y-%m-%dT23:59:59") + offset,
        })
        events = (res.data or {}).get("events", []) if res.success else []
        if not events:
            self._set_widget("calendar-content", self._note("Nothing scheduled today"))
            return
        self._set_widget("calendar-content", self._rows([
            (str(e.get("title", ""))[:28], "All day" if e.get("all_day") else str(e.get("start", ""))[11:16])
            for e in events[:4]
        ]))

    def _widget_mail(self, force: bool = False) -> None:
        """Mail that needs attention (not just the latest unread): triaged, cached, click-to-open."""
        try:
            items = self._mail.refresh(force)
        except MailUnavailable as exc:
            self._set_widget("mail-content", self._note("Not connected" if "connected" in str(exc) else "Unavailable"))
            return
        if items is None:
            return  # checked recently; chat messages trigger refreshes too, and Gmail needn't be hit each time
        self._js(f"setMailCount({len(items)})")
        self._set_widget("mail-content", self._mail_rows(items[:6], hidden=max(0, len(items) - 6)))

    @staticmethod
    def _mail_rows(items, *, hidden: int = 0) -> str:
        if not items:
            return WebApi._note("All clear. Nothing needs you.")
        rows = []
        for item in items:
            detail = item.reason or item.snippet[:70]
            if item.due:
                detail += f" · due {item.due}"
            rows.append(
                f"<div class='mail-row {html.escape(item.priority)}' data-thread='{html.escape(item.thread_id, quote=True)}' "
                f"data-id='{html.escape(item.id, quote=True)}' title='Open in Gmail'>"
                "<span class='mdot'></span><div class='mbody'>"
                f"<div class='row-t'>{html.escape(item.sender_name[:22])} · {html.escape(item.subject[:44])}</div>"
                f"<div class='row-s'>{html.escape(detail[:78])}</div></div>"
                "<button class='mx' title='Dismiss (Miki only)'>×</button></div>"
            )
        if hidden:
            rows.append(f"<div class='note more'>+{hidden} more: type /mail</div>")
        return "".join(rows)

    def _widget_drive(self) -> None:
        tool = self._core.get_tool("drive")
        if tool is None:
            self._set_widget("drive-content", self._note("Not connected"))
            return
        res = tool.execute("search_files", {"max_results": 3})
        files = (res.data or {}).get("files") if res.success else None
        if not files:
            self._set_widget("drive-content", self._note("No recent files"))
            return
        self._set_widget("drive-content", self._rows([(str(f.get("name", ""))[:32], "") for f in files]))

    def _widget_brain(self) -> None:
        manager = getattr(self._core, "memory_manager", None)
        if manager is None:
            return
        snapshot = manager.graph_snapshot()
        stats = snapshot.pop("stats")
        graph = layout_graph(snapshot)
        self._js(f"setBrainGraph({json.dumps(graph, separators=(',', ':'))}, {json.dumps(stats)})")

    def _widget_maps(self) -> None:
        self._set_widget("maps-content", self._note("Ask for directions anywhere"))



# Chromium switches that keep the embedded browser lean. Measured on this app: software rendering
# (--disable-gpu) drops the WebView2 GPU process from ~200 MB to ~30 MB private memory, and the
# page is static at idle so it costs almost no CPU. Set MIKI_GPU=1 to use the GPU anyway, or set
# WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS yourself to override everything.
_WEBVIEW2_LEAN_ARGS = (
    "--renderer-process-limit=1 "
    "--disable-features=Translate,MediaRouter,OptimizationHints,msSmartScreenProtection "
    "--disable-background-networking --disable-component-update --disable-sync "
    '--js-flags="--lite-mode --max-old-space-size=64"'
)


def configure_webview2() -> None:
    if "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS" in os.environ:
        return
    use_gpu = os.getenv("MIKI_GPU", "").strip().lower() in {"1", "true", "yes"}
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = ("" if use_gpu else "--disable-gpu ") + _WEBVIEW2_LEAN_ARGS


def _webview_profile_dir() -> Path:
    """A fixed browser profile (instead of a fresh temp dir every launch), so Google Fonts and other
    cached assets survive restarts and nothing accumulates in %TEMP%."""
    path = Path("data/webview").resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_local_server():
    import http.server
    import socketserver
    import os
    
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')
    
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=web_dir, **kwargs)
            
        def log_message(self, format, *args):
            pass
            
    class ThreadingHTTPServer(socketserver.TCPServer):
        allow_reuse_address = True

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    return httpd, port

def run_gui() -> int:
    configure_webview2()
    httpd, port = create_local_server()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    runtime = build_runtime(saved_model=load_saved_model())
    core, brain = runtime.core, runtime.brain

    api = WebApi(core, brain)
    api._phone = build_phone_service(
        core, runtime.settings, openai_client=runtime.openai_client, lock=api._core_lock, hooks=api.phone_hooks()
    )
    window = webview.create_window(
        'Miki Command Center',
        url=f'http://127.0.0.1:{port}/index.html',
        width=1440,
        height=920,
        min_size=(1180, 760),
        background_color='#050914',
        js_api=api
    )
    api._window = window
    window.events.loaded += api.start_loops
    
    webview.start(private_mode=False, storage_path=str(_webview_profile_dir()))
    # Window closed: stop serving and let the process end (daemon threads die with it).
    try:
        httpd.shutdown()
        httpd.server_close()
    except Exception:
        logger.debug("http server shutdown failed", exc_info=True)
    return 0

