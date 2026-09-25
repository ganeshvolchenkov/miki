from __future__ import annotations

import logging
import sys

from openai import OpenAI

from app.brain.embeddings import OpenAIEmbeddingClient
from app.brain.openai_client import OpenAIClient
from app.core.assistant import MikiCore
from app.core.config import Settings
from app.core.memory_manager import MemoryManager
from app.core.rag_wiring import build_rag_service
from app.core.storage import build_memory_manager, build_memory_store
from app.core.tool_wiring import build_tool_runner
from app.memory.conversation import JSONConversationStore
from app.memory.obsidian import ObsidianMemoryStore
from app.tools.calendar.auth import CalendarUnavailable, run_oauth_flow

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=[logging.StreamHandler()],
    )


def _print_startup_splash(memory_store) -> None:
    """Print a simple ASCII splash screen before the chat loop starts."""
    status_line = "Obsidian: connected" if isinstance(memory_store, ObsidianMemoryStore) and memory_store.is_connected() else "Obsidian: not connected"
    backend_line = "Memory backend: Obsidian" if isinstance(memory_store, ObsidianMemoryStore) else "Memory backend: local JSON"

    art = [
        " __  __ ___ _  __",
        "|  \\/  |_ _| |/ /",
        "| |\\/| || || ' < ",
        "| |  | || || |\\ \\",
        "|_|  |_|___|_| \\_\\",
    ]

    print()
    for line in art:
        print(f"   {line}")
    print("+" + "-" * 46 + "+")
    print("| Miki is ready to chat                      |")
    print("| Type a message and press Enter             |")
    print(f"| {backend_line:<44}|")
    print(f"| {status_line:<44}|")
    print("| Commands: /help /obsidian /sync            |")
    print("+" + "-" * 46 + "+")
    print()


def _format_memory_table(memories: list, show_details: bool = True) -> None:
    """Format and display memories in a nicely aligned table."""
    if not memories:
        print("Miki: No memories to display.")
        return

    # Print header
    print("\n" + "=" * 110)
    print(f"{'#':<3} {'Category':<12} {'Content':<50} {'Confidence':<12} {'Date':<18}")
    print("=" * 110)

    # Print rows
    for i, memory in enumerate(memories, 1):
        content = memory.content[:47] + "..." if len(memory.content) > 50 else memory.content
        date = memory.created_at[:10] if memory.created_at else "unknown"
        confidence_pct = f"{memory.confidence:.0%}" if memory.confidence else "N/A"
        
        print(f"{i:<3} {memory.category:<12} {content:<50} {confidence_pct:<12} {date:<18}")
        
        if show_details:
            print(f"    ID: {memory.memory_id} | Type: {memory.memory_type} | Source: {memory.source}")

    print("=" * 110 + "\n")


def _handle_memories_command(core, command: str) -> None:
    """Parse and handle /memories command with sorting and filtering options."""
    parts = command.split()
    memories = core.list_memories(active_only=True)

    if not memories:
        print("Miki: No active memories yet.")
        return

    # Parse sorting and filtering options
    sort_by = "created_at"  # Default: newest first
    sort_asc = False
    filter_category = None

    for part in parts[1:]:  # Skip "/memories"
        if part.startswith("sort:"):
            sort_key = part[5:].lower()
            if sort_key == "date":
                sort_by = "created_at"
                sort_asc = False
            elif sort_key == "date-asc":
                sort_by = "created_at"
                sort_asc = True
            elif sort_key == "confidence":
                sort_by = "confidence"
                sort_asc = False
            elif sort_key == "category":
                sort_by = "category"
                sort_asc = True
        elif part.startswith("filter:"):
            filter_category = part[7:].lower()

    # Apply filter
    if filter_category:
        memories = [m for m in memories if m.category.lower() == filter_category]

    if not memories:
        print(f"Miki: No memories found with category '{filter_category}'.")
        return

    # Apply sorting
    if sort_by == "created_at":
        memories = sorted(memories, key=lambda m: m.created_at, reverse=not sort_asc)
    elif sort_by == "confidence":
        memories = sorted(memories, key=lambda m: m.confidence, reverse=True)
    elif sort_by == "category":
        memories = sorted(memories, key=lambda m: m.category)

    # Display
    print(f"\nMiki: Showing {len(memories)} active memory/memories:")
    _format_memory_table(memories, show_details=True)


