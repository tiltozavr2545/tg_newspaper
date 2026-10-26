"""Shared look of the local web console: "the paste-up desk".

The console is the pre-press room of the paper. The page is a layout board
with a faint non-photo-blue grid; things that will be printed are paper with
crop marks; the four process inks carry fixed meanings (see the tokens in
STYLE). Every console page goes through page(); the module also holds the
small formatting helpers shared by the page modules (Russian dates, edition
names, the app version).

Fonts are bundled (static/fonts, served by scripts/console.py at
/static/fonts/<name>.woff2) — the console has a "no external CSS/JS" rule.
Fontsource packages @fontsource/pt-serif, pt-sans, pt-sans-narrow 5.3.0
(SIL OFL, see static/fonts/OFL.txt); macOS has the PT fonts installed, so
local() wins there and Windows gets the bundled files.
"""

from __future__ import annotations

import html
import os
import subprocess
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version

from .config import HOME_ENV, REPO_ROOT
from .layout import edition_slug

_MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)

_EDITION_RU = {
    "morning": "Утренний выпуск",
    "afternoon": "Дневной выпуск",
    "evening": "Вечерний выпуск",
    "night": "Ночной выпуск",
}


def ru_date(moment: datetime) -> str:
    """"26 октября 2026" — no locale involved."""
    return f"{moment.day} {_MONTHS_GENITIVE[moment.month - 1]} {moment.year}"


def ru_day_month(moment: datetime) -> str:
    """"26 октября"."""
    return f"{moment.day} {_MONTHS_GENITIVE[moment.month - 1]}"


def edition_ru(moment: datetime) -> str:
    """"Утренний выпуск" etc. — the edition by the local hour of the moment."""
    return _EDITION_RU[edition_slug(moment)]


def app_version() -> str:
    """The installed package's version; "dev" where there's no metadata.

    Only CI fills in the release number at build time, and in the repo, the
    pyproject version doesn't match the tag. So when running from the repo
    (no TG_NEWSPAPER_HOME), this shows that it's a dev build plus a short
    commit hash; if git is unavailable, it just stays "+dev". In a release,
    it's just the version.
    """
    try:
        ver = version("tg-newspaper")
    except PackageNotFoundError:
        return "dev"
    if os.environ.get(HOME_ENV):
        return ver
    ver = f"{ver}+dev"
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
            timeout=2, capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return ver
    commit = result.stdout.strip()
    return f"{ver} · {commit}" if commit else ver


# ----------------------------------------------------------------- fonts

_LATIN = (
    "U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,"
    "U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD"
)
_CYRILLIC = "U+0301,U+0400-045F,U+0490-0491,U+04B0-04B1,U+2116"

# (family, file stem, weight, style, local() names)
_FACES = (
    ("PT Serif", "pt-serif", 400, "normal", ("PT Serif", "PTSerif-Regular")),
    ("PT Serif", "pt-serif", 700, "normal", ("PT Serif Bold", "PTSerif-Bold")),
    ("PT Serif", "pt-serif", 400, "italic", ("PT Serif Italic", "PTSerif-Italic")),
    ("PT Sans", "pt-sans", 400, "normal", ("PT Sans", "PTSans-Regular")),
    ("PT Sans", "pt-sans", 700, "normal", ("PT Sans Bold", "PTSans-Bold")),
    ("PT Sans Narrow", "pt-sans-narrow", 700, "normal", ("PT Sans Narrow Bold", "PTSans-NarrowBold")),
)


def _font_faces() -> str:
    out = []
    for family, stem, weight, style, local_names in _FACES:
        for subset, rng in (("cyrillic", _CYRILLIC), ("latin", _LATIN)):
            local = ", ".join(f'local("{n}")' for n in local_names)
            out.append(
                f'@font-face {{ font-family: "{family}"; font-style: {style}; '
                f"font-weight: {weight}; font-display: swap; "
                f'src: {local}, url("/static/fonts/{stem}-{subset}-{weight}-{style}.woff2") '
                f'format("woff2"); unicode-range: {rng}; }}'
            )
    return "\n".join(out)


FONT_FACES = _font_faces()

