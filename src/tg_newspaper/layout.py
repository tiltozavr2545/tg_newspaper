"""Вёрстка газетной полосы (Этап 3): HTML/CSS-шаблон + рендер в PNG через
headless Chromium (Playwright). На вход — уже отфильтрованный и
дедуплицированный список постов (результат Этапов 1-2), на выходе — одна
или несколько PNG (по одной на печатную полосу) ровно того размера, что
уйдёт на печать на Этапе 4, без пересчёта DPI на её стороне.

Разметка использует физические CSS-единицы (мм) для размера листа, полей и
шрифтов — они не зависят от того, с каким DPI сделан скриншот: 1мм в
шаблоне всегда 1мм на бумаге. DPI влияет только на плотность пикселей
итогового PNG (см. render_pages).

Вёрстка — мозаичная CSS-грид (не проточные column-count колонки): у каждой
новости своя "важность" (lead/feature/brief) по объёму текста, и
соответствующий размер плитки в сетке — вперемешку, а не ровными столбцами
(так строят настоящие газетные полосы: broken-column/mosaic layout, а не
единая колоночная лента). Ширина плитки определяет только типографику —
высота всегда считается по фактическому объёму текста конкретного поста
(_estimate_row_span), полный текст печатается всегда, ничего не обрезается
многоточием на месте. Пропорции и разбивку на уровни см. в _assign_tiers.

Если новостей за сутки набралось больше, чем помещается на одну полосу,
газета получает вторую, третью и так далее полосу — передовица и штамп
только на первой, дальше идут облегчённые "внутренние" полосы с
running-header, как в настоящей многостраничной газете. Разбивка на полосы
(render_pages, _fit_page) — не просто оценка по объёму текста: каждая
страница-кандидат реально рендерится в headless Chromium (тем же
device_scale_factor, что и финальный скриншот — иначе другое округление
ширины символов до физических пикселей может перенести строки иначе и
разойтись с тем, что было измерено), и то, что физически не влезло,
по-настоящему переносится на следующую полосу или, если статья не влезает
даже в одиночку на всю полосу, сначала расширяется во всю ширину сетки
(меньше нужных строк при более широкой колонке) — обрезка текста
исключена везде, кроме теоретического предела "не влезает даже одна на всю
полосу". Оценка по числу символов (_estimate_row_span) — только стартовое
приближение, чтобы не начинать цикл подгонки с одной новости за раз; за
корректность отвечает именно измерение в браузере, а не эта оценка.
"""

from __future__ import annotations

import html
import logging
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from .storage import Post

logger = logging.getLogger(__name__)

# Физические размеры листа в мм.
PAGE_SIZES_MM: dict[str, tuple[float, float]] = {
    "A4": (210.0, 297.0),
    "Letter": (215.9, 279.4),
}

# Эталонное соотношение CSS-единиц: спецификация CSS фиксирует 1in = 96px
# независимо от реального DPI устройства — используется только чтобы
# получить целочисленный размер вьюпорта в px под нужный физический размер.
CSS_PX_PER_MM = 96 / 25.4

DEFAULT_PAGE_SIZE = "A4"
DEFAULT_DPI = 300
DEFAULT_COLUMNS = 6  # число базовых колонок мозаичной сетки, не "столбцов текста"

MASTHEAD_TITLE = "ГАЗЕТА ДНЯ"
MASTHEAD_TAGLINE = "«Не всё, что скроллится, — новость» · тираж: 1 экземпляр"

ROW_UNIT_MM = 5.2  # высота одного "кирпичика" мозаичной сетки

# Геометрия страницы для разбивки на полосы (см. render_pages, _fit_page): сколько места
# в мм съедают несжимаемые части листа — большая шапка (только на первой
# полосе), облегчённая "продолжение" шапка (на второй и далее — так делают
# настоящие газеты: разворот с передовицей один, дальше просто внутренние
# полосы) и колофон, одинаковый на каждой полосе. Измерено на реальном
# рендере (заголовок + орнамент + дата + тэглайн + линейка ≈ 37мм).
PAGE_PADDING_TOP_MM = 10.0
PAGE_PADDING_BOTTOM_MM = 8.0
FULL_HEADER_BLOCK_MM = 37.0
RUNNING_HEADER_BLOCK_MM = 9.0
FOOTER_BLOCK_MM = 8.0

# Ширина плитки (col span) по уровню важности — только ширина колонки и
# типографика (кегль заголовка, буквица у lead), НЕ обрезка текста: полный
# текст поста печатается всегда (см. _estimate_row_span), лишнее просто
# уходит на следующую полосу через пагинацию (render_pages), а не отрезается
# многоточием на месте. "Разнобой" получается из ширины плитки и реального
# разброса длины новостей, а не из искусственного клампинга.
LEAD_COL_SPAN = 4
FEATURE_COL_SPANS = (3, 2)  # чередуются через одну плитку — сетка вразнобой
BRIEF_COL_SPAN = 2
FEATURE_SLOTS = 4  # столько новостей (после лида) получают ширину feature; остальные — brief

