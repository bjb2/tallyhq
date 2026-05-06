"""Generate logo.png + apple-touch-icon.png for SEO knowledge-panel use.

Matches the inline-SVG favicon (3x3 tile heatmap) but at sizes Google and
Bing want for the SERP knowledge panel:
  logo.png            — 600x600 square, used by schema.org Organization.logo
  apple-touch-icon.png — 180x180 (iOS pinning + better SERP/share favicon)
  favicon-512.png     — 512x512 PNG fallback for clients that prefer raster

Run once; commit outputs.
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "src" / "conductor" / "politics" / "web" / "static"

# Same palette as the inline SVG favicon
COLORS = [
    ["#1a7f37", "#116329", "#1a7f37"],
    ["#4ac26b", "#1a7f37", "#116329"],
    ["#116329", "#4ac26b", "#1a7f37"],
]

BG = "#fbfaf7"  # warm paper, matches landing background


def render(size: int, *, padding_ratio: float = 0.08, gap_ratio: float = 0.06,
           radius_ratio: float = 0.16) -> Image.Image:
    img = Image.new("RGBA", (size, size), BG)
    d = ImageDraw.Draw(img)
    pad = int(size * padding_ratio)
    inner = size - 2 * pad
    gap = int(inner * gap_ratio)
    cell = (inner - 2 * gap) // 3
    radius = max(2, int(cell * radius_ratio))
    for r in range(3):
        for c in range(3):
            x0 = pad + c * (cell + gap)
            y0 = pad + r * (cell + gap)
            d.rounded_rectangle(
                [x0, y0, x0 + cell, y0 + cell],
                radius=radius,
                fill=COLORS[r][c],
            )
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    render(600).save(OUT / "logo.png", "PNG", optimize=True)
    render(512).save(OUT / "favicon-512.png", "PNG", optimize=True)
    render(180).save(OUT / "apple-touch-icon.png", "PNG", optimize=True)
    print(f"wrote: {OUT / 'logo.png'}")
    print(f"wrote: {OUT / 'favicon-512.png'}")
    print(f"wrote: {OUT / 'apple-touch-icon.png'}")


if __name__ == "__main__":
    main()
