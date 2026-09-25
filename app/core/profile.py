"""A living profile of the user: a portrait plus how well Miki knows each area of their life.

Facts alone aren't understanding. The synthesizer reads every memory and writes (a) a short portrait
of who the person is and (b) per-domain confidence, gaps and the best next questions to ask. The
portrait rides along in every prompt; the gaps drive the get-to-know-you interview.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from app.memory.memory import Memory

logger = logging.getLogger(__name__)

# (key, title, what it covers) -- the map of a person Miki tries to fill in.
DOMAINS: list[tuple[str, str, str]] = [
    ("identity", "Identity", "name, age/birthday, origin, languages, personality"),
    ("home", "Home & location", "where they live, living situation, hometown, places that matter"),
    ("people", "Family & relationships", "family, partner, close friends, who matters and why"),
    ("work", "Work & study", "studies, job, career direction, skills, projects"),
    ("routine", "Daily routine", "wake/sleep times, work hours, weekly rhythm, energy patterns"),
    ("health", "Health & fitness", "exercise, sports, sleep, diet, allergies, wellbeing goals"),
    ("food", "Food & drink", "favourite foods and drinks, cooking, dietary preferences, dislikes"),
    ("interests", "Interests & hobbies", "sports, music, games, media, things they geek out about"),
    ("style", "Style & shopping", "clothing sizes, brands, colours, budget attitude, taste"),
    ("goals", "Goals & values", "short- and long-term goals, what matters most, what motivates them"),
    ("social", "Social life & travel", "how they spend time with others, trips, places to visit"),
    ("tech", "Tech & tools", "devices, apps, tools they rely on, how they like to work"),
    ("communication", "How to talk to them", "tone, humour, level of detail, what annoys them in an assistant"),
]
DOMAIN_KEYS = [key for key, _, _ in DOMAINS]

# Used when there is no profile yet (or the model call fails), so the interview always has something good to ask.
FALLBACK_QUESTIONS = [
    "What should I call you, and where are you based?",
    "What do you do right now: studies, work, or both? What do you want it to lead to?",
    "What does a normal weekday look like for you, from waking up to going to bed?",
    "Which sports or activities do you do regularly, and how often?",
    "Who are the most important people in your life? Tell me a bit about them.",
    "What do you love to eat and drink, and is there anything you avoid?",
    "What are you working toward in the next year?",
    "How do you like me to talk to you: short and direct, or more detailed and chatty?",
    "What are your clothing and shoe sizes, and which brands or styles do you like?",
    "What do you do to switch off and have fun?",
]


class BrainProtocol(Protocol):
    def generate_response(self, user_input: str, system_prompt: str, history: list[dict[str, str]] | None = None) -> str:
        ...


@dataclass
class DomainProfile:
    key: str
    title: str
    summary: str = ""
    confidence: float = 0.0
    gaps: list[str] = field(default_factory=list)


@dataclass
class Profile:
    portrait: str = ""
    domains: list[DomainProfile] = field(default_factory=list)
    next_questions: list[str] = field(default_factory=list)
    generated_at: str = ""
    memory_count: int = 0

    @property
    def coverage(self) -> float:
        """0..1: how completely Miki knows this person, averaged over all areas of life."""
        if not self.domains:
            return 0.0
        return sum(d.confidence for d in self.domains) / len(self.domains)

    def weakest(self, limit: int = 3) -> list[DomainProfile]:
        return sorted(self.domains, key=lambda d: d.confidence)[:limit]

    def to_dict(self) -> dict[str, Any]:
        return {
            "portrait": self.portrait,
            "domains": [
                {"key": d.key, "title": d.title, "summary": d.summary, "confidence": d.confidence, "gaps": d.gaps}
                for d in self.domains
            ],
            "next_questions": self.next_questions,
            "generated_at": self.generated_at,
            "memory_count": self.memory_count,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Profile":
        titles = {key: title for key, title, _ in DOMAINS}
        domains = [
            DomainProfile(
                key=str(item.get("key", "")),
                title=str(item.get("title") or titles.get(str(item.get("key", "")), "")),
                summary=str(item.get("summary", "")),
                confidence=_clamp(item.get("confidence", 0.0)),
                gaps=[str(g) for g in item.get("gaps", []) if str(g).strip()],
            )
            for item in payload.get("domains", [])
            if isinstance(item, dict)
        ]
        return cls(
            portrait=str(payload.get("portrait", "")),
            domains=domains,
            next_questions=[str(q) for q in payload.get("next_questions", []) if str(q).strip()],
            generated_at=str(payload.get("generated_at", "")),
            memory_count=int(payload.get("memory_count", 0) or 0),
        )


class ProfileStore:
    def __init__(self, path: str | Path = "data/profile.json") -> None:
        self.path = Path(path)

    def load(self) -> Profile | None:
        try:
            return Profile.from_dict(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None

    def save(self, profile: Profile) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(profile.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            logger.warning("Could not save profile", exc_info=True)


_SYSTEM_PROMPT = """You are building a living profile of ONE person for their personal AI assistant, Miki, from \
a list of facts Miki has learned about them. Understand them as a person: connect the facts, notice what they \
value, how they spend their time, and who matters to them.

