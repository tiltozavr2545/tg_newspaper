"""
Ручная (пере)сборка номера по уже сохранённому прогону, ничего заново не
собирая через Telethon: то же, что делает кнопка "Свёрстать номер" в консоли.
В отличие от остального просмотра прогонов, МОЖЕТ обратиться к Gemini —
если новости не помещаются в отведённый объём, часть из них сокращается
нейронкой (см. pipeline.build_newspaper); это обычный, не дополнительный
вызов LLM для этого шага.

Запуск: uv run python scripts/render_preview.py [run_id] [--pages N]
Без run_id — берёт последний сохранённый прогон.
Результат — data/issues/run_<id>/page_1.png, page_2.png, ... (не больше
--pages полос мозаики плюс обложка); старые полосы и PDF этого прогона
перед сборкой удаляются.
Побочный эффект: состав собранного номера записывается в БД (issue_posts) как
"уже напечатанное" — по нему дедуплицируются следующие прогоны. Повторный
запуск по тому же прогону заменяет состав.
"""

from __future__ import annotations

import argparse

from tg_newspaper.config import load_config
from tg_newspaper.pipeline import NEWSPAPER_MAX_PAGES, assemble_issue, load_run
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

    included_count = sum(1 for o in load_run(conn, run.run_id) if o.included)
    result = assemble_issue(config, conn, run, max_pages=args.pages)
    if result is None:
        raise SystemExit(f"прогон #{run.run_id} не дал ни одной новости для газеты")
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
