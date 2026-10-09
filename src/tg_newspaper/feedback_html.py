"""Страницы консоли для Этапа 6: онбординг читателя, опрос после номера и
страница сравнения порядка читателя с порядком модели.

Вёрстка — тем же подходом, что и report_html.py (общий STYLE + небольшая
добавка ниже, без внешних CSS/JS). Чистые функции "данные -> HTML": ничего не
читают из БД и не знают про HTTP, поэтому страницы можно смотреть и проверять
отдельно от консоли.

Ранжирование — обычные <select> 1..n, а не перетаскивание: без JS-зависимостей
и одинаково работает везде; уникальность мест проверяет сервер (при ошибке
форма возвращается с сохранёнными ответами и сообщением).
"""

from __future__ import annotations

import html
from dataclasses import dataclass

from .feedback import model_ranks
from .report_html import STYLE, _text_html
from .storage import Survey, SurveyItem

EXTRA_STYLE = """
<style>
  .card { background: #fff; border: 1px solid #e2e2e5; border-radius: 8px;
          padding: 14px 18px; margin-bottom: 14px; }
  .card h2 { font-size: 15px; margin: 0 0 10px; }
  textarea { width: 100%; box-sizing: border-box; min-height: 90px; padding: 8px 10px;
             border: 1px solid #d0d0d5; border-radius: 6px; font: inherit; font-size: 13px; }
  label.field { display: block; font-weight: 600; font-size: 13px; margin: 10px 0 4px; }
  .hint { color: #666; font-size: 12px; margin: 0 0 8px; }
  .rank-item { display: flex; gap: 12px; align-items: flex-start; padding: 10px 0;
               border-top: 1px solid #eee; }
  .rank-item:first-of-type { border-top: none; }
  .rank-item select { font-size: 15px; padding: 4px 6px; }
  .rank-item .body { flex: 1; font-size: 13px; }
  .rank-item.bad select { border-color: #b02a2a; }
  .submit { border: none; border-radius: 8px; background: #1a7a2e; color: #fff;
            padding: 10px 20px; font-size: 14px; font-weight: 600; cursor: pointer; }
  .secondary { border: 1px solid #d0d0d5; border-radius: 8px; background: #fff;
               color: #333; padding: 10px 20px; font-size: 14px; cursor: pointer; }
  .actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .score { font-size: 20px; font-weight: 700; }
  .badge.printed { background: #e3f6e5; color: #1a7a2e; }
  .badge.dropped { background: #fbe8e8; color: #b02a2a; }
  .badge.same { background: #e3f6e5; color: #1a7a2e; }
  .badge.diff { background: #fff3d6; color: #8a5a00; }
</style>
"""


@dataclass(frozen=True)
class ItemView:
    """Пост опроса вместе с текстом для показа (напечатанный — для постов
    номера, исходный — для остальных; выбирает вызывающий)."""
    item: SurveyItem
    text: str


def rank_field_name(survey_id: int, item: SurveyItem) -> str:
    """Имя поля формы с местом поста. Ключ — (опрос, позиция показа): позиция
    уникальна в опросе, а канал/номер в имени поля не нужны и неудобны."""
    return f"rank_{survey_id}_{item.shown_position}"


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>TG Newspaper — {html.escape(title)}</title>
{STYLE}
{EXTRA_STYLE}
</head>
<body>
{body}
</body>
</html>"""


def _ranking_block_html(
    survey_id: int,
    views: list[ItemView],
    selected: dict[str, str],
    show_errors: bool,
) -> str:
    """Список постов с <select> мест. Оценки модели и признак "был в номере"
    ЗДЕСЬ не показываются: и то и другое подталкивает читателя (напечатано —
    значит, вроде бы важно), а смысл опроса — его собственное мнение. Всё это
    открывается на странице сравнения после отправки."""
    n = len(views)
    # Выбранные места ЭТОГО опроса — дубликаты ищем только среди них (в
    # онбординге три раунда на одной странице, у каждого свои места 1..n).
    own_values = [selected.get(rank_field_name(survey_id, v.item), "") for v in views]
    rows = []
    for view in views:
        name = rank_field_name(survey_id, view.item)
        chosen = selected.get(name, "")
        options = ['<option value="">—</option>'] + [
            f'<option value="{i}"{" selected" if chosen == str(i) else ""}>{i}</option>'
            for i in range(1, n + 1)
        ]
        bad = " bad" if show_errors and (not chosen or own_values.count(chosen) > 1) else ""
        rows.append(
            f'<div class="rank-item{bad}">'
            f'<select name="{name}">{"".join(options)}</select>'
            f'<div class="body"><span class="chan">{html.escape(view.item.channel)}/'
            f'{view.item.message_id}</span><br>{_text_html(view.text)}</div></div>'
        )
    return "\n".join(rows)


def _error_html(error: str | None) -> str:
    return f'<div class="error-box">{html.escape(error)}</div>' if error else ""


def render_onboarding_page(
    interests: str,
    disinterests: str,
    rounds: list[tuple[int, list[ItemView]]],
    selected: dict[str, str] | None = None,
    error: str | None = None,
) -> str:
    """Форма онбординга: два текстовых поля и до трёх раундов ранжирования на
    одной странице (одна отправка — проще пошагового мастера, а состояние
    между шагами хранить негде и незачем). rounds — [(survey_id, посты)];
    пустой список — в базе нет классифицированных постов, раунды пропущены."""
    selected = selected or {}
    if rounds:
        rounds_html = "".join(
            f"""<div class="card">
        <h2>Раунд {i} из {len(rounds)}: расставьте посты по важности для вас</h2>
        <p class="hint">1 — самое важное для вас, {len(views)} — наименее важное.
        Каждое место — один раз.</p>
        {_ranking_block_html(survey_id, views, selected, bool(error))}
      </div>"""
            for i, (survey_id, views) in enumerate(rounds, start=1)
        )
    else:
        rounds_html = (
            '<div class="banner info">В базе пока нет классифицированных постов, поэтому '
            "раунды ранжирования пропущены — сохранится только текст выше. "
            "Они появятся после первого прогона (но онбординг при этом уже будет пройден).</div>"
        )
    return _page(
        "онбординг",
        f"""
  <h1>Сначала расскажите о себе</h1>
  <p class="subtitle">Это нужно, чтобы газета училась отбирать новости под вас, а не для
  широкой аудитории. Займёт пару минут, пройти можно один раз.</p>
  {_error_html(error)}
  <form method="post" action="/onboarding">
    <div class="card">
      <h2>О чём вам интересно читать?</h2>
      <label class="field" for="interests">Что интересно</label>
      <textarea id="interests" name="interests">{html.escape(interests)}</textarea>
      <label class="field" for="disinterests">Что неинтересно</label>
      <textarea id="disinterests" name="disinterests">{html.escape(disinterests)}</textarea>
      <p class="hint">Свободным текстом: темы, каналы, форматы.</p>
    </div>
    {rounds_html}
    <div class="actions">
      <button class="submit" type="submit">Сохранить</button>
    </div>
  </form>
  <form method="post" action="/onboarding/skip" style="margin-top:12px">
    <button class="secondary" type="submit">Пропустить (можно позже не возвращаться)</button>
  </form>
