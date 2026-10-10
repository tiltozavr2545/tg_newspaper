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

from .feedback import LearningStats
from .pipeline import PostOutcome
from .storage import Run

STAGE_LABELS = {
    "heuristic": "эвристика",
    "copypaste": "копипаст-дедуп",
    "classification": "LLM-классификация",
    "paraphrase": "дедуп пересказов",
    "story_merge": "склеено в одну статью",
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
    # photo_relevance у отсеянных постов — заглушка по умолчанию (1), а не
    # настоящая оценка LLM: до классификации такие посты не доходят (см.
    # PostOutcome.photo_relevance в pipeline.py). Показываем отдельную
    # нейтральную заглушку, а не один из трёх реальных уровней, чтобы не
    # выдавать её за решение модели.
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
    """Оценки значимости: общая (generic), для читателя (personal) и итоговая.
    Для отсеянных — прочерк; для старых прогонов (final_importance == 0) —
    только общая, как раньше; personal == 0 (пустой профиль) не показывается."""
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


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение по числу: 1 полоса, 2 полосы, 5 полос."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _render_form_html(run_id: int, label: str, secondary: bool = False) -> str:
    # Сборка долгая (вёрстка + вызовы Gemini) — блокируем кнопку от повторных
    # кликов, как у основной.
    cls = "btn secondary" if secondary else "btn"
    return (
        f'<form method="post" action="/run_{run_id}/render">'
        f'<button class="{cls}" type="submit" onclick="this.disabled=true; '
        f"this.innerText='Верстаю номер… это может занять пару минут';\">"
        f"{label}</button></form>"
    )


def _issue_html(run_id: int, included: int, page_count: int, render_error: str | None) -> str:
    """Блок "Номер" на странице прогона: скачивание PDF, превью полос и
    пересборка. Список страниц приходит снаружи — файловую систему этот
    модуль не трогает."""
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
    elif included:
        body = f"""
    <p class="issue-empty">Номер ещё не свёрстан.</p>
    <div class="issue-actions">{_render_form_html(run_id, "Свёрстать номер")}</div>"""
    else:
        body = '<p class="issue-empty">В номер нечего ставить.</p>'
    return f'''<div class="issue-box">
    <h2>Номер</h2>
    {error_html}{body}
  </div>'''


def render_run_page(
    outcomes: list[PostOutcome],
    run_id: int,
    run_started_at: datetime,
    all_runs: list[Run],
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    issue_page_count: int = 0,
    render_error: str | None = None,
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

  {_issue_html(run_id, included, issue_page_count, render_error)}

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
    """Блоки Этапа 6 на главной: онбординг (пока не пройден — заметно, и
    запуск прогона заблокирован) и опрос по последнему собранному номеру."""
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
    """Компактный показатель "учится ли газета": число опросов и среднее
    совпадение порядка читателя с моделью (tau-b, -1..1) — последние 5 против
    предыдущих 5. Рост — модель всё лучше угадывает порядок читателя. Без
    графиков и JS: пока данных мало, честно пишем, что сравнивать нечего."""
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
  <p class="subtitle">Каждый прогон — сбор постов за последние сутки от нажатия кнопки и их отбор.
  · <a href="/setup">Настройки</a></p>
  {_feedback_banners_html(onboarded, issue_survey_run_id)}
  {_learning_html(learning)}
  {error_html}
  {_RUN_BUTTON_HTML}
  {list_html}
  <form method="post" action="/shutdown" style="margin-top:3em;text-align:right"
        onsubmit="return confirm('Выключить консоль? Запустить снова можно иконкой TG Newspaper.')">
    <button type="submit" style="background:none;border:none;color:#888;font-size:12px;cursor:pointer;text-decoration:underline">Выключить консоль</button>
  </form>
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
