"""Renders the local HTML console: the "Assemble newspaper" button (triggers
a full run over the last 24 hours) and the list of saved runs — each with a
detailed table of which posts made it into the paper, which were filtered
out, and why. Viewing an already-saved run (pipeline.load_run) doesn't touch
the LLM and doesn't trigger anything — the console can be browsed as many
times as you like, for free; Telethon/LLM calls only happen on button press.
"""

from __future__ import annotations

import html
import os
import subprocess
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version

from .config import HOME_ENV, REPO_ROOT
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

STYLE = """
<style>
  :root { color-scheme: light; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         margin: 0; padding: 24px; background: #f7f7f8; color: #1a1a1a; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  a { color: #2657d6; }
  .subtitle { color: #666; margin: 0 0 20px; font-size: 13px; }
  .nav { display: flex; gap: 8px; margin-bottom: 18px; flex-wrap: wrap; }
  .nav a, .nav span { border: 1px solid #d0d0d5; border-radius: 6px; padding: 6px 12px;
                       font-size: 13px; text-decoration: none; color: #333; background: #fff; }
  .nav .current { background: #1a1a1a; color: #fff; border-color: #1a1a1a; }
  .issue-box { background: #fff; border: 1px solid #e2e2e5; border-radius: 8px;
               padding: 14px 18px; margin-bottom: 20px; }
  .issue-box h2 { font-size: 15px; margin: 0 0 10px; }
  .issue-actions { display: flex; gap: 10px; flex-wrap: wrap; align-items: center;
                   margin-bottom: 12px; }
  .issue-actions form { margin: 0; }
  a.btn, button.btn { display: inline-block; border: 1px solid #1a1a1a; border-radius: 6px;
                      background: #1a1a1a; color: #fff; padding: 8px 14px; font-size: 13px;
                      font-weight: 600; text-decoration: none; cursor: pointer; }
  button.btn.secondary { background: #fff; color: #1a1a1a; border-color: #d0d0d5; }
  button.btn:disabled { opacity: .6; cursor: wait; }
  .issue-pages { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
  .issue-pages img { width: 100%; height: auto; display: block; border: 1px solid #ccc;
                     background: #fff; }
  .issue-empty { color: #666; font-size: 14px; margin: 0 0 10px; }
  .summary { display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }
  .stat { background: #fff; border: 1px solid #e2e2e5; border-radius: 8px;
          padding: 10px 16px; }
  .stat .n { font-size: 20px; font-weight: 700; display: block; }
  .stat .label { font-size: 12px; color: #666; }
  .filters { margin-bottom: 14px; display: flex; gap: 8px; }
  .filters button { border: 1px solid #d0d0d5; background: #fff; border-radius: 6px;
                     padding: 6px 12px; font-size: 13px; cursor: pointer; }
  .filters button.active { background: #1a1a1a; color: #fff; border-color: #1a1a1a; }
  table { width: 100%; border-collapse: collapse; background: #fff;
          border: 1px solid #e2e2e5; border-radius: 8px; overflow: hidden; }
  th, td { text-align: left; padding: 8px 10px; font-size: 13px; border-bottom: 1px solid #eee;
           vertical-align: top; }
  th { background: #fafafa; position: sticky; top: 0; font-weight: 600; }
  tr:last-child td { border-bottom: none; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px;
           font-weight: 600; white-space: nowrap; }
  .badge.in { background: #e3f6e5; color: #1a7a2e; }
  .badge.out { background: #fbe8e8; color: #b02a2a; }
  .badge.photo-unknown { background: transparent; color: #aaa; border: 1px dashed #ccc;
                          font-weight: 400; }
  .badge.photo-none { background: #eef1f5; color: #55606b; }
  .badge.photo-nice { background: #fff3d6; color: #8a5a00; }
  .badge.photo-essential { background: #fde2df; color: #a3231b; }
  .scores { white-space: nowrap; font-size: 12px; color: #444; }
  .learning { background: #fff; border: 1px solid #e2e2e5; border-radius: 8px;
              padding: 10px 14px; margin-bottom: 20px; font-size: 13px; color: #333; }
  .text-cell { max-width: 480px; }
  .reason-cell { max-width: 320px; color: #444; }
  .chan { color: #666; font-size: 12px; }
  tr.hidden { display: none; }
  details summary { cursor: pointer; }
  .full-text { white-space: pre-wrap; margin-top: 6px; color: #333; }
  ul.runs-list { list-style: none; padding: 0; }
  ul.runs-list li { margin-bottom: 8px; }
  ul.runs-list a { display: inline-block; padding: 10px 14px; background: #fff;
                    border: 1px solid #e2e2e5; border-radius: 8px; width: 100%;
                    box-sizing: border-box; }
  .run-button-row { margin-bottom: 20px; }
  .run-button { display: inline-block; border: none; border-radius: 8px;
                background: #1a7a2e; color: #fff; padding: 12px 22px;
                font-size: 15px; font-weight: 600; cursor: pointer; }
  .run-button:hover { background: #156024; }
  .run-note { color: #666; font-size: 13px; margin: 8px 0 0; }
  .error-box { background: #fbe8e8; border: 1px solid #f0b8b8; color: #7a1a1a;
               border-radius: 8px; padding: 14px 18px; margin-bottom: 20px;
               white-space: pre-wrap; font-size: 13px; }
  /* Блоки Этапа 6 (онбординг / опрос) на главной и страницах feedback_html. */
  .banner { background: #fff3d6; border: 1px solid #ecd28a; color: #5c4200;
            border-radius: 8px; padding: 14px 18px; margin-bottom: 20px; }
  .banner a { font-weight: 600; }
  .banner.info { background: #e8f0fe; border-color: #b9cdf5; color: #1d3a7a; }
</style>
"""

