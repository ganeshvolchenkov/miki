"""``python -m app.link token``: print a fresh MIKI_LINK_TOKEN to paste into BOTH .env files (server and laptop)."""

import sys

from app.link.protocol import new_token

if __name__ == "__main__":
    if sys.argv[1:] == ["token"]:
        print(f"MIKI_LINK_TOKEN={new_token()}")
    else:
        print("Usage: python -m app.link token")
