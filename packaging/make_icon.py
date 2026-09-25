"""Draw the app icon (neutral: teal→indigo, chat bubble + magnifier; no platform branding).

Writes packaging/icon.png (1024px), icon.ico (Windows) and, on macOS, icon.icns.
"""
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent
S = 1024


def draw():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # Vertical teal→indigo gradient, masked to a rounded square with macOS-style margins.
    grad = Image.new("RGBA", (S, S))
    top, bottom = (20, 184, 166), (79, 70, 229)
    for y in range(S):
        t = y / (S - 1)
        ImageDraw.Draw(grad).line([(0, y), (S, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([100, 100, 924, 924], radius=190, fill=255)
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)
    white = (255, 255, 255, 255)
    # Chat bubble with a tail.
    d.rounded_rectangle([230, 250, 794, 690], radius=120, fill=white)
    d.polygon([(330, 660), (300, 800), (450, 680)], fill=white)
    # Magnifier inside the bubble, in the gradient's mid colour.
    ink = (45, 120, 200, 255)
    cx, cy, r = 470, 430, 115
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=ink, width=44)
    d.line([(cx + 82, cy + 82), (cx + 175, cy + 175)], fill=ink, width=58)
    d.ellipse([cx + 146, cy + 146, cx + 204, cy + 204], fill=ink)  # round the handle end
    return img


def main():
    img = draw()
    img.save(HERE / "icon.png")
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
