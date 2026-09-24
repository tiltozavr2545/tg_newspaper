"""CLI-эквивалент кнопки "Собрать газету" из локальной консоли
(scripts/console.py) — на случай, когда нужен headless-запуск из терминала,
без браузера. Оба пути вызывают один и тот же tg_newspaper.pipeline —
раздельной логики нет.

Запускается вручную, по требованию — не по расписанию (см. AGENTS.md:
проект больше не собирает газету автоматически раз в сутки, только по
явной команде пользователя, будь то нажатие кнопки или запуск этого CLI).
"""

import logging

from .config import load_config
from .pipeline import run_pipeline_for_last_24h

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = load_config()
    if not config.gemini_api_key:
        raise SystemExit(
            "GEMINI_API_KEY не задан в .env — без него нельзя прогнать пайплайн "
            "(LLM-классификация и дедупликация)."
        )

    logger.info("команда 'Собрать газету': каналов=%d, окно=последние 24 часа", len(config.channels))
    result = run_pipeline_for_last_24h(config)

    included = [o for o in result.outcomes if o.included]
    excluded = [o for o in result.outcomes if not o.included]

    print(f"\nОкно сбора: {result.period_start.isoformat()} — {result.period_end.isoformat()}")
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
