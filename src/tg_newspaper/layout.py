"""Вёрстка газетной полосы (Этап 3): HTML/CSS-шаблон + рендер в PNG через
headless Chromium (Playwright). На вход — уже отфильтрованный и
дедуплицированный список постов (результат Этапов 1-2), на выходе — одна
или несколько PNG (по одной на печатную полосу) ровно того размера, что
уйдёт на печать на Этапе 4, без пересчёта DPI на её стороне.

Разметка использует физические CSS-единицы (мм) для размера листа, полей и
шрифтов — они не зависят от того, с каким DPI сделан скриншот: 1мм в
шаблоне всегда 1мм на бумаге. DPI влияет только на плотность пикселей
итогового PNG (см. render_pages).

Мозаика использует CSS-грид с двумя стандартными ширинами плитки —
feature/brief по LLM-оценке важности (см. _build_bands; самая значимая
новость номера, лид, в мозаику не входит вообще — см. ниже), сложенными в
ряды (_Band), которые всегда в сумме дают ровно ширину полосы (columns):
пары feature шириной в половину полосы, тройки brief шириной в треть
полосы. Это не проточные column-count колонки и не
свободная упаковка "как получится" (первая версия Этапа 3 использовала
grid-auto-flow:dense со свободной высотой каждой плитки по отдельности —
из-за разной длины текста соседние колонки расходились по высоте, и в
сетке появлялись беспорядочные серые провалы посреди полосы, а не только
внизу). Ряд — атомарная единица разбивки на полосы: все плитки одного ряда
получают ОДНУ и ту же высоту (по самой длинной статье в ряду), поэтому у
соседних плиток низ всегда совпадает, а более короткие статьи просто
получают немного пустого места внутри СВОЕЙ собственной белой плитки, а не
общий серый провал сквозь фон .mosaic. Неполный ряд в конце очереди (не
хватило статей набрать ряд целиком) не оставляет дыру, а растягивает
оставшиеся плитки на всю ширину полосы (см. _group_into_bands) — поэтому
пустое место в принципе может появиться только в самом низу последней
полосы номера, если статей не хватило заполнить её целиком. Ширина плитки
определяет только типографику — высота всегда считается по фактическому
объёму текста самой длинной статьи ряда (_estimate_row_span), полный текст
печатается всегда, ничего не обрезается многоточием на месте. Пропорции и
разбивку на уровни см. в _build_bands.

Первый склеенный лист номера (SHEET_UNITS полос) — титульная полоса-АФИША
(_cover_page_html), стилизованная по обложке Daily Bugle: название белым по
чёрной плашке с медальоном-горном, рамки-анонсы над ней, гигантский
заголовок-крик, кадр с диагональной лентой и инверсная полоса внизу. Афиша
НЕ печатает ни одной статьи — только зазывает внутрь номера крикливыми
анонсами (их для 4 главных новостей делает LLM, см. classifier.teasers), а
все новости целиком, включая самую важную, печатаются в мозаике на
следующих листах. Поэтому афиша ничего не изымает из номера и идёт СВЕРХ
отведённого мозаике max_pages (решение пользователя от 2026-09-27:
"добавим два [листа], которые образуют одну большую"). Большой мастхед —
эксклюзивно на афише; все полосы мозаики идут с одинаковой облегчённой
"внутренней" шапкой (running-header), даже самая первая.

Если новостей за сутки набралось больше, чем помещается на одну полосу
мозаики, газета получает вторую, третью и так далее полосу — все с той же
running-header, как внутренние полосы настоящей многостраничной газеты.
Разбивка на полосы
(render_pages, _fit_bands) — не просто оценка по объёму текста: каждая
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

# Название — по-английски и из двух слов специально (решение пользователя от
# 2026-10-06): на плашке мастхеда они расходятся по краям, а между ними
# встаёт медальон с горном — ровно композиция референса (DAILY [горн] BUGLE).
MASTHEAD_LEFT = "DAILY"
MASTHEAD_RIGHT = "TIMOHA"
MASTHEAD_TITLE = f"{MASTHEAD_LEFT} {MASTHEAD_RIGHT}"  # одной строкой — для running-header полос мозаики
MASTHEAD_EDITION = "Morning edition · Тимоха Пресс"
MASTHEAD_PRICE = "Тираж: 1 экз."
COVER_RIBBON_TEXT = "Спецвыпуск"  # диагональная лента на кадре главного анонса

# Минимальная оценка photo_relevance (см. classifier.py), при которой пост
# вообще получает фото — 1 ("не нужно") фото не показывает, даже если оно
# скачалось. Общий порог и для мозаичных плиток (feature/brief, см.
# _should_show_photo), и для кадров анонсов на титульной полосе-афише
# (см. _announce) — там тиров нет вообще, решает только эта оценка.
MIN_PHOTO_RELEVANCE_TO_SHOW = 2
PHOTO_ASPECT_RATIO = 3 / 2  # ширина:высота — тот же кадр, что согласован в мокапе оформления
PHOTO_ASPECT_CSS = "3 / 2"  # то же соотношение в синтаксисе CSS aspect-ratio
PHOTO_CAPTION_ROWS = 1  # под подпись-титр под фото хватает одной row-unit'ы

ROW_UNIT_MM = 5.2  # высота одного "кирпичика" мозаичной сетки

# Геометрия страницы для разбивки на полосы (см. render_pages, _fit_bands): сколько места
# в мм съедают несжимаемые части листа — облегчённая шапка running-header
# (мозаика вся идёт с ней теперь, большой мастхед и штамп — эксклюзивно на
# титульной полосе-обложке, см. _cover_page_html, для которой пагинация
# вообще не считается: она всегда ровно один лист без переполнения) и
# колофон, одинаковый на каждой полосе.
PAGE_PADDING_TOP_MM = 10.0
PAGE_PADDING_BOTTOM_MM = 8.0
RUNNING_HEADER_BLOCK_MM = 9.0
FOOTER_BLOCK_MM = 8.0

# Высота всей шапки титульной полосы-афиши (рамки-анонсы + чёрная плашка
# мастхеда + строка выходных данных) — нужна, чтобы отмерить главному
# анонсу ровно остаток ПЕРВОЙ физической полосы склеенного листа (см.
# render_pages): тогда линия разреза на два A4 проходит между блоками
# афиши, а не посреди крупного заголовка. Измерено на реальном рендере.
COVER_HEADER_BLOCK_MM = 51.0

# Какая доля рабочей высоты склеенного листа отходит главному анонсу
# (крик + подводка + кадр), остальное делят ряды вторичных и третичных
# анонсов с полосами. 0.58 — по ревью пользователя от 2026-10-09.
COVER_MAIN_BLOCK_SHARE = 0.58

# Сколько альбомных полос печатается на физическом принтере как один
# непрерывный лист (см. render_pages): при landscape=True раскладка и рендер
# ведутся сразу на весь склеенный лист (одна шапка, один колофон, одна
# сплошная мозаика — переход между "полосами" внутри листа НЕ виден, в
# отличие от перехода между отдельными листами), а нарезка на отдельные
# альбомные PNG для печати на обычном A4-принтере происходит уже после
# рендера, простым разрезанием готового изображения пополам (см. конец
# render_pages) — по пожеланию пользователя: "чтобы выглядело как одна
# страница с ровным переходом, но отдавать как картинки горизонтальные".
SHEET_UNITS = 2

# Ширина мозаичной плитки (col span) по уровню важности — только ширина
# колонки и типографика (кегль заголовка), НЕ обрезка текста: полный текст
# поста печатается всегда (см. _estimate_row_span), лишнее просто уходит на
# следующую полосу через пагинацию (render_pages), а не отрезается
# многоточием на месте. Три уровня: самый важный пост становится lead на
# всю ширину полосы, следующие FEATURE_SLOTS по важности — feature в
# половину ширины (columns // 2), всё остальное — brief в треть ширины
# (columns // 3). Титульная полоса-афиша (_cover_page_html) в этой системе
# не участвует вообще: она не печатает статей, только анонсирует их, и те
# же новости выходят внутри номера обычными плитками;
# ширины считаются от переданного в render_pages columns, а не от
# фиксированной константы (см. _group_into_bands) — поэтому columns должен
# быть кратен 6, иначе feature/brief перестанут делиться на columns без
# остатка и ряды начнут растягиваться чаще, чем нужно.
FEATURE_SLOTS = 4  # сколько постов после лида получают feature — чётное число, чтобы они всегда складывались в ряды по 2 без остатка на стыке с уровнем brief (см. _build_bands)

# Сколько новостей и куда попадает на титульную полосу-афишу (см.
# _cover_page_html). Все они берутся из РЕАЛЬНО напечатанных в номере
# новостей, в порядке значимости (_priority_order) — афиша только
# анонсирует то, что внутри, сама статей не печатает. Без номеров страниц
# (решение пользователя): точный номер потребовал бы такого же браузерного
# измерения, как основной текст, а анонс — декоративный элемент.
COVER_SECONDARY_COUNT = 3  # вторичные анонсы в ряд под главным
# Третий ряд — колонки одними заголовками, без подводок и кадров. Нужен,
# чтобы низ склеенного листа не пустовал: вторичных анонсов всего три, и
# на вторую физическую полосу (вдвое больше обычной) их не хватает —
# правка по ревью пользователя от 2026-10-09 ("много места отведено
# конечно во второй половине всего трём заголовкам").
COVER_TERTIARY_COUNT = 4
TEASER_TOP_COUNT = 2  # рамки-анонсы над плашкой мастхеда
TEASER_BOTTOM_COUNT = 3  # пункты в нижней инверсной полосе
# Для скольких новостей просим у LLM крикливые анонсы (screamer + подводка,
# см. classifier.teasers): главный плюс вторичные — остальным хватает
# обычного заголовка мелким кеглем.
COVER_LLM_TEASER_COUNT = 1 + COVER_SECONDARY_COUNT
# Сколько заголовков показать столбцом на месте кадра, если у главного
# анонса фото нет (см. _cover_list_html) — чтобы полоса не зияла пустотой.
COVER_FALLBACK_LIST_COUNT = 5

# Бюджет по СИМВОЛАМ, не по словам (см. _excerpt) — кириллица легко даёт
# компаунд-слова на 20+ букв ("низкоквалифицированной"), и обрезка по числу
# слов на них не защищает от переполнения плашки/строки screamer'а
# (проверено на реальных данных прогона #1 — 5-словный лимит дал 4-строчный
# screamer с некрасивым переносом внутри слова). Эти лимиты работают и как
# страховка на ответ LLM: модель просят дать 1-3 слова, но если она
# расщедрится — кегль в палец высотой этого не простит.
TEASER_TOP_MAX_CHARS = 70
TEASER_BOTTOM_MAX_CHARS = 50
SCREAMER_MAX_CHARS = 26
SECONDARY_SCREAMER_MAX_CHARS = 22
COVER_LIST_MAX_CHARS = 55  # пункт столбца-заполнителя: максимум две строки крупным кеглем
KICKER_MAX_CHARS = 85

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

# Только для _seed_bands — стартовая прикидка, сколько рядов пробовать
# впихнуть в полосу за один заход, ДО реальной проверки в браузере
# (_fit_bands). За корректность (гарантию, что текст не обрежется) отвечает
# именно измерение в headless Chromium, а не это число — оно лишь экономит
# число итераций цикла подгонки (без него пришлось бы начинать с одной
# новости за раз). Занижено умышленно: лучше на один лишний проход цикла
# больше, чем начинать с заведомо переполненной пробной полосы.
PAGE_BUDGET_SAFETY_FACTOR = 0.9

TIER_BODY_FONT_MM = {"lead": 3.2, "feature": 3.05, "brief": 2.9}
TIER_BODY_LINE_HEIGHT = 1.4  # соответствует line-height в CSS для .card p
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
    font-weight: 900; line-height: 0.92; letter-spacing: 0.5mm;
    text-transform: uppercase; margin: 0 0 1mm;
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
    font-size: 8.5mm; line-height: 1.1; text-transform: uppercase;
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
    font-size: 13mm; line-height: 1.02; margin: 0 0 2mm;
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


def _strip_markdown(text: str) -> str:
    """Ссылка "[текст](url)" -> видимый текст, "*_`" сняты — читаемый
    plain-текст, НЕ HTML (экранирование — забота вызывающего кода в месте
    вставки). Общий шаг для запасного пути _split_headline и для коротких
    анонсов _excerpt (тизеры/screamer на титульной полосе)."""
    plain = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return re.sub(r"[*_`]", "", plain)


def _excerpt(headline_raw: str, max_chars: int) -> str:
    """Короткий текстовый анонс для тизеров/screamer на титульной полосе
    (см. _cover_page_html) — снимает markdown (_strip_markdown) и обрезает
    по СИМВОЛАМ (не по числу слов, как заголовок в _split_headline):
    кириллица легко даёт компаунд-слова на 20+ букв, и лимит по словам не
    защищает от переполнения короткой плашки/гигантского screamer'а
    (проверено на реальных данных — 5 "слов" дали 4-строчный screamer с
    переносом внутри слова). Набирает целые слова, пока следующее слово не
    вылезло бы за max_chars; если не влезло даже первое слово — обрезает
    само слово (редкий случай очень длинного первого слова)."""
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
    plain = _strip_markdown(text)
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
    # Заголовок до HTML-эскейпинга и inline-markdown-конвертации (headline_html
    # — уже готовый для вставки HTML) — нужен отдельно для коротких plain-text
    # анонсов на титульной полосе-афише (_excerpt: рамки-анонсы, нижняя
    # полоса и запасной крик, если LLM-анонса для новости не нашлось, см.
    # _announce), где markdown-разметку нужно снять, а не превратить в теги.
    headline_raw: str
    headline_html: str
    full_body_html: str
    channel: str
    posted_at: datetime
    word_count: int  # объём текста поста — вторичный признак при разбивке на tier (после importance)
    headline_char_count: int  # для оценки высоты плитки (см. _estimate_row_span)
    body_char_count: int
    # Оценка значимости от LLM-классификатора (1-5, см. classifier.py) — 0,
    # если не передана явно (например, вызов без пайплайна). Определяет и
    # ширину плитки (_build_bands — важное крупнее), и порядок заполнения
    # полос (render_pages — важное раньше, значит с большей вероятностью
    # попадёт в номер при ограничении на число полос, см. build_newspaper в
    # pipeline.py).
    importance: int = 0
    # Оценка LLM (см. classifier.py), насколько посту помогло бы фото —
    # определяет, показывать ли photo_data_uri вообще (см. _should_show_photo),
    # не только его наличие: 1 ("не нужно") фото не ставит, даже если оно
    # скачалось и лежит на диске.
    photo_relevance: int = 1
    # data:-URI уже прочитанного и base64-закодированного файла фото, либо
    # None — нет фото/не скачалось/файл потерялся с диска. Кодируем один раз
    # тут, а не в шаблоне: _fit_bands перерисовывает HTML полосы много раз за
    # проход подгонки (см. render_pages), а сам файл фото за это время не
    # меняется. data:-URI, а не file://-путь — Chromium в контексте, куда
    # содержимое загружено через set_content (не через реальную навигацию по
    # file://), может отказаться грузить локальный файл как сторонний ресурс.
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
    headline_raw, body_raw = _split_headline(post.text)
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
    """Фото — у feature и brief (у каждого поста, независимо от ширины
    плитки, может быть фото), но не у lead-плитки (решение пользователя от
    2026-09-27: единственная крупная плитка номера держится на чистом
    тексте и типографике — заголовок, буквица). На титульной полосе-афише
    это правило не действует вообще: там фото — главный приём, и решение о
    нём принимает _cover_page_html."""
    return (
        tier in ("feature", "brief")
        and article.photo_data_uri is not None
        and article.photo_relevance >= MIN_PHOTO_RELEVANCE_TO_SHOW
    )


@dataclass(frozen=True)
class _Placement:
    tier: str  # "lead" | "feature" | "brief"
    col_span: int
    row_span: int


def _tile_width_mm(col_span: int, columns: int, page_w: float) -> float:
    """Полная физическая ширина плитки col_span колонок из columns — минус
    поля страницы и зазоры мозаики, но БЕЗ вычета паддинга самой плитки (в
    отличие от _text_width_mm): фото бьётся в край плитки через отрицательные
    поля (см. .card-photo в _STYLE_TEMPLATE), так что его ширина — это полная
    ширина плитки, а не ширина, доступная под текст."""
    content_width = page_w - 2 * PAGE_SIDE_PADDING_MM - 2 * PAGE_BORDER_MM
    col_width = (content_width - (columns - 1) * MOSAIC_GAP_MM) / columns
    return col_width * col_span + (col_span - 1) * MOSAIC_GAP_MM


def _text_width_mm(col_span: int, columns: int, page_w: float) -> float:
    """Ширина, доступная под текст в плитке шириной col_span колонок из
    columns — минус поля страницы, зазоры мозаики и паддинг самой плитки."""
    return _tile_width_mm(col_span, columns, page_w) - 2 * CARD_PADDING_H_MM


def _photo_row_span(col_span: int, columns: int, page_w: float) -> int:
    """Сколько row-unit'ов займёт фото-врезка (см. _should_show_photo) при
    заданной ширине плитки — кадр фиксированного соотношения сторон
    (PHOTO_ASPECT_RATIO) на всю ширину плитки, плюс строка под подпись.
    Только стартовая оценка для _seed_bands — реальную высоту, как и для
    текста, подтверждает браузерный цикл подгонки (_fit_bands)."""
    width_mm = _tile_width_mm(col_span, columns, page_w)
    height_mm = width_mm / PHOTO_ASPECT_RATIO
    return math.ceil(height_mm / ROW_UNIT_MM) + PHOTO_CAPTION_ROWS


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
    (render_pages, _fit_bands)."""
    text_width = _text_width_mm(col_span, columns, page_w)
    headline_rows = _rows_for_text(
        article.headline_char_count, text_width, TIER_HEADLINE_FONT_MM[tier], TIER_HEADLINE_LINE_HEIGHT[tier]
    )
    body_rows = _rows_for_text(
        article.body_char_count, text_width, TIER_BODY_FONT_MM[tier], TIER_BODY_LINE_HEIGHT
    )
    photo_rows = _photo_row_span(col_span, columns, page_w) if _should_show_photo(tier, article) else 0
    return ROW_OVERHEAD + photo_rows + headline_rows + body_rows