FILTER_SCRIPT = """
<script>
  const buttons = document.querySelectorAll('.filters button');
  const rows = document.querySelectorAll('tbody tr');
  buttons.forEach(btn => btn.addEventListener('click', () => {
    buttons.forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const filter = btn.dataset.filter;
    rows.forEach(row => {
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
    preview = html.escape(stripped.replace("\n", " "))[:140]
    if len(stripped) <= 140:
        return html.escape(stripped)
    full = html.escape(stripped)
    return f'<details><summary>{preview}…</summary><div class="full-text">{full}</div></details>'


def _row_html(outcome: PostOutcome) -> str:
    p = outcome.post
    status = "in" if outcome.included else "out"
    badge = "ПОШЁЛ" if outcome.included else "ОТСЕЯН"
    stage = STAGE_LABELS.get(outcome.stage, outcome.stage)
    text = _text_html(p.text)
    reason = html.escape(outcome.reason)
    # For filtered-out posts, photo_relevance is a default placeholder (1),
    # not a real LLM score: such posts never reach classification (see
    # PostOutcome.photo_relevance in pipeline.py). We show a separate neutral
    # placeholder, not one of the three real levels, so it isn't mistaken for
    # an actual model decision.
    if not outcome.included:
        photo_badge = '<span class="badge photo-unknown">— не оценивалось</span>'
    elif outcome.photo_relevance >= 3:
        photo_badge = '<span class="badge photo-essential">📷 без фото теряется смысл</span>'
    elif outcome.photo_relevance == 2:
        photo_badge = '<span class="badge photo-nice">📷 фото уместно</span>'
    else:
        photo_badge = '<span class="badge photo-none">📷 фото не нужно</span>'
    return f"""
    <tr data-status="{status}">
      <td>{p.posted_at.isoformat(timespec="minutes")}</td>
      <td><span class="chan">{html.escape(p.channel)}/{p.message_id}</span></td>
      <td class="text-cell">{text}</td>
      <td><span class="badge {status}">{badge}</span></td>
      <td>{html.escape(stage)}</td>
      <td class="reason-cell">{reason}</td>
      <td>{photo_badge}</td>
      <td>{_score_cell(outcome)}</td>
    </tr>"""


def _score_cell(outcome: PostOutcome) -> str:
    """Importance scores: generic, personal (for the reader), and final.
    Filtered-out posts get a dash; old runs (final_importance == 0) show just
    the generic score, as before; personal == 0 (empty profile) isn't shown."""
    if not outcome.included or outcome.importance <= 0:
        return '<span class="badge photo-unknown">—</span>'
    parts = [f"общая <b>{outcome.importance}</b>"]
    if outcome.final_importance > 0:
        if outcome.personal_importance > 0:
            parts.append(f"для вас <b>{outcome.personal_importance}</b>")
        parts.append(f"итог <b>{outcome.final_importance}</b>")
    return '<span class="scores">' + " · ".join(parts) + "</span>"


def _nav_html(runs: list[Run], current_run_id: int | None) -> str:
    items = []
    for run in runs[:NAV_RUNS_LIMIT]:
        label = to_local(run.started_at).strftime("%Y-%m-%d %H:%M")
        if run.run_id == current_run_id:
            items.append(f'<span class="current">{label}</span>')
        else:
            items.append(f'<a href="{run_page_filename(run.run_id)}">{label}</a>')
    items.append('<a href="index.html">все прогоны</a>')
    return f'<div class="nav">{"".join(items)}</div>'


