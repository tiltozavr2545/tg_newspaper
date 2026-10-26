"""Console pages for Stage 6: reader onboarding, the post-issue survey, and
the page comparing the reader's ranking against the model's.

Markup follows the same approach as report_html.py (the shared stylesheet
and page wrapper live in console_theme, no external CSS/JS). Pure
"data -> HTML" functions: they never read from the DB and know nothing about
HTTP, so pages can be viewed and tested independently of the console.

Ranking uses plain <select> dropdowns numbered 1..n, not drag-and-drop: no
JS dependencies, and it works the same everywhere; the server checks that
ranks are unique (on error, the form is returned with the saved answers and
a message).
"""

from __future__ import annotations

import html
from dataclasses import dataclass

from .console_theme import page, section_head
from .feedback import model_ranks
from .report_html import _text_html
from .storage import Survey, SurveyItem


@dataclass(frozen=True)
class ItemView:
    """A survey post together with the text to display (the printed text for
    posts that were in the issue, the original for the rest; the caller decides)."""
    item: SurveyItem
    text: str


def rank_field_name(survey_id: int, item: SurveyItem) -> str:
    """The form field name holding a post's rank. Keyed by (survey, display
    position): the position is unique within a survey, and the channel/id
    would be unnecessary and inconvenient in a field name."""
    return f"rank_{survey_id}_{item.shown_position}"


def _ranking_block_html(
    survey_id: int,
    views: list[ItemView],
    selected: dict[str, str],
    show_errors: bool,
) -> str:
    """The list of posts with rank <select> dropdowns. The model's scores and
    the "was in the issue" flag are deliberately NOT shown here: both would
    bias the reader (printed must mean important), and the point of the
    survey is their own opinion. All of that is revealed on the comparison
    page after submission."""
    n = len(views)
    # Selected ranks for THIS survey only — duplicates are checked only among
    # these (onboarding has three rounds on one page, each with its own 1..n ranks).
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
            f'<select name="{name}" aria-label="Место поста">{"".join(options)}</select>'
            f'<div class="clip-text"><span class="meta">{html.escape(view.item.channel)}/'
            f"{view.item.message_id}</span>{_text_html(view.text)}</div></div>"
        )
    return "\n".join(rows)


def _error_html(error: str | None) -> str:
    if not error:
        return ""
    return f'<div class="notice err"><b>{html.escape(error)}</b></div>'