def _handle_obsidian_command(memory_store) -> None:
    if isinstance(memory_store, ObsidianMemoryStore) and memory_store.is_connected():
        info = memory_store.describe()
        print("Miki: Obsidian: Connected")
        print(f"Miki: Vault: {info.vault_name}")
        print(f"Miki: Miki directory: {info.display_miki_dir}")
        print(f"Miki: Memory notes: {memory_store.count_memories(active_only=False)}")
        return

    print("Miki: Obsidian: Not connected")
    print("Miki: Vault: Not configured")
    print("Miki: Miki directory: Miki/")


def _describe_memory_storage(memory_store, memory) -> str:
    """Human-readable description of where a memory is physically stored."""
    if isinstance(memory_store, ObsidianMemoryStore):
        note_path = memory_store.get_note_path(memory.memory_id)
        if note_path is not None:
            try:
                relative = note_path.relative_to(memory_store.vault_path)
            except ValueError:
                relative = note_path
            return f"Obsidian note ({relative.as_posix()}, id {memory.memory_id})"
        return f"Obsidian vault ({memory_store.vault_path.name})"

    file_path = getattr(memory_store, "file_path", None)
    if file_path is not None:
        return f"local JSON store ({file_path})"

    return type(memory_store).__name__


def _describe_rag_status(core: MikiCore) -> str:
    """Human-readable description of whether a just-saved memory was indexed for RAG."""
    if core.rag_service is None:
        return "disabled"
    if not core.last_memory_rag_indexed:
        return "not indexed (RAG error -- check the logs)"
    status = core.rag_service.status()
    return f"indexed for retrieval (embedding model: {status.embedding_model})"


def _print_memory_saved(core: MikiCore, memory_store, *, intro: str) -> None:
    """Prints a detailed "memory saved" notice for core.last_created_memory.

    Only prints anything when a memory was actually stored -- callers must
    check that first (e.g. via the memory_created / memory_follow_up flags).
    """
    memory = core.last_created_memory
    if memory is None:
        return
    print(f"\nMiki: {intro}")
    print(f"Miki:    {memory.category} / {memory.memory_type} -- confidence {memory.confidence:.0%}")
    print(f"Miki:    Stored in: {_describe_memory_storage(memory_store, memory)}")
    print(f"Miki:    RAG: {_describe_rag_status(core)}")


def _handle_candidates_command(core: MikiCore) -> None:
    candidates = core.list_memory_candidates(status="pending")
    if not candidates:
        print("Miki: No pending memory candidates.")
        return

    print(f"Miki: {len(candidates)} pending memory candidate(s):")
    for i, candidate in enumerate(candidates, 1):
        print(f'  {i}. [{candidate.candidate_id}] "{candidate.raw_message}" (score: {candidate.score:.0%})')
        if candidate.reason:
            print(f"     Reason: {candidate.reason}")
    print("Miki: Use /memory approve <id> or /memory reject <id>.")


def _handle_memory_command(core: MikiCore, command: str) -> None:
    parts = command.split()
    if len(parts) < 3 or parts[1] not in {"approve", "reject"}:
        print("Miki: Usage: /memory approve <id>  or  /memory reject <id>")
        return

    action, candidate_id = parts[1], parts[2]
    if action == "approve":
        memory = core.approve_memory_candidate(candidate_id)
        if memory is None:
            print(f"Miki: Could not approve candidate '{candidate_id}' (not found or already resolved).")
        else:
            print(f"Miki: Approved. Stored as a {memory.category} memory: {memory.content}")
        return

    rejected = core.reject_memory_candidate(candidate_id)
    if rejected:
        print(f"Miki: Rejected candidate '{candidate_id}'. Thanks, I'll remember this for next time.")
    else:
        print(f"Miki: Could not reject candidate '{candidate_id}' (not found or already resolved).")