# ----------------------------------------------------------------- styles

STYLE = """
:root {
  --ink: #231F20; --ink-70: #5A5556; --ink-30: #B9B5B5;
  --cyan-line: #8FCDE3; --cyan-ink: #1D6C88; --grid: #DCEDF3; --rule: #D5E9F0;
  --magenta: #C8006F; --marker: #FFE94D; --marker-soft: #FFF6B0;
  --board: #F2F7F9; --paper: #FFFFFF;
  --serif: "PT Serif", Georgia, "Times New Roman", serif;
  --sans: "PT Sans", "Segoe UI", system-ui, sans-serif;
  --narrow: "PT Sans Narrow", "Arial Narrow", "PT Sans", sans-serif;
  --mono: "PT Mono", ui-monospace, Menlo, Consolas, monospace;
  color-scheme: light;
}
*, *::before, *::after { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; font: 400 15px/1.45 var(--sans); color: var(--ink);
  background-color: var(--board);
  background-image: linear-gradient(var(--grid) 1px, transparent 1px),
                    linear-gradient(90deg, var(--grid) 1px, transparent 1px);
  background-size: 24px 24px;
}
img { max-width: 100%; }
.wrap { max-width: 1180px; margin: 0 auto; padding: 0 32px; }
main.wrap { padding-top: 40px; padding-bottom: 72px; }
@media (max-width: 640px) { .wrap { padding: 0 16px; } main.wrap { padding-top: 28px; padding-bottom: 56px; } }

a { color: var(--ink); text-decoration: underline; text-decoration-color: var(--magenta);
    text-decoration-thickness: 1.5px; text-underline-offset: 3px; }
a:hover { color: var(--magenta); }
:focus-visible { outline: 2px solid var(--magenta); outline-offset: 2px; }

/* header plate */
.plate { background: var(--ink); color: #fff; }
.plate-in { display: flex; align-items: center; justify-content: space-between; gap: 16px; min-height: 56px; }
.plate a { color: #fff; text-decoration: none; }
.plate a:hover { color: #fff; }
.brand { font: 700 22px/1 Superclarendon, "Superclarendon Bold", "PT Serif", Georgia, serif; letter-spacing: .01em; }
.plate nav { display: flex; gap: 22px; }
.plate nav a { font: 400 15px/1 var(--sans); padding: 6px 0 5px; border-bottom: 3px solid transparent; }
.plate nav a:hover { border-bottom-color: var(--ink-70); }
.plate nav a[aria-current="page"] { border-bottom-color: var(--magenta); }
.plate :focus-visible { outline-color: #fff; }
@media (max-width: 480px) { .brand { font-size: 18px; } .plate nav { gap: 14px; } }

/* footer */
.foot { display: flex; justify-content: flex-end; align-items: baseline; gap: 14px; flex-wrap: wrap;
        padding-bottom: 32px; color: var(--cyan-ink); font: 400 13px/1.4 var(--sans); }
.foot form { margin: 0; }
.foot button { background: none; border: 0; padding: 0; font: inherit; color: var(--cyan-ink);
               text-decoration: underline; cursor: pointer; }
.foot button:hover { color: var(--magenta); }

/* type */
h1 { font: 700 56px/1.05 var(--serif); margin: 0 0 16px; max-width: 18ch; text-wrap: balance; }
@media (max-width: 640px) { h1 { font-size: 40px; } }
h2 { font: 700 28px/1.15 var(--serif); margin: 0; }
.sec-head { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between;
            gap: 12px 24px; margin: 56px 0 20px; padding-top: 12px; border-top: 2px solid var(--ink); }
.sec-actions { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
.sec-actions form { margin: 0; }
.meta { font: 700 13px/1.3 var(--narrow); color: var(--cyan-ink); }
.meta.dim { color: var(--ink-70); }
.lede { font: 400 17px/1.45 var(--sans); color: var(--ink-70); max-width: 62ch; margin: 0 0 20px; }
p { margin: 0 0 14px; max-width: 75ch; }
code { font: 400 13px/1.4 var(--mono); background: var(--board); padding: 1px 4px; }
.hint { font: 400 13px/1.45 var(--sans); color: var(--ink-70); max-width: 70ch; margin: 6px 0 8px; }
.note { font-size: 14px; color: var(--ink-70); max-width: 52ch; margin: 14px 0 0; }

/* paper and crop marks */
.paper { background: var(--paper); border: 1px solid var(--cyan-line); border-radius: 0; }
.crop { --m: 14px; --g: 6px; --p: calc(var(--m) + var(--g)); padding: var(--p);
  background:
    linear-gradient(var(--ink),var(--ink)) left 0 top var(--p) / var(--m) 1px no-repeat,
    linear-gradient(var(--ink),var(--ink)) left var(--p) top 0 / 1px var(--m) no-repeat,
    linear-gradient(var(--ink),var(--ink)) right 0 top var(--p) / var(--m) 1px no-repeat,
    linear-gradient(var(--ink),var(--ink)) right var(--p) top 0 / 1px var(--m) no-repeat,
    linear-gradient(var(--ink),var(--ink)) left 0 bottom var(--p) / var(--m) 1px no-repeat,
    linear-gradient(var(--ink),var(--ink)) left var(--p) bottom 0 / 1px var(--m) no-repeat,
    linear-gradient(var(--ink),var(--ink)) right 0 bottom var(--p) / var(--m) 1px no-repeat,
    linear-gradient(var(--ink),var(--ink)) right var(--p) bottom 0 / 1px var(--m) no-repeat; }

/* buttons */
.btn { display: inline-block; font: 700 15px/1.2 var(--sans); padding: 10px 18px; border: 0; border-radius: 2px;
       background: var(--ink); color: #fff; text-decoration: none; cursor: pointer; }
.btn:hover { color: #fff; box-shadow: inset 0 -3px 0 var(--magenta); }
.btn.secondary { background: var(--paper); color: var(--ink); border: 1.5px solid var(--ink); padding: 8.5px 16.5px; }
.btn.secondary:hover { color: var(--ink); border-color: var(--magenta); box-shadow: none; }
.btn.quiet { background: none; color: var(--ink); padding: 10px 6px; text-decoration: underline;
             text-decoration-color: var(--magenta); text-decoration-thickness: 1.5px; text-underline-offset: 3px; }
.btn.quiet:hover { color: var(--magenta); box-shadow: none; }
.btn.press.big { font: 700 28px/1.15 var(--serif); padding: 18px 32px; min-width: 300px; }
@media (max-width: 480px) { .btn.press.big { min-width: 0; width: 100%; } }
.btn.is-busy, .btn.secondary.is-busy {
  color: #fff; border: 0; cursor: progress; opacity: 1;
  background: repeating-linear-gradient(-45deg, var(--ink) 0 12px, #3D3839 12px 24px);
  background-size: 34px 34px; animation: feed 0.9s linear infinite; }
@keyframes feed { to { background-position: 34px 0; } }
@media (prefers-reduced-motion: reduce) { .btn.is-busy { animation: none; } }
.actions { display: flex; flex-wrap: wrap; gap: 10px 14px; align-items: center; margin-top: 18px; }

/* stamps */
.stamp { display: inline-block; font: 700 13px/1 var(--narrow); padding: 4px 7px 3px; margin: 3px;
         border: 1.5px solid currentColor; outline: 1px solid currentColor; outline-offset: 2px;
         transform: rotate(-2deg); white-space: nowrap; }
.stamp.ink { color: var(--ink); }
.stamp.nonrepro { color: var(--cyan-ink); border-style: dashed; outline: 0; }

/* banners, notices, errors */
.attn { background: var(--marker-soft); border-left: 6px solid var(--marker); padding: 16px 20px;
        max-width: 820px; margin: 0 0 20px; }
.attn a { font-weight: 700; }
.notice { background: var(--paper); padding: 12px 16px; margin: 12px 0; white-space: pre-wrap; max-width: 75ch; }
.notice.ok { border: 1px solid var(--ink); }
.notice.err { border: 2px solid var(--magenta); }
.notice.err b { color: var(--magenta); font-weight: 700; }
.error-box { background: var(--paper); border: 1px solid var(--magenta); border-left: 2px solid var(--magenta);
             padding: 16px 20px; margin: 0 0 20px; max-width: 100%; overflow-x: auto; }
.error-box pre { margin: 0; white-space: pre-wrap; word-break: break-word; font: 400 13px/1.45 var(--mono); color: var(--ink); }
.error-box .err-title { font-weight: 700; color: var(--magenta); margin: 0 0 8px; }

/* tables */
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
th { font: 700 13px/1.3 var(--narrow); color: var(--cyan-ink); text-align: left; padding: 10px 14px;
     background: var(--paper); position: sticky; top: 0; border-bottom: 2px solid var(--ink); white-space: nowrap; }
td { padding: 12px 14px; border-bottom: 1px solid var(--rule); vertical-align: top; }
tr:last-child td { border-bottom: 0; }
tr.hidden { display: none; }

/* home */
.home-grid { display: grid; grid-template-columns: minmax(0, 1fr); gap: 40px; align-items: start; }
@media (min-width: 980px) { .home-grid.two { grid-template-columns: 7fr 5fr; gap: 56px; } }
.home-grid h1 { margin-top: 6px; }
.slip { max-width: 640px; }
.cover { display: block; }
.cover img { display: block; width: 100%; height: auto; }
.cover img + img { border-top: 1px dashed var(--cyan-line); }
.cover-meta { margin-top: 10px; }
.cover-meta a { font: 700 17px/1.3 var(--serif); }
.learning { margin: 28px 0 0; max-width: 62ch; }
.learning b { font: 700 17px/1 var(--serif); }
.run-rows { list-style: none; margin: 0; padding: 0; }
.run-row { display: grid; grid-template-columns: 160px 80px 1fr auto; gap: 8px 16px; align-items: baseline;
           padding: 14px 18px; border-bottom: 1px solid var(--rule); }
.run-row:last-child { border-bottom: 0; }
.run-row .date { font: 400 17px/1.3 var(--serif); }
@media (max-width: 640px) { .run-row { grid-template-columns: 1fr auto; } .run-row .date { grid-column: 1 / -1; } }
.empty { padding: 18px; margin: 0; }

/* run page */
.runs-ribbon { display: flex; flex-wrap: wrap; align-items: stretch; margin: 0 0 28px; font: 700 13px/1.2 var(--narrow); }
.runs-ribbon > * { padding: 7px 12px; border-left: 1px solid var(--cyan-line); }
.runs-ribbon > :first-child { border-left: 0; padding-left: 0; }
.runs-ribbon a { color: var(--cyan-ink); text-decoration: none; }
.runs-ribbon a:hover { color: var(--magenta); text-decoration: underline; }
.runs-ribbon .current { background: var(--ink); color: #fff; }
.sheets { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 24px; margin: 0 0 20px; }
@media (max-width: 640px) { .sheets { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; } .sheets .crop { --m: 8px; --g: 3px; } }
.sheet figure { margin: 0; }
.sheet .paper { display: block; }
.sheet .paper a { display: block; line-height: 0; }
.sheet .paper img { display: block; width: 100%; height: auto; }
.sheet .paper a + a { border-top: 1px dashed var(--cyan-line); }
.sheet figcaption { margin-top: 6px; }
.video-row { display: grid; grid-template-columns: 200px 1fr auto; gap: 8px 16px; align-items: baseline;
             padding: 12px 18px; border-bottom: 1px solid var(--rule); }
.video-row:last-child { border-bottom: 0; }
@media (max-width: 640px) { .video-row { grid-template-columns: 1fr; } }
.seg { display: inline-flex; flex-wrap: wrap; margin: 0 0 18px; border: 1.5px solid var(--ink); border-radius: 2px; background: var(--paper); }
.seg button { font: 400 15px/1.2 var(--sans); padding: 9px 16px; border: 0; border-left: 1.5px solid var(--ink);
              background: var(--paper); color: var(--ink); cursor: pointer; }
.seg button:first-child { border-left: 0; }
.seg button b { font: 700 17px/1 var(--serif); margin-left: 6px; }
.seg button[aria-pressed="true"] { background: var(--ink); color: #fff; }
.seg button:focus-visible { outline-offset: 3px; }
.posts { min-width: 760px; }
.posts td.when { width: 110px; white-space: nowrap; }
.posts td.decision { width: 230px; font-size: 14px; color: var(--ink-70); }
.posts td.scores { width: 210px; }
.post-text { font: 400 15px/1.55 var(--serif); max-width: 75ch; }
tr[data-status="out"] .post-text { color: var(--cyan-ink); }
details.expand summary { cursor: pointer; list-style: none; }
details.expand summary::-webkit-details-marker { display: none; }
details.expand summary::after { content: " развернуть"; font: 700 13px/1 var(--narrow); color: var(--cyan-ink);
                                    text-decoration: underline; text-decoration-color: var(--magenta); white-space: nowrap; }
details.expand[open] summary::after { content: " свернуть"; }
.full-text { white-space: pre-wrap; margin-top: 8px; }
.fig { font: 700 28px/1 var(--serif); display: block; }
.fig.none, .dash { color: var(--cyan-ink); }
.subscores { font-size: 13px; color: var(--ink-70); margin: 4px 0 8px; }
.photo { font-size: 13px; color: var(--ink-70); }
.meter { display: inline-flex; gap: 2px; margin-left: 6px; vertical-align: middle; }
.meter i { width: 8px; height: 8px; border: 1px solid var(--cyan-line); }
.meter i.on { background: var(--ink); border-color: var(--ink); }

/* setup wizard */
.setup-grid { display: grid; grid-template-columns: minmax(0, 1fr); gap: 28px; align-items: start; }
@media (min-width: 980px) { .setup-grid { grid-template-columns: 240px minmax(0, 1fr); gap: 48px; } .rail { position: sticky; top: 24px; } }
.rail ol { list-style: none; margin: 0; padding: 0; }
.rail li { margin: 0 0 14px; }
.rail a { display: block; text-decoration: none; }
.rail a:hover .rail-title { text-decoration: underline; text-decoration-color: var(--magenta); }
.rail .rail-n { font: 700 15px/1.3 var(--serif); color: var(--cyan-ink); margin-right: 6px; }
.rail .rail-title { font: 700 15px/1.3 var(--sans); }
.rail .meta { display: block; margin-left: 20px; }
.rail .meta.done { color: var(--ink); }
.rail li.next .rail-label { background: var(--marker); padding: 1px 4px; margin: 0 -4px; }
@media (max-width: 979px) { .rail ol { display: flex; flex-wrap: wrap; gap: 8px 22px; } .rail li { margin: 0; } }
.steps { display: grid; gap: 32px; }
.step { padding: 28px; scroll-margin-top: 16px; }
.step.current { border: 2px solid var(--ink); padding: 27px; }
.step-head { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; }
.step-num { font: 700 40px/1 var(--serif); color: var(--cyan-ink); }
h2.step-title { font: 700 21px/1.25 var(--serif); }
.step .stamp-row { margin: 8px 0 4px -3px; }
@media (max-width: 640px) { .step { padding: 18px; } .step.current { padding: 17px; } }
label.field { display: block; font: 700 14px/1.3 var(--sans); margin: 18px 0 6px; }
input[type=text], input[type=password], input[type=tel], textarea {
  width: 100%; max-width: 640px; background: var(--paper); border: 1.5px solid var(--ink-30); border-radius: 2px;
  padding: 10px 12px; font: 400 15px/1.4 var(--sans); color: var(--ink); }
textarea { max-width: 75ch; line-height: 1.6; min-height: 220px; resize: vertical; }
textarea.short { min-height: 120px; }
input:focus, textarea:focus { border-color: var(--ink); }
ul.checks { list-style: none; margin: 14px 0 0; padding: 0; font-size: 14px; }
ul.checks li { margin: 4px 0; }
ul.checks li.ok { color: var(--ink); }
ul.checks li.err { color: var(--magenta); }
.who { font: 700 17px/1.4 var(--serif); margin: 8px 0 12px; }
details > summary { cursor: pointer; }

/* feedback pages */
.panel { padding: 28px; margin: 0 0 8px; max-width: 820px; }
@media (max-width: 640px) { .panel { padding: 18px; } }
.round { max-width: 820px; }
.clippings { padding: 0 20px; }
.rank-item { display: grid; grid-template-columns: 64px minmax(0, 1fr); gap: 20px; padding: 18px 0; border-top: 1px solid var(--rule); }
.rank-item:first-child { border-top: 0; }
.rank-item select { appearance: none; -webkit-appearance: none; width: 56px; height: 56px; padding: 0;
  font: 700 28px/1 var(--serif); text-align: center; text-align-last: center; color: var(--ink);
  background: var(--paper); border: 2px solid var(--ink); border-radius: 0; cursor: pointer; }
.rank-item select:has(option[value=""]:checked) { border: 1.5px dashed var(--cyan-line); color: var(--cyan-ink); }
.rank-item.bad select { border: 2px solid var(--magenta); background: #FCE6F1; }
.clip-text { font: 400 16px/1.55 var(--serif); max-width: 75ch; }
.clip-text .meta { display: block; margin-bottom: 4px; }
.agree { display: flex; flex-wrap: wrap; align-items: center; gap: 12px 32px; margin: 8px 0 32px; }
.agree-fig { font: 700 72px/1 var(--serif); }
.verdict { font: 400 21px/1.35 var(--serif); margin: 0 0 6px; max-width: 34ch; }
.cmp { min-width: 720px; }
.cmp .place { font: 700 28px/1 var(--serif); }
.cmp .mplace { font: 700 21px/1 var(--serif); }
.cmp .mplace.diff { color: var(--magenta); }
.back { margin-top: 28px; }
"""