def render_onboarding_page(
    interests: str,
    disinterests: str,
    rounds: list[tuple[int, list[ItemView]]],
    selected: dict[str, str] | None = None,
    error: str | None = None,
) -> str:
    """The onboarding form: two text fields and up to three ranking rounds on
    one page (a single submission is simpler than a step-by-step wizard, and
    there's nowhere convenient to keep state between steps anyway). rounds is
    [(survey_id, posts)]; an empty list means there are no classified posts
    in the DB yet, so the rounds are skipped."""
    selected = selected or {}
    if rounds:
        rounds_html = "".join(
            f"""<section class="round">
        {section_head(f"Раунд {i} из {len(rounds)}")}
        <p class="hint">Расставьте посты по важности для вас: 1 — самое важное, {len(views)} — наименее важное.
        Каждое место — один раз.</p>
        <div class="paper clippings">
        {_ranking_block_html(survey_id, views, selected, bool(error))}
        </div>
      </section>"""
            for i, (survey_id, views) in enumerate(rounds, start=1)
        )
    else:
        rounds_html = (
            '<div class="attn">В базе пока нет классифицированных постов, поэтому '
            "раунды ранжирования пропущены — сохранится только текст выше. "
            "Они появятся после первого прогона (но онбординг при этом уже будет пройден).</div>"
        )
    return page(
        "онбординг",
        f"""
  <h1>Сначала расскажите о себе</h1>
  <p class="lede">Это нужно, чтобы газета училась отбирать новости под вас, а не для
  широкой аудитории. Займёт пару минут, пройти можно один раз.</p>
  {_error_html(error)}
  <form method="post" action="/onboarding">
    <div class="paper panel">
      <h2>О чём вам интересно читать?</h2>
      <label class="field" for="interests">Что интересно</label>
      <textarea id="interests" name="interests" class="short">{html.escape(interests)}</textarea>
      <label class="field" for="disinterests">Что неинтересно</label>
      <textarea id="disinterests" name="disinterests" class="short">{html.escape(disinterests)}</textarea>
      <p class="hint">Свободным текстом: темы, каналы, форматы.</p>
    </div>
    {rounds_html}
    <div class="actions">
      <button class="btn" type="submit">Сохранить</button>
    </div>
  </form>
  <form method="post" action="/onboarding/skip" style="margin-top:12px">
    <button class="btn quiet" type="submit">Пропустить (можно позже не возвращаться)</button>
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
    return page(
        "оцените номер",
        f"""
  <h1>Оцените прошлый номер</h1>
  <p class="lede">Расставьте посты по порядку: 1 — самое важное для вас, {n} — наименее
  важное. Каждое место — один раз. Часть постов была в номере, часть нет — не гадайте
  какие, оценивайте сами новости.</p>
  {_error_html(error)}
  <form method="post" action="/survey/{survey.survey_id}">
    <div class="paper clippings round">
      {_ranking_block_html(survey.survey_id, views, selected or {}, bool(error))}
    </div>
    <div class="actions">
      <button class="btn" type="submit">Отправить</button>
      <a href="/">На главную</a>
    </div>
  </form>
""",
    )


def _verdict(value: float | None) -> str:
    if value is None:
        return "Совпадение не определено: у модели у всех постов одинаковая оценка."
    if value >= 0.6:
        return "Ваш порядок в целом совпал с порядком модели."
    if value <= -0.6:
        return "Ваш порядок почти противоположен порядку модели — ей стоит у вас поучиться."
    return "Ваш порядок совпал с порядком модели лишь частично."


def render_result_page(survey: Survey, views: list[ItemView]) -> str:
    """Compares the reader's order against the model's. Here, after
    submission, it's safe to show the model's scores and the "was in the
    issue / dropped" labels: an answer has already been given, so the hint
    can no longer bias it."""
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
                '<span class="stamp ink">в номере</span>'
                if it.in_issue
                else '<span class="stamp nonrepro">не влез в номер</span>'
            )
        else:
            status = ""
        rows.append(
            f"""<tr>
      <td><span class="place">{it.user_rank}</span></td>
      <td><span class="mplace {same}">{mplace_label}</span></td>
      <td>{it.model_score}</td>
      <td>{status}</td>
      <td><div class="clip-text"><span class="meta">{html.escape(it.channel)}/{it.message_id}</span>{_text_html(view.text)}</div></td>
    </tr>"""
        )
    status_head = "<th>Номер</th>" if survey.kind == "issue" else "<th></th>"
    figure = "—" if survey.agreement is None else f"{survey.agreement:+.2f}".replace("-", "\u2212")
    return page(
        "сравнение",
        f"""
  <h1>Спасибо! Вот как это выглядит</h1>
  <p class="lede">Ваш порядок против порядка, в котором эти посты расставила модель
  (по её оценке значимости 1-5; равные оценки дают одно общее место, помечено «=»).</p>
  <div class="agree">
    <div class="agree-fig">{figure}</div>
    <div>
      <p class="verdict">{_verdict(survey.agreement)}</p>
      <div class="meta">совпадение порядка, Kendall tau-b, от −1 до +1</div>
    </div>
  </div>
  <div class="paper table-wrap">
  <table class="cmp">
    <thead><tr><th>Ваше место</th><th>Место модели</th><th>Оценка модели</th>{status_head}<th>Пост</th></tr></thead>
    <tbody>
      {"".join(rows)}
    </tbody>
  </table>
  </div>
  <p class="back"><a href="/">На главную</a></p>
""",
    )


def render_message_page(title: str, message: str) -> str:
    return page(
        title,
        f"""
  <h1>{html.escape(title)}</h1>
  <p>{html.escape(message)}</p>
  <p><a href="/">На главную</a></p>
""",
    )
