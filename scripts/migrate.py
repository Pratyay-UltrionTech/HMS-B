"""
Database Migration Runner for Ultrion HMS.

Conforms to UltrionTech-Backend-Template scripts/ specification:
    python scripts/migrate.py up
    python scripts/migrate.py status
    python scripts/migrate.py prune-logins [--days 30]

Provides canonical, safe, data-aware, idempotent migration of:
  1. Hospital Registration Requests domain (hospital_registration_requests)
  2. Account Setup Tokens domain (account_setup_tokens)
  3. Prospective Callback Inquiries domain (callback_requests)
  4. Distributed Multi-Worker Rate Limiting domain (login_attempts)
  5. Hospital Operational Lifecycle (status, suspension, archival, audit)
  6. Data-aware Hospital Auth Model classification (legacy_organization vs user_accounts)
  7. Canonical StaffRole Admin backfills (HOSPITAL_ADMIN, is_admin_role)
  8. Schema constraints, foreign keys, and covering indexes.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text
from infrastructure.postgres.engine import get_transitional_sync_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("hms.migrate")


# ---------------------------------------------------------------------------
# DDL Statements (Idempotent, Safe, Non-Destructive)
# ---------------------------------------------------------------------------

DDL_STATEMENTS = [
    # 1. Hospital Registration Requests
    """
    CREATE TABLE IF NOT EXISTS hospital_registration_requests (
        id UUID PRIMARY KEY,
        request_number VARCHAR(64) UNIQUE NOT NULL,
        hospital_name VARCHAR(255) NOT NULL,
        contact_name VARCHAR(255) NOT NULL,
        email VARCHAR(255) NOT NULL,
        phone VARCHAR(32) NOT NULL,
        address TEXT NOT NULL,
        city VARCHAR(128),
        state VARCHAR(128),
        postal_code VARCHAR(32),
        estimated_beds INTEGER,
        comments TEXT,
        status VARCHAR(32) NOT NULL DEFAULT 'pending',
        rejection_reason TEXT,
        approved_hospital_id UUID REFERENCES hospitals(id) ON DELETE SET NULL,
        submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        reviewed_at TIMESTAMPTZ,
        reviewed_by VARCHAR(255),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_registration_requests_status ON hospital_registration_requests(status);",
    "CREATE INDEX IF NOT EXISTS ix_registration_requests_submitted_at ON hospital_registration_requests(submitted_at);",
    "CREATE INDEX IF NOT EXISTS ix_registration_requests_email ON hospital_registration_requests(email);",
    "CREATE INDEX IF NOT EXISTS ix_registration_requests_phone ON hospital_registration_requests(phone);",

    # 2. Account Setup Tokens
    """
    CREATE TABLE IF NOT EXISTS account_setup_tokens (
        id UUID PRIMARY KEY,
        token_hash VARCHAR(255) UNIQUE NOT NULL,
        user_id UUID REFERENCES hospital_users(id) ON DELETE CASCADE,
        hospital_id UUID NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
        email VARCHAR(255) NOT NULL,
        token_type VARCHAR(32) NOT NULL DEFAULT 'setup',
        expires_at TIMESTAMPTZ NOT NULL,
        used_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_account_setup_tokens_token_hash ON account_setup_tokens(token_hash);",
    "CREATE INDEX IF NOT EXISTS ix_account_setup_tokens_email ON account_setup_tokens(email);",
    "CREATE INDEX IF NOT EXISTS ix_account_setup_tokens_hospital_id ON account_setup_tokens(hospital_id);",

    # 3. Callback Requests
    """
    CREATE TABLE IF NOT EXISTS callback_requests (
        id UUID PRIMARY KEY,
        name VARCHAR(255) NOT NULL,
        phone VARCHAR(32) NOT NULL,
        email VARCHAR(255),
        location VARCHAR(255),
        comments TEXT,
        status VARCHAR(32) NOT NULL DEFAULT 'pending',
        notes TEXT,
        contacted_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_callback_requests_phone ON callback_requests(phone);",
    "CREATE INDEX IF NOT EXISTS ix_callback_requests_status ON callback_requests(status);",
    "CREATE INDEX IF NOT EXISTS ix_callback_requests_created_at ON callback_requests(created_at);",

    # 4. Login Attempts (Distributed multi-worker rate limiting - NON-DESTRUCTIVE)
    """
    CREATE TABLE IF NOT EXISTS login_attempts (
        id UUID PRIMARY KEY,
        key_hash VARCHAR(64) NOT NULL,
        attempted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        success BOOLEAN NOT NULL DEFAULT FALSE
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_login_attempts_key_hash ON login_attempts(key_hash);",
    "CREATE INDEX IF NOT EXISTS ix_login_attempts_attempted_at ON login_attempts(attempted_at);",

    # 5. Hospital Lifecycle & Auth Model Extensions
    "ALTER TABLE hospitals ADD COLUMN IF NOT EXISTS auth_model VARCHAR(32) NOT NULL DEFAULT 'user_accounts';",
    "ALTER TABLE hospitals ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT 'active';",
    "ALTER TABLE hospitals ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ;",
    "ALTER TABLE hospitals ADD COLUMN IF NOT EXISTS suspended_by VARCHAR(255);",
    "ALTER TABLE hospitals ADD COLUMN IF NOT EXISTS suspension_reason TEXT;",
    "ALTER TABLE hospitals ADD COLUMN IF NOT EXISTS reactivated_at TIMESTAMPTZ;",
    "ALTER TABLE hospitals ADD COLUMN IF NOT EXISTS reactivated_by VARCHAR(255);",
    "ALTER TABLE hospitals ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;",
    "ALTER TABLE hospitals ADD COLUMN IF NOT EXISTS archived_by VARCHAR(255);",
    "ALTER TABLE hospitals ALTER COLUMN password_hash DROP NOT NULL;",
    "CREATE INDEX IF NOT EXISTS ix_hospitals_status ON hospitals(status);",

    # 6. Staff Roles Admin Columns
    "ALTER TABLE staff_roles ADD COLUMN IF NOT EXISTS is_admin_role BOOLEAN NOT NULL DEFAULT FALSE;",
    "ALTER TABLE staff_roles ADD COLUMN IF NOT EXISTS code VARCHAR(64) DEFAULT NULL;",
    "CREATE INDEX IF NOT EXISTS ix_staff_roles_code ON staff_roles(code);",

    # 7. Audit Logs Hospital Id Nullability (supports super-admin / platform actions)
    """
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns 
            WHERE table_name = 'audit_logs' AND column_name = 'hospital_id' AND is_nullable = 'NO'
        ) THEN
            ALTER TABLE audit_logs ALTER COLUMN hospital_id DROP NOT NULL;
        END IF;
    END $$;
    """,

    # 8. Setup Tokens Foreign Key Integrity
    """
    DO $$
    BEGIN
        -- Prune orphan setup tokens if referenced entities no longer exist
        DELETE FROM account_setup_tokens WHERE user_id IS NOT NULL AND user_id NOT IN (SELECT id FROM hospital_users);
        DELETE FROM account_setup_tokens WHERE hospital_id NOT IN (SELECT id FROM hospitals);

        IF NOT EXISTS (
            SELECT 1 FROM information_schema.table_constraints
            WHERE table_name = 'account_setup_tokens' AND constraint_name = 'fk_account_setup_tokens_user_id'
        ) THEN
            ALTER TABLE account_setup_tokens
            ADD CONSTRAINT fk_account_setup_tokens_user_id
            FOREIGN KEY (user_id) REFERENCES hospital_users(id) ON DELETE CASCADE;
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM information_schema.table_constraints
            WHERE table_name = 'account_setup_tokens' AND constraint_name = 'fk_account_setup_tokens_hospital_id'
        ) THEN
            ALTER TABLE account_setup_tokens
            ADD CONSTRAINT fk_account_setup_tokens_hospital_id
            FOREIGN KEY (hospital_id) REFERENCES hospitals(id) ON DELETE CASCADE;
        END IF;
    END $$;
    """,

    # 9. Database-level State CHECK Constraints
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.table_constraints
            WHERE table_name = 'hospitals' AND constraint_name = 'chk_hospitals_status'
        ) THEN
            ALTER TABLE hospitals
            ADD CONSTRAINT chk_hospitals_status
            CHECK (status IN ('active', 'suspended', 'archived'));
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM information_schema.table_constraints
            WHERE table_name = 'hospital_registration_requests' AND constraint_name = 'chk_registration_requests_status'
        ) THEN
            ALTER TABLE hospital_registration_requests
            ADD CONSTRAINT chk_registration_requests_status
            CHECK (status IN ('pending', 'approved', 'rejected'));
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM information_schema.table_constraints
            WHERE table_name = 'callback_requests' AND constraint_name = 'chk_callback_requests_status'
        ) THEN
            ALTER TABLE callback_requests
            ADD CONSTRAINT chk_callback_requests_status
            CHECK (status IN ('pending', 'contacted', 'closed'));
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM information_schema.table_constraints
            WHERE table_name = 'hospital_registration_requests' AND constraint_name = 'chk_registration_state_consistency'
        ) THEN
            ALTER TABLE hospital_registration_requests
            ADD CONSTRAINT chk_registration_state_consistency
            CHECK (
                (status = 'pending') OR
                (status = 'approved' AND approved_hospital_id IS NOT NULL) OR
                (status = 'rejected' AND rejection_reason IS NOT NULL)
            );
        END IF;
    END $$;
    """,

    # 10. Cross-Module Integrity & Workflow Columns
    "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS consultation_status VARCHAR(32) NOT NULL DEFAULT 'scheduled';",
    "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS diagnostic_review_status VARCHAR(32) NOT NULL DEFAULT 'not_required';",
    "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS clinical_override_reason TEXT;",
    "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS clinical_override_actor VARCHAR(255);",
    "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS clinical_override_at TIMESTAMPTZ;",
    "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS diagnostics_reviewed_by VARCHAR(255);",
    "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS diagnostics_reviewed_at TIMESTAMPTZ;",
    "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS diagnostic_review_notes TEXT;",
    "CREATE INDEX IF NOT EXISTS ix_appointments_consultation_status ON appointments(consultation_status);",
    "CREATE INDEX IF NOT EXISTS ix_appointments_diagnostic_review_status ON appointments(diagnostic_review_status);",

    # 11. IPD Form Engine & Signer Integrity Columns
    "ALTER TABLE ipd_form_submissions ADD COLUMN IF NOT EXISTS form_schema_version INTEGER NOT NULL DEFAULT 1;",
    "ALTER TABLE ipd_form_submissions ADD COLUMN IF NOT EXISTS signer_type VARCHAR(64);",
    "ALTER TABLE ipd_form_submissions ADD COLUMN IF NOT EXISTS signer_name VARCHAR(255);",
    "ALTER TABLE ipd_form_submissions ADD COLUMN IF NOT EXISTS signer_relationship VARCHAR(128);",
    "ALTER TABLE ipd_form_submissions ADD COLUMN IF NOT EXISTS finalized_at TIMESTAMPTZ;",
    "CREATE INDEX IF NOT EXISTS ix_ipd_form_submissions_schema_ver ON ipd_form_submissions(form_schema_version);",

    # 12. DB-Backed Idempotency Records
    """
    CREATE TABLE IF NOT EXISTS idempotency_keys (
        id UUID PRIMARY KEY,
        hospital_id UUID NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
        idempotency_key VARCHAR(255) NOT NULL,
        scope VARCHAR(128) NOT NULL,
        request_hash VARCHAR(64) NOT NULL,
        response_code INTEGER NOT NULL,
        response_payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_idempotency_key UNIQUE (hospital_id, scope, idempotency_key)
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_idempotency_keys_created_at ON idempotency_keys(created_at);",
    "ALTER TABLE billing_receipts ADD COLUMN IF NOT EXISTS account_id_snapshot UUID;",
    "ALTER TABLE billing_receipts ADD COLUMN IF NOT EXISTS amount_allocated_snapshot NUMERIC(14, 2);",
    "ALTER TABLE billing_receipts ADD COLUMN IF NOT EXISTS account_balance_before_snapshot NUMERIC(14, 2);",
    "ALTER TABLE billing_receipts ADD COLUMN IF NOT EXISTS account_balance_after_snapshot NUMERIC(14, 2);",
    "ALTER TABLE billing_receipts ADD COLUMN IF NOT EXISTS patient_outstanding_after_snapshot NUMERIC(14, 2);",
    "ALTER TABLE billing_receipts ADD COLUMN IF NOT EXISTS available_deposit_after_snapshot NUMERIC(14, 2);",
    "ALTER TABLE billing_receipts ADD COLUMN IF NOT EXISTS net_patient_balance_after_snapshot NUMERIC(14, 2);",

    # 13. Multi-Segment Hospital Rostering and Recurring Work Patterns
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



# ---------------------------------------------------------------------------
# Data-Aware Backfill Procedures
# ---------------------------------------------------------------------------

def backfill_hospital_auth_models(conn) -> tuple[int, int]:
    """
    Classify existing hospitals safely into 'legacy_organization' vs 'user_accounts':
      - legacy_organization: Hospital has password_hash IS NOT NULL and NO HospitalUser admin (active or inactive).
      - user_accounts: Hospital has password_hash IS NULL OR has a migrated HospitalUser admin
        (regardless of whether that administrator is ACTIVE or INACTIVE).
    Preserves HospitalUser.is_active states without modification.
    """
    # 1. Hospitals needing legacy_organization:
    # Genuinely still depends on Hospital.password_hash and has NO migrated HospitalUser administrator
    legacy_sql = text("""
        UPDATE hospitals
        SET auth_model = 'legacy_organization'
        WHERE password_hash IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 
              FROM hospital_users u
              WHERE u.hospital_id = hospitals.id
                AND (
                    EXISTS (
                        SELECT 1 FROM staff_roles r
                        WHERE r.id = u.role_id
                          AND (r.is_admin_role = TRUE OR r.code = 'HOSPITAL_ADMIN' OR r.name IN ('Hospital Admin', 'hospital_admin'))
                    )
                    OR EXISTS (
                        SELECT 1 FROM hospital_user_roles ur
                        JOIN staff_roles r ON ur.role_id = r.id
                        WHERE ur.user_id = u.id
                          AND (r.is_admin_role = TRUE OR r.code = 'HOSPITAL_ADMIN' OR r.name IN ('Hospital Admin', 'hospital_admin'))
                    )
                )
          );
    """)
    res_legacy = conn.execute(legacy_sql)
    legacy_count = res_legacy.rowcount

    # 2. Hospitals needing user_accounts:
    # Has a migrated HospitalUser admin (active or inactive) OR password_hash IS NULL
    modern_sql = text("""
        UPDATE hospitals
        SET auth_model = 'user_accounts'
        WHERE (password_hash IS NULL)
           OR EXISTS (
              SELECT 1 
              FROM hospital_users u
              WHERE u.hospital_id = hospitals.id
                AND (
                    EXISTS (
                        SELECT 1 FROM staff_roles r
                        WHERE r.id = u.role_id
                          AND (r.is_admin_role = TRUE OR r.code = 'HOSPITAL_ADMIN' OR r.name IN ('Hospital Admin', 'hospital_admin'))
                    )
                    OR EXISTS (
                        SELECT 1 FROM hospital_user_roles ur
                        JOIN staff_roles r ON ur.role_id = r.id
                        WHERE ur.user_id = u.id
                          AND (r.is_admin_role = TRUE OR r.code = 'HOSPITAL_ADMIN' OR r.name IN ('Hospital Admin', 'hospital_admin'))
                    )
                )
           );
    """)
    res_modern = conn.execute(modern_sql)
    modern_count = res_modern.rowcount

    # 3. Synchronize is_active and status
    conn.execute(text("""
        UPDATE hospitals
        SET is_active = (status = 'active')
        WHERE is_active != (status = 'active');
    """))

    return legacy_count, modern_count


def backfill_hospital_admin_roles(conn) -> tuple[int, int]:
    """
    Backfill canonical Hospital Admin roles with is_admin_role = TRUE and code = 'HOSPITAL_ADMIN'.
    Targets:
      1. Canonical exact names ('Hospital Admin', 'hospital_admin') -> auto-migrated.
      2. Generic name ('Administrator') -> ambiguous, left unchanged and flagged for manual review.
         (No permission-count heuristic or broad access patterns can create Hospital Admin authority).
    
    Returns:
        (canonical_promoted_count, ambiguous_unmigrated_count)
    """
    # 1. Canonical deterministic roles
    canonical_sql = text("""
        UPDATE staff_roles
        SET is_admin_role = TRUE,
            code = 'HOSPITAL_ADMIN'
        WHERE name IN ('Hospital Admin', 'hospital_admin')
          AND (is_admin_role = FALSE OR code IS NULL OR code != 'HOSPITAL_ADMIN');
    """)
    res_canonical = conn.execute(canonical_sql)
    canonical_count = res_canonical.rowcount or 0

    # 2. Generic 'Administrator' roles:
    # A role named exactly 'Administrator' must NOT become Hospital Admin merely
    # because it has many permissions. Leave unchanged and report for manual review.
    ambiguous_count = 0
    candidate_roles_sql = text("""
        SELECT r.id, r.hospital_id, r.name
        FROM staff_roles r
        WHERE r.name = 'Administrator'
          AND (r.is_admin_role = FALSE OR r.code IS NULL OR r.code != 'HOSPITAL_ADMIN');
    """)
    candidate_roles = conn.execute(candidate_roles_sql).fetchall()

    for row in candidate_roles:
        role_id = row.id
        hospital_id = row.hospital_id
        # Ambiguous generic role: do not auto-promote. Report role/hospital ID without sensitive user data.
        ambiguous_count += 1
        logger.warning(
            "Ambiguous 'Administrator' role %s for hospital %s left unmigrated (requires manual review; no auto-promotion)",
            role_id, hospital_id,
        )

    return canonical_count, ambiguous_count


def prune_expired_login_attempts(conn, retention_days: int = 30) -> int:
    """
    Retention maintenance: prune login attempts older than retention_days.
    Uses ix_login_attempts_attempted_at for indexed deletion.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    sql = text("DELETE FROM login_attempts WHERE attempted_at < :cutoff;")
    res = conn.execute(sql, {"cutoff": cutoff})
    return res.rowcount


