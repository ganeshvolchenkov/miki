from __future__ import annotations

import json
import logging
import re
import uuid
import zipfile
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.memory.brain import CATEGORY_HUBS, USER_HUB, Entity, MemoryBrain, hub_for_category, safe_note_name
from app.memory.memory import MEMORY_UPDATABLE_FIELDS, Memory, MemoryStore, utc_now

logger = logging.getLogger(__name__)

BRAIN_NOTE = "Brain"
# Sections Miki generates; anything else a user adds to a note is preserved.
_GENERATED_SECTIONS = {"summary", "connections", "metadata"}
_VERB_FORMS = {
    "like": "Likes", "love": "Loves", "enjoy": "Enjoys", "prefer": "Prefers", "hate": "Hates", "dislike": "Dislikes",
}


# Obsidian's graph colours nodes by the first matching "color group", so order matters: the specific
# groups come first and the broad fallbacks (#entity, #memory) last. Every query is a tag our own notes
# already carry (memory notes: #<category>; entities: #<type>; hubs: #me/#hub; journal: #journal).
GRAPH_COLOR_GROUPS: list[tuple[str, int, str, str]] = [
    # (query, 0xRRGGBB, emoji for the legend, meaning)
    ("tag:#me", 0xFF5B2E, "🟠", "Me (the centre)"),
    ("tag:#profile", 0xFFFFFF, "⚪", "Profile / Brain overview"),
    ("tag:#brain", 0xFFFFFF, "⚪", "Profile / Brain overview"),
    ("tag:#hub", 0xECE8DF, "⬜", "Category hubs"),
    ("tag:#journal", 0x8899AA, "🔘", "Daily journal"),
    ("tag:#person", 0xFF7EB6, "🩷", "People"),
    ("tag:#place", 0x7BD88F, "🟢", "Places"),
    ("tag:#organization", 0x4D96FF, "🔵", "Organisations"),
    ("tag:#interest", 0xF5B83D, "🟡", "Interests & activities"),
    ("tag:#subject", 0x2EC4B6, "🩵", "Subjects"),
    ("tag:#event", 0x4DD0E1, "🩵", "Events"),
    ("tag:#object", 0xA0A8B0, "⚫", "Objects"),
    ("tag:#entity", 0xA0A8B0, "⚫", "Other entities"),
    ("tag:#identity", 0xFF4D4D, "🔴", "Memory: identity"),
    ("tag:#relationship", 0xFF9EC7, "🌸", "Memory: relationships"),
    ("tag:#preference", 0xFF9F1C, "🟠", "Memory: preferences"),
    ("tag:#habit", 0xFFD60A, "🟨", "Memory: habits & routines"),
    ("tag:#routine", 0xFFD60A, "🟨", "Memory: habits & routines"),
    ("tag:#goal", 0x2ECC71, "🟩", "Memory: goals"),
    ("tag:#project", 0x4D96FF, "🟦", "Memory: projects"),
    ("tag:#skill", 0xB388FF, "🟪", "Memory: skills"),
    ("tag:#constraint", 0x9AA0A6, "⬛", "Memory: constraints"),
    ("tag:#event", 0x4DD0E1, "🩵", "Memory: events"),
    ("tag:#fact", 0xCFD8DC, "⬜", "Memory: facts"),
    ("tag:#memory", 0xCFD8DC, "⬜", "Memory: other"),
]


@dataclass(frozen=True)
class ObsidianVaultInfo:
    vault_path: Path
    miki_dir: Path

    @property
    def vault_name(self) -> str:
        return self.vault_path.name

    @property
    def display_miki_dir(self) -> str:
        return "Miki/"


@dataclass
class _GraphContext:
    """Everything needed to render wikilinks consistently for one pass."""

    memories: dict[str, Memory]  # active + archived
    brain: MemoryBrain  # active only
    stems: dict[str, str]  # memory_id -> note name
    entity_stems: dict[str, str]  # entity key -> note name
    hub_names: set[str]


