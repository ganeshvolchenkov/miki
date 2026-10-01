# Miki server handbook (for the next agent)

This is the working manual for Miki as it runs today: **one brain on a Contabo server, the laptop as
its hands**. Read it before you touch anything that runs on the server, the link, focus mode, the phone
bot, or the dashboard. `docs/deploy-contabo.md` is the beginner walkthrough the owner followed; this
file is the engineer's view: what runs where, how to ship, how to debug, and the rules that keep it
working.

> **Status on 2026-09-29:** the brain/hands split (`app/link`, `app/hands`, `app/focus/desk.py`,
> `app/interfaces/remote_gui.py`) and the background memory pipeline are written, pass 378 tests, and
> were verified live on the laptop against the real OpenAI API and a real hub. They are **not yet
> committed or deployed**. The server still runs the older bot-only `app.headless`. To go live:
> commit, push, then follow "Shipping a change" below plus Part 10 of `docs/deploy-contabo.md`
> (token, `MIKI_TIMEZONE`, laptop `.env`). Never verified yet: the real SSH tunnel to the real
> server, the thin dashboard window on the laptop, a real `/focus` from the phone driving the laptop.

---

## 1. The big picture

```
┌──────────────────────────── laptop (Windows 11, the owner's PC) ────────────────────────────┐
│  Miki.exe / miki.pyw  ──►  thin dashboard (app/interfaces/remote_gui.py, pywebview)         │
│        │ connects to 127.0.0.1:18765                                                          │
│        ▼                                                                                      │
│  hands agent (python -m app.hands / Miki.exe --hands)                                         │
│        owns: ssh -N -L 127.0.0.1:18765:127.0.0.1:8765 miki@<server-ip>                         │
│        does: focus screens (Chrome/Gemini/Claude), bouncer, pixel pet, beep                   │
└──────────────────────────────────────────────┬───────────────────────────────────────────────┘
                                                │ SSH (key auth) + MIKI_LINK_TOKEN (HMAC)
┌───────────────────────── Contabo VPS (Ubuntu 24.04, user `miki`) ────────────────────────────┐
│  systemd --user service `miki-phone`  →  ~/miki/.venv/bin/python -m app.headless              │
│    = run_brain():                                                                             │
│      LinkHub (127.0.0.1:8765 ONLY)                                                            │
│      WebApi (the dashboard's back end, paints the laptop page via JS over the link)           │
│      PhoneService (Telegram long-polling bot, mail watcher, morning brief)                    │
│      FocusService (focus clock, recap, study habits) with RemoteHands → laptop                │
│      MikiCore (brain: OpenAI chat + tools, memory pipeline, RAG)                              │
│  ~/miki/data/      all state (memories, conversations, rag index, focus, phone pairing)       │
│  ~/miki/obsidian/  the Obsidian vault = the memory graph ◄── Syncthing ──► laptop Obsidian    │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
          ▲ Telegram Bot API (outbound polling)      ▲ OpenAI API    ▲ Google APIs (Calendar/Gmail/Drive)
          phone
```

**One brain, one memory.** Memory, conversations and the vault exist only on the server. The laptop
must never run a second brain; `app/main.py` and `app/headless.py` enforce that (see §4).

### What lives where

