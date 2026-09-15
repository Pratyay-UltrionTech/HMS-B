"""
One-off schema creation for the target (hms_migration) architecture.

Run this explicitly whenever entities change, instead of paying the cost of
create_all() on every application boot:

    python -m scripts.create_schema
"""

import logging

from infrastructure.postgres.base import Base
from infrastructure.postgres.engine import get_transitional_sync_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hms.scripts.create_schema")


from sqlalchemy import text
import app.main  # Ensure all model tables are registered in Base.metadata

def sync_missing_columns(engine) -> None:
    with engine.connect() as conn:
        db_cols = conn.execute(text("""
            SELECT table_name, column_name 
            FROM information_schema.columns 
            WHERE table_schema = 'public';
        """)).fetchall()
        
        table_columns: dict[str, set[str]] = {}
        for t, c in db_cols:
            table_columns.setdefault(t, set()).add(c)
            
        missing_count = 0
        for table_name, table in Base.metadata.tables.items():
            if table_name not in table_columns:
                continue
            existing = table_columns[table_name]
            for col in table.columns:
                if col.name not in existing:
                    missing_count += 1
                    col_type = col.type.compile(engine.dialect)
                    sql = f'ALTER TABLE "{table_name}" ADD COLUMN IF NOT EXISTS "{col.name}" {col_type} NULL;'
                    logger.info("Adding missing column: %s.%s (%s)", table_name, col.name, col_type)
                    conn.execute(text(sql))
        if missing_count > 0:
            conn.commit()
            logger.info("Added %d missing column(s).", missing_count)
        else:
            logger.info("All table columns are up to date.")


def main() -> None:
    engine = get_transitional_sync_engine()
    Base.metadata.create_all(bind=engine)
    logger.info("Schema created/verified for %d table(s).", len(Base.metadata.tables))
    sync_missing_columns(engine)


if __name__ == "__main__":
    main()
