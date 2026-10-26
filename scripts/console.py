"""
Локальная консоль управления газетой — кнопка "Собрать газету" (полный
прогон за последние 24 часа: Telethon → эвристики → LLM-классификация →
дедупликация) и просмотр истории уже сохранённых прогонов.

Просмотр списка и открытие прошлого прогона не обращается к LLM/Telethon —
только кнопка "Собрать газету" запускает реальный прогон.

Этап 6: пока читатель не прошёл онбординг (/onboarding), прогон не
запускается; после каждого собранного номера на главной появляется опрос
"расставьте 5 постов по важности" (/issue_survey/<run_id>), по его итогам —
страница сравнения с порядком модели. Профиль и отзывы лежат в той же БД.

Запуск: uv run python scripts/console.py
Открывает http://localhost:8420/ — Ctrl+C останавливает сервер.
Флаги: --no-browser (не открывать браузер), --port N. Путь к БД можно
переопределить переменной окружения TG_NEWSPAPER_DB (для проверки на копии);
пути .env, channels.yaml и каталога файла сессии — TG_NEWSPAPER_ENV,
TG_NEWSPAPER_CHANNELS, TG_NEWSPAPER_SESSION_DIR (см. config.py); все данные
пользователя разом — TG_NEWSPAPER_HOME (так работает установленный релиз).

Обычно консоль запускает приложение "TG Newspaper" (scripts/make_app.sh →
scripts/launch.sh): сервер живёт в фоне без окна, останавливается кнопкой
"Выключить консоль" на главной (POST /shutdown). Каталог лога и pid-файла —
TG_NEWSPAPER_RUN_DIR (по умолчанию data/).

Первый запуск: пока проект не настроен (нет Telegram API, входа в аккаунт,
ключа Gemini или списка каналов), все страницы ведут на мастер настройки
/setup — он же доступен позже по ссылке "Настройки" на главной.
"""

from __future__ import annotations

import argparse
import http.server
import importlib.resources
import logging
import os
import re
import threading
import traceback
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs

from tg_newspaper import setup_wizard as wizard
from tg_newspaper.config import channels_path, env_path, home_dir, load_config
from tg_newspaper.console_theme import page
from tg_newspaper.feedback import (
    agreement,
    create_issue_survey,
    create_onboarding_surveys,
    learning_stats,
    load_item_texts,
    validate_ranks,
)
from tg_newspaper.feedback_html import (
    ItemView,
    rank_field_name,
    render_message_page,
    render_onboarding_page,
    render_result_page,
    render_survey_page,
)
from tg_newspaper.issue_pdf import build_pdf
from tg_newspaper.layout import edition_slug, to_local
from tg_newspaper.pipeline import (
    assemble_issue,
    issue_dir,
    issue_pages,
    load_run,
    run_pipeline_for_last_24h,
)
from tg_newspaper.report_html import (
    render_error_page,
    render_index_page,
    render_run_page,
    run_page_filename,
)
from tg_newspaper.setup_html import SetupView, render_setup_page
from tg_newspaper.storage import (
    complete_onboarding,
    connect,
    latest_issue_run_id,
    list_runs,
    list_surveys,
    load_profile,
    load_run_videos,
    load_survey,
    load_survey_items,
    save_profile,
    save_survey_answers,
)

PORT = 8420

_SHUTDOWN_PAGE = page(
    "консоль остановлена",
    '<h1>Консоль остановлена</h1><p class="lede">Запустить снова — иконкой TG Newspaper.</p>',
    nav=False,
    footer=False,
)

logger = logging.getLogger(__name__)

# Results of the latest wizard actions ({step: StepResult}) — "flash": POST
# responds with a redirect (so F5 doesn't repeat the submission), and the
# message waits for the next GET. One console — one person, a global dict is enough.
_flash: dict[str, wizard.StepResult] = {}
_channel_checks: list[tuple[str, wizard.StepResult]] = []
_youtube_checks: list[tuple[str, wizard.StepResult]] = []
# Entered text on a parse/resolve error — put it back in the textarea instead of losing the input.
_drafts: dict[str, str] = {}
_flash_lock = threading.Lock()