| Thing | Where | Code |
|---|---|---|
| Chat, tools, memory pipeline, RAG | server | `app/core/assistant.py` (`MikiCore`), `app/core/memory_manager.py`, `app/rag/` |
| Telegram bot + pushes | server | `app/phone/` (`bot.py` controller, `ui.py` screens, `backend.py`, `notifier.py`, `service.py`, `state.py`) |
| Dashboard back end (`WebApi`) | server | `app/interfaces/web_gui.py`, driven through `app/link/remote.py` `RemoteWindow` |
| Dashboard window | laptop | `app/interfaces/remote_gui.py` (thin; imports no brain) + `app/interfaces/web/` (HTML/JS/CSS) |
| Focus timeline, recap, habits memory | server | `app/focus/service.py`, `session.py`, `recap.py`, `habits.py`, `memory.py` |
| Day planner `/plan` (reader, scheduler, calendar, nudges) | server | `app/plan/` (`request.py` AI → checked JSON, `schedule.py` pure scheduler, `service.py`, `store.py`) |
| Mail + photo reader (dates → calendar, tasks → texts/reminders, facts → memory) | server | `app/inbox/` (`findings.py` checks the AI's JSON, `reader.py` prompts + vision OCR, `service.py` acts, undo, reminders); wired in `PhoneService`; photos handled in `bot.py` `_on_photo`, undo button `ib:undo:<batch>` |
| Focus screen work (windows, bouncer, pet) | laptop | `app/focus/desk.py`, `guard.py`, `chrome.py`, `winapi.py`, `pet.py`, `pet_host.py`, run by `app/hands/agent.py` |
| The link | both | `app/link/protocol.py`, `hub.py` (server), `client.py` + `tunnel.py` (laptop), `remote.py` (server-side stand-ins) |
| Obsidian vault | server (synced to laptop by Syncthing) | `app/memory/obsidian.py`, `brain.py` |

---

## 2. The Contabo server

- **Plan:** Contabo Cloud VPS 4 (4 vCPU / 8 GB RAM / 100 GB SSD), **Ubuntu 24.04**.
- **Login:** `ssh miki@<server-ip>` with the owner's ed25519 key (`C:\Users\ahmet\.ssh\id_ed25519` on the
  laptop). Root login was only used for first setup. The IP is not stored in the repo: ask the owner,
  or look in their `~/.ssh/known_hosts` / `~/.ssh/config`. Never commit it.
- **Firewall:** `ufw` allows **OpenSSH only**. Keep it that way. The link hub binds `127.0.0.1:8765` and
  must never be exposed. Syncthing's UI (8384) is also loopback-only and reached with `ssh -L 8384:...`.
- **Code:** `~/miki` is a git clone of `https://github.com/ganeshvolchenkov/miki.git` (branch `main`),
  Python venv in `~/miki/.venv`. The server does **not** have pywebview (not in `requirements.txt`),
  so nothing on the brain's import path may `import webview` at module level.
- **Service:** systemd **user** service `miki-phone` (`deploy/miki-phone.service`, installed to
  `~/.config/systemd/user/`), `Restart=on-failure`, and `loginctl enable-linger miki` so it runs
  with nobody logged in and comes back after a reboot. The name is historical: it now runs the whole brain.
