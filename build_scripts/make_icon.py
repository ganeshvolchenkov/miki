"""One-off: render Miki's pixel-art mascot (app/interfaces/face.py) into assets/icon.ico."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from app.interfaces.face import (
    ACCENT, ACCENT_BUSY, _EYE_HAPPY, _FACE_BODY_COLOR, _FACE_HIGHLIGHT_COLOR,
    _MOUTH_SMALL, _compose_face_frame,
)

COLORS = {"B": _FACE_BODY_COLOR, "H": _FACE_HIGHLIGHT_COLOR, "E": ACCENT, "M": ACCENT_BUSY, ".": None}


def render(scale: int) -> Image.Image:
    grid = _compose_face_frame(_EYE_HAPPY, _EYE_HAPPY, _MOUTH_SMALL)
    h, w = len(grid), len(grid[0])
    img = Image.new("RGBA", (w * scale, h * scale), (0, 0, 0, 0))
    px = img.load()
    for r, row in enumerate(grid):
        for c, code in enumerate(row):
            hexcolor = COLORS.get(code)
            if hexcolor is None:
                continue
            rgb = tuple(int(hexcolor[i:i + 2], 16) for i in (1, 3, 5))
            for dy in range(scale):
                for dx in range(scale):
                    px[c * scale + dx, r * scale + dy] = (*rgb, 255)
    return img


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "assets" / "icon.ico"
    out.parent.mkdir(exist_ok=True)
    face = render(32)  # 16x14 grid * 32 = 512x448
    side = max(face.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(face, ((side - face.width) // 2, (side - face.height) // 2), face)
    square.save(out.with_suffix(".png"))
    square.save(out, format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    print(f"Wrote {out} ({square.size})")
