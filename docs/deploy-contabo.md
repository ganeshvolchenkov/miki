# Running Miki 24/7 on a Contabo server — full walkthrough

You've never set up a server before, so this spells out every step and every word. It moves
just the **bot + memory brain** (Telegram, chat, memory, mail/calendar/weather) onto an
always-on Linux box so Miki keeps responding when your laptop is off. It does **not** cover
n8n (that's a later, separate pass) or the desktop dashboard / `/focus` mode's screen control
(those need your actual monitor and only make sense running on your own PC).

Budget about 45–60 minutes for the first pass. Everything here is one-time setup — after
today, shipping a new Miki feature is just "push code, run one script."

---

## Words you'll see a lot

You don't need to memorize these, just come back here if something below doesn't make sense.

- **VPS (Virtual Private Server)** — a computer you rent that runs 24/7 in a data center. That's
  the Contabo box you're buying.
- **SSH** — the standard way to open a secure remote terminal on another computer over the
  internet. `ssh miki@1.2.3.4` = "open a terminal session on the server at that address, as
  the user `miki`."
- **SSH key** — a pair of files: a private key (stays on your laptop, never shared) and a
  public key (you give this to the server). The server lets in anyone who can prove they hold
  the matching private key. It replaces typing a password every time and is much harder to
  break into.
- **root** — the server's all-powerful admin account. You use it once, to set things up, then
  stop using it day-to-day (see Part 2 for why).
- **sudo** — "run this one command as root," while logged in as a normal user. Prefixing a
  command with `sudo` is how a non-root user does admin things.
- **Terminal / shell** — the black text window where you type commands. On your Windows laptop
  that's PowerShell; on the server (once you SSH in) it's Bash.
- **systemd service** — a way to tell Linux "keep this program running forever, and restart it
  automatically if it crashes or the server reboots." This is what keeps Miki's bot alive
  without you doing anything.
- **venv (virtual environment)** — an isolated folder of Python packages just for one project,
  so Miki's dependencies never clash with anything else on the server (like n8n, later).
- **.env file** — a plain text file holding secrets and settings (API keys, tokens) that the
  app reads on startup. It's never committed to git.

---

## Part 1: Buy the server

Order a **Contabo Cloud VPS 4** (4 vCPU / 8 GB RAM / 100 GB SSD, ~€5.32/mo) from the Contabo
customer control panel (my.contabo.com):

- Image: **Ubuntu 24.04**
- Region: closest to you
- SSH key: Contabo may offer to add one during checkout — skip that for now, we'll generate one
  properly in Part 2 and add it ourselves. It's fine either way.

After checkout, Contabo emails you the server's **IP address** and an initial **root password**.
Keep that email until Part 2 is done.

---

## Part 2: Connect to the server for the first time

### 2.1 Generate an SSH key on your laptop

You don't have one yet, so make one. Open **PowerShell** on your Windows machine and run:

```powershell
ssh-keygen -t ed25519 -C "miki-server"
```

- When it asks *"Enter a file in which to save the key"* — just press **Enter** to accept the
  default location.
- When it asks for a passphrase — you can press **Enter** twice to leave it empty (simpler for
  now), or set one if you want an extra layer of protection (you'd type it once per login).

This creates two files in `C:\Users\<you>\.ssh\`: `id_ed25519` (private — never share this) and
`id_ed25519.pub` (public — this is what you hand to servers).

### 2.2 Log in as root (password, one-time)

```powershell
ssh root@<server-ip>
```

Replace `<server-ip>` with the address from Contabo's email. First time connecting to any new
server, you'll see something like:

```
The authenticity of host '...' can't be established.
Are you sure you want to continue connecting (yes/no)?
```

Type `yes` and press Enter — this is normal, it happens once per new server. Then paste in the
root password from Contabo's email (right-click to paste in most Windows terminals; you won't
see characters appear as you type/paste — that's normal for passwords).

Once you're in, immediately change the password so the emailed one stops being valid:

```bash
passwd
```

(Type a new password twice when asked.)

### 2.3 Copy your public key to the server

Still logged in as root, run this **on the server**:

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
nano ~/.ssh/authorized_keys
```

`nano` opens a simple text editor inside the terminal. Now, on **your laptop**, open a second
PowerShell window (leave the SSH session open!) and print your public key:

```powershell
Get-Content $env:USERPROFILE\.ssh\id_ed25519.pub
```

Copy that entire single line of text (starts with `ssh-ed25519 ...`). Back in the `nano` window
on the server, paste it in (right-click), then save and exit: **Ctrl+O**, **Enter**, **Ctrl+X**.

Lock down the permissions (SSH refuses to use a key file that's too open):

```bash
chmod 600 ~/.ssh/authorized_keys
```

### 2.4 Test key login before doing anything else

From a **new** PowerShell window (keep the old root session open, just in case):

```powershell
ssh root@<server-ip>
```

It should log you in with no password prompt this time. If it works, great — close the extra
window. If it doesn't, you still have your original session open to fix `authorized_keys`
before you lock yourself out.

---

## Part 3: Create a non-root user

Running everything as `root` forever is unnecessary risk (one typo in the wrong command can do
real damage). Create an everyday user instead:

```bash
adduser miki
```

It'll ask for a password (set one) and some optional info you can skip (just press Enter through
those). Then:

```bash
usermod -aG sudo miki
rsync --archive --chown=miki:miki ~/.ssh /home/miki
```

That last command copies your `authorized_keys` (and its permissions) from root's home to the new
user's home, so the same key logs into both.

**Test it now, in a new window, before closing your root session:**

```powershell
ssh miki@<server-ip>
```

Once that works, all future steps in this guide happen as `miki`, not `root`.

### 3.1 Basic firewall

```bash
sudo ufw allow OpenSSH
sudo ufw enable
```

Type `y` to confirm. Miki itself needs **no inbound ports at all** — the Telegram bot reaches
out to Telegram's servers, nothing reaches in — so this can stay locked down indefinitely.

### 3.2 (Optional, do this last) Turn off password login entirely

Once you're confident key login works for `miki` and you'd survive if you had to fix something,
you can stop the server from accepting passwords at all (removes a whole class of attacks):

```bash
sudo nano /etc/ssh/sshd_config
```

Find the line `#PasswordAuthentication yes`, change it to `PasswordAuthentication no` (remove the
`#`), save (Ctrl+O, Enter, Ctrl+X), then:

```bash
sudo systemctl restart ssh
```

---

## Part 4: Install Python and get Miki's code

From here on, you're logged in as `miki` (`ssh miki@<server-ip>`).

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git
git clone https://github.com/ganeshvolchenkov/miki.git ~/miki
cd ~/miki
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

What this does, line by line: updates the list of available packages, installs Python/git;
downloads Miki's actual code from GitHub into `~/miki`; creates an isolated Python environment
just for Miki (`.venv`); installs everything Miki needs into it. The last line can take a minute
or two — it's downloading several packages.

---

## Part 5: Configure secrets (.env)

```bash
cp .env.example .env
nano .env
```

Fill in (same values as your laptop's `.env` — open that one locally to copy from, don't retype
from memory):

- `OPENAI_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `MIKI_ENV=production`
- `OBSIDIAN_VAULT_PATH=obsidian/` — a fresh folder under `~/miki` that Part 7 will sync back to
  your laptop's real Obsidian vault.

Save with Ctrl+O, Enter, then exit with Ctrl+X.

**Important — do this once the server is confirmed working:** go back to your **laptop's**
`.env` and blank out `TELEGRAM_BOT_TOKEN` (or stop running the dashboard's phone service).
Two processes checking Telegram for messages with the *same* bot token fight each other —
Telegram will reject one of them, and you'll get duplicate or missing replies. Everything else
about the dashboard (chat, memory, `/focus` locally) keeps working fine with that one line
blank; only the phone/Telegram piece needs to be exclusive to one machine.

---

## Part 6: Google Calendar (skip this whole part if you don't use it in Miki)

Google's login flow opens a browser on your screen, which doesn't exist over SSH. So you do the
*first* login on your laptop (where you already have Miki running normally), and copy the result
to the server:

```powershell
# on your laptop, in the Miki folder, after Calendar has worked at least once locally:
scp data/google_calendar/credentials.json data/google_calendar/token.json miki@<server-ip>:~/miki/data/google_calendar/
```

Also check: if your Google Cloud project's OAuth consent screen is still in **"Testing"** mode,
Google expires that login every 7 days — fine on your laptop where you just click through the
prompt again, but it'll silently break on an unattended server. Go to Google Cloud Console →
APIs & Services → OAuth consent screen, and **publish** the app (a one-time button click) to
stop that.

---

## Part 7: Sync the Obsidian vault back to your laptop

The vault Miki writes to now lives on the server (`~/miki/obsidian/`). To actually *see* it in
your Obsidian app, mirror it to your laptop with **Syncthing** — free, no account, and it syncs
in both directions automatically whenever both machines are online.

1. On the server: `sudo apt install -y syncthing`. Run it as a background service for your user:
   ```bash
   systemctl --user enable --now syncthing
   sudo loginctl enable-linger miki
   ```
2. Syncthing's control screen is a web page normally only reachable from the server itself. To
   view it from your laptop, open a tunnel (run this on your laptop):
   ```powershell
   ssh -L 8384:localhost:8384 miki@<server-ip>
   ```
   Leave that window open, then visit `http://localhost:8384` in your laptop's browser — that's
   the server's Syncthing UI, tunneled securely over SSH.
3. Install the **Syncthing desktop app** on your laptop too (from syncthing.net) — it has its own
   UI at `http://localhost:8384` locally.
4. In each Syncthing UI, click "Add Remote Device" and paste in the other one's Device ID (shown
   at the top of its UI). Approve the pairing on both sides.
5. On the server's Syncthing, share the `~/miki/obsidian` folder; on your laptop's Syncthing,
   accept it into a folder your local Obsidian vault points at (or make that folder *be* your
   vault).

From then on, edits on either side sync automatically in the background — no more manual steps.

---

## Part 8: Make Miki run forever

This is a **systemd user service** — Linux keeps it running, restarts it if it crashes, and (with
one extra command) restarts it after the server reboots, even though nobody's logged in.

```bash
mkdir -p ~/.config/systemd/user
cp ~/miki/deploy/miki-phone.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now miki-phone
sudo loginctl enable-linger miki
```

That last command is the one that lets the service keep running after you close your SSH
session — without it, the service would stop the moment you log out.

Check it actually started:

```bash
systemctl --user status miki-phone
```

You want to see `Active: active (running)` in green. If instead it says `failed`, run:

```bash
journalctl --user -u miki-phone -n 50 --no-pager
```

That prints the last 50 log lines, which will say what went wrong (usually a missing/typo'd
line in `.env`). Fix it, then:

```bash
systemctl --user restart miki-phone
```

**Final check:** send your Telegram bot a message from your phone. It should reply, exactly like
it does when your laptop runs it. Everything (chat, memory, mail, calendar, weather) works the same
as on your laptop. `/focus` needs one more step, connecting your laptop as Miki's hands: that's
Part 10.

---

## Part 9: Shipping new Miki features later

Your local workflow doesn't change at all — keep building and testing on your laptop like
always. When something is ready to go live on the always-on bot:

```powershell
# on your laptop
git push
```

```bash
# on the server
ssh miki@<server-ip>
~/miki/deploy/update.sh
```

(If it says `Permission denied`, run `chmod +x ~/miki/deploy/update.sh` once, or just call it as
`bash ~/miki/deploy/update.sh`.)

That script pulls the new code, installs any new/changed dependencies, and restarts the bot —
the whole update loop is one push plus one SSH command.

---

## n8n

Not covered yet — we'll add it as its own section once Miki is confirmed working end to end.

---

## Part 10: Your laptop becomes Miki's hands

After this part there is **one Miki**, and she lives on the server. Your laptop no longer has its own
brain or its own memory. It only has:

- the **dashboard window**, which now shows the server's brain (the same chat, memory and widgets
  your phone sees), and
