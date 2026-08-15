from __future__ import annotations

import logging
import sys

from app.brain.openai_client import OpenAIClient
from app.core.assistant import MikiCore
from app.core.config import Settings
from app.memory.conversation import JSONConversationStore

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=[logging.StreamHandler()],
    )


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
        brain = OpenAIClient(api_key=settings.openai_api_key, model=settings.model)
    except Exception as exc:
        print(f"Unable to initialize AI provider: {exc}")
        logger.exception("AI provider initialization failed")
        return 1

    conversation_store = JSONConversationStore("data/conversations")
    core = MikiCore(brain=brain, conversation_store=conversation_store)

    print("================================")
    print("            MIKI")
    print("================================")
    print()
    print("Miki is online.")

    while True:
        try:
            user_input = input("\nYou: ")
        except EOFError:
            print("\nMiki: I’m here when you are.")
            break
        except KeyboardInterrupt:
            print("\nMiki: See you later.")
            break

        if user_input.strip() == "":
            continue

        command = user_input.strip()
        if command in {"/exit", "/quit"}:
            print("Miki: See you later.")
            logger.info("Shutdown requested by user")
            break

        if command == "/help":
            print("Miki: Available commands: /help, /exit, /clear")
            continue

        if command == "/clear":
            print("\n" * 40)
            continue

        try:
            response = core.process_user_input(user_input)
            if response:
                print(f"\nMiki: {response}")
        except Exception as exc:
            print("Miki: I’m having trouble responding right now.")
            logger.exception("Failed to respond to user input")
            continue

    print("\nMiki shutting down...")
    logger.info("Miki shutting down")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
