"""Newspaper page layout (Stage 3): HTML/CSS template + render to PNG via
headless Chromium (Playwright). Input — an already filtered and
deduplicated list of posts (the result of Stages 1-2); output — one
or several PNGs (one per printed page) at exactly the size that will
go to print at Stage 4, with no DPI recalculation on that side.

The layout uses physical CSS units (mm) for the sheet size, margins and
fonts — they don't depend on the DPI the screenshot was taken at: 1mm in
the template is always 1mm on paper. DPI only affects the pixel density
of the resulting PNG (see render_pages).

The mosaic uses a CSS grid with two standard tile widths —
feature/brief, by the LLM importance score (see _build_bands; the most
significant story of the issue, the lead, isn't part of the mosaic at
all — see below), stacked into rows (_Band) that always sum to exactly
the page width (columns): pairs of feature tiles at half the page width,
triples of brief tiles at a third of the page width. These are not
flowing column-count columns, nor
free "however it fits" packing (the first version of Stage 3 used
grid-auto-flow:dense with each tile's height computed independently —
because text length varied, neighboring columns ended up different
heights, and the grid got messy gray gaps in the middle of the page, not
just at the bottom). A row is the atomic unit of page splitting: all
tiles in one row get the SAME height (sized to the longest article in
the row), so neighboring tiles' bottoms always line up, and shorter
articles simply get a bit of empty space inside their OWN white tile,
instead of a shared gray gap showing through the .mosaic background. An
incomplete row at the end of the queue (not enough articles left to fill
a whole row) doesn't leave a hole — it stretches the remaining tiles to
the full page width (see _group_into_bands) — so empty space can only
ever appear at the very bottom of the last page of the issue, if there
weren't enough articles to fill it completely. Tile width determines
only typography — height is always computed from the actual text volume
of the longest article in the row (_estimate_row_span); the full text is
always printed, nothing gets truncated with an ellipsis in place.
See _build_bands for the proportions and the tier split.

The first merged sheet of the issue (SHEET_UNITS pages) is the
cover/POSTER page (_cover_page_html), styled after the Daily Bugle cover:
the title in white on a black plate with a bugle medallion, teaser boxes
above it, a giant screamer headline, a photo with a diagonal ribbon, and
an inverse strip at the bottom. The cover does NOT print a single
article — it only entices the reader into the issue with loud teasers
(generated for the 4 main stories by the LLM, see classifier.teasers),
while all the stories in full, including the most important one, are
printed in the mosaic on the following pages. So the cover doesn't take
anything away from the issue and comes ON TOP OF the max_pages budget
allotted to the mosaic (user decision from 2026-09-27: "let's add two
[pages] that form one big one"). The large masthead is exclusive to the
cover; every mosaic page uses the same lightweight "inner" header
(running-header), even the very first one.

If a day's news adds up to more than fits on one mosaic page, the
newspaper gets a second, third, and so on page — all with the same
running-header, like the inner pages of a real multi-page newspaper.
Page splitting
(render_pages, _fit_bands) is not just an estimate based on text volume:
each candidate page is actually rendered in headless Chromium (with the
same device_scale_factor as the final screenshot — otherwise different
rounding of character widths to physical pixels could wrap lines
differently and diverge from what was measured), and whatever physically
doesn't fit is genuinely moved to the next page, or, if an article
doesn't fit even alone on a whole page, is first expanded to the full
grid width (fewer lines needed with a wider column) — text truncation is
ruled out everywhere except the theoretical limit of "doesn't fit even
alone on a whole page". The character-count estimate (_estimate_row_span)
is only a starting approximation, so the fitting loop doesn't have to
start with one story at a time; correctness is the responsibility of the
browser measurement, not this estimate.

Stage 7. Rubrics — mosaic sections: each one is preceded by a full-width
header row (_header_band); section rows always run consecutively
(_fill_sheet won't let a row from the next section land on a page before
a deferred row of the previous one, the header never ends up orphaned,
and on a new page the section resumes with "(continued)"). The "New
videos" block (_video_bands) is the last section in the queue; on the
last page allowed by max_pages, space for it is reserved in advance.
"""

from __future__ import annotations

import base64
import html
import logging
import math
import mimetypes
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from .storage import Post
from .youtube import YoutubeVideo

logger = logging.getLogger(__name__)

# Physical sheet dimensions in mm.
PAGE_SIZES_MM: dict[str, tuple[float, float]] = {
    "A4": (210.0, 297.0),
    "Letter": (215.9, 279.4),
}

# Reference ratio for CSS units: the CSS spec fixes 1in = 96px
# regardless of the device's actual DPI — used only to get an integer
# viewport size in px for the physical size we need.
CSS_PX_PER_MM = 96 / 25.4

DEFAULT_PAGE_SIZE = "A4"
DEFAULT_DPI = 300
DEFAULT_COLUMNS = 6  # number of base mosaic grid columns, not "text columns"

# The title is in English and deliberately two words (user decision from
# 2026-10-06): on the masthead plate they sit at opposite edges, with a
# bugle medallion between them — exactly the composition of the reference
# (DAILY [bugle] BUGLE).
MASTHEAD_LEFT = "DAILY"
MASTHEAD_RIGHT = "TIMOHA"
MASTHEAD_TITLE = f"{MASTHEAD_LEFT} {MASTHEAD_RIGHT}"  # on one line — for the mosaic pages' running-header
MASTHEAD_PUBLISHER = "Тимоха Пресс"
MASTHEAD_PRICE = "Тираж: 1 экз."
COVER_RIBBON_TEXT = "Спецвыпуск"  # diagonal ribbon on the main announcement's photo

# The minimum photo_relevance score (see classifier.py) at which a post
# gets a photo at all — a score of 1 ("not needed") never shows a photo,
# even if one was downloaded. The threshold for teaser photos on the
# cover page (see _announce) — there are no tiers there at all, only this
# score decides; the mosaic has its own, higher threshold
# (MOSAIC_MIN_PHOTO_RELEVANCE).
MIN_PHOTO_RELEVANCE_TO_SHOW = 2
# The threshold in the mosaic is higher (user decision 2026-10-14: for the
# sake of fitting more stories into the issue, mosaic photos are shown
# only where meaning is actually lost without one — score 3): a row of
# brief tiles with photos took up almost a whole page. The cover stays at
# MIN_PHOTO_RELEVANCE_TO_SHOW: the cover doesn't affect mosaic capacity,
# and there the photo is the main device. The only place the mosaic
# threshold is used is _should_show_photo (the same one _estimate_row_span
# uses for its height estimate).
MOSAIC_MIN_PHOTO_RELEVANCE = 3
PHOTO_ASPECT_RATIO = 3 / 2  # width:height — the same framing agreed on in the design mockup (feature)
PHOTO_ASPECT_CSS = "3 / 2"  # the same ratio in CSS aspect-ratio syntax
# In a brief tile (a third of the page width) the photo is shorter: 16:9
# instead of 3:2 saves about 3 row-units per row, without the photo
# losing its point at that narrower tile width.
BRIEF_PHOTO_ASPECT_RATIO = 16 / 9
BRIEF_PHOTO_ASPECT_CSS = "16 / 9"
PHOTO_CAPTION_ROWS = 1  # one row-unit is enough for the caption line under a photo

ROW_UNIT_MM = 5.2  # height of one "brick" of the mosaic grid

# Rubrics (Stage 7): a section header is a separate full-width row of
# fixed small height in the same grid. Channels with no rubric assigned
# (when rubrics are configured at all) are collected into the last
# section under this title.
MISC_RUBRIC_TITLE = "Разное"
CONTINUED_MARK = "(продолжение)"
SECTION_HEADER_ROWS = 2
VIDEOS_SECTION_TITLE = "Новые видео"

# The "New videos" block at the end of the last mosaic page: cards in 3
# columns, a 16:9 color thumbnail on the left (not a wirephoto — this
# isn't a news photo, it's a recognizable video cover), title, channel
# with timestamp and a short link on the right. Card height is fixed so
# the whole block's height can be estimated before rendering; the exact
# value is still confirmed by _fit_bands.
MAX_VIDEOS = 12
VIDEO_COLUMNS = 3
VIDEO_THUMB_W_MM = 38.0
VIDEO_CARD_H_MM = 24.0
VIDEO_GRID_GAP_MM = 2.4
VIDEO_BLOCK_PAD_V_MM = 2.6
VIDEO_MORE_LINE_MM = 6.0

# Page geometry for splitting into pages (see render_pages, _fit_bands):
# how much space in mm is taken up by the incompressible parts of the
# sheet — the lightweight running-header (the whole mosaic uses it now;
# the large masthead and stamp are exclusive to the cover page, see
# _cover_page_html, for which pagination isn't computed at all: it's
# always exactly one page with no overflow) and the colophon footer,
# the same on every page.
PAGE_PADDING_TOP_MM = 10.0
PAGE_PADDING_BOTTOM_MM = 8.0
RUNNING_HEADER_BLOCK_MM = 9.0
FOOTER_BLOCK_MM = 8.0

# Height of the whole cover page header (teaser boxes + black masthead
# plate + the imprint line) — needed to size the main announcement to
# exactly the remainder of the FIRST physical page of the merged sheet
# (see render_pages): that way the cut line between the two A4 halves
# falls between cover blocks, not through the middle of a big headline.
# Measured on an actual render.
COVER_HEADER_BLOCK_MM = 51.0

# What share of the merged sheet's working height goes to the main
# announcement (screamer + kicker + photo); the rest is split between
# the secondary and tertiary announcement rows. 0.58 — per user review
# from 2026-10-09.
COVER_MAIN_BLOCK_SHARE = 0.58

# How many landscape pages are printed on the physical printer as one
# continuous sheet (see render_pages): with landscape=True, layout and
# rendering happen in a single pass over the whole merged sheet (one
# header, one colophon, one continuous mosaic — the transition between
# "pages" within the sheet is NOT visible, unlike the transition between
# separate sheets), and slicing into separate landscape PNGs for printing
# on a regular A4 printer happens after rendering, by simply cutting the
# finished image in half (see the end of render_pages) — per the user's
# request: "so it looks like one page with a seamless transition, but
# comes out as landscape images".
SHEET_UNITS = 2

# Mosaic tile width (col span) by importance tier — only affects column
# width and typography (headline size), NOT text truncation: the full
# post text is always printed (see _estimate_row_span), anything extra
# simply moves to the next page via pagination (render_pages), it's never
# cut off with an ellipsis in place. Three tiers: the most important post
# becomes the lead at the full page width, the next FEATURE_SLOTS by
# importance become feature tiles at half width (columns // 2), everything
# else becomes brief tiles at a third width (columns // 3). The cover page
# (_cover_page_html) doesn't participate in this system at all: it
# doesn't print articles, only announces them, and the same stories still
# appear inside the issue as regular tiles; widths are computed from the
# columns value passed into render_pages, not from a fixed constant (see
# _group_into_bands) — so columns must be a multiple of 6, otherwise
# feature/brief tiles stop dividing columns evenly and rows start
# stretching more often than they should.
FEATURE_SLOTS = 4  # how many posts after the lead get feature tiles — an even number so they always pack into rows of 2 with no remainder at the boundary with the brief tier (see _build_bands)

# How many stories, and where, land on the cover page (see
# _cover_page_html). All of them are taken from stories ACTUALLY printed
# in the issue, in order of importance (_priority_order) — the cover only
# teases what's inside, it doesn't print any articles itself. No page
# numbers (user decision): an exact page number would need the same
# browser measurement as the main text, and a teaser is a decorative
# element.
COVER_SECONDARY_COUNT = 3  # secondary teasers in a row under the main one
# The third row is columns of headlines only, with no kickers or photos.
# Needed so the bottom of the merged sheet doesn't stay empty: there are
# only three secondary teasers, and they don't fill the second physical
# page (twice the usual size) — fix from user review on 2026-10-09
# ("of course a lot of space is given in the second half to just three
# headlines").
COVER_TERTIARY_COUNT = 4
TEASER_TOP_COUNT = 2  # teaser boxes above the masthead plate
# How many stories are needed for the cover to fill up completely: slots
# are taken in order of importance — main, secondary, teaser boxes above
# the masthead, third row (see render_pages). With fewer, the last row
# stays incomplete.
COVER_MIN_ARTICLES = 1 + COVER_SECONDARY_COUNT + TEASER_TOP_COUNT + COVER_TERTIARY_COUNT
TEASER_BOTTOM_COUNT = 3  # items in the bottom inverse strip
# How many stories we ask the LLM for loud teasers for (screamer + kicker,
# see classifier.teasers): the main one plus the secondaries — the rest
# get by with a plain small-size headline.
COVER_LLM_TEASER_COUNT = 1 + COVER_SECONDARY_COUNT
# How many headlines to show as a column in place of the photo, if the
# main announcement has no photo (see _cover_list_html) — so the page
# doesn't gape empty.
COVER_FALLBACK_LIST_COUNT = 5

# Budget in CHARACTERS, not words (see _excerpt) — Cyrillic easily
# produces compound words of 20+ letters ("низкоквалифицированной"), and
# truncating by word count doesn't protect against overflowing a
# plate/screamer line (verified on real data from run #1 — a 5-word
# limit produced a 4-line screamer with an ugly mid-word hyphenation).
# These limits also act as a safeguard against the LLM's answer: the
# model is asked for 1-3 words, but if it gets generous, a type size the
# height of a finger won't forgive it.
TEASER_TOP_MAX_CHARS = 70
TEASER_BOTTOM_MAX_CHARS = 50
SCREAMER_MAX_CHARS = 26
SECONDARY_SCREAMER_MAX_CHARS = 22
COVER_LIST_MAX_CHARS = 55  # fallback column item: at most two lines at large type size
KICKER_MAX_CHARS = 85

# Geometry for estimating how many lines a post's text will actually take
# in a tile of a given width — so row span is computed from the actual
# text volume, not a fixed range. Not exact typography (only the browser
# itself can give that), but an estimate calibrated on live renders with
# a margin on the generous side — underestimating would mean truncating
# text via the .card overflow:hidden safeguard, which is worse than a bit
# of extra empty space at the bottom of a tile.
MOSAIC_GAP_MM = 0.5
CARD_PADDING_H_MM = 3.0
PAGE_SIDE_PADDING_MM = 9.0
PAGE_BORDER_MM = 0.5
CHAR_WIDTH_FACTOR = 0.5  # average character width of a Cyrillic serif font ≈ 0.5 of the font size
ROW_SAFETY_FACTOR = 1.1  # a small margin on top of the character-based estimate (word wrapping breaks lines a bit less efficiently than a raw character count)
ROW_OVERHEAD = 2  # tile padding + byline spacing, in row-units

