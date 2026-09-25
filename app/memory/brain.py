"""Knowledge-graph layer over Miki's memories.

A memory is a node. It links to *entities* it mentions (people, places,
interests, ...), to a *category hub* (Preferences, Habits, ...), to the user's
central ``Me`` hub, and to other memories it is closely related to. The same
structure feeds three consumers, so they can never disagree:

* recall  -- which memories to hand the model, incl. connected ones
* Obsidian -- the wikilinks/notes that make the vault a real graph
* the dashboard's brain view

Everything here is pure Python and offline (no API calls).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from typing import Iterable

from app.memory.memory import Memory

USER_HUB = "Me"

CATEGORY_HUBS = {
    "identity": "Identity",
    "preference": "Preferences",
    "habit": "Habits",
    "goal": "Goals",
    "project": "Projects",
    "relationship": "Relationships",
    "skill": "Skills",
    "routine": "Routines",
    "constraint": "Constraints",
    "fact": "Facts",
    "event": "Events",
    "general": "General",
}

# Entities that would connect everything to everything -- never linked.
GENERIC_ENTITIES = {"user", "the user", "me", "i", "myself", "miki", "you", "he", "she", "they"}

# Entities mentioned by more memories than this are too generic to derive
# memory-to-memory edges from (they still get their own note).
_MAX_PAIRWISE_FANOUT = 25

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "have", "has", "had", "are", "was", "were", "but", "not",
    "you", "your", "his", "her", "their", "its", "our", "who", "what", "when", "where", "why", "how", "which",
    "user", "users", "about", "into", "than", "then", "also", "very", "just", "some", "any", "can", "will",
    "would", "could", "should", "does", "did", "doing", "done", "been", "being", "they", "them", "there",
    "usually", "often", "always", "really", "like", "likes", "love", "loves", "enjoy", "enjoys", "prefer", "prefers",
}

_RELATION_NOUNS = {
    "dad", "father", "mom", "mother", "brother", "sister", "wife", "husband", "girlfriend", "boyfriend", "partner",
    "friend", "boss", "son", "daughter", "grandmother", "grandfather", "uncle", "aunt", "cousin", "roommate",
    "colleague", "manager", "teacher", "coach",
}

_STOP_ENTITY_WORDS = {
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february", "march",
    "april", "may", "june", "july", "august", "september", "october", "november", "december", "user", "i", "miki",
    "the", "a", "an", "my", "his", "her", "their", "it", "its", "this", "that", "these", "those", "there", "here",
    "weekly", "daily", "monthly", "yearly", "today", "tomorrow", "yesterday", "now", "sometimes", "often", "always",
    "never", "usually", "however", "also", "then", "plans", "planning", "wants", "started", "likes", "loves",
}

_LEADING_FILLERS = {
    "play", "plays", "playing", "drink", "drinks", "drinking", "eat", "eats", "eating", "watch", "watches",
    "watching", "read", "reads", "reading", "listen", "listens", "listening", "go", "goes", "going", "do",
    "does", "doing", "to", "the", "a", "an", "some", "his", "her", "their", "my",
}

_ENTITY_STOPPERS = {
    "in", "on", "at", "with", "every", "during", "after", "before", "because", "when", "and", "but", "or", "for",
    "since", "while", "so", "as", "than", "if", "approximately", "about", "around", "per", "each", "once", "twice",
}

_INTEREST_RE = re.compile(
    r"\b(?:loves?|likes?|enjoys?|prefers?|plays?|playing|drinks?|drinking|eats?|eating|watches|watching|reads?|reading|"
    r"listens? to|is into|fan of|hobby is|favou?rite [\w ]{1,20}? is)\s+([A-Za-z][\w'’&+.-]*(?:\s+[A-Za-z][\w'’&+.-]*){0,3})",
    re.IGNORECASE,
)
_PLACE_RE = re.compile(
    r"\b(?:lives? in|living in|moved to|born in|based in|grew up in|is from|comes from|from)\s+"
    r"([A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*){0,2})"
)
_ORG_RE = re.compile(
    r"\b(?:works? (?:at|for)|employed (?:at|by)|studies at|studying at|attends?|interns? at)\s+"
    r"([A-Z][\w'’&-]*(?:\s+[A-Z][\w'’&-]*){0,3})"
)
_SUBJECT_RE = re.compile(
    r"\b(?:studies|studying|majors? in|degree in|learning|learns)\s+([A-Za-z][\w'’&-]*(?:\s+[A-Za-z][\w'’&-]*){0,2})",
    re.IGNORECASE,
)
_PROPER_RE = re.compile(r"\b([A-Z][a-z][\w'’-]*)\b")


# ---------------------------------------------------------------- naming

def is_junk_entity(key: str) -> bool:
    """Days, months, filler words, bare numbers/times: never useful graph nodes."""
    if not key or key in GENERIC_ENTITIES or key in _STOP_ENTITY_WORDS:
        return True
    if len(key) < 2:
        return True
    return bool(re.fullmatch(r"\d[\d\s:.,-]*(?:am|pm)?", key))


def hub_for_category(category: str | None) -> str:
    return CATEGORY_HUBS.get((category or "general").strip().lower(), "General")


def entity_key(name: str) -> str:
    cleaned = re.sub(r"[^\w\s&+'’-]", " ", str(name).lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if cleaned.startswith("the "):
        cleaned = cleaned[4:]
    return cleaned


def display_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(name)).strip(" .,;:!?\"'()[]")
    if cleaned and cleaned == cleaned.lower():
        cleaned = " ".join(word[:1].upper() + word[1:] for word in cleaned.split(" "))
    return cleaned


def safe_note_name(name: str, *, limit: int = 80) -> str:
    """A filesystem- and wikilink-safe note name (no path separators, no [] | # ^)."""
    cleaned = re.sub(r'[<>:"/\\|?*\[\]#^]', " ", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:limit].strip(" .") or "Untitled"


def content_tokens(text: str) -> set[str]:
    tokens = set()
    for word in re.findall(r"[a-zA-Z0-9]+", (text or "").lower()):
        if len(word) < 3 or word in _STOPWORDS:
            continue
        tokens.add(_stem(word))
    return tokens


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


# ------------------------------------------------- heuristic entity finding

def derive_entities(content: str) -> tuple[list[str], dict[str, str]]:
    """Best-effort entity extraction with no API call.

    Used as a fallback when the extractor gave no entities (LLM failure, legacy
    memories, manual ``/remember``). Deliberately conservative.
    """
    text = (content or "").strip()
    found: dict[str, tuple[str, str]] = {}

    def add(raw: str, kind: str) -> None:
        name = _trim_entity(raw)
        key = entity_key(name)
        if not key or key in GENERIC_ENTITIES or key in _STOP_ENTITY_WORDS:
            return
        found.setdefault(key, (display_name(name), kind))

    for match in _PLACE_RE.finditer(text):
        add(match.group(1), "place")
    for match in _ORG_RE.finditer(text):
        add(match.group(1), "organization")
    for match in _SUBJECT_RE.finditer(text):
        add(match.group(1), "subject")
    for match in _INTEREST_RE.finditer(text):
        add(match.group(1), "interest")

    words = re.findall(r"[A-Za-z][\w'’-]*", text)
    for word in words:
        base = re.sub(r"['’]s$", "", word.lower())
        if base in _RELATION_NOUNS:
            add(base, "person")

    for match in _PROPER_RE.finditer(text):
        word = match.group(1)
        if match.start() == 0 or word.lower() in _STOP_ENTITY_WORDS:
            continue
        if text[: match.start()].rstrip().endswith((".", "!", "?")):
            continue  # capitalised only because it starts a sentence
        if any(word.lower() in key.split() for key in found):
            continue  # already covered by a longer entity ("Business Analytics")
        add(word, "person" if re.search(r"\b(?:name is|named|called|dad|mom|father|mother|friend|brother|sister)\b", text, re.I) else "thing")

    entities = [name for name, _ in found.values()]
    types = {name: kind for name, kind in found.values()}
    return entities[:8], types


def _trim_entity(raw: str) -> str:
    words = re.split(r"\s+", raw.strip())
    while words and words[0].lower() in _LEADING_FILLERS:
        words = words[1:]
    kept: list[str] = []
    for word in words:
        if word.lower() in _ENTITY_STOPPERS:
            break
        kept.append(word)
    return " ".join(kept[:3]).strip(" .,;:!?\"'")


# ------------------------------------------------------------- the graph

@dataclass
class Entity:
    key: str
    name: str
    type: str = "thing"
    memory_ids: list[str] = field(default_factory=list)


@dataclass
class Connection:
    memory: Memory
    weight: float
    via: list[str]


class MemoryBrain:
    """Read-only graph view over a set of memories (rebuilt cheaply on demand)."""

    def __init__(self, memories: Iterable[Memory]) -> None:
        self.memories: dict[str, Memory] = {m.memory_id: m for m in memories if m.active}
        self.entities: dict[str, Entity] = {}
        self._memory_entity_keys: dict[str, list[str]] = {}
        self._adjacency: dict[str, dict[str, float]] = defaultdict(dict)
        self._via: dict[tuple[str, str], set[str]] = defaultdict(set)
        self._build()

    # -- construction
    def _build(self) -> None:
        for memory in self.memories.values():
            keys: list[str] = []
            for raw in memory.entities:
                key = entity_key(raw)
                if not key or key in GENERIC_ENTITIES or key in keys:
                    continue
                keys.append(key)
                entity = self.entities.get(key)
                if entity is None:
                    entity = Entity(key=key, name=display_name(raw), type=memory.entity_types.get(raw, "thing") or "thing")
                    self.entities[key] = entity
                elif entity.type == "thing" and memory.entity_types.get(raw):
                    entity.type = memory.entity_types[raw]
                entity.memory_ids.append(memory.memory_id)
            self._memory_entity_keys[memory.memory_id] = keys

        for entity in self.entities.values():
            ids = entity.memory_ids
            if len(ids) < 2 or len(ids) > _MAX_PAIRWISE_FANOUT:
                continue
            weight = 1.0 / (1.0 + 0.1 * (len(ids) - 2))
            for a, b in combinations(ids, 2):
                self._connect(a, b, weight, via=entity.name)

        for memory in self.memories.values():
            for other_id in memory.related:
                if other_id in self.memories and other_id != memory.memory_id:
                    self._connect(memory.memory_id, other_id, 1.5, via=None)

    def _connect(self, a: str, b: str, weight: float, *, via: str | None) -> None:
        for x, y in ((a, b), (b, a)):
            self._adjacency[x][y] = self._adjacency[x].get(y, 0.0) + weight
        if via:
            self._via[(a, b)].add(via)
            self._via[(b, a)].add(via)

    # -- queries
    def entity_keys(self, memory_id: str) -> list[str]:
        return list(self._memory_entity_keys.get(memory_id, []))

    def entity_names(self, memory: Memory) -> list[str]:
        return [self.entities[key].name for key in self._memory_entity_keys.get(memory.memory_id, []) if key in self.entities]

    def neighbors(self, memory_id: str, *, limit: int | None = None) -> list[Connection]:
        pairs = sorted(self._adjacency.get(memory_id, {}).items(), key=lambda item: item[1], reverse=True)
        connections = [
            Connection(memory=self.memories[other], weight=weight, via=sorted(self._via.get((memory_id, other), ())))
            for other, weight in pairs
            if other in self.memories
        ]
        return connections[:limit] if limit else connections

    def degree(self, memory_id: str) -> int:
        return len(self._adjacency.get(memory_id, {}))

    def find_entity(self, text: str) -> list[Entity]:
        """Entities whose name appears in ``text`` (whole-word, case-insensitive)."""
        normalized = re.sub(r"[^\w\s&+’-]", " ", (text or "").lower())
        lowered = " " + re.sub(r"\s+", " ", normalized) + " "
        hits = [entity for entity in self.entities.values() if " " + entity.key + " " in lowered]
        return sorted(hits, key=lambda entity: len(entity.key), reverse=True)

    def suggest_related(
        self,
        content: str,
        entities: list[str],
        *,
        exclude_id: str | None = None,
        limit: int = 4,
    ) -> list[str]:
        """Which existing memories should a new memory link to directly?

        Shared entities (rare ones count more) plus wording overlap; kept
        deliberately selective so the graph stays readable.
        """
        my_keys = {entity_key(name) for name in entities} - GENERIC_ENTITIES
        my_tokens = content_tokens(content)
        scored: list[tuple[float, str]] = []
        for memory_id, memory in self.memories.items():
            if memory_id == exclude_id:
                continue
            shared = my_keys & set(self._memory_entity_keys.get(memory_id, []))
            rarity = sum(1.0 / max(1, len(self.entities[key].memory_ids)) ** 0.5 for key in shared if key in self.entities)
            other_tokens = content_tokens(memory.content)
            union = my_tokens | other_tokens
            overlap = len(my_tokens & other_tokens)
            jaccard = (overlap / len(union)) if union else 0.0
            score = rarity + (jaccard if overlap >= 2 else 0.0)
            if shared or (overlap >= 2 and jaccard >= 0.3):
                scored.append((score, memory_id))
        scored.sort(reverse=True)
        return [memory_id for _, memory_id in scored[:limit]]

    # -- exports
    def stats(self) -> dict[str, int]:
        links = sum(len(v) for v in self._adjacency.values()) // 2
        return {
            "memories": len(self.memories),
            "entities": len(self.entities),
            "memory_links": links,
            "connected_memories": sum(1 for m in self.memories if self._adjacency.get(m)),
        }

    def top_entities(self, limit: int = 5) -> list[Entity]:
        return sorted(self.entities.values(), key=lambda e: (len(e.memory_ids), e.name), reverse=True)[:limit]

    def to_graph(self) -> dict[str, list[dict]]:
        """Nodes/links for the dashboard (same shape the vault produces)."""
        nodes: list[dict] = [{"id": "me", "label": USER_HUB, "kind": "me", "weight": 10}]
        links: list[dict] = []
        hubs_seen: set[str] = set()
        related_seen: set[frozenset] = set()

        for memory in self.memories.values():
            hub = hub_for_category(memory.category)
            hub_id = f"hub:{hub.lower()}"
            if hub_id not in hubs_seen:
                hubs_seen.add(hub_id)
                nodes.append({"id": hub_id, "label": hub, "kind": "hub", "weight": 6})
                links.append({"source": "me", "target": hub_id, "kind": "hub"})
            label = memory.title or _clip(memory.content, 34)
            nodes.append({"id": f"m:{memory.memory_id}", "label": label, "kind": "memory", "group": memory.category, "weight": 3})
            links.append({"source": hub_id, "target": f"m:{memory.memory_id}", "kind": "category"})
            for key in self._memory_entity_keys.get(memory.memory_id, []):
                links.append({"source": f"m:{memory.memory_id}", "target": f"e:{key}", "kind": "entity"})
            for other_id in memory.related:
                pair = frozenset((memory.memory_id, other_id))
                if other_id in self.memories and other_id != memory.memory_id and pair not in related_seen:
                    related_seen.add(pair)
                    links.append({"source": f"m:{memory.memory_id}", "target": f"m:{other_id}", "kind": "related"})

        for entity in self.entities.values():
            nodes.append({"id": f"e:{entity.key}", "label": entity.name, "kind": "entity", "group": entity.type, "weight": 2 + len(entity.memory_ids)})
        return {"nodes": nodes, "links": links}


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ----------------------------------------------------------------- layout

def layout_graph(graph: dict[str, list[dict]], *, iterations: int = 40) -> dict[str, list[dict]]:
    """Give every node in ``MemoryBrain.to_graph()`` an ``x``/``y`` in [0, 1].

    Radial, deterministic and cheap: ``Me`` in the centre, category hubs on a ring around it, each
    hub's memories fanned out beyond it, and entities pushed outwards toward the memories that
    mention them. A few repulsion passes stop labels piling up. Computed once per change, so the
    dashboard can draw a still picture instead of running a physics animation.
    """
    import math

    nodes = {n["id"]: n for n in graph["nodes"]}
    pos: dict[str, list[float]] = {"me": [0.5, 0.5]}

    hubs = [n for n in graph["nodes"] if n["kind"] == "hub"]
    for i, hub in enumerate(hubs):
        angle = 2 * math.pi * i / max(1, len(hubs)) - math.pi / 2
        pos[hub["id"]] = [0.5 + 0.2 * math.cos(angle), 0.5 + 0.2 * math.sin(angle)]
        hub["_angle"] = angle

    members: dict[str, list[str]] = {}
    for link in graph["links"]:
        if link["kind"] == "category":
            members.setdefault(link["source"], []).append(link["target"])
    span = 2 * math.pi / max(1, len(hubs))
    for hub in hubs:
        ids = members.get(hub["id"], [])
        for j, memory_id in enumerate(ids):
            offset = (j - (len(ids) - 1) / 2) * min(span * 0.85 / max(1, len(ids)), 0.5)
            radius = 0.34 + 0.05 * (j % 2)
            angle = hub["_angle"] + offset
            pos[memory_id] = [0.5 + radius * math.cos(angle), 0.5 + radius * math.sin(angle)]

    linked: dict[str, list[str]] = {}
    for link in graph["links"]:
        if link["kind"] == "entity":
            linked.setdefault(link["target"], []).append(link["source"])
    for i, (entity_id, memory_ids) in enumerate(sorted(linked.items())):
        points = [pos[m] for m in memory_ids if m in pos]
        if points:
            cx = sum(p[0] for p in points) / len(points) - 0.5
            cy = sum(p[1] for p in points) / len(points) - 0.5
            norm = math.hypot(cx, cy) or 1.0
            pos[entity_id] = [0.5 + cx / norm * 0.47, 0.5 + cy / norm * 0.47]
        else:
            angle = 2 * math.pi * i / max(1, len(linked))
            pos[entity_id] = [0.5 + 0.47 * math.cos(angle), 0.5 + 0.47 * math.sin(angle)]

    ids = [i for i in pos if i != "me"]
    for _ in range(iterations):
        for a_index, a in enumerate(ids):
            for b in ids[a_index + 1:]:
                dx, dy = pos[a][0] - pos[b][0], pos[a][1] - pos[b][1]
                dist = math.hypot(dx, dy)
                if 0 < dist < 0.05:
                    push = (0.05 - dist) / 2 / dist
                    pos[a][0] += dx * push; pos[a][1] += dy * push
                    pos[b][0] -= dx * push; pos[b][1] -= dy * push
        for i in ids:
            pos[i][0] = min(0.95, max(0.05, pos[i][0]))
            pos[i][1] = min(0.95, max(0.05, pos[i][1]))

    for node_id, node in nodes.items():
        x, y = pos.get(node_id, [0.5, 0.5])
        node["x"], node["y"] = round(x, 4), round(y, 4)
        node.pop("_angle", None)
    return graph
