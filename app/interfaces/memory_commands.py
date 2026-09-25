"""Chat-command logic for Miki's memory (``/memory``, ``/forget``, ``/brain``).

Pure functions over the core/manager -- no GUI imports -- so they are easy to test and reuse from the
web dashboard, the CLI, or anything else.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from app.memory.brain import MemoryBrain, hub_for_category


def toast_payload(memories: list[Any]) -> list[dict[str, Any]]:
    """What the dashboard shows after Miki saves something."""
    return [
        {
            "title": m.title or m.content,
            "content": m.content,
            "category": m.category,
            "entities": list(m.entities)[:4],
        }
        for m in memories
    ]


def short_id(memory: Any) -> str:
    return memory.memory_id[:6]


def memory_overview(manager: Any, query: str = "", *, limit: int = 60) -> str:
    """Everything Miki remembers (grouped by category), or the matches for ``query``."""
    memories = manager.search_memories(query) if query else manager.list_memories(active_only=True)
    if not memories:
        return f'No memories match "{query}".' if query else "I don't remember anything about you yet."

    brain = MemoryBrain(manager.list_memories(active_only=True))
    groups: dict[str, list[Any]] = {}
    for memory in memories[:limit]:
        groups.setdefault(hub_for_category(memory.category), []).append(memory)

    noun = "memory" if len(memories) == 1 else "memories"
    heading = f'{len(memories)} {noun} matching "{query}"' if query else f"{len(memories)} {noun}"
    lines = [heading, ""]
    for hub in sorted(groups):
        lines.append(hub.upper())
        for memory in groups[hub]:
            about = brain.entity_names(memory)
            suffix = f"  → {', '.join(about[:3])}" if about else ""
            lines.append(f"  • {memory.title or memory.content}  [{short_id(memory)}]{suffix}")
        lines.append("")
    if len(memories) > limit:
        lines.append(f"…and {len(memories) - limit} more. Narrow it with /memory <word>.")
    lines.append("Forget one with /forget <word or id>.")
    return "\n".join(lines).rstrip()


def forget(core: Any, target: str) -> str:
    target = (target or "").strip()
    if not target:
        return "Usage: /forget <part of the memory, or its id>"
    forgotten = core.delete_memory_by_text(target)
    if forgotten is None:
        return f'No memory matches "{target}". Try /memory to see what I have.'
    return f"Forgot: {forgotten.content}"


def brain_status(manager: Any) -> str:
    memories = manager.list_memories(active_only=True)
    if not memories:
        return "My brain is empty so far. Tell me about yourself, or use /remember <fact>."
    brain = MemoryBrain(memories)
    stats = brain.stats()
    top = ", ".join(f"{e.name} ({len(e.memory_ids)})" for e in brain.top_entities(5)) or "none yet"
    lines = [
        "Brain",
        f"  {stats['memories']} memories · {stats['entities']} entities · {stats['memory_links']} links",
        f"  {stats['connected_memories']} of {stats['memories']} memories are connected to others",
        f"  Most connected: {top}",
    ]
    describe = getattr(manager.memory_store, "describe", None)
    if callable(describe):
        info = describe()
        lines.append(f"  Obsidian vault: {getattr(info, 'vault_path', '')}")
    if manager.needs_graph_migration():
        lines.append("  Some older memories aren't linked yet: run /brain rebuild.")
    lines.append("Commands: /memory · /profile · /interview · /remember · /forget · /brain rebuild|learn|sleep|open")
    return "\n".join(lines)


def open_vault(manager: Any) -> str:
    """Open the Obsidian vault (Obsidian if it's registered, else the folder in Explorer)."""
    describe = getattr(manager.memory_store, "describe", None)
    info = describe() if callable(describe) else None
    if info is None:
        return "Memories aren't stored in an Obsidian vault right now."
    vault = str(info.vault_path.resolve())
    if sys.platform != "win32":
        return f"Your vault is at {vault}"
    try:
        os.startfile(f"obsidian://open?path={vault.replace(os.sep, '/')}")
        return "Opening your vault in Obsidian…"
    except OSError:
        try:
            os.startfile(vault)
            return "Opened the vault folder."
        except OSError:
            return f"Couldn't open it automatically. It's at {vault}"


def profile_text(profile: Any) -> str:
    """The portrait, how well each area is known, and what's still missing."""
    if profile is None or (not profile.portrait and profile.coverage == 0 and not profile.memory_count):
        return "I haven't built a profile of you yet. Tell me about yourself, or run /interview and I'll ask."
    lines = []
    if profile.portrait:
        lines += ["WHO YOU ARE", profile.portrait, ""]
    lines.append(f"HOW WELL I KNOW YOU: {profile.coverage:.0%}   (from {profile.memory_count} memories)")
    for domain in profile.domains:
        filled = round(domain.confidence * 5)
        bar = "▰" * filled + "▱" * (5 - filled)
        summary = f"  {domain.summary}" if domain.summary else ""
        lines.append(f"  {domain.title:<24} {bar} {domain.confidence:>4.0%}{summary}")
    gaps = [(d.title, g) for d in profile.weakest(4) for g in d.gaps[:1]]
    if gaps:
        lines += ["", "STILL UNKNOWN"] + [f"  • {title}: {gap}" for title, gap in gaps]
    lines += ["", "Type /interview and I'll ask about the gaps."]
    return "\n".join(lines)


def learn_summary(result: dict[str, Any]) -> str:
    if not result.get("messages_read"):
        return "Nothing new to learn from earlier chats."
    left = f" ({result['remaining']} left, run it again)" if result.get("remaining") else ""
    return f"Read {result['messages_read']} earlier messages and stored or updated {result['memories_stored_or_updated']} memories.{left}"


def consolidate_summary(report: dict[str, Any]) -> str:
    parts = ["Profile refreshed." if report.get("profile") else "Profile could not be refreshed."]
    if report.get("journal"):
        parts.append("Journal written: " + ", ".join(report["journal"]) + ".")
    else:
        parts.append("No new journal entry (nothing to summarise).")
    return " ".join(parts)


def describe_rebuild(result: dict[str, Any]) -> str:
    lines = [
        "Brain rebuilt.",
        f"  {result.get('memories', 0)} memories · {result.get('entities', 0)} entities · {result.get('memory_links', 0)} links",
        f"  {result.get('enriched', 0)} older memories described, {result.get('relinked', 0)} re-linked",
    ]
    if result.get("backup"):
        lines.append(f"  Backup of your vault first: {result['backup']}")
    return "\n".join(lines)
