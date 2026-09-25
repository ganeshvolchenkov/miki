# Miki: Your Long-Term Personal AI Assistant

<p align="center">
  <img src="assets/screenshots/dashboard_1.png" alt="Miki Command Center Dashboard" width="100%">
</p>

Miki is a privacy-first, locally-run personal AI assistant that remembers who you are, tracks your schedule, manages your emails, and connects to your phone via Telegram. Rather than acting as a stateless chatbot, Miki continuously builds a long-term **knowledge graph** of your life using an Obsidian-backed memory store and a Retrieval-Augmented Generation (RAG) pipeline.

## 🎬 Demo

<p align="center">
  <a href="assets/demo/miki-demo.mp4">
    <img src="assets/demo/miki-demo.gif" alt="Miki demo: graph memory, RAG and calendar management" width="100%">
  </a>
  <br>
  <sub>Graph memory, RAG and calendar management in 50 seconds. <a href="assets/demo/miki-demo.mp4">Watch the full video with sound</a>. (Names and data in the demo are fictional.)</sub>
</p>

---

## ✨ Key Features
- **The Command Center (Web GUI)**: A futuristic, lightweight `webview`-based dashboard featuring an animated pixel-art mascot, an interactive map of your memory connections, and live widgets (Calendar, Weather, Mail, Maps, Drive).
  <p align="center"><img src="assets/screenshots/dashboard_2.png" alt="Miki Command Center Interface" width="100%"></p>
- **Miki On Your Phone (Telegram Integration)**: Link Miki to your phone to access her 24/7. Send text or voice notes, receive urgent email pushes, and manage your day—all secured via a private 1-to-1 pairing code.
- **Memory Intelligence**: Miki doesn't just match keywords; she uses an AI evaluator to determine if your messages contain long-term facts, temporary contexts, or habits. These are extracted into canonical third-person memories, automatically deduplicated, and stored as readable Markdown notes in your Obsidian vault.
- **Extensible Tools System**: Miki can view and edit your Google Calendar (requiring UI confirmation for write/destructive actions) and fetch real-time weather using Open-Meteo (no API key required). Tools bypass the LLM for UI widget rendering to save tokens and reduce latency.
- **RAG & Knowledge Retrieval**: Miki seamlessly pulls context from your past conversations, your Obsidian vault, and her own extracted memories using a local vector index, ensuring she always has the context she needs.
- **Headless Mode**: Run Miki in the background on your machine so the Telegram phone integration stays alive even when the dashboard is closed.

---

## 🚀 Getting Started

### 1. Installation

Create and activate a virtual environment:

**Windows PowerShell:**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```
**macOS/Linux:**
```bash
python -m venv .venv
source .venv/bin/activate
```

Install dependencies:
```bash
pip install -r requirements.txt
```

### 2. Configuration (`.env`)

Miki reads configuration from a `.env` file in the project root. Create `.env` and fill in your values (all values except `OPENAI_API_KEY` are optional):

```env
OPENAI_API_KEY=your_key_here
MIKI_MODEL=gpt-4o-mini
MIKI_ENV=development
OBSIDIAN_VAULT_PATH=C:\Users\<username>\Desktop\MyVault
MIKI_TIMEZONE=UTC

# RAG & Memory
MIKI_RAG_ENABLED=true
MIKI_EMBEDDING_MODEL=text-embedding-3-small
MIKI_MEMORY_INTELLIGENCE_ENABLED=true

