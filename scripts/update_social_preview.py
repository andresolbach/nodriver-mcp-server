#!/usr/bin/env python
"""Stamp the current tool count into the social-preview card.

The card advertises "N tools"; that number went stale once already. This patches
just that pill in place, taking N from the server itself, so the card cannot
disagree with the code.

The original card was rendered with Segoe UI Regular at 27px — verified by
matching glyph ink boxes against the existing pixels — so the patch is
invisible. Windows-only for that reason; it is a maintainer script, not part of
the build.

    python scripts/update_social_preview.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nodriver_mcp.multiplexer import list_tools as client_tools  # noqa: E402

CARD = ROOT / "assets" / "social-preview.png"

PILL_BG = (30, 37, 50)
TEXT_RGB = (34, 211, 238)  # cyan-400

# The pill's interior, comfortably inside its rounded corners.
CLEAR_BOX = (378, 412, 482, 448)
# Where the old text's ink began, so the new text lands in exactly the same place.
INK_ORIGIN = (382, 420)

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\segoeui.ttf",
    "/Library/Fonts/Segoe UI.ttf",
    "/usr/share/fonts/truetype/segoeui.ttf",
]
FONT_SIZE = 27


def _font() -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, FONT_SIZE)
    raise SystemExit(
        "Segoe UI not found — the card was rendered with it, and any other face "
        "would be visible next to the untouched text. Run this on Windows."
    )


def _ink_box(mask: Image.Image):
    box = mask.getbbox()
    if box is None:
        raise SystemExit("rendered text produced no pixels")
    return box


def main() -> None:
    # The client-visible surface, not what server.py registers: browser_status
    # is hidden and the routing layer adds two of its own.
    count = len(asyncio.run(client_tools()))
    label = f"{count} tools"

    card = Image.open(CARD).convert("RGB")
    font = _font()

    # Render the label to a mask first, so it can be positioned by its ink rather
    # than by the font's own bearings.
    mask = Image.new("L", (300, 90), 0)
    ImageDraw.Draw(mask).text((20, 20), label, font=font, fill=255)
    ink = mask.crop(_ink_box(mask))

    ImageDraw.Draw(card).rectangle(CLEAR_BOX, fill=PILL_BG)
    card.paste(Image.new("RGB", ink.size, TEXT_RGB), INK_ORIGIN, ink)
    x1, y1 = ink.size

    card.save(CARD)
    print(f'{CARD.relative_to(ROOT)} now reads "{label}" ({x1}x{y1}px ink)')


if __name__ == "__main__":
    main()
