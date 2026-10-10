"""Одноразовая генерация иконок приложения в packaging/icons/ (ассеты коммитятся,
CI их не рисует). Запуск на macOS: uv run python scripts/make_icons.py

  icon-1024.png — рисуется в Chromium из HTML (Playwright уже в зависимостях);
  AppIcon.icns  — sips + iconutil (есть только в macOS);
  app.ico       — ICO-контейнер с PNG внутри, собран struct'ом без Pillow
                  (Windows Vista+ понимает PNG в ICO).
"""

from __future__ import annotations

import struct
import subprocess
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent.parent / "packaging" / "icons"
ICO_SIZES = (16, 32, 48, 64, 128, 256)

HTML = """<html><body style="margin:0;background:transparent">
<div style="width:824px;height:824px;margin:100px;border-radius:185px;background:#f3ecd9;
 box-shadow:0 12px 30px rgba(0,0,0,.35);border:14px solid #1a1a1a;box-sizing:border-box;
 display:flex;flex-direction:column;align-items:center;justify-content:center;font-family:Georgia,'Times New Roman',serif;color:#1a1a1a">
 <div style="font-size:300px;font-weight:bold;line-height:1">TG</div>
 <div style="width:560px;height:16px;background:#1a1a1a;margin:24px 0 18px"></div>
 <div style="width:560px;height:6px;background:#1a1a1a;margin-bottom:16px"></div>
 <div style="font-size:92px;letter-spacing:6px;font-weight:bold">NEWSPAPER</div>
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

    # ICONDIR (6 байт) + по ICONDIRENTRY (16 байт) на размер + данные PNG.
    # Размер 256 в поле ширины/высоты кодируется нулём.
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