# Phone / Telegram
TELEGRAM_BOT_TOKEN=your_telegram_token
MIKI_QUIET_HOURS=23:00-08:00
```

### 3. Launching Miki

**Desktop Dashboard:**
Launch `miki.pyw` on Windows (double-click in File Explorer or run `pythonw miki.pyw`). This opens the Command Center interface.

**Headless Mode (Background Service):**
To keep Miki running 24/7 in the background (ideal for the Telegram integration):
```bash
python -m app.headless                      # run in the foreground
python -m app.headless --install-autostart  # start silently at Windows login
python -m app.headless --remove-autostart   # remove from startup
```

---

## 📱 Miki On Your Phone (Telegram)

<p align="center">
  <img src="assets/screenshots/phone.png" alt="Miki Telegram Interface" width="400">
</p>

Miki functions as a real bot on your phone, not just a chat window. 

1. In Telegram, open **@BotFather**, send `/newbot`, pick a name and a username ending in `bot`.
2. Put the token it gives you in `.env`: `TELEGRAM_BOT_TOKEN=...`
3. Restart Miki and type `/phone` in the dashboard chat. It shows a one-time code (valid 10 minutes).
4. In Telegram, open your bot and send `/pair <code>` (or tap the link Miki shows).

**Security Guarantee:** The bot is strictly locked to your account. Group chats are ignored, and any message from an unlinked account is entirely ignored. 

**Features:**
- **Voice Notes:** Send voice notes; Miki transcribes them and can reply with voice notes (`MIKI_TTS_MODEL`).
- **Mail Triage:** Urgent emails are pushed to your phone with "Open in Gmail / Dismiss / Snooze" buttons.
- **Commands:** Use `/home`, `/mail`, `/today`, `/brief`, `/memory`, and `/profile` directly in the chat to navigate her capabilities.

---

## 🧠 Memory & Obsidian Integration

Miki uses **Memory Intelligence** to reason about what to remember. The pipeline consists of an **Evaluator** (decides if a fact is useful), an **Extractor** (converts the fact to a 3rd-person canonical statement), and a **Comparator** (checks for duplicates/contradictions against existing memory).

When `OBSIDIAN_VAULT_PATH` is set, memories are written directly to your vault in human-readable Markdown:

```text
MyVault/
└── Miki/
    ├── Memories/
    │   ├── Identity/
    │   ├── Habits/
    │   └── Facts/
    └── Conversations/
```

- Updating a memory rewrites the existing note.
- Deleting a memory archives it safely.
- Type `/sync` in the dashboard to refresh the memory store.
- Use `/candidates` to review uncertain memories that Miki flagged for human approval.

---

## 🛠 Tools Setup

Miki utilizes an extensible tool architecture via OpenAI structured function calling.

### Google Calendar
1. Set up a Desktop OAuth client ID in the [Google Cloud Console](https://console.cloud.google.com/).
2. Save the downloaded JSON as `data/google_calendar/credentials.json`.
3. In Miki, type `/calendar connect` to authorize via your browser.
4. Miki can now read your events and proactively preview/confirm creations or deletions.

### Weather Tool
Powered by [Open-Meteo](https://open-meteo.com) (no API key required). It works out-of-the-box. The UI dashboard queries the tool directly and caches results to avoid spending LLM tokens on UI updates.

---

## 💬 Command Reference

Miki can be interacted with naturally, but also supports slash commands for precise control:

- `/remember <fact>`: Explicitly store a memory.
- `/memory [word]`: See what Miki remembers about you or search.
- `/forget <id>`: Delete a memory.
- `/profile`: See a summary of who Miki thinks you are.
- `/interview`: Miki asks you questions to get to know you better.
- `/brain rebuild`: Rebuild the memory graph and local vector index.
- `/model`: Switch the active OpenAI model on the fly to manage costs.
- `/mail`: See triaged emails needing your attention.
- `/calendar connect`: Re-authenticate Google Calendar.
- `/weather [location]`: Check the weather instantly.

---

## 🏗 Architecture & Storage

- **Local Vector Index**: Miki builds a brute-force cosine-similarity RAG index (`data/rag_index/index.json`) entirely locally. No external vector databases are used.
- **Graph Database**: Memory connections are managed in memory via `networkx`. For personal use (thousands of notes), this is lightning-fast and requires no external dependencies.
- **No Telemetry**: Everything lives in `data/`, your Obsidian vault, or your `.env` file. Miki never phones home.

To run the test suite (which uses deterministic fakes and doesn't touch your real vault):
```bash
python -m pytest
```
