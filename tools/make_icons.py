"""Render the NewsNest logo into the PWA icon PNGs (dev tool; needs Pillow, not part of the app image).

    python3 tools/make_icons.py

The logo is two signal arcs and a lime dot, white on an ink tile. Writes newsnest/static/icons/:
icon-192.png, icon-512.png and favicon-32.png (rounded tile, transparent corners), icon-maskable-512.png and
apple-touch-icon.png (full-bleed ink so the OS can crop them).
"""

from pathlib import Path

from PIL import Image, ImageDraw

INK = (10, 10, 12, 255)
WHITE = (244, 244, 246, 255)
LIME = (198, 255, 61, 255)
OUT = Path(__file__).resolve().parent.parent / "newsnest" / "static" / "icons"
SS = 8  # supersampling for smooth edges

# The mark in its 32-unit box: arcs around the dot's centre, as in the logo_mark() macro
DOT = (8.8, 23.2, 4.2)
ARCS = (9.6, 16.8)
STROKE = 3.6


def logo(size: int, bleed: bool, scale: float) -> Image.Image:
    """The logo on a size x size canvas. bleed=True fills the square; otherwise a rounded tile."""
    big = size * SS
    img = Image.new("RGBA", (big, big), INK if bleed else (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if not bleed:
        draw.rounded_rectangle([0, 0, big - 1, big - 1], radius=big * 0.22, fill=INK)
    unit = big * scale / 32
    off = (big - 32 * unit) / 2

    def pt(x, y):
        return off + x * unit, off + y * unit

    cx, cy, dot_r = DOT
    px, py = pt(cx, cy)
    width = round(STROKE * unit)  # caps use the same rounded width as the band, so their edges line up
    for r in ARCS:
        outer = r * unit + width / 2  # PIL draws the arc width inward from the box edge
        draw.arc([px - outer, py - outer, px + outer, py + outer], start=270, end=360, fill=WHITE, width=width)
        for x, y in ((px, py - r * unit), (px + r * unit, py)):  # round caps
            draw.ellipse([x - width / 2, y - width / 2, x + width / 2, y + width / 2], fill=WHITE)
    draw.ellipse([*pt(cx - dot_r, cy - dot_r), *pt(cx + dot_r, cy + dot_r)], fill=LIME)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    logo(192, bleed=False, scale=0.72).save(OUT / "icon-192.png", optimize=True)
    logo(512, bleed=False, scale=0.72).save(OUT / "icon-512.png", optimize=True)
    # Maskable: the mark stays inside the central 80% "safe zone"
    logo(512, bleed=True, scale=0.62).save(OUT / "icon-maskable-512.png", optimize=True)
    logo(180, bleed=True, scale=0.7).convert("RGB").save(OUT / "apple-touch-icon.png", optimize=True)
    logo(32, bleed=False, scale=0.84).save(OUT / "favicon-32.png", optimize=True)
    for p in sorted(OUT.glob("*.png")):
        print(p.name, p.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
