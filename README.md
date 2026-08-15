# Miki

Miki is a long-term personal AI assistant project intended to evolve into a persistent, proactive companion with memory, tools, and multimodal interfaces.

## Current status

Miki v0.1 — initial scaffolding and basic conversational assistant.

## What Miki is

This repository is the first foundation for a personal AI system inspired by the idea of a persistent assistant that learns habits, remembers context, and eventually interacts across desktop, phone, voice, and hardware interfaces.

At this stage, the goal is not to build a full AI operating system. It is to create a clean, readable foundation that can be extended over time.

## How to install

Create a virtual environment:

```bash
python -m venv .venv
```

Activate it:

- Windows PowerShell:
  ```powershell
  .\.venv\Scripts\Activate.ps1
  ```

- Windows Command Prompt:
  ```bat
  .venv\Scripts\activate.bat
  ```

- macOS/Linux:
  ```bash
  source .venv/bin/activate
  ```

## How to install dependencies

```bash
pip install -r requirements.txt
```

## Environment setup

Miki reads configuration from a `.env` file in the project root.

Create a `.env` file based on `.env.example` and add your own API key:

```env
OPENAI_API_KEY=your_key_here
MIKI_MODEL=gpt-4o-mini
MIKI_ENV=development
```

The OpenAI API key is required for the application to start.

## How to run

```bash
python -m app.main
```

## Current capabilities

- Simple CLI chat loop
- Basic Miki personality prompt
- OpenAI provider abstraction via the official SDK
- Local JSON conversation persistence
- Log-based startup and error tracking
- Minimal test coverage for configuration and core behavior

## Planned future capabilities

These are not implemented yet, but they are directionally important for the project:

- persistent memory
- Obsidian integration
- RAG and retrieval
- voice interaction
- HUD / visual assistant interface
- tools for filesystem, browser, calendar, notifications, and terminal
- proactive behavior and scheduling
- phone integration
- Raspberry Pi room interface

## Project structure

```text
miki/
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── assistant.py
│   │   ├── config.py
│   │   └── events.py
│   ├── brain/
│   │   ├── __init__.py
│   │   └── openai_client.py
│   ├── memory/
│   │   ├── __init__.py
│   │   └── conversation.py
│   ├── tools/
│   │   └── __init__.py
│   └── interfaces/
│       ├── __init__.py
│       └── cli.py
├── data/
│   └── conversations/
├── tests/
│   ├── __init__.py
│   └── test_smoke.py
├── .env.example
├── .gitignore
├── README.md
├── requirements.txt
├── pyproject.toml
└── .venv/
```

## Notes

This is intentionally a minimal first version. The code is intentionally readable and easy to extend, and the architecture keeps the AI provider, memory store, and CLI interface separated.
