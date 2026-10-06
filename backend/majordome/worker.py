"""Worker process: runs every ingester concurrently."""

import asyncio
import logging

import uvicorn

from majordome.config import settings
from majordome.ingest.imap import ImapIngester, load_accounts
from majordome.ingest.matrix import AppserviceIngester
from majordome.policy.apply import purge_expired
from majordome.store import db

log = logging.getLogger("majordome.worker")

RETENTION_INTERVAL_S = 3600


async def retention_loop() -> None:
    """Delete messages whose retention period (set by policy rules) has passed."""
    while True:
        try:
            async with db.session() as s:
                n = await purge_expired(s)
                await s.commit()
            if n:
                log.info("retention: deleted %d expired message(s)", n)
        except Exception:
            log.exception("retention purge failed")
        await asyncio.sleep(RETENTION_INTERVAL_S)


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
    tasks.append(asyncio.create_task(retention_loop(), name="retention"))
    log.info("running %s", ", ".join(t.get_name() for t in tasks))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())
