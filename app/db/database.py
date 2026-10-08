from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

_engines: dict[str, Any] = {}
_sessionmakers: dict[str, async_sessionmaker[AsyncSession]] = {}


def get_engine() -> Any:
    """Return the cached async engine for the current DATABASE_URL."""
    settings = get_settings()
    url = settings.DATABASE_URL
    if url not in _engines:
        _engines[url] = create_async_engine(
            url,
            echo=False,
            future=True,
            connect_args={"check_same_thread": False} if "sqlite" in url else {},
        )
    return _engines[url]


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the cached async_sessionmaker for the current DATABASE_URL."""
    settings = get_settings()
    url = settings.DATABASE_URL
    if url not in _sessionmakers:
        _sessionmakers[url] = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
            autocommit=False,
        )
    return _sessionmakers[url]


# Backwards compatibility handles for module-level imports
engine = get_engine()
AsyncSessionLocal = get_sessionmaker()


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a DB session and closes it after the request."""
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_tables() -> None:
    """Create all tables (called at startup)."""
    eng = get_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