# Only for _seed_bands — a starting guess at how many rows to try
# cramming into a page in one go, BEFORE the actual browser check
# (_fit_bands). Correctness (the guarantee that text never gets
# truncated) is the responsibility of the headless Chromium measurement,
# not this number — it only saves iterations of the fitting loop
# (without it we'd have to start with one story at a time). Deliberately
# set low: better one extra pass of the loop than starting from a
# trial page that's already clearly overflowing.
PAGE_BUDGET_SAFETY_FACTOR = 0.9

TIER_BODY_FONT_MM = {"lead": 3.2, "feature": 3.05, "brief": 2.9}
TIER_BODY_LINE_HEIGHT = 1.4  # matches the line-height in the CSS for .card p
TIER_HEADLINE_FONT_MM = {"lead": 5.6, "feature": 4.5, "brief": 3.7}
TIER_HEADLINE_LINE_HEIGHT = {"lead": 1.14, "feature": 1.18, "brief": 1.2}

_STYLE_TEMPLATE = """
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; }}
  body {{ background: #fff; }}

  .page {{
    position: relative;
    display: flex;
    flex-direction: column;
    width: {page_w}mm;
    height: {page_h}mm;
    padding: {pad_top}mm 9mm {pad_bottom}mm;
    overflow: hidden;
    background-color: #fdfcf8;
    /* лёгкий растровый узор "под газетную бумагу" — виден в полях и шапке,
       под самой мозаикой его перекрывает сплошной фон плиток */
    background-image: radial-gradient(circle, rgba(0,0,0,0.05) 0.28mm, transparent 0.3mm);
    background-size: 1.6mm 1.6mm;
    border: 0.5mm solid #1a1a1a;
    font-family: "PT Serif", Georgia, "Times New Roman", serif;
    color: #1a1a1a;
  }}

  /* Титульная полоса (см. _cover_page_html) — единственное место, где
     используется весь этот блок .masthead. Тизеры-анонсы (см.
     TEASER_TOP_COUNT) идут отдельной строкой НАД title-блоком, а не внутри
     него — issue-box/stamp по-прежнему абсолютно спозиционированы, но уже
     относительно вложенного .masthead-title, а не всего .masthead, иначе
     их офсеты "уехали" бы вниз вместе с добавившейся сверху строкой
     тизеров. */
  /* ШАПКА ТИТУЛЬНОЙ ПОЛОСЫ-АФИШИ — стилизация ровно по референсу заказчика
     (обложка Daily Bugle): две рамки-анонса над шапкой, название белым по
     чёрной плашке во всю ширину с медальоном-горном посередине, под ней
     тонкая строка выходных данных. Цвет не используем (решение
     пользователя — остаёмся ч/б), и для этого приёма он не нужен: у
     референса плашка и так чёрная, красный там только фоновой подложкой.
     Всё это — ТОЛЬКО на обложке; полосы мозаики идут с .running-header. */
  .cover-teasers {{
    flex: 0 0 auto;
    display: flex; justify-content: space-between; align-items: stretch;
    gap: 4mm; margin-bottom: 2.5mm;
  }}
  .teaser-box {{
    flex: 0 1 78mm; border: 0.5mm solid #1a1a1a; padding: 1.6mm 2.6mm;
    text-align: center;
  }}
  .teaser-label {{
    display: block; font-size: 2mm; letter-spacing: 0.5mm; text-transform: uppercase;
    color: #555; margin-bottom: 0.8mm;
  }}
  .teaser-box strong {{
    font-family: "Superclarendon", Georgia, serif;
    font-size: 3.4mm; line-height: 1.15; text-transform: uppercase;
    hyphens: auto; overflow-wrap: break-word;
  }}

  .masthead-plate {{
    flex: 0 0 auto;
    background: #1a1a1a; color: #fdfcf8;
    display: flex; align-items: center; justify-content: center; gap: 4mm;
    padding: 2.5mm 6mm;
    border-top: 0.8mm solid #1a1a1a; border-bottom: 0.8mm solid #1a1a1a;
  }}
  .plate-word {{
    font-family: "Superclarendon", Georgia, "Times New Roman", serif;
    font-weight: 900; font-size: 20mm; line-height: 1; letter-spacing: 1mm;
    /* Белая обводка вокруг букв, как у логотипа в референсе: плашка
       чёрная, буквы светлые, и тонкий контур отделяет их от фона даже
       там, где краска при печати "залипает". */
    -webkit-text-stroke: 0.4mm #fdfcf8;
  }}
  /* Медальон между словами — чёрный квадрат с белой рамкой и горном
     (bugle) внутри: ровно тот же приём, что у референса, где между DAILY и
     BUGLE стоит квадрат с трубой в лавровом венке. */
  .plate-medallion {{
    flex: 0 0 auto; width: 26mm; height: 20mm;
    border: 0.5mm solid #fdfcf8;
    display: flex; align-items: center; justify-content: center;
  }}
  .plate-medallion svg {{ width: 22mm; height: 16mm; }}

  /* Строка выходных данных под плашкой — мелкий служебный текст по краям и
     дата посередине ("MORNING EDITION / Weather: Page 28 / 50c" в
     референсе). Сюда же переехали пасхалки: номер выпуска слева и
     "Тимоха Пресс" справа. */
  .masthead-strip {{
    flex: 0 0 auto;
    display: flex; justify-content: space-between; align-items: baseline;
    padding: 1mm 1mm 1.4mm;
    border-bottom: 0.4mm solid #1a1a1a;
    font-size: 2.3mm; letter-spacing: 0.3mm; text-transform: uppercase; color: #444;
  }}
  .masthead-strip .edition-date {{
    font-family: "Superclarendon", Georgia, serif; font-size: 3mm; letter-spacing: 1mm;
    color: #1a1a1a;
  }}

  /* Уголковые метки-крестики — отсылка к типографским меткам приводки на
     настоящих печатных пробах. Функции ноль, но без них полоса выглядит
     "экранной", а не "напечатанной" — та же логика, что у растровой
     подложки бумаги и водяных знаков на плитках. */
  .reg-mark {{
    position: absolute; width: 3mm; height: 3mm; opacity: 0.4; pointer-events: none;
  }}
  .reg-mark::before, .reg-mark::after {{ content: ""; position: absolute; background: #1a1a1a; }}
  .reg-mark::before {{ top: 50%; left: 0; width: 100%; height: 0.15mm; transform: translateY(-50%); }}
  .reg-mark::after {{ left: 50%; top: 0; height: 100%; width: 0.15mm; transform: translateX(-50%); }}
  .reg-mark.tl {{ top: 2.5mm; left: 2.5mm; }}
  .reg-mark.tr {{ top: 2.5mm; right: 2.5mm; }}
  .reg-mark.bl {{ bottom: 2.5mm; left: 2.5mm; }}
  .reg-mark.br {{ bottom: 2.5mm; right: 2.5mm; }}

  /* Облегчённая шапка для ВСЕХ полос мозаики без исключения — большой
     мастхед со штампом теперь только на титульной полосе-обложке (см.
     .masthead ниже, используется только там), мозаика вся идёт как
     внутренние полосы газеты с тонкой строкой-ориентиром. */
  .running-header {{
    flex: 0 0 auto;
    text-align: center; text-transform: uppercase; letter-spacing: 0.8mm;
    font-size: 3mm; color: #333;
    padding-bottom: 1.5mm; margin-bottom: 3mm;
    border-bottom: 0.4mm solid #1a1a1a;
  }}
  .running-header strong {{ font-variant: small-caps; font-size: 3.6mm; }}

  .rule-double {{
    flex: 0 0 auto;
    border-top: 0.6mm solid #1a1a1a; border-bottom: 0.25mm solid #1a1a1a;
    height: 1mm; margin: 2mm 0 4mm;
  }}

  /* Мозаика — flex:1 внутри колоночного .page, а не высота по контенту:
     плитки всегда заполняют ровно то место, что осталось до колофона, и
     overflow:hidden обрезает лишнее там же, а не наезжает на подвал (баг
     первой версии: колофон лежал абсолютным слоем НАД мозаикой, а не
     ограничивал её высоту, и текст карточек проступал сквозь и вокруг
     строки выходных данных). min-height:0 обязателен — иначе flex-элемент
     не сжимается меньше высоты своего содержимого и overflow не сработает.
     Фон грид-контейнера служит цветом тонких линеек между плитками
     (grid-gap не красится напрямую) — плитки лежат сверху сплошным фоном. */
  .mosaic {{
    flex: 1 1 auto;
    min-height: 0;
    overflow: hidden;
    display: grid;
    grid-template-columns: repeat({columns}, 1fr);
    grid-auto-rows: {row_unit}mm;
    /* Ряды (см. _build_bands в layout.py) уже посчитаны в Python так, что
       ширины плиток внутри каждого ряда в сумме всегда дают ровно columns —
       обычный построчный поток сам укладывает их одну строку за другой без
       участия браузера в решении, куда что поставить. grid-auto-flow:dense
       (было раньше) перекладывал плитки в более ранние пустые ячейки, когда
       колонки расходились по высоте из-за разной длины текста — из-за этого
       в сетке появлялись беспорядочные серые провалы посреди полосы, а не
       только в её низу. Раз дыр внутри ряда больше не бывает по построению,
       дополнительная переукладка не нужна и только мешала бы порядку
       по важности (важные новости должны идти раньше по потоку). */
    grid-auto-flow: row;
    gap: 0.5mm;
    background-color: #cfc7b8;
  }}

  .card {{
    position: relative;
    overflow: hidden;
    background-color: #fdfcf8;
    padding: 2.6mm 3mm;
  }}

  /* Заголовок рубрики: "▌НАЗВАНИЕ ────" — жирный капслок с линейкой до
     правого края; отдельный ряд сетки, не плитка (нет вензеля .card::after). */
  .sec-head {{
    overflow: hidden;
    background-color: #fdfcf8;
    padding: 0 3mm;
  }}
  .sec-head .card-content {{
    display: flex; align-items: center; gap: 2.4mm; height: 100%;
    font-family: "Superclarendon", Georgia, "Times New Roman", serif;
    font-weight: 900; font-size: 4.6mm; line-height: 1.1;
    text-transform: uppercase; letter-spacing: 0.9mm; white-space: nowrap;
  }}
  .sec-head .sec-bar {{ font-size: 6mm; letter-spacing: 0; }}
  .sec-head .sec-cont {{
    font-weight: 400; font-size: 2.6mm; letter-spacing: 0.4mm; color: #666;
  }}
  .sec-head .sec-rule {{
    flex: 1 1 auto; height: 1.2mm; min-width: 6mm;
    border-top: 0.6mm solid #1a1a1a; border-bottom: 0.25mm solid #1a1a1a;
  }}

  /* Блок "Новые видео" (см. _videos_band_html). */
  .video-block {{
    overflow: hidden;
    background-color: #fdfcf8;
    padding: {video_pad}mm 3mm;
  }}
  .video-block .card-content {{ height: 100%; }}
  .video-grid {{
    display: grid; grid-template-columns: repeat({video_cols}, 1fr);
    gap: {video_gap}mm 4mm;
  }}
  .video-card {{ display: flex; gap: 2.6mm; height: {video_card_h}mm; overflow: hidden; }}
  .video-thumb {{
    flex: 0 0 {video_thumb_w}mm; width: {video_thumb_w}mm; aspect-ratio: 16 / 9;
    background-color: #bdb8ad; overflow: hidden;
  }}
  .video-thumb img {{ display: block; width: 100%; height: 100%; object-fit: cover; }}
  .video-info {{ min-width: 0; display: flex; flex-direction: column; }}
  .video-title {{
    font-size: 3.1mm; line-height: 1.22; font-weight: 700; margin: 0 0 1mm;
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
    overflow: hidden; overflow-wrap: anywhere;
  }}
  .video-meta {{ font-size: 2.4mm; line-height: 1.25; color: #555; margin-bottom: 0.6mm; }}
  .video-link {{ font-size: 2.2mm; color: #888; letter-spacing: 0.1mm; }}
  .video-more {{
    margin-top: 2mm; font-size: 2.8mm; font-style: italic; color: #555; text-align: right;
  }}
  /* Едва заметный водяной вензель на каждой плитке — те самые "мини
     пасхалки": инициал автора вёрстки, растиражированный так тихо, что
     заметен только если приглядеться. Угол чуть гуляет по плиткам, чтобы
     не выглядеть отпечатанным трафаретом. */
  .card::after {{
    content: "T";
    position: absolute; right: 1mm; bottom: -2mm;
    font-size: 9mm; font-weight: 900; color: #000; opacity: 0.05;
    line-height: 1; pointer-events: none; z-index: 0;
  }}
  .card.tier-lead::after {{ font-size: 18mm; bottom: -4mm; }}
  .card.tier-feature::after {{ font-size: 14mm; bottom: -3mm; }}
  .card:nth-of-type(3n)::after {{ transform: rotate(-8deg); }}
  .card:nth-of-type(3n+1)::after {{ transform: rotate(6deg); }}
  .card:nth-of-type(3n+2)::after {{ transform: rotate(-2deg); }}
  .card-content {{ position: relative; z-index: 1; height: 100%; }}

  /* Фото-врезка (feature и brief, photo_relevance >= 2, см.
     _should_show_photo; у каждого поста, независимо от размера плитки,
     может быть фото — лид сюда не попадает вообще, он не часть мозаики,
     у него своя, более крупная фото-врезка .cover-photo на титульной
     полосе, см. _cover_page_html)
     — кадр бьётся в край плитки отрицательными полями, равными паддингу
     .card, дальше текст идёт как обычно с тем же паддингом. Обработка —
     имитация фототелеграфа/wirephoto (согласовано с пользователем):
     ч/б + жёсткий контраст + горизонтальные сканлайны поверх, вместо
     обычной фотографии. Реальный принтер цветной — это осознанный
     стилистический выбор, не техническое ограничение. */
  .card-photo {{
    position: relative;
    margin: -2.6mm -3mm 2mm;
    overflow: hidden;
    background-color: #333;
  }}
  /* .cover-photo — тот же приём на титульной полосе (см. _cover_page_html),
     но без отрицательных полей .card-photo: у .cover-body нет своего
     паддинга, который надо было бы компенсировать (см. комментарий у
     .cover-body ниже), кадр просто на всю ширину блока. Фильтр и
     скан-линии — общие правила с .card-photo, не дублируются: это один и
     тот же согласованный с пользователем приём "имитация фототелеграфа",
     не специфика мозаичной плитки. */
  .cover-photo {{
    position: relative;
    overflow: hidden;
    background-color: #333;
    margin-bottom: 2mm;
  }}
  .card-photo img, .cover-photo img, .secondary-photo img {{
    display: block; width: 100%;
    object-fit: cover;
    filter: grayscale(1) contrast(1.45) brightness(0.94);
  }}
  .card-photo img {{ aspect-ratio: {photo_aspect}; }}
  .tier-brief .card-photo img {{ aspect-ratio: {brief_photo_aspect}; }}
  /* Кадру ГЛАВНОГО анонса высоту задаёт не соотношение сторон, а остаток
     полосы — он должен заполнять её до низа. Вторичным оставлено
     соотношение 4:3 (чуть выше газетного 3:2, так колонка смотрится
     плотнее): во всю высоту колонки они растягивались в неестественные
     вертикальные полосы с крупным планом вместо кадра. */
  .cover-photo img {{ height: 100%; }}
  .secondary-photo img {{ aspect-ratio: 4 / 3; }}
  .card-photo::after, .cover-photo::after, .secondary-photo::after {{
    content: ""; position: absolute; inset: 0; pointer-events: none;
    background-image: repeating-linear-gradient(to bottom, rgba(0,0,0,.6) 0 0.3mm, transparent 0.3mm 0.9mm);
    mix-blend-mode: multiply; opacity: .85;
  }}
  .photo-cutline {{
    font-size: 2.1mm; font-style: italic; color: #777; margin: 0 0 1.4mm;
  }}

  .byline {{
    font-size: 2.4mm; text-transform: uppercase; letter-spacing: 0.4mm;
    color: #777; margin-bottom: 1mm;
  }}
  .card p {{
    font-size: 3.1mm; line-height: 1.4; margin: 0 0 1.4mm;
    text-align: justify; hyphens: auto;
  }}

  .card.tier-lead h2 {{ font-size: 5.6mm; line-height: 1.14; margin: 0 0 2mm; }}
  .card.tier-lead {{ grid-column: span {lead_col}; }}
  .card.tier-lead p:first-of-type::first-letter {{
    float: left; font-size: 8.5mm; line-height: 7mm; padding: 1mm 1.2mm 0 0;
    font-weight: 900;
  }}

  .card.tier-feature h2 {{ font-size: 4.5mm; line-height: 1.18; margin: 0 0 1.4mm; }}
  .card.tier-feature p {{ font-size: 3.05mm; }}

  .card.tier-brief h2 {{ font-size: 3.7mm; line-height: 1.2; margin: 0 0 1mm; }}
  .card.tier-brief p {{ font-size: 2.9mm; }}

  /* ТЕЛО АФИШИ (см. _cover_page_html). Принципиально: тут НЕТ текстов
     статей — только анонсы (крик + подводка + кадр), сами статьи целиком
     печатаются внутри номера, в мозаике. Поэтому высоту тут никто не
     "набирает" текстом: блоки сами распределяются по листу (space-between),
     и полоса выглядит заполненной независимо от того, длинные новости в
     номере или короткие. */
  .cover-body {{
    flex: 1 1 auto;
    min-height: 0;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    padding-top: 2mm;
  }}

  /* Главный анонс забирает всю высоту, не занятую вторичными и полосами:
     при фото оно растягивается на этот остаток (как огромный кадр на
     обложке-референсе), без фото — крик с подводкой встают по центру
     свободного места, а не повисают наверху с дырой под собой. */
  .cover-main {{
    flex: 1 1 auto; min-height: 0; overflow: hidden;
    display: flex; flex-direction: column;
    text-align: center;
  }}
  .screamer {{
    font-family: "Superclarendon", Georgia, "Times New Roman", serif;
    font-weight: 900; line-height: 1.12; letter-spacing: 0.5mm;
    /* 1.12, а не плотные 0.92: при 0.92 краткая над «Й» и точки над «Ё»
       налезали на строку выше. padding-top — чтобы диакритика первой
       строки не срезалась overflow. */
    text-transform: uppercase; margin: 0 0 1mm; padding-top: 0.06em;
    /* Кегль приходит инлайном из _screamer_font_mm — он считается от длины
       крика, как верстальщик подбирает кегль под строку: короткое "СУХО!"
       печатается во всю полосу, длинная "МОБИЛИЗАЦИЯ?" — помельче, но всё
       равно одной строкой. Перенос по слогам оставлен страховкой на совсем
       невероятный случай. */
    hyphens: auto; overflow-wrap: break-word;
  }}
  .cover-kicker {{
    font-size: 6.5mm; line-height: 1.2; font-weight: 700; margin: 0 auto 2.5mm;
    max-width: 200mm;
  }}
  .cover-source {{
    font-size: 2.4mm; text-transform: uppercase; letter-spacing: 0.6mm;
    color: #777; margin-bottom: 1.5mm;
  }}

  /* Кадр главного анонса с диагональной лентой в углу — тот самый
     "SPECIAL EDITION" из референса. */
  .cover-photo {{
    position: relative; overflow: hidden; background-color: #333;
    margin: 0 auto; flex: 1 1 auto; min-height: 0;
  }}
  .cover-ribbon {{
    position: absolute; right: -19mm; top: 9mm; width: 74mm;
    transform: rotate(40deg); z-index: 2;
    background: #1a1a1a; color: #fdfcf8;
    font-family: "Superclarendon", Georgia, serif;
    font-size: 3.2mm; letter-spacing: 0.7mm; text-transform: uppercase;
    text-align: center; padding: 1.4mm 0;
  }}

  /* Чем заполняется место главного анонса, когда у новости нет кадра
     (решение пользователя: без фото афиша держится на одной типографике,
     никаких рамок-заглушек): туда идёт столбец ещё нескольких анонсов
     крупным кеглем через линейки — полоса остаётся плотной, и это читается
     как намеренная "афиша текстом", а не как дыра на месте картинки. */
  .cover-list {{
    flex: 1 1 auto; min-height: 0;
    display: flex; flex-direction: column; justify-content: space-between;
    gap: 1mm; margin: 3mm auto 0; width: 100%; max-width: 230mm;
  }}
  .cover-list-item {{
    border-top: 0.4mm solid #1a1a1a; padding-top: 2mm;
    font-family: "Superclarendon", Georgia, serif;
    font-size: 8.5mm; line-height: 1.12; text-transform: uppercase;
    hyphens: auto; overflow-wrap: break-word;
  }}

  /* Ряд вторичных анонсов — те же "крик + подводка", только мельче, с
     маленьким кадром сверху, если фото есть. */
  .cover-secondary {{
    flex: 1 1 auto; min-height: 0;
    display: flex; gap: 4mm; align-items: stretch;
    border-top: 0.5mm solid #1a1a1a; padding-top: 3mm;
  }}
  .secondary-item {{
    flex: 1 1 0; text-align: center; overflow: hidden;
    border-left: 0.3mm solid #1a1a1a; padding: 0 3mm;
    display: flex; flex-direction: column; justify-content: center;
  }}
  .secondary-item:first-child {{ border-left: none; }}
  .secondary-item h3 {{
    font-family: "Superclarendon", Georgia, serif;
    font-size: 13mm; line-height: 1.14; margin: 0 0 2mm;
    /* 1.14: при 1.02 краткая над «Й» врезалась в строку выше; padding-top —
       чтобы диакритика первой строки не срезалась overflow у .secondary-item */
    padding-top: 0.08em;
    text-transform: uppercase; hyphens: auto; overflow-wrap: break-word;
  }}
  .secondary-item p {{ font-size: 4.6mm; line-height: 1.25; margin: 0; }}

  /* Третий ряд анонсов — только заголовки, колонками через линейки (см.
     COVER_TERTIARY_COUNT): добирает низ полосы, когда трёх вторичных
     анонсов на него не хватает. */
  .cover-tertiary {{
    flex: 0 0 auto;
    display: flex; gap: 4mm; align-items: stretch;
    border-top: 0.5mm solid #1a1a1a; padding-top: 2.5mm; margin-top: 2.5mm;
  }}
  .tertiary-item {{
    flex: 1 1 0; text-align: center; overflow: hidden;
    border-left: 0.25mm solid #1a1a1a; padding: 0 2.5mm;
    font-family: "Superclarendon", Georgia, serif;
    font-size: 4.4mm; line-height: 1.15; text-transform: uppercase;
    hyphens: auto; overflow-wrap: break-word;
  }}
  .tertiary-item:first-child {{ border-left: none; }}
  .secondary-photo {{
    position: relative; overflow: hidden; background-color: #333;
    margin: 0 auto 2mm; flex: 0 1 auto;
    /* кадр не сжимается в узкую полосу, когда текст анонса высокий */
    min-height: 14mm;
  }}

  /* Нижняя полоса-анонс — инверсная (белым по чёрному), как чёрная плашка
     "INSIDE: EXCLUSIVE BUGLE PICS..." по низу обложки в референсе. */
  .inside-strip {{
    flex: 0 0 auto;
    margin-top: 2.5mm; padding: 1.8mm 4mm;
    background: #1a1a1a; color: #fdfcf8;
    font-size: 2.9mm; letter-spacing: 0.3mm; text-align: center;
    text-transform: uppercase;
  }}
  .inside-strip strong {{ font-family: "Superclarendon", Georgia, serif; letter-spacing: 0.6mm; }}

  /* Подвал-колофон — стандартная для настоящих газет строка выходных
     данных. В обычном потоке flex-колонки .page, а не абсолютным слоем
     поверх мозаики — .mosaic (flex:1) сама заканчивается ровно там, где
     начинается колофон, так что плитки физически не могут наехать на
     подвал или проступить сквозь него. */
  .colophon {{
    flex: 0 0 auto;
    margin-top: 2mm; padding-top: 1.2mm; border-top: 0.5mm double #1a1a1a;
    font-size: 2.3mm; letter-spacing: 0.3mm; text-transform: uppercase;
    color: #555; text-align: center;
  }}
</style>
"""