# Issue assembly errors ({run_id: text}) — also "flash": the selection is
# already saved by this point, so POST still redirects to the run page, and
# the reason waits there until the next render.
_render_errors: dict[int, str] = {}
# The PDF is built in Chromium; two concurrent requests for the same file
# must not write it in parallel.
_pdf_lock = threading.Lock()

_ISSUE_PNG_RE = re.compile(r"/issue/([0-9]+)/page_([0-9]+)\.png")
_ISSUE_PDF_RE = re.compile(r"/issue/([0-9]+)/(a4|a3)\.pdf")
_RENDER_RE = re.compile(r"/run_([0-9]+)/render")

# Bundled fonts: a strict whitelist pattern, so the name can never be a path.
_FONT_RE = re.compile(r"/static/fonts/([a-z0-9-]+\.woff2)")

_SESSION_NAME_RE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")


class ConsoleHandler(http.server.BaseHTTPRequestHandler):
    def _send_html(self, body: str, status: int = 200) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def _send_file(self, data: bytes, content_type: str, filename: str | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def _serve_font(self, name: str) -> None:
        """Serves a bundled woff2 from the package (needed before the setup
        gate: the wizard page uses the fonts too)."""
        font = importlib.resources.files("tg_newspaper") / "static" / "fonts" / name
        if not font.is_file():
            self._send_html("not found", status=404)
            return
        data = font.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "font/woff2")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=31536000")
        self.end_headers()
        self.wfile.write(data)

    def _serve_issue_file(self, conn, config, path: str) -> bool:
        """Serves issue page images and PDF. Paths are built from numbers, not
        from the request string (protection against path traversal). True — request handled."""
        m_png = _ISSUE_PNG_RE.fullmatch(path)
        m_pdf = _ISSUE_PDF_RE.fullmatch(path)
        if not (m_png or m_pdf):
            return False
        run_id = int((m_png or m_pdf).group(1))
        pages = issue_pages(config, run_id)
        if m_png:
            png = issue_dir(config, run_id) / f"page_{int(m_png.group(2))}.png"
            if png not in pages:
                self._send_html("not found", status=404)
                return True
            self._send_file(png.read_bytes(), "image/png")
            return True

        kind = m_pdf.group(2)
        run = next((r for r in list_runs(conn) if r.run_id == run_id), None)
        if run is None or not pages or (kind == "a3" and len(pages) % 2):
            self._send_html("not found", status=404)
            return True
        pdf = issue_dir(config, run_id) / f"{kind}.pdf"
        try:
            with _pdf_lock:
                if not pdf.exists():
                    build_pdf(pages, pdf, sheets=(kind == "a3"))
        except Exception as exc:  # noqa: BLE001 — we show the reason, don't crash the server
            logger.exception("failed to build PDF for issue #%d", run_id)
            self._send_html(render_error_page(f"Не удалось собрать PDF: {exc}"), status=500)
            return True
        moment = run.period_end or run.started_at
        date = to_local(moment).strftime("%Y-%m-%d")
        self._send_file(
            pdf.read_bytes(), "application/pdf",
            f"tg-newspaper-{date}-{edition_slug(moment)}-{kind}.pdf",
        )
        return True

    def _ready_for_llm(self, config) -> bool:
        """Common check before anything that talks to Gemini: onboarding is
        done and the key is set. Otherwise responds itself with a redirect/error page."""
        # Without onboarding the run doesn't start (Stage 6): the profile is
        # needed for personalized scoring; "Skip" on the onboarding page lifts the block.
        if not load_profile(connect(config.db_path)).onboarded:
            self._redirect("/onboarding")
            return False
        if not config.gemini_api_key:
            self._send_html(
                render_error_page(
                    "GEMINI_API_KEY не задан в .env — без него нельзя прогнать "
                    "пайплайн (LLM-классификация и дедупликация)."
                ),
                status=400,
            )
            return False
        return True

    @staticmethod
    def _try_assemble(config, run_id: int) -> None:
        """Assembles the issue; a failure (Gemini/Chromium) is not propagated
        but stored in _render_errors — the selection is already saved, the run page will show the reason."""
        conn = connect(config.db_path)
        try:
            run = next(r for r in list_runs(conn) if r.run_id == run_id)
            assemble_issue(config, conn, run)
            _render_errors.pop(run_id, None)
        except Exception as exc:  # noqa: BLE001
            logger.exception("assembling issue for run #%d failed", run_id)
            _render_errors[run_id] = f"{exc}\n\n{traceback.format_exc()}"

    def _read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        # parse_qs returns lists; our form fields are single-valued — take the first.
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    @staticmethod
    def _views(conn, survey) -> list[ItemView]:
        items = load_survey_items(conn, survey.survey_id)
        texts = load_item_texts(
            conn, survey.kind, survey.run_id, [(i.channel, i.message_id) for i in items]
        )
        return [ItemView(i, texts[(i.channel, i.message_id)]) for i in items]

    @staticmethod
    def _collect_ranks(form, survey, views):
        """From the form fields — ranks for the survey posts ({key: rank|None})
        and the raw field values (to return the form with saved answers)."""
        ranks, selected = {}, {}
        for v in views:
            name = rank_field_name(survey.survey_id, v.item)
            raw = form.get(name, "").strip()
            selected[name] = raw
            ranks[(v.item.channel, v.item.message_id)] = int(raw) if raw.isdigit() else None
        return ranks, selected

    @staticmethod
    def _agreement_for(views, ranks) -> float | None:
        keys = [(v.item.channel, v.item.message_id) for v in views]
        return agreement([ranks[k] for k in keys], [v.item.model_score for v in views])

    def _handle_onboarding_get(self, conn) -> None:
        profile = load_profile(conn)
        if profile.onboarded:
            self._redirect("/")
            return
        rounds = []
        for sid in create_onboarding_surveys(conn):
            survey = load_survey(conn, sid)
            rounds.append((sid, self._views(conn, survey)))
        self._send_html(render_onboarding_page(profile.interests, profile.disinterests, rounds))

    def _handle_onboarding_post(self, conn) -> None:
        if load_profile(conn).onboarded:  # resubmission/double click — don't overwrite
            self._redirect("/")
            return
        form = self._read_form()
        interests = form.get("interests", "").strip()
        disinterests = form.get("disinterests", "").strip()
        # Rounds are taken from the ones already created (no new ones created):
        # the reader answers about the posts they saw, even if others appeared in the DB meanwhile.
        surveys = list_surveys(conn, "onboarding", only_pending=True)
        rounds, answers, selected, error = [], [], {}, None
        for survey in surveys:
            views = self._views(conn, survey)
            ranks, sel = self._collect_ranks(form, survey, views)
            selected.update(sel)
            rounds.append((survey.survey_id, views))
            problem = validate_ranks(ranks)
            if problem and error is None:
                error = f"Раунд {len(rounds)}: {problem}"
            answers.append((survey.survey_id, ranks, None if problem else self._agreement_for(views, ranks)))
        if error:
            self._send_html(render_onboarding_page(interests, disinterests, rounds, selected, error), status=400)
            return
        complete_onboarding(conn, interests, disinterests, answers)
        self._redirect("/")


    # ------------------------------------------------------------ setup wizard

    def _setup_gate(self) -> bool:
        """True if the project is set up. Otherwise responds with a redirect
        to the wizard pointing at the next step and returns False. The check
        doesn't fail when .env/channels.yaml/session is missing (unlike load_config)."""
        missing = wizard.next_missing_step(wizard.compute_status())
        if missing is None:
            return True
        self._redirect(f"/setup?step={missing.key}")
        return False

    def _flash_set(self, step: str, result: wizard.StepResult) -> None:
        with _flash_lock:
            _flash[step] = result

    def _setup_redirect(self, step: str) -> None:
        self._redirect(f"/setup?step={step}#step-{step}")

    def _handle_setup_get(self) -> None:
        wizard.load_env_into_process()
        steps = wizard.compute_status()
        query = parse_qs(self.path.partition("?")[2])
        highlight = (query.get("step") or [None])[0]
        if highlight not in wizard.STEP_ORDER:
            nxt = wizard.next_missing_step(steps)
            highlight = nxt.key if nxt else None
        with _flash_lock:
            flash = dict(_flash)
            _flash.clear()
            checks = list(_channel_checks)
            _channel_checks.clear()
            youtube_checks = list(_youtube_checks)
            _youtube_checks.clear()
            drafts = dict(_drafts)
            _drafts.clear()
        _, who = wizard.session_account() if any(s.key == wizard.STEP_LOGIN and s.done for s in steps) else (False, "")
        cf = wizard.load_channels(channels_path())
        view = SetupView(
            steps=steps,
            highlight=highlight,
            api_id=wizard.env_value("TG_API_ID"),
            api_hash_masked=wizard.mask_secret(wizard.env_value("TG_API_HASH")),
            session_name=wizard.env_value("TG_SESSION_NAME", wizard.DEFAULT_SESSION_NAME) or wizard.DEFAULT_SESSION_NAME,
            login_stage=wizard.LOGIN.stage,
            login_who=who,
            gemini_key_masked=wizard.mask_secret(wizard.env_value("GEMINI_API_KEY")),
            gemini_model=wizard.env_value("GEMINI_MODEL", wizard.DEFAULT_GEMINI_MODEL) or wizard.DEFAULT_GEMINI_MODEL,
            gemini_base_url=wizard.env_value("GEMINI_BASE_URL"),
            channels_text=drafts.get("channels", wizard.channels_to_text(cf)),
            youtube_text=drafts.get("youtube", wizard.youtube_to_text(cf.youtube)),
            youtube_checks=youtube_checks,
            flash=flash,
            channel_checks=checks,
        )
        self._send_html(render_setup_page(view))

    def _handle_setup_post(self) -> None:
        form = self._read_form()
        path = self.path.partition("?")[0]
        # We don't write secrets to logs: only the path is logged, not the form.

        if path == "/setup/telegram":
            api_id = form.get("api_id", "").strip()
            api_hash = form.get("api_hash", "").strip()  # empty = don't change
            name = form.get("session_name", "").strip() or wizard.DEFAULT_SESSION_NAME
            if not api_id.isdigit():
                self._flash_set(wizard.STEP_TELEGRAM, wizard.StepResult(False, "api_id должен быть целым числом (только цифры)."))
            elif not api_hash and not wizard.env_value("TG_API_HASH"):
                self._flash_set(wizard.STEP_TELEGRAM, wizard.StepResult(False, "Укажите api_hash."))
            elif not _SESSION_NAME_RE.fullmatch(name):
                self._flash_set(wizard.STEP_TELEGRAM, wizard.StepResult(False, "Имя сессии: только буквы, цифры, «_», «-», «.»."))
            else:
                updates = {"TG_API_ID": api_id, "TG_SESSION_NAME": name}
                if api_hash:
                    updates["TG_API_HASH"] = api_hash
                wizard.update_env_file(env_path(), updates)
                wizard.LOGIN.reset()  # different credentials/session name — the previous login doesn't count
                self._flash_set(wizard.STEP_TELEGRAM, wizard.StepResult(True, "Сохранено."))
            self._setup_redirect(wizard.STEP_TELEGRAM)
            return

        if path == "/setup/login/phone":
            self._flash_set(wizard.STEP_LOGIN, wizard.start_login(form.get("phone", "")))
        elif path == "/setup/login/code":
            self._flash_set(wizard.STEP_LOGIN, wizard.submit_code(form.get("code", "")))
        elif path == "/setup/login/password":
            self._flash_set(wizard.STEP_LOGIN, wizard.submit_password(form.get("password", "")))
        elif path == "/setup/login/cancel":
            wizard.cancel_login()
        elif path == "/setup/gemini":
            key = form.get("gemini_key", "").strip() or wizard.env_value("GEMINI_API_KEY")
            model = form.get("gemini_model", "").strip() or wizard.DEFAULT_GEMINI_MODEL
            base_url = form.get("gemini_base_url", "").strip().rstrip("/")
            if not re.match(r"https://\S+$", base_url):
                self._flash_set(wizard.STEP_GEMINI, wizard.StepResult(False, "Адрес прокси (GEMINI_BASE_URL) обязателен и должен начинаться с https://."))
            elif not key:
                self._flash_set(wizard.STEP_GEMINI, wizard.StepResult(False, "Укажите ключ Gemini."))
            else:
                wizard.update_env_file(
                    env_path(), {"GEMINI_API_KEY": key, "GEMINI_MODEL": model, "GEMINI_BASE_URL": base_url}
                )
                if form.get("action") == "check":
                    self._flash_set(wizard.STEP_GEMINI, wizard.check_gemini(key, model, base_url))
                else:
                    self._flash_set(wizard.STEP_GEMINI, wizard.StepResult(True, "Сохранено."))
        elif path == "/setup/channels":
            text = form.get("channels", "")
            parsed, invalid = wizard.parse_channels_text(text)
            if invalid:
                self._flash_set(wizard.STEP_CHANNELS, wizard.StepResult(
                    False, "Не удалось разобрать: " + ", ".join(invalid) + ". Список не сохранён."))
                with _flash_lock:
                    _drafts["channels"] = text
            elif not parsed.channels:
                self._flash_set(wizard.STEP_CHANNELS, wizard.StepResult(False, "Укажите хотя бы один канал."))
                with _flash_lock:
                    _drafts["channels"] = text
            else:
                # YouTube lives in the same file — the cards must not overwrite each other.
                parsed.youtube = wizard.load_channels(channels_path()).youtube
                wizard.save_channels(channels_path(), parsed)
                channels = parsed.channels
                self._flash_set(wizard.STEP_CHANNELS, wizard.StepResult(True, f"Сохранено каналов: {len(channels)}."))
                if form.get("action") == "check":
                    results = wizard.check_channels(channels)
                    with _flash_lock:
                        _channel_checks[:] = results
        elif path == "/setup/youtube":
            text = form.get("youtube", "")
            cf = wizard.load_channels(channels_path())
            resolved, results = wizard.resolve_youtube_text(text, cf.youtube)
            if any(not r.ok for _, r in results):
                self._flash_set("youtube", wizard.StepResult(False, "Не все каналы распознаны. Список не сохранён."))
                with _flash_lock:
                    _drafts["youtube"] = text
                    _youtube_checks[:] = results
            else:
                cf.youtube = resolved
                wizard.save_channels(channels_path(), cf)
                self._flash_set("youtube", wizard.StepResult(True, f"Сохранено YouTube-каналов: {len(resolved)}."))
                with _flash_lock:
                    _youtube_checks[:] = results
        else:
            self._send_html("not found", status=404)
            return
        step = wizard.STEP_LOGIN if path.startswith("/setup/login") else path.rsplit("/", 1)[-1]
        self._setup_redirect(step)

    def do_GET(self) -> None:  # noqa: N802 (method name dictated by http.server)
        m_font = _FONT_RE.fullmatch(self.path.partition("?")[0])
        if m_font:
            self._serve_font(m_font.group(1))
            return
        if self.path == "/setup" or self.path.startswith("/setup?"):
            self._handle_setup_get()
            return
        if not self._setup_gate():
            return
        config = load_config()
        conn = connect(config.db_path)

        if self.path in ("/", "/index.html"):
            run_id = latest_issue_run_id(conn)
            # We offer the survey as long as there's no submitted one for the
            # latest assembled issue (not submitted/not created — we offer it).
            pending = run_id is not None and not any(
                s.submitted for s in list_surveys(conn, "issue", run_id=run_id)
            )
            runs = list_runs(conn)
            self._send_html(
                render_index_page(
                    runs,
                    onboarded=load_profile(conn).onboarded,
                    issue_survey_run_id=run_id if pending else None,
                    learning=learning_stats(conn),
                    page_counts={r.run_id: len(issue_pages(config, r.run_id)) for r in runs},
                )
            )
            return

        if self.path == "/onboarding":
            self._handle_onboarding_get(conn)
            return

        if self.path.startswith("/issue_survey/"):
            try:
                run_id = int(self.path[len("/issue_survey/"):])
            except ValueError:
                self._send_html("not found", status=404)
                return
            # Lazy creation: the first visit fixes 5 posts in the DB, after
            # that it's the same survey (create_issue_survey is idempotent).
            survey_id = create_issue_survey(conn, run_id)
            if survey_id is None:
                self._send_html(
                    render_message_page("Опрос недоступен", "В номере и среди отобранных слишком мало постов, чтобы их расставлять."),
                    status=404,
                )
                return
            self._redirect(f"/survey/{survey_id}")
            return

        if self.path.startswith("/survey/"):
            try:
                survey = load_survey(conn, int(self.path[len("/survey/"):]))
            except ValueError:
                survey = None
            if survey is None:
                self._send_html("опрос не найден", status=404)
                return
            views = self._views(conn, survey)
            if survey.submitted:
                self._send_html(render_result_page(survey, views))
            else:
                self._send_html(render_survey_page(survey, views))
            return

        if self.path.startswith("/issue/"):
            if not self._serve_issue_file(conn, config, self.path.partition("?")[0]):
                self._send_html("not found", status=404)
            return

        if self.path.startswith("/run_") and self.path.endswith(".html"):
            try:
                run_id = int(self.path[len("/run_") : -len(".html")])
            except ValueError:
                self._send_html("not found", status=404)
                return
            runs = list_runs(conn)
            run = next((r for r in runs if r.run_id == run_id), None)
            if run is None:
                self._send_html("прогон не найден", status=404)
                return
            outcomes = load_run(conn, run_id)
            self._send_html(
                render_run_page(
                    outcomes, run_id, run.started_at, runs, run.period_start, run.period_end,
                    issue_page_count=len(issue_pages(config, run_id)),
                    render_error=_render_errors.pop(run_id, None),
                    videos=load_run_videos(conn, run_id),
                    youtube_enabled=bool(config.youtube_channels),
                )
            )
            return

        self._send_html("not found", status=404)

    def do_POST(self) -> None:  # noqa: N802
        # Shutdown is handled before the setup wizard: the console runs in
        # the background with no window, and it must be stoppable at any stage.
        if self.path == "/shutdown":
            self._send_html(_SHUTDOWN_PAGE)
            # server.shutdown() waits for serve_forever to exit; calling it
            # from the handler thread would deadlock — hence the separate thread.
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if self.path.startswith("/setup/"):
            self._handle_setup_post()
            return
        if not self._setup_gate():
            return
        if self.path == "/onboarding":
            self._handle_onboarding_post(connect(load_config().db_path))
            return

        if self.path == "/onboarding/skip":
            # Skipping sets onboarded_at with empty answers — otherwise
            # declining onboarding would permanently block running the pipeline.
            save_profile(connect(load_config().db_path), "", "", onboarded=True)
            self._redirect("/")
            return

        if self.path.startswith("/survey/"):
            conn = connect(load_config().db_path)
            try:
                survey = load_survey(conn, int(self.path[len("/survey/"):]))
            except ValueError:
                survey = None
            if survey is None:
                self._send_html("опрос не найден", status=404)
                return
            if survey.submitted:  # resubmission doesn't overwrite the answer
                self._redirect(f"/survey/{survey.survey_id}")
                return
            views = self._views(conn, survey)
            ranks, selected = self._collect_ranks(self._read_form(), survey, views)
            problem = validate_ranks(ranks)
            if problem:
                self._send_html(render_survey_page(survey, views, selected, problem), status=400)
                return
            save_survey_answers(conn, survey.survey_id, ranks, self._agreement_for(views, ranks))
            self._redirect(f"/survey/{survey.survey_id}")
            return

        m_render = _RENDER_RE.fullmatch(self.path)
        if m_render:
            # Manual (re)assembly of an issue: for old runs and after a failure.
            run_id = int(m_render.group(1))
            config = load_config()
            if not any(r.run_id == run_id for r in list_runs(connect(config.db_path))):
                self._send_html("прогон не найден", status=404)
                return
            if not self._ready_for_llm(config):
                return
            logger.info("assembling issue for run #%d via button", run_id)
            self._try_assemble(config, run_id)
            self._redirect(run_page_filename(run_id))
            return

        if self.path != "/run":
            self._send_html("not found", status=404)
            return

        config = load_config()
        if not self._ready_for_llm(config):
            return

        logger.info("received 'Собрать газету' command — starting run")
        try:
            result = run_pipeline_for_last_24h(config)
        except Exception as exc:  # noqa: BLE001 — we show the reason in the browser, don't crash the server
            logger.exception("run finished with an error")
            self._send_html(render_error_page(f"{exc}\n\n{traceback.format_exc()}"), status=500)
            return

        included = sum(1 for o in result.outcomes if o.included)
        logger.info(
            "run #%d finished: posts=%d included=%d",
            result.run_id, len(result.outcomes), included,
        )
        # We lay out the issue right away: the selection is already in the DB,
        # so a layout failure doesn't lose the run — the run page will show the
        # error and a "Lay out" button.
        self._try_assemble(config, result.run_id)
        self._redirect(run_page_filename(result.run_id))

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — base class's method signature
        logger.info("%s - %s", self.address_string(), format % args)


