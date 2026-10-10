"""
indexer.py
──────────
Runs one Drive sync from the command line. You normally don't need this — the
server syncs on its own every SYNC_INTERVAL_MINUTES — but it's handy for the
very first load into a new database, or to check that Drive access works.

Usage:
    python indexer.py           # read new/changed files, drop deleted ones
    python indexer.py --full    # re-read every file
"""

import sys
import logging

from dotenv import load_dotenv

load_dotenv()

from doc_store import DocStore            # noqa: E402
from drive_sync import sync_from_env      # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def main() -> int:
    full = "--full" in sys.argv[1:]
    store = DocStore()
    try:
        result = sync_from_env(store, full=full)
    except Exception as e:
        logger.error(f"Sync failed: {e}")
        return 1

    logger.info("=" * 60)
    logger.info(f"Files in Drive:   {result.total_files}")
    logger.info(f"Added:            {result.added}")
    logger.info(f"Updated:          {result.updated}")
    logger.info(f"Renamed/moved:    {result.renamed}")
    logger.info(f"Removed:          {result.removed}")
    logger.info(f"Failed:           {result.failed}")
    logger.info(f"By status:        {store.counts()}")
    logger.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