# Геометрия для оценки, сколько строк реально займёт текст поста в плитке
# заданной ширины — чтобы row span считался по факту объёма текста, а не по
# фиксированному диапазону. Не точная типографика (её даёт только сам
# браузер), а откалиброванная на живых рендерах оценка с запасом в большую
# сторону — недооценка означала бы обрезку текста через overflow:hidden
# страховки на .card, что хуже, чем чуть лишнего пустого места внизу плитки.
MOSAIC_GAP_MM = 0.5
CARD_PADDING_H_MM = 3.0
PAGE_SIDE_PADDING_MM = 9.0
PAGE_BORDER_MM = 0.5
CHAR_WIDTH_FACTOR = 0.5  # средняя ширина символа кириллического serif ≈ 0.5 кегля
ROW_SAFETY_FACTOR = 1.1  # небольшой запас поверх оценки по символам (переносы по словам режут строки чуть менее эффективно, чем голый подсчёт символов)
ROW_OVERHEAD = 2  # паддинги плитки + отступ под byline, в row-unit'ах

# Только для _seed_batch — стартовая прикидка, сколько новостей пробовать
# впихнуть в полосу за один заход, ДО реальной проверки в браузере
# (_fit_page). За корректность (гарантию, что текст не обрежется) отвечает
# именно измерение в headless Chromium, а не это число — оно лишь экономит
# число итераций цикла подгонки (без него пришлось бы начинать с одной
# новости за раз). Занижено умышленно: лучше на один лишний проход цикла
# больше, чем начинать с заведомо переполненной пробной полосы.
PAGE_BUDGET_SAFETY_FACTOR = 0.9

