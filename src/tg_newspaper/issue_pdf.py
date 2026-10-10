"""PDF номера из готовых PNG-полос: либо по одной A4 на страницу, либо
склеенные листы A3 (две альбомные полосы вплотную одна над другой).

PDF делает тот же Chromium, что и вёрстка (layout.py), — отдельной
библиотеки для PDF проект не тянет."""

from __future__ import annotations

import html
import os
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

# Альбомная A4; лист A3 портрет — две такие полосы друг над другом.
STRIP_W_MM = 297
STRIP_H_MM = 210


def build_pdf(pages: list[Path], out_path: Path, sheets: bool) -> None:
    """sheets=False — по полосе на страницу A4 (297x210 мм); sheets=True —
    по две полосы на страницу 297x420 мм (склеенный лист, см.
    layout.render_pages). Число PNG должно быть чётным для листов."""
    if not pages:
        raise ValueError("нет полос для PDF")
    if sheets and len(pages) % 2:
        raise ValueError("для листов A3 нужно чётное число полос")

    page_h = STRIP_H_MM * 2 if sheets else STRIP_H_MM
    # Полосы одного листа — без зазоров и переносов внутри листа, иначе
    # склеенный лист перестанет выглядеть единой страницей.
    imgs = []
    step = 2 if sheets else 1
    for i in range(0, len(pages), step):
        group = "".join(
            f'<img src="{html.escape(p.resolve().as_uri())}">' for p in pages[i:i + step]
        )
        imgs.append(f'<div class="sheet">{group}</div>')
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><style>
@page {{ size: {STRIP_W_MM}mm {page_h}mm; margin: 0 }}
html, body {{ margin: 0; padding: 0 }}
.sheet {{ width: {STRIP_W_MM}mm; height: {page_h}mm; overflow: hidden;
          break-after: page; page-break-after: always }}
.sheet:last-child {{ break-after: auto; page-break-after: auto }}
img {{ display: block; width: {STRIP_W_MM}mm; height: {STRIP_H_MM}mm; margin: 0 }}
</style></head><body>{''.join(imgs)}</body></html>"""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # HTML кладём во временный каталог, а не рядом с PNG; PDF пишем во
    # временный файл и переименовываем, чтобы оборванная генерация не
    # оставила битый кеш под настоящим именем.
    with tempfile.TemporaryDirectory(prefix="issue_pdf_") as tmp:
        html_path = Path(tmp) / "issue.html"
        html_path.write_text(doc, encoding="utf-8")
        tmp_pdf = Path(tmp) / "issue.pdf"
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                # "load" ждёт загрузки file://-картинок — иначе PDF мог бы
                # уйти с пустыми местами.
                page.goto(html_path.as_uri(), wait_until="load")
                page.pdf(path=str(tmp_pdf), prefer_css_page_size=True, print_background=True)
            finally:
                browser.close()
        staged = out_path.with_name(out_path.name + ".tmp")
        staged.write_bytes(tmp_pdf.read_bytes())
        os.replace(staged, out_path)