Rules:
- Use ONLY the facts given. Never invent details. If little is known, say little.
- "portrait": 3-5 warm, specific sentences in the third person describing who this person is (not a list of facts).
- For every domain, "summary" is one sentence of what is known ("" if nothing), "confidence" is 0.0-1.0 for how \
completely the area is known (0 = nothing known, 0.5 = the basics, 1 = thoroughly known), and "gaps" are up to \
3 short things still unknown that would matter most.
- "next_questions": up to 6 friendly, conversational questions to ask them next, most valuable first. One question \
each. Never ask about something already known. Where useful, refer to what you know ("You play basketball, how \
often do you play?"). Prioritise the domains with the lowest confidence.

Respond ONLY with valid JSON (no markdown):
{"portrait": "...", "domains": {"<domain key>": {"summary": "...", "confidence": 0.0, "gaps": ["..."]}}, \
"next_questions": ["..."]}"""


class ProfileSynthesizer:
    def __init__(self, brain: BrainProtocol) -> None:
        self.brain = brain

    def synthesize(self, memories: list[Memory]) -> Profile:
        active = [m for m in memories if m.active and m.content.strip()]
        now = datetime.now().isoformat(timespec="seconds")
        if not active:
            return Profile(
                portrait="",
                domains=[DomainProfile(key=k, title=t) for k, t, _ in DOMAINS],
                next_questions=list(FALLBACK_QUESTIONS[:6]),
                generated_at=now,
                memory_count=0,
            )

        facts = "\n".join(f"- [{m.category}] {m.content.strip()}" for m in active)
        domain_lines = "\n".join(f'- "{key}": {title} ({about})' for key, title, about in DOMAINS)
        prompt = f"Domains to assess:\n{domain_lines}\n\nFacts known about the person:\n{facts}\n\nWrite the profile JSON now."
        try:
            response = self.brain.generate_response(user_input=prompt, system_prompt=_SYSTEM_PROMPT, history=None)
            payload = _parse_json(response)
        except Exception:
            logger.exception("Profile synthesis failed")
            raise

        raw_domains = payload.get("domains") if isinstance(payload.get("domains"), dict) else {}
        domains = []
        for key, title, _ in DOMAINS:
            item = raw_domains.get(key) if isinstance(raw_domains.get(key), dict) else {}
            domains.append(
                DomainProfile(
                    key=key,
                    title=title,
                    summary=str(item.get("summary", "")).strip(),
                    confidence=_clamp(item.get("confidence", 0.0)),
                    gaps=[str(g).strip() for g in item.get("gaps", []) if str(g).strip()][:3],
                )
            )
        questions = [str(q).strip() for q in payload.get("next_questions", []) if str(q).strip()][:6]
        return Profile(
            portrait=str(payload.get("portrait", "")).strip(),
            domains=domains,
            next_questions=questions or list(FALLBACK_QUESTIONS[:6]),
            generated_at=now,
            memory_count=len(active),
        )


def _clamp(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _parse_json(text: str) -> dict:
    cleaned = (text or "").strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return json.loads(cleaned.strip())
