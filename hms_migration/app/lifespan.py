"""
Application lifespan context manager.

Conforms to UltrionTech-Backend-Template app/lifespan.py specification.
Manages clean startup initialization, background loops, and graceful teardown
of resources (e.g., database connection pool disposal).
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI

from hms_migration.infrastructure.postgres.engine import get_async_engine
from hms_migration.infrastructure.postgres.session import get_transitional_sync_session_factory
from hms_migration.modules.appointments.services.appointment_lifecycle import (
    auto_cancel_missed_appointments,
)

logger = logging.getLogger("hms.lifespan")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup and graceful teardown hooks for the application."""
    logger.info("Initializing HMS target application lifecycle...")

    # Ensure database schema on startup if database is reachable
    try:
        from hms_migration.infrastructure.postgres.base import Base
        from hms_migration.infrastructure.postgres.engine import get_transitional_sync_engine

        sync_engine = get_transitional_sync_engine()
        Base.metadata.create_all(bind=sync_engine)
        logger.info("Target database schema verified/created.")
    except Exception as exc:
        logger.warning("Target database schema initialization skipped: %s", exc)

    async def _missed_appointment_loop() -> None:
        factory = get_transitional_sync_session_factory()
        while True:
            await asyncio.sleep(60)
            try:
                db = factory()
                try:
                    auto_cancel_missed_appointments(db)
                finally:
                    db.close()
            except Exception as exc:
                logger.warning("Target missed appointment auto-cancel loop: %s", exc)

    miss_task = asyncio.create_task(_missed_appointment_loop())

    try:
        yield
    finally:
        logger.info("Tearing down HMS target application resources...")
        miss_task.cancel()
        try:
            await miss_task
        except asyncio.CancelledError:
            pass

        try:
            async_engine = get_async_engine()
            await async_engine.dispose()
            logger.info("Async database engine disposed cleanly.")
        except Exception as exc:
            logger.warning("Error during async engine disposal: %s", exc)
