"""Rebuild the session-close image fixtures: `label.png` and `card.png`.

Standard library only (zlib and struct). The text is drawn with a 5x7 block font at a large scale, so a vision
model reads it plainly. Every name, product and number is invented; the card number is the public Visa test number.

    python render_fixtures.py

writes both files next to this script. Run it again after a change here and commit the PNGs it writes.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

# One glyph is seven rows of five columns; "#" is ink. The set covers the characters the two fixtures print.
_GLYPH_ROWS = {
    "0": ".###. #...# #..## #.#.# ##..# #...# .###.",
    "1": "..#.. .##.. ..#.. ..#.. ..#.. ..#.. .###.",
    "2": ".###. #...# ....# ...#. ..#.. .#... #####",
    "3": "##### ...#. ..#.. ...#. ....# #...# .###.",
    "4": "...#. ..##. .#.#. #..#. ##### ...#. ...#.",
    "5": "##### #.... ####. ....# ....# #...# .###.",
    "6": "..##. .#... #.... ####. #...# #...# .###.",
    "7": "##### ....# ...#. ..#.. .#... .#... .#...",
    "8": ".###. #...# #...# .###. #...# #...# .###.",
    "9": ".###. #...# #...# .#### ....# ...#. .##..",
    "A": ".###. #...# #...# ##### #...# #...# #...#",
    "B": "####. #...# #...# ####. #...# #...# ####.",
    "C": ".###. #...# #.... #.... #.... #...# .###.",
    "D": "####. #...# #...# #...# #...# #...# ####.",
    "E": "##### #.... #.... ####. #.... #.... #####",
    "F": "##### #.... #.... ####. #.... #.... #....",
    "G": ".###. #...# #.... #.### #...# #...# .####",
    "H": "#...# #...# #...# ##### #...# #...# #...#",
    "I": ".###. ..#.. ..#.. ..#.. ..#.. ..#.. .###.",
    "J": "..### ...#. ...#. ...#. ...#. #..#. .##..",
    "K": "#...# #..#. #.#.. ##... #.#.. #..#. #...#",
    "L": "#.... #.... #.... #.... #.... #.... #####",
    "M": "#...# ##.## #.#.# #.#.# #...# #...# #...#",
    "N": "#...# #...# ##..# #.#.# #..## #...# #...#",
    "O": ".###. #...# #...# #...# #...# #...# .###.",
    "P": "####. #...# #...# ####. #.... #.... #....",
    "Q": ".###. #...# #...# #...# #.#.# #..#. .##.#",
    "R": "####. #...# #...# ####. #.#.. #..#. #...#",
    "S": ".#### #.... #.... .###. ....# ....# ####.",
    "T": "##### ..#.. ..#.. ..#.. ..#.. ..#.. ..#..",
    "U": "#...# #...# #...# #...# #...# #...# .###.",
    "V": "#...# #...# #...# #...# #...# .#.#. ..#..",
    "W": "#...# #...# #...# #.#.# #.#.# ##.## #...#",
    "X": "#...# #...# .#.#. ..#.. .#.#. #...# #...#",
    "Y": "#...# #...# .#.#. ..#.. ..#.. ..#.. ..#..",
    "Z": "##### ....# ...#. ..#.. .#... #.... #####",
    "-": "..... ..... ..... ##### ..... ..... .....",
    "/": "....# ....# ...#. ..#.. .#... #.... #....",
    ".": "..... ..... ..... ..... ..... .##.. .##..",
    " ": "..... ..... ..... ..... ..... ..... .....",
}
GLYPHS = {char: rows.split() for char, rows in _GLYPH_ROWS.items()}
GLYPH_WIDTH, GLYPH_HEIGHT, GLYPH_GAP = 5, 7, 1

Color = tuple[int, int, int]
WHITE: Color = (255, 255, 255)
BLACK: Color = (18, 18, 18)


class Canvas:
    """A flat RGB raster with the three drawing moves the fixtures need."""

    def __init__(self, width: int, height: int, background: Color) -> None:
        self.width, self.height = width, height
        self.rows = [[background] * width for _ in range(height)]

    def rect(self, x: int, y: int, width: int, height: int, color: Color) -> None:
        for row in self.rows[max(y, 0) : max(y + height, 0)]:
            row[max(x, 0) : max(x + width, 0)] = [color] * (min(x + width, self.width) - max(x, 0))

    def frame(self, x: int, y: int, width: int, height: int, thickness: int, color: Color) -> None:
        self.rect(x, y, width, thickness, color)
        self.rect(x, y + height - thickness, width, thickness, color)
        self.rect(x, y, thickness, height, color)
        self.rect(x + width - thickness, y, thickness, height, color)

    def text(self, x: int, y: int, text: str, scale: int, color: Color) -> int:
        """Draw `text` with its top-left corner at (x, y); return the x just past it."""
        for char in text:
            for row, bits in enumerate(GLYPHS[char]):
                for column, bit in enumerate(bits):
                    if bit == "#":
                        self.rect(x + column * scale, y + row * scale, scale, scale, color)
            x += (GLYPH_WIDTH + GLYPH_GAP) * scale
        return x

    def png(self) -> bytes:
        raw = b"".join(b"\x00" + bytes(channel for pixel in row for channel in pixel) for row in self.rows)

        def chunk(kind: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

        header = struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def label() -> Canvas:
    """The box label of an invented product: brand, model, serial number and a decorative barcode."""
    canvas = Canvas(840, 520, WHITE)
    canvas.frame(10, 10, 820, 500, 8, BLACK)
    canvas.rect(18, 18, 804, 96, BLACK)
    canvas.text(60, 34, "NORTHWIND", 8, WHITE)
    canvas.text(60, 140, "HP-7 WIRELESS HEADPHONES", 5, BLACK)
    canvas.text(60, 200, "MODEL HP-7", 5, BLACK)
    canvas.frame(40, 258, 760, 78, 5, BLACK)
    canvas.text(60, 272, "S/N NW7-2611-0042", 7, BLACK)
    # Alternating bar widths, fixed so a rebuild draws the same barcode. Not a valid EAN symbol.
    x = 60
    for index in range(46):
        width = 2 + (index * 7 % 5)
        if index % 2 == 0:
            canvas.rect(x, 356, width, 84, BLACK)
        x += width + 2
    canvas.text(60, 456, "EAN 2611004200006", 5, BLACK)
    return canvas


def card() -> Canvas:
    """A payment card front with the public test number, an invented bank and an invented holder."""
    canvas = Canvas(840, 530, (20, 40, 92))
    canvas.frame(10, 10, 820, 510, 6, (120, 150, 215))
    canvas.text(60, 50, "EXAMPLE BANK", 6, WHITE)
    canvas.rect(60, 150, 110, 84, (226, 184, 70))
    canvas.frame(60, 150, 110, 84, 4, (150, 118, 30))
    canvas.text(60, 290, "4111 1111 1111 1111", 6, WHITE)
    canvas.text(60, 380, "VALID THRU 09/29", 4, WHITE)
    canvas.text(60, 450, "SAMPLE CARDHOLDER", 4, WHITE)
    return canvas


def main() -> None:
    here = Path(__file__).resolve().parent
    for name, build in (("label.png", label), ("card.png", card)):
        data = build().png()
        (here / name).write_bytes(data)
        print(f"{name}: {len(data)} bytes")


if __name__ == "__main__":
    main()