def _handle_tools_command(core: MikiCore) -> None:
    tools = core.list_tools()
    if not tools:
        print("Miki: No tools are configured.")
        return
    print("Miki: Available tools:")
    for tool in tools:
        connected = "connected" if tool.is_available() else "not connected"
        print(f"  - {tool.name}: {tool.description} ({connected})")
        for operation in tool.operations:
            print(f"      {operation.name} [{operation.access}] - {operation.description}")


def _handle_calendar_command(core: MikiCore, settings: Settings, command: str) -> None:
    parts = command.split()
    subcommand = parts[1] if len(parts) > 1 else "status"

    tool = core.get_tool("calendar")
    if tool is None:
        print("Miki: The Calendar tool is not enabled (set MIKI_CALENDAR_ENABLED=true and restart Miki).")
        return

    if subcommand == "connect":
        print("Miki: Opening your browser to connect Google Calendar...")
        try:
            run_oauth_flow(settings.google_calendar_credentials_path, settings.google_calendar_token_path)
        except CalendarUnavailable as err:
            print(f"Miki: Could not connect Google Calendar: {err}")
            return
        except Exception as err:
            print(f"Miki: Google Calendar authentication failed: {err}")
            logger.exception("Google Calendar OAuth flow failed")
            return
        print("Miki: Google Calendar connected.")
        return


    status = tool.status()
    print("Miki: Google Calendar:")
    print(f"  {'Connected' if status.get('connected') else 'Not connected'}")
    if not status.get("connected") and status.get("reason"):
        print(f"  Reason: {status['reason']}")
        print("  Run '/calendar connect' to connect your Google account.")


def _handle_weather_command(core: MikiCore, command: str) -> None:
    """Debug/convenience command -- calls the Weather tool directly (no
    Brain round-trip, spec section 14) rather than going through
    process_user_input, so this is instant and costs no LLM tokens."""
    tool = core.get_tool("weather")
    if tool is None:
        print("Miki: The Weather tool is not enabled (set MIKI_WEATHER_ENABLED=true and restart Miki).")
        return

    location = command[len("/weather"):].strip() or None

    current_result = core.call_tool("weather", "get_current_weather", {"location": location} if location else {})
    if not current_result.success:
        print(f"Miki: Weather unavailable -- {current_result.message}")
        return

    current = current_result.data["current"]
    print(f"Miki: Weather for {current_result.data['location']}")
    print(f"  {current['condition']} ({current['condition_symbol']}), {current['temperature']}°C (feels like {current['feels_like']}°C)")
    print(f"  Precipitation: {current['precipitation_probability']}%   Wind: {current['wind_speed']} km/h   Humidity: {current['humidity']}%")

    daily_result = core.call_tool("weather", "get_daily_forecast", {"location": location, "days": 3} if location else {"days": 3})
    if daily_result.success:
        print("  Next days:")
        for day in daily_result.data["daily"]:
            print(f"    {day['date']}  {day['condition']} ({day['condition_symbol']})  high {day['temperature_high']}°C / low {day['temperature_low']}°C  rain {day['precipitation_probability']}%")


def _handle_sync_command(memory_manager: MemoryManager) -> None:
    result = memory_manager.sync_memories()
    print(
        "Miki: Sync complete. "
        f"Active memories: {result.get('active', 0)} | "
        f"Inactive memories: {result.get('inactive', 0)} | "
        f"Total: {result.get('total', 0)}"
    )


