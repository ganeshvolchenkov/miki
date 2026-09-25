"""Always-on, graph-aware recall.

Instead of hoping a retrieval gate fires, every message gets a compact
"what Miki remembers" block:

* small memory (<= ``full_context_limit`` active memories): all of it, ordered so
  what is relevant to the message comes first;
* larger memory: a core profile (identity + most important) plus the memories
  relevant to this message *and their neighbours in the knowledge graph*.

No API calls -- it runs on every message for free. Semantic (embedding) recall
for large memories is still provided by the RAG layer alongside this.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.memory.brain import MemoryBrain, content_tokens, hub_for_category
from app.memory.memory import Memory

_GUIDANCE = (
    "How to use this: it is everything you have learned about the user. Use it naturally when it helps -- never "
    "recite it or announce that you 'remember'. Treat entries marked (unsure) as tentative. If entries conflict, "
    "prefer the more recently updated one. If the user asks what you know about them, answer from this list and "
    "do not invent anything beyond it."
)


@dataclass
class _Scored:
    memory: Memory
    score: float = 0.0
    relevant: bool = False
    via: str | None = None


class MemoryRecall:
    def __init__(self, *, full_context_limit: int = 30, max_items: int = 14, max_chars: int = 2200) -> None:
        self.full_context_limit = full_context_limit
        self.max_items = max_items
        self.max_chars = max_chars

    def build(self, query: str, memories: list[Memory], portrait: str = "") -> str | None:
        active = [m for m in memories if m.active and m.content.strip()]
        if not active:
            return None

        brain = MemoryBrain(active)
        scored = self._score(query, active, brain)

        if len(active) <= self.full_context_limit:
            ordered = sorted(scored.values(), key=lambda s: s.score, reverse=True)
            relevant = [s for s in ordered if s.relevant]
            rest = [s for s in ordered if not s.relevant]
            sections = [("Most relevant to this message", relevant), ("Also known", rest)] if relevant else [("Known about the user", rest)]
        else:
            profile = self._core_profile(scored)
            profile_ids = {s.memory.memory_id for s in profile}
            budget = max(4, self.max_items - len(profile))
            relevant = [
                s for s in sorted(scored.values(), key=lambda s: s.score, reverse=True)
                if s.relevant and s.memory.memory_id not in profile_ids
            ][:budget]
            sections = [("Core profile", profile), ("Relevant to this message", relevant)]

        return self._format(sections, brain, portrait.strip()[:700])

    # ------------------------------------------------------------ scoring
    def _score(self, query: str, memories: list[Memory], brain: MemoryBrain) -> dict[str, _Scored]:
        query_tokens = content_tokens(query)
        query_entities = {entity.key for entity in brain.find_entity(query)}
        scored: dict[str, _Scored] = {}

        for memory in memories:
            entity_keys = set(brain.entity_keys(memory.memory_id))
            haystack = content_tokens(" ".join([memory.content, memory.title, " ".join(memory.entities)]))
            overlap = len(query_tokens & haystack)
            entity_hit = bool(entity_keys & query_entities)

            prior = 0.4 * memory.importance + 0.3 * memory.confidence + 0.1 * memory.usefulness
            if memory.category == "identity":
                prior += 0.4
            score = min(overlap, 3) * 0.6 + (1.5 if entity_hit else 0.0) + prior * 0.5
            scored[memory.memory_id] = _Scored(memory, score, relevant=overlap > 0 or entity_hit)

        # Pull in graph neighbours of directly relevant memories.
        seeds = [s for s in scored.values() if s.relevant]
        for seed in seeds:
            for connection in brain.neighbors(seed.memory.memory_id, limit=4):
                neighbour = scored[connection.memory.memory_id]
                bonus = 0.5 * seed.score * min(connection.weight, 1.5) / 1.5
                if not neighbour.relevant:
                    neighbour.relevant = True
                    neighbour.via = ", ".join(connection.via) or _short(seed.memory.content)
                neighbour.score += bonus
        return scored

    def _core_profile(self, scored: dict[str, _Scored]) -> list[_Scored]:
        identity = sorted(
            (s for s in scored.values() if s.memory.category == "identity"),
            key=lambda s: s.memory.importance + s.memory.confidence,
            reverse=True,
        )[:5]
        taken = {s.memory.memory_id for s in identity}
        important = sorted(
            (s for s in scored.values() if s.memory.memory_id not in taken),
            key=lambda s: 0.6 * s.memory.importance + 0.4 * s.memory.confidence,
            reverse=True,
        )[:4]
        return identity + important

    # ---------------------------------------------------------- formatting
    def _format(self, sections: list[tuple[str, list[_Scored]]], brain: MemoryBrain, portrait: str = "") -> str | None:
        lines = ["WHAT MIKI REMEMBERS ABOUT THE USER (long-term memory):"]
        if portrait:
            lines.append(f"Who they are: {portrait}")
        used = sum(len(line) for line in lines)
        emitted = 0
        for heading, items in sections:
            block: list[str] = []
            for item in items:
                line = self._line(item, brain)
                if used + len(line) > self.max_chars:
                    break
                block.append(line)
                used += len(line) + 1
            if block:
                lines.append(f"{heading}:")
                lines.extend(block)
                emitted += len(block)
        if not emitted:
            return None
        lines.append("")
        lines.append(_GUIDANCE)
        return "\n".join(lines)

    @staticmethod
    def _line(item: _Scored, brain: MemoryBrain) -> str:
        memory = item.memory
        notes = [hub_for_category(memory.category).rstrip("s").lower()]
        entities = brain.entity_names(memory)[:3]
        if entities:
            notes.append("about " + ", ".join(entities))
        if item.via:
            notes.append("connected via " + item.via)
        if memory.confidence < 0.6:
            notes.append("unsure")
        return f"- {memory.content.strip()}  ({'; '.join(notes)})"


def _short(text: str, limit: int = 40) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