# One delegated listener: forms with data-busy turn their submit button into
# the "press is running" strip. The disabling is deferred one tick so the
# browser still serialises the submitter's own name/value into the request.
BUSY_SCRIPT = """
<script>
  document.addEventListener('submit', function (e) {
    var form = e.target;
    var msg = form.getAttribute && form.getAttribute('data-busy');
    if (!msg) return;
    var btn = e.submitter || form.querySelector('button[type=submit]');
    if (!btn) return;
    setTimeout(function () {
      btn.disabled = true;
      btn.classList.add('is-busy');
      btn.textContent = msg;
    }, 0);
  });
</script>
"""


def section_head(title: str, actions: str = "") -> str:
    """An h2 with the ink rule above it and optional right-aligned actions
    (already-built HTML). title is escaped here."""
    acts = f'<div class="sec-actions">{actions}</div>' if actions else ""
    return f'<div class="sec-head"><h2>{html.escape(title)}</h2>{acts}</div>'


def page(
    title: str,
    body: str,
    *,
    active: str | None = None,
    nav: bool = True,
    footer: bool = True,
) -> str:
    """A full console document. active is "runs" or "setup" (which nav link
    is current); nav=False hides the links; footer=False drops the footer
    (the shutdown page). title is escaped; body is trusted HTML."""
    def link(key: str, href: str, label: str) -> str:
        cur = ' aria-current="page"' if active == key else ""
        return f'<a href="{href}"{cur}>{label}</a>'

    nav_html = (
        f'<nav aria-label="Разделы">{link("runs", "/", "Прогоны")}{link("setup", "/setup", "Настройки")}</nav>'
        if nav
        else ""
    )
    foot_html = ""
    if footer:
        foot_html = (
            '<footer class="wrap foot">'
            f"<span>TG Newspaper {html.escape(app_version())}</span>"
            '<form method="post" action="/shutdown" '
            "onsubmit=\"return confirm('Выключить консоль? Запустить снова можно иконкой TG Newspaper.')\">"
            '<button type="submit">Выключить консоль</button></form></footer>'
        )
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TG Newspaper — {html.escape(title)}</title>
<style>
{FONT_FACES}
{STYLE}
</style>
</head>
<body>
<header class="plate"><div class="wrap plate-in"><a class="brand" href="/">DAILY TIMOHA</a>{nav_html}</div></header>
<main class="wrap">
{body}
</main>
{foot_html}
{BUSY_SCRIPT}
</body>
</html>"""
