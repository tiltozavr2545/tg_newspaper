"""Рендер локальной HTML-консоли: кнопка "Собрать газету" (запускает полный
прогон за последние сутки) и список сохранённых прогонов — по каждому
подробная таблица, какие посты пошли в газету, какие отсеяны и почему.
Просмотр уже сохранённого прогона (pipeline.load_run) не обращается к LLM
и ничего не запускает — консоль можно листать сколько угодно раз бесплатно;
обращение к Telethon/LLM происходит только по нажатию кнопки.
"""

from __future__ import annotations

import html
from datetime import datetime

from .pipeline import PostOutcome
from .storage import Run

STAGE_LABELS = {
    "heuristic": "эвристика",
    "copypaste": "копипаст-дедуп",
    "classification": "LLM-классификация",
    "paraphrase": "дедуп пересказов",
    "history_dedup": "уже публиковалось",
    "final": "—",
}

# Сколько последних прогонов показывать в навигации/на главной — по мотивам
# скользящего окна дедупликации за 5 дней (Этап 2 п.4).
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
  .placeholder {
    background: repeating-linear-gradient(45deg, #eee, #eee 10px, #e4e4e4 10px, #e4e4e4 20px);
    border: 1px dashed #bbb; border-radius: 8px; height: 140px;
    display: flex; align-items: center; justify-content: center;
    color: #777; font-size: 14px; margin-bottom: 20px; text-align: center; padding: 0 20px;
  }
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
  .badge.photo { background: #fff3d6; color: #8a5a00; }
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
    """Короткое превью с раскрытием по клику — полный текст поста без обрезки."""
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
    photo_badge = '<span class="badge photo">📷 нужно фото</span>' if outcome.needs_photo else ""
    return f"""
    <tr data-status="{status}">
      <td>{p.posted_at.isoformat(timespec="minutes")}</td>
      <td><span class="chan">{html.escape(p.channel)}/{p.message_id}</span></td>
      <td class="text-cell">{text}</td>
      <td><span class="badge {status}">{badge}</span></td>
      <td>{html.escape(stage)}</td>
      <td class="reason-cell">{reason}</td>
      <td>{photo_badge}</td>
    </tr>"""


def _nav_html(runs: list[Run], current_run_id: int | None) -> str:
    items = []
    for run in runs[:NAV_RUNS_LIMIT]:
        label = run.started_at.strftime("%Y-%m-%d %H:%M")
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
        f'<p class="subtitle">Окно сбора: {period_start.strftime(fmt)} — '
        f'{period_end.strftime(fmt)} (последние сутки от запуска).</p>'
    )


def render_run_page(
    outcomes: list[PostOutcome],
    run_id: int,
    run_started_at: datetime,
    all_runs: list[Run],
    period_start: datetime | None = None,
    period_end: datetime | None = None,
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
<title>TG Newspaper — прогон {run_started_at.strftime("%Y-%m-%d %H:%M")}</title>
{STYLE}
</head>
<body>
  <h1>Прогон {run_started_at.strftime("%Y-%m-%d %H:%M")}</h1>
  <p class="subtitle">Эвристики → копипаст-дедуп → LLM-классификация → дедуп пересказов.</p>
  {_period_html(period_start, period_end)}

  {_nav_html(all_runs, run_id)}

  <div class="placeholder">
    Здесь будет картинка напечатанной газетной полосы —<br>
    рендер (Этап 3) и печать (Этап 4) ещё не реализованы.
  </div>

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
      Заберёт посты за последние 24 часа, отфильтрует и отсеет дубли — займёт время
      (Telethon + вызовы LLM), страница обновится по готовности.
    </p>
  </div>
"""


def render_index_page(runs: list[Run], error: str | None = None) -> str:
    if not runs:
        list_html = (
            "<p>Пока нет ни одного сохранённого прогона — нажмите кнопку выше, "
            "чтобы собрать первый.</p>"
        )
    else:
        items = "\n".join(
            f'<li><a href="{run_page_filename(run.run_id)}">{run.started_at.strftime("%Y-%m-%d %H:%M")}</a></li>'
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
  <p class="subtitle">Каждый прогон — сбор постов за последние сутки от нажатия кнопки и их отбор.</p>
  {error_html}
  {_RUN_BUTTON_HTML}
  {list_html}
</body>
</html>"""


def render_error_page(message: str) -> str:
    """Страница ошибки прогона (сбой Telethon/Gemini и т.п.) — показывается
    вместо результата, чтобы не терять сообщение об ошибке в консоли сервера."""
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
