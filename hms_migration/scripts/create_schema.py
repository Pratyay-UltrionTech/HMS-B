"""
One-off schema creation for the target (hms_migration) architecture.

Run this explicitly whenever entities change, instead of paying the cost of
create_all() on every application boot:

    python -m hms_migration.scripts.create_schema
"""

import logging

from hms_migration.infrastructure.postgres.base import Base
from hms_migration.infrastructure.postgres.engine import get_transitional_sync_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hms.scripts.create_schema")


def main() -> None:
    engine = get_transitional_sync_engine()
    Base.metadata.create_all(bind=engine)
    logger.info("Schema created/verified for %d table(s).", len(Base.metadata.tables))


if __name__ == "__main__":
    main()
