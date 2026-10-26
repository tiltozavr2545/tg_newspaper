"""The initial setup wizard page (logic lives in setup_wizard.py).

Markup follows the same approach as report_html.py/feedback_html.py: the
shared stylesheet and page wrapper live in console_theme, no external
JS/CSS. Every value is escaped; secrets arrive here already masked
(mask_secret) and are never filled into input fields — an empty secret
field means "don't change it".
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field

from .console_theme import page
from .setup_wizard import (
    STEP_CHANNELS,
    STEP_GEMINI,
    STEP_LOGIN,
    STEP_TELEGRAM,
    STEP_TITLES,
    StepResult,
    StepStatus,
)


@dataclass
class SetupView:
    """Everything the page needs: step status, what to show in the fields,
    and the results of the most recent actions (flash, keyed by step)."""
    steps: list[StepStatus]
    highlight: str | None = None
    api_id: str = ""
    api_hash_masked: str = ""
    session_name: str = "tg_newspaper"
    login_stage: str | None = None  # None | "code" | "password"
    login_who: str = ""
    gemini_key_masked: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_base_url: str = ""
    channels_text: str = ""
    flash: dict[str, StepResult] = field(default_factory=dict)
    channel_checks: list[tuple[str, StepResult]] = field(default_factory=list)
    youtube_text: str = ""
    youtube_checks: list[tuple[str, StepResult]] = field(default_factory=list)


def _e(value: str) -> str:
    return html.escape(value, quote=True)


def _notice(result: StepResult | None) -> str:
    if result is None:
        return ""
    if result.ok:
        return f'<div class="notice ok"><span class="stamp ink">готово</span> {_e(result.message)}</div>'
    first, _, rest = result.message.partition("\n")
    tail = f"\n{_e(rest)}" if rest else ""
    return f'<div class="notice err"><b>{_e(first)}</b>{tail}</div>'


def _secret_hint(masked: str) -> str:
    if masked:
        return f'<p class="hint">Сейчас задан: <code>{_e(masked)}</code>. Оставьте поле пустым, чтобы не менять.</p>'
    return '<p class="hint">Пока не задан.</p>'


def _panel(view: SetupView, key: str, number: int, title: str, stamp: str, detail: str, body: str) -> str:
    current = " current" if view.highlight == key else ""
    detail_html = f'<p class="hint">{_e(detail)}</p>' if detail else ""
    return (
        f'<section class="paper step{current}" id="step-{key}">'
        f'<div class="step-head"><span class="step-num">{number}</span>'
        f'<h2 class="step-title">{_e(title)}</h2></div>'
        f'<div class="stamp-row">{stamp}</div>'
        f"{detail_html}{_notice(view.flash.get(key))}{body}</section>"
    )


def _card(view: SetupView, key: str, number: int, body: str) -> str:
    status = next(s for s in view.steps if s.key == key)
    stamp = (
        '<span class="stamp ink">готово</span>'
        if status.done
        else '<span class="stamp nonrepro">не настроено</span>'
    )
    return _panel(view, key, number, STEP_TITLES[key], stamp, status.detail, body)


def _telegram_card(view: SetupView) -> str:
    body = f"""
<form method="post" action="/setup/telegram">
  <p class="hint">Получить на <a href="https://my.telegram.org" target="_blank" rel="noopener">my.telegram.org</a>
  в разделе «API development tools». Заходите под <strong>отдельным аккаунтом проекта</strong>, не личным:
  сессия Telethon — это ключ к аккаунту (см. AGENTS.md). Бот-токен не нужен — сбор идёт под пользовательским аккаунтом.</p>
  <label class="field" for="api_id">api_id (целое число)</label>
  <input type="text" id="api_id" name="api_id" value="{_e(view.api_id)}" inputmode="numeric" autocomplete="off">
  <label class="field" for="api_hash">api_hash</label>
  <input type="password" id="api_hash" name="api_hash" value="" autocomplete="off">
  {_secret_hint(view.api_hash_masked)}
  <label class="field" for="session_name">Имя файла сессии</label>
  <input type="text" id="session_name" name="session_name" value="{_e(view.session_name)}" autocomplete="off">
  <p class="hint">Буквы, цифры, «_», «-», «.»; по умолчанию tg_newspaper.</p>
  <div class="actions"><button class="btn" type="submit">Сохранить</button></div>
</form>"""
    return _card(view, STEP_TELEGRAM, 1, body)


def _login_card(view: SetupView) -> str:
    status = next(s for s in view.steps if s.key == STEP_LOGIN)
    phone_form = """
