"""Renders the local HTML console: the "Assemble newspaper" button (triggers
a full run over the last 24 hours) and the list of saved runs — each with a
detailed table of which posts made it into the paper, which were filtered
out, and why. Viewing an already-saved run (pipeline.load_run) doesn't touch
the LLM and doesn't trigger anything — the console can be browsed as many
times as you like, for free; Telethon/LLM calls only happen on button press.

Styling lives in console_theme (one shared stylesheet, page() wrapper).
"""

from __future__ import annotations

import html
from datetime import datetime

from .console_theme import (
    edition_ru,
    page,
    ru_date,
    ru_day_month,
    section_head,
)
from .feedback import LearningStats
from .layout import to_local
from .pipeline import PostOutcome, enough_news_for_issue, few_news_message
from .storage import Run
from .youtube import YoutubeVideo

STAGE_LABELS = {
    "heuristic": "эвристика",
    "copypaste": "копипаст-дедуп",
    "classification": "LLM-классификация",
    "paraphrase": "дедуп пересказов",
    "story_merge": "склеено в одну статью",
    "history_dedup": "уже публиковалось",
    "final": "—",
}

# How many recent runs to show in the nav/home page — echoes the 5-day
# rolling dedup window (Stage 2 item 4).
NAV_RUNS_LIMIT = 5


BUSY_RENDER = "Верстаю номер… это может занять пару минут"
BUSY_RUN = "Собираю газету…"

FILTER_SCRIPT = """
<script>
  const segButtons = document.querySelectorAll('.seg button');
  const postRows = document.querySelectorAll('tbody tr');
  segButtons.forEach(btn => btn.addEventListener('click', () => {
    segButtons.forEach(b => b.setAttribute('aria-pressed', b === btn ? 'true' : 'false'));
    const filter = btn.dataset.filter;
    postRows.forEach(row => {
      const show = filter === 'all' || row.dataset.status === filter;
      row.classList.toggle('hidden', !show);
    });
  }));
</script>
"""


def run_page_filename(run_id: int) -> str:
    return f"run_{run_id}.html"


def _text_html(raw_text: str) -> str:
    """A short preview that expands on click — the post's full text, never truncated."""
    stripped = raw_text.strip()
    if len(stripped) <= 140:
        return html.escape(stripped)
    preview = html.escape(stripped[:140].replace("\n", " "))
    full = html.escape(stripped)
    return f'<details class="expand"><summary>{preview}…</summary><div class="full-text">{full}</div></details>'


def _short_dt(moment: datetime) -> str:
    return to_local(moment).strftime("%d.%m %H:%M")


def _photo_html(relevance: int) -> str:
    """The photo line with a 3-square meter (filled = how much the post needs a photo)."""
    if relevance >= 3:
        level, label = 3, "обязательно"
    elif relevance == 2:
        level, label = 2, "уместно"
    else:
        level, label = 1, "не нужно"
    squares = "".join("<i class=\"on\"></i>" if n < level else "<i></i>" for n in range(3))
    return f'<div class="photo">Фото: {label}<span class="meter" aria-hidden="true">{squares}</span></div>'


def _score_cell(outcome: PostOutcome) -> str:
    """Importance scores: the final one as a big figure, under it the generic
    and personal parts. Filtered-out posts get a dash (they never reach the
    LLM scoring — photo_relevance is a placeholder for them too); old runs
    (final_importance == 0) show just the generic score; personal == 0
    (empty profile) isn't shown."""
    if not outcome.included:
        return '<span class="fig none">—</span>'
    figure = outcome.final_importance if outcome.final_importance > 0 else outcome.importance
    if figure <= 0:
        top = '<span class="fig none">—</span>'
    else:
        parts = []
        if outcome.final_importance <= 0:
            parts.append("общая оценка")  # old run: the figure is the generic score itself
        else:
            if outcome.importance > 0:
                parts.append(f"общая {outcome.importance}")
            if outcome.personal_importance > 0:
                parts.append(f"для вас {outcome.personal_importance}")
        sub = f'<div class="subscores">{", ".join(parts)}</div>' if parts else ""
        top = f'<span class="fig">{figure}</span>{sub}'
    return top + _photo_html(outcome.photo_relevance)


