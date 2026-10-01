#!/usr/bin/env bash
# Rebuild Miki.exe from the current source tree. Run this (from Git Bash / the Bash tool)
# after pulling new code, any time you want the desktop app to reflect the latest changes.
#
# Uses a dedicated .venv (created on first run) with ONLY Miki's actual runtime dependencies --
# deliberately excludes streamlit/customtkinter (the separate, unused Streamlit and Tk UIs also
# in this repo) so the exe stays small and doesn't bundle pandas/numpy/pyarrow along with them.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd -W)"

if [ ! -d .venv ]; then
    echo "==> Creating build venv"
    python -m venv .venv
    .venv/Scripts/pip.exe install --quiet --upgrade pip
fi

echo "==> Installing dependencies"
.venv/Scripts/pip.exe install --quiet -r requirements.txt
.venv/Scripts/pip.exe install --quiet pywebview pyinstaller pillow

echo "==> Building Miki.exe"
rm -rf build
.venv/Scripts/pyinstaller.exe --noconfirm --onefile --windowed --name Miki \
  --icon "${ROOT}/assets/icon.ico" \
  --add-data "${ROOT}/app/interfaces/web;app/interfaces/web" \
  --exclude-module streamlit --exclude-module customtkinter \
  --exclude-module pandas --exclude-module numpy --exclude-module pyarrow \
  --exclude-module altair --exclude-module pydeck --exclude-module narwhals \
  --exclude-module watchdog \
  --distpath . --workpath build --specpath build \
  miki.pyw

rm -rf build
echo "==> Done: Miki.exe"
