#!/usr/bin/env python3
"""Generate the HACS brand assets from the card's camera glyph.

Draws the same rounded-body + lens mark the card uses in its header, in the
Earthy Dark palette, and writes it as a 256x256 PNG. Pure standard library, so
it can be re-run anywhere:

    python scripts/make_brand_assets.py
"""

from __future__ import annotations

import pathlib
import struct
import zlib

SIZE = 256
SAMPLES = 4  # supersampling factor per axis

BACKGROUND = (0x9A, 0x88, 0x73)  # --taupe
GLYPH = (0x22, 0x1B, 0x14)  # near --onyx

OUT_DIR = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "reolink_clip_cache" / "brand"

# The card's glyph is drawn on a 24-unit grid spanning x 2..22, y 6..18.
# Scale by 8 and centre it inside the 256px square.
SCALE, OFFSET_X, OFFSET_Y = 8, 32, 32


def _u(value: float) -> float:
    """Map a grid unit on the x axis to a pixel."""
    return OFFSET_X + value * SCALE


def _v(value: float) -> float:
    """Map a grid unit on the y axis to a pixel."""
    return OFFSET_Y + value * SCALE


BODY = (_u(2), _v(6), _u(15), _v(18), 2 * SCALE)  # x0, y0, x1, y1, radius
LENS = ((_u(22), _v(8)), (_u(17), _v(12)), (_u(22), _v(16)))
CARD_RADIUS = 58


def in_rounded_rect(x: float, y: float, x0: float, y0: float, x1: float, y1: float, r: float) -> bool:
    """Return True if the point lies inside a rounded rectangle."""
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def in_triangle(x: float, y: float, tri: tuple) -> bool:
    """Return True if the point lies inside a triangle."""
    (ax, ay), (bx, by), (cx, cy) = tri
    d1 = (x - bx) * (ay - by) - (ax - bx) * (y - by)
    d2 = (x - cx) * (by - cy) - (bx - cx) * (y - cy)
    d3 = (x - ax) * (cy - ay) - (cx - ax) * (y - ay)
    return not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0))


def coverage(px: int, py: int, test) -> float:
    """Supersample one pixel against a shape test, returning 0.0-1.0."""
    hits = 0
    for sy in range(SAMPLES):
        for sx in range(SAMPLES):
            x = px + (sx + 0.5) / SAMPLES
            y = py + (sy + 0.5) / SAMPLES
            if test(x, y):
                hits += 1
    return hits / (SAMPLES * SAMPLES)


def blend(under: tuple, over: tuple, alpha: float) -> tuple:
    """Composite over onto under."""
    return tuple(round(u + (o - u) * alpha) for u, o in zip(under, over))


def render() -> bytes:
    """Render the icon as raw RGBA scanlines."""
    rows = bytearray()
    for py in range(SIZE):
        rows.append(0)  # PNG filter type: none
        for px in range(SIZE):
            card = coverage(px, py, lambda x, y: in_rounded_rect(
                x, y, 0, 0, SIZE, SIZE, CARD_RADIUS))
            if card == 0.0:
                rows.extend((0, 0, 0, 0))
                continue

            glyph = max(
                coverage(px, py, lambda x, y: in_rounded_rect(x, y, *BODY)),
                coverage(px, py, lambda x, y: in_triangle(x, y, LENS)),
            )
            rows.extend((*blend(BACKGROUND, GLYPH, glyph), round(card * 255)))
    return bytes(rows)


def write_png(path: pathlib.Path, raw: bytes) -> None:
    """Write RGBA scanlines out as a PNG."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)  # 8-bit RGBA
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main() -> None:
    """Write icon.png and logo.png."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    raw = render()
    for name in ("icon.png", "logo.png"):
        write_png(OUT_DIR / name, raw)
        print(f"wrote {OUT_DIR / name}")


if __name__ == "__main__":
    main()
