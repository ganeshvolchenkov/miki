"""Get-to-know-you interview.

Miki asks one good question at a time, chosen from what the profile says she doesn't know yet, stores
each answer (with the question as context, so a short "Amsterdam" still means something), and shows
progress. No chat-model call per turn -- only the answer extraction and an occasional profile refresh.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.core.profile import FALLBACK_QUESTIONS

logger = logging.getLogger(__name__)

_SKIP = {"skip", "pass", "next", "idk", "no idea", "dont know", "i dont know", "not sure", "rather not say"}
_STOP = {"stop", "done", "quit", "exit", "enough", "later", "end", "finish", "thats all", "that is all"}


@dataclass
class InterviewTurn:
    text: str
    memories: list[Any] = field(default_factory=list)
    finished: bool = False


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower().replace("'", "")).strip()


class Interview:
    def __init__(self, manager: Any, *, refresh_every: int = 3, max_questions: int = 15) -> None:
        self.manager = manager
        self.refresh_every = refresh_every
        self.max_questions = max_questions
        self.active = False
        self.current: str | None = None
        self.answered = 0
        self.asked: list[str] = []
        self._queue: list[str] = []

    # ------------------------------------------------------------------ flow
    def start(self) -> InterviewTurn:
        self.active = True
        self.answered = 0
        self.asked = []
        self._queue = self._fresh_questions(refresh=True)
        question = self._next_question()
        if question is None:
            return self._finish("I can't think of anything else to ask you right now. Tell me anything you want me to remember.")
        intro = (
            f"Let's get to know each other. Right now I know you about {self._coverage():.0%}. "
            "Answer as much or as little as you like: say 'skip' to pass a question, 'stop' to end.\n\n"
        )
        return InterviewTurn(intro + question)

    def respond(self, text: str) -> InterviewTurn:
        if not self.active or self.current is None:
            return InterviewTurn("There's no interview running. Type /interview to start one.", finished=True)

        cleaned = _normalise(text)
        if cleaned in _STOP:
            return self._finish("Thanks for telling me all that.")
        if cleaned in _SKIP:
            question = self._next_question()
            return InterviewTurn(question) if question else self._finish("That's all I wanted to ask for now.")

        memories: list[Any] = []
        try:
            memories = self.manager.remember(f'Asked "{self.current}", the user answered: "{text.strip()}"')
        except Exception:
            logger.exception("Could not store an interview answer")
        self.answered += 1

        if self.answered % self.refresh_every == 0:
            self._queue = self._fresh_questions(refresh=True)

        acknowledgement = (
            "Noted: " + "; ".join((m.title or m.content) for m in memories[:3]) + "." if memories else "Got it."
        )
        if self.answered >= self.max_questions:
            return self._finish(acknowledgement + " That's plenty for one sitting.", memories)
        question = self._next_question()
        if question is None:
            return self._finish(acknowledgement + " That's everything I wanted to ask for now.", memories)
        return InterviewTurn(f"{acknowledgement}\n\n{question}", memories)

    def stop(self) -> InterviewTurn:
        return self._finish("Okay, we can pick this up any time with /interview.")

    # ------------------------------------------------------------------ helpers
    def _finish(self, message: str, memories: list[Any] | None = None) -> InterviewTurn:
        self.active = False
        self.current = None
        summary = ""
        if self.answered:
            try:
                self.manager.refresh_profile()
            except Exception:
                logger.exception("Profile refresh after interview failed")
            summary = f"\n\nI now know you about {self._coverage():.0%}. See /profile for the full picture."
        return InterviewTurn(message + summary, memories or [], finished=True)

    def _fresh_questions(self, *, refresh: bool) -> list[str]:
        profile = None
        try:
            profile = self.manager.refresh_profile() if refresh else self.manager.get_profile()
        except Exception:
            logger.exception("Could not refresh the profile for the interview")
            profile = self.manager.get_profile()
        questions = list(profile.next_questions) if profile is not None else []
        return questions or list(FALLBACK_QUESTIONS)

    def _next_question(self) -> str | None:
        while self._queue:
            question = self._queue.pop(0)
            if question not in self.asked:
                self.asked.append(question)
                self.current = question
                return question
        self.current = None
        return None

    def _coverage(self) -> float:
        profile = self.manager.get_profile()
        return profile.coverage if profile is not None else 0.0
