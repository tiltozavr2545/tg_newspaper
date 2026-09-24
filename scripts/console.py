"""
Локальная консоль управления газетой — кнопка "Собрать газету" (полный
прогон за последние 24 часа: Telethon → эвристики → LLM-классификация →
дедупликация) и просмотр истории уже сохранённых прогонов.

Просмотр списка и открытие прошлого прогона не обращается к LLM/Telethon —
только кнопка "Собрать газету" запускает реальный прогон.

Запуск: uv run python scripts/console.py
Открывает http://localhost:8420/ — Ctrl+C останавливает сервер.
"""

from __future__ import annotations

import http.server
import logging
import traceback
import webbrowser

from tg_newspaper.config import load_config
from tg_newspaper.pipeline import load_run, run_pipeline_for_last_24h
from tg_newspaper.report_html import (
    render_error_page,
    render_index_page,
    render_run_page,
    run_page_filename,
)
from tg_newspaper.storage import connect, list_runs

PORT = 8420

logger = logging.getLogger(__name__)


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

    def do_GET(self) -> None:  # noqa: N802 (имя метода задано http.server)
        config = load_config()
        conn = connect(config.db_path)

        if self.path in ("/", "/index.html"):
            self._send_html(render_index_page(list_runs(conn)))
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
        if self.path != "/run":
            self._send_html("not found", status=404)
            return

        config = load_config()
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

    url = f"http://localhost:{PORT}/"
    print(f"Открываю {url} (Ctrl+C — остановить сервер)")
    webbrowser.open(url)
    # ThreadingHTTPServer, не HTTPServer: однопоточный сервер обслуживает
    # запросы по одному — зависшее или незакрытое соединение (например,
    # keep-alive от браузера) блокирует вообще все следующие запросы,
    # проверено на практике.
    with http.server.ThreadingHTTPServer(("localhost", PORT), ConsoleHandler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nОстановлено.")


if __name__ == "__main__":
    main()
