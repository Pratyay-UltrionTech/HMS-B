"""
PostgreSQL session management and FastAPI dependency injection.

Conforms to UltrionTech-Backend-Template infrastructure/postgres/ specification.
Provides async session management as target standard, with an explicit
transitional synchronous session generator.
"""

from collections.abc import AsyncGenerator, Generator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from hms_migration.infrastructure.postgres.engine import (
    get_async_engine,
    get_transitional_sync_engine,
)


def get_async_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return an async_sessionmaker bound to the async PostgreSQL engine."""
    return async_sessionmaker(
        bind=get_async_engine(),
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Target FastAPI dependency yielding an AsyncSession.

    Every new migrated module must consume this dependency for database operations.
    """
    factory = get_async_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


def get_transitional_sync_session_factory() -> sessionmaker[Session]:
    """Return a synchronous sessionmaker bound to the transitional sync engine."""
    return sessionmaker(
        bind=get_transitional_sync_engine(),
        autocommit=False,
        autoflush=False,
    )


def get_transitional_sync_session() -> Generator[Session, None, None]:
    """
    TRANSITIONAL: Synchronous Session dependency for hybrid coexistence.

    Why necessary:
        Allows the first vertical slice (Vitals) to be verified and cut over
        against the shared architecture without requiring a forced, risky big-bang
        async rewrite of all interconnected ORM entities in app/models.py.
    Consumers:
        hms_migration.modules.vitals (during Phase 4).
    Removal target:
        Slated for deprecation in Phase 5 and full removal in Phase 6.
    """
    factory = get_transitional_sync_session_factory()
    session = factory()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
