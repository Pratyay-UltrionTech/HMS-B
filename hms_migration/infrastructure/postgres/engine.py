"""
PostgreSQL database engine infrastructure.

Conforms to UltrionTech-Backend-Template infrastructure/postgres/ specification.
Provides async SQLAlchemy engine (asyncpg) as primary target architecture,
with an explicitly marked transitional synchronous engine for legacy compatibility.
"""

from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from hms_migration.config.settings import Settings, get_settings


@lru_cache
def get_async_engine(settings: Settings | None = None) -> AsyncEngine:
    """
    Primary async database engine using asyncpg driver.

    Target architecture for all new and migrated modules.
    """
    cfg = settings or get_settings()
    return create_async_engine(
        cfg.asyncpg_database_url,
        pool_pre_ping=True,
        echo=False,
    )


@lru_cache
def get_transitional_sync_engine(settings: Settings | None = None) -> Engine:
    """
    TRANSITIONAL: Synchronous database engine using psycopg2.

    Why necessary:
        Allows incremental migration while legacy synchronous ORM models
        in app.models.py (53 tables) and existing synchronous endpoints coexist.
    Consumer:
        Transitional sync session bridge and legacy DDL tasks.
    Lifecycle:
        Active in Phases 1-5; slated for complete removal in Phase 6.
    """
    cfg = settings or get_settings()
    return create_engine(
        cfg.sqlalchemy_database_url,
        pool_pre_ping=True,
        echo=False,
    )
