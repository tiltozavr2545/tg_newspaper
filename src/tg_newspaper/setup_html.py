"""Страница мастера первоначальной настройки (логика — в setup_wizard.py).

Вёрстка — тем же подходом, что report_html.py/feedback_html.py: общий STYLE и
небольшой EXTRA_STYLE, без внешних JS/CSS. Все значения экранируются;
секреты сюда приходят уже маскированными (mask_secret) и в поля ввода не
подставляются — пустое поле секрета означает "не менять".
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field

from .feedback_html import EXTRA_STYLE
from .report_html import STYLE
from .setup_wizard import (
    STEP_CHANNELS,
    STEP_GEMINI,
    STEP_LOGIN,
    STEP_TELEGRAM,
    STEP_TITLES,
    StepResult,
    StepStatus,
)

SETUP_STYLE = """
<style>
  input[type=text], input[type=password], input[type=tel] {
    width: 100%; box-sizing: border-box; padding: 8px 10px; border: 1px solid #d0d0d5;
    border-radius: 6px; font: inherit; font-size: 13px; }
  .card.current { border-color: #2657d6; box-shadow: 0 0 0 2px #dbe5fb; }
  .card h2 .badge { margin-left: 8px; vertical-align: middle; }
  .notice { border-radius: 8px; padding: 10px 14px; margin: 10px 0; font-size: 13px;
            white-space: pre-wrap; }
  .notice.ok { background: #e3f6e5; border: 1px solid #b7e0bc; color: #1a5a26; }
  .notice.err { background: #fbe8e8; border: 1px solid #f0b8b8; color: #7a1a1a; }
  ul.checks { margin: 8px 0 0; padding-left: 20px; font-size: 13px; }
  ul.checks li.ok { color: #1a5a26; } ul.checks li.err { color: #7a1a1a; }
</style>
"""


@dataclass
class SetupView:
    """Всё, что нужно странице: статус шагов, что показать в полях и
    результаты последних действий (flash — по ключу шага)."""
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


def _e(value: str) -> str:
    return html.escape(value, quote=True)


def _notice(result: StepResult | None) -> str:
    if result is None:
        return ""
    cls = "ok" if result.ok else "err"
    return f'<div class="notice {cls}">{_e(result.message)}</div>'


def _secret_hint(masked: str) -> str:
    if masked:
        return f'<p class="hint">Сейчас задан: <code>{_e(masked)}</code>. Оставьте поле пустым, чтобы не менять.</p>'
    return '<p class="hint">Пока не задан.</p>'


def _card(view: SetupView, key: str, number: int, body: str) -> str:
    status = next(s for s in view.steps if s.key == key)
    badge = (
        '<span class="badge in">готово</span>' if status.done else '<span class="badge out">не настроено</span>'
    )
    current = " current" if view.highlight == key else ""
    return (
        f'<div class="card{current}" id="step-{key}">'
        f"<h2>{number}. {_e(STEP_TITLES[key])}{badge}</h2>"
        f'<p class="hint">{_e(status.detail)}</p>'
        f"{_notice(view.flash.get(key))}{body}</div>"
    )


def _telegram_card(view: SetupView) -> str:
    body = f"""
<form method="post" action="/setup/telegram">
  <p class="hint">Получить на <a href="https://my.telegram.org" target="_blank" rel="noopener">my.telegram.org</a>
  → «API development tools». Заходите под <strong>отдельным аккаунтом проекта</strong>, не личным:
  сессия Telethon — это ключ к аккаунту (см. AGENTS.md). Бот-токен не нужен — сбор идёт под пользовательским аккаунтом.</p>
  <label class="field" for="api_id">api_id (целое число)</label>
  <input type="text" id="api_id" name="api_id" value="{_e(view.api_id)}" inputmode="numeric" autocomplete="off">
  <label class="field" for="api_hash">api_hash</label>
  <input type="password" id="api_hash" name="api_hash" value="" autocomplete="off">
  {_secret_hint(view.api_hash_masked)}
  <label class="field" for="session_name">Имя файла сессии</label>
  <input type="text" id="session_name" name="session_name" value="{_e(view.session_name)}" autocomplete="off">
  <p class="hint">Буквы, цифры, «_», «-», «.»; по умолчанию tg_newspaper.</p>
  <div class="actions"><button class="submit" type="submit">Сохранить</button></div>
</form>"""
    return _card(view, STEP_TELEGRAM, 1, body)


def _login_card(view: SetupView) -> str:
    status = next(s for s in view.steps if s.key == STEP_LOGIN)
    phone_form = """
<form method="post" action="/setup/login/phone">
  <label class="field" for="phone">Номер телефона аккаунта проекта</label>
  <input type="tel" id="phone" name="phone" placeholder="+79991234567" autocomplete="off">
  <div class="actions" style="margin-top:10px"><button class="submit" type="submit">Отправить код</button></div>
</form>"""
    if view.login_stage == "code":
        body = """
<form method="post" action="/setup/login/code">
  <label class="field" for="code">Код из Telegram</label>
  <input type="text" id="code" name="code" inputmode="numeric" autocomplete="off">
  <div class="actions" style="margin-top:10px">
    <button class="submit" type="submit">Войти</button>
    <button class="secondary" type="submit" formaction="/setup/login/cancel" formnovalidate>Начать заново</button>
  </div>
</form>"""
    elif view.login_stage == "password":
        body = """
<form method="post" action="/setup/login/password">
  <label class="field" for="password">Пароль двухфакторной защиты</label>
  <input type="password" id="password" name="password" autocomplete="off">
  <div class="actions" style="margin-top:10px">
    <button class="submit" type="submit">Войти</button>
    <button class="secondary" type="submit" formaction="/setup/login/cancel" formnovalidate>Начать заново</button>
  </div>
</form>"""
    elif status.done and view.login_who:
        body = f"""
<p><strong>Вход выполнен: {_e(view.login_who)}</strong></p>
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
    <button class="submit" type="submit" name="action" value="save">Сохранить</button>
    <button class="secondary" type="submit" name="action" value="check">Сохранить и проверить</button>
  </div>
</form>"""
    return _card(view, STEP_GEMINI, 3, body)


def _channels_card(view: SetupView) -> str:
    checks = ""
    if view.channel_checks:
        items = "".join(
            f'<li class="{"ok" if r.ok else "err"}">@{_e(ch)} — {_e(r.message)}</li>'
            for ch, r in view.channel_checks
        )
        checks = f'<ul class="checks">{items}</ul>'
    body = f"""
<form method="post" action="/setup/channels">
  <label class="field" for="channels">Каналы-источники, по одному на строку</label>
  <textarea id="channels" name="channels" rows="8" placeholder="durov&#10;@telegram&#10;https://t.me/example_channel">{_e(view.channels_text)}</textarea>
  <p class="hint">Принимаются username, @username и ссылки t.me/&lt;username&gt; — сохраняется чистый username.
  Список пишется в config/channels.yaml.</p>
  <div class="actions">
    <button class="submit" type="submit" name="action" value="save">Сохранить</button>
    <button class="secondary" type="submit" name="action" value="check">Сохранить и проверить доступ</button>
  </div>
</form>{checks}"""
    return _card(view, STEP_CHANNELS, 4, body)


def render_setup_page(view: SetupView) -> str:
    all_done = all(s.done for s in view.steps)
    if all_done:
        footer = (
            '<div class="banner info"><strong>Всё настроено.</strong> Следующий шаг — короткий онбординг '
            'по интересам, он подбирает газету под вас. <a href="/onboarding">Перейти к онбордингу →</a> '
            '· <a href="/">На главную</a></div>'
        )
        intro = ""
    else:
        nxt = next(s for s in view.steps if not s.done)
        intro = (
            f'<div class="banner"><strong>Проект ещё не настроен.</strong> Следующий шаг — '
            f'<a href="#step-{nxt.key}">{_e(STEP_TITLES[nxt.key])}</a>: {_e(nxt.detail)}.</div>'
        )
        footer = ""
    cards = "\n".join(
        (_telegram_card(view), _login_card(view), _gemini_card(view), _channels_card(view))
    )
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>TG Newspaper — настройка</title>
{STYLE}
{EXTRA_STYLE}
{SETUP_STYLE}
</head>
<body>
  <h1>Настройка TG Newspaper</h1>
  <p class="subtitle">Любой шаг можно пройти заново. Секреты хранятся локально в .env и в файле сессии
  (права 0600), на странице показываются только в маске.</p>
  {intro}
  {cards}
  {footer}
  <p><a href="/">← на главную</a></p>
</body>
</html>"""
