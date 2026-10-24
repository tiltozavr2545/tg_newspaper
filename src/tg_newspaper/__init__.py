"""CLI equivalent of the "Assemble newspaper" button from the local console
(scripts/console.py) — for cases where a headless run from the terminal is
needed, without a browser. Post selection and issue layout use the same
tg_newspaper.pipeline functions as the button: there is no separate logic.

Run manually, on demand — not on a schedule (see AGENTS.md: the project no
longer assembles the newspaper automatically once a day, only on the user's
explicit command, whether that's clicking the button or running this CLI).
"""

import logging

from .config import load_config
from .layout import to_local
from .pipeline import assemble_issue, few_news_message, issue_dir, run_pipeline_for_last_24h
from .storage import connect, list_runs

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = load_config()
    if not config.gemini_api_key:
        raise SystemExit(
            "GEMINI_API_KEY не задан в .env — без него нельзя прогнать пайплайн "
            "(LLM-классификация и дедупликация)."
        )

    logger.info("'assemble newspaper' command: channels=%d, window=last 24 hours", len(config.channels))
    result = run_pipeline_for_last_24h(config)

    included = [o for o in result.outcomes if o.included]
    excluded = [o for o in result.outcomes if not o.included]

    fmt = "%Y-%m-%d %H:%M"
    print(f"\nОкно сбора: {to_local(result.period_start).strftime(fmt)} — {to_local(result.period_end).strftime(fmt)}")
    print(f"Всего постов: {len(result.outcomes)}")
    print(f"Пошли в газету: {len(included)}")
    print(f"Отсеяны: {len(excluded)}")

    by_stage: dict[str, int] = {}
    for o in excluded:
        by_stage[o.stage] = by_stage.get(o.stage, 0) + 1
    for stage, count in sorted(by_stage.items(), key=lambda kv: -kv[1]):
        print(f"  {stage}: {count}")

    print(f"\nСохранено как прогон #{result.run_id}.")
    print("Смотреть: uv run python scripts/console.py")

    # The selection is already saved to the DB; a layout failure (Gemini/Chromium)
    # does not undo it — explain how to re-render the issue instead of a bare traceback.
    try:
        conn = connect(config.db_path)
        run = next(r for r in list_runs(conn) if r.run_id == result.run_id)
        issue = assemble_issue(config, conn, run)
    except Exception as exc:  # noqa: BLE001
        logger.exception("issue assembly failed")
        print(
            f"\nНомер свёрстать не удалось: {exc}\n"
            f"Отбор сохранён; повторить вёрстку: uv run python scripts/render_preview.py {result.run_id}"
        )
        return
    if issue is None:
        print(f"\n{few_news_message(len(included))}")
        return
    print(f"\nНомер: {len(issue.pages)} полос → {issue_dir(config, result.run_id)}")
