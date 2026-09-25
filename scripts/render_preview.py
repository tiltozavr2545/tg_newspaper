"""
Ручная сверка вёрстки на реальных данных (Этап 3): собирает печатный номер
фиксированного объёма по уже сохранённому прогону, ничего заново не собирая
через Telethon. В отличие от остального просмотра прогонов, МОЖЕТ обратиться
к Gemini — если новости не помещаются в отведённый объём, часть из них
сокращается нейронкой (см. pipeline.build_newspaper); это обычный, не
дополнительный вызов LLM для этого шага.

Запуск: uv run python scripts/render_preview.py [run_id] [--pages N]
Без run_id — берёт последний сохранённый прогон.
Результат — data/preview_run_<id>_1.png, _2.png, ... (не больше --pages штук).
"""

from __future__ import annotations

import argparse

from tg_newspaper.config import load_config
from tg_newspaper.pipeline import NEWSPAPER_MAX_PAGES, build_newspaper, load_run
from tg_newspaper.storage import connect, list_runs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", nargs="?", type=int, help="ID прогона (по умолчанию — последний)")
    parser.add_argument("--pages", type=int, default=NEWSPAPER_MAX_PAGES, help="жёсткий лимит числа полос")
    args = parser.parse_args()

    config = load_config()
    conn = connect(config.db_path)
    runs = list_runs(conn)
    if not runs:
        raise SystemExit("нет сохранённых прогонов — сначала запустите uv run tg-newspaper")

    if args.run_id is not None:
        run = next((r for r in runs if r.run_id == args.run_id), None)
        if run is None:
            raise SystemExit(f"прогон #{args.run_id} не найден")
    else:
        run = runs[0]

    outcomes = load_run(conn, run.run_id)
    included_count = sum(1 for o in outcomes if o.included)
    if not included_count:
        raise SystemExit(f"прогон #{run.run_id} не дал ни одной новости для газеты")

    result = build_newspaper(
        config,
        outcomes,
        config.db_path.parent,
        basename=f"preview_run_{run.run_id}",
        run_date=run.period_end or run.started_at,
        max_pages=args.pages,
    )
    pages_list = "\n".join(f"  {p}" for p in result.pages)
    print(
        f"Прогон #{run.run_id}: {included_count} новостей → {len(result.pages)} полос(ы) "
        f"(лимит {args.pages}), сокращено нейронкой: {result.shortened_count}, "
        f"выброшено из-за нехватки места: {len(result.dropped)}\n{pages_list}"
    )
    if result.dropped:
        print("\nВыброшено (не поместилось даже после сокращения значимых):")
        for p in result.dropped:
            print(f"  {p.channel}/{p.message_id}: {p.text[:80].replace(chr(10), ' ')}…")


if __name__ == "__main__":
    main()