# The leading **bold** fragment of a post — in real data this is almost
# always the lead phrase/headline of the story (Telethon returns
# Message.text with markdown entity formatting already applied by
# default). The length cap guards against the case where there's no
# second "**" within a reasonable range, where .+? would greedily match
# too little but non-greedily grab too much text. After the closing
# "**" we optionally consume trailing headline punctuation (bold text. —
# rest) and a dash connector (bold — rest) — otherwise they'd end up at
# the start of the body as a separate one-period "paragraph" or, worse,
# as the first character of the drop-cap letter (see build_article). The
# prefix excludes "[]()" — otherwise on posts like "[**Headline**](link):
# text" the opening "[" itself would pass for a "decorative" prefix, the
# pattern would find the "**" INSIDE the link as the lead, and
# "](link)" would be left as garbage at the start of the body; such
# posts simply fall through to the fallback path below.
_LEADING_BOLD_RE = re.compile(
    r"^(?P<prefix>[^*\w\[\]()]*)\*\*(?P<headline>.+?)\*\*\s*"
    r"(?:[.!?…:;,]+\s*)?(?:[—–-]\s*)?",
    re.DOTALL,
)
_MAX_HEADLINE_MARKUP_LEN = 200


# Markdown link "[text](url)": group 1 — visible text, 2 — the address.
# html.escape doesn't touch "[]()", so the same pattern also works on
# already-escaped text (_inline_markdown_to_html).
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _inline_markdown_to_html(escaped_text: str) -> str:
    """escaped_text has already gone through html.escape — this only
    converts Telegram-markdown formatting (bold/underline/italic/code/
    links) into HTML tags."""
    text = _LINK_RE.sub(r"\1", escaped_text)  # link -> its visible text, there's nowhere for a link to lead on a printed page
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"<u>\1</u>", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"<em>\1</em>", text, flags=re.DOTALL)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    return text


# Many channels sign off a post with a short "[Channel name](link)" at
# the end — that's a source credit, not part of the story, and in a
# printed column it reads as a dangling two-word phrase on its own. We
# strip only such short paragraphs (after resolving the markdown link to
# text — no more than 4 "words"), so as not to touch paragraphs where
# the link is just part of a full sentence.
_MAX_SIGNATURE_WORDS = 4
# A promo tail made of nothing but links ("[Channel](…) | [We're on
# MAX](…)") counts as a signature regardless of word count: after emoji
# cleanup (strip_emoji) it's left as links and separators, and it can
# have more than four "words".
_LINK_SEPARATORS_RE = re.compile(r"[\s|•·—\-/,]+")


def _looks_like_signature(paragraph: str) -> bool:
    if "](" not in paragraph:
        return False
    if not _LINK_SEPARATORS_RE.sub("", _LINK_RE.sub("", paragraph)):
        return True
    resolved = _LINK_RE.sub(r"\1", paragraph)
    return len(resolved.split()) <= _MAX_SIGNATURE_WORDS


def _strip_signature_tail(paragraphs: list[str]) -> list[str]:
    parts = list(paragraphs)
    while len(parts) > 1 and _looks_like_signature(parts[-1]):
        parts.pop()
    return parts


# Emoji and decorative icons aren't printed (user decision 2026-10-14).
# The ranges follow Unicode pictograph blocks; plain arrows U+2190-21FF,
# typographic marks («» — – … № ° ™ • ·), and currency symbols are not
# included.
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # emoji, flags (regional indicators), skin-tone modifiers
    "\u2600-\u27BF\u2B00-\u2BFF\u25A0-\u25FF"  # symbols, dingbats, arrow icons, geometric shapes (bullets)
    "\u231A\u231B\u23E9-\u23FA\u203C\u2049\u2139\u24C2\u3030\u303D\u3297\u3299"
    "\U000E0020-\U000E007F"  # tags (sub-flags)
    "\uFE0E\uFE0F\u200D\u20E3"  # variation selectors, ZWJ, keycap
    "]"
)
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+(?=[,.:;!?)])")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
# A marker left where a removed emoji/link/bold span used to be: it shows
# which lines were edited (and which became empty specifically because
# of the cleanup). A private-use-area character — never occurs in actual
# post text.
_MARK = "\ue000"