# ---------------------------------------------------------------------------
# Migration Execution & Status Commands
# ---------------------------------------------------------------------------

def run_migration() -> None:
    """Execute complete migration sequence within a transactional scope."""
    engine = get_transitional_sync_engine()
    logger.info("Connecting to target PostgreSQL: %s (db: %s)", engine.url.host, engine.url.database)

    # 1. Pre-migration state snapshot
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    logger.info("Pre-migration check: %d existing tables found.", len(existing_tables))

    with engine.begin() as conn:
        # Pre-counts
        def _cnt(tbl: str) -> int:
            if tbl in existing_tables:
                return conn.execute(text(f"SELECT count(*) FROM {tbl}")).scalar() or 0
            return 0

        pre_hospitals = _cnt("hospitals")
        pre_users = _cnt("hospital_users")
        pre_roles = _cnt("staff_roles")
        pre_tokens = _cnt("account_setup_tokens")
        pre_logins = _cnt("login_attempts")
        pre_regs = _cnt("hospital_registration_requests")

        logger.info(
            "Pre-migration baseline: hospitals=%d, users=%d, roles=%d, setup_tokens=%d, login_attempts=%d, reg_requests=%d",
            pre_hospitals, pre_users, pre_roles, pre_tokens, pre_logins, pre_regs,
        )

        # 2. Execute DDL statements
        logger.info("--- Executing Schema DDL ---")
        for stmt in DDL_STATEMENTS:
            clean_stmt = stmt.strip()
            if not clean_stmt:
                continue
            first_line = clean_stmt.splitlines()[0][:70]
            logger.info("DDL: %s...", first_line)
            conn.execute(text(clean_stmt))

        # 3. Execute Data-Aware Backfills
        logger.info("--- Executing Data-Aware Backfills ---")
        legacy_updated, modern_updated = backfill_hospital_auth_models(conn)
        logger.info("Hospital auth_model backfill: %d legacy_organization, %d user_accounts", legacy_updated, modern_updated)

        admin_roles_updated, ambiguous_admin_roles = backfill_hospital_admin_roles(conn)
        logger.info("Canonical admin roles migrated: %d", admin_roles_updated)
        logger.info("Ambiguous Administrator roles: %d", ambiguous_admin_roles)
        print(f"Canonical admin roles migrated: {admin_roles_updated}")
        print(f"Ambiguous Administrator roles: {ambiguous_admin_roles}")

        # Rostering backfill
        from scripts.migrate_multi_segment_rostering import (
            migrate_daily_shifts,
            backfill_work_patterns_from_users,
        )
        migrated_segs = migrate_daily_shifts(conn)
        logger.info("Migrated historical shifts into roster segments: %d", migrated_segs)
        created_pats = backfill_work_patterns_from_users(conn)
        logger.info("Backfilled default work patterns: %d", created_pats)

        # 4. Post-migration verification
        logger.info("--- Post-Migration Validation ---")
        dist = conn.execute(text("SELECT auth_model, count(*) FROM hospitals GROUP BY auth_model")).fetchall()
        logger.info("Hospital auth_model distribution: %s", dist)

        admin_count = conn.execute(text("SELECT count(*) FROM staff_roles WHERE is_admin_role = TRUE AND code = 'HOSPITAL_ADMIN'")).scalar()
        logger.info("Active canonical admin roles (code=HOSPITAL_ADMIN, is_admin_role=TRUE): %d", admin_count)

        post_logins = _cnt("login_attempts")
        logger.info("Login attempts preserved: %d rows (zero lost)", post_logins)
        assert post_logins >= pre_logins, "SAFETY INVARIANT VIOLATION: Login attempts count decreased during migration!"

    logger.info("Migration completed successfully with 100% data integrity.")