- **Secrets:** `~/miki/.env` (git-ignored). Google OAuth files in `~/miki/data/google_calendar/`
  (copied from the laptop with `scp`, because the OAuth browser flow can't run over SSH).
- **Vault sync:** Syncthing (user service) shares `~/miki/obsidian` with the laptop's Obsidian vault.
- **Timezone:** the server clock is UTC. `app/headless.py` applies `MIKI_TIMEZONE` (e.g.
  `Europe/Amsterdam`) to the process with `time.tzset()`, so focus times, the 21:00 recap, quiet hours
  and the morning brief follow the owner's clock. Without it, all of those are off by 1-2 hours.

### Everyday server commands

```bash
ssh miki@<server-ip>
systemctl --user status miki-phone             # running?
journalctl --user -u miki-phone -n 100 --no-pager   # stdout/stderr (startup line, crashes)
journalctl --user -u miki-phone -f             # follow live
tail -f ~/miki/data/phone.log                  # the app's own log (INFO): turns, link, bot, focus
systemctl --user restart miki-phone
~/miki/deploy/update.sh                        # git pull --ff-only, pip install -r, restart, status
free -h; df -h; uptime                         # health
```

On a healthy start `journalctl` shows:
`Miki's brain is running. Link: 127.0.0.1:8765 (reach it through SSH). Phone: @<bot> (linked).`

---

## 3. Shipping a change

1. Develop and test **on the laptop** (`.venv\Scripts\python.exe -m pytest -q`; everything must pass).
2. For anything that runs on the brain, remember it runs on **Linux, without a screen, on UTC with
   `MIKI_TIMEZONE`, without pywebview**. Guard Windows-only code (`sys.platform`, `hasattr(time, "tzset")`,
   `ctypes.WinDLL` inside functions). `single_instance.acquire_named()` is a no-op off Windows.
3. Commit and push to `main` (only when the owner asks you to commit).
4. On the server: `~/miki/deploy/update.sh`, then check `journalctl` for the startup line.
5. If you added a dependency, add it to `requirements.txt` (update.sh installs it). Laptop-only
   dependencies (pywebview, pyinstaller, pillow) are installed by `build_scripts/build_exe.sh`, not
   by `requirements.txt`.
6. If you changed the laptop side (hands, thin dashboard, focus desk, web page), rebuild the exe
   (`bash build_scripts/build_exe.sh` from Git Bash) or run `miki.pyw` from source, and restart the
   hands agent (it's a separate process: end `pythonw.exe -m app.hands` / `Miki.exe --hands`, or log out
   and in if it autostarts).
7. **Link protocol changes:** `app/link/protocol.py` `VERSION` must be bumped when the wire format
   changes incompatibly; the client refuses a mismatched server with a readable message. Deploy the
   server and update the laptop together.

**Data never goes through git.** `data/`, `.env`, `obsidian/` are git-ignored. Moving state between
machines is `scp` (e.g. `scp data\focus.json data\focus_log.jsonl miki@<server-ip>:~/miki/data/`), then
restart the service. Back up before touching live data: `cp -a ~/miki/data ~/miki-data-$(date +%F)`.

---

## 4. Configuration (.env)

Which role a machine plays is decided **only** by `.env`:

| Machine | `MIKI_LINK_TOKEN` | `MIKI_SERVER` | `TELEGRAM_BOT_TOKEN` | What `app.headless` / `Miki.exe` become |
|---|---|---|---|---|
| Server | set | **empty** | set | `app.headless` → `run_brain()` (hub + WebApi + bot + focus clock) |
| Laptop | same value | `miki@<server-ip>` | **empty** | `Miki.exe` → thin dashboard; `app.headless` / `app.hands` → hands agent |
| Old single-machine setup | empty | empty | set | local dashboard with its own brain, as before |

Rules:
- **Never** set `MIKI_SERVER` on the server, and never run a brain on the laptop once the server is
  the brain: two brains means two diverging memories.
- **Only one process may poll a Telegram bot token.** Two pollers get 409 conflicts and
  duplicate/missing replies. The laptop's `TELEGRAM_BOT_TOKEN` stays empty.
- `MIKI_LINK_TOKEN` must be ≥ 24 characters (`python -m app.link token` makes one) and identical on
  both machines. A shorter value counts as unset.

Every setting the code reads (grep `os.getenv` to refresh this list):

| Group | Variables |
|---|---|
| Required | `OPENAI_API_KEY` |
| Model | `MIKI_MODEL` (default `gpt-4o-mini`), `MIKI_MEMORY_MODEL`, `MIKI_EMBEDDING_MODEL`, `MIKI_TRANSCRIBE_MODEL`, `MIKI_TTS_MODEL`, `MIKI_TTS_VOICE`, `MIKI_HISTORY_MESSAGES` (30), `MIKI_MAX_TOOL_ITERATIONS`. `/model` choice persists in `data/model.json` (currently `gpt-4.1-nano`) and wins over `MIKI_MODEL`. |
| Link | `MIKI_LINK_TOKEN`, `MIKI_SERVER`, `MIKI_SSH_KEY`, `MIKI_SSH_PORT`, `MIKI_LINK_PORT` (8765, server loopback), `MIKI_LINK_LOCAL_PORT` (18765, laptop), `MIKI_LINK_ADDRESS` (host:port, bypasses SSH; tests and dev only) |
| Env | `MIKI_ENV` (`production` on the server), `MIKI_TIMEZONE`, `MIKI_UI` (`gui`/`cli`), `MIKI_ALLOW_MULTIPLE` (disables all single-instance mutexes; tests only), `MIKI_GPU` |
| Memory / RAG | `OBSIDIAN_VAULT_PATH` (must exist, `obsidian/` on the server), `MIKI_MEMORY_INTELLIGENCE_ENABLED`, `MIKI_MEMORY_CANDIDATE_THRESHOLD`, `MIKI_MEMORY_CONFIDENT_THRESHOLD`, `MIKI_RAG_ENABLED`, `MIKI_RAG_TOP_K`, `MIKI_RAG_CHUNK_SIZE`, `MIKI_RAG_CHUNK_OVERLAP`, `MIKI_RAG_SIMILARITY_THRESHOLD`, `MIKI_RAG_INDEX_PATH` |
| Phone | `TELEGRAM_BOT_TOKEN`, `MIKI_PHONE`, `MIKI_QUIET_HOURS` (`23:00-08:00`), `MIKI_MAIL_WATCH_SECONDS`, `MIKI_MAIL_TRIAGE`, `MIKI_PHONE_CLEANUP_HOURS` (24: Miki deletes chat messages, yours and its own, once this old; `0` keeps everything. Telegram only lets a bot delete messages under 48 h old, and only ones the bot saw after this was deployed) |
| Tools | `MIKI_TOOLS_ENABLED`, `MIKI_CALENDAR_ENABLED`, `MIKI_CALENDAR_REQUIRE_CREATE_CONFIRMATION`, `MIKI_GOOGLE_CALENDAR_ID`, `MIKI_GOOGLE_CREDENTIALS_PATH`, `MIKI_GOOGLE_TOKEN_PATH`, `MIKI_GOOGLE_DRIVE_FOLDER_NAME`, `MIKI_GOOGLE_MAPS_API_KEY`, `MIKI_WEATHER_ENABLED`, `WEATHER_PROVIDER`, `WEATHER_API_KEY`, `WEATHER_DEFAULT_LOCATION`, `WEATHER_UNITS`, `WEATHER_CACHE_TTL_SECONDS` |
| Inbox | `MIKI_INBOX` (on), `MIKI_INBOX_MODEL` (`gpt-4.1-mini`: reads mail and photos) |
| Plan | `MIKI_PLAN` (on), `MIKI_PLAN_MODEL` (`gpt-4.1-mini`: reads requests; nano was tested and drops items) |
| Focus (server reads the timing ones, laptop reads the screen ones) | `MIKI_FOCUS`, `MIKI_FOCUS_MINUTES`, `MIKI_FOCUS_BREAK_MINUTES`, `MIKI_FOCUS_RECAP_TIME`, `MIKI_FOCUS_BAN` (server), `MIKI_FOCUS_SCREEN1_URL`, `MIKI_FOCUS_SCREEN2_URL`, `MIKI_FOCUS_SWAP_SCREENS`, `MIKI_CHROME_PATH`, `MIKI_FOCUS_PORT` (laptop) |

`.env` is loaded by `app/core/config.py` at import, and explicitly at the top of `app/headless.py`,
`app/main.py` and `app/hands/agent.py` (they read link settings before config is imported).

---

## 5. State on disk (server: `~/miki/data`, all git-ignored)

| Path | What | Notes |
|---|---|---|
| `obsidian/` (vault) | memories as Markdown notes + `Miki/Graph`, `Miki/Journal` | Source of truth for memory. Plain `write_text` (not atomic): reads and writes are serialised in `MikiCore` (see §7). |
| `data/memories/`, `data/memory_candidates/`, `data/memory_training/` | JSON store / pending candidates / decision logs | |
| `data/conversations/` | chat history (one never-closed session) | only the last `MIKI_HISTORY_MESSAGES` are sent to the model |
| `data/rag_index/index.json` | local vector index | atomic writes (`tmp` + replace); rebuilt at brain start |
| `data/profile.json`, `data/learned_messages.json` | portrait + interview/learner bookkeeping | |
| `data/model.json` | `/model` choice | |
| `data/phone.json` | Telegram pairing (hashed code, owner chat id) + prefs | Deleting it unpairs the phone. |
| `data/mail_triage.json`, `data/mail_dismissed.json` | triage cache, dismissed/snoozed mail | |
| `data/focus.json` (+ `.bak`, `.corrupt`), `data/focus_log.jsonl` | focus rounds, stats, bans; append-only round log | move from laptop once with `scp` |
| `data/plan.json` (+ `.corrupt`) | `/plan`: your places (travel times, usual lengths, study place), the draft, the active plan (its calendar event ids, which nudges went out) | Atomic writes. Deleting it forgets the places. Miki's calendar events carry "Planned by Miki (/plan)." in their description. |
| `data/inbox.json` (+ `.corrupt`) | read mail ids, undo batches (calendar event ids Miki added on its own), pending reminders, notices held back by quiet hours | Atomic writes. Events Miki adds carry "Added by Miki from …" in their description. |
| `data/google_calendar/credentials.json`, `token.json` | Google OAuth | Consent screen must be **published**, or the refresh token dies after 7 days. |
| `data/backups/` | vault backups before graph rebuilds | |
| `data/phone.log` | the brain's app log (server) | INFO level |
| laptop `data/hands.log` | hands agent + tunnel log | |
| laptop `data/focus-chrome/`, `data/webview/` | focus Chrome profile, WebView2 cache | laptop only |

---

## 6. The link (brain ↔ hands), in detail

**Transport.** The hands agent runs
`ssh -N -T -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -L 127.0.0.1:18765:127.0.0.1:8765 miki@<server-ip>`
(`app/link/tunnel.py`). `BatchMode` means ssh never prompts, so the key must work without a
passphrase or through ssh-agent, and the server's host key must already be in `known_hosts`. ssh
errors are translated into plain sentences by `explain_ssh_error` and shown in the dashboard.

**Wire format** (`app/link/protocol.py`): one JSON object per line, max 8 MB.
Handshake: server `hello{version, nonce}` → client `auth{roles, mac=HMAC-SHA256(token, nonce|roles), state}`
→ server `welcome` or close. The token never crosses the wire. Roles: `dashboard`, `hands`. The client
pings every 20 s, and either side drops the line after 65 s of silence. The client reconnects forever
with backoff (1,2,3,5,8,13 s). A newer connection for a role replaces the older one.

**Messages**

| Direction | Type | Meaning |
|---|---|---|
| dashboard → brain | `call{method, args:[str]}` | only `process_message`, `open_mail`, `dismiss_mail` (whitelist in `remote.py` `DASHBOARD_CALLS`) |
| dashboard → brain | `repaint` | page reloaded, so the brain calls `WebApi.dashboard_connected()` |
| brain → dashboard | `js{code}` | JavaScript to run in the page (everything the WebApi used to `evaluate_js`) |
| brain → dashboard | `open_url{url}` | laptop opens it **only** if it starts with `https://mail.google.com/` |
| brain → hands | `focus.arm{sites, apps, until}` | lay out screens, clean slate, pet out, start bouncing until `until` |
| brain → hands | `focus.disarm`, `focus.bans{sites,apps}`, `focus.until{until}`, `focus.beep` | |
| brain → hands | `focus.pet{op: start/timeline/mood/say/info/stop, ...}` | the pet, driven by the server's `FocusService` |
| hands → brain | `focus.bounce{kind: app/site, label}` | counted by `FocusService.laptop_bounced` |
| hands (at login) | `state{focus_armed, focus_problem, host}` | used by `RemoteHands.problem()` and `FocusService.hands_connected()` |

**Focus across the link.** `FocusService(hands=RemoteHands(hub))` keeps the whole timeline on the
server. `self.pet` is a `RemotePet`, beeps go to the laptop, and `_arm()` / `_disarm()` send
`focus.arm` / `focus.disarm`. Local mode (no `hands`) uses a `FocusDesk` on the same machine, so the
tests and a single-PC setup work unchanged. Reconnect rules (`hands_connected`): mid-round and the
laptop isn't armed → re-arm; laptop still armed → refresh bans/until/pet only; round over → disarm.
**Failsafe:** the agent stops guarding by itself `GRACE_SECONDS` (120) after `until` if the brain
never says stop. No laptop connected → `/focus` refuses with a clear message instead of starting an
invisible round.

**The dashboard across the link.** The server runs the unchanged `WebApi` with `_window = RemoteWindow(hub)`.
The laptop page's `window.pywebview.api.*` calls go to `ThinApi`, which forwards them. `WebApi._page_open`
is False when no dashboard is connected: the face loop then sends nothing, and the 5-minute widget
refresh skips Google entirely.

**Security model (don't weaken it):** hub on loopback only; SSH key auth; HMAC token; whitelist of
dashboard calls, each taking one string capped at 20 000 chars; hands input validated (`_clean_list`:
lists ≤ 500, strings ≤ 120, apps must end `.exe`; pet ops whitelisted); the laptop opens only Gmail
URLs. The Telegram bot stays locked to one paired chat id (one-time 6-digit code, hashed, 10-min
expiry, lockout after 5 wrong tries); strangers get silence, groups are ignored, stale messages are
skipped, and the token is scrubbed from errors.

---

## 7. Performance: why chat is fast now, and how to keep it fast

Measured 2026-09-29 (gpt-4.1-nano, empty test data): a turn used to take **3-8 s**, because
`MikiCore.process_user_input` ran the memory pipeline (evaluate → extract → compare → store → embed,
~3-4 s of model calls) **before** returning the reply. Now:

- `process_user_input(text, on_memories=callback)` returns as soon as the model answers
  (**~1.5-2.5 s**, first call after start ~3.5 s for the TLS warm-up). The memory pipeline runs on
  one background worker (`miki-memory` thread), in message order, and calls the callback only if
  something was learned. Dashboard: toast. Phone: a separate "🧠 Remembered" message. Without the
  callback (CLI, Tk GUI, tests) it's synchronous as before.
- Store and index operations (`create/update/delete/list/get/count_memories`,
  `index_memory/remove_memory/retrieve_context_for_query`) are serialised per operation by
  `MikiCore._memory_lock` (`_serialise`), so a read never sees a half-written note, and the next
  reply never waits for the previous message's model calls.
- Every turn logs `Turn answered in X s (memory + search A s, model B s)` and
  `Memory pipeline took X s in the background` to `data/phone.log`. **Start every speed complaint
  there.**

Remaining latency and ideas, in order of payoff: (1) streaming replies (Telegram: edit the message as
tokens arrive; dashboard: `updateAssistantBubble` progressively); (2) the RAG query embedding
(0.3-1.4 s) could run in parallel with building the prompt; (3) the memory evaluator still spends a
model call on trivial messages ("hey miki"), so tighten `_is_trivial_message`; (4) recall re-reads all
vault notes per message (fine at dozens, cache it at hundreds).

Dashboard resource rules (measured, enforced by `tests/test_efficiency.py`): an idle page must render
zero frames (no infinite CSS animations or always-on rAF; gate on `body[data-state]`); WebView2 runs
with `--disable-gpu` and lean flags (`app/interfaces/webview_host.py`); the laptop's thin dashboard
and hands agent must not import the brain (`openai`, `networkx`, `yaml`): `app/core/__init__.py` is
lazy for this reason (thin side imports in ~0.2 s instead of ~2.4 s).

---

## 8. Debugging playbook

| Symptom | Look at | Usual cause / fix |
|---|---|---|
| Bot silent, service `active` | `tail data/phone.log`; is the laptop also polling? | Same bot token on two machines (409). Empty it on the laptop. |
| Service `failed` | `journalctl --user -u miki-phone -n 50` | Bad `.env` value, `OBSIDIAN_VAULT_PATH` missing, dependency not installed (`update.sh`). |
| Dashboard says "Brain: offline" | its chat line (plain-English reason), laptop `data/hands.log` | Key needs a passphrase / unknown host key (`ssh miki@<ip> echo ok` must print only `ok`); token mismatch ("server refused this laptop"); brain not running; `MIKI_LINK_TOKEN` < 24 chars. |
| Dashboard connects but is blank | server `phone.log` for `Link: dashboard connected` | `dashboard_connected()` repaints; a JS error in the page would show in WebView2 devtools (run from source). |
| `/focus` says the laptop isn't connected | laptop: is `app.hands` running? `hands.log` | Start it (`python -m app.hands`), or `--install-autostart`. Asleep laptop = no hands; that's expected. |
| `/focus` starts but nothing moves on screen | `hands.log` for `Hands: focus.arm failed` | Chrome missing (`focus_problem`), exe path, monitors. Laptop-side code is `desk.py`. |
| Focus times / recap an hour or two off | `MIKI_TIMEZONE` on the server | Set it; restart. |
| Calendar/mail stop after ~a week | `phone.log` "authentication is required" | OAuth consent screen still in Testing: publish it, redo the laptop login, re-`scp` token.json. |
| Slow replies | `Turn answered in ...` lines | See §7. If "model" dominates, try `/model`; if "memory + search", look at RAG/recall. |
| Memory not saved | `Memory pipeline took ... (0 learned)`, `data/memory_training` logs | Evaluator judged it not memory-worthy; `/remember` forces it. |
| Vault edits not on the laptop | Syncthing UI via `ssh -L 8384:localhost:8384 miki@<ip>` | Device/folder not shared or paused. |

Reproduce the brain locally without touching real data: run it in a scratch folder with an empty
vault, `TELEGRAM_BOT_TOKEN=""`, a test `MIKI_LINK_TOKEN` and `MIKI_LINK_PORT=18999`, then connect a
`LinkClient(("127.0.0.1", 18999), token, ["dashboard"], on_message=print)` and send
`{"type":"call","method":"process_message","args":["hi"]}`. That's how the split was verified. Do
**not** copy the owner's `data/` or vault into scratch folders (personal data), and never run a
second bot poller against the real token.

---

## 9. Building new features: where things go

- **Anything that thinks, remembers, schedules or pushes** → server side. New proactive pushes go
  through `PhoneNotifier.send` (quiet hours, dedupe, hourly limit, focus hold; `direct=True` only for
  time-critical focus timers). New phone screens are pure builders in `app/phone/ui.py` (callback data
  scheme in its docstring); `bot.py` is the controller; `backend.py` is what the phone may ask.
- **Anything that needs the laptop's screen, apps or files** → a new `hands` message type: add the
  sender on the server (a method on `RemoteHands` in `app/link/remote.py`), the handler in
  `HandsAgent.handle` (**validate every field**, it's input from the network), and a test in
  `tests/link/test_link.py` over the real hub/client. Handlers run on one worker queue, so a slow
  handler delays later ones: keep them short or thread them.
- **Anything the dashboard page needs** → the server-side `WebApi` pushes JS as it always did. A new
  page → Python call must be added to `ThinApi`, `DASHBOARD_CALLS`, and the local `WebApi`.
- **Slash commands** exist in three places: dashboard (`WebApi._handle_command`), phone
  (`bot.py` + `backend.command`), and CLI. Keep them consistent.
- **Tools** (calendar/mail/drive/maps/weather) live in `app/tools/<name>/` with `wiring.py`; anything
  that writes needs the confirmation flow (`PendingToolAction`).
- **`/plan`** (built 2026-10-01, `app/plan/`): the AI only turns the sentence into wishes; `schedule.py`
  does every minute (tries each order of the stops, study as focus-length rounds, lunch at the nearest
  break, calendar events as obstacles, "ends_day" anchors like dinner last) and `ways_to_fit` proposes
  checked fixes. "Looks good" writes one calendar event per block (`confirm=True`: the button *is* the
  confirmation) and the `miki-plan` clock (20 s tick, named lock `MikiPlan`) sends nudges through
  `PhoneNotifier.send(direct=True)` keyed `plan:<day>:<created>:<block>`. Phone callbacks `pl:*`.
- **Inbox reader** (built 2026-10-01, `app/inbox/`): the owner chose *automatic* calendar adds, so the guardrails live in code:
  email/photo text is untrusted, the AI only returns JSON that `findings.parse` checks (future dates only, https links only,
  ≤10 items, ≤8 events per source); Miki's only actions are add a tagged calendar event (deduped, one undoable batch), text the
  owner, remember a fact. It never sends mail, opens links, signs up or deletes anything it didn't add. The first mail pass
  only records what's already in the inbox (no flood). Notices wait out quiet hours in `data/inbox.json`'s outbox.
- Parked feature ideas (owner-approved direction): calendar-event nudges beyond `/plan`, mail "draft a reply" button (Gmail draft only, never send), weekly wrap-up, `/health` + off-site vault backup + OAuth-expiry warning. Memory work still open:
  contradiction history, recall cache, opt-in mining of calendar/mail.

---

## 10. Tests and conventions

- Run everything: `.venv\Scripts\python.exe -m pytest -q` (433 tests, ~15 s). Suites with real content:
  `tests/focus/` (focus), `tests/plan/` (scheduler, request checks, service with fake AI/calendar, phone flow),
  `tests/link/` (link over real sockets, focus split, tunnel command),
  `tests/test_background_memory.py`. **Many other files under `tests/` are committed as 0-byte
  placeholders**: don't assume coverage exists for phone/memory/RAG just because a file does.
- Tests must never touch the real `data/`, vault, Telegram token or the owner's screen: use `tmp_path`,
  fakes (`tests/focus/fakes.py`: `FakeDesktop`, `FakeChrome`, `FakePet`), `lock_name=None` /
  `MIKI_ALLOW_MULTIPLE=1`, and `MIKI_LINK_ADDRESS` instead of ssh.
- Code style: plain-English docstrings that explain *why*, user-facing strings in Miki's friendly
  voice, no new heavy dependencies (the phone and link layers are stdlib-only on purpose).
- Tooling gotchas on the owner's Windows machine: the Bash tool is Git Bash (use `/c/Users/...` paths
  or quote Windows paths); heredocs can mangle `\n`, so write scripts with the file tool; the repo's
  `.venv` is the one to use (`.venv/Scripts/python.exe`).
- Commit only when the owner asks. End commit messages with the attribution line your harness gives you.

---

## 11. Emergency procedures

- **Stop Miki everywhere now:** server `systemctl --user stop miki-phone`; laptop: close the dashboard,
  end `app.hands` (Task Manager: `pythonw.exe` / `Miki.exe --hands`). Focus mode never kills
  anything or changes system settings, so stopping the hands agent frees the laptop immediately.
- **Laptop stuck in focus:** `/focus stop` from the phone. If the brain is down, the agent stands down
  by itself 2 minutes after the round's end, or end the hands process.
- **Leaked link token:** generate a new one (`python -m app.link token`), put it in both `.env` files,
  restart both. Leaked SSH key: remove it from `~/.ssh/authorized_keys` on the server.
- **Leaked Telegram token:** revoke in @BotFather (`/revoke`), put the new one in the server `.env`,
  restart, re-pair with `/phone`.
- **Rollback a bad deploy:** `cd ~/miki && git log --oneline -5 && git checkout <good-sha> && systemctl --user restart miki-phone`
  (then fix forward on `main` and `git checkout main` before the next `update.sh`).
- **Corrupted focus state:** `focus.json` restores itself from `.bak` (the bad file becomes
  `focus.corrupt`). `focus_log.jsonl` is append-only and never trimmed.
