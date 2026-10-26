"""One-off generation of the app icons into packaging/icons/ (the assets are
committed, CI doesn't draw them). Run on macOS: uv run python scripts/make_icons.py

  icon-1024.png — drawn in Chromium from HTML (Playwright is already a dependency);
  AppIcon.icns  — sips + iconutil (available only on macOS);
  app.ico       — an ICO container with a PNG inside, assembled with struct
                  without Pillow (Windows Vista+ understands PNG inside ICO).
"""

from __future__ import annotations

import struct
import subprocess
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent.parent / "packaging" / "icons"
ICO_SIZES = (16, 32, 48, 64, 128, 256)

# The console's paste-up-desk look: a board with a non-photo-blue grid, a
# printable sheet with crop marks, the masthead plate with the "T" monogram
# the paper prints on every tile, and one line under a yellow marker.
HTML = """<html><body style="margin:0;background:transparent">
<div style="position:relative;width:824px;height:824px;margin:100px;border-radius:185px;overflow:hidden;
 box-shadow:0 12px 30px rgba(0,0,0,.35);background-color:#F2F7F9;
 background-image:linear-gradient(#CFE6EE 4px,transparent 4px),linear-gradient(90deg,#CFE6EE 4px,transparent 4px);
 background-size:82px 82px;background-position:-2px -2px">
 <div style="position:absolute;left:182px;top:142px;width:460px;height:540px">
  <div style="position:absolute;inset:0;background:#fff;border:4px solid #8FCDE3;box-sizing:border-box"></div>
  <i style="position:absolute;left:-70px;top:-2px;width:50px;height:5px;background:#231F20"></i>
  <i style="position:absolute;left:-2px;top:-70px;width:5px;height:50px;background:#231F20"></i>
  <i style="position:absolute;right:-70px;top:-2px;width:50px;height:5px;background:#231F20"></i>
  <i style="position:absolute;right:-2px;top:-70px;width:5px;height:50px;background:#231F20"></i>
  <i style="position:absolute;left:-70px;bottom:-2px;width:50px;height:5px;background:#231F20"></i>
  <i style="position:absolute;left:-2px;bottom:-70px;width:5px;height:50px;background:#231F20"></i>
  <i style="position:absolute;right:-70px;bottom:-2px;width:50px;height:5px;background:#231F20"></i>
  <i style="position:absolute;right:-2px;bottom:-70px;width:5px;height:50px;background:#231F20"></i>
  <div style="position:absolute;left:40px;right:40px;top:40px;height:200px;background:#231F20;color:#fff;
   display:flex;align-items:center;justify-content:center;font:900 190px/1 Superclarendon,'PT Serif',Georgia,serif">T</div>
  <div style="position:absolute;left:40px;right:40px;top:282px;height:26px;background:#231F20"></div>
  <div style="position:absolute;left:28px;right:96px;top:338px;height:50px;background:#FFE94D"></div>
  <div style="position:absolute;left:40px;right:108px;top:350px;height:26px;background:#231F20"></div>
  <div style="position:absolute;left:40px;right:40px;top:418px;height:26px;background:#231F20"></div>
 </div>
</div></body></html>"""


def sips_resize(src: Path, size: int, dest: Path) -> None:
    subprocess.run(["sips", "-z", str(size), str(size), str(src), "--out", str(dest)],
                   check=True, capture_output=True)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    png = OUT / "icon-1024.png"
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1024, "height": 1024})
        page.set_content(HTML)
        page.screenshot(path=str(png), omit_background=True)
        b.close()

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        iconset = work / "icon.iconset"
        iconset.mkdir()
        for s in (16, 32, 128, 256, 512):
            sips_resize(png, s, iconset / f"icon_{s}x{s}.png")
            sips_resize(png, s * 2, iconset / f"icon_{s}x{s}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(OUT / "AppIcon.icns")],
                       check=True)

        images = []
        for s in ICO_SIZES:
            f = work / f"ico_{s}.png"
            sips_resize(png, s, f)
            images.append((s, f.read_bytes()))

    # ICONDIR (6 bytes) + one ICONDIRENTRY (16 bytes) per size + PNG data.
    # Size 256 in the width/height field is encoded as zero.
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for s, data in images:
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    (OUT / "app.ico").write_bytes(header + entries + blobs)
    print("Готово:", OUT)


if __name__ == "__main__":
    main()