@dataclass(frozen=True)
class _Band:
    """Один ряд мозаики: несколько плиток ОДНОЙ ширины (col_span), которые
    в сумме всегда дают ровно ширину полосы (columns) — см. модуль-докстринг
    про переход от свободной упаковки к стандартизированным рядам без дыр.
    row_span общий на весь ряд, посчитанный по самой длинной статье в нём
    (_band_row_span) — остальные участники ряда просто получают немного
    пустого места внутри СВОЕЙ собственной плитки, если их текст короче.
    Ряд — атомарная единица пагинации (_fit_bands, _fill_bands): либо весь
    целиком помещается на полосу, либо весь целиком уходит на следующую."""

    tier: str  # "lead" | "feature" | "brief"
    col_span: int
    members: tuple[Article, ...]
    row_span: int


def _band_row_span(tier: str, col_span: int, columns: int, page_w: float, members: tuple[Article, ...]) -> int:
    return max(_estimate_row_span(tier, col_span, columns, page_w, m) for m in members)


def _make_band(tier: str, col_span: int, members: list[Article], columns: int, page_w: float) -> _Band:
    members_t = tuple(members)
    return _Band(tier, col_span, members_t, _band_row_span(tier, col_span, columns, page_w, members_t))


def _group_into_bands(
    tier: str, pool: list[Article], group_size: int, columns: int, page_w: float
) -> list[_Band]:
    """Режет pool на полноширинные ряды по group_size статей подряд — ширина
    каждой плитки в ряду равна columns // group_size, так что group_size
    таких плиток в сумме всегда дают ровно columns без остатка. Последний
    ряд, если статей не хватило набрать его целиком (буквально конец
    очереди — дальше в pool ничего нет), не оставляет дыру, а растягивает
    оставшиеся плитки на всю ширину полосы поровну между ними (см. модуль-
    докстринг: "растянуть иконку поста" вместо серого провала в сетке)."""
    span = columns // group_size
    bands: list[_Band] = []
    for i in range(0, len(pool), group_size):
        chunk = pool[i : i + group_size]
        chunk_span = span if len(chunk) == group_size else columns // len(chunk)
        bands.append(_make_band(tier, chunk_span, chunk, columns, page_w))
    return bands


