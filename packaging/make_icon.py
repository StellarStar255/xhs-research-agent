"""Draw the app icon: a chat bubble with a magnifier and an AI sparkle on a coral→rose
red tile (no platform logo or wordmark), plus a monochrome menu-bar (tray) glyph.

Drawn at 4× and downsampled for smooth edges. Writes packaging/icon.png (1024px),
icon.ico (Windows), xhs_reader/static/icon.png (runtime Dock icon / favicon),
xhs_reader/static/tray.png (macOS menu-bar template image) and, on macOS, icon.icns.
"""
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

HERE = Path(__file__).parent
K = 4                 # supersampling factor
S = 1024 * K


def lerp(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def diagonal_gradient(size, c1, c2):
    """Top-left c1 → bottom-right c2."""
    small = Image.new("RGB", (256, 256))
    px = small.load()
    for y in range(256):
        for x in range(256):
            px[x, y] = lerp(c1, c2, (x + y) / 510)
    return small.resize((size, size), Image.BICUBIC)


def rounded_mask(size, box, radius):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle(box, radius=radius, fill=255)
    return m


def shadow(mask, offset, blur, opacity):
    sh = Image.new("L", mask.size, 0)
    sh.paste(mask, offset)
    sh = sh.filter(ImageFilter.GaussianBlur(blur))
    return sh.point(lambda v: round(v * opacity))


def draw():
    u = K  # 1 design px (on the 1024 grid) = K real px
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # --- tile: macOS grid (824px square, ~185px corner), soft drop shadow
    tile_box = [100 * u, 92 * u, 924 * u, 916 * u]
    tile = rounded_mask(S, tile_box, 186 * u)
    img.paste((60, 10, 20, 255), (0, 0), shadow(tile, (0, 12 * u), 22 * u, 0.35))
    grad = diagonal_gradient(S, (255, 112, 96), (214, 30, 84)).convert("RGBA")
    img.paste(grad, (0, 0), tile)
    # glossy highlight on the upper half
    gloss = Image.new("L", (S, S), 0)
    ImageDraw.Draw(gloss).ellipse([-260 * u, -620 * u, 1284 * u, 520 * u], fill=34)
    gloss = ImageChops.multiply(gloss.filter(ImageFilter.GaussianBlur(110 * u)), tile)
    img.paste((255, 255, 255, 255), (0, 0), gloss)

    # --- chat bubble with tail, soft shadow
    bubble = Image.new("L", (S, S), 0)
    bd = ImageDraw.Draw(bubble)
    bd.rounded_rectangle([222 * u, 250 * u, 802 * u, 682 * u], radius=150 * u, fill=255)
    bd.polygon([(318 * u, 640 * u), (282 * u, 792 * u), (452 * u, 672 * u)], fill=255)
    img.paste((110, 10, 40, 255), (0, 0), shadow(bubble, (0, 18 * u), 26 * u, 0.28))
    img.paste((255, 255, 255, 255), (0, 0), bubble)

    # --- magnifier: gradient ring + glassy lens + handle
    cx, cy, r, w = 492 * u, 450 * u, 118 * u, 40 * u
    ring = Image.new("L", (S, S), 0)
    rd = ImageDraw.Draw(ring)
    rd.ellipse([cx - r, cy - r, cx + r, cy + r], outline=255, width=w)
    hx0, hy0 = cx + int(r * 0.72), cy + int(r * 0.72)
    hx1, hy1 = cx + 176 * u, cy + 176 * u
    rd.line([(hx0, hy0), (hx1, hy1)], fill=255, width=54 * u)
    rd.ellipse([hx1 - 27 * u, hy1 - 27 * u, hx1 + 27 * u, hy1 + 27 * u], fill=255)
    ink = diagonal_gradient(S, (250, 96, 88), (206, 28, 82)).convert("RGBA")
    img.paste(ink, (0, 0), ring)
    lens = Image.new("L", (S, S), 0)
    ImageDraw.Draw(lens).ellipse([cx - r + w, cy - r + w, cx + r - w, cy + r - w], fill=40)
    img.paste((240, 80, 110, 255), (0, 0), lens)
    glint = Image.new("L", (S, S), 0)
    ImageDraw.Draw(glint).arc([cx - r + w + 14 * u, cy - r + w + 14 * u, cx + r - w - 14 * u, cy + r - w - 14 * u],
                              start=200, end=265, fill=235, width=14 * u)
    img.paste((255, 255, 255, 255), (0, 0), glint)

    # --- AI sparkle, top-right of the bubble
    def sparkle(x, y, R, color):
        m = Image.new("L", (S, S), 0)
        d = ImageDraw.Draw(m)
        k = R * 0.22
        d.polygon([(x, y - R), (x + k, y - k), (x + R, y), (x + k, y + k),
                   (x, y + R), (x - k, y + k), (x - R, y), (x - k, y - k)], fill=255)
        img.paste(color, (0, 0), m)
    sparkle(706 * u, 336 * u, 62 * u, (255, 196, 64, 255))
    sparkle(640 * u, 262 * u, 26 * u, (255, 214, 120, 255))

    return img.resize((1024, 1024), Image.LANCZOS)


def draw_tray(px=44):
    """Menu-bar glyph (bubble outline + magnifier), black on transparent; macOS tints it."""
    T = px * 8
    m = Image.new("L", (T, T), 0)
    d = ImageDraw.Draw(m)
    lw = T // 11
    d.rounded_rectangle([lw, T * 0.12, T - lw, T * 0.74], radius=T * 0.2, outline=255, width=lw)
    d.polygon([(T * 0.2, T * 0.70), (T * 0.14, T * 0.95), (T * 0.42, T * 0.72)], fill=255)
    cx, cy, r = T * 0.45, T * 0.42, T * 0.15
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=255, width=int(lw * 0.85))
    d.line([(cx + r * 0.7, cy + r * 0.7), (cx + r * 1.9, cy + r * 1.9)], fill=255, width=int(lw * 0.95))
    glyph = Image.new("RGBA", (T, T), (0, 0, 0, 0))
    glyph.putalpha(m)
    return glyph.resize((px, px), Image.LANCZOS)


def main():
    draw_tray().save(HERE.parent / "xhs_reader" / "static" / "tray.png")
    img = draw()
    img.save(HERE / "icon.png")
    img.resize((256, 256), Image.LANCZOS).save(HERE.parent / "xhs_reader" / "static" / "icon.png")
    img.save(HERE / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    if sys.platform == "darwin" and shutil.which("iconutil"):
        iconset = HERE / "icon.iconset"
        iconset.mkdir(exist_ok=True)
        for size in (16, 32, 128, 256, 512):
            img.resize((size, size), Image.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
            img.resize((size * 2, size * 2), Image.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(HERE / "icon.icns")], check=True)
        shutil.rmtree(iconset)


if __name__ == "__main__":
    main()