TIER_BODY_FONT_MM = {"lead": 3.4, "feature": 3.1, "brief": 2.9}
TIER_BODY_LINE_HEIGHT = 1.4  # соответствует line-height в CSS для .card p
TIER_HEADLINE_FONT_MM = {"lead": 7.2, "feature": 4.6, "brief": 3.7}
TIER_HEADLINE_LINE_HEIGHT = {"lead": 1.12, "feature": 1.18, "brief": 1.2}

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

  .masthead {{ position: relative; flex: 0 0 auto; text-align: center; margin-bottom: 3mm; }}
  .masthead .ornament {{ letter-spacing: 5mm; font-size: 4mm; color: #444; }}
  .masthead h1 {{
    margin: 1mm 0; font-size: 13mm; letter-spacing: 2mm;
    font-variant: small-caps; font-weight: 900;
  }}
  .masthead .date {{
    font-size: 3.4mm; text-transform: uppercase; letter-spacing: 1.5mm; color: #333;
  }}
  .masthead .tagline {{
    font-size: 2.6mm; font-style: italic; color: #666; margin-top: 1mm;
  }}

  /* Пасхалка-печать в углу полосы — тот самый "сделано Тимохой", только не
     плашкой в подвале, а как штамп типографии на настоящей газете. */
  .stamp {{
    position: absolute; top: -2mm; right: 0; width: 21mm; height: 21mm;
    border-radius: 50%; border: 0.4mm dashed #555; transform: rotate(-11deg);
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    font-size: 2.4mm; letter-spacing: 0.3mm; text-transform: uppercase;
    color: #555; text-align: center; line-height: 1.35;
  }}
  .stamp span {{ font-size: 4.4mm; margin-bottom: 0.4mm; }}

  /* Облегчённая шапка для второй и последующих полос — настоящий разворот
     с передовицей и штампом только один, дальше страницы идут как обычные
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
    grid-auto-flow: dense;
    gap: 0.5mm;
    background-color: #cfc7b8;
  }}

  .card {{
    position: relative;
    overflow: hidden;
    background-color: #fdfcf8;
    padding: 2.6mm 3mm;
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
  .card.tier-lead::after {{ font-size: 26mm; bottom: -5mm; }}
  .card.tier-feature::after {{ font-size: 14mm; bottom: -3mm; }}
  .card:nth-of-type(3n)::after {{ transform: rotate(-8deg); }}
  .card:nth-of-type(3n+1)::after {{ transform: rotate(6deg); }}
  .card:nth-of-type(3n+2)::after {{ transform: rotate(-2deg); }}
  .card-content {{ position: relative; z-index: 1; height: 100%; }}

  .byline {{
    font-size: 2.4mm; text-transform: uppercase; letter-spacing: 0.4mm;
    color: #777; margin-bottom: 1mm;
  }}
  .card p {{
    font-size: 3.1mm; line-height: 1.4; margin: 0 0 1.4mm;
    text-align: justify; hyphens: auto;
  }}

  .card.tier-lead h2 {{ font-size: 7.2mm; line-height: 1.12; margin: 0 0 2mm; }}
  .card.tier-lead {{ grid-column: span {lead_col}; }}
  .card.tier-lead p:first-of-type::first-letter {{
    float: left; font-size: 11mm; line-height: 9mm; padding: 1mm 1.2mm 0 0;
    font-weight: 900;
  }}

  .card.tier-feature h2 {{ font-size: 4.6mm; line-height: 1.18; margin: 0 0 1.4mm; }}

  .card.tier-brief h2 {{ font-size: 3.7mm; line-height: 1.2; margin: 0 0 1mm; }}
  .card.tier-brief p {{ font-size: 2.9mm; }}

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

# Ведущий **жирный** фрагмент поста — в реальных данных это почти всегда и
# есть лид-фраза/заголовок новости (Telethon отдаёт Message.text уже с
# markdown-разметкой entities по умолчанию). Ограничение длины — защита от
# случая без второго "**" в разумных пределах, когда .+? нежадно захватит
# слишком много текста. После закрывающих "**" опционально съедаем финальную
# пунктуацию заголовка (жирный текст. — рест) и тире-связку (жирный — рест)
# — иначе они попадают в начало тела как отдельный "абзац" из одной точки
# или, что хуже, как первый символ буквицы (см. build_article). Префикс
# исключает "[]()" — иначе на постах вида "[**Заголовок**](ссылка): текст"
# открывающая "[" сама сходит за "декоративный" префикс, паттерн находит
# "**" ВНУТРИ ссылки как лид, а "](ссылка)" остаётся в начале тела
# оборванным мусором; такие посты просто уходят в запасной путь ниже.
_LEADING_BOLD_RE = re.compile(
    r"^(?P<prefix>[^*\w\[\]()]*)\*\*(?P<headline>.+?)\*\*\s*"
    r"(?:[.!?…:;,]+\s*)?(?:[—–-]\s*)?",
    re.DOTALL,
)
_MAX_HEADLINE_MARKUP_LEN = 200


def _inline_markdown_to_html(escaped_text: str) -> str:
    """escaped_text уже прогнан через html.escape — тут только разметка
    Telegram-markdown (жирный/подчёркнутый/курсив/код/ссылки) в HTML-теги."""
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", escaped_text)  # ссылка -> её видимый текст, вести печатной странице некуда
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"<u>\1</u>", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"<em>\1</em>", text, flags=re.DOTALL)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    return text


# Многие каналы подписывают пост коротким "[Название канала](ссылка)" в
# конце — это кредит источнику, не часть новости, и в печатной колонке
# читается как оборванная фраза из двух слов сама по себе. Отрезаем только
# короткие такие абзацы (после разрешения markdown-ссылки в текст — не
# больше 4 "слов"), чтобы не задеть абзацы, где ссылка — только часть
# полноценного предложения.
_MAX_SIGNATURE_WORDS = 4


def _looks_like_signature(paragraph: str) -> bool:
    if "](" not in paragraph:
        return False
    resolved = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", paragraph)
    return len(resolved.split()) <= _MAX_SIGNATURE_WORDS


def _strip_signature_tail(paragraphs: list[str]) -> list[str]:
    parts = list(paragraphs)
    while len(parts) > 1 and _looks_like_signature(parts[-1]):
        parts.pop()
    return parts


def _paragraphs_html(text: str) -> str:
    parts = _strip_signature_tail(
        [p.strip() for p in re.split(r"\n{2,}", text.strip()) if p.strip()]
    )
    out = []
    for p in parts:
        inner = _inline_markdown_to_html(html.escape(p)).replace("\n", "<br>")
        out.append(f"<p>{inner}</p>")
    return "\n".join(out)


def _split_headline(raw_text: str) -> tuple[str, str]:
    """Возвращает (заголовок, тело) как исходный markdown-текст (без
    HTML-эскейпинга — им занимаются вызывающие функции)."""
    text = raw_text.strip()
    m = _LEADING_BOLD_RE.match(text)
    if m and len(m.group("headline")) <= _MAX_HEADLINE_MARKUP_LEN:
        prefix = m.group("prefix").strip()
        headline = m.group("headline").strip()
        if prefix:
            headline = f"{prefix} {headline}"  # эмодзи-префикс перед лидом (частый стиль каналов) — сохраняем в заголовке
        rest = text[m.end():].strip()
        return headline, rest or headline  # пустое тело после лида — дублируем, чтобы новость не осталась без текста
    # Без ведущего жирного лида берём только первую "строку" поста (до
    # пустой строки) — иначе при обрезке по числу слов заголовок утекает в
    # начало следующего абзаца и дублирует его (проверено на реальном посте
    # без лида, где так получалось "...в сольном фильме Конечно. Да,…").
    # Ссылки резолвим в видимый текст ДО обрезки по словам — иначе сырой URL
    # из "[текст](ссылка)" попадает в заголовок целиком как одно "слово"
    # (проверено на посте, начинавшемся с "[**Исследование**](https://…)").
    plain = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    plain = re.sub(r"[*_`]", "", plain)
    first_para = plain.split("\n\n", 1)[0].strip()

    if len(first_para) <= _MAX_HEADLINE_MARKUP_LEN:
        # Первый абзац сам по себе достаточно короткий, чтобы целиком стать
        # заголовком — тогда, как и в случае с жирным лидом, убираем его из
        # тела (граница "\n\n" в исходном markdown-тексте там же, где и в
        # plain — её резолвинг ссылок/убирание разметки не сдвигает: обе
        # регулярки трогают только символы внутри абзаца, не сам перенос).
        # Раньше тело оставалось целиком с этим же абзацем внутри — тот же
        # текст читался дважды подряд, один раз крупным заголовком, потом
        # ещё раз обычным шрифтом первой строкой.
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
    post: Post  # ссылка на исходный пост — чтобы вернуть вызывающему коду, что не поместилось (см. render_pages)
    headline_html: str
    full_body_html: str
    channel: str
    posted_at: datetime
    word_count: int  # объём текста поста — вторичный признак при разбивке на tier (после importance)
    headline_char_count: int  # для оценки высоты плитки (см. _estimate_row_span)
    body_char_count: int
    # Оценка значимости от LLM-классификатора (1-5, см. classifier.py) — 0,
    # если не передана явно (например, вызов без пайплайна). Определяет и
    # ширину плитки (_assign_tiers — важное крупнее), и порядок заполнения
    # полос (render_pages — важное раньше, значит с большей вероятностью
    # попадёт в номер при ограничении на число полос, см. build_newspaper в
    # pipeline.py).
    importance: int = 0


def build_article(post: Post, importance: int = 0) -> Article:
    headline_raw, body_raw = _split_headline(post.text)
    headline_html = _inline_markdown_to_html(html.escape(headline_raw))
    full_body_html = _paragraphs_html(body_raw)
    return Article(
        post=post,
        headline_html=headline_html,
        full_body_html=full_body_html,
        channel=post.channel,
        posted_at=post.posted_at,
        word_count=len(body_raw.split()),
        headline_char_count=len(headline_raw),
        body_char_count=len(body_raw),
        importance=importance,
    )


@dataclass(frozen=True)
class _Placement:
    tier: str  # "lead" | "feature" | "brief"
    col_span: int
    row_span: int


def _text_width_mm(col_span: int, columns: int, page_w: float) -> float:
    """Ширина, доступная под текст в плитке шириной col_span колонок из
    columns — минус поля страницы, зазоры мозаики и паддинг самой плитки."""
    content_width = page_w - 2 * PAGE_SIDE_PADDING_MM - 2 * PAGE_BORDER_MM
    col_width = (content_width - (columns - 1) * MOSAIC_GAP_MM) / columns
    span_width = col_width * col_span + (col_span - 1) * MOSAIC_GAP_MM
    return span_width - 2 * CARD_PADDING_H_MM


def _rows_for_text(char_count: int, text_width_mm: float, font_size_mm: float, line_height: float) -> int:
    if char_count <= 0:
        return 0
    chars_per_line = max(5, int(text_width_mm / (font_size_mm * CHAR_WIDTH_FACTOR)))
    lines = math.ceil(char_count / chars_per_line)
    return math.ceil(lines * font_size_mm * line_height * ROW_SAFETY_FACTOR / ROW_UNIT_MM)


def _estimate_row_span(tier: str, col_span: int, columns: int, page_w: float, article: Article) -> int:
    """Высота плитки — по факту того, сколько строк реально займут заголовок
    и полный текст поста на этой ширине, а не по фиксированному диапазону:
    текст никогда не обрезается, лишнее просто уезжает на следующую полосу
    (render_pages, _fit_page)."""
    text_width = _text_width_mm(col_span, columns, page_w)
    headline_rows = _rows_for_text(
        article.headline_char_count, text_width, TIER_HEADLINE_FONT_MM[tier], TIER_HEADLINE_LINE_HEIGHT[tier]
    )
    body_rows = _rows_for_text(
        article.body_char_count, text_width, TIER_BODY_FONT_MM[tier], TIER_BODY_LINE_HEIGHT
    )
    return ROW_OVERHEAD + headline_rows + body_rows


def _assign_tiers(
    articles: list[Article], columns: int, page_w: float, avail_rows_first: int
) -> dict[int, _Placement]:
    """Разбивает новости на уровни важности по LLM-оценке (importance,
    длина текста — только тай-брейк при равной оценке): одна широкая
    "передовица" (самая значимая новость дня, а не просто самый длинный
    пост), несколько плиток среднего размера, остальное — узкие плитки.
    Ширина плитки определяет только typографику и ширину колонки — высота
    везде считается по фактическому объёму текста (_estimate_row_span), так
    что полный текст поста печатается всегда. Вперемешку по ширине, а не
    ровными столбцами — то самое broken-column/mosaic-расположение
    настоящих газетных полос. Возвращает {id(article): _Placement}."""
    by_importance = sorted(articles, key=lambda a: (a.importance, a.word_count), reverse=True)

    placements: dict[int, _Placement] = {}
    if not by_importance:
        return placements

    lead, *rest = by_importance
    # Обычная ширина передовицы — LEAD_COL_SPAN, но если конкретный пост
    # настолько длинный, что даже на всю доступную высоту первой полосы не
    # уместится (реальный случай: пост на 2700 знаков — 47 row-unit'ов
    # против 44 доступных, последнее слово статьи обрезалось), расширяем
    # передовицу до полной ширины сетки — шире колонка, короче строка нужна
    # на единицу текста, ниже требуемая высота. Обрезка текста тут
    # недопустима (см. модуль-докстринг), а перенос лида на вторую полосу
    # выглядел бы гораздо страннее, чем более широкая передовица. Прыгаем
    # сразу на полную ширину, а не по одной колонке за шаг: промежуточная
    # ширина (например, 5 из 6) оставляет соседний столбец шириной в 1
    # колонку — туда не влезает уже ни одна brief-плитка (все они шириной 2),
    # и он просто пропадает пустой полосой сбоку от передовицы.
    lead_col = LEAD_COL_SPAN
    lead_row = _estimate_row_span("lead", lead_col, columns, page_w, lead)
    if lead_row > avail_rows_first:
        lead_col = columns
        lead_row = _estimate_row_span("lead", lead_col, columns, page_w, lead)
    placements[id(lead)] = _Placement("lead", lead_col, min(lead_row, avail_rows_first))

    feature_pool, rest = rest[:FEATURE_SLOTS], rest[FEATURE_SLOTS:]
    for i, a in enumerate(feature_pool):
        col = FEATURE_COL_SPANS[i % len(FEATURE_COL_SPANS)]
        placements[id(a)] = _Placement("feature", col, _estimate_row_span("feature", col, columns, page_w, a))

    for a in rest:
        placements[id(a)] = _Placement(
            "brief", BRIEF_COL_SPAN, _estimate_row_span("brief", BRIEF_COL_SPAN, columns, page_w, a)
        )

    return placements


def _article_html(article: Article, placement: _Placement) -> str:
    when = article.posted_at.astimezone().strftime("%H:%M")
    css_class = f"card tier-{placement.tier}"
    style = f'style="grid-column: span {placement.col_span}; grid-row: span {placement.row_span};"'

    return f"""
    <div class="{css_class}" {style}>
      <div class="card-content">
        <div class="byline">{html.escape(article.channel)} · {when}</div>
        <h2>{article.headline_html}</h2>
        {article.full_body_html}
      </div>
    </div>"""


def _seed_batch(
    remaining: list[Article], placements: dict[int, _Placement], columns: int, avail_rows: int
) -> int:
    """Сколько новостей из начала remaining взять как первую попытку для
    страницы — по бюджету "ячеек" (доступные строки × колонки), с запасом
    (PAGE_BUDGET_SAFETY_FACTOR). Это только стартовая эвристика, чтобы не
    гонять цикл проверки в браузере (_fit_page) от одной новости за раз —
    настоящая проверка, влезло ли реально, всегда происходит в браузере."""
    budget = avail_rows * columns * PAGE_BUDGET_SAFETY_FACTOR
    used = 0
    count = 0
    for a in remaining:
        p = placements[id(a)]
        cost = p.col_span * p.row_span
        if count and used + cost > budget:
            break
        used += cost
        count += 1
    return max(count, 1)


# Два независимых вида переполнения, и лечатся они по-разному:
# - boxOverflow: сама плитка (грид-ячейка) вылезает за нижний край .mosaic —
#   значит на полосе тупо не хватило места, статью нужно переносить на
#   следующую (см. _fit_page).
# - contentOverflow: плитка помещается на полосе, но её СОБСТВЕННЫЙ текст не
#   влезает в отведённую ей высоту (card.scrollHeight > card.clientHeight —
#   scrollHeight видит реальный контент даже под overflow:hidden). Перенос
#   такой статьи на другую полосу ничего не даст — у неё та же оценка
#   row_span, тот же результат на любой полосе; нужно увеличить именно её
#   row_span (см. _fit_page) и переизмерить.
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


def _fit_page(
    probe_page: Page,
    candidates: list[Article],
    placements: dict[int, _Placement],
    row_unit_px: float,
    columns: int,
    page_w: float,
    build_probe_html: Callable[[list[Article]], str],
) -> tuple[list[Article], list[Article]]:
    """Реально рендерит кандидатов в headless-браузере и смотрит, что не
    влезло — это не оценка, а измерение в настоящем layout-движке Chromium,
    единственный надёжный способ гарантировать, что текст никогда не
    обрежется overflow:hidden на .card/.mosaic втихую (см. модуль-
    докстринг). Различает два вида переполнения (см. _OVERFLOW_CHECK_JS):

    - Текст не влезает в СВОЮ ЖЕ плитку (card.scrollHeight > clientHeight) —
      значит оценка _estimate_row_span для этой статьи была занижена.
      Перенос на другую полосу тут не поможет: row_span у статьи не
      зависит от полосы, на любой странице получится то же самое. Вместо
      этого увеличиваем её row_span прямо в общем placements (см.
      render_pages — тот же словарь используется при финальной сборке) и
      перерисовываем эту же полосу заново.
    - Сама плитка (грид-ячейка) вылезает за нижний край .mosaic — вот это
      уже реальная нехватка места на полосе, такие статьи переходят на
      следующую (после того как переполнений по первому пункту не осталось
      — иначе появление лишней высоты у одной плитки может ВНОВЬ вытолкнуть
      другую за край, поэтому первый вид переполнения всегда лечится
      целиком, прежде чем разбираться со вторым).

    Возвращает (влезло на этой полосе, не влезло — на следующую)."""
    queue = list(candidates)
    overflow_accum: list[Article] = []
    iteration = 0
    while queue:
        iteration += 1
        probe_page.set_content(build_probe_html(queue), wait_until="load")
        result = probe_page.eval_on_selector(".mosaic", _OVERFLOW_CHECK_JS) or {}
        content_overflow = result.get("contentOverflow") or []
        box_overflow = set(result.get("boxOverflow") or [])
        logger.debug(
            "_fit_page: попытка %d, кандидатов=%d, тесно_в_своей_плитке=%d, не влезло_на_полосу=%d",
            iteration, len(queue), len(content_overflow), len(box_overflow),
        )

        if content_overflow:
            for index, shortfall_px in content_overflow:
                a = queue[index]
                extra_rows = math.ceil(shortfall_px / row_unit_px)
                old = placements[id(a)]
                placements[id(a)] = replace(old, row_span=old.row_span + extra_rows)
            continue  # тот же queue, но с исправленными row_span — перерисовываем

        if not box_overflow:
            return queue, overflow_accum

        fitted = [a for i, a in enumerate(queue) if i not in box_overflow]
        newly_bad = [a for i, a in enumerate(queue) if i in box_overflow]
        if not fitted:
            if len(queue) == 1:
                # Единственная статья на всю полосу, и даже так не влезает.
                # Прежде чем сдаваться, пробуем то же, что и для лида
                # (_assign_tiers): расширить её на всю ширину сетки — шире
                # колонка, короче строка, меньше нужных row-unit'ов. Пока
                # есть куда расширяться — увеличиваем col_span и
                # перевычисляем row_span под новую ширину, перерисовываем.
                a = queue[0]
                old = placements[id(a)]
                if old.col_span < columns:
                    new_col = columns
                    new_row = _estimate_row_span(old.tier, new_col, columns, page_w, a)
                    placements[id(a)] = replace(old, col_span=new_col, row_span=new_row)
                    continue
                # Дальше расширять некуда (уже во всю ширину) — совсем
                # некуда сжимать (редчайший случай, аномально длинный
                # пост). Отдаём как есть: страховка .card{overflow:hidden}
                # не даст ей наехать на соседей (их тут и нет), но её
                # собственный текст может обрезаться — больше сжимать нечем.
                return queue, overflow_accum
            # Раньше здесь сразу отдавался queue[:1] как "влезло" без
            # проверки — а он мог не влезть и в одиночку (переполнение было
            # именно от соседства с другими, не от него самого). Баг нашёлся
            # не на этапе подгонки, а только в финальном документе: empty
            # (влезло) отчитывалось, а собранная полоса реально обрезала
            # текст. Теперь вместо мгновенного возврата сокращаем queue до
            # первой статьи и ПЕРЕПРОВЕРЯЕМ её в изоляции на следующем
            # круге — остальные уходят в overflow.
            overflow_accum = queue[1:] + overflow_accum
            queue = queue[:1]
            continue
        overflow_accum = newly_bad + overflow_accum
        queue = fitted
    return queue, overflow_accum


def _fill_page(
    probe_page: Page,
    remaining: list[Article],
    placements: dict[int, _Placement],
    row_unit_px: float,
    columns: int,
    page_w: float,
    avail_rows: int,
    build_probe_html: Callable[[list[Article]], str],
) -> tuple[list[Article], list[Article]]:
    """Наполняет одну полосу, не останавливаясь на первой удачной, но
    осторожной оценке (_seed_batch). Пример реальной проблемы, которую это
    чинит: самая значимая новость дня (передовица по importance) оказалась
    короткой — её реальная высота небольшая, а _seed_batch, посчитав по ней
    бюджет "ячеек" на глаз, решил, что вторая взятая следом новость уже не
    влезет, и остановился на партии из 1-2 статей, оставив почти всю
    альбомную полосу пустой, хотя место реально было. При фиксированном
    числе полос (Этап 3 — печатный номер, не бесконечная лента) пустовать
    целой полосе — намного хуже, чем ошибиться в размере партии.

    После стартовой партии пробуем добавить остаток ПО ОДНОЙ статье — и,
    важно, не останавливаемся на первой же, которая не влезла: она просто
    откладывается (skipped), а дальше пробуются следующие. Иначе одна
    крупная статья сразу после лида блокировала бы место для более мелких,
    которые прекрасно поместились бы в тот же остаток (реальный случай на
    живых данных — вторая по важности статья не влезала, и вся полоса, кроме
    лида, оставалась пустой, хотя дальше по очереди были статьи заметно
    меньше)."""
    seed_n = _seed_batch(remaining, placements, columns, avail_rows)
    current, rest = remaining[:seed_n], remaining[seed_n:]
    fitted, overflow = _fit_page(probe_page, current, placements, row_unit_px, columns, page_w, build_probe_html)
    current = fitted
    pending = overflow + rest

    skipped: list[Article] = []
    for candidate in pending:
        trial_fitted, trial_overflow = _fit_page(
            probe_page, current + [candidate], placements, row_unit_px, columns, page_w, build_probe_html
        )
        if trial_overflow:
            skipped.append(candidate)
            continue
        current = trial_fitted
    return current, skipped


def _page_html(
    page_articles: list[Article],
    placements: dict[int, _Placement],
    page_num: int,
    total_pages: int,
    date_label: str,
) -> str:
    cards_html = "\n".join(_article_html(a, placements[id(a)]) for a in page_articles)

    if page_num == 1:
        header_html = f"""
    <div class="masthead">
      <div class="stamp"><span>&#9733;</span>Тимоха<br>Пресс</div>
      <div class="ornament">❧ ⁘ ✦ ⁘ ❧</div>
      <h1>{MASTHEAD_TITLE}</h1>
      <div class="date">{date_label}</div>
      <div class="tagline">{MASTHEAD_TAGLINE}</div>
    </div>
    <div class="rule-double"></div>"""
    else:
        header_html = f"""
    <div class="running-header">
      <strong>{MASTHEAD_TITLE}</strong> · {date_label} · стр. {page_num} из {total_pages}
    </div>"""

    footer_suffix = f" · стр. {page_num}/{total_pages}" if total_pages > 1 else ""

    return f"""
  <div class="page" data-page="{page_num}">
    {header_html}
    <div class="mosaic">
      {cards_html}
    </div>
    <div class="colophon">TG Newspaper · собрано и свёрстано агентом Тимохи{footer_suffix}</div>
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
) -> tuple[list[Path], list[Post]]:
    """Рендерит газету в одну или несколько PNG нужного физического размера
    при заданном DPI — по файлу на полосу (`<basename>_1.png`,
    `<basename>_2.png`, ...). Вьюпорт браузера выставляется в CSS-пикселях
    под точный размер страницы, а device_scale_factor = dpi/96 отвечает за
    плотность пикселей скриншота — итоговые PNG ложатся на лист 1:1, без
    пересчёта на Этапе 4.

    Разбивка на страницы — не просто оценка по объёму текста: каждая
    страница-кандидат реально рендерится в headless Chromium, и то, что не
    влезло (см. _fit_page), по-настоящему переносится на следующую полосу.
    Иначе (доверять только оценке по числу символов) — на практике
    случается недооценка на конкретных постах, и текст обрезается
    overflow:hidden молча, что ровно то, чего вся эта многостраничность
    должна избегать (проверено на реальных данных — без этой проверки
    несколько статей теряли последние строки).

    max_pages — жёсткий потолок числа полос (Этап 3, по итогам ревью
    пользователя: печатная газета — это фиксированный номер, не бесконечная
    лента). Если после max_pages полос ещё что-то осталось, оно НЕ рендерится
    вообще — возвращается вторым элементом кортежа как список Post, которые
    не поместились, чтобы вызывающий код (см. build_newspaper в pipeline.py)
    решил, что с ними делать: сократить нейронкой и попробовать снова или
    выбросить из номера как недостаточно важные. importance_by_key
    определяет и то, что попадёт в номер раньше (см. ordered ниже), и ширину
    плитки каждой новости (_assign_tiers) — самая значимая становится
    передовицей, а не просто самая длинная."""
    run_date = run_date or datetime.now()
    page_w, page_h = PAGE_SIZES_MM[page_size]
    if landscape:
        page_w, page_h = page_h, page_w
    importance_by_key = importance_by_key or {}
    articles = [
        build_article(p, importance=importance_by_key.get((p.channel, p.message_id), 0))
        for p in posts
    ]
    if not articles:
        return [], []

    avail_rows_first = int(
        (page_h - PAGE_PADDING_TOP_MM - PAGE_PADDING_BOTTOM_MM - FULL_HEADER_BLOCK_MM - FOOTER_BLOCK_MM)
        // ROW_UNIT_MM
    )
    avail_rows_rest = int(
        (page_h - PAGE_PADDING_TOP_MM - PAGE_PADDING_BOTTOM_MM - RUNNING_HEADER_BLOCK_MM - FOOTER_BLOCK_MM)
        // ROW_UNIT_MM
    )

    placements = _assign_tiers(articles, columns, page_w, avail_rows_first)

    # Порядок плиток: передовица первой (самая значимая по LLM-оценке, а
    # при равной оценке — самая длинная, см. _assign_tiers), остальное — по
    # убыванию важности (не по хронологии): при ограничении на число полос
    # именно порядок этого списка решает, что попадёт в номер, если места
    # на всех не хватит, — важное должно оказаться раньше в очереди.
    lead = next(a for a in articles if placements[id(a)].tier == "lead")
    ordered = [lead] + sorted(
        (a for a in articles if a is not lead),
        key=lambda a: (-a.importance, a.posted_at),
    )

    style = _STYLE_TEMPLATE.format(
        page_w=page_w,
        page_h=page_h,
        pad_top=PAGE_PADDING_TOP_MM,
        pad_bottom=PAGE_PADDING_BOTTOM_MM,
        columns=columns,
        row_unit=ROW_UNIT_MM,
        lead_col=LEAD_COL_SPAN,
    )
    date_label = run_date.strftime("%d.%m.%Y")

    def wrap(fragment: str) -> str:
        return f'<!doctype html><html lang="ru"><head><meta charset="utf-8">{style}</head><body>{fragment}</body></html>'

    viewport_w = round(page_w * CSS_PX_PER_MM)
    viewport_h = round(page_h * CSS_PX_PER_MM)
    scale = dpi / 96
    row_unit_px = ROW_UNIT_MM * CSS_PX_PER_MM  # в CSS-пикселях — тех же единицах, что getBoundingClientRect/scrollHeight

    out_dir.mkdir(parents=True, exist_ok=True)
    out_paths: list[Path] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            # Подгонка (_fit_page) рендерится в том же контексте (тот же
            # device_scale_factor), что и финальный скриншот, — иначе при
            # другом масштабе растеризации текст может перенестись по
            # строкам чуть иначе (округление ширины символов до физических
            # пикселей отличается), и полоса, которая "влезла" на пробном
            # рендере при масштабе 1, реально обрежется на финальном при
            # масштабе 3+ (проверено на реальных данных — расхождение было
            # именно в один перенос строки на статью).
            context = browser.new_context(
                viewport={"width": viewport_w, "height": viewport_h},
                device_scale_factor=scale,
            )
            probe_page = context.new_page()

            pages: list[list[Article]] = []
            remaining = ordered
            while remaining:
                is_first = not pages
                avail_rows = avail_rows_first if is_first else avail_rows_rest
                probe_page_num = 1 if is_first else 2

                def build_probe(queue: list[Article], page_num: int = probe_page_num) -> str:
                    # page_num здесь только выбирает вид шапки (полная на
                    # первой полосе / облегчённая на остальных, см.
                    # _page_html) — на реальную высоту .mosaic, которую мы
                    # измеряем в _fit_page, это и должно влиять.
                    return wrap(_page_html(queue, placements, page_num, page_num, date_label))

                candidates_count = len(remaining)
                fitted, remaining = _fill_page(
                    probe_page, remaining, placements, row_unit_px, columns, page_w, avail_rows, build_probe
                )
                pages.append(fitted)
                logger.info(
                    "полоса %d: в очереди было=%d влезло=%d осталось_в_очереди=%d",
                    len(pages), candidates_count, len(fitted), len(remaining),
                )
                if max_pages is not None and len(pages) >= max_pages:
                    break

            # Если вышли по max_pages, а не по опустевшей очереди — то, что
            # осталось, в номер не попадает вообще (см. докстринг). Отдаём
            # исходные Post вызывающему коду вместо рендера лишних полос.
            leftover_posts = [a.post for a in remaining]

            total_pages = len(pages)
            pages_html = "\n".join(
                _page_html(page_articles, placements, i + 1, total_pages, date_label)
                for i, page_articles in enumerate(pages)
            )
            final_page = context.new_page()
            final_page.set_content(wrap(pages_html), wait_until="load")
            locator = final_page.locator(".page")
            for i in range(locator.count()):
                out_path = out_dir / f"{basename}_{i + 1}.png"
                locator.nth(i).screenshot(path=str(out_path))
                out_paths.append(out_path)
        finally:
            browser.close()
    return out_paths, leftover_posts
