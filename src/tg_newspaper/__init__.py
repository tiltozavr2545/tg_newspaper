import asyncio
import logging
from datetime import datetime, timezone

from .collector import collect_all, collection_since
from .config import load_config
from .storage import connect, save_posts

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = load_config()
    run_started_at = datetime.now(timezone.utc)
    conn = connect(config.db_path)

    since = collection_since(run_started_at)
    logger.info(
        "старт прогона: каналов=%d период=(%s, %s]",
        len(config.channels),
        since.isoformat(),
        run_started_at.isoformat(),
    )

    posts = asyncio.run(collect_all(config, since))
    save_posts(conn, posts)

    logger.info("прогон завершён: собрано постов=%d", len(posts))
