"""Issue PDF built from finished PNG pages: either one A4 page per strip, or
merged A3 sheets (two landscape strips stacked directly one above the other).

The PDF is produced by the same Chromium as the layout (layout.py) —
the project doesn't pull in a separate PDF library."""

from __future__ import annotations

import html
import os
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

# Landscape A4; portrait A3 sheet — two such strips one above the other.
STRIP_W_MM = 297
STRIP_H_MM = 210


def build_pdf(pages: list[Path], out_path: Path, sheets: bool) -> None:
    """sheets=False — one strip per A4 page (297x210 mm); sheets=True —
    two strips per 297x420 mm page (a merged sheet, see
    layout.render_pages). The number of PNGs must be even for sheets."""
    if not pages:
        raise ValueError("нет полос для PDF")
    if sheets and len(pages) % 2:
        raise ValueError("для листов A3 нужно чётное число полос")

    page_h = STRIP_H_MM * 2 if sheets else STRIP_H_MM
    # Strips on the same sheet — no gaps or breaks within the sheet, otherwise
    # the merged sheet would stop looking like a single page.
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
    # We put the HTML in a temp directory rather than next to the PNGs; the
    # PDF is written to a temp file and renamed, so an interrupted generation
    # doesn't leave a broken cache under the real name.
    with tempfile.TemporaryDirectory(prefix="issue_pdf_") as tmp:
        html_path = Path(tmp) / "issue.html"
        html_path.write_text(doc, encoding="utf-8")
        tmp_pdf = Path(tmp) / "issue.pdf"
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                # "load" waits for file:// images to load — otherwise the PDF
                # could end up with blank spots.
                page.goto(html_path.as_uri(), wait_until="load")
                page.pdf(path=str(tmp_pdf), prefer_css_page_size=True, print_background=True)
            finally:
                browser.close()
        staged = out_path.with_name(out_path.name + ".tmp")
        staged.write_bytes(tmp_pdf.read_bytes())
        os.replace(staged, out_path)