def strip_emoji(text: str) -> str:
    """Removes emoji and pictographs while preserving markdown markup,
    line breaks, and ordinary text. Only touches lines where something
    was removed, so it doesn't disturb whitespace elsewhere in the text.
    What exactly gets cleaned up:
    - a link whose text is made entirely of emoji ("[👉](url)") is removed
      together with its address: no parser understands an empty link, a
      raw URL would get printed; for a link with emoji text, it simply
      disappears;
    - bold formatting is cleaned across the WHOLE text, not line by line:
      in Telethon markdown it can span multiple lines, and doing it line
      by line would treat a "** **" at the seam between two spans as
      empty bold. A span left with nothing but emoji ("**🔥** Headline")
      disappears along with the markup;
    - a line that became empty due to removal ("Text\n🔥\ncontinuation")
      is dropped instead of splitting the paragraph in two; a paragraph
      left with nothing at all ("🔥🔥🔥" as its own paragraph) disappears
      entirely."""
    if not _EMOJI_RE.search(text):
        return text
    text = _EMOJI_RE.sub(_MARK, text)

    def link(m: re.Match) -> str:
        label = m.group(1).replace(_MARK, "").strip()
        if _MARK not in m.group(0):
            return m.group(0)
        return f"{_MARK}[{label}]({m.group(2)})" if label else _MARK

    def bold(m: re.Match) -> str:
        if _MARK not in m.group(1):
            return m.group(0)
        inner = m.group(1).replace(_MARK, "").strip()
        return f"{_MARK}**{inner}**" if inner else _MARK

    # Links before bold: "**[👉](url)**" should collapse entirely.
    text = _BOLD_RE.sub(bold, _LINK_RE.sub(link, text))
    lines = []
    for line in text.split("\n"):
        if _MARK in line:
            line = line.replace(_MARK, "")
            line = re.sub(r"[ \t]{2,}", " ", line).strip()
            line = _SPACE_BEFORE_PUNCT_RE.sub("", line)
            if not line:
                continue
        lines.append(line)
    paragraphs = [p for p in re.split(r"\n{2,}", "\n".join(lines)) if p.strip()]
    return "\n\n".join(paragraphs)


def _paragraphs_html(text: str) -> str:
    parts = _strip_signature_tail(
        [p.strip() for p in re.split(r"\n{2,}", text.strip()) if p.strip()]
    )
    out = []
    for p in parts:
        inner = _inline_markdown_to_html(html.escape(p)).replace("\n", "<br>")
        out.append(f"<p>{inner}</p>")
    return "\n".join(out)


def _strip_markdown(text: str) -> str:
    """Link "[text](url)" -> visible text, "*_`" stripped — readable
    plain text, NOT HTML (escaping is the caller's job at the point of
    insertion). A shared step for the _split_headline fallback path and
    for the short _excerpt teasers (teasers/screamer on the cover page)."""
    plain = _LINK_RE.sub(r"\1", text)
    return re.sub(r"[*_`]", "", plain)


def _excerpt(headline_raw: str, max_chars: int) -> str:
    """Short text teaser for teasers/screamer on the cover page
    (see _cover_page_html) — strips markdown (_strip_markdown) and
    truncates by CHARACTERS (not word count, like the headline in
    _split_headline): Cyrillic easily produces compound words of 20+
    letters, and a word-count limit doesn't protect against overflowing
    a short box/a giant screamer (verified on real data — 5 "words"
    produced a 4-line screamer with a mid-word hyphenation). Accumulates
    whole words as long as the next word wouldn't push past max_chars;
    if even the first word doesn't fit, truncates the word itself (a
    rare case of a very long first word)."""
    words = _strip_markdown(headline_raw).strip().split()
    out: list[str] = []
    length = 0
    for w in words:
        extra = len(w) + (1 if out else 0)
        if out and length + extra > max_chars:
            break
        out.append(w)
        length += extra
    if not out and words:
        out = [words[0][:max_chars]]
    text = " ".join(out)
    return text + "…" if len(out) < len(words) else text


def _split_headline(raw_text: str) -> tuple[str, str]:
    """Returns (headline, body) as the original markdown text (without
    HTML escaping — that's handled by the calling functions)."""
    text = raw_text.strip()
    m = _LEADING_BOLD_RE.match(text)
    if m and len(m.group("headline")) <= _MAX_HEADLINE_MARKUP_LEN:
        prefix = m.group("prefix").strip()
        headline = m.group("headline").strip()
        if prefix:
            headline = f"{prefix} {headline}"  # prefix before the lead (text, not emoji: strip_emoji already ran in build_article) — kept in the headline
        rest = text[m.end():].strip()
        return headline, rest or headline  # empty body after the lead — duplicated so the story isn't left with no text
    # Without a leading bold lead, we only take the post's first "line"
    # (up to the blank line) — otherwise, when truncating by word count,
    # the headline bleeds into the start of the next paragraph and
    # duplicates it (verified on a real post with no lead, where this
    # produced "...in the solo film Of course. Yes,…").
    # Links are resolved to their visible text BEFORE truncating by words
    # — otherwise a raw URL from "[text](link)" would end up in the
    # headline whole, as a single "word" (verified on a post that started
    # with "[**Research**](https://…)").
    plain = _strip_markdown(text)
    first_para = plain.split("\n\n", 1)[0].strip()

    if len(first_para) <= _MAX_HEADLINE_MARKUP_LEN:
        # The first paragraph is short enough on its own to become the
        # whole headline — then, just like with a bold lead, we strip it
        # from the body (the "\n\n" boundary in the original markdown
        # text is in the same place as in plain — resolving links/
        # stripping markup doesn't shift it: both regexes only touch
        # characters inside the paragraph, not the line break itself).
        # Previously the body kept this same paragraph in full — the same
        # text was read twice in a row, once as a large headline, then
        # again in regular type as the first line.
        parts = text.split("\n\n", 1)
        rest = parts[1].strip() if len(parts) > 1 else ""
        return first_para, rest or first_para

    words = first_para.split()
    headline = " ".join(words[:12])
    if len(words) > 12:
        headline += "…"
    return headline, text


@dataclass(frozen=True)
class Article:
    post: Post  # reference to the original post — so it can be returned to the caller as something that didn't fit (see render_pages)
    # Headline before HTML escaping and inline-markdown conversion
    # (headline_html is already ready to insert as HTML) — needed
    # separately for the short plain-text teasers on the cover page
    # (_excerpt: teaser boxes, the bottom strip, and the fallback screamer
    # if no LLM teaser was found for the story, see _announce), where
    # markdown markup needs to be stripped, not turned into tags.
    headline_raw: str
    headline_html: str
    full_body_html: str
    channel: str
    posted_at: datetime
    word_count: int  # post text volume — a secondary signal for the tier split (after importance)
    headline_char_count: int  # for estimating tile height (see _estimate_row_span)
    body_char_count: int
    # Importance score from the LLM classifier (1-5, see classifier.py) —
    # 0 if not passed explicitly (e.g. a call outside the pipeline). It
    # determines both tile width (_build_bands — more important means
    # bigger) and the order pages are filled (render_pages — more
    # important goes earlier, so it's more likely to make the issue when
    # the number of pages is capped, see build_newspaper in
    # pipeline.py).
    importance: int = 0
    # LLM score (see classifier.py) of how much a photo would help this
    # post — determines whether photo_data_uri is shown at all (see
    # _should_show_photo), not just whether it's present: a score of 1
    # ("not needed") never gets a photo, even if one was downloaded and
    # sits on disk.
    photo_relevance: int = 1
    # The data: URI of the photo file, already read and base64-encoded,
    # or None if there's no photo/it wasn't downloaded/the file got lost
    # from disk. Encoded once here rather than in the template: _fit_bands
    # redraws the page HTML many times during the fitting pass (see
    # render_pages), and the photo file itself doesn't change in that
    # time. A data: URI rather than a file:// path — Chromium, in a
    # context where content was loaded via set_content (not via actual
    # navigation to a file:// URL), may refuse to load a local file as a
    # third-party resource.
    photo_data_uri: str | None = None


def _load_photo_data_uri(photo_path: str | None) -> str | None:
    if not photo_path:
        return None
    path = Path(photo_path)
    if not path.is_file():
        logger.warning("photo_path не найден на диске, печатаю без фото: %s", photo_path)
        return None
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def build_article(
    post: Post,
    importance: int = 0,
    photo_relevance: int = 1,
    photo_path: str | None = None,
) -> Article:
    headline_raw, body_raw = _split_headline(strip_emoji(post.text))
    headline_html = _inline_markdown_to_html(html.escape(headline_raw))
    full_body_html = _paragraphs_html(body_raw)
    return Article(
        post=post,
        headline_raw=headline_raw,
        headline_html=headline_html,
        full_body_html=full_body_html,
        channel=post.channel,
        posted_at=post.posted_at,
        word_count=len(body_raw.split()),
        headline_char_count=len(headline_raw),
        body_char_count=len(body_raw),
        importance=importance,
        photo_relevance=photo_relevance,
        photo_data_uri=_load_photo_data_uri(photo_path),
    )


def _should_show_photo(tier: str, article: Article) -> bool:
    """Photos are shown for feature and brief tiles (any post, regardless
    of tile width, can have a photo), but never for the lead tile (user
    decision from 2026-09-27: the issue's one large tile stays pure text
    and typography — headline, drop cap). This rule doesn't apply at all
    on the cover page: there a photo is the main device, and the decision
    about it is made by _cover_page_html."""
    return (
        tier in ("feature", "brief")
        and article.photo_data_uri is not None
        and article.photo_relevance >= MOSAIC_MIN_PHOTO_RELEVANCE
    )


@dataclass(frozen=True)
class _Placement:
    tier: str  # "lead" | "feature" | "brief"
    col_span: int
    row_span: int


def _tile_width_mm(col_span: int, columns: int, page_w: float) -> float:
    """Full physical width of a tile spanning col_span columns out of
    columns — minus page margins and mosaic gaps, but WITHOUT subtracting
    the tile's own padding (unlike _text_width_mm): a photo bleeds to the
    tile's edge via negative margins (see .card-photo in
    _STYLE_TEMPLATE), so its width is the full tile width, not the width
    available for text."""
    content_width = page_w - 2 * PAGE_SIDE_PADDING_MM - 2 * PAGE_BORDER_MM
    col_width = (content_width - (columns - 1) * MOSAIC_GAP_MM) / columns
    return col_width * col_span + (col_span - 1) * MOSAIC_GAP_MM


def _text_width_mm(col_span: int, columns: int, page_w: float) -> float:
    """Width available for text in a tile spanning col_span columns out
    of columns — minus page margins, mosaic gaps, and the tile's own
    padding."""
    return _tile_width_mm(col_span, columns, page_w) - 2 * CARD_PADDING_H_MM


def _photo_row_span(tier: str, col_span: int, columns: int, page_w: float) -> int:
    """How many row-units a photo inset (see _should_show_photo) will
    take up at a given tile width — a frame of fixed aspect ratio (by
    tier: 16:9 for brief, 3:2 for feature) at the full tile width, plus a
    line for the caption.
    Only a starting estimate for _seed_bands — the actual height, like
    for text, is confirmed by the browser fitting loop (_fit_bands)."""
    width_mm = _tile_width_mm(col_span, columns, page_w)
    aspect = BRIEF_PHOTO_ASPECT_RATIO if tier == "brief" else PHOTO_ASPECT_RATIO
    height_mm = width_mm / aspect
    return math.ceil(height_mm / ROW_UNIT_MM) + PHOTO_CAPTION_ROWS


def _rows_for_text(char_count: int, text_width_mm: float, font_size_mm: float, line_height: float) -> int:
    if char_count <= 0:
        return 0
    chars_per_line = max(5, int(text_width_mm / (font_size_mm * CHAR_WIDTH_FACTOR)))
    lines = math.ceil(char_count / chars_per_line)
    return math.ceil(lines * font_size_mm * line_height * ROW_SAFETY_FACTOR / ROW_UNIT_MM)


def _estimate_row_span(tier: str, col_span: int, columns: int, page_w: float, article: Article) -> int:
    """Tile height — based on how many lines the headline and full post
    text will actually take at this width, not a fixed range: text is
    never truncated, anything extra simply moves to the next page
    (render_pages, _fit_bands)."""
    text_width = _text_width_mm(col_span, columns, page_w)
    headline_rows = _rows_for_text(
        article.headline_char_count, text_width, TIER_HEADLINE_FONT_MM[tier], TIER_HEADLINE_LINE_HEIGHT[tier]
    )
    body_rows = _rows_for_text(
        article.body_char_count, text_width, TIER_BODY_FONT_MM[tier], TIER_BODY_LINE_HEIGHT
    )
    photo_rows = _photo_row_span(tier, col_span, columns, page_w) if _should_show_photo(tier, article) else 0
    return ROW_OVERHEAD + photo_rows + headline_rows + body_rows


@dataclass(frozen=True)
class _Band:
    """One mosaic row: several tiles of the SAME width (col_span) that
    always sum to exactly the page width (columns) — see the module
    docstring on the move from free packing to standardized rows with no
    gaps. row_span is shared by the whole row, computed from the longest
    article in it (_band_row_span) — the other members of the row simply
    get a bit of empty space inside their OWN tile if their text is
    shorter. A row is the atomic unit of pagination (_fit_bands,
    _fill_sheet): either the whole thing fits on a page, or the whole
    thing moves to the next one.

    kind distinguishes three kinds of rows so pagination can treat them
    all uniformly (one grid DOM child = one row for header/videos,
    len(members) for news): "news" — news tiles; "header" — a full-width
    rubric header (or the video block's header); "videos" — the "New
    videos" block. header/videos have no members — their ready-made
    markup lives in html (the grid row count is substituted in at output
    time, since _fit_bands may increase it).
    section — the section number: pagination uses it to prevent a row
    from the next section landing on a page before a row of the previous
    one; section_title — the rubric name for news rows ("" — no rubrics
    in this issue)."""

    tier: str  # "lead" | "feature" | "brief" ("" for header/videos)
    col_span: int
    members: tuple[Article, ...]
    row_span: int
    kind: str = "news"  # "news" | "header" | "videos"
    section: int = 0
    section_title: str = ""
    html: str = ""


