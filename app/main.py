from __future__ import annotations

import os


def main() -> int:
    ui_mode = os.getenv("MIKI_UI", "gui").strip().lower()
    if ui_mode == "cli":
        from app.interfaces.cli import run_cli

        return run_cli()

    from app.core import single_instance

    if not single_instance.acquire():
        single_instance.focus_existing_window()
        print("Miki is already running; brought its window to the front.")
        return 0

    try:
        from app.interfaces.web_gui import run_gui
        return run_gui()
    except (ImportError, ModuleNotFoundError):
        from app.interfaces.cli import run_cli

        return run_cli()


def run() -> None:
    """Entry point for `python -m app.main` / miki.pyw: run, then exit immediately so no stray
    thread or WebView2 helper can keep a closed Miki alive in the background."""
    import sys

    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code or 0)


if __name__ == "__main__":
    run()
