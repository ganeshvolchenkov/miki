from __future__ import annotations

import difflib
import json
import logging
import re
from datetime import datetime
from typing import Any, Iterable, Protocol

from app.core.memory_candidate import MemoryCandidate, MemoryCandidateStore
from app.core.memory_comparator import MemoryComparator, MemoryComparison
from app.core.memory_decision_logger import MemoryDecisionLogger
from app.core.memory_evaluator import MemoryEvaluation, MemoryEvaluator
from app.core.memory_extractor import MemoryExtraction, MemoryExtractor
from app.core.journal import DailyJournal, yesterday_entry_missing
from app.core.memory_learner import ConversationLearner
from app.core.memory_recall import MemoryRecall
from app.core.profile import Profile, ProfileStore, ProfileSynthesizer
from app.core.memory_feedback import MemoryFeedbackDataset
from app.memory.brain import MemoryBrain, derive_entities, entity_key, is_junk_entity
from app.memory.memory import Memory, MemoryStore, utc_now

logger = logging.getLogger(__name__)


# Synonym mapping for improved duplicate detection
SYNONYM_MAP = {
    "like": {"enjoy", "love", "prefer"},
    "enjoy": {"like", "love", "prefer"},
    "love": {"like", "enjoy", "prefer"},
    "prefer": {"like", "enjoy", "love"},
    "hate": {"dislike", "don't like", "don't enjoy"},
    "dislike": {"hate", "don't like", "don't enjoy"},
    "cycling": {"bike", "biking"},
    "hiking": {"walking", "trek", "trekking"},
    "reading": {"books", "reading books"},
    "tea": {"tea drinking"},
    "coffee": {"caffeine"},
}


MEMORY_SIGNAL_PATTERNS = [
    r"\bmy name is\b",
    r"\bi am\b",
    r"\bi[' ]?m\b",
    r"\bi was born\b",
    r"\bi live in\b",
    r"\bi work at\b",
    r"\bi stud(?:y|ying|ies|ied)\b",
    r"\bi own\b",
    r"\bi have\b",
    r"\bi need\b",
    r"\bi like\b",
    r"\bi love\b",
    r"\bi enjoy\b",
    r"\bi prefer\b",
    r"\bi(?:\s+\w+){0,3}\s+like\b",
    r"\bi(?:\s+\w+){0,3}\s+love\b",
    r"\bi(?:\s+\w+){0,3}\s+enjoy\b",
    r"\bi(?:\s+\w+){0,3}\s+prefer\b",
    r"\bi hate\b",
    r"\bi dislike\b",
    r"\bi do not like\b",
    r"\bi don't like\b",
    r"\bi usually\b",
    r"\bi often\b",
    r"\bi always\b",
    r"\bi tend to\b",
    r"\bmy favorite\b",
    r"\bmy favourite\b",
    r"\bi moved to\b",
]


# Leading words/phrases that mark a message as a question or a request for
# Miki to retrieve/recall something, rather than a new statement about the
# user. A message like "Tell me about a sport I like" contains the substring
# "i like" but is asking Miki to recall a preference, not disclosing one.
_QUESTION_LEAD_WORDS = {"what", "who", "when", "where", "why", "how", "which", "whats", "what's", "who's"}

_REQUEST_LEAD_PHRASES = [
    "tell me",
    "remind me",
    "show me",
    "list my",
    "give me",
    "recall",
    "do you know",
    "do you remember",
    "did you know",
    "did i tell you",
    "can you tell me",
    "could you tell me",
    "would you tell me",
    "can you remind me",
    "what do you know",
]


# Explicit user instructions to remember something. Detecting these is a
# narrow speech-act check (not a memory-worthiness decision by itself) --
# per spec, explicit requests get very high priority, and this signal is fed
# into the AI-driven MemoryEvaluator rather than used to decide anything on
# its own.
_EXPLICIT_REMEMBER_PATTERNS = [
    r"\bremember that\b",
    r"\bremember this\b",
    r"\bplease remember\b",
    r"\balways remember\b",
    r"\bdon'?t forget\b",
    r"\bkeep (?:this|that) in mind\b",
    r"\bkeep in mind\b",
    r"\bfrom now on\b",
    r"\bnote that\b",
    r"\bmake a note\b",
]


_ACTION_REQUEST_PATTERNS = [
    r"\badd\b.*\b(?:to|on|in)\b.*\bcalendar\b",
    r"\bput\b.*\b(?:on|in|to)\b.*\bcalendar\b",
    r"\b(?:please\s+)?schedule\s+(?:a|my|the|\w+\s+session|\w+\s+for|\w+\s+at|\w+\s+on|\d+)\b",
    r"^\s*schedule\b",
    r"\bdelete\b.*\b(?:event|appointment|meeting|calendar|memory|task)\b",
    r"^\s*delete\b",
    r"\bforget\b",
    r"\bmove\b.*\b(?:my|the)\b.*\bto\b",
    r"\bsearch\b.*\b(?:for|the|web|online)\b",
    r"^\s*search\b",
    r"\blook up\b",
    r"\bopen\b.*\b(?:file|readme|obsidian|vault|app|document)\b",
    r"\bread\b.*\b(?:pdf|file|doc|text)\b",
    r"\bbook\b.*\b(?:table|flight|hotel|ticket|room)\b",
    r"\bremind me\b",
    r"\bwhat['\s]?s on my calendar\b",
    r"\bwhat do i have\b",
    r"\bfind me a free\b",
    r"\bcreate a task\b",
    r"\bsend\b.*\bmessage\b",
]