_ROWS_PLACEHOLDER = "__ROWS__"


def _band_child_count(band: _Band) -> int:
    return len(band.members) if band.kind == "news" else 1


def _band_row_span(tier: str, col_span: int, columns: int, page_w: float, members: tuple[Article, ...]) -> int:
    return max(_estimate_row_span(tier, col_span, columns, page_w, m) for m in members)


def _make_band(
    tier: str, col_span: int, members: list[Article], columns: int, page_w: float,
    section: int = 0, section_title: str = "",
) -> _Band:
    members_t = tuple(members)
    return _Band(
        tier, col_span, members_t, _band_row_span(tier, col_span, columns, page_w, members_t),
        section=section, section_title=section_title,
    )


def _group_into_bands(
    tier: str, pool: list[Article], group_size: int, columns: int, page_w: float,
    section: int = 0, section_title: str = "",
) -> list[_Band]:
    """Cuts pool into full-width rows of group_size articles at a time —
    each tile in a row is columns // group_size wide, so group_size such
    tiles always sum to exactly columns with no remainder. The last row,
    if there weren't enough articles to fill it completely (literally the
    end of the queue — nothing left in pool), doesn't leave a hole; it
    stretches the remaining tiles evenly across the full page width (see
    the module docstring: "stretch the post tile" instead of a gray gap
    in the grid). With rubrics, "end of the queue" means end of the
    SECTION: rows never mix stories from different rubrics."""
    span = columns // group_size
    bands: list[_Band] = []
    for i in range(0, len(pool), group_size):
        chunk = pool[i : i + group_size]
        chunk_span = span if len(chunk) == group_size else columns // len(chunk)
        bands.append(_make_band(tier, chunk_span, chunk, columns, page_w, section, section_title))
    return bands


def _importance_order(articles: list[Article]) -> list[Article]:
    """The issue's stories by descending importance: the lead first (the
    most significant, with text length as a tie-breaker for equal
    scores), then by score, with older stories first for ties. This is
    the global rank: tiers (lead/feature/brief) and the choice of cover
    announcements are computed from it, not from row order — with
    rubrics, row order is already different."""
    if not articles:
        return []
    by_importance = sorted(articles, key=lambda a: (a.importance, a.word_count), reverse=True)
    lead, *rest = by_importance
    return [lead, *sorted(rest, key=lambda a: (-a.importance, a.posted_at))]


def _tier_for_rank(rank: int) -> str:
    """Tile tier by global importance rank (0 — most significant): one
    lead, the next FEATURE_SLOTS are feature, everything else is brief.
    The single place this rule lives: used by both the layout
    (_build_bands) and brief_keys."""
    return "lead" if rank == 0 else "feature" if rank <= FEATURE_SLOTS else "brief"


def brief_keys(
    posts: list[Post], importance_by_key: dict[tuple[str, int], int]
) -> set[tuple[str, int]]:
    """Keys (channel, message_id) of the posts that the tier rule
    (_tier_for_rank) would make brief, by global rank
    (_importance_order). For code outside layout (the pipeline shortens
    such texts in advance), this avoids duplicating the rule; photos
    aren't needed since they don't affect rank."""
    articles = [
        build_article(p, importance_by_key.get((p.channel, p.message_id), 0)) for p in posts
    ]
    return {
        (a.channel, a.post.message_id)
        for rank, a in enumerate(_importance_order(articles))
        if _tier_for_rank(rank) == "brief"
    }


def _plan_sections(
    articles: list[Article],
    rubric_by_channel: dict[str, str] | None,
    rubric_order: list[str] | None,
) -> list[tuple[str, list[Article]]]:
    """The issue's sections: [(name, stories)] in display order. With no
    rubrics — a single unnamed section with all the stories (no headers,
    issue laid out as before Stage 7). With rubrics: first rubric_order,
    then rubrics not listed there (in case the config is out of sync),
    and last a "Разное" (Misc) section for channels with no rubric. Empty
    sections aren't printed. Channel lookup is case-insensitive — in the
    config it may have been written differently than in the post."""
    if not rubric_by_channel:
        return [("", articles)]
    lower = {ch.lower(): name for ch, name in rubric_by_channel.items()}
    groups: dict[str, list[Article]] = {}
    for a in articles:
        groups.setdefault(lower.get(a.channel.lower(), MISC_RUBRIC_TITLE), []).append(a)
    order = list(rubric_order or [])
    order += [n for n in dict.fromkeys(rubric_by_channel.values()) if n not in order]
    order = [n for n in order if n != MISC_RUBRIC_TITLE] + [MISC_RUBRIC_TITLE]
    return [(name, groups[name]) for name in order if groups.get(name)]


def _header_band(title: str, section: int, columns: int, continued: bool = False) -> _Band:
    cont = f'<span class="sec-cont">{CONTINUED_MARK}</span>' if continued else ""
    inner = (
        '<span class="sec-bar">▌</span>'
        f'<span class="sec-title">{html.escape(title)}</span>{cont}<span class="sec-rule"></span>'
    )
    markup = (
        f'<div class="sec-head" style="grid-column: span {columns}; grid-row: span {_ROWS_PLACEHOLDER};">'
        f'<div class="card-content">{inner}</div></div>'
    )
    return _Band("", columns, (), SECTION_HEADER_ROWS, kind="header", section=section, section_title=title, html=markup)


def _build_bands(
    articles: list[Article],
    columns: int,
    page_w: float,
    rubric_by_channel: dict[str, str] | None = None,
    rubric_order: list[str] | None = None,
) -> list[_Band]:
    """Splits the stories into three importance tiers by the LLM score
    (importance, with text length as a tie-breaker for the lead and
    publish date for the rest): one wide "front-page lead" at the full
    page width (the day's most significant story, not just the longest
    post, and always exactly one per issue), the next FEATURE_SLOTS by
    importance become tiles at half the page width (feature), everything
    else at a third width (brief). lead/feature/brief widths are columns,
    columns // 2 and columns // 3 (all three divide columns evenly), so
    the lead is always alone in its row, feature tiles come in pairs,
    brief tiles in triples (_group_into_bands): rows are always filled
    with no remainder, except possibly the very last row of a section
    (see its docstring).

    Tiers are decided GLOBALLY across the whole issue (_importance_order),
    and only then are stories laid out by rubric (_plan_sections): a
    section starts with a header row (_header_band), with the same
    importance-ordered rows inside it; the lead sits first in its own
    section, not necessarily in the first section. With no rubrics, rows
    run as a single queue, exactly as before.

    Every story in the issue, including the most important one, is
    printed RIGHT HERE, in the mosaic: the cover page (_cover_page_html)
    is a showcase/poster — it only teases stories with big headlines and
    photos, but doesn't print a single article in full, so it doesn't
    take anything away from the mosaic.

    The row order in the output is the print order within the issue. It
    no longer sets importance priority when space runs short (with
    rubrics, "didn't fit" means the tail of the last section): the choice
    of what to drop is made by build_newspaper based on importance, and
    the cover takes stories by _importance_order. Returns a flat list of
    _Band (section headers interleaved with news rows)."""
    if not articles:
        return []

    ordered = _importance_order(articles)
    tier_of: dict[int, str] = {}
    for rank, a in enumerate(ordered):
        tier_of[id(a)] = _tier_for_rank(rank)

    sections = _plan_sections(articles, rubric_by_channel, rubric_order)
    bands: list[_Band] = []
    for sec_i, (title, sec_articles) in enumerate(sections):
        ids = {id(a) for a in sec_articles}
        sec_ordered = [a for a in ordered if id(a) in ids]
        if title:
            bands.append(_header_band(title, sec_i, columns))
        lead_pool = [a for a in sec_ordered if tier_of[id(a)] == "lead"]
        feature_pool = [a for a in sec_ordered if tier_of[id(a)] == "feature"]
        brief_pool = [a for a in sec_ordered if tier_of[id(a)] == "brief"]
        for a in lead_pool:
            bands.append(_make_band("lead", columns, [a], columns, page_w, sec_i, title))
        bands += _group_into_bands("feature", feature_pool, 2, columns, page_w, sec_i, title)
        bands += _group_into_bands("brief", brief_pool, 3, columns, page_w, sec_i, title)
    return bands


def to_local(moment: datetime) -> datetime:
    """A moment in time in the timezone of the system doing the layout.
    Everything is stored in the DB as UTC (aware); the user is shown the
    local time. A naive datetime is assumed to already be local
    (`datetime.now()`)."""
    return moment.astimezone() if moment.tzinfo else moment


# Edition name by the local hour of the run: (start hour, slug, name).
# A conventional division of the day: morning 5-12, afternoon 12-17,
# evening 17-23, night 23-5.
_EDITIONS = (
    (5, "morning", "Morning edition"),
    (12, "afternoon", "Afternoon edition"),
    (17, "evening", "Evening edition"),
    (23, "night", "Night edition"),
)


def _edition(run_date: datetime) -> tuple[str, str]:
    hour = to_local(run_date).hour
    slug, name = _EDITIONS[-1][1:]  # before 05:00 — still the night edition
    for start, s, n in _EDITIONS:
        if hour >= start:
            slug, name = s, n
    return slug, name


def edition_name(run_date: datetime) -> str:
    """Edition name for the line under the cover's masthead plate
    ("Evening edition")."""
    return _edition(run_date)[1]


def edition_slug(run_date: datetime) -> str:
    """Short edition slug for the file name (`evening`)."""
    return _edition(run_date)[0]


# --- "New videos" block ---------------------------------------------------

def _video_time_label(published_at: datetime, run_date: datetime) -> str:
    """Local HH:MM; if not today relative to run_date — "вчера HH:MM"
    ("yesterday HH:MM") (or the date, if further back: a run over an
    arbitrary window)."""
    local = published_at.astimezone()
    rd = to_local(run_date)
    days = (rd.date() - local.date()).days
    clock = local.strftime("%H:%M")
    if days <= 0:
        return clock
    if days == 1:
        return f"вчера {clock}"
    return f"{local.strftime('%d.%m')} {clock}"


def _video_card_html(video: YoutubeVideo, run_date: datetime) -> str:
    # The short link is only there as a caption: paper can't be clicked,
    # but it lets the video be found by hand.
    uri = _load_photo_data_uri(video.thumbnail_path)
    thumb = f'<img src="{uri}" alt="">' if uri else ""
    when = _video_time_label(video.published_at, run_date)
    return (
        '<div class="video-card">'
        f'<div class="video-thumb">{thumb}</div>'
        '<div class="video-info">'
        f'<div class="video-title">{html.escape(strip_emoji(video.title))}</div>'
        f'<div class="video-meta">{html.escape(video.channel_title)} · {when}</div>'
        f'<div class="video-link">youtu.be/{html.escape(video.video_id)}</div>'
        "</div></div>"
    )


def _videos_block_rows(shown: int, has_more: bool) -> int:
    """Starting estimate of the block's height in row-units (the exact
    value is confirmed by _fit_bands, same as for text tiles)."""
    grid_rows = math.ceil(shown / VIDEO_COLUMNS)
    mm = (
        2 * VIDEO_BLOCK_PAD_V_MM
        + grid_rows * VIDEO_CARD_H_MM
        + (grid_rows - 1) * VIDEO_GRID_GAP_MM
        + (VIDEO_MORE_LINE_MM if has_more else 0.0)
    )
    return math.ceil(mm / ROW_UNIT_MM)


def _video_bands(
    videos: list[YoutubeVideo] | None, run_date: datetime, section: int, columns: int
) -> list[_Band]:
    """[header, block] or [] with no videos. Newest first, no more than
    MAX_VIDEOS; the rest become a "и ещё N видео" ("and N more videos")
    line."""
    if not videos:
        return []
    ordered = sorted(videos, key=lambda v: v.published_at, reverse=True)
    shown, hidden = ordered[:MAX_VIDEOS], len(ordered) - MAX_VIDEOS
    cards = "".join(_video_card_html(v, run_date) for v in shown)
    more = ""
    if hidden > 0:
        more = f'<div class="video-more">и ещё {hidden} видео</div>'
    markup = (
        f'<div class="video-block" style="grid-column: span {columns}; grid-row: span {_ROWS_PLACEHOLDER};">'
        f'<div class="card-content"><div class="video-grid">{cards}</div>{more}</div></div>'
    )
    rows = _videos_block_rows(len(shown), hidden > 0)
    return [
        _header_band(VIDEOS_SECTION_TITLE, section, columns),
        _Band("", columns, (), rows, kind="videos", section=section, section_title=VIDEOS_SECTION_TITLE, html=markup),
    ]


def _article_html(article: Article, placement: _Placement) -> str:
    when = article.posted_at.astimezone().strftime("%H:%M")
    css_class = f"card tier-{placement.tier}"
    style = f'style="grid-column: span {placement.col_span}; grid-row: span {placement.row_span};"'

    photo_html = ""
    if _should_show_photo(placement.tier, article):
        photo_html = f"""
        <div class="card-photo"><img src="{article.photo_data_uri}" alt=""></div>
        <div class="photo-cutline">Фото: {html.escape(article.channel)}</div>"""

    return f"""
    <div class="{css_class}" {style}>
      <div class="card-content">
        {photo_html}
        <div class="byline">{html.escape(article.channel)} · {when}</div>
        <h2>{article.headline_html}</h2>
        {article.full_body_html}
      </div>
    </div>"""


def _seed_bands(remaining: list[_Band], columns: int, avail_rows: int) -> int:
    """How many rows from the start of remaining to take as the first
    attempt for a page — based on a "cell" budget (available rows ×
    columns), with a margin (PAGE_BUDGET_SAFETY_FACTOR). A row is always
    full width (see _Band), so its "cost" in cells is simply
    columns * row_span, regardless of how many tiles are in it and how
    wide they are. This is only a starting heuristic, so the browser
    check loop (_fit_bands) doesn't have to run one row at a time — the
    real check of whether it actually fits always happens in the
    browser."""
    budget = avail_rows * columns * PAGE_BUDGET_SAFETY_FACTOR
    used = 0
    count = 0
    for b in remaining:
        cost = columns * b.row_span
        if count and used + cost > budget:
            break
        used += cost
        count += 1
    return max(count, 1)