def _handle_rag_command(core: MikiCore, command: str) -> None:
    parts = command.split()
    subcommand = parts[1] if len(parts) > 1 else "status"

    if core.rag_service is None:
        print("Miki: RAG is disabled or not configured (set MIKI_RAG_ENABLED=true to enable it).")
        return

    if subcommand == "rebuild":
        print("Miki: Rebuilding RAG index from memories, Obsidian notes, and conversations...")
        try:
            status = core.rag_service.rebuild()
        except Exception as exc:
            print(f"Miki: RAG rebuild failed: {exc}")
            logger.exception("RAG rebuild failed")
            return
        print(
            "Miki: Rebuild complete. "
            f"{status.total_documents} document(s) indexed as {status.total_chunks} chunk(s) "
            f"(memory: {status.memory_documents}, obsidian: {status.obsidian_documents}, "
            f"conversation: {status.conversation_documents})."
        )
        return

    status = core.rag_service.status()
    print("Miki: RAG status")
    print(f"  Enabled: {status.enabled}")
    print(f"  Indexed documents: {status.total_documents} ({status.total_chunks} chunk(s))")
    print(f"    Memory: {status.memory_documents}")
    print(f"    Obsidian: {status.obsidian_documents}")
    print(f"    Conversation: {status.conversation_documents}")
    print(f"  Embedding model: {status.embedding_model}")
    print(f"  Top-K: {status.top_k}")
    print(f"  Similarity threshold: {status.similarity_threshold}")
    print(f"  Index location: {status.index_path}")


def _handle_graph_command(settings: Settings, command: str) -> None:
    """/graph [stats|search <q>|related <title>] over the Obsidian vault's wikilink graph."""
    from app.memory.vault import VaultManager

    vault_path = settings.obsidian_vault_path
    if vault_path is None:
        print("Miki: Set OBSIDIAN_VAULT_PATH in .env to use the memory graph.")
        return
    vault = VaultManager(str(vault_path))
    vault.scan_vault()
    graph = vault.graph
    parts = command.split(maxsplit=2)
    sub = parts[1] if len(parts) > 1 else "stats"

    if sub == "stats":
        print(f"Miki: Nodes: {len(graph.nodes_data)}, Edges: {len(graph.graph.edges)}")
        for title, score in graph.get_important_memories(5):
            print(f"  - {title} ({score:.2f})")
    elif sub == "search" and len(parts) > 2:
        results = graph.search(parts[2])
        print(f"Miki: Found {len(results)} matches:")
        for node in results:
            print(f"  - {node.title}: {node.content.strip()[:50]}...")
    elif sub == "related" and len(parts) > 2:
        related = graph.get_related(parts[2], depth=1)
        print("Miki: Related: " + (", ".join(sorted(related)) if related else "none"))
    else:
        print("Miki: Usage: /graph [stats | search <query> | related <title>]")


