from __future__ import annotations

import re
from typing import Protocol

_GREETING_WORDS = {"hi", "hello", "hey", "yo", "sup", "hiya", "howdy", "greetings"}

_SMALL_TALK_PHRASES = {
    "how are you",
    "how are you doing",
    "how's it going",
    "hows it going",
    "what's up",
    "whats up",
    "good morning",
    "good afternoon",
    "good evening",
    "good night",
    "thanks",
    "thank you",
    "ok",
    "okay",
    "cool",
    "nice",
    "bye",
    "goodbye",
    "see you",
}

_MATH_QUESTION_RE = re.compile(
    r"^(what\s+is|what's|whats|calculate|compute|solve)\s+[\d\s.+\-*/x×÷()=]+\??$",
    re.IGNORECASE,
)
_MATH_ONLY_RE = re.compile(r"^[\d\s.+\-*/x×÷()=]+\??$")

# Explicit signals that the user is referring back to something Miki should
# already know about them.
_RETRIEVAL_SIGNAL_RE = re.compile(
    r"\b("
    r"remember|recall|"
    r"i (?:told|mentioned|said)|(?:you )?told you|said before|"
    r"what do you know|what did i|what were?\s|what was\b|"
    r"idea i had|my\s+\w+|"
    r"routine|goal|history|last time|earlier|before|previously"
    r")\b",
    re.IGNORECASE,
)


class RetrievalDecision(Protocol):
    def should_retrieve(self, query: str) -> bool:
        ...


class DeterministicRetrievalDecision:
    """Lightweight, rule-based gate for whether RAG retrieval should run.

    Deliberately not an LLM call (per v0.5 scope) -- this is a cheap
    heuristic meant to skip retrieval for greetings/small-talk/arithmetic and
    run it for messages that reference the user's own history, habits, goals,
    or past statements. Kept behind the ``RetrievalDecision`` protocol so it
    can be swapped for something smarter later without touching callers.
    """

    def should_retrieve(self, query: str) -> bool:
        text = (query or "").strip()
        if not text:
            return False

        stripped = text.strip(" !?.")
        lower = stripped.lower()

        if lower in _GREETING_WORDS or lower in _SMALL_TALK_PHRASES:
            return False

        words = lower.split()
        if words and words[0] in _GREETING_WORDS and len(words) <= 3:
            return False

        if _MATH_QUESTION_RE.match(text.strip()) or _MATH_ONLY_RE.match(text.strip()):
            return False

        if _RETRIEVAL_SIGNAL_RE.search(lower):
            return True

        word_count = len(text.split())
        if "?" in text and word_count >= 6:
            return True

        return False
