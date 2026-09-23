"""Generate the tool icons.

Icons are produced from code, never checked in as binaries, so they can be
reviewed, restyled and regenerated at any size without a design tool. Run::

    python tools/make_icons.py

and the three PNGs under ``configuration/`` are rewritten.

On the artwork: a plain rounded speech bubble with a direction arrow. It is
emphatically *not* the WhatsApp logo. That mark belongs to Meta, and shipping a
lookalike would be trademark infringement as well as
implying an endorsement that does not exist. A generic chat bubble says
"messaging" perfectly well.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover
    sys.exit(
        "Pillow is required to regenerate the icons.\n"
        "  python -m pip install pillow"
    )

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "configuration"

#: Rendered at 4x and downsampled, which is cheaper than antialiasing by hand
#: and gives clean curves at Designer's 64px tool size.
SCALE = 4
SIZE = 64

GREEN_DARK = (18, 140, 90, 255)
GREEN_LIGHT = (37, 190, 120, 255)
WHITE = (255, 255, 255, 255)


def _bubble(draw: "ImageDraw.ImageDraw", size: int, colour: tuple[int, int, int, int]) -> None:
    """A rounded rectangle with a tail in the bottom-left corner."""
    margin = size * 0.10
    body = (margin, margin, size - margin, size - margin * 2.2)
    draw.rounded_rectangle(body, radius=size * 0.22, fill=colour)
    # The tail: a triangle tucked under the body's bottom-left corner.
    tail = [
        (margin + size * 0.14, size - margin * 2.4),
        (margin + size * 0.14, size - margin * 0.7),
        (margin + size * 0.40, size - margin * 2.4),
    ]
    draw.polygon(tail, fill=colour)


def _arrow_down(draw: "ImageDraw.ImageDraw", size: int) -> None:
    """Downward arrow: data coming *in* to the workflow."""
    cx, cy = size * 0.5, size * 0.40
    shaft = size * 0.055
    draw.rectangle(
        (cx - shaft, cy - size * 0.15, cx + shaft, cy + size * 0.05), fill=WHITE
    )
    draw.polygon(
        [
            (cx - size * 0.145, cy + size * 0.02),
            (cx + size * 0.145, cy + size * 0.02),
            (cx, cy + size * 0.19),
        ],
        fill=WHITE,
    )


def _arrow_up(draw: "ImageDraw.ImageDraw", size: int) -> None:
    """Upward arrow: data going *out* to WhatsApp."""
    cx, cy = size * 0.5, size * 0.40
    shaft = size * 0.055
    draw.rectangle(
        (cx - shaft, cy - size * 0.05, cx + shaft, cy + size * 0.16), fill=WHITE
    )
    draw.polygon(
        [
            (cx - size * 0.145, cy - size * 0.02),
            (cx + size * 0.145, cy - size * 0.02),
            (cx, cy - size * 0.19),
        ],
        fill=WHITE,
    )


def _lines(draw: "ImageDraw.ImageDraw", size: int) -> None:
    """Three text lines, for the package icon."""
    for index, width in enumerate((0.44, 0.52, 0.34)):
        top = size * (0.30 + index * 0.125)
        draw.rounded_rectangle(
            (size * 0.26, top, size * (0.26 + width), top + size * 0.072),
            radius=size * 0.036,
            fill=WHITE,
        )


def render(name: str, colour, glyph) -> Path:
    canvas = SIZE * SCALE
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    _bubble(draw, canvas, colour)
    glyph(draw, canvas)
    image = image.resize((SIZE, SIZE), Image.LANCZOS)

    target = CONFIG / name
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, "PNG")
    return target


def main() -> int:
    written = [
        render("WhatsAppInput/icon.png", GREEN_DARK, _arrow_down),
        render("WhatsAppOutput/icon.png", GREEN_LIGHT, _arrow_up),
        render("package/icon.png", GREEN_DARK, _lines),
    ]
    for path in written:
        print(f"wrote {path.relative_to(ROOT)}  ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