def _build_bands(articles: list[Article], columns: int, page_w: float) -> list[_Band]:
    """Разбивает новости на три уровня важности по LLM-оценке (importance,
    тай-брейк при равной оценке — длина текста для лида, дата публикации для
    остальных): одна широкая "передовица" на всю ширину полосы (самая
    значимая новость дня, а не просто самый длинный пост, и всегда ровно
    одна на весь номер), следующие FEATURE_SLOTS по важности — плитки
    в половину ширины полосы (feature), всё остальное — в треть ширины
    (brief). lead/feature/brief-ширины — columns, columns // 2 и columns // 3
    (все три — делители columns), поэтому lead всегда один в своём ряду,
    feature — парами, brief — тройками (_group_into_bands): ряды всегда
    заполняются без остатка, кроме, возможно, самого последнего ряда во всей
    очереди (см. его докстринг).

    Все новости номера, включая самую важную, печатаются ИМЕННО ЗДЕСЬ, в
    мозаике: титульная полоса (_cover_page_html) — это афиша-витрина, она
    только анонсирует новости крупными заголовками и фото, но не печатает
    ни одной статьи целиком, поэтому ничего из мозаики не изымает.

    Порядок рядов на выходе — приоритет печати: лид первым, дальше по
    убыванию важности — тот же порядок, в котором render_pages наполняет
    полосы, так что при нехватке места важное попадёт в номер раньше
    неважного (и раньше окажется среди анонсов на афише). Возвращает
    список _Band."""
    if not articles:
        return []

    by_importance = sorted(articles, key=lambda a: (a.importance, a.word_count), reverse=True)
    lead, *rest = by_importance
    rest_ordered = sorted(rest, key=lambda a: (-a.importance, a.posted_at))

    feature_pool, brief_pool = rest_ordered[:FEATURE_SLOTS], rest_ordered[FEATURE_SLOTS:]

    bands = [_make_band("lead", columns, [lead], columns, page_w)]
    bands += _group_into_bands("feature", feature_pool, 2, columns, page_w)
    bands += _group_into_bands("brief", brief_pool, 3, columns, page_w)
    return bands


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
    """Сколько рядов из начала remaining взять как первую попытку для
    полосы — по бюджету "ячеек" (доступные строки × колонки), с запасом
    (PAGE_BUDGET_SAFETY_FACTOR). Ряд всегда полной ширины (см. _Band), так
    что его "стоимость" в ячейках — просто columns * row_span, без разницы,
    сколько в нём плиток и какой они ширины. Это только стартовая
    эвристика, чтобы не гонять цикл проверки в браузере (_fit_bands) от
    одного ряда за раз — настоящая проверка, влезло ли реально, всегда
    происходит в браузере."""
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