def _pid_file() -> Path:
    run_dir = os.environ.get("TG_NEWSPAPER_RUN_DIR")
    # Without RUN_DIR — next to the user's data (in development that's
    # <repo>/data, in a release — <TG_NEWSPAPER_HOME>/data), not next to the program code.
    base = Path(run_dir) if run_dir else home_dir() / "data"
    return base / "console.pid"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    port = args.port

    url = f"http://localhost:{port}/"
    # ThreadingHTTPServer, not HTTPServer: a single-threaded server handles
    # requests one at a time — a stuck or unclosed connection (e.g. browser
    # keep-alive) blocks every subsequent request, confirmed in practice.
    try:
        server = http.server.ThreadingHTTPServer(("localhost", port), ConsoleHandler)
    except OSError:
        # Port in use — almost certainly an already-running console. We don't
        # touch the pid file: it belongs to the running instance.
        print(f"Порт {port} занят — консоль уже запущена: {url}", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        return
    try:
        with server:
            print(f"Консоль: {url} (Ctrl+C — остановить сервер)", flush=True)
            # We open the browser only after bind: otherwise it could get
            # "connection refused" before the server has started.
            if not args.no_browser:
                webbrowser.open(url)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("\nОстановлено.", flush=True)
    finally:
        _pid_file().unlink(missing_ok=True)


if __name__ == "__main__":
    main()