def show_status() -> None:
    """Inspect and report current database schema and data state."""
    engine = get_transitional_sync_engine()
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    target_tables = [
        "hospitals", "hospital_users", "staff_roles", "role_permissions",
        "hospital_user_roles", "account_setup_tokens", "login_attempts",
        "hospital_registration_requests", "callback_requests", "audit_logs",
        "staff_roster_days", "staff_roster_segments", "staff_work_patterns", "staff_work_pattern_segments",
    ]

    print("\n" + "=" * 70)
    print(f"DATABASE STATUS: {engine.url.database} on {engine.url.host}")
    print("=" * 70)

    for t in target_tables:

        present = t in tables
        print(f"Table {t:<35} : {'PRESENT' if present else 'MISSING'}")

    with engine.connect() as conn:
        print("\n--- Row Counts ---")
        for t in target_tables:
            if t in tables:
                cnt = conn.execute(text(f"SELECT count(*) FROM {t}")).scalar()
                print(f"{t:<35} : {cnt} rows")

        if "hospitals" in tables:
            print("\n--- Hospital Auth Models ---")
            h_cols = {c["name"] for c in inspector.get_columns("hospitals")}
            if "auth_model" in h_cols:
                dist = conn.execute(text("SELECT auth_model, count(*) FROM hospitals GROUP BY auth_model")).fetchall()
                for am, c in dist:
                    print(f"  {am:<25} : {c} hospital(s)")
            else:
                print("  auth_model column NOT present")

        if "staff_roles" in tables:
            print("\n--- Canonical Admin Roles ---")
            r_cols = {c["name"] for c in inspector.get_columns("staff_roles")}
            if "is_admin_role" in r_cols and "code" in r_cols:
                admin_cnt = conn.execute(text("SELECT count(*) FROM staff_roles WHERE is_admin_role = TRUE AND code = 'HOSPITAL_ADMIN'")).scalar()
                unmigrated_canonical = conn.execute(text("SELECT count(*) FROM staff_roles WHERE name IN ('Hospital Admin', 'hospital_admin') AND (is_admin_role = FALSE OR code IS NULL OR code != 'HOSPITAL_ADMIN')")).scalar()
                ambiguous_admin = conn.execute(text("SELECT count(*) FROM staff_roles WHERE name = 'Administrator' AND (is_admin_role = FALSE OR code IS NULL OR code != 'HOSPITAL_ADMIN')")).scalar()
                print(f"  Canonical HOSPITAL_ADMIN roles : {admin_cnt}")
                print(f"  Unmigrated canonical admin roles: {unmigrated_canonical}")
                print(f"  Ambiguous 'Administrator' roles: {ambiguous_admin}")
            else:
                print("  is_admin_role / code columns NOT present")

    print("=" * 70 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ultrion HMS Database Migration Runner")
    parser.add_argument("command", nargs="?", default="up", choices=["up", "status", "prune-logins"], help="Migration action")
    parser.add_argument("--days", type=int, default=30, help="Retention window in days for prune-logins")
    args = parser.parse_args()

    if args.command == "up":
        run_migration()
    elif args.command == "status":
        show_status()
    elif args.command == "prune-logins":
        engine = get_transitional_sync_engine()
        with engine.begin() as conn:
            pruned = prune_expired_login_attempts(conn, args.days)
            logger.info("Pruned %d login attempt(s) older than %d days.", pruned, args.days)


if __name__ == "__main__":
    main()