# Два независимых вида переполнения, и лечатся они по-разному:
# - boxOverflow: сама плитка (грид-ячейка) вылезает за нижний край .mosaic —
#   значит на полосе тупо не хватило места, весь её ряд целиком нужно
#   переносить на следующую (см. _fit_bands — ряд атомарен).
# - contentOverflow: плитка помещается на полосе, но её СОБСТВЕННЫЙ текст не
#   влезает в отведённую ей высоту (card.scrollHeight > card.clientHeight —
#   scrollHeight видит реальный контент даже под overflow:hidden). Перенос
#   такой статьи на другую полосу ничего не даст — у неё та же оценка
#   row_span, тот же результат на любой полосе; нужно увеличить row_span
#   всего ряда, в котором она стоит (см. _fit_bands), и переизмерить.
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
    """(start, end) индексов в плоском списке статей (_flatten_bands) для
    каждого ряда — чтобы перевести индексы карточек из результата
    _OVERFLOW_CHECK_JS (он видит только плоский DOM) обратно в ряды."""
    bounds = []
    pos = 0
    for b in bands:
        bounds.append((pos, pos + len(b.members)))
        pos += len(b.members)
    return bounds


def _fit_bands(
    probe_page: Page,
    candidates: list[_Band],
    placements: dict[int, _Placement],
    row_unit_px: float,
    columns: int,
    page_w: float,
    build_probe_html: Callable[[list[Article]], str],
) -> tuple[list[_Band], list[_Band]]:
    """Реально рендерит ряды-кандидаты в headless-браузере и смотрит, что не
    влезло — это не оценка, а измерение в настоящем layout-движке Chromium,
    единственный надёжный способ гарантировать, что текст никогда не
    обрежется overflow:hidden на .card/.mosaic втихую (см. модуль-
    докстринг). Различает два вида переполнения (см. _OVERFLOW_CHECK_JS),
    но применяет их на уровне РЯДА целиком, а не отдельной плитки — все
    плитки ряда обязаны иметь одну и ту же высоту (см. _Band):

    - Текст какой-то плитки не влезает в СВОЮ ЖЕ ячейку — значит оценка
      row_span для всего её ряда была занижена (перенос на другую полосу
      тут не поможет: row_span ряда не зависит от полосы, результат будет
      тем же). Увеличиваем row_span всего ряда целиком (остальные плитки
      этого ряда просто получат чуть больше пустого места внутри себя) и
      перерисовываем эту же полосу заново.
    - Хотя бы одна плитка ряда (грид-ячейка) вылезает за нижний край
      .mosaic — реальная нехватка места на полосе, весь ряд целиком
      переходит на следующую (после того как переполнений по первому
      пункту не осталось — иначе лишняя высота у одного ряда может ВНОВЬ
      вытолкнуть другой за край).

    Возвращает (влезло на этой полосе, не влезло — на следующую), оба как
    списки _Band."""
    queue = list(candidates)
    overflow_accum: list[_Band] = []
    iteration = 0
    while queue:
        iteration += 1
        flat = _flatten_bands(queue)
        bounds = _band_bounds(queue)
        probe_page.set_content(build_probe_html(flat), wait_until="load")
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
            continue  # тот же queue, но с исправленными row_span — перерисовываем

        if not box_overflow:
            return queue, overflow_accum

        bad_bands = {_band_index_of(i) for i in box_overflow}
        fitted = [b for i, b in enumerate(queue) if i not in bad_bands]
        newly_bad = [b for i, b in enumerate(queue) if i in bad_bands]
        if not fitted:
            if len(queue) == 1:
                band = queue[0]
                if len(band.members) > 1:
                    # Единственный ряд на всю полосу, и даже так не влезает,
                    # но в нём несколько плиток — прежде чем сдаваться,
                    # пробуем то же "растягивание", что и для неполного
                    # последнего ряда очереди (_group_into_bands): разбиваем
                    # его на отдельные плитки во всю ширину полосы и пробуем
                    # каждую по отдельности — шире колонка, короче строка
                    # нужна на единицу текста, ниже требуемая высота.
                    solo_bands = [
                        _make_band(band.tier, columns, [m], columns, page_w) for m in band.members
                    ]
                    for b in solo_bands:
                        for m in b.members:
                            placements[id(m)] = _Placement(b.tier, b.col_span, b.row_span)
                    queue = solo_bands
                    continue
                # Уже одна-единственная плитка во всю ширину полосы, и всё
                # равно не влезает (редчайший случай, аномально длинный
                # пост) — дальше сжимать некуда. Отдаём как есть: страховка
                # .card{overflow:hidden} не даст ей наехать на соседей (их
                # тут и нет), но её собственный текст может обрезаться.
                return queue, overflow_accum
            # Раньше здесь сразу отдавался queue[:1] как "влезло" без
            # проверки — а он мог не влезть и в одиночку (переполнение было
            # именно от соседства с другими, не от него самого). Вместо
            # мгновенного возврата сокращаем queue до первого ряда и
            # ПЕРЕПРОВЕРЯЕМ его в изоляции на следующем круге — остальные
            # уходят в overflow.
            overflow_accum = queue[1:] + overflow_accum
            queue = queue[:1]
            continue
        overflow_accum = newly_bad + overflow_accum
        queue = fitted
    return queue, overflow_accum


