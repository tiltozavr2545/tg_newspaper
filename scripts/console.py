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
TG_NEWSPAPER_CHANNELS, TG_NEWSPAPER_SESSION_DIR (см. config.py).

Первый запуск: пока проект не настроен (нет Telegram API, входа в аккаунт,
ключа Gemini или списка каналов), все страницы ведут на мастер настройки
/setup — он же доступен позже по ссылке "Настройки" на главной.
"""

from __future__ import annotations

import argparse
import http.server
import logging
import re
import threading
import traceback
import webbrowser
from urllib.parse import parse_qs

from tg_newspaper import setup_wizard as wizard
from tg_newspaper.config import channels_path, env_path, load_config
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
from tg_newspaper.pipeline import load_run, run_pipeline_for_last_24h
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
    load_survey,
    load_survey_items,
    save_profile,
    save_survey_answers,
)

PORT = 8420

logger = logging.getLogger(__name__)

# Результаты последних действий мастера ({шаг: StepResult}) — "flash": POST
# отвечает редиректом (чтобы F5 не повторял отправку), а сообщение ждёт
# следующего GET. Одна консоль — один человек, глобальный словарь достаточен.
_flash: dict[str, wizard.StepResult] = {}
_channel_checks: list[tuple[str, wizard.StepResult]] = []
_flash_lock = threading.Lock()

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

    def _read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        # parse_qs отдаёт списки; поля формы у нас одиночные — берём первое.
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
        """Из полей формы — места по постам опроса ({ключ: место|None}) и
        сырые значения полей (чтобы вернуть форму с сохранёнными ответами)."""
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
        if load_profile(conn).onboarded:  # повторная отправка/двойной клик — не перезаписываем
            self._redirect("/")
            return
        form = self._read_form()
        interests = form.get("interests", "").strip()
        disinterests = form.get("disinterests", "").strip()
        # Раунды берём из уже созданных (не создаём новые): читатель отвечает
        # на те посты, что видел, даже если в базе за это время появились другие.
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


    # ------------------------------------------------------------ мастер настройки

    def _setup_gate(self) -> bool:
        """True, если проект настроен. Иначе отвечает редиректом на мастер с
        указанием следующего шага и возвращает False. Проверка не падает при
        отсутствии .env/channels.yaml/сессии (в отличие от load_config)."""
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
        _, who = wizard.session_account() if any(s.key == wizard.STEP_LOGIN and s.done for s in steps) else (False, "")
        channels = wizard.load_channels(channels_path())
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
            channels_text="\n".join(channels),
            flash=flash,
            channel_checks=checks,
        )
        self._send_html(render_setup_page(view))

    def _handle_setup_post(self) -> None:
        form = self._read_form()
        path = self.path.partition("?")[0]
        # Секреты в логи не пишем: логируется только путь, не форма.

        if path == "/setup/telegram":
            api_id = form.get("api_id", "").strip()
            api_hash = form.get("api_hash", "").strip()  # пустой = не менять
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
                wizard.LOGIN.reset()  # другие реквизиты/имя сессии — прежний вход не в счёт
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
            channels, invalid = wizard.normalize_channels(form.get("channels", ""))
            if invalid:
                self._flash_set(wizard.STEP_CHANNELS, wizard.StepResult(
                    False, "Не удалось разобрать: " + ", ".join(invalid) + ". Список не сохранён."))
            elif not channels:
                self._flash_set(wizard.STEP_CHANNELS, wizard.StepResult(False, "Укажите хотя бы один канал."))
            else:
                wizard.save_channels(channels_path(), channels)
                self._flash_set(wizard.STEP_CHANNELS, wizard.StepResult(True, f"Сохранено каналов: {len(channels)}."))
                if form.get("action") == "check":
                    results = wizard.check_channels(channels)
                    with _flash_lock:
                        _channel_checks[:] = results
        else:
            self._send_html("not found", status=404)
            return
        step = wizard.STEP_LOGIN if path.startswith("/setup/login") else path.rsplit("/", 1)[-1]
        self._setup_redirect(step)

    def do_GET(self) -> None:  # noqa: N802 (имя метода задано http.server)
        if self.path == "/setup" or self.path.startswith("/setup?"):
            self._handle_setup_get()
            return
        if not self._setup_gate():
            return
        config = load_config()
        conn = connect(config.db_path)

        if self.path in ("/", "/index.html"):
            run_id = latest_issue_run_id(conn)
            # Опрос предлагаем, пока по последнему собранному номеру нет
            # отправленного (неотправленный/несозданный — предлагаем).
            pending = run_id is not None and not any(
                s.submitted for s in list_surveys(conn, "issue", run_id=run_id)
            )
            self._send_html(
                render_index_page(
                    list_runs(conn),
                    onboarded=load_profile(conn).onboarded,
                    issue_survey_run_id=run_id if pending else None,
                    learning=learning_stats(conn),
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
            # Ленивое создание: первый заход фиксирует 5 постов в БД, дальше
            # тот же опрос (create_issue_survey идемпотентна).
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
                    outcomes, run_id, run.started_at, runs, run.period_start, run.period_end
                )
            )
            return

        self._send_html("not found", status=404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/setup/"):
            self._handle_setup_post()
            return
        if not self._setup_gate():
            return
        if self.path == "/onboarding":
            self._handle_onboarding_post(connect(load_config().db_path))
            return

        if self.path == "/onboarding/skip":
            # Пропуск ставит onboarded_at с пустыми ответами — иначе отказ от
            # онбординга навсегда заблокировал бы запуск прогона.
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
            if survey.submitted:  # повторная отправка не перезаписывает ответ
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

        if self.path != "/run":
            self._send_html("not found", status=404)
            return

        config = load_config()
        # Без онбординга прогон не стартует (Этап 6): профиль нужен персональной
        # оценке; "Пропустить" на странице онбординга снимает блокировку.
        if not load_profile(connect(config.db_path)).onboarded:
            self._redirect("/onboarding")
            return
        if not config.gemini_api_key:
            self._send_html(
                render_error_page(
                    "GEMINI_API_KEY не задан в .env — без него нельзя прогнать "
                    "пайплайн (LLM-классификация и дедупликация)."
                ),
                status=400,
            )
            return

        logger.info("получена команда 'Собрать газету' — запускаю прогон")
        try:
            result = run_pipeline_for_last_24h(config)
        except Exception as exc:  # noqa: BLE001 — показываем причину в браузере, не роняем сервер
            logger.exception("прогон завершился с ошибкой")
            self._send_html(render_error_page(f"{exc}\n\n{traceback.format_exc()}"), status=500)
            return

        included = sum(1 for o in result.outcomes if o.included)
        logger.info(
            "прогон #%d завершён: постов=%d пошло=%d",
            result.run_id, len(result.outcomes), included,
        )
        self._redirect(run_page_filename(result.run_id))

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — сигнатура базового класса
        logger.info("%s - %s", self.address_string(), format % args)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    port = args.port

    url = f"http://localhost:{port}/"
    print(f"Открываю {url} (Ctrl+C — остановить сервер)")
    if not args.no_browser:
        webbrowser.open(url)
    # ThreadingHTTPServer, не HTTPServer: однопоточный сервер обслуживает
    # запросы по одному — зависшее или незакрытое соединение (например,
    # keep-alive от браузера) блокирует вообще все следующие запросы,
    # проверено на практике.
    with http.server.ThreadingHTTPServer(("localhost", port), ConsoleHandler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nОстановлено.")


if __name__ == "__main__":
    main()
