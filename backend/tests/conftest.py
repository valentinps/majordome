"""Integration tests need a throwaway Postgres: set MAJORDOME_TEST_DATABASE_URL.

    docker run -d --name majordome-devpg -e POSTGRES_USER=majordome -e POSTGRES_PASSWORD=majordome \
        -e POSTGRES_DB=majordome -p 55432:5432 pgvector/pgvector:pg17
    MAJORDOME_TEST_DATABASE_URL=postgresql+asyncpg://majordome:majordome@127.0.0.1:55432/majordome

The tests run migrations and TRUNCATE every table: never point this at real data.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

TEST_DB = os.environ.get("MAJORDOME_TEST_DATABASE_URL")
if TEST_DB:
    os.environ["MAJORDOME_DATABASE_URL"] = TEST_DB
os.environ.setdefault("MAJORDOME_SESSION_SECRET", "test-secret-" + "x" * 32)


@pytest.fixture(scope="session")
def migrated():
    if not TEST_DB:
        pytest.skip("MAJORDOME_TEST_DATABASE_URL not set")
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True,
                   cwd=Path(__file__).parent.parent)


@pytest.fixture
async def s(migrated):
    from sqlalchemy import text

    from majordome.store import db

    db.engine.cache_clear()
    db.sessionmaker.cache_clear()
    async with db.session() as session:
        await session.execute(text(
            "TRUNCATE account, conversation, contact, identity, message, attachment, sync_state, "
            "policy_rule "
            "RESTART IDENTITY CASCADE"))
        await session.commit()
        yield session
    await db.engine().dispose()