def _row_html(outcome: PostOutcome) -> str:
    p = outcome.post
    status = "in" if outcome.included else "out"
    stamp = (
        '<span class="stamp ink">отобран</span>'
        if outcome.included
        else '<span class="stamp nonrepro">отсеян</span>'
    )
    stage = STAGE_LABELS.get(outcome.stage, outcome.stage)
    stage_html = "" if outcome.stage == "final" else f"<div>{html.escape(stage)}</div>"
    return f"""
    <tr data-status="{status}">
      <td class="when"><div class="meta">{_short_dt(p.posted_at)}</div><div class="meta dim">{html.escape(p.channel)}/{p.message_id}</div></td>
      <td><div class="post-text">{_text_html(p.text)}</div></td>
      <td class="decision">{stamp}{stage_html}<div>{html.escape(outcome.reason)}</div></td>
      <td class="scores">{_score_cell(outcome)}</td>
    </tr>"""


def _nav_html(runs: list[Run], current_run_id: int | None) -> str:
    items = ['<a href="index.html">Все прогоны</a>']
    for run in runs[:NAV_RUNS_LIMIT]:
        label = to_local(run.started_at).strftime("%d.%m %H:%M")
        if run.run_id == current_run_id:
            items.append(f'<span class="current" aria-current="page">{label}</span>')
        else:
            items.append(f'<a href="{run_page_filename(run.run_id)}">{label}</a>')
    return f'<nav class="runs-ribbon" aria-label="Прогоны">{"".join(items)}</nav>'


def _window_part(period_start: datetime | None, period_end: datetime | None) -> str:
    if period_start is None or period_end is None:
        return ""
    def fmt(m: datetime) -> str:
        local = to_local(m)
        return f"{ru_day_month(local)}, {local.strftime('%H:%M')}"
    return f" Окно сбора: {fmt(period_start)} — {fmt(period_end)}."


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Russian plural inflection by count: 1 полоса, 2 полосы, 5 полос."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _render_form_html(run_id: int, label: str, secondary: bool = False) -> str:
    # Assembly is slow (layout + Gemini calls) — data-busy turns the button
    # into the "press is running" strip on submit (console_theme.BUSY_SCRIPT).
    cls = "btn secondary press" if secondary else "btn press"
    return (
        f'<form method="post" action="/run_{run_id}/render" data-busy="{BUSY_RENDER}">'
        f'<button class="{cls}" type="submit">{label}</button></form>'
    )


def _sheets_html(run_id: int, page_count: int) -> str:
    """One A3 sheet = two landscape strips stacked (pages 2k-1 over 2k)."""
    sheets = []
    for first in range(1, page_count + 1, 2):
        nums = [n for n in (first, first + 1) if n <= page_count]
        imgs = "".join(
            f'<a href="/issue/{run_id}/page_{n}.png" target="_blank">'
            f'<img src="/issue/{run_id}/page_{n}.png" loading="lazy" alt="Полоса {n}"></a>'
            for n in nums
        )
        caption = (
            f"Лист {(first + 1) // 2}, полосы {nums[0]}–{nums[1]}"
            if len(nums) == 2
            else f"Лист {(first + 1) // 2}, полоса {nums[0]}"
        )
        sheets.append(
            f'<div class="sheet"><figure><div class="crop"><div class="paper">{imgs}</div></div>'
            f'<figcaption class="meta">{caption}</figcaption></figure></div>'
        )
    return f'<div class="sheets">{"".join(sheets)}</div>'


def _issue_html(run_id: int, included: int, page_count: int, render_error: str | None) -> str:
    """The "Issue" block on the run page: PDF download, page previews, and
    re-assembly. The page list is passed in from outside — this module never
    touches the filesystem."""
    error_html = (
        '<div class="error-box"><p class="err-title">Не удалось свёрстать номер (отбор сохранён):</p>'
        f"<pre>{html.escape(render_error)}</pre></div>"
        if render_error
        else ""
    )
    if page_count:
        sheets = (page_count + 1) // 2
        actions = (
            f'<a class="btn" href="/issue/{run_id}/a4.pdf">PDF A4, {page_count} {_plural(page_count, "полоса", "полосы", "полос")}</a>'
            f'<a class="btn" href="/issue/{run_id}/a3.pdf">PDF A3, {sheets} {_plural(sheets, "лист", "листа", "листов")}</a>'
            f'{_render_form_html(run_id, "Пересобрать номер", secondary=True)}'
        )
        return section_head("Номер", actions) + error_html + _sheets_html(run_id, page_count)
    head = section_head("Номер")
    if enough_news_for_issue(included):
        body = (
            "<p>Номер ещё не свёрстан.</p>"
            f'<form method="post" action="/run_{run_id}/render" data-busy="{BUSY_RENDER}">'
            '<button class="btn press" type="submit">Свёрстать номер</button></form>'
        )
    else:
        body = f"<p>{html.escape(few_news_message(included))}</p>"
    return head + error_html + body


