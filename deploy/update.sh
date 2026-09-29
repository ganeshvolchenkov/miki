#!/usr/bin/env bash
# Run this ON THE SERVER whenever you've pushed new Miki code, to pull it and restart the bot.
# Usage: ~/miki/deploy/update.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> Pulling latest code"
git pull --ff-only

echo "==> Installing/updating dependencies"
.venv/bin/pip install -q -r requirements.txt

echo "==> Restarting the phone service"
systemctl --user restart miki-phone
sleep 2
systemctl --user status miki-phone --no-pager -l | head -20
