from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from majordome.config import settings
from majordome.store.models import SyncState


@lru_cache
def engine() -> AsyncEngine:
    return create_async_engine(settings().database_url, pool_pre_ping=True)


@lru_cache
def sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine(), expire_on_commit=False)


@asynccontextmanager
async def session() -> AsyncIterator[AsyncSession]:
    async with sessionmaker()() as s:
        yield s


async def get_state(s: AsyncSession, key: str) -> dict[str, Any] | None:
    row = await s.scalar(select(SyncState).where(SyncState.key == key))
    return row.value if row else None


async def set_state(s: AsyncSession, key: str, value: dict[str, Any]) -> None:
    stmt = insert(SyncState).values(key=key, value=value)
    stmt = stmt.on_conflict_do_update(
        index_elements=[SyncState.key], set_={"value": value, "updated_at": func.now()}
    )
    await s.execute(stmt)
