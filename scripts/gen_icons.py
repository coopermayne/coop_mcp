"""
Generate the PWA / home-screen icons from the favicon's visual language: a black
square with three white "journal line" bars (see webapp/static/favicon.svg). Run
once and commit the PNGs; nothing at runtime depends on Pillow.

    python scripts/gen_icons.py

Writes into webapp/static/:
  icon-192.png, icon-512.png            — purpose "any" (rounded, transparent corners)
  icon-maskable-192.png, -512.png       — purpose "maskable" (full-bleed black square)
  apple-touch-icon.png (180)            — iOS home screen (full-bleed; iOS masks it)
"""

import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(os.path.dirname(HERE), "webapp", "static")

BLACK = (0, 0, 0, 255)
WHITE = (255, 255, 255, 255)

# Bar geometry as fractions of the canvas, taken from favicon.svg's 32px viewBox:
# bars at x=9, widths 14/14/9, y=8/14/20, height 2 → comfortably inside the central
# 80% "safe zone", so the same geometry doubles for maskable icons.
BARS = [(9 / 32, 8 / 32, 23 / 32, 10 / 32),
        (9 / 32, 14 / 32, 23 / 32, 16 / 32),
        (9 / 32, 20 / 32, 18 / 32, 22 / 32)]


def _render(size, *, rounded):
    """Render at 4× then downscale for clean anti-aliased edges."""
    ss = size * 4
    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if rounded:
        d.rounded_rectangle([0, 0, ss - 1, ss - 1], radius=int(ss * 6 / 32), fill=BLACK)
    else:
        d.rectangle([0, 0, ss, ss], fill=BLACK)
    for x0, y0, x1, y1 in BARS:
        d.rounded_rectangle(
            [x0 * ss, y0 * ss, x1 * ss, y1 * ss],
            radius=(y1 - y0) * ss / 2, fill=WHITE,
        )
    return img.resize((size, size), Image.LANCZOS)


def main():
    out = {
        "icon-192.png": _render(192, rounded=True),
        "icon-512.png": _render(512, rounded=True),
        "icon-maskable-192.png": _render(192, rounded=False),
        "icon-maskable-512.png": _render(512, rounded=False),
        "apple-touch-icon.png": _render(180, rounded=False),
    }
    for name, img in out.items():
        img.save(os.path.join(STATIC, name))
        print("wrote", os.path.join(STATIC, name))


if __name__ == "__main__":
    main()