""",
    )


def render_survey_page(
    survey: Survey,
    views: list[ItemView],
    selected: dict[str, str] | None = None,
    error: str | None = None,
) -> str:
    n = len(views)
    return _page(
        "оцените номер",
        f"""
  <h1>Оцените прошлый номер</h1>
  <p class="subtitle">Расставьте посты по порядку: 1 — самое важное для вас, {n} — наименее
  важное. Каждое место — один раз. Часть постов была в номере, часть нет — не гадайте
  какие, оценивайте сами новости.</p>
  {_error_html(error)}
  <form method="post" action="/survey/{survey.survey_id}">
    <div class="card">
      {_ranking_block_html(survey.survey_id, views, selected or {}, bool(error))}
    </div>
    <div class="actions">
      <button class="submit" type="submit">Отправить</button>
      <a href="/">← на главную</a>
    </div>
  </form>
""",
    )


def _format_agreement(value: float | None) -> str:
    if value is None:
        return "не определено (у модели у всех постов одинаковая оценка)"
    return f"{value:+.2f}"


def _verdict(value: float | None) -> str:
    if value is None:
        return ""
    if value >= 0.6:
        return "Ваш порядок в целом совпал с порядком модели."
    if value <= -0.6:
        return "Ваш порядок почти противоположен порядку модели — ей стоит у вас поучиться."
    return "Ваш порядок совпал с порядком модели лишь частично."


def render_result_page(survey: Survey, views: list[ItemView]) -> str:
    """Сравнение порядка читателя и модели. Здесь, уже после отправки, можно
    показать оценки модели и пометки "был в номере / выброшен": ответ дан, и
    подсказка больше не влияет на него."""
    rows_data = sorted(views, key=lambda v: v.item.user_rank or 0)
    scores = [v.item.model_score for v in rows_data]
    model_place = model_ranks(scores)
    rows = []
    for view, mplace in zip(rows_data, model_place):
        it = view.item
        tie = any(p == mplace and v is not view for v, p in zip(rows_data, model_place))
        mplace_label = f"{mplace}{'=' if tie else ''}"
        same = "same" if it.user_rank == mplace and not tie else "diff"
        if survey.kind == "issue":
            status = (
                '<span class="badge printed">в номере</span>'
                if it.in_issue
                else '<span class="badge dropped">не влез в номер</span>'
            )
        else:
            status = ""
        rows.append(
            f"""<tr>
      <td><span class="score">{it.user_rank}</span></td>
      <td><span class="badge {same}">{mplace_label}</span></td>
      <td>{it.model_score}</td>
      <td>{status}</td>
      <td class="text-cell"><span class="chan">{html.escape(it.channel)}/{it.message_id}</span><br>{_text_html(view.text)}</td>
    </tr>"""
        )
    status_head = "<th>Номер</th>" if survey.kind == "issue" else "<th></th>"
    return _page(
        "сравнение",
        f"""
  <h1>Спасибо! Вот как это выглядит</h1>
  <p class="subtitle">Ваш порядок против порядка, в котором эти посты расставила модель
  (по её оценке значимости 1-5; равные оценки дают одно общее место, помечено «=»).</p>
  <div class="summary">
    <div class="stat"><span class="n">{_format_agreement(survey.agreement)}</span>
      <span class="label">совпадение порядка (Kendall tau-b, от -1 до +1)</span></div>
  </div>
  <p>{_verdict(survey.agreement)}</p>
  <table>
    <thead><tr><th>Ваше место</th><th>Место модели</th><th>Оценка модели</th>{status_head}<th>Пост</th></tr></thead>
    <tbody>
      {"".join(rows)}
    </tbody>
  </table>
  <p><a href="/">← на главную</a></p>
""",
    )


def render_message_page(title: str, message: str) -> str:
    return _page(
        title,
        f"""
  <h1>{html.escape(title)}</h1>
  <p>{html.escape(message)}</p>
  <p><a href="/">← на главную</a></p>
""",
    )