# Two independent kinds of overflow, and they're fixed differently:
# - boxOverflow: the tile itself (the grid cell) sticks out past the
#   bottom edge of .mosaic — meaning the page simply didn't have enough
#   room, and its whole row must move to the next page (see _fit_bands —
#   a row is atomic).
# - contentOverflow: the tile fits on the page, but its OWN text doesn't
#   fit the height it was given (card.scrollHeight > card.clientHeight —
#   scrollHeight sees the actual content even under overflow:hidden).
#   Moving such an article to another page wouldn't help — it has the
#   same row_span estimate, the same result on any page; the row_span of
#   the whole row it's in needs to be increased (see _fit_bands), and
#   re-measured.
_OVERFLOW_CHECK_JS = """
(mosaic) => {
  const mRect = mosaic.getBoundingClientRect();
  const boxOverflow = [];
  const contentOverflow = [];
  Array.from(mosaic.children).forEach((card, i) => {
    const r = card.getBoundingClientRect();
    if (r.bottom > mRect.bottom + 0.5) boxOverflow.push(i);
    // Меряем именно .card-content, а не сам .card: у .card есть декоративный
    // ::after (водяной знак), нарочно торчащий за нижний край плитки
    // (bottom: -Nmm, см. CSS) — card.scrollHeight его учитывает и всегда
    // показывает одну и ту же "нехватку" независимо от реальной высоты
    // плитки, что зацикливало подгонку (проверено на реальных данных).
    // .card-content — обёртка текста без вензеля, ей вообще ничего не
    // должно торчать за пределы.
    const content = card.querySelector('.card-content');
    const shortfall = content.scrollHeight - content.clientHeight;
    if (shortfall > 0.5) contentOverflow.push([i, shortfall]);
  });
  return {boxOverflow, contentOverflow};
}
"""


def _flatten_bands(bands: list[_Band]) -> list[Article]:
    return [m for b in bands for m in b.members]


def _band_bounds(bands: list[_Band]) -> list[tuple[int, int]]:
    """(start, end) indices of .mosaic children for each row — to map
    indices from the _OVERFLOW_CHECK_JS result (it only sees the flat
    DOM) back onto rows. A header or video block counts as one child per
    row."""
    bounds = []
    pos = 0
    for b in bands:
        n = _band_child_count(b)
        bounds.append((pos, pos + n))
        pos += n
    return bounds


def _fit_bands(
    probe_page: Page,
    candidates: list[_Band],
    placements: dict[int, _Placement],
    row_unit_px: float,
    columns: int,
    page_w: float,
    build_probe_html: Callable[[list[_Band]], str],
    tail_count: int = 0,
) -> tuple[list[_Band], list[_Band]]:
    """Actually renders the candidate rows in a headless browser and sees
    what didn't fit — this is not an estimate, it's a measurement in a
    real Chromium layout engine, the only reliable way to guarantee text
    is never silently truncated by overflow:hidden on .card/.mosaic (see
    the module docstring). Distinguishes two kinds of overflow (see
    _OVERFLOW_CHECK_JS), but applies them at the level of the whole ROW,
    not an individual tile — all tiles in a row are required to have the
    same height (see _Band):

    - Some tile's text doesn't fit its OWN cell — meaning the row_span
      estimate for its whole row was too low (moving it to another page
      wouldn't help here: a row's row_span doesn't depend on the page,
      the result would be the same). We increase the row_span of the
      whole row (the other tiles in that row simply get a bit more empty
      space inside themselves) and redraw the same page again.
    - At least one tile in a row (a grid cell) sticks out past the
      bottom edge of .mosaic — a genuine lack of room on the page, the
      whole row moves to the next one (after overflows from the first
      case are gone — otherwise the extra height of one row could AGAIN
      push another one past the edge).

    tail_count — how many of the last rows in queue are reserved (the
    "New videos" block on the last page): they aren't dropped; if space
    runs short, room is freed up by removing the last NEWS row instead —
    the video reservation matters more than one more story.

    Returns (what fit on this page, what didn't fit — for the next one),
    both as lists of _Band."""
    queue = list(candidates)
    overflow_accum: list[_Band] = []
    iteration = 0
    while queue:
        iteration += 1
        bounds = _band_bounds(queue)
        probe_page.set_content(build_probe_html(queue), wait_until="load")
        result = probe_page.eval_on_selector(".mosaic", _OVERFLOW_CHECK_JS) or {}
        content_overflow = result.get("contentOverflow") or []
        box_overflow = set(result.get("boxOverflow") or [])
        logger.debug(
            "_fit_bands: попытка %d, рядов=%d, тесно_в_своей_плитке=%d, не влезло_на_полосу=%d",
            iteration, len(queue), len(content_overflow), len(box_overflow),
        )

        def _band_index_of(flat_index: int) -> int:
            return next(i for i, (s, e) in enumerate(bounds) if s <= flat_index < e)

        if content_overflow:
            extra_rows_by_band: dict[int, int] = {}
            for index, shortfall_px in content_overflow:
                band_i = _band_index_of(index)
                extra_rows = math.ceil(shortfall_px / row_unit_px)
                extra_rows_by_band[band_i] = max(extra_rows_by_band.get(band_i, 0), extra_rows)
            for band_i, extra_rows in extra_rows_by_band.items():
                band = queue[band_i]
                new_band = replace(band, row_span=band.row_span + extra_rows)
                queue[band_i] = new_band
                for m in new_band.members:
                    placements[id(m)] = replace(placements[id(m)], row_span=new_band.row_span)
            continue  # same queue, but with row_span fixed — redraw

        if not box_overflow:
            return queue, overflow_accum

        bad_bands = {_band_index_of(i) for i in box_overflow}
        if tail_count:
            news_n = len(queue) - tail_count
            if any(i >= news_n for i in bad_bands):
                if news_n <= 0:
                    return queue, overflow_accum  # the reservation doesn't fit even on its own — nothing to remove
                bad_bands = {i for i in bad_bands if i < news_n} | {news_n - 1}
        fitted = [b for i, b in enumerate(queue) if i not in bad_bands]
        newly_bad = [b for i, b in enumerate(queue) if i in bad_bands]
        if not fitted:
            if len(queue) == 1:
                band = queue[0]
                if len(band.members) > 1:
                    # A single row takes the whole page, and even so it
                    # doesn't fit, but it has several tiles — before
                    # giving up, try the same "stretching" used for an
                    # incomplete last row of the queue (_group_into_bands):
                    # split it into individual full-width tiles and try
                    # each one separately — a wider column needs fewer
                    # lines per unit of text, so the required height is
                    # lower.
                    solo_bands = [
                        _make_band(band.tier, columns, [m], columns, page_w, band.section, band.section_title)
                        for m in band.members
                    ]
                    for b in solo_bands:
                        for m in b.members:
                            placements[id(m)] = _Placement(b.tier, b.col_span, b.row_span)
                    queue = solo_bands
                    continue
                # Already a single tile at the full page width, and it
                # still doesn't fit (an extremely rare case, an abnormally
                # long post) — there's nothing left to shrink. Return it
                # as is: the .card{overflow:hidden} safeguard will keep
                # it from overlapping neighbors (there are none here),
                # but its own text may get truncated.
                return queue, overflow_accum
            # This used to immediately return queue[:1] as "fit" with
            # no check — but it could fail to fit even on its own (the
            # overflow was caused by sharing the page with others, not by
            # itself). Instead of returning right away, we shrink queue to
            # the first row and RE-CHECK it in isolation on the next
            # pass — the rest goes to overflow.
            overflow_accum = queue[1:] + overflow_accum
            queue = queue[:1]
            continue
        overflow_accum = newly_bad + overflow_accum
        queue = fitted
    return queue, overflow_accum


def _fill_sheet(
    probe_page: Page,
    remaining: list[_Band],
    tail: list[_Band],
    placements: dict[int, _Placement],
    row_unit_px: float,
    columns: int,
    page_w: float,
    avail_rows: int,
    build_probe_html: Callable[[list[_Band]], str],
) -> tuple[list[_Band], list[_Band]]:
    """Fills one page with rows, without stopping at the first-pass but
    cautious estimate (_seed_bands). An example of the real problem this
    fixes: the first row in the queue turned out to be short — its
    actual height is small, but _seed_bands, computing a rough "cell"
    budget from it, decided the next row wouldn't fit and stopped at 1-2
    rows, leaving almost the whole landscape page empty even though
    there was actually room. With a fixed number of pages (Stage 3 — a
    printed issue, not an endless feed), a whole empty page is far worse
    than misjudging the batch size.

    After the starting batch, we try adding the rest ONE row AT A TIME —
    and, importantly, we don't stop at the first one that doesn't fit:
    it's simply deferred, and the next ones are tried after it.
    Otherwise one large row near the start of the queue would block room
    for smaller ones that would have fit perfectly well in the same
    leftover space. With rubrics (sections) this is only allowed within
    the CURRENT section: as soon as something from a section has been
    deferred, rows from later sections aren't taken onto this page —
    sections run strictly in sequence, a row from the next one can never
    land on a page before a row of the previous one. The cost may be a
    bit of empty space left at the bottom of the page.

    A section header is never left orphaned: it's placed on a page only
    together with the first row of its section (the pair is tried as a
    whole), otherwise it moves to the next page. If a section started on
    a previous page, this page's instance of it starts with the header
    marked "(continued)".

    tail — reserved rows at the end of the page (the "New videos" block
    on the last allowed page): they're present in every trial, so the
    news only takes up what's left over. Returns (this page's rows
    including the tail, the queue's deferred rows — without the tail)."""
    tail = list(tail)
    n_tail = len(tail)

    def fit(cur: list[_Band]) -> tuple[list[_Band], list[_Band]]:
        nonlocal tail
        fitted, over = _fit_bands(
            probe_page, cur + tail, placements, row_unit_px, columns, page_w, build_probe_html, tail_count=n_tail
        )
        if n_tail:
            tail = fitted[-n_tail:]  # the block's height may have grown based on the measurement
            fitted = fitted[:-n_tail]
        return fitted, over

    items = list(remaining)
    if items and items[0].kind == "news" and items[0].section_title:
        items.insert(0, _header_band(items[0].section_title, items[0].section, columns, continued=True))

    tail_rows = sum(b.row_span for b in tail)
    seed_n = _seed_bands(items, columns, max(avail_rows - tail_rows, 1)) if items else 0
    fitted, overflow = fit(items[:seed_n])
    rest = items[seed_n:]
    if fitted and fitted[-1].kind == "header":  # orphan at the edge of the starting batch
        overflow.insert(0, fitted.pop())
    pending = overflow + rest

    if items and not fitted:
        # No row landed at all (either the first one didn't fit along
        # with the header, or the batch was a single header) — otherwise
        # the page would be left empty and the queue wouldn't advance.
        # Take the first non-header row: on its own it always fits (this
        # is guaranteed by _fit_bands). A "continued" header before it
        # isn't needed here — the section already continues from it.
        first = next((i for i, b in enumerate(pending) if b.kind != "header"), None)
        if first is None:
            pending = []  # nothing but headers with no rows — drop them, otherwise the page loop never advances
        else:
            fitted, over = fit([pending[first]])
            cut = first
            if fitted and first > 0 and pending[first - 1].section == pending[first].section:
                cut = first - 1
            pending = pending[:cut] + over + pending[first + 1 :]
            if not fitted:
                logger.warning("лист мозаики: резерв под видео не оставил места ни одной новости")
    current = fitted

    skipped: list[_Band] = []
    open_header: _Band | None = None  # the current section's header, not yet placed on a page
    sec_skipped: list[_Band] = []  # deferred rows of the current section

    def close_section() -> None:
        nonlocal open_header, sec_skipped
        if open_header is not None:
            skipped.append(open_header)
        skipped.extend(sec_skipped)
        open_header, sec_skipped = None, []

    for item in pending:
        if item.kind == "header":
            close_section()
            if skipped:  # something from earlier sections has already been deferred — this section doesn't land here
                skipped.append(item)
            else:
                open_header = item
            continue
        if skipped:
            skipped.append(item)
            continue
        extra = [open_header] if open_header is not None else []
        trial_fitted, trial_overflow = fit(current + extra + [item])
        if trial_overflow:
            sec_skipped.append(item)
            continue
        current = trial_fitted
        open_header = None
    close_section()
    return current + tail, skipped


# The corner crosshair registration marks (see .reg-mark in
# _STYLE_TEMPLATE) are the same on every page, cover and mosaic alike,
# so they're factored out into a shared constant.
_REG_MARKS_HTML = (
    '<div class="reg-mark tl"></div><div class="reg-mark tr"></div>'
    '<div class="reg-mark bl"></div><div class="reg-mark br"></div>'
)


def _footer_suffix(page_num: int, total_pages: int) -> str:
    return f" · стр. {page_num}/{total_pages}" if total_pages > 1 else ""


def _band_html(band: _Band, placements: dict[int, _Placement]) -> str:
    if band.kind == "news":
        return "\n".join(_article_html(a, placements[id(a)]) for a in band.members)
    return band.html.replace(_ROWS_PLACEHOLDER, str(band.row_span))


def _page_html(
    page_bands: list[_Band],
    placements: dict[int, _Placement],
    page_num: int,
    total_pages: int,
    date_label: str,
    edition: str,
) -> str:
    """A mosaic page (feature/brief) — the lead isn't part of it, it has
    its own cover page (_cover_page_html). All mosaic pages are
    identical: every single one uses the lightweight running-header, the
    large masthead is exclusive to the cover (see the module
    docstring)."""
    cards_html = "\n".join(_band_html(b, placements) for b in page_bands)

    header_html = f"""
    <div class="running-header">
      <strong>{MASTHEAD_TITLE}</strong> · {date_label} · {edition} · стр. {page_num} из {total_pages}
    </div>"""

    return f"""
  <div class="page" data-page="{page_num}">
    {_REG_MARKS_HTML}
    {header_html}
    <div class="mosaic">
      {cards_html}
    </div>
    <div class="colophon">TG Newspaper · собрано и свёрстано в «Тимоха Пресс»{_footer_suffix(page_num, total_pages)}</div>
  </div>"""


