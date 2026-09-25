"""
Migration script for Laboratory Specimens & Radiology Attachments.

Creates:
1. `lab_specimens` table
2. `radiology_attachments` table
3. Adds `specimen_id` to `lab_order_items`
4. Adds necessary indexes and foreign keys

Usage:
    .venv\\Scripts\\python -m scripts.migrate_specimens_and_radiology_attachments
"""

from __future__ import annotations

import logging
from sqlalchemy import text

import app.main  # noqa: F401 - Register all entities
from infrastructure.postgres.base import Base
from infrastructure.postgres.engine import get_transitional_sync_engine
from scripts.create_schema import sync_missing_columns

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hms.scripts.migrate_specimens_and_attachments")


def main() -> None:
    engine = get_transitional_sync_engine()
    
    from modules.laboratory.entities.lab_entities import LabSpecimen
    from modules.radiology.entities.radiology_entities import RadiologyAttachment

    # 1. Create target missing tables (lab_specimens and radiology_attachments)
    logger.info("Creating lab_specimens and radiology_attachments tables if not exist...")
    Base.metadata.create_all(bind=engine, tables=[LabSpecimen.__table__, RadiologyAttachment.__table__])

    # 2. Add column lab_order_items.specimen_id if missing
    with engine.connect() as conn:
        logger.info("Ensuring column lab_order_items.specimen_id exists...")
        conn.execute(text("""
            ALTER TABLE "lab_order_items"
            ADD COLUMN IF NOT EXISTS "specimen_id" UUID NULL;
        """))

        logger.info("Applying indexes and constraints...")
        # Unique index on (hospital_id, specimen_no)
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_lab_specimens_hospital_specimen_no
            ON lab_specimens (hospital_id, specimen_no);
        """))
        
        # Index on (order_id)
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_lab_specimens_order_id
            ON lab_specimens (order_id);
        """))

        # Index on (hospital_id, order_id)
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_radiology_attachments_order_id
            ON radiology_attachments (hospital_id, order_id);
        """))
        
        conn.commit()

    logger.info("Migration for laboratory specimens and radiology attachments completed successfully.")


if __name__ == "__main__":
    main()