<form method="post" action="/setup/login/phone">
  <label class="field" for="phone">Номер телефона аккаунта проекта</label>
  <input type="tel" id="phone" name="phone" placeholder="+79991234567" autocomplete="off">
  <div class="actions"><button class="btn" type="submit">Отправить код</button></div>
</form>"""
    if view.login_stage == "code":
        body = """
<form method="post" action="/setup/login/code">
  <label class="field" for="code">Код из Telegram</label>
  <input type="text" id="code" name="code" inputmode="numeric" autocomplete="off">
  <div class="actions">
    <button class="btn" type="submit">Войти</button>
    <button class="btn quiet" type="submit" formaction="/setup/login/cancel" formnovalidate>Начать заново</button>
  </div>
</form>"""
    elif view.login_stage == "password":
        body = """
<form method="post" action="/setup/login/password">
  <label class="field" for="password">Пароль двухфакторной защиты</label>
  <input type="password" id="password" name="password" autocomplete="off">
  <div class="actions">
    <button class="btn" type="submit">Войти</button>
    <button class="btn quiet" type="submit" formaction="/setup/login/cancel" formnovalidate>Начать заново</button>
  </div>
</form>"""
    elif status.done and view.login_who:
        body = f"""
<p class="who">Вход выполнен: {_e(view.login_who)}</p>
<details><summary>Войти заново (другим аккаунтом)</summary>{phone_form}</details>"""
    elif status.done:
        body = f"<details open><summary>Войти заново</summary>{phone_form}</details>"
    else:
        body = (
            '<p class="hint">Telegram пришлёт код в приложение (обычно не по SMS). Если включена '
            "двухфакторная защита, дальше спросим пароль. Файл сессии создаётся с правами 0600.</p>"
            + phone_form
        )
    return _card(view, STEP_LOGIN, 2, body)


def _gemini_card(view: SetupView) -> str:
    body = f"""
<form method="post" action="/setup/gemini">
  <label class="field" for="gemini_key">GEMINI_API_KEY</label>
  <input type="password" id="gemini_key" name="gemini_key" value="" autocomplete="off">
  {_secret_hint(view.gemini_key_masked)}
  <p class="hint">Ключ — в <a href="https://aistudio.google.com/app/apikey" target="_blank" rel="noopener">Google AI Studio</a>.</p>
  <label class="field" for="gemini_model">Модель</label>
  <input type="text" id="gemini_model" name="gemini_model" value="{_e(view.gemini_model)}" autocomplete="off">
  <label class="field" for="gemini_base_url">GEMINI_BASE_URL (обязательно)</label>
  <input type="text" id="gemini_base_url" name="gemini_base_url" value="{_e(view.gemini_base_url)}" autocomplete="off" required
         placeholder="https://tg-newspaper-gemini.ВАШ-АККАУНТ.workers.dev">
  <p class="hint">Адрес прокси через свой Cloudflare Worker нужен всегда: агент работает из РФ, а Gemini напрямую
  гео-блокирует («User location is not supported») — Google должен видеть адрес Cloudflare, а не ваш. Код воркера —
  <code>cloudflare-worker.js</code> в репозитории, пошаговая инструкция — в README, раздел «LLM-классификация: Gemini и
  обход гео-блокировки через Cloudflare» («Обход гео-блокировки»). Только https://, слэш в конце срезается.
  Кнопка «Сохранить и проверить» ходит именно через этот прокси.</p>
  <div class="actions">
    <button class="btn" type="submit" name="action" value="save">Сохранить</button>
    <button class="btn secondary" type="submit" name="action" value="check">Сохранить и проверить</button>
  </div>
</form>"""
    return _card(view, STEP_GEMINI, 3, body)


def _channels_card(view: SetupView) -> str:
    checks = ""
    if view.channel_checks:
        items = "".join(
            f'<li class="{"ok" if r.ok else "err"}">{"✓" if r.ok else "✕"} @{_e(ch)} — {_e(r.message)}</li>'
            for ch, r in view.channel_checks
        )
        checks = f'<ul class="checks">{items}</ul>'
    body = f"""