def _fill_bands(
    probe_page: Page,
    remaining: list[_Band],
    placements: dict[int, _Placement],
    row_unit_px: float,
    columns: int,
    page_w: float,
    avail_rows: int,
    build_probe_html: Callable[[list[Article]], str],
) -> tuple[list[_Band], list[_Band]]:
    """Наполняет одну полосу рядами, не останавливаясь на первой удачной, но
    осторожной оценке (_seed_bands). Пример реальной проблемы, которую это
    чинит: первый ряд очереди оказался коротким — его реальная высота
    небольшая, а _seed_bands, посчитав по нему бюджет "ячеек" на глаз,
    решил, что следующий ряд уже не влезет, и остановился на 1-2 рядах,
    оставив почти всю альбомную полосу пустой, хотя место реально было. При
    фиксированном числе полос (Этап 3 — печатный номер, не бесконечная
    лента) пустовать целой полосе — намного хуже, чем ошибиться в размере
    партии.

    После стартовой партии пробуем добавить остаток ПО ОДНОМУ ряду — и,
    важно, не останавливаемся на первом же, который не влез: он просто
    откладывается (skipped), а дальше пробуются следующие. Иначе один
    крупный ряд в начале очереди блокировал бы место для более мелких,
    которые прекрасно поместились бы в тот же остаток."""
    seed_n = _seed_bands(remaining, columns, avail_rows)
    current, rest = remaining[:seed_n], remaining[seed_n:]
    fitted, overflow = _fit_bands(probe_page, current, placements, row_unit_px, columns, page_w, build_probe_html)
    current = fitted
    pending = overflow + rest

    skipped: list[_Band] = []
    for candidate in pending:
        trial_fitted, trial_overflow = _fit_bands(
            probe_page, current + [candidate], placements, row_unit_px, columns, page_w, build_probe_html
        )
        if trial_overflow:
            skipped.append(candidate)
            continue
        current = trial_fitted
    return current, skipped


# Уголковые метки-крестики (см. .reg-mark в _STYLE_TEMPLATE) — одинаковые
# на любом листе, и обложке, и мозаике, поэтому вынесены в общую константу.
_REG_MARKS_HTML = (
    '<div class="reg-mark tl"></div><div class="reg-mark tr"></div>'
    '<div class="reg-mark bl"></div><div class="reg-mark br"></div>'
)


def _footer_suffix(page_num: int, total_pages: int) -> str:
    return f" · стр. {page_num}/{total_pages}" if total_pages > 1 else ""


