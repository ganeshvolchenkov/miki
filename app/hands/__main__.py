import os
import sys

from app.hands.agent import main

if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