<form method="post" action="/setup/channels">
  <label class="field" for="channels">Каналы-источники, по одному на строку</label>
  <textarea id="channels" name="channels" rows="10" placeholder="# Новости&#10;rotondamedia&#10;@telegram&#10;&#10;# Блогеры&#10;https://t.me/example_channel">{_e(view.channels_text)}</textarea>
  <p class="hint">Принимаются username, @username и ссылки t.me/&lt;username&gt; — сохраняется чистый username.
  Строка, начинающаяся с «#», — название рубрики (секции газеты): каналы под ней относятся к ней, например
  «# Новости». Каналы до первого заголовка — без рубрики. Список пишется в config/channels.yaml.</p>
  <div class="actions">
    <button class="btn" type="submit" name="action" value="save">Сохранить</button>
    <button class="btn secondary" type="submit" name="action" value="check">Сохранить и проверить доступ</button>
  </div>
</form>{checks}"""
    return _card(view, STEP_CHANNELS, 4, body)


def _youtube_card(view: SetupView) -> str:
    """An optional step: not part of STEP_ORDER, has no effect on "done"/the next step."""
    checks = ""
    if view.youtube_checks:
        items = "".join(
            f'<li class="{"ok" if r.ok else "err"}">{"✓" if r.ok else "✕"} {_e(src)} — {_e(r.message)}</li>'
            for src, r in view.youtube_checks
        )
        checks = f'<ul class="checks">{items}</ul>'
    body = f"""
<form method="post" action="/setup/youtube">
  <label class="field" for="youtube">YouTube-каналы, по одному на строку</label>
  <textarea id="youtube" name="youtube" class="short" rows="5" placeholder="@GoogleDevelopers&#10;https://www.youtube.com/@handle&#10;https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw">{_e(view.youtube_text)}</textarea>
  <p class="hint">Принимаются @handle, ссылки youtube.com (@handle, /channel/UC…, /c/…, /user/…) и голый id UC….
  При сохранении каналы проверяются по сети. Новые видео за сутки попадают в блок «Новые видео» в конце номера.
  Шаг необязателен: пустой список — без видео.</p>
  <div class="actions"><button class="btn" type="submit">Сохранить</button></div>
</form>{checks}"""
    stamp = '<span class="stamp nonrepro">необязательно</span>'
    return _panel(view, "youtube", 5, "YouTube-каналы", stamp, "", body)


def _rail_html(view: SetupView, next_key: str | None) -> str:
    """The step list on the left: number, title, status word; the next
    not-done step is highlighted with the marker."""
    items = []
    for n, st in enumerate(view.steps, start=1):
        word = '<span class="meta done">готово</span>' if st.done else '<span class="meta">не настроено</span>'
        nxt = " next" if st.key == next_key else ""
        items.append(
            f'<li class="{nxt.strip()}"><a href="#step-{st.key}"><span class="rail-label">'
            f'<span class="rail-n">{n}</span><span class="rail-title">{_e(STEP_TITLES[st.key])}</span></span>{word}</a></li>'
        )
    items.append(
        '<li><a href="#step-youtube"><span class="rail-label"><span class="rail-n">5</span>'
        '<span class="rail-title">YouTube</span></span><span class="meta">необязательно</span></a></li>'
    )
    return f'<nav class="rail" aria-label="Шаги настройки"><ol>{"".join(items)}</ol></nav>'


def render_setup_page(view: SetupView) -> str:
    all_done = all(s.done for s in view.steps)
    next_key = None
    if all_done:
        banner = (
            '<div class="attn"><strong>Всё настроено.</strong> Следующий шаг — короткий онбординг '
            'по интересам, он подбирает газету под вас. <a href="/onboarding">Перейти к онбордингу</a>. '
            '<a href="/">На главную</a></div>'
        )
    else:
        nxt = next(s for s in view.steps if not s.done)
        next_key = nxt.key
        banner = (
            f'<div class="attn"><strong>Проект ещё не настроен.</strong> Следующий шаг — '
            f'<a href="#step-{nxt.key}">{_e(STEP_TITLES[nxt.key])}</a>: {_e(nxt.detail)}.</div>'
        )
    cards = "\n".join(
        (_telegram_card(view), _login_card(view), _gemini_card(view), _channels_card(view), _youtube_card(view))
    )
    body = f"""
  <h1>Настройка</h1>
  <p class="lede">Любой шаг можно пройти заново. Секреты хранятся локально в .env и в файле сессии
  (права 0600), на странице показываются только в маске.</p>
  {banner}
  <div class="setup-grid">
    {_rail_html(view, next_key)}
    <div class="steps">
    {cards}
    </div>
  </div>
"""
    return page("настройка", body, active="setup")
