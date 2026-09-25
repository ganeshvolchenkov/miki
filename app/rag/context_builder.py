from __future__ import annotations

from app.rag.retriever import RetrievedDocument

_SOURCE_LABELS = {
    "memory": "Memory",
    "obsidian": "Obsidian",
    "conversation": "Conversation",
}

_USAGE_GUIDANCE = (
    "Guidance for using the knowledge above: use it when relevant to the user's message; "
    "do not mention it unless it's actually useful; do not treat uncertain or low-confidence "
    "memories as settled fact; if entries conflict, prefer the more recent one; never invent "
    "information beyond what's listed here."
)


class ContextBuilder:
    """Turns retrieved KnowledgeDocuments into a context block for the Brain.

    Only ever includes what was actually retrieved -- never the full
    knowledge base -- and clearly delimits it from the rest of the prompt so
    the model doesn't confuse retrieved knowledge with the live user message.
    """

    def build(self, retrieved: list[RetrievedDocument]) -> str | None:
        if not retrieved:
            return None

        lines = ["RELEVANT USER KNOWLEDGE:", ""]
        for item in retrieved:
            label = _SOURCE_LABELS.get(item.source, item.source.title())
            lines.append(f"[{label}]")
            lines.append(item.content.strip())
            lines.append("")

        lines.append(_USAGE_GUIDANCE)

        return "\n".join(lines).rstrip()
