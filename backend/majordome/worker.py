"""Worker process: runs every ingester concurrently."""

import asyncio
import logging

import uvicorn

from majordome.agent.triage import triage_once
from majordome.config import settings
from majordome.ingest.imap import ImapIngester, load_accounts
from majordome.ingest.matrix import AppserviceIngester
from majordome.llm import provider_from
from majordome.notify.discord import load_webhook
from majordome.policy.apply import purge_expired
from majordome.store import db

log = logging.getLogger("majordome.worker")

RETENTION_INTERVAL_S = 3600


async def triage_loop(cfg) -> None:
    """Batch new readable messages to the triage agent, then notify."""
    provider = provider_from(cfg)
    delay = cfg.triage_interval_s
    while True:
        try:
            webhook = load_webhook(cfg.discord_webhook_file)
            async with db.session() as s:
                st = await triage_once(s, cfg, provider, webhook)
            if st.triaged:
                log.info("triage: %d message(s), %d notification(s)", st.triaged, st.notified)
            # Back off while the LLM fails; go again immediately if the batch was full.
            delay = min(delay * 2, 900) if st.failed else (
                1 if st.triaged >= cfg.triage_batch_size else cfg.triage_interval_s)
        except Exception:
            log.exception("triage failed")
            delay = min(delay * 2, 900)
        await asyncio.sleep(delay)


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
    if cfg.triage_enabled and cfg.llm_gateway_token:
        tasks.append(asyncio.create_task(triage_loop(cfg), name="triage"))
    else:
        log.warning("triage disabled (MAJORDOME_TRIAGE_ENABLED or LLM gateway token not set)")
    log.info("running %s", ", ".join(t.get_name() for t in tasks))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())
