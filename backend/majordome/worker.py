"""Worker process: runs every ingester concurrently."""

import asyncio
import logging

import uvicorn

from majordome.config import settings
from majordome.ingest.imap import ImapIngester, load_accounts
from majordome.ingest.matrix import AppserviceIngester

log = logging.getLogger("majordome.worker")


async def main() -> None:
    cfg = settings()
    tasks = []
    if cfg.matrix_hs_token and cfg.matrix_as_token:
        app = AppserviceIngester(cfg).http_app()
        # Reachable only on the internal Docker network (no published port).
        server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0",
                                               port=cfg.matrix_appservice_port,
                                               log_level="warning"))
        tasks.append(asyncio.create_task(server.serve(), name="matrix-appservice"))
    else:
        log.warning("appservice tokens not set: matrix ingestion disabled")
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
