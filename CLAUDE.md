# Miki

Personal AI assistant. **Its brain runs 24/7 on a Contabo Linux server; the owner's Windows laptop is only its hands**
(the dashboard window + a focus-mode agent), connected through an SSH tunnel.

**Read `docs/SERVER.md` before changing anything**: architecture, the server, deploying, the link protocol, debugging,
where new features go, and the rules below in full.

Non-negotiables:
- One brain, one memory: never run a second brain or a second Telegram poller on the laptop (`MIKI_SERVER` set = laptop).
- The link hub stays on the server's loopback; validate every field arriving over the link.
- Nothing on the brain's import path may import `webview`; the laptop's thin side must not import the brain.
- Tests never touch real `data/`, the vault, the bot token or the owner's screen. Run `.venv\Scripts\python.exe -m pytest -q`.
- Ship: commit/push (only when asked), then on the server `~/miki/deploy/update.sh`.
