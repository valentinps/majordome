"""Worker process: runs every ingester concurrently."""

import asyncio
import logging

from majordome.config import settings
from majordome.ingest.imap import ImapIngester, load_accounts
from majordome.ingest.matrix import MatrixIngester

log = logging.getLogger("majordome.worker")


async def main() -> None:
    cfg = settings()
    tasks = []
    if cfg.matrix_password:
        tasks.append(asyncio.create_task(MatrixIngester(cfg).run(), name="matrix"))
    else:
        log.warning("MAJORDOME_MATRIX_PASSWORD not set: matrix ingestion disabled")
    for acc in load_accounts(cfg):
        tasks.append(asyncio.create_task(ImapIngester(cfg, acc).run(), name=f"imap:{acc.name}"))
    if not tasks:
        raise SystemExit("nothing to ingest")
    log.info("running %s", ", ".join(t.get_name() for t in tasks))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())