class ObsidianMemoryStore(MemoryStore):
    """Filesystem-backed memory store that keeps Miki notes inside an Obsidian vault.

    Besides one note per memory, it maintains a generated graph: entity notes
    (people, places, interests...), category hubs, a ``Me`` hub and a ``Brain``
    overview, all connected with ``[[wikilinks]]`` so Obsidian's graph view
    shows how what Miki knows fits together.
    """

    ACTIVE_CATEGORY_DIRS = {
        "identity": "Identity",
        "preference": "Preferences",
        "habit": "Habits",
        "fact": "Facts",
        "general": "General",
    }

    def __init__(self, vault_path: str | Path) -> None:
        self.vault_path = Path(vault_path).expanduser()
        if not self.vault_path.exists():
            raise ValueError("Configured OBSIDIAN_VAULT_PATH does not exist.")
        self.miki_dir = self.vault_path / "Miki"
        self.memories_dir = self.miki_dir / "Memories"
        self.archive_dir = self.memories_dir / "Archive"
        self.graph_dir = self.miki_dir / "Graph"
        self.entities_dir = self.graph_dir / "Entities"
        self.hubs_dir = self.graph_dir / "Hubs"
        self.journal_dir = self.miki_dir / "Journal"
        self._memory_path_index: dict[str, Path] = {}
        self._rebuilding = False
        self._ensure_structure()
        self._refresh_memory_index()

    def _ensure_structure(self) -> None:
        for path in [
            self.miki_dir,
            self.memories_dir,
            self.archive_dir,
            self.graph_dir,
            self.entities_dir,
            self.hubs_dir,
            self.journal_dir,
            *(self.memories_dir / folder for folder in self.ACTIVE_CATEGORY_DIRS.values()),
        ]:
            path.mkdir(parents=True, exist_ok=True)

    def is_connected(self) -> bool:
        return self.vault_path.exists() and self.vault_path.is_dir()

    def describe(self) -> ObsidianVaultInfo:
        return ObsidianVaultInfo(vault_path=self.vault_path, miki_dir=self.miki_dir)

    # ------------------------------------------------------------------ CRUD
    def create_memory(
        self,
        *,
        content: str,
        category: str = "general",
        memory_type: str = "fact",
        confidence: float = 0.5,
        source: str = "conversation",
        importance: float = 0.5,
        stability: float = 0.5,
        usefulness: float = 0.5,
        title: str = "",
        entities: list[str] | None = None,
        entity_types: dict[str, str] | None = None,
        related: list[str] | None = None,
    ) -> Memory:
        if not content or not content.strip():
            raise ValueError("Memory content cannot be empty.")

        category = category.lower().strip() or "general"
        normalized = self._normalize_content(content)
        existing = self._find_duplicate_memory(normalized, category)
        if existing is not None:
            return existing

        memory = Memory.from_dict(
            {
                "memory_id": uuid.uuid4().hex[:12],
                "content": content.strip(),
                "category": category,
                "memory_type": memory_type,
                "confidence": max(0.0, min(1.0, float(confidence))),
                "source": source,
                "importance": max(0.0, min(1.0, float(importance))),
                "stability": max(0.0, min(1.0, float(stability))),
                "usefulness": max(0.0, min(1.0, float(usefulness))),
                "title": title or "",
                "entities": entities or [],
                "entity_types": entity_types or {},
                "related": related or [],
            }
        )
        self._write_memory(memory)
        return memory

    def get_memory(self, memory_id: str) -> Memory | None:
        path = self._find_memory_path(memory_id)
        if path is None:
            return None
        return self._read_memory_file(path)

    def list_memories(self, *, active_only: bool = True) -> list[Memory]:
        memories: list[Memory] = []
        for path in self._iter_memory_files(active_only=active_only):
            memory = self._read_memory_file(path)
            if memory is not None and (not active_only or memory.active):
                memories.append(memory)
        memories.sort(key=lambda item: item.created_at, reverse=True)
        return memories

    def update_memory(self, memory_id: str, **updates: Any) -> Memory | None:
        path = self._find_memory_path(memory_id)
        if path is None:
            return None

        current = self._read_memory_file(path)
        if current is None:
            return None

        payload = current.to_dict()
        for key, value in updates.items():
            if key in MEMORY_UPDATABLE_FIELDS:
                payload[key] = value
        payload["updated_at"] = utc_now()

        updated = Memory.from_dict(payload)
        self._write_memory(updated, existing_path=path)
        return updated

    def delete_memory(self, memory_id: str) -> Memory | None:
        path = self._find_memory_path(memory_id)
        if path is None:
            return None

        memory = self._read_memory_file(path)
        if memory is None:
            return None

        updated = replace(memory, active=False, updated_at=utc_now())
        self._write_memory(updated, existing_path=path)
        return updated

    def count_memories(self, *, active_only: bool = True) -> int:
        return len(self.list_memories(active_only=active_only))

    def list_note_paths(self, *, active_only: bool = True) -> list[Path]:
        return list(self._iter_memory_files(active_only=active_only))

    def get_note_path(self, memory_id: str) -> Path | None:
        """Public accessor for the Markdown note backing a given memory_id."""
        return self._find_memory_path(memory_id)

    def sync(self) -> dict[str, int]:
        self._ensure_structure()
        self._refresh_memory_index()
        active = self.count_memories(active_only=True)
        inactive = self.count_memories(active_only=False) - active
        return {"active": active, "inactive": inactive, "total": active + inactive}

    def refresh(self) -> None:
        """Re-scan the vault and rebuild the memory_id -> note path index."""
        self._refresh_memory_index()

    def backup(self, destination_dir: str | Path = "data/backups") -> Path:
        """Zip the whole ``Miki/`` folder so a graph rebuild can always be undone."""
        destination = Path(destination_dir)
        destination.mkdir(parents=True, exist_ok=True)
        archive = destination / f"vault-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in self.miki_dir.rglob("*"):
                if path.is_file():
                    bundle.write(path, path.relative_to(self.vault_path).as_posix())
        return archive

    # ------------------------------------------------------------ graph pass
    def rebuild_graph(self) -> dict[str, int]:
        """Regenerate every memory note's links plus all entity/hub/brain notes.

        Idempotent and cheap to re-run: files whose content did not change are
        left untouched, and sections a user added to a note by hand are kept.
        """
        self._ensure_structure()
        self._rebuilding = True
        try:
            context = self._build_context()
            rewritten = 0
            for memory in context.memories.values():
                if self._write_memory(memory, existing_path=self._find_memory_path(memory.memory_id), context=context, refresh_graph=False):
                    rewritten += 1
            counts = self._write_graph_notes(context)
            stubs = self._remove_shadow_stubs()
            self._refresh_memory_index()
        finally:
            self._rebuilding = False
        return {"notes_rewritten": rewritten, "stubs_removed": stubs, **counts}

    def write_graph_colors(self) -> bool:
        """Colour the Obsidian graph by kind (people, places, interests, memory categories...).

        Only ``colorGroups`` in ``.obsidian/graph.json`` is touched: the user's other graph settings
        are preserved, and any colour groups they added themselves are kept after ours. Returns True
        if the file changed. (If Obsidian is open, reopen the graph view to see the new colours.)
        """
        import json

        config_dir = self.vault_path / ".obsidian"
        path = config_dir / "graph.json"
        try:
            config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(config, dict):
                config = {}
        except (OSError, ValueError):
            return False  # unreadable/corrupt user config: leave it alone

        ours = []
        seen: set[str] = set()
        for query, rgb, _emoji, _meaning in GRAPH_COLOR_GROUPS:
            if query not in seen:
                seen.add(query)
                ours.append({"query": query, "color": {"a": 1, "rgb": rgb}})
        theirs = [g for g in config.get("colorGroups", []) if isinstance(g, dict) and g.get("query") not in seen]
        updated = {**config, "colorGroups": ours + theirs}
        if updated == config:
            return False
        try:
            config_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(updated, indent=2), encoding="utf-8")
        except OSError:
            logger.warning("Could not write graph colours", exc_info=True)
            return False
        return True

    def journal_exists(self, iso_day: str) -> bool:
        return (self.journal_dir / f"{iso_day}.md").exists()

    def write_journal_note(
        self,
        iso_day: str,
        *,
        summary: str,
        topics: list[str],
        open_loops: list[str],
        message_count: int = 0,
    ) -> Path:
        """One note per day in ``Miki/Journal/``, linked to the people/interests it mentions."""
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        context = self._build_context()
        mentioned = context.brain.find_entity(" ".join([summary, *topics, *open_loops]))
        lines = [
            "---",
            "type: journal",
            f"date: {iso_day}",
            f"messages: {message_count}",
            "tags: [journal]",
            "---",
            "",
            f"# {iso_day}",
            "",
            "## Summary",
            "",
            summary or "(nothing notable)",
            "",
        ]
        if topics:
            lines.extend(["## Topics", ""] + [f"- {topic}" for topic in topics] + [""])
        if open_loops:
            lines.extend(["## Open loops", ""] + [f"- [ ] {item}" for item in open_loops] + [""])
        links = [f"[[{context.entity_stems[e.key]}]]" for e in mentioned if e.key in context.entity_stems]
        if links:
            lines.extend(["## Mentions", "", ", ".join(dict.fromkeys(links)), ""])
        path = self.journal_dir / f"{iso_day}.md"
        self._write_if_changed(path, "\n".join(lines))
        return path

    def write_profile_note(self, profile: dict[str, Any]) -> Path:
        """Write ``Miki/Graph/Profile.md``: the portrait plus how well each area of life is known."""
        context = self._build_context()
        lines = [
            "---",
            "type: hub",
            "tags: [hub, profile]",
            f"memories_considered: {profile.get('memory_count', 0)}",
            f"generated_at: {profile.get('generated_at', '')}",
            "---",
            "",
            "# Profile",
            "",
            f"Part of [[{USER_HUB}]].",
            "",
        ]
        portrait = str(profile.get("portrait", "")).strip()
        if portrait:
            lines.extend(["## Who you are", "", f"> {portrait}", ""])

        domains = [d for d in profile.get("domains", []) if isinstance(d, dict)]
        if domains:
            coverage = sum(float(d.get("confidence", 0)) for d in domains) / len(domains)
            lines.extend([f"## How well I know you ({coverage:.0%})", "", "| Area | Known | Summary |", "| --- | --- | --- |"])
            for domain in domains:
                confidence = max(0.0, min(1.0, float(domain.get("confidence", 0))))
                bar = "▰" * round(confidence * 5) + "▱" * (5 - round(confidence * 5))
                lines.append(f"| {domain.get('title', '')} | {bar} {confidence:.0%} | {str(domain.get('summary', '')).replace('|', '/') or '—'} |")
            lines.append("")
            gaps = [(d.get("title", ""), g) for d in domains for g in d.get("gaps", [])]
            if gaps:
                lines.extend(["## Still unknown", ""])
                lines.extend(f"- **{title}:** {gap}" for title, gap in gaps[:15])
                lines.append("")

        questions = [str(q) for q in profile.get("next_questions", []) if str(q).strip()]
        if questions:
            lines.extend(["## Questions I'd like to ask you", ""])
            lines.extend(f"- {q}" for q in questions)
            lines.append("")

        top = context.brain.top_entities(8)
        if top:
            lines.extend(["## Key people & interests", ""])
            lines.extend(f"- [[{context.entity_stems[e.key]}]]" for e in top if e.key in context.entity_stems)
            lines.append("")

        path = self.graph_dir / "Profile.md"
        self._write_if_changed(path, "\n".join(lines))
        return path

    def _remove_shadow_stubs(self) -> int:
        """Delete empty notes that duplicate a real note's name.

        Clicking a link whose target doesn't exist makes Obsidian create an empty note, which then
        shows up as a second, uncoloured node with the same name. Only files that are completely empty
        AND shadow an existing non-empty note are removed; anything with content is left alone.
        """
        real: set[str] = set()
        empty: list[Path] = []
        for path in self.miki_dir.rglob("*.md"):
            try:
                if path.read_text(encoding="utf-8").strip():
                    real.add(path.stem.lower())
                else:
                    empty.append(path)
            except OSError:
                continue
        removed = 0
        for path in empty:
            if path.stem.lower() in real:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    logger.warning("Could not remove stub %s", path)
        return removed

    def _build_context(self, override: Memory | None = None) -> _GraphContext:
        memories = {m.memory_id: m for m in self.list_memories(active_only=False)}
        if override is not None:
            memories[override.memory_id] = override

        brain = MemoryBrain(m for m in memories.values() if m.active)

        hub_names = {safe_note_name(name).lower() for name in CATEGORY_HUBS.values()} | {USER_HUB.lower(), BRAIN_NOTE.lower()}
        entity_stems: dict[str, str] = {}
        used = set(hub_names)
        for key, entity in sorted(brain.entities.items()):
            stem = safe_note_name(entity.name)
            if stem.lower() in used:
                stem = f"{stem} (entity)"
            used.add(stem.lower())
            entity_stems[key] = stem

        stems: dict[str, str] = {}
        # Active notes claim plain names first; archived ones take the suffix on a clash.
        for memory in sorted(memories.values(), key=lambda m: (not m.active, m.created_at, m.memory_id)):
            base = safe_note_name(self._display_title(memory))
            candidate = base if base.lower() not in used else f"{base} ({memory.memory_id[:6]})"
            used.add(candidate.lower())
            stems[memory.memory_id] = candidate

        return _GraphContext(memories=memories, brain=brain, stems=stems, entity_stems=entity_stems, hub_names=hub_names)

    def _write_graph_notes(self, context: _GraphContext) -> dict[str, int]:
        brain = context.brain
        active = [m for m in context.memories.values() if m.active]

        self._match_case(self.entities_dir, {f"{stem}.md" for stem in context.entity_stems.values()})
        expected_entities: set[str] = set()
        for key, entity in brain.entities.items():
            stem = context.entity_stems[key]
            expected_entities.add(f"{stem}.md")
            self._write_if_changed(self.entities_dir / f"{stem}.md", self._render_entity_note(entity, context))
        self._prune(self.entities_dir, expected_entities, marker="type: entity")

        by_hub: dict[str, list[Memory]] = {}
        for memory in active:
            by_hub.setdefault(hub_for_category(memory.category), []).append(memory)

        self._match_case(self.hubs_dir, {f"{safe_note_name(hub)}.md" for hub in by_hub} | {f"{USER_HUB}.md"})
        expected_hubs: set[str] = set()
        for hub, members in by_hub.items():
            stem = safe_note_name(hub)
            expected_hubs.add(f"{stem}.md")
            self._write_if_changed(self.hubs_dir / f"{stem}.md", self._render_category_hub(hub, members, context))
        expected_hubs.add(f"{USER_HUB}.md")
        self._write_if_changed(self.hubs_dir / f"{USER_HUB}.md", self._render_me_hub(by_hub, context))
        self._prune(self.hubs_dir, expected_hubs, marker="type: hub")

        self._write_if_changed(self.graph_dir / f"{BRAIN_NOTE}.md", self._render_brain_note(context, len(by_hub)))
        self.write_graph_colors()
        return {"entity_notes": len(expected_entities), "hub_notes": len(expected_hubs)}

    # ---------------------------------------------------------- note writing
    def _write_memory(
        self,
        memory: Memory,
        existing_path: Path | None = None,
        *,
        context: _GraphContext | None = None,
        refresh_graph: bool = True,
    ) -> bool:
        """Write a memory note. Returns True if the file was created/changed."""
        context = context or self._build_context(override=memory)
        folder = self._folder_for_memory(memory)
        folder.mkdir(parents=True, exist_ok=True)

        target_path = folder / f"{context.stems[memory.memory_id]}.md"
        # Compare names as strings: on Windows Path equality ignores case, so 'Project Plan.md' vs
        # 'Project plan.md' would look unchanged and the file would keep the stale capitalisation.
        renamed = (
            existing_path is not None
            and existing_path.exists()
            and (existing_path.parent != target_path.parent or existing_path.name != target_path.name)
        )

        user_sections = self._read_user_sections(existing_path) if existing_path is not None and existing_path.exists() else ""
        text = self._render_markdown(memory, context) + user_sections

        changed = True
        if not renamed and target_path.exists():
            try:
                changed = target_path.read_text(encoding="utf-8") != text
            except OSError:
                changed = True

        if renamed:
            if existing_path.parent == target_path.parent and existing_path.name.lower() == target_path.name.lower():
                existing_path.rename(target_path)  # case-only change: the same file on a case-insensitive disk
            else:
                existing_path.unlink()
        if changed:
            target_path.write_text(text, encoding="utf-8")
            logger.info("Saved Obsidian memory note %s", memory.memory_id)
        self._memory_path_index[memory.memory_id] = target_path

        if refresh_graph:
            if renamed and not self._rebuilding:
                # Other notes may link to the old note name; regenerate everything.
                self.rebuild_graph()
            else:
                self._write_graph_notes(context)
        return changed

    def _folder_for_memory(self, memory: Memory) -> Path:
        if not memory.active:
            return self.archive_dir / self._category_dirname(memory.category)
        return self.memories_dir / self._category_dirname(memory.category)

    def _category_dirname(self, category: str) -> str:
        return self.ACTIVE_CATEGORY_DIRS.get(category.lower().strip(), "General")

    def _display_title(self, memory: Memory) -> str:
        return memory.title.strip() or self._semantic_title(memory)

    # ------------------------------------------------------------- rendering
    def _render_markdown(self, memory: Memory, context: _GraphContext) -> str:
        title = self._display_title(memory)
        status = "active" if memory.active else "inactive"
        confidence_percent = f"{memory.confidence:.0%}"
        tags = self._tags_for_memory(memory)
        aliases_value = ", ".join(f'"{alias}"' for alias in tags)
        note_tags = ["memory", memory.category, *(re.sub(r"[^a-z0-9_-]+", "-", e.lower()).strip("-") for e in memory.entities[:4])]
        frontmatter = [
            "---",
            f"memory_id: {memory.memory_id}",
            f"title: {json.dumps(title, ensure_ascii=False)}",
            f"category: {memory.category}",
            f"memory_type: {memory.memory_type}",
            f"confidence: {memory.confidence:.4f}",
            f"importance: {memory.importance:.4f}",
            f"stability: {memory.stability:.4f}",
            f"usefulness: {memory.usefulness:.4f}",
            f"source: {memory.source}",
            f"created_at: {memory.created_at}",
            f"updated_at: {memory.updated_at}",
            f"last_confirmed_at: {memory.last_confirmed_at}",
            f"status: {status}",
            f"active: {str(memory.active).lower()}",
            f"aliases: [{aliases_value}]",
            f"tags: [{', '.join(dict.fromkeys(t for t in note_tags if t))}]",
            f"entities: {json.dumps(memory.entities, ensure_ascii=False)}",
            f"entity_types: {json.dumps(memory.entity_types, ensure_ascii=False)}",
            f"related: {json.dumps(memory.related, ensure_ascii=False)}",
            "---",
            "",
            f"# {title}",
            "",
            "## Summary",
            "",
            f"> {memory.content.strip()}",
            "",
        ]
        frontmatter.extend(self._render_connections(memory, context))
        frontmatter.extend(
            [
                "## Metadata",
                "",
                "| Field | Value |",
                "| --- | --- |",
                f"| Memory ID | `{memory.memory_id}` |",
                f"| Category | `{memory.category}` |",
                f"| Memory Type | `{memory.memory_type}` |",
                f"| Confidence | `{confidence_percent}` |",
                f"| Importance | `{memory.importance:.0%}` |",
                f"| Stability | `{memory.stability:.0%}` |",
                f"| Usefulness | `{memory.usefulness:.0%}` |",
                f"| Source | `{memory.source}` |",
                f"| Status | `{status}` |",
                f"| Created At | `{memory.created_at}` |",
                f"| Updated At | `{memory.updated_at}` |",
                f"| Last Confirmed At | `{memory.last_confirmed_at}` |",
                "",
            ]
        )
        return "\n".join(frontmatter)

    def _render_connections(self, memory: Memory, context: _GraphContext) -> list[str]:
        if not memory.active:
            return []
        lines = ["## Connections", "", f"- **Part of:** [[{safe_note_name(hub_for_category(memory.category))}]]"]

        entity_links = [
            f"[[{context.entity_stems[key]}]]"
            for key in context.brain.entity_keys(memory.memory_id)
            if key in context.entity_stems
        ]
        if entity_links:
            lines.append(f"- **About:** {', '.join(entity_links)}")

        related_links = []
        for connection in context.brain.neighbors(memory.memory_id, limit=6):
            stem = context.stems.get(connection.memory.memory_id)
            if stem:
                related_links.append(f"[[{stem}]]")
        if related_links:
            lines.append(f"- **Related:** {', '.join(related_links)}")
        lines.append("")
        return lines

    def _render_entity_note(self, entity: Entity, context: _GraphContext) -> str:
        members = [context.memories[i] for i in entity.memory_ids if i in context.memories]
        count = len(members)
        co_mentioned: Counter[str] = Counter()
        for member in members:
            for key in context.brain.entity_keys(member.memory_id):
                if key != entity.key and key in context.entity_stems:
                    co_mentioned[key] += 1

        lines = [
            "---",
            "type: entity",
            f"entity_type: {entity.type}",
            f"tags: [entity, {re.sub(r'[^a-z0-9_-]+', '-', entity.type.lower()).strip('-') or 'thing'}]",
            "---",
            "",
            f"# {entity.name}",
            "",
            f"> {entity.type.title()} · mentioned in {count} memor{'y' if count == 1 else 'ies'}",
            "",
            "## Memories",
            "",
        ]
        for member in sorted(members, key=lambda m: m.created_at):
            lines.append(f"- [[{context.stems[member.memory_id]}]] — {member.content.strip()}")
        if co_mentioned:
            lines.extend(["", "## Connected to", ""])
            for key, _ in co_mentioned.most_common(8):
                lines.append(f"- [[{context.entity_stems[key]}]]")
        lines.append("")
        return "\n".join(lines)

    def _render_category_hub(self, hub: str, members: list[Memory], context: _GraphContext) -> str:
        lines = [
            "---",
            "type: hub",
            "tags: [hub, category]",
            "---",
            "",
            f"# {hub}",
            "",
            f"Part of [[{USER_HUB}]].",
            "",
        ]
        for member in sorted(members, key=lambda m: m.created_at):
            lines.append(f"- [[{context.stems[member.memory_id]}]] — {member.content.strip()}")
        lines.append("")
        return "\n".join(lines)

    def _render_me_hub(self, by_hub: dict[str, list[Memory]], context: _GraphContext) -> str:
        lines = [
            "---",
            "type: hub",
            "tags: [hub, me]",
            "---",
            "",
            f"# {USER_HUB}",
            "",
            "> Everything Miki knows about you, as one connected graph.",
            "",
            "## Categories",
            "",
        ]
        if (self.graph_dir / "Profile.md").exists():
            lines.insert(lines.index("## Categories"), f"Portrait and what I still want to learn: [[Profile]]\n")
        for hub, members in sorted(by_hub.items()):
            lines.append(f"- [[{safe_note_name(hub)}]] · {len(members)}")
        top = context.brain.top_entities(12)
        if top:
            lines.extend(["", "## Most connected", ""])
            for entity in top:
                stem = context.entity_stems.get(entity.key)
                if stem:
                    lines.append(f"- [[{stem}]] · {len(entity.memory_ids)}")
        lines.append("")
        return "\n".join(lines)

    def _render_brain_note(self, context: _GraphContext, categories: int) -> str:
        stats = context.brain.stats()
        return "\n".join(
            [
                "---",
                "type: hub",
                "tags: [hub, brain]",
                "---",
                "",
                "# Brain",
                "",
                "Generated by Miki. Open the graph view to see how memories connect.",
                "",
                f"- Memories: {stats['memories']}",
                f"- Entities: {stats['entities']}",
                f"- Links between memories: {stats['memory_links']}",
                f"- Categories: {categories}",
                f"- Last updated: {date.today().isoformat()}",
                "",
                "## Graph colours",
                "",
                *self._legend_lines(),
                "",
                f"Start at [[{USER_HUB}]].",
                "",
            ]
        )

    @staticmethod
    def _legend_lines() -> list[str]:
        seen: set[str] = set()
        lines = []
        for _query, _rgb, emoji, meaning in GRAPH_COLOR_GROUPS:
            if meaning not in seen:
                seen.add(meaning)
                lines.append(f"- {emoji} {meaning}")
        return lines

    @staticmethod
    def _write_if_changed(path: Path, text: str) -> None:
        try:
            if path.exists() and path.read_text(encoding="utf-8") == text:
                return
        except OSError:
            pass
        path.write_text(text, encoding="utf-8")

    @staticmethod
    def _match_case(directory: Path, wanted: set[str]) -> None:
        """Rename generated notes whose capitalisation drifted (e.g. 'Uva.md' -> 'UvA.md')."""
        by_lower = {name.lower(): name for name in wanted}
        for path in directory.glob("*.md"):
            target = by_lower.get(path.name.lower())
            if target is not None and target != path.name:
                try:
                    path.rename(path.with_name(target))
                except OSError:
                    logger.warning("Could not fix the capitalisation of %s", path)

    @staticmethod
    def _prune(directory: Path, keep: set[str], *, marker: str) -> None:
        """Delete stale generated notes (only files carrying Miki's marker)."""
        keep_lower = {name.lower() for name in keep}
        for path in directory.glob("*.md"):
            if path.name.lower() in keep_lower:
                continue
            try:
                if marker in path.read_text(encoding="utf-8")[:300]:
                    path.unlink()
            except OSError:
                logger.warning("Could not prune %s", path)

    # -------------------------------------------------------------- reading
    def _iter_memory_files(self, *, active_only: bool) -> list[Path]:
        roots = [self.memories_dir / folder for folder in self.ACTIVE_CATEGORY_DIRS.values()]
        if not active_only:
            roots.extend(self.archive_dir / folder for folder in self.ACTIVE_CATEGORY_DIRS.values())

        files: list[Path] = []
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*.md"):
                if path.is_file():
                    files.append(path)
        return files

    def _find_duplicate_memory(self, normalized_content: str, category: str) -> Memory | None:
        for memory in self.list_memories(active_only=True):
            if memory.category == category and self._normalize_content(memory.content) == normalized_content:
                return memory
        return None

    def _find_memory_path(self, memory_id: str) -> Path | None:
        cached = self._memory_path_index.get(memory_id)
        if cached is not None and cached.exists():
            memory = self._read_memory_file(cached)
            if memory is not None and memory.memory_id == memory_id:
                return cached

        for path in self._iter_memory_files(active_only=False):
            memory = self._read_memory_file(path)
            if memory is not None and memory.memory_id == memory_id:
                self._memory_path_index[memory_id] = path
                return path

        self._memory_path_index.pop(memory_id, None)
        return None

    def _read_memory_file(self, path: Path) -> Memory | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            logger.exception("Could not read Obsidian memory note")
            return None

        metadata, body = self._parse_markdown(raw)
        memory_id = str(metadata.get("memory_id", "")).strip()
        if not memory_id:
            return None

        content = self._extract_body_content(body)
        status = str(metadata.get("status", "active")).strip().lower()
        active = status != "inactive"
        if "active" in metadata:
            active = str(metadata.get("active")).strip().lower() not in {"false", "0", "no"}

        updated_at = str(metadata.get("updated_at", utc_now()))
        return Memory.from_dict(
            {
                "memory_id": memory_id,
                "content": content,
                "category": str(metadata.get("category", "general")).strip().lower() or "general",
                "memory_type": str(metadata.get("memory_type", "fact")),
                "confidence": self._parse_float(metadata.get("confidence"), default=0.5),
                "source": str(metadata.get("source", "unknown")),
                "created_at": str(metadata.get("created_at", utc_now())),
                "updated_at": updated_at,
                "active": active,
                "importance": self._parse_float(metadata.get("importance"), default=0.5),
                "stability": self._parse_float(metadata.get("stability"), default=0.5),
                "usefulness": self._parse_float(metadata.get("usefulness"), default=0.5),
                "last_confirmed_at": str(metadata.get("last_confirmed_at") or updated_at),
                "title": self._parse_scalar(metadata.get("title")),
                "entities": self._parse_json(metadata.get("entities"), []),
                "entity_types": self._parse_json(metadata.get("entity_types"), {}),
                "related": self._parse_json(metadata.get("related"), []),
            }
        )

    def _read_user_sections(self, path: Path) -> str:
        """Sections in an existing note that Miki did not generate (user additions)."""
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return ""
        _, body = self._parse_markdown(raw)
        kept: list[str] = []
        keep = False
        for line in body.splitlines():
            if line.startswith("## "):
                keep = line[3:].strip().lower() not in _GENERATED_SECTIONS
            elif line.startswith("# "):
                keep = False
            if keep:
                kept.append(line)
        text = "\n".join(kept).strip()
        return f"\n{text}\n" if text else ""

    def _refresh_memory_index(self) -> None:
        index: dict[str, Path] = {}
        for path in self._iter_memory_files(active_only=False):
            memory = self._read_memory_file(path)
            if memory is not None:
                index[memory.memory_id] = path
        self._memory_path_index = index

    def _parse_markdown(self, text: str) -> tuple[dict[str, Any], str]:
        if not text.startswith("---"):
            return {}, text

        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return {}, text

        frontmatter: dict[str, Any] = {}
        body_start = 0
        for index in range(1, len(lines)):
            line = lines[index]
            if line.strip() == "---":
                body_start = index + 1
                break
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            frontmatter[key.strip()] = value.strip()

        body = "\n".join(lines[body_start:])
        return frontmatter, body

    def _extract_body_content(self, body: str) -> str:
        text = body.strip()
        if not text:
            return ""

        lines = text.splitlines()
        if lines and lines[0].startswith("#"):
            lines = lines[1:]

        summary_lines: list[str] = []
        in_summary = False
        for line in lines:
            stripped = line.strip()
            if stripped.lower() == "## summary":
                in_summary = True
                summary_lines = []
                continue
            if stripped.startswith("## ") and in_summary:
                break
            if in_summary:
                if stripped.startswith(">"):
                    summary_lines.append(stripped.lstrip("> ").strip())
                elif stripped == "":
                    continue
                elif summary_lines:
                    summary_lines.append(stripped)

        if summary_lines:
            return " ".join(part for part in summary_lines if part).strip()

        return "\n".join(line for line in lines if line.strip()).strip()

    @staticmethod
    def _parse_scalar(value: Any) -> str:
        text = str(value or "").strip()
        if text.startswith('"') and text.endswith('"'):
            try:
                return str(json.loads(text))
            except ValueError:
                return text.strip('"')
        return text

    @staticmethod
    def _parse_json(value: Any, default: Any) -> Any:
        try:
            parsed = json.loads(str(value))
        except (TypeError, ValueError):
            return default
        return parsed if isinstance(parsed, type(default)) else default

    # ------------------------------------------------------------- titling
    @staticmethod
    def _slugify(value: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
        slug = re.sub(r"-{2,}", "-", slug)
        return slug[:60].strip("-")

    @staticmethod
    def _semantic_title(memory: Memory) -> str:
        content = memory.content.strip()
        lower = content.lower()
        fallback = ObsidianMemoryStore._clip_title(content) if content else "Memory Note"

        if memory.category == "identity":
            name_match = re.search(r"\b(?:my name is|name is)\s+([A-Za-z][A-Za-z\s'-]{1,40})", content, flags=re.IGNORECASE)
            if name_match:
                return f"Name: {ObsidianMemoryStore._title_case_phrase(name_match.group(1).strip())}"
            return fallback

        if memory.category == "preference":
            match = re.search(
                r"\b(?:i\s+|user\s+)?(?:really\s+)?(like|love|enjoy|prefer|hate|dislike)s?\s+(?:playing\s+|to\s+)?(.+?)(?:[.?!]?$)",
                content,
                flags=re.IGNORECASE,
            )
            if match:
                verb = _VERB_FORMS.get(match.group(1).lower(), "Likes")
                thing = match.group(2).strip().rstrip(".!?")
                return f"{verb} {ObsidianMemoryStore._title_case_phrase(thing)}"
            return fallback

        if memory.category == "habit":
            match = re.search(
                r"\b(?:i\s+)?(?:usually|often|always|tend to|go to the)\s+(.+?)(?:\s+(?:\d+|once|twice|three|four|five|six|seven|eight|nine|ten|times?|per|a|an|every)\b|[.?!]?$)",
                content,
                flags=re.IGNORECASE,
            )
            if match:
                thing = match.group(1).strip().rstrip(".!?")
                return f"{ObsidianMemoryStore._title_case_phrase(thing)} Habit"
            if "gym" in lower:
                return "Gym Frequency"
            return fallback

        if memory.category == "fact":
            location_match = re.search(r"\b(?:i|user)\s+lives? in\s+(.+?)(?:[.?!]?$)", content, flags=re.IGNORECASE)
            if location_match:
                return f"Lives in {ObsidianMemoryStore._title_case_phrase(location_match.group(1).strip())}"
            work_match = re.search(r"\b(?:i|user)\s+works? at\s+(.+?)(?:[.?!]?$)", content, flags=re.IGNORECASE)
            if work_match:
                return f"Works at {ObsidianMemoryStore._title_case_phrase(work_match.group(1).strip())}"
            return fallback

        return fallback

    @staticmethod
    def _clip_title(content: str, limit: int = 56) -> str:
        """A readable title from raw content: no leading 'User', cut at a word boundary."""
        text = re.sub(r"^(?:the\s+)?user(?:'s)?\s+", "", content.strip(), flags=re.IGNORECASE)
        phrase = ObsidianMemoryStore._title_case_phrase(text)
        if len(phrase) <= limit:
            return phrase
        return phrase[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-") or phrase[:limit]

    @staticmethod
    def _title_case_phrase(value: str) -> str:
        words = re.split(r"\s+", value.strip())
        cleaned = [word.strip(".,;:!?") for word in words if word.strip(".,;:!?")]
        if not cleaned:
            return "Memory Note"
        return " ".join(word[:1].upper() + word[1:] if word else "" for word in cleaned)

    @staticmethod
    def _tags_for_memory(memory: Memory) -> list[str]:
        tags = list(dict.fromkeys([memory.category, memory.memory_type, memory.source]))
        words = re.findall(r"[a-zA-Z0-9]+", memory.content.lower())
        for word in words:
            if len(word) >= 4 and word not in tags:
                tags.append(word)
        return tags[:8]

    @staticmethod
    def _parse_float(value: Any, *, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _normalize_content(value: str) -> str:
        return " ".join(value.strip().lower().split())