def _page_html(
    page_articles: list[Article],
    placements: dict[int, _Placement],
    page_num: int,
    total_pages: int,
    date_label: str,
) -> str:
    """Лист мозаики (feature/brief) — лид сюда не входит, у него отдельная
    титульная полоса (_cover_page_html). Все листы мозаики одинаковы: без
    исключения облегчённая running-header, большой мастхед эксклюзивно на
    обложке (см. модуль-докстринг)."""
    cards_html = "\n".join(_article_html(a, placements[id(a)]) for a in page_articles)

    header_html = f"""
    <div class="running-header">
      <strong>{MASTHEAD_TITLE}</strong> · {date_label} · стр. {page_num} из {total_pages}
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


# Медальон в центре плашки мастхеда — горн (bugle) в лавровом венке, как
# знак между DAILY и BUGLE на обложке-референсе. Рисуется прямо в SVG, а не
# берётся картинкой/эмодзи: эмодзи-труба в ч/б печати выглядит цветной
# кляксой, а внешние файлы в этот рендер не подгрузить (HTML отдаётся через
# set_content, см. render_pages).
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
    """Один анонс на афише: крик, подводка и кадр. Текст статьи сюда не
    входит вообще — афиша зазывает внутрь номера, а не печатает новость."""

    screamer: str
    kicker: str
    photo_data_uri: str | None
    channel: str


def _announce(
    article: Article, teasers: dict[tuple[str, int], tuple[str, str]], screamer_max_chars: int
) -> _Announce:
    """Собирает анонс: крик и подводку берёт у LLM (classifier.teasers), а
    если для этой новости модель ничего не вернула (или тизеры вообще не
    запрашивались) — честно откатывается на её собственный заголовок.
    Афиша без интриги хуже, чем с интригой, но номер из-за этого срывать
    нельзя."""
    key = (article.channel, article.post.message_id)
    llm_screamer, llm_kicker = teasers.get(key, ("", ""))
    screamer = _excerpt(llm_screamer or article.headline_raw, screamer_max_chars)
    kicker = _excerpt(llm_kicker or article.headline_raw, KICKER_MAX_CHARS)
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


# Кегль крика подбирается под его длину, как верстальщик подбирает кегль под
# строку: короткое "СУХО!" печатается во всю полосу, длинная "МОБИЛИЗАЦИЯ?" —
# помельче, но всё так же ОДНОЙ строкой. Ступенчатая таблица тут не годится
# (проверено рендером: 12 символов на 38мм не влезли и перенеслись с дефисом
# посреди слова) — ширина строки линейна по кеглю, значит и считать надо
# делением. Фактор — средняя ширина заглавной Superclarendon Black в долях
# кегля, с запасом на letter-spacing.
SCREAMER_CHAR_WIDTH_FACTOR = 1.0
SCREAMER_FONT_MAX_MM = 52.0
SCREAMER_FONT_MIN_MM = 14.0
SECONDARY_FONT_MAX_MM = 19.0  # потолок кегля вторичного крика — он всё-таки не главный на полосе


def _screamer_font_mm(text: str, available_mm: float) -> float:
    if not text:
        return SCREAMER_FONT_MIN_MM
    ideal = available_mm / (len(text) * SCREAMER_CHAR_WIDTH_FACTOR)
    return round(max(SCREAMER_FONT_MIN_MM, min(SCREAMER_FONT_MAX_MM, ideal)), 1)


def _cover_list_html(articles: list[Article]) -> str:
    """Запасное наполнение места кадра — столбец анонсов текстом (см.
    .cover-list в _STYLE_TEMPLATE)."""
    if not articles:
        return ""
    items = "".join(
        f'<div class="cover-list-item">{html.escape(_excerpt(a.headline_raw, COVER_LIST_MAX_CHARS))}</div>'
        for a in articles
    )
    return f'<div class="cover-list">{items}</div>'


def _cover_photo_html(announce: _Announce, width_mm: float, ribbon: bool) -> str:
    """Кадр анонса. Без фото возвращает пустую строку — афиша тогда держится
    на одной типографике (решение пользователя от 2026-10-06), а не на
    рамке-заглушке."""
    if announce.photo_data_uri is None:
        return ""
    cls = "cover-photo" if ribbon else "secondary-photo"
    ribbon_html = f'<div class="cover-ribbon">{COVER_RIBBON_TEXT}</div>' if ribbon else ""
    return (
        f'<div class="{cls}" style="width: {width_mm}mm;">{ribbon_html}'
        f'<img src="{announce.photo_data_uri}" alt=""></div>'
    )


def _secondary_item_html(announce: _Announce, photo_width_mm: float, column_mm: float) -> str:
    # Кегль — по той же логике, что у главного крика (_screamer_font_mm),
    # только мерится ширина колонки, а не всей полосы: иначе короткое
    # "ВЗЛОМ!" и длинная "ТРАГЕДИЯ!" печатаются одним кеглем, и вторая
    # разрывается переносом посреди слова (проверено рендером).
    # 0.72 — запас на широкие заглавные (Ж, Д, Ш): в узкой колонке средняя
    # ширина литеры врёт сильнее, чем на всю полосу, и "ДОРОЖЕ?" переносилось
    # и при 0.85 (проверено рендером).
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
    issue_number: int,
    total_pages: int,
    main_photo_mm: float,
    secondary_photo_mm: float,
    secondary_column_mm: float,
    main_block_mm: float,
    screamer_width_mm: float,
) -> str:
    """Титульная полоса — АФИША номера, а не статья: крупные анонсы
    (крик + подводка + кадр), рамки-анонсы над мастхедом и инверсная полоса
    внизу, но ни одного текста новости. Сами новости, включая самую
    важную, печатаются внутри, в мозаике (см. _build_bands) — поэтому
    афиша ничего не "уносит" из номера и не рискует обрезать текст.

    Высоту листа блоки распределяют между собой (justify-content:
    space-between у .cover-body), а не набирают текстом, так что афиша
    выглядит одинаково плотной и в день с длинными новостями, и в день с
    короткими. Печатается безусловно и ровно одна на номер, поэтому
    _fit_bands-подгонка тут не нужна: переполнять нечему."""
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

    # Место под главным криком: кадр, если он есть, иначе — столбец анонсов
    # текстом (полоса не должна зиять пустотой в день без единого фото).
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
      <span>{MASTHEAD_EDITION} · выпуск № {issue_number}</span>
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
) -> tuple[list[Path], list[Post]]:
    """Рендерит газету в одну или несколько PNG нужного физического размера
    при заданном DPI. Вьюпорт браузера выставляется в CSS-пикселях под точный
    размер страницы, а device_scale_factor = dpi/96 отвечает за плотность
    пикселей скриншота — итоговые PNG ложатся на лист 1:1, без пересчёта на
    Этапе 4.

    При landscape=True печатный лист склеивается из SHEET_UNITS альбомных
    полос (по умолчанию 2) — но не постфактум склейкой готовых картинок, а
    ЕДИНЫМ проходом раскладки и рендера на весь склеенный лист сразу: одна
    шапка, один колофон, один сплошной контент на всю высоту листа — переход
    между "полосами" внутри листа никак не выделен, лист выглядит как одна
    непрерывная страница (по пожеланию пользователя: "чтобы это была
    буквально одна страничка, полный ровный переход"). Наружу лист уходит
    нарезанным на SHEET_UNITS альбомных PNG — простым разрезанием уже
    готового изображения пополам по высоте (см. конец функции), а не
    отдельным рендером, — потому что печатать целиком (без нарезки) не на
    чем: обычный принтер берёт A4, а не склеенный вдвое лист. Из-за этого
    перенос статьи на стык двух альбомных PNG внутри одного мозаичного листа
    — ожидаемый и допустимый случай (как перенос колонки на настоящей
    газетной полосе).

    Первый склеенный лист — всегда титульная полоса-афиша
    (_cover_page_html: крикливые анонсы, кадры и большой мастхед, но ни
    одной статьи целиком) — она СВЕРХ лимита max_pages на мозаику (решение
    пользователя от 2026-09-27: "добавим два [листа], которые образуют одну
    большую"). `basename_1.png`/`basename_2.png` — её половинки. Дальше идут
    листы мозаики, где и печатаются все новости номера, тоже парами PNG на
    лист:
    `basename_3.png`/`basename_4.png` — первый лист мозаики, и так далее. В
    отличие от обложки с её большим мастхедом, все листы мозаики одинаковы —
    у всех облегчённая running-header, никакого "первый лист мозаики
    особенный" больше нет.

    Разбивка на страницы — не просто оценка по объёму текста: каждый
    лист-кандидат реально рендерится в headless Chromium, и то, что не влезло
    (см. _fit_bands), по-настоящему переносится на следующий лист. Иначе
    (доверять только оценке по числу символов) — на практике случается
    недооценка на конкретных постах, и текст обрезается overflow:hidden молча,
    что ровно то, чего вся эта многостраничность должна избегать (проверено
    на реальных данных — без этой проверки несколько статей теряли последние
    строки).

    max_pages — жёсткий потолок числа альбомных полос МОЗАИКИ (Этап 3, по
    итогам ревью пользователя: печатная газета — это фиксированный номер, не
    бесконечная лента); переводится в потолок числа склеенных листов мозаики
    как ceil(max_pages / SHEET_UNITS). Титульная полоса-обложка в этот лимит
    не входит — рендерится безусловно, отдельным листом, всегда ровно одна
    на номер (при непустом posts). Если после лимита мозаики ещё что-то
    осталось, оно НЕ рендерится вообще — возвращается вторым элементом
    кортежа как список Post, которые не поместились, чтобы вызывающий код
    (см. build_newspaper в pipeline.py) решил, что с ними делать: сократить
    нейронкой и попробовать снова или выбросить из номера как недостаточно
    важные. importance_by_key определяет и то, что попадёт в номер раньше
    (см. порядок рядов в _build_bands), и то, какую новость афиша вынесет в
    главный анонс, а какие — во вторичные: берутся первые по этому же
    порядку из тех, что реально поместились в номер.

    photo_relevance_by_key/photo_path_by_key — фото поста (см.
    collector.py, pipeline._merge_story_arcs) и оценка LLM, насколько оно
    нужно (classifier.py). Показывается у мозаичных плиток и у лида на
    обложке при photo_relevance >= MIN_PHOTO_RELEVANCE_TO_SHOW (мозаика —
    _should_show_photo, обложка — отдельная проверка прямо в
    _cover_page_html: лид больше не часть тирной системы мозаики, но порог
    тот же). Обработка кадра — имитация фототелеграфа/wirephoto (см.
    .card-photo/.cover-photo в _STYLE_TEMPLATE), а не сама фотография как
    есть — осознанный стилистический выбор, не техническое ограничение
    принтера (он цветной).

    issue_number — номер выпуска в строке выходных данных под плашкой
    мастхеда (.masthead-strip) — только на титульной полосе, мозаика его не
    показывает. Пока не связан со счётчиком прогонов в
    БД — вызывающий код (build_newspaper) его не передаёт, поэтому по
    умолчанию всегда 0; параметр существует уже сейчас, чтобы наружное
    подключение (например, к run_id из storage.py) было чистым добавлением
    одной строки, без переделки сигнатуры."""
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

    # Ряды уже в порядке приоритета печати (лид первым, дальше по убыванию
    # важности, см. _build_bands) — при ограничении на число полос именно
    # порядок этого списка решает, что попадёт в номер, если места на всех
    # не хватит, важное должно оказаться раньше в очереди (и раньше — среди
    # анонсов на афише, см. ниже).
    bands_all = _build_bands(articles, columns, page_w)
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
    )
    date_label = run_date.strftime("%d.%m.%Y")

    def wrap(fragment: str) -> str:
        return f'<!doctype html><html lang="ru"><head><meta charset="utf-8">{style}</head><body>{fragment}</body></html>'

    viewport_w = round(page_w * CSS_PX_PER_MM)
    viewport_h = round(sheet_h * CSS_PX_PER_MM)
    scale = dpi / 96
    row_unit_px = ROW_UNIT_MM * CSS_PX_PER_MM  # в CSS-пикселях — тех же единицах, что getBoundingClientRect/scrollHeight

    max_sheets = math.ceil(max_pages / sheet_units) if max_pages is not None else None

    out_dir.mkdir(parents=True, exist_ok=True)
    out_paths: list[Path] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            # Подгонка (_fit_bands) рендерится в том же контексте (тот же
            # device_scale_factor), что и финальный скриншот, — иначе при
            # другом масштабе растеризации текст может перенестись по
            # строкам чуть иначе (округление ширины символов до физических
            # пикселей отличается), и лист, который "влезал" на пробном
            # рендере при масштабе 1, реально обрежется на финальном при
            # масштабе 3+ (проверено на реальных данных — расхождение было
            # именно в один перенос строки на статью).
            context = browser.new_context(
                viewport={"width": viewport_w, "height": viewport_h},
                device_scale_factor=scale,
            )
            probe_page = context.new_page()

            sheets: list[list[Article]] = []
            remaining = bands_all
            while remaining:
                # Вся мозаика печатается с одной и той же running-header —
                # никакого "первый лист особенный" больше нет, большой
                # мастхед эксклюзивно на обложке (см. докстринг функции).
                def build_probe(queue: list[Article]) -> str:
                    # page_num/total_pages тут — заглушки: реальная
                    # нумерация известна только после того, как посчитано
                    # итоговое число листов мозаики (см. ниже). На высоту
                    # .mosaic, которую измеряет _fit_bands, они не влияют —
                    # у running-header фиксированная высота
                    # (RUNNING_HEADER_BLOCK_MM) независимо от текста в ней.
                    return wrap(_page_html(queue, placements, 2, 2, date_label))

                candidates_count = len(remaining)
                fitted_bands, remaining = _fill_bands(
                    probe_page, remaining, placements, row_unit_px, columns, page_w, avail_rows, build_probe
                )
                sheets.append(_flatten_bands(fitted_bands))
                logger.info(
                    "лист мозаики %d: рядов в очереди было=%d влезло=%d осталось_в_очереди=%d",
                    len(sheets), candidates_count, len(fitted_bands), len(remaining),
                )
                if max_sheets is not None and len(sheets) >= max_sheets:
                    break

            # Если вышли по max_sheets, а не по опустевшей очереди — то, что
            # осталось, в номер не попадает вообще (см. докстринг). Отдаём
            # исходные Post вызывающему коду вместо рендера лишних листов.
            leftover_posts = [m.post for b in remaining for m in b.members]

            # Анонсы афиши — из уже РЕАЛЬНО напечатанных (влезших) новостей,
            # в порядке значимости (_build_bands кладёт лид первым), а не из
            # bands_all целиком: иначе афиша зазывала бы на статью, которая в
            # итоге не поместилась и была выброшена (см. build_newspaper).
            fitted_articles = [a for sheet_articles in sheets for a in sheet_articles]
            main_article = fitted_articles[0]
            secondary_articles = fitted_articles[1 : 1 + COVER_SECONDARY_COUNT]
            rest_for_teasers = fitted_articles[1 + COVER_SECONDARY_COUNT :]

            # Крикливые анонсы просим у LLM одним батчем и только для тех
            # новостей, что реально попали на афишу крупным кеглем — отсюда
            # и поздний момент вызова: раньше просто неизвестно, кто это.
            teasers: dict[tuple[str, int], tuple[str, str]] = {}
            if teaser_provider is not None:
                announced = [main_article, *secondary_articles][:COVER_LLM_TEASER_COUNT]
                teasers = teaser_provider([a.post for a in announced])

            main_announce = _announce(main_article, teasers, SCREAMER_MAX_CHARS)
            secondary_announces = [
                _announce(a, teasers, SECONDARY_SCREAMER_MAX_CHARS) for a in secondary_articles
            ]

            # Дальше по важности новости разбираются по слотам афиши без
            # повторов (иначе один заголовок печатается дважды — проверено
            # рендером). Столбец-заполнитель забирает свою долю ТОЛЬКО когда
            # у главного анонса нет кадра и его реально будет видно: иначе
            # он молча съедал новости у рамок и нижней полосы, и та просто
            # не печаталась.
            # Рамки над плашкой разбираются РАНЬШЕ третьего ряда: они часть
            # узнаваемого вида референса, и в день с небольшим числом
            # новостей лучше оставить их и укоротить третий ряд, чем
            # наоборот (проверено рендером на восьми новостях — рамки
            # пропадали целиком).
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
            # Главному анонсу — большая доля листа, остальным рядам
            # остаток. Доля подобрана так, чтобы, во-первых, низ листа не
            # пустовал (правка по ревью: трём вторичным анонсам доставалась
            # половина склеенного листа, и они висели в воздухе), во-вторых,
            # граница разреза на два A4 приходилась на кадр главного
            # анонса, а не на крупный заголовок — разрез кадра не виден,
            # разрыв заголовка виден сразу.
            work_h = sheet_h - PAGE_PADDING_TOP_MM - PAGE_PADDING_BOTTOM_MM - COVER_HEADER_BLOCK_MM
            main_block_mm = round(work_h * COVER_MAIN_BLOCK_SHARE, 1)

            # Нумерация страниц — по листам (сквозная редакционная, не по
            # физическим PNG): обложка — лист 1, дальше листы мозаики.
            total_pages = 1 + len(sheets)
            cover_html = _cover_page_html(
                main_announce, secondary_announces, teaser_top, teaser_bottom,
                tertiary_articles, fallback_list,
                date_label, issue_number, total_pages, main_photo_mm, secondary_photo_mm,
                secondary_column_mm, main_block_mm, content_w,
            )
            mosaic_html = "\n".join(
                _page_html(sheet_articles, placements, i + 2, total_pages, date_label)
                for i, sheet_articles in enumerate(sheets)
            )
            all_sheets_html = cover_html + "\n" + mosaic_html
            total_page_divs = 1 + len(sheets)  # обложка + листы мозаики

            final_page = context.new_page()
            # Вьюпорт под ВСЕ листы сразу — обложку и мозаику (а не один, как
            # для проб) — иначе Page.screenshot(clip=...) отказывается резать
            # область за пределами вьюпорта на втором и последующих листах.
            final_page.set_viewport_size({"width": viewport_w, "height": viewport_h * total_page_divs})
            final_page.set_content(wrap(all_sheets_html), wait_until="load")
            locator = final_page.locator(".page")
            # Каждый .page здесь — уже целый склеенный лист (высотой в
            # sheet_units альбомных полос, см. докстринг) — первый всегда
            # обложка, дальше листы мозаики. Печатать его целым не на чем
            # (принтер берёт A4), поэтому наружу отдаём его же, просто
            # разрезанным на sheet_units альбомных PNG по высоте — без
            # повторного рендера, тем же готовым изображением, так что
            # никакого дополнительного шва разрезание не добавляет (в отличие
            # от раздельного рендера двух полос, который и было решено
            # заменить этим проходом).
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
