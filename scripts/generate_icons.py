#!/usr/bin/env python3
"""Regenerate web/icons/*.png (and nothing else) from the committed SVG
sources, for #731.

Rasterizes the deterministic vector marks in web/icons/icon.svg and
web/icons/icon-maskable.svg into the PNG sizes referenced by
web/manifest.webmanifest and the <link rel="apple-touch-icon"> in
web/index.html / web/crm.html / web/home.html. web/favicon.svg is served
directly as SVG (no raster needed).

Toolchain: GdkPixbuf's SVG loader (backed by librsvg), via PyGObject. This
repo's virtualenv doesn't carry PyGObject (it's a GObject-introspection
binding, not a pip package), so this script deliberately targets the
*system* Python, where Ubuntu/Omakub ship it already: `python3-gi` +
`gir1.2-gdkpixbuf-2.0` (pulled in by librsvg2-common) are standard desktop
packages, not something this script installs. If they're missing:

    sudo apt install python3-gi gir1.2-gdkpixbuf-2.0 librsvg2-common

Run with the system interpreter, not the lifeos venv:

    /usr/bin/python3 scripts/generate_icons.py

The apple-touch-icon additionally gets flattened onto its own opaque
background with Pillow (also present on system Python here) as a belt-
and-suspenders step: iOS composites this icon on an opaque rounded rect
with no transparency of its own, so any anti-aliased edge alpha from the
SVG rasterizer must be removed, not just be visually near-zero.
"""
import sys
from pathlib import Path

try:
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
except (ImportError, ValueError) as exc:
    sys.exit(
        "generate_icons.py requires PyGObject + GdkPixbuf's SVG loader "
        "(system packages, not a pip dependency of this repo). Install "
        "with: sudo apt install python3-gi gir1.2-gdkpixbuf-2.0 "
        "librsvg2-common -- and run with /usr/bin/python3, not the lifeos "
        f"venv.\nUnderlying error: {exc}"
    )

from PIL import Image

ICONS_DIR = Path(__file__).resolve().parent.parent / "web" / "icons"
ICON_SVG = ICONS_DIR / "icon.svg"
MASKABLE_SVG = ICONS_DIR / "icon-maskable.svg"

# LifeOS's navy background (must match the <rect> fill in icon.svg /
# icon-maskable.svg) — used only to flatten the apple-touch-icon's alpha.
BACKGROUND = (0x1A, 0x1A, 0x2E)


def render_svg(svg_path: Path, size: int) -> Image.Image:
    pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(svg_path), size, size, False)
    mode = "RGBA" if pixbuf.get_has_alpha() else "RGB"
    data = pixbuf.get_pixels()
    stride = pixbuf.get_rowstride()
    img = Image.frombuffer(mode, (pixbuf.get_width(), pixbuf.get_height()), data, "raw", mode, stride, 1)
    return img.copy()


def write_png(img: Image.Image, out_path: Path) -> None:
    img.save(out_path, "PNG")
    print(f"wrote {out_path.relative_to(ICONS_DIR.parent.parent)} ({img.width}x{img.height}, {img.mode})")


def main() -> None:
    # Standard "any"-purpose icons: any size cleanly upsamples/downsamples
    # from the same square vector source.
    for size in (192, 512):
        img = render_svg(ICON_SVG, size)
        write_png(img, ICONS_DIR / f"icon-{size}.png")

    # apple-touch-icon: 180x180, flattened onto an explicit opaque
    # background so no alpha channel (or near-invisible edge alpha) ever
    # reaches iOS's own compositing.
    touch = render_svg(ICON_SVG, 180).convert("RGBA")
    flattened = Image.new("RGB", touch.size, BACKGROUND)
    flattened.paste(touch, mask=touch.split()[3])
    write_png(flattened, ICONS_DIR / "apple-touch-icon.png")

    # Maskable variant for Android adaptive icons (padded source svg).
    maskable = render_svg(MASKABLE_SVG, 512)
    write_png(maskable, ICONS_DIR / "icon-maskable-512.png")


if __name__ == "__main__":
    main()
