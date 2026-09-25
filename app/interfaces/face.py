"""Miki's pixel-art mascot: sprite data and frame composition.

Pure data/logic with no GUI-toolkit imports, so the web dashboard (and tests) can use the
face without loading Tk or the rest of the desktop GUI.
"""

from __future__ import annotations

import random

# Base eye colours (kept identical to the Tk GUI theme).
ACCENT = "#ffb238"
ACCENT_BUSY = "#ffd479"

# ---------------------------------------------------------------------------
# Pixel-art face -- a small hand-authored sprite (16x14 "pixels") composed
# from a fixed body shape plus swappable eye/mouth sprites, so each state
# gets its own tiny animation loop. Not a literal face -- a cute mascot icon,
# in the spirit of an app mascot that reacts to what's happening.
# ---------------------------------------------------------------------------
_FACE_BODY_COLOR = "#241708"
_FACE_HIGHLIGHT_COLOR = "#4a3418"

_FACE_BODY = [
    "....BBBBBBBB....",
    "..BBBBBBBBBBBB..",
    ".BBBHBBBBBBBBBB.",
    "BBBBBBBBBBBBBBBB",
    "BBBBBBBBBBBBBBBB",
    "BBBBBBBBBBBBBBBB",
    "BBBBBBBBBBBBBBBB",
    "BBBBBBBBBBBBBBBB",
    "BBBBBBBBBBBBBBBB",
    "BBBBBBBBBBBBBBBB",
    "BBBBBBBBBBBBBBBB",
    "BBBBBBBBBBBBBBBB",
    ".BBBBBBBBBBBBBB.",
    "..BBBBBBBBBBBB..",
]
_FACE_GRID_W = 16
_FACE_GRID_H = len(_FACE_BODY)
assert all(len(row) == _FACE_GRID_W for row in _FACE_BODY), "pixel face body rows must all be the same width"

# Eye/mouth sprites are small transparent ('.') / filled ('X') grids, stamped
# onto a copy of the body at fixed positions -- see _compose_face_frame().
_EYE_OPEN = [
    "....",
    ".XX.",
    "XXXX",
    "XXXX",
    ".XX.",
]
_EYE_BLINK = [
    "....",
    "....",
    "....",
    "XXXX",
    "....",
]
_EYE_WIDE = [
    ".XX.",
    "XXXX",
    "XXXX",
    "XXXX",
    ".XX.",
]
_EYE_LOOK_LEFT = [
    "....",
    "XX..",
    "XXX.",
    "XX..",
    "....",
]
_EYE_LOOK_RIGHT = [
    "....",
    "..XX",
    ".XXX",
    "..XX",
    "....",
]
_EYE_HAPPY = [
    "....",
    "X..X",
    ".XX.",
    "....",
    "....",
]

_MOUTH_CLOSED = [
    "......",
    ".XXXX.",
    "......",
]
_MOUTH_SMALL = [
    ".XXXX.",
    ".X..X.",
    ".XXXX.",
]
_MOUTH_WIDE = [
    "XXXXXX",
    "X....X",
    "XXXXXX",
]

_EYE_L_TOP, _EYE_L_LEFT = 5, 3
_EYE_R_TOP, _EYE_R_LEFT = 5, 10
_MOUTH_TOP, _MOUTH_LEFT = 10, 5

# Per-state animation loops: (left_eye, right_eye, mouth_or_None, duration_ms).
# "speaking" isn't looped from this table -- see _random_speaking_frame().
_FACE_SEQUENCES: dict[str, list[tuple[list[str], list[str], list[str] | None, int]]] = {
    "idle": [
        (_EYE_OPEN, _EYE_OPEN, None, 2600),
        (_EYE_BLINK, _EYE_BLINK, None, 110),
        (_EYE_OPEN, _EYE_OPEN, None, 3600),
        (_EYE_HAPPY, _EYE_HAPPY, None, 700),
    ],
    "listening": [
        (_EYE_WIDE, _EYE_WIDE, None, 420),
        (_EYE_WIDE, _EYE_WIDE, None, 420),
    ],
    "thinking": [
        (_EYE_LOOK_LEFT, _EYE_LOOK_LEFT, None, 480),
        (_EYE_LOOK_RIGHT, _EYE_LOOK_RIGHT, None, 480),
    ],
    "speaking": [
        (_EYE_OPEN, _EYE_OPEN, _MOUTH_CLOSED, 150),
    ],
}

# A one-shot "bored/playful" sequence that plays once after _SUPER_IDLE_DELAY_MS
# of uninterrupted idle, then hands back to the normal idle loop. Includes a
# wink -- the first place we actually use asymmetric left/right eye sprites.
_SUPER_IDLE_SEQUENCE: list[tuple[list[str], list[str], list[str] | None, int]] = [
    (_EYE_HAPPY, _EYE_HAPPY, None, 900),
    (_EYE_LOOK_LEFT, _EYE_LOOK_LEFT, None, 500),
    (_EYE_LOOK_RIGHT, _EYE_LOOK_RIGHT, None, 500),
    (_EYE_OPEN, _EYE_OPEN, None, 350),
    (_EYE_BLINK, _EYE_OPEN, None, 260),  # wink
    (_EYE_OPEN, _EYE_OPEN, None, 900),
]

# Weighted mouth shapes for the randomized "talking" flap -- favors small/medium
# openings with the occasional wide one and brief closed beat, closer to real
# speech rhythm than a fixed cyclic loop.
_SPEAKING_MOUTH_WEIGHTS: list[tuple[list[str], int]] = [
    (_MOUTH_CLOSED, 2),
    (_MOUTH_SMALL, 5),
    (_MOUTH_WIDE, 3),
]

_FACE_STATE_EYE_COLOR = {
    "idle": ACCENT,
    "listening": ACCENT_BUSY,
    "thinking": ACCENT_BUSY,
    "speaking": ACCENT,
}
_FACE_STATE_BOUNCE = {
    "idle": (0.06, 2.0),
    "listening": (0.10, 3.2),
    "thinking": (0.03, 1.4),
    "speaking": (0.09, 3.4),
}


def _compose_face_frame(eye_left: list[str], eye_right: list[str], mouth_sprite: list[str] | None) -> list[list[str]]:
    grid = [list(row) for row in _FACE_BODY]

    def stamp(sprite: list[str], top: int, left: int) -> None:
        for r, row in enumerate(sprite):
            for c, ch in enumerate(row):
                if ch == "X":
                    grid[top + r][left + c] = "E"

    stamp(eye_left, _EYE_L_TOP, _EYE_L_LEFT)
    stamp(eye_right, _EYE_R_TOP, _EYE_R_LEFT)
    if mouth_sprite is not None:
        for r, row in enumerate(mouth_sprite):
            for c, ch in enumerate(row):
                if ch == "X":
                    grid[_MOUTH_TOP + r][_MOUTH_LEFT + c] = "M"

    return grid


def _random_speaking_frame() -> tuple[list[str], list[str], list[str] | None, int]:
    mouths, weights = zip(*_SPEAKING_MOUTH_WEIGHTS)
    mouth = random.choices(mouths, weights=weights, k=1)[0]
    duration = random.randint(90, 190)
    return (_EYE_OPEN, _EYE_OPEN, mouth, duration)
