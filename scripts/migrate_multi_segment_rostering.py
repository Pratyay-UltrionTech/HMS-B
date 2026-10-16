"""
Database Migration: Multi-Segment Hospital Rostering & Recurring Work Patterns.

Conforms to UltrionTech-Backend-Template scripts/ specification:
    python -m scripts.migrate_multi_segment_rostering [--dry-run]

Safely and idempotently:
1. Creates staff_roster_days table & indexes
2. Creates staff_roster_segments table & indexes
3. Creates staff_work_patterns table & indexes
4. Creates staff_work_pattern_segments table & indexes
5. Migrates historical StaffDailyShift records into StaffRosterDay + StaffRosterSegment
   (correctly handling cross-midnight shifts so end_at is on the next day)
6. Backfills default StaffWorkPattern records from HospitalUser.shift_id
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
import logging
from pathlib import Path
import sys
import uuid

# Ensure repository root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text
from infrastructure.postgres.engine import get_transitional_sync_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("hms.migrate_multi_segment_rostering")


DDL_STATEMENTS = [
    # 1. staff_roster_days
    """
    CREATE TABLE IF NOT EXISTS staff_roster_days (
        id UUID PRIMARY KEY,
        hospital_id UUID NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
        user_id UUID NOT NULL REFERENCES hospital_users(id) ON DELETE CASCADE,
        roster_date DATE NOT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'working',
        notes TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_staff_roster_day UNIQUE (hospital_id, user_id, roster_date)
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_staff_roster_days_hosp_date ON staff_roster_days(hospital_id, roster_date);",
    "CREATE INDEX IF NOT EXISTS ix_staff_roster_days_lookup ON staff_roster_days(hospital_id, user_id, roster_date);",

    # 2. staff_roster_segments
    """
    CREATE TABLE IF NOT EXISTS staff_roster_segments (
        id UUID PRIMARY KEY,
        hospital_id UUID NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
        roster_day_id UUID NOT NULL REFERENCES staff_roster_days(id) ON DELETE CASCADE,
        user_id UUID NOT NULL REFERENCES hospital_users(id) ON DELETE CASCADE,
        shift_type_id UUID REFERENCES shift_types(id) ON DELETE SET NULL,
        department_id UUID REFERENCES departments(id) ON DELETE SET NULL,
        start_at TIMESTAMPTZ NOT NULL,
        end_at TIMESTAMPTZ NOT NULL,
        duty_kind VARCHAR(32) NOT NULL DEFAULT 'OPD',
        presence_type VARCHAR(32) NOT NULL DEFAULT 'ONSITE',
        is_bookable BOOLEAN NOT NULL DEFAULT TRUE,
        notes TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_roster_seg_hosp_user_dates ON staff_roster_segments(hospital_id, user_id, start_at, end_at);",
    "CREATE INDEX IF NOT EXISTS ix_roster_seg_hosp_dept_dates ON staff_roster_segments(hospital_id, department_id, start_at);",
    "CREATE INDEX IF NOT EXISTS ix_roster_seg_hosp_dates ON staff_roster_segments(hospital_id, start_at, end_at);",
    "CREATE INDEX IF NOT EXISTS ix_roster_seg_day_id ON staff_roster_segments(roster_day_id);",

    # 3. staff_work_patterns
    """
    CREATE TABLE IF NOT EXISTS staff_work_patterns (
        id UUID PRIMARY KEY,
        hospital_id UUID NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
        user_id UUID NOT NULL REFERENCES hospital_users(id) ON DELETE CASCADE,
        name VARCHAR(128) NOT NULL DEFAULT 'Default Recurring Schedule',
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        notes TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_staff_work_pattern_user UNIQUE (hospital_id, user_id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_staff_work_patterns_lookup ON staff_work_patterns(hospital_id, user_id);",

    # 4. staff_work_pattern_segments
    """
    CREATE TABLE IF NOT EXISTS staff_work_pattern_segments (
        id UUID PRIMARY KEY,
        hospital_id UUID NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
        pattern_id UUID NOT NULL REFERENCES staff_work_patterns(id) ON DELETE CASCADE,
        day_of_week INTEGER NOT NULL,
        start_time TIME NOT NULL,
        end_time TIME NOT NULL,
        shift_type_id UUID REFERENCES shift_types(id) ON DELETE SET NULL,
        department_id UUID REFERENCES departments(id) ON DELETE SET NULL,
        duty_kind VARCHAR(32) NOT NULL DEFAULT 'OPD',
        presence_type VARCHAR(32) NOT NULL DEFAULT 'ONSITE',
        is_bookable BOOLEAN NOT NULL DEFAULT TRUE,
        notes TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_work_pattern_seg_pattern_day ON staff_work_pattern_segments(pattern_id, day_of_week);",
    "CREATE INDEX IF NOT EXISTS ix_work_pattern_seg_hosp_day ON staff_work_pattern_segments(hospital_id, day_of_week);",
]


def run_ddl(conn, dry_run: bool = False) -> None:
    logger.info("Executing DDL statements (dry_run=%s)...", dry_run)
    for stmt in DDL_STATEMENTS:
        cleaned = " ".join(stmt.strip().split())
        logger.info("DDL: %s", cleaned[:100] + "..." if len(cleaned) > 100 else cleaned)
        if not dry_run:
            conn.execute(text(stmt))


def migrate_daily_shifts(conn, dry_run: bool = False) -> int:
    """Migrate rows from staff_daily_shifts into staff_roster_days + staff_roster_segments."""
    logger.info("Checking for historical staff_daily_shifts to migrate...")
    rows = conn.execute(text("""
        SELECT s.id, s.hospital_id, s.user_id, s.roster_date, s.shift_id, s.status, s.notes,
               st.start_time, st.end_time, st.department_id, u.department_id as user_department_id
        FROM staff_daily_shifts s
        LEFT JOIN shift_types st ON s.shift_id = st.id
        LEFT JOIN hospital_users u ON s.user_id = u.id
    """)).fetchall()

    logger.info("Found %d staff_daily_shifts record(s) to process.", len(rows))
    migrated_count = 0

    inspector = inspect(conn)
    tables = set(inspector.get_table_names())
    if "staff_roster_days" not in tables:
        logger.info("[Dry Run] staff_roster_days table does not exist yet; would migrate %d records after table creation.", len(rows))
        return len(rows)

    for r in rows:
        h_id, u_id, r_date = r.hospital_id, r.user_id, r.roster_date
        status_val = r.status or "on_duty"
        canonical_status = "working" if status_val == "on_duty" else status_val

        # 1. Check or insert staff_roster_days
        day_row = conn.execute(text("""
            SELECT id FROM staff_roster_days
            WHERE hospital_id = :h_id AND user_id = :u_id AND roster_date = :r_date
        """), {"h_id": h_id, "u_id": u_id, "r_date": r_date}).fetchone()


        if not day_row:
            day_id = str(uuid.uuid4())
            if not dry_run:
                conn.execute(text("""
                    INSERT INTO staff_roster_days (id, hospital_id, user_id, roster_date, status, notes)
                    VALUES (:id, :h_id, :u_id, :r_date, :status, :notes)
                """), {
                    "id": day_id,
                    "h_id": h_id,
                    "u_id": u_id,
                    "r_date": r_date,
                    "status": canonical_status,
                    "notes": r.notes,
                })
        else:
            day_id = str(day_row[0])

        # 2. If status is on_duty and shift exists, create segment
        if canonical_status == "working" and r.shift_id and r.start_time and r.end_time:
            # Check if segment already exists for this day and shift
            seg_exists = conn.execute(text("""
                SELECT id FROM staff_roster_segments
                WHERE roster_day_id = :day_id AND shift_type_id = :shift_id
            """), {"day_id": day_id, "shift_id": r.shift_id}).fetchone()

            if not seg_exists:
                # Compute start_at and end_at (handling overnight shifts)
                st_time = r.start_time
                et_time = r.end_time
                start_dt = datetime.combine(r_date, st_time).replace(tzinfo=timezone.utc)
                if et_time <= st_time:
                    end_dt = datetime.combine(r_date + timedelta(days=1), et_time).replace(tzinfo=timezone.utc)
                else:
                    end_dt = datetime.combine(r_date, et_time).replace(tzinfo=timezone.utc)

                dept_id = r.department_id or r.user_department_id
                seg_id = str(uuid.uuid4())
                if not dry_run:
                    conn.execute(text("""
                        INSERT INTO staff_roster_segments (
                            id, hospital_id, roster_day_id, user_id, shift_type_id,
                            department_id, start_at, end_at, duty_kind, presence_type,
                            is_bookable, notes
                        ) VALUES (
                            :id, :h_id, :day_id, :u_id, :shift_id,
                            :dept_id, :start_at, :end_at, 'OPD', 'ONSITE',
                            TRUE, :notes
                        )
                    """), {
                        "id": seg_id,
                        "h_id": h_id,
                        "day_id": day_id,
                        "u_id": u_id,
                        "shift_id": r.shift_id,
                        "dept_id": dept_id,
                        "start_at": start_dt,
                        "end_at": end_dt,
                        "notes": r.notes,
                    })
                migrated_count += 1

    logger.info("Migrated/verified %d duty segment(s) from historical shifts.", migrated_count)
    return migrated_count


def backfill_work_patterns_from_users(conn, dry_run: bool = False) -> int:
    """Create default recurring weekly patterns for staff with HospitalUser.shift_id."""
    logger.info("Backfilling default work patterns from HospitalUser.shift_id...")
    users = conn.execute(text("""
        SELECT u.id, u.hospital_id, u.shift_id, u.department_id,
               st.start_time, st.end_time, st.department_id as shift_dept_id
        FROM hospital_users u
        JOIN shift_types st ON u.shift_id = st.id
        WHERE u.shift_id IS NOT NULL AND u.is_active = TRUE
    """)).fetchall()

    logger.info("Found %d user(s) with active default shifts.", len(users))
    created_patterns = 0

    inspector = inspect(conn)
    tables = set(inspector.get_table_names())
    if "staff_work_patterns" not in tables:
        logger.info("[Dry Run] staff_work_patterns table does not exist yet; would backfill %d patterns after table creation.", len(users))
        return len(users)

    for u in users:
        # Check if user already has a pattern
        existing = conn.execute(text("""

            SELECT id FROM staff_work_patterns
            WHERE hospital_id = :h_id AND user_id = :u_id
        """), {"h_id": u.hospital_id, "u_id": u.id}).fetchone()

        if not existing:
            pattern_id = str(uuid.uuid4())
            dept_id = u.shift_dept_id or u.department_id
            if not dry_run:
                conn.execute(text("""
                    INSERT INTO staff_work_patterns (id, hospital_id, user_id, name, is_active)
                    VALUES (:id, :h_id, :u_id, 'Default Weekly Schedule', TRUE)
                """), {"id": pattern_id, "h_id": u.hospital_id, "u_id": u.id})

                # Monday through Friday (0..4) or all 7 days (0..6)
                for day in range(7):
                    seg_id = str(uuid.uuid4())
                    conn.execute(text("""
                        INSERT INTO staff_work_pattern_segments (
                            id, hospital_id, pattern_id, day_of_week, start_time, end_time,
                            shift_type_id, department_id, duty_kind, presence_type, is_bookable
                        ) VALUES (
                            :id, :h_id, :pattern_id, :day, :start_time, :end_time,
                            :shift_id, :dept_id, 'OPD', 'ONSITE', TRUE
                        )
                    """), {
                        "id": seg_id,
                        "h_id": u.hospital_id,
                        "pattern_id": pattern_id,
                        "day": day,
                        "start_time": u.start_time,
                        "end_time": u.end_time,
                        "shift_id": u.shift_id,
                        "dept_id": dept_id,
                    })
            created_patterns += 1

    logger.info("Created %d default work pattern(s).", created_patterns)
    return created_patterns


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print operations without modifying database")
    args = parser.parse_args()

    engine = get_transitional_sync_engine()
    with engine.begin() as conn:
        run_ddl(conn, dry_run=args.dry_run)
        migrate_daily_shifts(conn, dry_run=args.dry_run)
        backfill_work_patterns_from_users(conn, dry_run=args.dry_run)

    logger.info("Multi-segment hospital rostering migration completed successfully.")


if __name__ == "__main__":
    main()