# The medallion at the center of the masthead plate — a bugle in a
# laurel wreath, as the device between DAILY and BUGLE on the reference
# cover. Drawn directly as SVG rather than taken as an image/emoji: a
# trumpet emoji in black-and-white print looks like a colored blob, and
# external files can't be loaded into this render anyway (the HTML is
# delivered via set_content, see render_pages).
_MEDALLION_SVG = """
<svg viewBox="0 0 120 86" xmlns="http://www.w3.org/2000/svg" fill="none">
  <g stroke="#fdfcf8" stroke-width="2.4" stroke-linecap="round">
    <path d="M30 72 Q12 44 28 16"/>
    <path d="M90 72 Q108 44 92 16"/>
  </g>
  <g fill="#fdfcf8">
    <ellipse cx="20" cy="54" rx="5" ry="2.4" transform="rotate(-28 20 54)"/>
    <ellipse cx="17" cy="42" rx="5" ry="2.4" transform="rotate(-10 17 42)"/>
    <ellipse cx="19" cy="30" rx="5" ry="2.4" transform="rotate(14 19 30)"/>
    <ellipse cx="100" cy="54" rx="5" ry="2.4" transform="rotate(28 100 54)"/>
    <ellipse cx="103" cy="42" rx="5" ry="2.4" transform="rotate(10 103 42)"/>
    <ellipse cx="101" cy="30" rx="5" ry="2.4" transform="rotate(-14 101 30)"/>
  </g>
  <g fill="#fdfcf8">
    <path d="M38 32 L54 38 L54 56 L38 62 Z"/>
    <rect x="54" y="42" width="26" height="9"/>
    <rect x="58" y="36" width="3.4" height="7"/>
    <rect x="65" y="36" width="3.4" height="7"/>
    <rect x="72" y="36" width="3.4" height="7"/>
    <circle cx="84" cy="46.5" r="4.6"/>
  </g>
</svg>
"""


@dataclass(frozen=True)
class _Announce:
    """One cover-page announcement: screamer, kicker, and photo. The
    article's text isn't part of this at all — the cover entices the
    reader into the issue, it doesn't print the story."""

    screamer: str
    kicker: str
    photo_data_uri: str | None
    channel: str


def _announce(
    article: Article, teasers: dict[tuple[str, int], tuple[str, str]], screamer_max_chars: int
) -> _Announce:
    """Assembles an announcement: takes the screamer and kicker from the
    LLM (classifier.teasers), and if the model returned nothing for this
    story (or teasers weren't requested at all), honestly falls back to
    its own headline. A cover with less intrigue is worse than one with
    more, but that's no reason to hold up the issue."""
    key = (article.channel, article.post.message_id)
    llm_screamer, llm_kicker = teasers.get(key, ("", ""))
    screamer = _excerpt(strip_emoji(llm_screamer) or article.headline_raw, screamer_max_chars)
    kicker = _excerpt(strip_emoji(llm_kicker) or article.headline_raw, KICKER_MAX_CHARS)
    photo = (
        article.photo_data_uri
        if article.photo_relevance >= MIN_PHOTO_RELEVANCE_TO_SHOW
        else None
    )
    return _Announce(screamer, kicker, photo, article.channel)


def _teaser_box_html(article: Article) -> str:
    excerpt = html.escape(_excerpt(article.headline_raw, TEASER_TOP_MAX_CHARS))
    return (
        '<div class="teaser-box"><span class="teaser-label">Читайте в номере</span>'
        f"<strong>{excerpt}</strong></div>"
    )


def _cover_tertiary_html(articles: list[Article]) -> str:
    if not articles:
        return ""
    items = "".join(
        f'<div class="tertiary-item">{html.escape(_excerpt(a.headline_raw, COVER_LIST_MAX_CHARS))}</div>'
        for a in articles
    )
    return f'<div class="cover-tertiary">{items}</div>'


def _inside_strip_html(teaser_bottom: list[Article]) -> str:
    if not teaser_bottom:
        return ""
    items = " &nbsp;·&nbsp; ".join(
        html.escape(_excerpt(a.headline_raw, TEASER_BOTTOM_MAX_CHARS)) for a in teaser_bottom
    )
    return f'<div class="inside-strip"><strong>Внутри номера:</strong> {items}</div>'


# The screamer's font size is fit to its length, the way a typesetter
# picks a size for a line: a short "СУХО!" ("DRY!") is printed at the
# full page width, a long "МОБИЛИЗАЦИЯ?" ("MOBILIZATION?") is smaller,
# but still ONE line. A stepped lookup table doesn't work here (verified
# by rendering: 12 characters at 38mm didn't fit and wrapped with a
# mid-word hyphen) — line width is linear in font size, so it has to be
# computed by division. The factor is the average width of a capital
# Superclarendon Black letter as a fraction of its size, with a margin
# for letter-spacing.
SCREAMER_CHAR_WIDTH_FACTOR = 1.0
SCREAMER_FONT_MAX_MM = 52.0
SCREAMER_FONT_MIN_MM = 14.0
SECONDARY_FONT_MAX_MM = 19.0  # ceiling on the secondary screamer's font size — it's still not the main one on the page


def _screamer_font_mm(text: str, available_mm: float) -> float:
    if not text:
        return SCREAMER_FONT_MIN_MM
    ideal = available_mm / (len(text) * SCREAMER_CHAR_WIDTH_FACTOR)
    return round(max(SCREAMER_FONT_MIN_MM, min(SCREAMER_FONT_MAX_MM, ideal)), 1)


def _cover_list_html(articles: list[Article]) -> str:
    """Fallback content for the photo spot — a column of text
    announcements (see .cover-list in _STYLE_TEMPLATE)."""
    if not articles:
        return ""
    items = "".join(
        f'<div class="cover-list-item">{html.escape(_excerpt(a.headline_raw, COVER_LIST_MAX_CHARS))}</div>'
        for a in articles
    )
    return f'<div class="cover-list">{items}</div>'


def _cover_photo_html(announce: _Announce, width_mm: float, ribbon: bool) -> str:
    """The announcement's photo. Returns an empty string with no photo —
    the cover then relies purely on typography (user decision from
    2026-10-06), not a placeholder frame."""
    if announce.photo_data_uri is None:
        return ""
    cls = "cover-photo" if ribbon else "secondary-photo"
    ribbon_html = f'<div class="cover-ribbon">{COVER_RIBBON_TEXT}</div>' if ribbon else ""
    return (
        f'<div class="{cls}" style="width: {width_mm}mm;">{ribbon_html}'
        f'<img src="{announce.photo_data_uri}" alt=""></div>'
    )


def _secondary_item_html(announce: _Announce, photo_width_mm: float, column_mm: float) -> str:
    # Font size follows the same logic as the main screamer
    # (_screamer_font_mm), only it measures the column width rather than
    # the whole page: otherwise a short "ВЗЛОМ!" ("HACKED!") and a long
    # "ТРАГЕДИЯ!" ("TRAGEDY!") would print at the same size, and the
    # second one would wrap with a mid-word hyphen (verified by
    # rendering).
    # 0.72 is a margin for wide capital letters (Ж, Д, Ш): in a narrow
    # column the average letter width is off by more than across the
    # whole page, and "ДОРОЖЕ?" ("PRICIER?") still wrapped even at 0.85
    # (verified by rendering).
    font_mm = min(_screamer_font_mm(announce.screamer, column_mm * 0.72), SECONDARY_FONT_MAX_MM)
    return f"""
      <div class="secondary-item">
        {_cover_photo_html(announce, photo_width_mm, ribbon=False)}
        <h3 style="font-size: {font_mm}mm;">{html.escape(announce.screamer)}</h3>
        <p>{html.escape(announce.kicker)}</p>
      </div>"""


def _cover_page_html(
    main: _Announce,
    secondary: list[_Announce],
    teaser_top: list[Article],
    teaser_bottom: list[Article],
    tertiary: list[Article],
    fallback_list: list[Article],
    date_label: str,
    edition: str,
    issue_number: int,
    total_pages: int,
    main_photo_mm: float,
    secondary_photo_mm: float,
    secondary_column_mm: float,
    main_block_mm: float,
    screamer_width_mm: float,
) -> str:
    """The cover page is the issue's POSTER, not an article: big
    announcements (screamer + kicker + photo), teaser boxes above the
    masthead, and an inverse strip at the bottom, but not a single word
    of story text. The stories themselves, including the most important
    one, are printed inside, in the mosaic (see _build_bands) — so the
    cover doesn't "take away" anything from the issue and risks no text
    truncation.

    The page's height is distributed among the blocks themselves
    (justify-content: space-between on .cover-body) rather than filled
    with text, so the cover looks equally full on a day with long
    stories and on a day with short ones. It's always printed, exactly
    once per issue, so the _fit_bands fitting pass isn't needed here:
    there's nothing that can overflow."""
    teasers_html = ""
    if teaser_top:
        boxes = "".join(_teaser_box_html(a) for a in teaser_top)
        teasers_html = f'<div class="cover-teasers">{boxes}</div>'

    secondary_html = ""
    if secondary:
        items = "".join(
            _secondary_item_html(a, secondary_photo_mm, secondary_column_mm) for a in secondary
        )
        secondary_html = f'<div class="cover-secondary">{items}</div>'

    # Room for the main screamer: a photo if there is one, otherwise a
    # text column of announcements (the page shouldn't gape empty on a
    # day with not a single photo).
    main_fill_html = _cover_photo_html(main, main_photo_mm, ribbon=True) or _cover_list_html(
        fallback_list
    )

    return f"""
  <div class="page" data-page="1">
    {_REG_MARKS_HTML}
    {teasers_html}
    <div class="masthead-plate">
      <span class="plate-word">{MASTHEAD_LEFT}</span>
      <span class="plate-medallion">{_MEDALLION_SVG}</span>
      <span class="plate-word">{MASTHEAD_RIGHT}</span>
    </div>
    <div class="masthead-strip">
      <span>{edition} · {MASTHEAD_PUBLISHER} · выпуск № {issue_number}</span>
      <span class="edition-date">{date_label}</span>
      <span>{MASTHEAD_PRICE}</span>
    </div>
    <div class="cover-body">
      <div class="cover-main" style="flex: 0 0 {main_block_mm}mm;">
        <div class="screamer" style="font-size: {_screamer_font_mm(main.screamer, screamer_width_mm)}mm;">{html.escape(main.screamer)}</div>
        <div class="cover-kicker">{html.escape(main.kicker)}</div>
        <div class="cover-source">Подробности — внутри номера · {html.escape(main.channel)}</div>
        {main_fill_html}
      </div>
      {secondary_html}
      {_cover_tertiary_html(tertiary)}
    </div>
    {_inside_strip_html(teaser_bottom)}
    <div class="colophon">TG Newspaper · собрано и свёрстано в «Тимоха Пресс»{_footer_suffix(1, total_pages)}</div>
  </div>"""