def _videos_html(videos: list[YoutubeVideo], youtube_enabled: bool) -> str:
    """The "New videos" section: not shown at all with no channels and no videos."""
    if not videos and not youtube_enabled:
        return ""
    if not videos:
        return section_head("Новые видео (0)") + "<p>За период новых видео нет.</p>"
    rows = "".join(
        '<div class="video-row">'
        f'<span class="meta">{html.escape(v.channel_title)}</span>'
        f'<a href="https://youtu.be/{html.escape(v.video_id)}" target="_blank" rel="noopener">{html.escape(v.title)}</a>'
        f'<span class="meta">{v.published_at.astimezone().strftime("%d.%m %H:%M")}</span></div>'
        for v in videos
    )
    return section_head(f"Новые видео ({len(videos)})") + f'<div class="paper">{rows}</div>'


def render_run_page(
    outcomes: list[PostOutcome],
    run_id: int,
    run_started_at: datetime,
    all_runs: list[Run],
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    issue_page_count: int = 0,
    render_error: str | None = None,
    videos: list[YoutubeVideo] | None = None,
    youtube_enabled: bool = False,
) -> str:
    outcomes_sorted = sorted(outcomes, key=lambda o: o.post.posted_at)
    total = len(outcomes_sorted)
    included = sum(1 for o in outcomes_sorted if o.included)
    excluded = total - included
    rows = "\n".join(_row_html(o) for o in outcomes_sorted)

    moment = to_local(period_end or run_started_at)
    title = f"{edition_ru(moment)}, {ru_date(moment)}"
    lede = (
        f"Прогон запущен в {to_local(run_started_at).strftime('%H:%M')}."
        + _window_part(period_start, period_end)
    )

    body = f"""
  {_nav_html(all_runs, run_id)}
  <h1>{html.escape(title)}</h1>
  <p class="lede">{html.escape(lede)}</p>

  {_issue_html(run_id, included, issue_page_count, render_error)}

  {_videos_html(videos or [], youtube_enabled)}

  {section_head("Отбор постов")}
  <p class="lede">Эвристики, копипаст-дедуп, LLM-классификация, дедуп пересказов.</p>
  <div class="seg" role="group" aria-label="Фильтр постов">
    <button type="button" data-filter="all" aria-pressed="true">Все <b>{total}</b></button>
    <button type="button" data-filter="in" aria-pressed="false">Отобраны <b>{included}</b></button>
    <button type="button" data-filter="out" aria-pressed="false">Отсеяны <b>{excluded}</b></button>
  </div>
  <div class="paper table-wrap">
  <table class="posts">
    <thead>
      <tr>
        <th>Когда и где</th>
        <th>Пост</th>
        <th>Решение</th>
        <th>Оценки</th>
      </tr>
    </thead>
    <tbody>
      {rows}
    </tbody>
  </table>
  </div>
{FILTER_SCRIPT}"""
    return page(f"прогон {to_local(run_started_at).strftime('%Y-%m-%d %H:%M')}", body, active="runs")


def _feedback_banners_html(onboarded: bool, issue_survey_run_id: int | None) -> str:
    """Stage 6 blocks on the home page: onboarding (prominent while not done,
    and the run button is blocked) and the survey for the last assembled issue."""
    parts = []
    if not onboarded:
        parts.append(
            '<div class="attn"><strong>Сначала расскажите о себе.</strong> '
            "Пока вы не прошли короткий онбординг, прогон не запустится — газета "
            'подбирается под ваши интересы. <a href="/onboarding">Пройти онбординг</a></div>'
        )
    if issue_survey_run_id is not None:
        parts.append(
            '<div class="attn"><strong>Оцените прошлый номер.</strong> '
            "Расставьте 5 постов по важности — так газета научится отбирать "
            f'лучше. <a href="/issue_survey/{issue_survey_run_id}">Оценить</a></div>'
        )
    return "\n  ".join(parts)