_TRIVIAL_MESSAGE_PATTERNS = [
    r"^(?:hello|hi|hey|thanks|thank you|thanks!|okay|ok|yes|no|confirm|do it|sure|cancel|bye|exit|quit|help|clear)$",
    r"^(?:what time is it\??|what['\s]s \d+\s*[\+\-\*\/]\s*\d+\??)$",
]


class BrainProtocol(Protocol):
    """Protocol for AI brain access in memory manager."""

    def generate_response(self, user_input: str, system_prompt: str, history: list[dict[str, str]] | None = None) -> str:
        ...


class MemoryManager:
    """Handles long-term memory extraction and storage without coupling to the conversation layer."""

    def __init__(
        self,
        memory_store: MemoryStore,
        brain: BrainProtocol | None = None,
        *,
        evaluator: MemoryEvaluator | None = None,
        extractor: MemoryExtractor | None = None,
        comparator: MemoryComparator | None = None,
        candidate_store: MemoryCandidateStore | None = None,
        feedback_dataset: MemoryFeedbackDataset | None = None,
        decision_logger: MemoryDecisionLogger | None = None,
        profile_store: ProfileStore | None = None,
        candidate_score_threshold: float = 0.35,
        confident_score_threshold: float = 0.65,
    ) -> None:
        self.memory_store = memory_store
        self.brain = brain

        self.evaluator = evaluator
        self.extractor = extractor
        self.comparator = comparator
        self.candidate_store = candidate_store
        self.feedback_dataset = feedback_dataset
        self.decision_logger = decision_logger or MemoryDecisionLogger()
        self.profile_store = profile_store
        self._profile: Profile | None = None
        self._profile_loaded = False
        self.candidate_score_threshold = candidate_score_threshold
        self.confident_score_threshold = confident_score_threshold

        # Telemetry statistics tracking
        self.stats_processed_count = 0
        self.stats_skipped_count = 0
        self.stats_llm_evaluation_count = 0
        self.stats_memories_created_count = 0
        self.stats_candidates_created_count = 0
        self.stats_duplicates_prevented_count = 0
        self.stats_memories_updated_count = 0

        # Tracking attributes for introspection after a call, mirroring the
        # `MikiCore.last_created_memory` pattern.
        self.last_evaluation: MemoryEvaluation | None = None
        self.last_candidate: MemoryCandidate | None = None
        self.last_comparison: MemoryComparison | None = None
        # Every memory created/updated by the most recent interaction (a message
        # with several facts yields several).
        self.last_memories: list[Memory] = []
        self.recall = MemoryRecall()

    def get_telemetry_stats(self) -> dict[str, int]:
        return {
            "messages_processed": self.stats_processed_count,
            "skipped_locally": self.stats_skipped_count,
            "llm_evaluations": self.stats_llm_evaluation_count,
            "memories_created": self.stats_memories_created_count,
            "candidates_created": self.stats_candidates_created_count,
            "duplicates_prevented": self.stats_duplicates_prevented_count,
            "memories_updated": self.stats_memories_updated_count,
        }

    def format_telemetry_stats(self) -> str:
        stats = self.get_telemetry_stats()
        return (
            "Memory Intelligence Statistics\n"
            "------------------------------\n"
            f"Messages processed: {stats['messages_processed']:,}\n"
            f"Skipped locally (no LLM): {stats['skipped_locally']:,}\n"
            f"LLM evaluations: {stats['llm_evaluations']:,}\n"
            f"Memories created: {stats['memories_created']:,}\n"
            f"Candidates created: {stats['candidates_created']:,}\n"
            f"Duplicates prevented: {stats['duplicates_prevented']:,}\n"
            f"Memories updated: {stats['memories_updated']:,}"
        )

    def extract_memory_from_interaction(
        self,
        user_input: str,
        assistant_response: str | None = None,
        *,
        has_tool_call: bool = False,
    ) -> Memory | None:
        """Extract memory from user input, using AI if available, otherwise falling back to pattern matching."""
        self.stats_processed_count += 1
        text = (user_input or "").strip()
        if not text:
            return None

        self.last_evaluation = None
        self.last_candidate = None
        self.last_comparison = None
        self.last_memories = []

        explicit_request = self._is_explicit_remember_request(text)

        # Level 0 & Level 1 Deterministic Cheap Local Filtering:
        # Avoid running expensive memory LLM calls on trivial messages, recall requests, or pure action requests.
        if not explicit_request:
            if self._is_trivial_message(text):
                logger.info("Skipped memory classification: trivial message or confirmation")
                self.stats_skipped_count += 1
                self.decision_logger.log_evaluation(text, MemoryEvaluation(is_memory_worthy=False, score=0.0, reason="trivial_message"))
                return None

            if self._is_information_request(text):
                logger.info("Skipped memory classification: message looks like a question/request, not a new fact")
                self.stats_skipped_count += 1
                self.decision_logger.log_evaluation(text, MemoryEvaluation(is_memory_worthy=False, score=0.0, reason="information_request"))
                return None

            if has_tool_call or self._is_action_request(text):
                personal_clause = self._extract_personal_disclosure_clause(text)
                if personal_clause:
                    logger.info("Extracted personal disclosure clause from mixed action message: '%s'", personal_clause)
                    text = personal_clause
                else:
                    logger.info("Skipped memory classification: action/tool request without durable personal disclosures")
                    self.stats_skipped_count += 1
                    self.decision_logger.log_evaluation(text, MemoryEvaluation(is_memory_worthy=False, score=0.0, reason="action_request"))
                    return None

        if self.evaluator is not None:
            self.stats_llm_evaluation_count += 1
            return self._extract_via_intelligence(text, explicit_request=explicit_request)

        # ---- legacy v0.4/v0.5 behavior (used whenever no evaluator is configured) ----

        # Fall back to pattern matching
        if self.brain is None:
            return self._extract_memory_via_patterns(text)

        # Avoid sending every message to the LLM. Only escalate to AI when the
        # text contains a cheap, high-signal memory cue.
        if not self._has_memory_signal(text):
            logger.info("Skipped memory classification: no memory signal detected")
            self.stats_skipped_count += 1
            return None

        # If a simple rule already captures the memory, store it without the LLM.
        pattern_memory = self._extract_memory_via_patterns(text)
        if pattern_memory is not None:
            self.stats_memories_created_count += 1
            return pattern_memory

        # Otherwise, use AI-based extraction.
        self.stats_llm_evaluation_count += 1
        return self._extract_memory_via_ai(text, assistant_response)

    def _extract_via_intelligence(self, text: str, *, explicit_request: bool = False) -> Memory | None:
        """New AI-driven pipeline: Evaluator -> (candidate | Extractor -> Comparator -> store/update)."""
        if self._is_information_request(text) and not explicit_request:
            logger.info("Skipped memory classification: message looks like a question/request, not a new fact")
            return None

        related_context = [memory.content for memory in self._cheap_related_memories(text, limit=3)]

        evaluation = self.evaluator.evaluate(text, explicit_request_hint=explicit_request, related_context=related_context)
        self.last_evaluation = evaluation
        self.decision_logger.log_evaluation(text, evaluation)

        if evaluation.score < self.candidate_score_threshold:
            return None

        is_confident = evaluation.score >= self.confident_score_threshold or evaluation.explicit_request
        if not is_confident:
            self.decision_logger.log_candidate(text, evaluation)
            if self.candidate_store is not None:
                self.stats_candidates_created_count += 1
                self.last_candidate = self.candidate_store.add(
                    raw_message=text,
                    score=evaluation.score,
                    reason=evaluation.reason,
                    category=evaluation.category,
                    memory_type=evaluation.memory_type,
                )
            return None

        return self._store_from_evaluation(text, evaluation)

    def _store_from_evaluation(self, text: str, evaluation: MemoryEvaluation) -> Memory | None:
        """Extract canonical memories, compare each against existing ones, and store/update accordingly.

        Returns the primary (first) memory; ``self.last_memories`` holds all of them.
        """
        stored: list[Memory] = []
        for extraction in self._extract_all(text, evaluation):
            # Facts from the same message are siblings: never merge one into another.
            memory = self._store_extraction(extraction, evaluation, exclude_ids={m.memory_id for m in stored})
            if memory is not None and all(memory.memory_id != other.memory_id for other in stored):
                stored.append(memory)
                self._backlink_mentions(memory)
        self.last_memories = stored
        return stored[0] if stored else None

    def _extract_all(self, text: str, evaluation: MemoryEvaluation) -> list[MemoryExtraction]:
        if self.extractor is not None:
            extract_all = getattr(self.extractor, "extract_all", None)
            if callable(extract_all):
                return extract_all(text, evaluation)
            return [self.extractor.extract(text, evaluation)]
        return [
            MemoryExtraction(
                content=self._normalize_user_fact(text),
                category=evaluation.category or "fact",
                memory_type=evaluation.memory_type or "long_term_fact",
            )
        ]

    def _store_extraction(
        self,
        extraction: MemoryExtraction,
        evaluation: MemoryEvaluation,
        exclude_ids: set[str] | None = None,
    ) -> Memory | None:
        content = extraction.content.strip()
        if not content:
            return None

        comparison: MemoryComparison | None = None
        if self.comparator is not None:
            candidates = [m for m in self.list_memories(active_only=True) if m.memory_id not in (exclude_ids or set())]
            comparison = self.comparator.compare(content, extraction.category, candidates)
            self.last_comparison = comparison
            self.decision_logger.log_comparison(comparison.relationship, comparison.reason)

        if comparison is not None and comparison.existing_memory_id:
            existing_id = comparison.existing_memory_id
            if comparison.relationship == "duplicate":
                self.stats_duplicates_prevented_count += 1
                updates: dict[str, Any] = {"last_confirmed_at": utc_now()}
                current = self.get_memory(existing_id)
                if current is not None and not current.entities:
                    updates.update(self._graph_fields(content, extraction=extraction, exclude_id=existing_id))
                touched = self.memory_store.update_memory(existing_id, **updates)
                return touched or self.get_memory(existing_id)
            if comparison.should_update_existing:
                self.stats_memories_updated_count += 1
                return self.memory_store.update_memory(
                    existing_id,
                    content=content,
                    confidence=evaluation.score,
                    importance=evaluation.importance,
                    stability=evaluation.stability,
                    usefulness=evaluation.usefulness,
                    last_confirmed_at=utc_now(),
                    **self._graph_fields(content, extraction=extraction, exclude_id=existing_id),
                )

        self.stats_memories_created_count += 1
        return self.memory_store.create_memory(
            content=content,
            category=extraction.category,
            memory_type=extraction.memory_type,
            confidence=evaluation.score,
            source="conversation",
            importance=evaluation.importance,
            stability=evaluation.stability,
            usefulness=evaluation.usefulness,
            **self._graph_fields(content, extraction=extraction),
        )

    def _backlink_mentions(self, memory: Memory) -> None:
        """A new entity may already be mentioned in older memories; link them to it."""
        names = [name for name in memory.entities if not is_junk_entity(entity_key(name)) and len(entity_key(name)) >= 3]
        if not names:
            return
        for other in self.list_memories(active_only=True):
            if other.memory_id == memory.memory_id:
                continue
            have = {entity_key(name) for name in other.entities}
            text = " " + re.sub(r"[^\w\s&+'’-]", " ", f"{other.title} {other.content}".lower()) + " "
            missing = [name for name in names if entity_key(name) not in have and f" {entity_key(name)} " in text]
            if missing:
                self.memory_store.update_memory(
                    other.memory_id,
                    entities=[*other.entities, *missing][:8],
                    entity_types={**other.entity_types, **{name: memory.entity_types.get(name, "other") for name in missing}},
                )

    def _graph_fields(
        self,
        content: str,
        *,
        extraction: MemoryExtraction | None = None,
        exclude_id: str | None = None,
    ) -> dict[str, Any]:
        """Title, entities and direct links for a memory about to be stored.

        Falls back to offline heuristics when the extractor supplied no entities.
        """
        title = extraction.title if extraction is not None else ""
        entities = list(extraction.entities) if extraction is not None else []
        entity_types = dict(extraction.entity_types) if extraction is not None else {}
        if not entities:
            entities, entity_types = derive_entities(content)
        brain = MemoryBrain(self.list_memories(active_only=True))
        # Entity linking: any already-known entity mentioned in the text becomes a link,
        # even if the model didn't list it (e.g. "gym for basketball conditioning").
        known = {entity_key(name) for name in entities}
        for entity in brain.find_entity(f"{title} {content}"):
            if entity.key not in known and not is_junk_entity(entity.key) and len(entities) < 8:
                entities.append(entity.name)
                entity_types[entity.name] = entity.type
                known.add(entity.key)
        related = brain.suggest_related(content, entities, exclude_id=exclude_id)
        fields: dict[str, Any] = {"entities": entities, "entity_types": entity_types, "related": related}
        if title:
            fields["title"] = title
        return fields

    @staticmethod
    def _is_action_request(text: str) -> bool:
        lower = text.lower()
        return any(re.search(pattern, lower) for pattern in _ACTION_REQUEST_PATTERNS)

    @staticmethod
    def _is_trivial_message(text: str) -> bool:
        lower = text.lower().strip()
        return any(re.search(pattern, lower) for pattern in _TRIVIAL_MESSAGE_PATTERNS)

    @classmethod
    def _extract_personal_disclosure_clause(cls, text: str) -> str | None:
        """If a message contains an action request alongside a separate personal disclosure
        (e.g., 'Add gym tomorrow at 7. I usually train around that time.'), returns the disclosure clause."""
        sentences = re.split(r"[.!?\n]+", text)
        clauses = []
        for s in sentences:
            s_clean = s.strip()
            if not s_clean:
                continue
            if any(re.search(pat, s_clean.lower()) for pat in _ACTION_REQUEST_PATTERNS):
                continue
            if any(re.search(pat, s_clean.lower()) for pat in MEMORY_SIGNAL_PATTERNS) or re.search(r"\b(prefer|like|love|usually|always|tend to|habit|routine|work out|train|study)\b", s_clean.lower()):
                clauses.append(s_clean)
        if clauses:
            return " ".join(clauses)
        return None

    def _cheap_related_memories(self, text: str, *, limit: int = 3) -> list[Memory]:
        """Best-effort, no-API-call related-memory lookup, used only to give
        the evaluator optional context (not for the real dedup decision)."""
        memories = self.list_memories(active_only=True)
        if not memories:
            return []
        scored = [(memory, self._text_similarity(text, memory.content)) for memory in memories]
        scored = [pair for pair in scored if pair[1] >= 0.25]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [memory for memory, _ in scored[:limit]]

    @staticmethod
    def _is_explicit_remember_request(text: str) -> bool:
        lower = text.lower()
        return any(re.search(pattern, lower) for pattern in _EXPLICIT_REMEMBER_PATTERNS)

    def list_candidates(self, *, status: str | None = "pending") -> list[MemoryCandidate]:
        if self.candidate_store is None:
            return []
        return self.candidate_store.list(status=status)

    def approve_candidate(self, candidate_id: str) -> Memory | None:
        if self.candidate_store is None:
            return None
        candidate = self.candidate_store.get(candidate_id)
        if candidate is None or candidate.status != "pending":
            return None

        evaluation = MemoryEvaluation(
            is_memory_worthy=True,
            score=max(candidate.score, self.confident_score_threshold),
            reason=candidate.reason,
            category=candidate.category,
            memory_type=candidate.memory_type,
        )
        memory = self._store_from_evaluation(candidate.raw_message, evaluation)
        self.candidate_store.resolve(
            candidate_id,
            status="approved",
            resulting_memory_id=memory.memory_id if memory is not None else None,
        )
        if self.feedback_dataset is not None:
            self.feedback_dataset.record(
                message=candidate.raw_message,
                memory_decision=True,
                user_feedback="approved",
                category=(memory.category if memory is not None else candidate.category),
                confidence=candidate.score,
            )
        return memory

    def reject_candidate(self, candidate_id: str) -> bool:
        if self.candidate_store is None:
            return False
        candidate = self.candidate_store.get(candidate_id)
        if candidate is None or candidate.status != "pending":
            return False

        self.candidate_store.resolve(candidate_id, status="rejected")
        if self.feedback_dataset is not None:
            self.feedback_dataset.record(
                message=candidate.raw_message,
                memory_decision=False,
                user_feedback="rejected",
                category=candidate.category,
                confidence=candidate.score,
            )
        return True

    @staticmethod
    def _has_memory_signal(text: str) -> bool:
        return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in MEMORY_SIGNAL_PATTERNS)

    @staticmethod
    def _is_information_request(text: str) -> bool:
        """True if the message is asking Miki to tell/recall something,
        rather than disclosing a new fact about the user."""
        stripped = text.strip()
        if stripped.endswith("?"):
            return True

        lower = stripped.lower().rstrip("?!. ")
        words = lower.split()
        if words and words[0] in _QUESTION_LEAD_WORDS:
            return True

        return any(lower.startswith(phrase) for phrase in _REQUEST_LEAD_PHRASES)

    def _extract_memory_via_ai(self, user_input: str, assistant_response: str | None = None) -> Memory | None:
        """Use AI to decide if a message is memory-worthy and extract the memory content."""
        try:
            prompt = f"""Analyze this user statement and decide if it contains information worth remembering.

User said: "{user_input}"

If this is memory-worthy, extract the essential fact/preference/habit and respond ONLY with valid JSON (no markdown, no extra text):
{{"is_memory_worthy": true, "memory_content": "...", "category": "identity|preference|habit|fact", "confidence": 0.0-1.0}}

If NOT memory-worthy, respond ONLY with:
{{"is_memory_worthy": false}}

Categories:
- identity: facts about who the user is (name, role, background)
- preference: likes, dislikes, favorites
- habit: regular behaviors and patterns
- fact: circumstances, possessions, skills, locations

Important:
- Extract ONLY the essential memory, not the original phrasing
- For example: "My name is Alex" → memory_content: "Name is Alex"
- For example: "I really love hiking" → memory_content: "Enjoys hiking"
- Be concise. Do NOT include the original user phrasing."""

            response_text = self.brain.generate_response(
                user_input=prompt,
                system_prompt="You are a memory extraction system. You respond ONLY with valid JSON, no markdown or extra text.",
                history=None,
            )

            response_text = response_text.strip()
            if response_text.startswith("```json"):
                response_text = response_text[7:]
            if response_text.startswith("```"):
                response_text = response_text[3:]
            if response_text.endswith("```"):
                response_text = response_text[:-3]
            response_text = response_text.strip()

            memory_eval = json.loads(response_text)

            if not memory_eval.get("is_memory_worthy", False):
                logger.info("AI determined this is not memory-worthy")
                return None

            memory_content = memory_eval.get("memory_content", "").strip()
            if not memory_content:
                logger.info("AI extraction yielded empty memory content")
                return None

            category = memory_eval.get("category", "fact")
            confidence = min(1.0, max(0.0, memory_eval.get("confidence", 0.8)))

            canonical_key = self._canonical_memory_key(memory_content, category)
            for existing in self.list_memories(active_only=True):
                if self._canonical_memory_key(existing.content, existing.category) == canonical_key:
                    logger.info("Skipped duplicate memory: %s", existing.memory_id)
                    return existing

            logger.info("AI extracted memory: %s (confidence: %.1f)", memory_content, confidence)
            return self.memory_store.create_memory(
                content=memory_content,
                category=category,
                memory_type="fact" if category in {"identity", "fact"} else "preference",
                confidence=confidence,
                source="conversation",
                **self._graph_fields(memory_content),
            )

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Failed to extract memory via AI: %s. Falling back to pattern matching.", e)
            return self._extract_memory_via_patterns(user_input)

    def _extract_memory_via_patterns(self, user_input: str) -> Memory | None:
        """Fall back pattern matching for memory extraction."""
        text = (user_input or "").strip()
        if not text:
            return None

        lower = text.lower()
        memory_patterns = [
            ("identity", ["my name is", "i am called", "i am ", "i'm ", "i was born"]),
            ("preference", ["i like", "i love", "i enjoy", "i prefer", "i hate", "i dislike", "i don't like"]),
            ("habit", ["i usually", "i often", "i always", "i tend to", "i do not like"]),
            ("fact", ["i work at", "i live in", "i study", "i have", "i own", "i need"]),
        ]

        for category, patterns in memory_patterns:
            for pattern in patterns:
                if pattern in lower:
                    memory_type = "fact" if category in {"identity", "fact"} else "preference"
                    confidence = 0.9 if category in {"identity", "preference"} else 0.7
                    normalized = self._normalize_user_fact(text)
                    if not normalized:
                        return None

                    canonical_key = self._canonical_memory_key(normalized, category)
                    for existing in self.list_memories(active_only=True):
                        if self._canonical_memory_key(existing.content, existing.category) == canonical_key:
                            logger.info("Skipped duplicate memory: %s", existing.memory_id)
                            return existing

                    return self.memory_store.create_memory(
                        content=normalized,
                        category=category,
                        memory_type=memory_type,
                        confidence=confidence,
                        source="conversation",
                        **self._graph_fields(normalized),
                    )

        return None

    def extract_and_store_from_interaction(
        self,
        user_input: str,
        assistant_response: str | None = None,
        *,
        has_tool_call: bool = False,
    ) -> Memory | None:
        memory = self.extract_memory_from_interaction(user_input, assistant_response, has_tool_call=has_tool_call)
        if memory is None:
            logger.info("No long-term memory extracted from interaction")
            return None
        logger.info("Stored long-term memory %s", memory.memory_id)
        return memory

    def create_memory(self, content: str, *, category: str = "general", memory_type: str = "fact", confidence: float = 0.5, source: str = "manual") -> Memory:
        return self.memory_store.create_memory(
            content=content,
            category=category,
            memory_type=memory_type,
            confidence=confidence,
            source=source,
            **self._graph_fields(content),
        )

    def list_memories(self, *, active_only: bool = True) -> list[Memory]:
        return self.memory_store.list_memories(active_only=active_only)

    def get_memory(self, memory_id: str) -> Memory | None:
        return self.memory_store.get_memory(memory_id)

    def update_memory(self, memory_id: str, **updates) -> Memory | None:
        return self.memory_store.update_memory(memory_id, **updates)

    def delete_memory(self, memory_id: str) -> Memory | None:
        return self.memory_store.delete_memory(memory_id)

    def count_memories(self, *, active_only: bool = True) -> int:
        return self.memory_store.count_memories(active_only=active_only)

    def delete_memory_by_text(self, lookup: str) -> Memory | None:
        if not lookup:
            return None
        lookup_value = lookup.strip().lower()
        for memory in self.list_memories(active_only=True):
            if memory.memory_id == lookup_value or lookup_value in memory.content.lower():
                return self.delete_memory(memory.memory_id)
        return None

    # ------------------------------------------------------------------ brain
    def build_recall_block(self, query: str) -> str | None:
        """Always-on, graph-aware "what Miki remembers" block for the system prompt."""
        try:
            return self.recall.build(query, self.list_memories(active_only=True), portrait=self.portrait)
        except Exception:
            logger.exception("Memory recall failed; continuing without it")
            return None

    # ---------------------------------------------------------------- profile
    def get_profile(self) -> Profile | None:
        if not self._profile_loaded:
            self._profile_loaded = True
            if self.profile_store is not None:
                self._profile = self.profile_store.load()
        return self._profile

    @property
    def portrait(self) -> str:
        profile = self.get_profile()
        return profile.portrait if profile is not None else ""

    def refresh_profile(self) -> Profile:
        """Re-read every memory and rewrite the portrait, per-area coverage and next questions."""
        if self.brain is None:
            raise ValueError("A brain is needed to build the profile.")
        previous = self.get_profile()
        profile = ProfileSynthesizer(self.brain).synthesize(self.list_memories(active_only=True))
        if previous is not None and profile.memory_count >= previous.memory_count:
            # Learning more can't make Miki know you *less*: the model re-scores every time, so keep
            # each area's confidence from dipping while memories only grow.
            before = {d.key: d.confidence for d in previous.domains}
            for domain in profile.domains:
                domain.confidence = max(domain.confidence, before.get(domain.key, 0.0))
        self._profile, self._profile_loaded = profile, True
        if self.profile_store is not None:
            self.profile_store.save(profile)
        write_note = getattr(self.memory_store, "write_profile_note", None)
        if callable(write_note):
            try:
                write_note(profile.to_dict())
            except Exception:
                logger.exception("Could not write the profile note")
        return profile

    def profile_is_stale(self, min_new: int = 5) -> bool:
        """True once enough has been learned since the last profile that it is worth refreshing."""
        count = self.count_memories(active_only=True)
        profile = self.get_profile()
        if profile is None:
            return count >= 3
        return abs(count - profile.memory_count) >= min_new

    def maybe_refresh_profile(self, min_new: int = 5) -> bool:
        if self.brain is None or not self.profile_is_stale(min_new):
            return False
        try:
            self.refresh_profile()
            return True
        except Exception:
            logger.exception("Profile refresh failed")
            return False

    def mail_context(self) -> str:
        """A short description of the user for judging their email: portrait plus the people who matter."""
        parts = []
        if self.portrait:
            parts.append(self.portrait.strip()[:500])
        brain = MemoryBrain(self.list_memories(active_only=True))
        people = [e.name for e in brain.entities.values() if e.type == "person"][:12]
        if people:
            parts.append("People who matter to them: " + ", ".join(people) + ".")
        return " ".join(parts)

    def learn_from_conversations(
        self,
        conversations_dir: str = "data/conversations",
        state_path: str = "data/learned_messages.json",
        *,
        limit: int | None = None,
        on_progress=None,
    ) -> dict[str, int]:
        """Replay what the user said in earlier chats through the memory pipeline (idempotent)."""
        learner = ConversationLearner(self, conversations_dir, state_path)
        return learner.learn(limit=limit, on_progress=on_progress)

    def count_unlearned_messages(self, conversations_dir: str = "data/conversations", state_path: str = "data/learned_messages.json") -> int:
        return len(ConversationLearner(self, conversations_dir, state_path).pending())

    def catch_up(self, conversations_dir: str = "data/conversations") -> dict[str, Any]:
        """Cheap startup upkeep: refresh a stale profile and write a missing past-day journal entry."""
        report: dict[str, Any] = {"profile": False, "journal": None}
        report["profile"] = self.maybe_refresh_profile()
        if self.brain is not None:
            journal = DailyJournal(self.brain, self.memory_store, conversations_dir)
            missing = yesterday_entry_missing(journal)
            if missing is not None:
                try:
                    path = journal.write(missing)
                    report["journal"] = path.name if path else None
                except Exception:
                    logger.exception("Catch-up journal for %s failed", missing)
        return report

    def consolidate(self, conversations_dir: str = "data/conversations") -> dict[str, Any]:
        """The "sleep" pass: refresh the portrait and write the journal entries that are missing."""
        report: dict[str, Any] = {"profile": False, "journal": []}
        try:
            self.refresh_profile()
            report["profile"] = True
        except Exception:
            logger.exception("Consolidation: profile refresh failed")
        if self.brain is not None:
            journal = DailyJournal(self.brain, self.memory_store, conversations_dir)
            days = [date_ for date_ in (yesterday_entry_missing(journal), datetime.now().date()) if date_ is not None]
            for day in days:
                try:
                    path = journal.write(day)
                    if path is not None:
                        report["journal"].append(str(path.name))
                except Exception:
                    logger.exception("Consolidation: journal for %s failed", day)
        return report

    def remember(self, text: str) -> list[Memory]:
        """Explicitly store what the user asked to be remembered (skips the worthiness check)."""
        cleaned = (text or "").strip()
        if not cleaned:
            return []
        evaluation = MemoryEvaluation(
            is_memory_worthy=True,
            score=0.95,
            reason="explicit /remember",
            importance=0.7,
            stability=0.7,
            usefulness=0.7,
            explicit_request=True,
        )
        self._store_from_evaluation(cleaned, evaluation)
        return list(self.last_memories)

    def graph_snapshot(self) -> dict[str, Any]:
        brain = MemoryBrain(self.list_memories(active_only=True))
        return {**brain.to_graph(), "stats": brain.stats()}

    def enrich_existing_memories(self) -> int:
        """Backfill titles/entities on memories saved before the graph existed."""
        changed = 0
        for memory in self.list_memories(active_only=True):
            if memory.entities and memory.title:
                continue
            title, entities, types = memory.title, list(memory.entities), dict(memory.entity_types)
            content_update: str | None = None
            if self.extractor is not None and (not entities or not title):
                probe = MemoryEvaluation(
                    is_memory_worthy=True, score=0.9, reason="graph backfill",
                    category=memory.category, memory_type=memory.memory_type,
                )
                try:
                    found = self.extractor.extract_all(memory.content, probe)[0]
                    title = title or found.title
                    if not entities:
                        entities, types = list(found.entities), dict(found.entity_types)
                    # Old memories were stored close to verbatim ("I love playing basketball");
                    # restate them in the canonical third person.
                    if found.content and re.match(r"^(?:i|i'm|i've|i'd|my)\b", memory.content.strip(), re.IGNORECASE):
                        content_update = found.content
                except Exception:
                    logger.exception("Could not enrich memory %s", memory.memory_id)
            if not entities:
                entities, types = derive_entities(memory.content)
            updates: dict[str, Any] = {"entities": entities, "entity_types": types}
            if title:
                updates["title"] = title
            if content_update:
                updates["content"] = content_update
            self.memory_store.update_memory(memory.memory_id, **updates)
            changed += 1
        return changed

    def relink_all(self) -> int:
        """Recompute each memory's direct links from the current graph."""
        for memory in self.list_memories(active_only=True):
            self._backlink_mentions(memory)
        memories = self.list_memories(active_only=True)
        brain = MemoryBrain(memories)
        changed = 0
        for memory in memories:
            related = brain.suggest_related(memory.content, memory.entities, exclude_id=memory.memory_id)
            if set(related) != set(memory.related):
                self.memory_store.update_memory(memory.memory_id, related=related)
                changed += 1
        return changed

    def rebuild_brain(self) -> dict[str, Any]:
        """Backfill, relink and regenerate the Obsidian graph notes. Safe to run any time."""
        backup = getattr(self.memory_store, "backup", None)
        backup_path = str(backup()) if callable(backup) else None
        enriched = self.enrich_existing_memories()
        relinked = self.relink_all()
        rebuild = getattr(self.memory_store, "rebuild_graph", None)
        notes = rebuild() if callable(rebuild) else {}
        stats = MemoryBrain(self.list_memories(active_only=True)).stats()
        return {"enriched": enriched, "relinked": relinked, "backup": backup_path, **stats, **(notes or {})}

    def needs_graph_migration(self) -> bool:
        """True when some active memory predates the knowledge graph (no title/entities)."""
        return any(not (m.entities and m.title) for m in self.list_memories(active_only=True))

    def sync_memories(self) -> dict[str, int]:
        sync = getattr(self.memory_store, "sync", None)
        if callable(sync):
            result = sync()
            if isinstance(result, dict):
                return result
        active = self.count_memories(active_only=True)
        total = self.count_memories(active_only=False)
        return {"active": active, "inactive": total - active, "total": total}

    def describe_memory_store(self) -> dict[str, Any]:
        describe = getattr(self.memory_store, "describe", None)
        if callable(describe):
            info = describe()
            if hasattr(info, "__dict__"):
                return dict(info.__dict__)
        return {"backend": type(self.memory_store).__name__}

    def search_memories(self, keyword: str, category: str | None = None) -> list[Memory]:
        """Search memories by keyword (fuzzy match) and optionally filter by category.
        
        Args:
            keyword: Search term to match against memory content
            category: Optional category filter (e.g., 'preference', 'identity', 'habit')
        
        Returns:
            List of matching memories sorted by relevance (highest similarity first)
        """
        if not keyword or not keyword.strip():
            return []
        
        keyword_lower = keyword.strip().lower()
        all_memories = self.list_memories(active_only=True)
        
        # Filter by category if provided
        if category is not None:
            all_memories = [m for m in all_memories if m.category == category.lower()]
        
        # Score memories by similarity to keyword
        scored = []
        for memory in all_memories:
            content_lower = memory.content.lower()
            
            # Exact substring match gets highest score
            if keyword_lower in content_lower:
                score = 1.0
                scored.append((memory, score))
            else:
                # Use fuzzy matching only if substring not found
                # Calculate similarity ratio - must be reasonably high to include
                sim = difflib.SequenceMatcher(None, keyword_lower, content_lower).ratio()
                # Only include if similarity is meaningful (> 0.6)
                if sim > 0.6:
                    scored.append((memory, sim))
        
        # Sort by score descending, then by confidence descending
        scored.sort(key=lambda x: (-x[1], -x[0].confidence))
        return [memory for memory, _ in scored]

    @staticmethod
    def _normalize_user_fact(value: str) -> str:
        cleaned = re.sub(r"\s+", " ", value).strip()
        cleaned = re.sub(r"^(hey|miki|please)\s+", "", cleaned, flags=re.IGNORECASE)
        cleaned = cleaned.rstrip("?!. ")
        return cleaned if cleaned else ""

    @staticmethod
    def _are_synonyms(word1: str, word2: str) -> bool:
        """Check if two words are synonyms."""
        word1_lower = word1.lower().strip()
        word2_lower = word2.lower().strip()
        
        if word1_lower == word2_lower:
            return True
        
        # Check synonym map
        for base_word, synonyms in SYNONYM_MAP.items():
            if base_word.lower() == word1_lower and word2_lower in {s.lower() for s in synonyms}:
                return True
            if base_word.lower() == word2_lower and word1_lower in {s.lower() for s in synonyms}:
                return True
        
        return False

    @staticmethod
    def _get_word_list(text: str) -> list[str]:
        """Extract meaningful words from text for similarity comparison."""
        # Remove common words and extract content words
        stop_words = {"my", "i", "is", "am", "are", "the", "a", "an", "and", "or", "but", "in", "at", "to"}
        words = re.findall(r"\b\w+\b", text.lower())
        return [w for w in words if w not in stop_words and len(w) > 2]

    @staticmethod
    def _text_similarity(text1: str, text2: str) -> float:
        """Calculate similarity between two text strings using Levenshtein-inspired approach."""
        if text1.lower() == text2.lower():
            return 1.0
        
        # Compare word lists with synonym awareness
        words1 = MemoryManager._get_word_list(text1)
        words2 = MemoryManager._get_word_list(text2)
        
        if not words1 or not words2:
            return 0.0
        
        # Count matching words (including synonyms)
        matches = 0
        for w1 in words1:
            for w2 in words2:
                if MemoryManager._are_synonyms(w1, w2):
                    matches += 1
                    break
        
        # Similarity is matches / max length
        max_len = max(len(words1), len(words2))
        return matches / max_len if max_len > 0 else 0.0

    @staticmethod
    def _canonical_memory_key(value: str, category: str) -> str:
        cleaned = MemoryManager._normalize_user_fact(value).lower()
        if category == "identity":
            for prefix in ("my name is ", "i am ", "i'm ", "i am called ", "i was born "):
                if cleaned.startswith(prefix):
                    cleaned = cleaned[len(prefix):]
                    break
        cleaned = cleaned.strip().rstrip("?!.")
        
        # Use improved similarity detection for duplicates
        # Extract key phrases for better duplicate detection
        if category == "preference":
            # For preferences, focus on the verb + object: "enjoy X", "like X"
            verb_match = re.search(r"\b(like|enjoy|love|prefer|hate|dislike|don't like)\s+(.+)", cleaned)
            if verb_match:
                verb, obj = verb_match.groups()
                return f"prefer:{obj.strip()}"
        elif category == "habit":
            # For habits, extract the action
            action_match = re.search(r"\b(usually|often|always|tend to)\s+(.+)", cleaned)
            if action_match:
                _, action = action_match.groups()
                return f"habit:{action.strip()}"
        
        return cleaned