def render_pages(
    posts: list[Post],
    out_dir: Path,
    basename: str = "page",
    run_date: datetime | None = None,
    page_size: str = DEFAULT_PAGE_SIZE,
    dpi: int = DEFAULT_DPI,
    columns: int = DEFAULT_COLUMNS,
    landscape: bool = False,
    max_pages: int | None = None,
    importance_by_key: dict[tuple[str, int], int] | None = None,
    photo_relevance_by_key: dict[tuple[str, int], int] | None = None,
    photo_path_by_key: dict[tuple[str, int], str] | None = None,
    issue_number: int = 0,
    teaser_provider: Callable[[list[Post]], dict[tuple[str, int], tuple[str, str]]] | None = None,
    rubric_by_channel: dict[str, str] | None = None,
    rubric_order: list[str] | None = None,
    videos: list[YoutubeVideo] | None = None,
    fit_only: bool = False,
) -> tuple[list[Path], list[Post]]:
    """Renders the newspaper into one or more PNGs at the physical size
    needed, at the given DPI. The browser's viewport is set in CSS
    pixels to the exact page size, and device_scale_factor = dpi/96
    handles the screenshot's pixel density — the resulting PNGs land on
    the sheet 1:1, with no recalculation at Stage 4.

    With landscape=True, the printed sheet is merged from SHEET_UNITS
    landscape pages (2 by default) — not by gluing together finished
    images after the fact, but in a SINGLE layout-and-render pass over
    the whole merged sheet at once: one header, one colophon, one
    continuous piece of content spanning the full sheet height — the
    transition between "pages" within the sheet isn't marked in any way,
    the sheet reads as one continuous page (per the user's request:
    "so it's literally one single page, a completely seamless
    transition"). The sheet leaves this function already sliced into
    SHEET_UNITS landscape PNGs — by simply cutting the already-finished
    image in half by height (see the end of the function), not by a
    separate render — because there's nothing to print the whole thing
    on (without slicing): a regular printer takes A4, not a sheet twice
    that size glued together. Because of this, an article wrapping
    across the seam between two landscape PNGs within one mosaic sheet
    is an expected and acceptable case (like a column carrying over on
    a real newspaper page).

    The first merged sheet is always the cover page
    (_cover_page_html: loud announcements, photos and the large masthead,
    but not a single full article) — it comes ON TOP OF the max_pages
    budget for the mosaic (user decision from 2026-09-27: "let's add two
    [pages] that form one big one"). `basename_1.png`/`basename_2.png`
    are its two halves. After that come the mosaic pages, where all the
    issue's stories are actually printed, also as pairs of PNGs per
    page:
    `basename_3.png`/`basename_4.png` is the first mosaic page, and so
    on. Unlike the cover with its large masthead, every mosaic page is
    identical — all of them use the lightweight running-header, there's
    no more "the first mosaic page is special".

    Page splitting is not just an estimate based on text volume: each
    candidate page is actually rendered in headless Chromium, and
    whatever doesn't fit (see _fit_bands) is genuinely moved to the next
    page. Otherwise (trusting only a character-count estimate) — in
    practice specific posts get underestimated, and text is silently
    truncated by overflow:hidden, which is exactly what all this
    pagination is supposed to avoid (verified on real data — without
    this check several articles lost their last lines).

    max_pages — a hard ceiling on the number of landscape pages in the
    MOSAIC (Stage 3, following user review: a printed newspaper is a
    fixed issue, not an endless feed); it's converted into a ceiling on
    the number of merged mosaic sheets as ceil(max_pages / SHEET_UNITS).
    The cover page doesn't count against this limit — it's always
    rendered, as a separate sheet, exactly once per issue (for non-empty
    posts). If anything is left over after the mosaic limit, it is NOT
    rendered at all — it's returned as the second element of the tuple,
    as the list of Posts that didn't fit, so the caller (see
    build_newspaper in pipeline.py) can decide what to do with them:
    shorten them with the LLM and try again, or drop them from the issue
    as not important enough. importance_by_key determines both what
    makes the issue earlier (see the row order in _build_bands) and
    which story the cover promotes to the main announcement versus the
    secondary ones: the first ones by this same order, among those that
    actually made it into the issue, are taken.

    photo_relevance_by_key/photo_path_by_key — a post's photo (see
    collector.py, pipeline._merge_story_arcs) and the LLM's score of how
    much it's needed (classifier.py). Shown on mosaic tiles and on the
    lead on the cover when photo_relevance >= MIN_PHOTO_RELEVANCE_TO_SHOW
    (mosaic — _should_show_photo; cover — a separate check right inside
    _cover_page_html: the lead is no longer part of the mosaic's tier
    system, but the threshold is the same). The photo's processing is an
    imitation of a wirephoto/phototelegraph (see .card-photo/.cover-photo
    in _STYLE_TEMPLATE), not the photo as-is — a deliberate stylistic
    choice, not a technical limitation of the printer (which is color).

    issue_number — the issue number shown in the imprint line under the
    masthead plate (.masthead-strip) — only on the cover page, the
    mosaic doesn't show it. Not yet tied to a run counter in the
    DB — the caller (build_newspaper) doesn't pass it, so it defaults to
    0; the parameter already exists now so that wiring it up externally
    later (e.g. to run_id from storage.py) is a clean one-line addition,
    with no signature changes needed.

    rubric_by_channel/rubric_order (Stage 7) — rubrics as mosaic sections
    (see _build_bands, _fill_sheet): empty means an issue with no
    sections, as before.

    videos — the "New videos" block at the end of the LAST mosaic page.
    Room for it is guaranteed and news stories can't push it out: on the
    last page allowed by max_pages, the block is reserved in advance
    (tail in _fill_sheet), and the stories that end up without room
    because of that go into leftover — build_newspaper will shorten or
    drop them. No videos means no block.

    fit_only — fitting only: the function returns ([], leftover) right
    after laying out the mosaic, without building the cover (no
    teaser_provider or LLM calls), without taking screenshots, and
    without creating out_dir — nothing is written to disk. Fitting
    follows the same path (_fill_sheet in the same browser context) as a
    full render, so leftover matches. Needed by build_newspaper for
    intermediate trials: no pages from failed attempts are left in the
    issue's directory."""
    run_date = run_date or datetime.now()
    page_w, page_h = PAGE_SIZES_MM[page_size]
    if landscape:
        page_w, page_h = page_h, page_w
    importance_by_key = importance_by_key or {}
    photo_relevance_by_key = photo_relevance_by_key or {}
    photo_path_by_key = photo_path_by_key or {}
    articles = [
        build_article(
            p,
            importance=importance_by_key.get((p.channel, p.message_id), 0),
            photo_relevance=photo_relevance_by_key.get((p.channel, p.message_id), 1),
            photo_path=photo_path_by_key.get((p.channel, p.message_id)),
        )
        for p in posts
    ]
    if not articles:
        return [], []

    sheet_units = SHEET_UNITS if landscape else 1
    sheet_h = page_h * sheet_units

    avail_rows = int(
        (sheet_h - PAGE_PADDING_TOP_MM - PAGE_PADDING_BOTTOM_MM - RUNNING_HEADER_BLOCK_MM - FOOTER_BLOCK_MM)
        // ROW_UNIT_MM
    )

    # Rows are already in print-priority order (lead first, then
    # descending by importance, see _build_bands) — when the number of
    # pages is capped, it's precisely this list's order that decides
    # what makes the issue if there isn't room for everything; important
    # stories need to be earlier in the queue (and earlier among the
    # cover announcements, see below).
    bands_all = _build_bands(articles, columns, page_w, rubric_by_channel, rubric_order)
    placements: dict[int, _Placement] = {}
    for b in bands_all:
        for m in b.members:
            placements[id(m)] = _Placement(b.tier, b.col_span, b.row_span)

    style = _STYLE_TEMPLATE.format(
        page_w=page_w,
        page_h=sheet_h,
        pad_top=PAGE_PADDING_TOP_MM,
        pad_bottom=PAGE_PADDING_BOTTOM_MM,
        columns=columns,
        row_unit=ROW_UNIT_MM,
        lead_col=columns,
        photo_aspect=PHOTO_ASPECT_CSS,
        brief_photo_aspect=BRIEF_PHOTO_ASPECT_CSS,
        video_pad=VIDEO_BLOCK_PAD_V_MM,
        video_cols=VIDEO_COLUMNS,
        video_gap=VIDEO_GRID_GAP_MM,
        video_card_h=VIDEO_CARD_H_MM,
        video_thumb_w=VIDEO_THUMB_W_MM,
    )
    date_label = to_local(run_date).strftime("%d.%m.%Y")
    edition = edition_name(run_date)

    def wrap(fragment: str) -> str:
        return f'<!doctype html><html lang="ru"><head><meta charset="utf-8">{style}</head><body>{fragment}</body></html>'

    viewport_w = round(page_w * CSS_PX_PER_MM)
    viewport_h = round(sheet_h * CSS_PX_PER_MM)
    scale = dpi / 96
    row_unit_px = ROW_UNIT_MM * CSS_PX_PER_MM  # in CSS pixels — the same units as getBoundingClientRect/scrollHeight

    max_sheets = math.ceil(max_pages / sheet_units) if max_pages is not None else None

    if not fit_only:
        out_dir.mkdir(parents=True, exist_ok=True)
    out_paths: list[Path] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            # Fitting (_fit_bands) is rendered in the same context (the
            # same device_scale_factor) as the final screenshot —
            # otherwise, at a different rasterization scale, text might
            # wrap onto lines slightly differently (character widths
            # round to physical pixels differently), and a page that
            # "fit" on a trial render at scale 1 could actually get
            # truncated in the final one at scale 3+ (verified on real
            # data — the discrepancy was exactly one line wrap on one
            # article).
            context = browser.new_context(
                viewport={"width": viewport_w, "height": viewport_h},
                device_scale_factor=scale,
            )
            probe_page = context.new_page()

            sheets: list[list[_Band]] = []
            # The video block is the last "section" in the queue: it comes after all the news.
            video_section = 1 + max((b.section for b in bands_all), default=0)
            video_items = _video_bands(videos, run_date, video_section, columns)
            remaining = bands_all + video_items
            while remaining:
                # The whole mosaic is printed with the same running-header
                # — there's no "the first page is special" anymore, the
                # large masthead is exclusive to the cover (see the
                # function's docstring).
                def build_probe(queue: list[_Band]) -> str:
                    # page_num/total_pages here are placeholders: the
                    # real numbering is only known once the final number
                    # of mosaic pages has been computed (see below).
                    # They don't affect the .mosaic height that
                    # _fit_bands measures — the running-header has a
                    # fixed height (RUNNING_HEADER_BLOCK_MM) regardless
                    # of the text in it.
                    return wrap(_page_html(queue, placements, 2, 2, date_label, edition))

                candidates_count = len(remaining)
                # The last allowed page: the video block is reserved in advance.
                tail: list[_Band] = []
                if (
                    max_sheets is not None
                    and len(sheets) == max_sheets - 1
                    and video_items
                    and remaining[-1].kind == "videos"
                ):
                    tail = remaining[-len(video_items):]
                    remaining = remaining[: -len(video_items)]
                fitted_bands, remaining = _fill_sheet(
                    probe_page, remaining, tail, placements, row_unit_px, columns, page_w, avail_rows, build_probe
                )
                sheets.append(fitted_bands)
                logger.info(
                    "лист мозаики %d: рядов в очереди было=%d влезло=%d осталось_в_очереди=%d",
                    len(sheets), candidates_count, len(fitted_bands), len(remaining),
                )
                if max_sheets is not None and len(sheets) >= max_sheets:
                    break

            # If we exited due to max_sheets rather than an emptied
            # queue, whatever's left doesn't make the issue at all (see
            # the docstring). We hand the original Posts back to the
            # caller instead of rendering extra pages.
            leftover_posts = [m.post for b in remaining for m in b.members]  # header/videos have empty members
            if fit_only:
                return [], leftover_posts

            # Cover announcements are drawn from stories that were
            # ACTUALLY printed (that fit), in order of importance
            # (_build_bands puts the lead first), not from bands_all as a
            # whole — otherwise the cover would tease an article that
            # ultimately didn't fit and was dropped (see
            # build_newspaper). With rubrics, row order no longer equals
            # importance order, so announcements are picked by global
            # rank (_importance_order).
            placed_ids = {id(m) for sheet_bands in sheets for b in sheet_bands for m in b.members}
            fitted_articles = [a for a in _importance_order(articles) if id(a) in placed_ids]
            main_article = fitted_articles[0]
            secondary_articles = fitted_articles[1 : 1 + COVER_SECONDARY_COUNT]
            rest_for_teasers = fitted_articles[1 + COVER_SECONDARY_COUNT :]

            # Loud teasers are requested from the LLM in one batch, and
            # only for the stories that actually made it onto the cover
            # in large type — hence the late point of the call: before
            # this, there's simply no way to know who they are.
            teasers: dict[tuple[str, int], tuple[str, str]] = {}
            if teaser_provider is not None:
                announced = [main_article, *secondary_articles][:COVER_LLM_TEASER_COUNT]
                teasers = teaser_provider([a.post for a in announced])

            main_announce = _announce(main_article, teasers, SCREAMER_MAX_CHARS)
            secondary_announces = [
                _announce(a, teasers, SECONDARY_SCREAMER_MAX_CHARS) for a in secondary_articles
            ]

            # Stories further down by importance are assigned to cover
            # slots with no repeats (otherwise one headline gets printed
            # twice — verified by rendering). The fallback column only
            # takes its share WHEN the main announcement has no photo
            # and it will actually be shown: otherwise it silently ate
            # into the stories meant for the teaser boxes and the bottom
            # strip, and that strip simply wasn't printed.
            # Teaser boxes above the plate are assigned BEFORE the third
            # row: they're part of the reference's recognizable look,
            # and on a day with few stories it's better to keep them and
            # shorten the third row than the other way around (verified
            # by rendering with eight stories — the teaser boxes
            # disappeared entirely).
            teaser_top = rest_for_teasers[:TEASER_TOP_COUNT]
            after_top = rest_for_teasers[len(teaser_top) :]
            tertiary_articles = after_top[:COVER_TERTIARY_COUNT]
            after_tertiary = after_top[len(tertiary_articles) :]
            fallback_list = (
                []
                if main_announce.photo_data_uri is not None
                else after_tertiary[:COVER_FALLBACK_LIST_COUNT]
            )
            teaser_bottom = after_tertiary[len(fallback_list) :][:TEASER_BOTTOM_COUNT]

            content_w = page_w - 2 * PAGE_SIDE_PADDING_MM - 2 * PAGE_BORDER_MM
            main_photo_mm = round(content_w * 0.72, 1)
            secondary_column_mm = round(content_w / max(len(secondary_announces), 1) - 10, 1)
            secondary_photo_mm = round(secondary_column_mm * 0.95, 1)
            # The main announcement gets the larger share of the page,
            # the remaining rows get the rest. The share is tuned so
            # that, first, the bottom of the page doesn't stay empty
            # (review fix: the three secondary announcements used to get
            # half the merged sheet and hung there with nothing below
            # them), and second, the A4 cut line falls on the main
            # announcement's photo, not on a large headline — a cut
            # through a photo is invisible, a cut through a headline is
            # obvious at a glance.
            work_h = sheet_h - PAGE_PADDING_TOP_MM - PAGE_PADDING_BOTTOM_MM - COVER_HEADER_BLOCK_MM
            main_block_mm = round(work_h * COVER_MAIN_BLOCK_SHARE, 1)

            # Page numbering is by sheet (a continuous editorial count,
            # not by physical PNG): the cover is page 1, then the mosaic
            # pages.
            total_pages = 1 + len(sheets)
            cover_html = _cover_page_html(
                main_announce, secondary_announces, teaser_top, teaser_bottom,
                tertiary_articles, fallback_list,
                date_label, edition, issue_number, total_pages, main_photo_mm, secondary_photo_mm,
                secondary_column_mm, main_block_mm, content_w,
            )
            mosaic_html = "\n".join(
                _page_html(sheet_bands, placements, i + 2, total_pages, date_label, edition)
                for i, sheet_bands in enumerate(sheets)
            )
            all_sheets_html = cover_html + "\n" + mosaic_html
            total_page_divs = 1 + len(sheets)  # cover + mosaic pages

            final_page = context.new_page()
            # The viewport covers ALL the sheets at once — the cover and
            # the mosaic (not just one, as for trials) — otherwise
            # Page.screenshot(clip=...) refuses to cut a region outside
            # the viewport on the second and later sheets.
            final_page.set_viewport_size({"width": viewport_w, "height": viewport_h * total_page_divs})
            final_page.set_content(wrap(all_sheets_html), wait_until="load")
            locator = final_page.locator(".page")
            # Each .page here is already a whole merged sheet (height
            # equal to sheet_units landscape pages, see the docstring) —
            # the first is always the cover, then the mosaic pages.
            # There's nothing to print it whole on (a printer takes A4),
            # so we hand it back the same way, just sliced into
            # sheet_units landscape PNGs by height — with no re-render,
            # using the same already-finished image, so slicing adds no
            # extra seam (unlike rendering the two pages separately,
            # which this pass was specifically chosen to replace).
            file_index = 1
            for i in range(locator.count()):
                box = locator.nth(i).bounding_box()
                assert box is not None, "не удалось получить размеры отрендеренного листа"
                slice_h = box["height"] / sheet_units
                for j in range(sheet_units):
                    out_path = out_dir / f"{basename}_{file_index}.png"
                    final_page.screenshot(
                        path=str(out_path),
                        clip={"x": box["x"], "y": box["y"] + j * slice_h, "width": box["width"], "height": slice_h},
                    )
                    out_paths.append(out_path)
                    file_index += 1
        finally:
            browser.close()
    return out_paths, leftover_posts