def run_cli() -> int:
    configure_logging()
    logger.info("Miki starting up")

    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        logger.error("Initialization failed: %s", exc)
        return 1

    try:
        shared_openai_client = OpenAI(api_key=settings.openai_api_key)
        brain = OpenAIClient(api_key=settings.openai_api_key, model=settings.model, client=shared_openai_client)
    except Exception as exc:
        print(f"Unable to initialize AI provider: {exc}")
        logger.exception("AI provider initialization failed")
        return 1

    conversation_store = JSONConversationStore("data/conversations")
    memory_store = build_memory_store(settings, json_storage_dir="data/memories")
    embedding_client = OpenAIEmbeddingClient(client=shared_openai_client, model=settings.embedding_model)
    memory_manager = build_memory_manager(settings, memory_store=memory_store, brain=brain, embedding_client=embedding_client)
    rag_service = build_rag_service(
        settings,
        memory_store=memory_store,
        shared_openai_client=shared_openai_client,
        conversations_dir="data/conversations",
        embedding_client=embedding_client,
    )
    tool_runner = build_tool_runner(settings, brain)
    core = MikiCore(brain=brain, conversation_store=conversation_store, memory_manager=memory_manager, rag_service=rag_service, tool_runner=tool_runner)

    _print_startup_splash(memory_store)

    while True:
        try:
            user_input = input("\nYou: ")
        except EOFError:
            conversation_store.close_session()
            print("\nMiki: I’m here when you are.")
            break
        except KeyboardInterrupt:
            conversation_store.close_session()
            print("\nMiki: See you later.")
            break

        raw_input = user_input.strip()
        if raw_input == "":
            continue

        command = raw_input.lower()
        if command in {"/exit", "/quit", "exit", "quit"}:
            conversation_store.close_session()
            print("Miki: See you later.")
            logger.info("Shutdown requested by user")
            break

        if command in {"/restart", "restart"}:
            conversation_store.close_session()
            print("Miki: Restarting session...")
            logger.info("Restart requested by user")
            break

        if command in {"/help", "help"}:
            print("Miki: Available commands:")
            print("  /help                    - Show this help message")
            print("  /restart                 - Start a new session")
            print("  /exit, /quit             - Exit Miki")
            print("  /clear                   - Clear screen")
            print("  /memories [options]      - List memories (see options below)")
            print("  /search <keyword>        - Search memories by keyword")
            print("  /remember <content>      - Manually save a memory")
            print("  /delete <id_or_text>     - Delete a memory by ID or text search")
            print("  /forget <text>           - Alias for /delete")
            print("  /obsidian                - Show Obsidian connection status")
            print("  /sync                    - Sync the memory store")
            print("  /graph [stats|search|related] - Explore the wikilink memory graph")
            print("  /rag [status|rebuild]    - Show RAG index status or rebuild it")
            print("  /candidates              - Show pending uncertain memory candidates")
            print("  /memory approve <id>     - Approve a candidate and store it as a memory")
            print("  /memory reject <id>      - Reject a candidate")
            print("  /tools                   - List available tools")
            print("  /calendar status         - Show Google Calendar connection status")
            print("  /calendar connect        - Connect your Google account (opens a browser)")
            print("  /weather [location]      - Show current weather + 3-day forecast")
            print()
            print("  /memories view options:")
            print("    /memories                     - Show all active memories")
            print("    /memories sort:date           - Sort by creation date (newest first)")
            print("    /memories sort:date-asc       - Sort by date (oldest first)")
            print("    /memories sort:confidence     - Sort by confidence (highest first)")
            print("    /memories sort:category       - Sort by category")
            print("    /memories filter:preference   - Show only preferences")
            print("    /memories filter:identity     - Show only identity facts")
            print("    /memories filter:habit        - Show only habits")
            print("    /memories sort:date filter:preference - Combine multiple options")
            print()
            print("Miki: Current capabilities:")
            print("  - Basic CLI chat with Miki")
            print("  - OpenAI-powered responses via the official SDK")
            print("  - Session-based JSON conversation storage")
            print("  - Persistent memory store with search")
            print("  - Environment-based configuration from .env")
            print("  - Memory management with sorting and filtering")
            print("  - Improved duplicate detection with synonym awareness")
            print("  - Obsidian-backed memory notes when OBSIDIAN_VAULT_PATH is set")
            print("  - Retrieval-Augmented Generation (RAG) over memories, Obsidian notes, and conversations")
            print("  - Memory Intelligence: AI-driven memory evaluation, extraction, deduplication, and conflict resolution")
            print("  - Tool use: Miki can call tools (Google Calendar, Weather) through natural language, no special syntax needed")
            continue

        if command == "/obsidian":
            _handle_obsidian_command(memory_store)
            continue

        if command == "/graph" or command.startswith("/graph "):
            _handle_graph_command(settings, raw_input.strip())
            continue

        if command == "/sync":
            _handle_sync_command(core.memory_manager)
            continue

        if command == "/rag" or command.startswith("/rag "):
            _handle_rag_command(core, command)
            continue

        if command == "/tools":
            _handle_tools_command(core)
            continue

        if command == "/calendar" or command.startswith("/calendar "):
            _handle_calendar_command(core, settings, command)
            continue

        if command == "/weather" or command.startswith("/weather "):
            _handle_weather_command(core, command)
            continue

        if command in {"/candidates", "/memory_candidates"}:
            _handle_candidates_command(core)
            continue

        if command in {"/memory stats", "/memory_stats", "/stats"}:
            if core.memory_manager is not None:
                print(core.memory_manager.format_telemetry_stats())
            else:
                print("Miki: Memory manager is not configured.")
            continue

        if command.startswith("/memory "):
            _handle_memory_command(core, command)
            continue

        # Handle /memories with optional sorting/filtering
        if command.startswith("/memories"):
            _handle_memories_command(core, command)
            continue

        if command.startswith("/search "):
            keyword = command[len("/search "):].strip()
            if not keyword:
                print("Miki: Please provide a search keyword.")
                continue
            results = core.memory_manager.search_memories(keyword)
            if not results:
                print(f"Miki: No memories found matching '{keyword}'.")
            else:
                print(f"Miki: Found {len(results)} memory/memories matching '{keyword}':")
                for i, memory in enumerate(results, 1):
                    print(f"  {i}. [{memory.category}] {memory.content}")
                    print(f"     ID: {memory.memory_id} | Confidence: {memory.confidence:.1%} | Created: {memory.created_at[:10]}")
            continue

        if command.startswith("/remember "):
            content = command[len("/remember "):].strip()
            if not content:
                print("Miki: Please provide memory content after /remember.")
                continue
            memory = core.create_memory(content, category="general", memory_type="fact", confidence=0.8, source="manual")
            _print_memory_saved(core, memory_store, intro=f"💾 Memory saved: {memory.content}")

            # Ask follow-up questions to build more memory
            print(f"\nMiki: I'm curious to learn more! Let me ask you a few things about this...")
            follow_up_prompts = [
                f"Why is {content.lower()} important to you?",
                f"How often do you {content.lower()}?",
                f"What do you enjoy most about {content.lower()}?",
                f"When did you first start {content.lower()}?",
            ]
            selected_prompt = follow_up_prompts[0]  # Use first prompt for now
            print(f"\nYou (via Miki): {selected_prompt}")
            
            # Process this as a user input to gather more memories
            follow_up_response, memory_follow_up = core.process_user_input(selected_prompt)
            if follow_up_response:
                print(f"\nMiki: {follow_up_response}")
                if memory_follow_up:
                    _print_memory_saved(core, memory_store, intro="✨ Great! I learned something new about you and saved it to memory!")
            continue

        if command.startswith("/delete "):
            lookup = command[len("/delete "):].strip()
            if not lookup:
                print("Miki: Please provide the memory text or ID after /delete.")
                continue
            match = core.delete_memory_by_text(lookup)
            if match is None:
                print(f"Miki: I could not find a matching memory to delete.")
            else:
                print(f"Miki: Memory deleted: {match.content}")
            continue

        if command.startswith("/forget "):
            lookup = command[len("/forget "):].strip()
            if not lookup:
                print("Miki: Please provide the memory text or ID after /forget.")
                continue
            match = core.delete_memory_by_text(lookup)
            if match is None:
                print("Miki: I could not find a matching memory to forget.")
            else:
                print(f"Miki: Forgotten memory: {match.content}")
            continue

        if command in {"/clear", "clear"}:
            print("\n" * 40)
            continue

        try:
            response, memory_created = core.process_user_input(user_input)
            if response:
                print(f"\nMiki: {response}")
                if memory_created:
                    _print_memory_saved(core, memory_store, intro="✨ I learned something new about you and saved it to memory!")
        except Exception as exc:
            print("Miki: I’m having trouble responding right now.")
            logger.exception("Failed to respond to user input")
            continue

    print("\nMiki shutting down...")
    logger.info("Miki shutting down")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
