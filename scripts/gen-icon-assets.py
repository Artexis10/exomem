"""Generate the exomem brand mark — an `E` monogram in the Substrate visual
language (pure-black rounded square, white Helvetica-proportioned glyph).

Single source of truth: the geometry constants below drive BOTH outputs, so the
vector `icon.svg` (the MCP `initialize` icon + `/favicon.svg`) and the raster
`favicon.ico` (served at `/favicon.ico`, which is what the claude.ai connector
fetches from the domain) can never drift apart. Re-run after any tweak:

    <venv-with-Pillow>/python scripts/gen-icon-assets.py

Pillow is only needed for the .ico raster (it ships in the `media` extra); the
.svg is emitted with the stdlib alone.
"""

from __future__ import annotations

from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "src" / "exomem"

# --- Geometry (normalized fractions of the square side) ---------------------
# Tuned to Substrate's favicon: a moderately-rounded black square with a white
# glyph whose cap height fills ~56% of the square. The `E` strokes are uniform
# at ~15% of cap height (Helvetica-weight); its bounding box is centered.
SIZE = 512
CORNER = 0.14          # corner radius / side
CAP_H = 0.56           # glyph cap height / side
GLYPH_W = 0.35         # glyph width / side  (≈0.62 of cap height — Helvetica E)
STROKE = 0.15          # stroke thickness / cap height
MID_ARM = 0.90         # middle arm length / full arm length (E's shorter waist)
BG = "#000000"
FG = "#ffffff"


def _rects(size: int) -> list[tuple[float, float, float, float]]:
    """The four white rectangles of the `E` (spine + top/middle/bottom arms),
    each as (x0, y0, x1, y1) in a `size`×`size` space."""
    cap_h = CAP_H * size
    top = (size - cap_h) / 2
    bottom = top + cap_h
    glyph_w = GLYPH_W * size
    left = (size - glyph_w) / 2
    right = left + glyph_w
    t = STROKE * cap_h
    cy = size / 2
    mid_right = left + MID_ARM * glyph_w
    return [
        (left, top, left + t, bottom),          # spine
        (left, top, right, top + t),            # top arm
        (left, cy - t / 2, mid_right, cy + t / 2),  # middle arm (shorter)
        (left, bottom - t, right, bottom),      # bottom arm
    ]


def build_svg() -> str:
    r = round(CORNER * SIZE)
    rects = "".join(
        f'\n    <rect x="{x0:.1f}" y="{y0:.1f}" '
        f'width="{x1 - x0:.1f}" height="{y1 - y0:.1f}"/>'
        for x0, y0, x1, y1 in _rects(SIZE)
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}">\n'
        f'  <rect width="{SIZE}" height="{SIZE}" rx="{r}" fill="{BG}"/>\n'
        f'  <g fill="{FG}">{rects}\n  </g>\n</svg>\n'
    )


def build_ico(path: Path) -> None:
    from PIL import Image, ImageDraw

    ss = 4  # supersample for crisp downscaled edges
    n = SIZE * ss
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, n - 1, n - 1), radius=CORNER * n, fill=(0, 0, 0, 255))
    for x0, y0, x1, y1 in _rects(n):
        d.rectangle((x0, y0, x1, y1), fill=(255, 255, 255, 255))
    img = img.resize((SIZE, SIZE), Image.LANCZOS)
    img.save(path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def main() -> None:
    (OUT_DIR / "icon.svg").write_text(build_svg(), encoding="utf-8", newline="\n")
    print(f"wrote {OUT_DIR / 'icon.svg'}")
    build_ico(OUT_DIR / "favicon.ico")
    print(f"wrote {OUT_DIR / 'favicon.ico'}")


if __name__ == "__main__":
    main()