def _learning_html(stats: LearningStats | None) -> str:
    """A compact "is the newspaper learning" indicator: survey count and the
    average match between the reader's order and the model's (tau-b, -1..1)
    — the last 5 against the previous 5. Rising means the model is getting
    better at guessing the reader's order. No charts, no JS: while there's
    too little data, it just honestly says there's nothing to compare yet."""
    if stats is None or stats.submitted == 0:
        return ""
    if stats.recent_avg is None:
        body = "совпадение ещё не определено (у модели одинаковые оценки в опросах)"
    elif stats.previous_avg is None:
        body = (
            f"совпадение порядка с моделью (последние опросы): <b>{stats.recent_avg:+.2f}</b>; "
            "для сравнения нужно хотя бы 6 опросов"
        )
    else:
        delta = stats.recent_avg - stats.previous_avg
        trend = "растёт" if delta > 0.05 else "падает" if delta < -0.05 else "без изменений"
        body = (
            f"совпадение порядка с моделью: последние 5 — <b>{stats.recent_avg:+.2f}</b>, "
            f"предыдущие 5 — {stats.previous_avg:+.2f} ({trend})"
        )
    return (
        f'<p class="learning"><span class="meta">Учится ли газета</span><br>'
        f"опросов отправлено <b>{stats.submitted}</b>; {body}.</p>"
    )


def _next_issue_html(now: datetime) -> str:
    return f"""<div class="slip">
      <div class="meta">Следующий номер</div>
      <h1>{html.escape(edition_ru(now))}</h1>
      <p class="lede">{html.escape(ru_date(now))}</p>
      <form method="post" action="/run" data-busy="{BUSY_RUN}">
        <button class="btn press big" type="submit">Собрать газету</button>
      </form>
      <p class="note">Заберёт посты за последние 24 часа, отфильтрует, отсеет дубли и сразу
      свёрстает номер. Это займёт пару минут — страница обновится сама.</p>
    </div>"""


def _run_moment(run: Run) -> datetime:
    return to_local(run.period_end or run.started_at)


def _cover_html(run: Run, count: int) -> str:
    imgs = "".join(
        f'<img src="/issue/{run.run_id}/page_{n}.png" loading="lazy" alt="Полоса {n}">'
        for n in range(1, min(count, 2) + 1)
    )
    moment = _run_moment(run)
    return f"""<div>
      <div class="crop"><a class="paper cover" href="{run_page_filename(run.run_id)}">{imgs}</a></div>
      <div class="cover-meta"><div class="meta">Последний номер</div>
      <a href="{run_page_filename(run.run_id)}">{html.escape(edition_ru(moment))}, {html.escape(ru_date(moment))}</a></div>
    </div>"""


def _run_row_html(run: Run, count: int) -> str:
    moment = _run_moment(run)
    pages = f"{count} {_plural(count, 'полоса', 'полосы', 'полос')}" if count else "без номера"
    return (
        '<li class="run-row">'
        f'<span class="date">{html.escape(ru_date(moment))}</span>'
        f'<span class="meta">{to_local(run.started_at).strftime("%H:%M")}</span>'
        f'<a href="{run_page_filename(run.run_id)}">{html.escape(edition_ru(moment))}</a>'
        f'<span class="meta">{pages}</span></li>'
    )


def render_index_page(
    runs: list[Run],
    error: str | None = None,
    onboarded: bool = True,
    issue_survey_run_id: int | None = None,
    learning: LearningStats | None = None,
    page_counts: dict[int, int] | None = None,
) -> str:
    counts = page_counts or {}
    # The cover is the newest run that has an assembled issue.
    with_pages = [r for r in runs if counts.get(r.run_id, 0) > 0]
    latest = max(with_pages, key=lambda r: r.run_id) if with_pages else None

    if not runs:
        list_html = (
            '<p class="paper empty">Прогонов пока нет. Нажмите «Собрать газету» — '
            "первый появится здесь.</p>"
        )
    else:
        rows = "\n".join(_run_row_html(r, counts.get(r.run_id, 0)) for r in runs)
        list_html = f'<ul class="paper run-rows">{rows}</ul>'

    error_html = (
        f'<div class="error-box"><pre>{html.escape(error)}</pre></div>' if error else ""
    )
    left = _next_issue_html(datetime.now()) + _learning_html(learning)
    if latest is not None:
        grid = f'<div class="home-grid two"><div>{left}</div>{_cover_html(latest, counts[latest.run_id])}</div>'
    else:
        grid = f'<div class="home-grid"><div>{left}</div></div>'

    body = f"""
  {_feedback_banners_html(onboarded, issue_survey_run_id)}
  {error_html}
  {grid}
  {section_head("Прогоны")}
  {list_html}
"""
    return page("прогоны", body, active="runs")


def render_error_page(message: str) -> str:
    """A run-failure page (a Telethon/Gemini failure, etc.) — shown instead of
    a result, so the error message isn't lost in the server console."""
    body = f"""
  <h1>Прогон не удался</h1>
  <div class="error-box"><pre>{html.escape(message)}</pre></div>
  <p><a href="/">На главную</a></p>
"""
    return page("ошибка прогона", body)