- a small background program called the **hands agent**, which does what needs a real screen: when a
  focus round starts (from your phone or the dashboard) it puts Gemini and Claude on your monitors,
  runs the bouncer and shows the pet.

```
laptop                                          server
hands agent ──── SSH tunnel (your key) ──────▶  Miki's brain (127.0.0.1:8765, not on the internet)
dashboard  ──┘                                   ├─ Telegram bot
                                                 ├─ memory + Obsidian vault
                                                 └─ focus clock, recap, study habits
```

**Why SSH?** The brain only listens on the server's own loopback address, so nothing new is open to
the internet (no firewall change). The laptop reaches it through an SSH tunnel with the key you
already use to log in, and every connection must also know a shared secret, `MIKI_LINK_TOKEN`.

### 10.1 Make the shared secret

On your **laptop**, in the Miki folder:

```powershell
.\.venv\Scripts\python.exe -m app.link token
```

It prints a line like `MIKI_LINK_TOKEN=Qm3...`. Copy it.

### 10.2 Server: switch the brain on

```bash
ssh miki@<server-ip>
nano ~/miki/.env
```

Add these two lines (the token from 10.1, and your timezone, since the server's clock runs on UTC and
focus times, the evening recap, quiet hours and the morning brief should follow *your* clock):

```
MIKI_LINK_TOKEN=Qm3...
MIKI_TIMEZONE=Europe/Amsterdam
```