def _period_html(period_start: datetime | None, period_end: datetime | None) -> str:
    if period_start is None or period_end is None:
        return ""
    fmt = "%Y-%m-%d %H:%M"
    return (
        f'<p class="subtitle">Окно сбора: {to_local(period_start).strftime(fmt)} — '
        f'{to_local(period_end).strftime(fmt)} (последние сутки от запуска).</p>'
    )


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Russian plural inflection by count: 1 полоса, 2 полосы, 5 полос."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _render_form_html(run_id: int, label: str, secondary: bool = False) -> str:
    # Assembly is slow (layout + Gemini calls) — block the button against
    # repeated clicks, same as the main one.
    cls = "btn secondary" if secondary else "btn"
    return (
        f'<form method="post" action="/run_{run_id}/render">'
        f'<button class="{cls}" type="submit" onclick="this.disabled=true; '
        f"this.innerText='Верстаю номер… это может занять пару минут';\">"
        f"{label}</button></form>"
    )


def _issue_html(run_id: int, included: int, page_count: int, render_error: str | None) -> str:
    """The "Issue" block on the run page: PDF download, page previews, and
    re-assembly. The page list is passed in from outside — this module never
    touches the filesystem."""
    error_html = (
        f'<div class="error-box">Не удалось свёрстать номер (отбор сохранён):\n'
        f"{html.escape(render_error)}</div>"
        if render_error
        else ""
    )
    if page_count:
        sheets = page_count // 2
        previews = "\n".join(
            f'<a href="/issue/{run_id}/page_{n}.png" target="_blank">'
            f'<img src="/issue/{run_id}/page_{n}.png" loading="lazy" alt="Полоса {n}"></a>'
            for n in range(1, page_count + 1)
        )
        body = f"""
    <div class="issue-actions">
      <a class="btn" href="/issue/{run_id}/a4.pdf">Скачать PDF — {page_count} {_plural(page_count, "полоса", "полосы", "полос")} A4</a>
      <a class="btn" href="/issue/{run_id}/a3.pdf">Скачать PDF — {sheets} {_plural(sheets, "лист", "листа", "листов")} A3</a>
      {_render_form_html(run_id, "Пересобрать номер", secondary=True)}
    </div>
    <div class="issue-pages">{previews}</div>"""
    elif enough_news_for_issue(included):
        body = f"""
    <p class="issue-empty">Номер ещё не свёрстан.</p>
    <div class="issue-actions">{_render_form_html(run_id, "Свёрстать номер")}</div>"""
    else:
        body = f'<p class="issue-empty">{html.escape(few_news_message(included))}</p>'
    return f'''<div class="issue-box">
    <h2>Номер</h2>
    {error_html}{body}
  </div>'''


def _videos_html(videos: list[YoutubeVideo], youtube_enabled: bool) -> str:
    """The "New videos" section: not shown at all with no channels and no videos."""
    if not videos and not youtube_enabled:
        return ""
    if not videos:
        return '<div class="issue-box"><h2>Новые видео (0)</h2><p class="issue-empty">За период новых видео нет.</p></div>'
    items = "".join(
        f'<li>{html.escape(v.channel_title)} · '
        f'<a href="https://youtu.be/{html.escape(v.video_id)}" target="_blank" rel="noopener">{html.escape(v.title)}</a>'
        f' · {v.published_at.astimezone().strftime("%Y-%m-%d %H:%M")}</li>'
        for v in videos
    )
    return f'<div class="issue-box"><h2>Новые видео ({len(videos)})</h2><ul>{items}</ul></div>'


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

    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>TG Newspaper — прогон {to_local(run_started_at).strftime("%Y-%m-%d %H:%M")}</title>
{STYLE}
</head>
<body>
  <h1>Прогон {to_local(run_started_at).strftime("%Y-%m-%d %H:%M")}</h1>
  <p class="subtitle">Эвристики → копипаст-дедуп → LLM-классификация → дедуп пересказов.</p>
  {_period_html(period_start, period_end)}

  {_nav_html(all_runs, run_id)}

  {_issue_html(run_id, included, issue_page_count, render_error)}

  {_videos_html(videos or [], youtube_enabled)}

  <div class="summary">
    <div class="stat"><span class="n">{total}</span><span class="label">всего постов</span></div>
    <div class="stat"><span class="n">{included}</span><span class="label">пошли в газету</span></div>
    <div class="stat"><span class="n">{excluded}</span><span class="label">отсеяны</span></div>
  </div>

  <div class="filters">
    <button class="active" data-filter="all">Все</button>
    <button data-filter="in">Пошли в газету</button>
    <button data-filter="out">Отсеяны</button>
  </div>

  <table>
    <thead>
      <tr>
        <th>Время</th>
        <th>Канал/ID</th>
        <th>Текст</th>
        <th>Статус</th>
        <th>Этап</th>
        <th>Причина</th>
        <th>Фото</th>
        <th>Значимость</th>
      </tr>
    </thead>
    <tbody>
      {rows}
    </tbody>
  </table>
{FILTER_SCRIPT}
</body>
</html>"""


_RUN_BUTTON_HTML = """
  <div class="run-button-row">
    <form method="post" action="/run">
      <button class="run-button" type="submit" onclick="this.disabled=true; this.innerText='Собираю газету… это может занять пару минут';">
        🗞️ Собрать газету
      </button>
    </form>
    <p class="run-note">
      Заберёт посты за последние 24 часа, отфильтрует, отсеет дубли и сразу свёрстает
      номер (PDF для печати) — займёт время (Telethon + вызовы LLM + вёрстка),
      страница обновится по готовности.
    </p>
  </div>