Make sure `MIKI_SERVER` is **not** set on the server. Then pull the new code and restart:

```bash
~/miki/deploy/update.sh
journalctl --user -u miki-phone -n 20 --no-pager
```

You should see `Miki's brain is running. Link: 127.0.0.1:8765`.

**Bring your focus history along (optional, once):** your study stats and habits so far live in the
laptop's `data/` folder. Copy them up, then restart the brain:

```powershell
scp data\focus.json data\focus_log.jsonl miki@<server-ip>:~/miki/data/
```

```bash
systemctl --user restart miki-phone
```

### 10.3 Laptop: point Miki at the server

Check once that SSH works **without typing anything** (the hands agent can't type a password):

```powershell
ssh miki@<server-ip> echo ok
```

It must print `ok` and nothing else. If it asks for a passphrase, either start the Windows
`ssh-agent` service and run `ssh-add`, or make a separate key without a passphrase and set
`MIKI_SSH_KEY` to it.

Then edit the laptop's `.env`:

```
MIKI_LINK_TOKEN=Qm3...            (the same value as on the server)
MIKI_SERVER=miki@<server-ip>
TELEGRAM_BOT_TOKEN=               (empty: the server runs the bot)
```

Open Miki as usual (`Miki.exe` or `miki.pyw`). The dashboard starts the hands agent by itself,
connects, and the memory pill turns green. Type something: the answer comes from the server.

To have the hands ready even when the dashboard is closed (so `/focus` from your phone always
works when the laptop is on), start them at login:

```powershell
.\.venv\Scripts\python.exe -m app.hands --install-autostart
```

If you'd set up the older "Miki Phone" login launcher (`python -m app.headless --install-autostart`),
it now starts the hands on this laptop instead of a second bot, because `MIKI_SERVER` is set.

### 10.4 Try it

From your phone: `/focus 25 min`. Within a few seconds your laptop opens Gemini and Claude, tucks the
rest away and the pet appears. Open YouTube: it's bounced, and `/focus status` on your phone counts
it. If the laptop is off or asleep, Miki says so instead of starting a round nobody can see.

**If something is off:** the laptop writes `data/hands.log`, and the server logs to
`journalctl --user -u miki-phone`. The dashboard also says in plain words why it can't connect
(wrong key, wrong token, server unreachable).

**Safety net:** the hands agent knows when the round ends. If it loses the server and that time
passes, it stops guarding on its own, so a dropped connection can never leave your laptop stuck in
focus mode.

---

## What stays local (on your own PC, not the server)

Only what needs your actual screen: the dashboard **window** (not its brain) and the hands agent,
which does focus mode's window arranging, bouncing and the pet. Everything that thinks, remembers or
sends you messages runs on the server.

---

## Cheat sheet / troubleshooting

| Symptom | What to try |
|---|---|
| `Permission denied (publickey)` when SSHing | You're using the wrong key, or `authorized_keys` on the server has a typo/wrong permissions. Log in as `root` (or from a session that still works) and re-check Part 2.3. |
| `systemctl --user status miki-phone` says `failed` | `journalctl --user -u miki-phone -n 50 --no-pager` — almost always a missing/wrong value in `~/miki/.env`. |
| Bot doesn't reply on Telegram, service says `active (running)` | Check you didn't leave the *same* bot token running on your laptop too (Part 5's warning) — the two processes fight for messages. |
| Lost your SSH session / can't get back in | If you still have another open terminal to the server, fix things there first before closing it. This is why Part 2.4 and Part 3 both say "test in a new window before closing the old one." |
| Want to reboot the server | `sudo reboot` — thanks to `loginctl enable-linger`, Miki's service comes back up on its own after the reboot finishes (give it ~30 seconds). |