"""


def _feedback_banners_html(onboarded: bool, issue_survey_run_id: int | None) -> str:
    """Stage 6 blocks on the home page: onboarding (prominent while not done,
    and the run button is blocked) and the survey for the last assembled issue."""
    parts = []
    if not onboarded:
        parts.append(
            '<div class="banner"><strong>Сначала расскажите о себе.</strong> '
            "Пока вы не прошли короткий онбординг, прогон не запустится — газета "
            'подбирается под ваши интересы. <a href="/onboarding">Пройти онбординг →</a></div>'
        )
    if issue_survey_run_id is not None:
        parts.append(
            '<div class="banner info"><strong>Оцените прошлый номер.</strong> '
            "Расставьте 5 постов по важности — так газета научится отбирать "
            f'лучше. <a href="/issue_survey/{issue_survey_run_id}">Оценить →</a></div>'
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
        f'<div class="learning"><strong>Учится ли газета:</strong> опросов отправлено '
        f"{stats.submitted}; {body}.</div>"
    )


def render_index_page(
    runs: list[Run],
    error: str | None = None,
    onboarded: bool = True,
    issue_survey_run_id: int | None = None,
    learning: LearningStats | None = None,
) -> str:
    if not runs:
        list_html = (
            "<p>Пока нет ни одного сохранённого прогона — нажмите кнопку выше, "
            "чтобы собрать первый.</p>"
        )
    else:
        items = "\n".join(
            f'<li><a href="{run_page_filename(run.run_id)}">{to_local(run.started_at).strftime("%Y-%m-%d %H:%M")}</a></li>'
            for run in runs
        )
        list_html = f'<ul class="runs-list">{items}</ul>'

    error_html = f'<div class="error-box">{html.escape(error)}</div>' if error else ""

    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>TG Newspaper — прогоны</title>
{STYLE}
</head>
<body>
  <h1>TG Newspaper</h1>
  <p class="subtitle">Каждый прогон — сбор постов за последние сутки от нажатия кнопки и их отбор.
  · <a href="/setup">Настройки</a></p>
  {_feedback_banners_html(onboarded, issue_survey_run_id)}
  {_learning_html(learning)}
  {error_html}
  {_RUN_BUTTON_HTML}
  {list_html}
  <form method="post" action="/shutdown" style="margin-top:3em;text-align:right"
        onsubmit="return confirm('Выключить консоль? Запустить снова можно иконкой TG Newspaper.')">
    <span style="color:#aaa;font-size:11px;margin-right:1em">TG Newspaper {_app_version()}</span>
    <button type="submit" style="background:none;border:none;color:#888;font-size:12px;cursor:pointer;text-decoration:underline">Выключить консоль</button>
  </form>
</body>
</html>"""


def _app_version() -> str:
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


def render_error_page(message: str) -> str:
    """A run-failure page (a Telethon/Gemini failure, etc.) — shown instead of
    a result, so the error message isn't lost in the server console."""
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>TG Newspaper — ошибка прогона</title>
{STYLE}
</head>
<body>
  <h1>Прогон не удался</h1>
  <div class="error-box">{html.escape(message)}</div>
  <p><a href="/">← вернуться</a></p>
</body>
</html>"""
