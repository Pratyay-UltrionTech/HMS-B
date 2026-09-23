"""
Canonical IPD lifecycle migration (spec §§25-26).

Additive, zero-data-loss migration for the REQUESTED → ADMITTED →
DISCHARGE_REQUESTED → DISCHARGED lifecycle:

  1. ``admission_status`` enum gains ``requested`` (PG ALTER TYPE).
  2. ``admissions.ward_id/room_id/bed_id`` become nullable (bedless
     request / "Admitted — Awaiting Bed").
  3. Open-episode partial unique indexes widened to
     ``('requested','admitted','discharge_requested')`` (Invariant 1+6).
  4. ``uq_admission_source_appointment`` partial unique (one canonical
     admission per appointment, Invariant 11).
  5. ``uq_open_bed_stay_segment_per_admission`` partial unique (Invariant 7).
  6. ``admissions.source_appointment_id`` FK → appointments (NOT VALID +
     VALIDATE pattern, orphan-safe).
  7. Deterministic orphan-form reconciliation (spec §13): link ONLY when the
     patient has exactly one admission ever; multiple candidates are flagged
     for human review; zero candidates stay historically unlinked. Never
     guesses by timestamp proximity.

Run with: python -m scripts.migrate_ipd_canonical_lifecycle [--dry-run] [--apply]
Default is --dry-run. --apply refuses to create unique indexes while
duplicate open episodes exist (resolve from the dry-run report first).
"""

from __future__ import annotations

import argparse
import logging

from sqlalchemy import inspect, text

import app.router  # noqa: F401  (populates Base.metadata)
from infrastructure.postgres.engine import get_transitional_sync_engine

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("hms.scripts.migrate_ipd_canonical_lifecycle")

OPEN = "('requested', 'admitted', 'discharge_requested')"


def _report(conn) -> dict:
    out: dict = {}
    q = lambda sql: conn.execute(text(sql)).scalar_one()  # noqa: E731

    out["orphan_forms"] = q("SELECT COUNT(*) FROM ipd_form_submissions WHERE admission_id IS NULL")
    out["orphan_exact_one"] = q(
        "SELECT COUNT(*) FROM ipd_form_submissions f WHERE f.admission_id IS NULL "
        "AND (SELECT COUNT(*) FROM admissions a WHERE a.patient_id = f.patient_id) = 1"
    )
    out["orphan_ambiguous"] = q(
        "SELECT COUNT(*) FROM ipd_form_submissions f WHERE f.admission_id IS NULL "
        "AND (SELECT COUNT(*) FROM admissions a WHERE a.patient_id = f.patient_id) > 1"
    )
    out["orphan_no_admission"] = (
        out["orphan_forms"] - out["orphan_exact_one"] - out["orphan_ambiguous"]
    )
    out["pending_appointments"] = q(
        "SELECT COUNT(*) FROM appointments WHERE status = 'ipd_transfer_requested'"
    )
    out["pending_appts_no_open_admission"] = q(
        "SELECT COUNT(*) FROM appointments ap WHERE ap.status = 'ipd_transfer_requested' "
        "AND NOT EXISTS (SELECT 1 FROM admissions a WHERE a.patient_id = ap.patient_id "
        f"AND a.status::text IN {OPEN})"
    )
    out["admissions_by_status"] = [
        dict(r) for r in conn.execute(
            text("SELECT status, COUNT(*) AS n FROM admissions GROUP BY status ORDER BY status")
        ).mappings()
    ]
    out["duplicate_open_patients"] = q(
        "SELECT COUNT(*) FROM (SELECT patient_id FROM admissions "
        f"WHERE status::text IN {OPEN} GROUP BY hospital_id, patient_id HAVING COUNT(*) > 1) d"
    )
    out["beds_occupied_no_admission"] = q(
        "SELECT COUNT(*) FROM beds b WHERE b.is_occupied IS TRUE AND NOT EXISTS "
        f"(SELECT 1 FROM admissions a WHERE a.bed_id = b.id AND a.status::text IN {OPEN})"
    )
    out["admissions_bed_flag_off"] = q(
        "SELECT COUNT(*) FROM admissions a JOIN beds b ON b.id = a.bed_id "
        f"WHERE a.status::text IN {OPEN} AND (b.is_occupied IS NOT TRUE)"
    )
    out["multi_open_segments"] = q(
        "SELECT COUNT(*) FROM (SELECT admission_id FROM bed_stay_segments "
        "WHERE ended_at IS NULL GROUP BY hospital_id, admission_id HAVING COUNT(*) > 1) d"
    )
    out["discharged_null_date"] = q(
        "SELECT COUNT(*) FROM admissions WHERE status::text = 'discharged' AND discharged_at IS NULL"
    )
    return out


def _print_report(rep: dict) -> None:
    logger.info("── canonical IPD reconciliation report ──")
    logger.info("orphan IPD forms (admission_id NULL): total=%d exact_one=%d ambiguous=%d no_admission=%d",
                rep["orphan_forms"], rep["orphan_exact_one"], rep["orphan_ambiguous"], rep["orphan_no_admission"])
    logger.info("appointments ipd_transfer_requested: total=%d without_open_admission=%d",
                rep["pending_appointments"], rep["pending_appts_no_open_admission"])
    logger.info("admissions by status: %s", rep["admissions_by_status"])
    logger.info("patients with >1 open episode (must be 0 before index rebuild): %d", rep["duplicate_open_patients"])
    logger.info("beds occupied-flagged with no open admission: %d", rep["beds_occupied_no_admission"])
    logger.info("open admissions whose bed flag is off: %d", rep["admissions_bed_flag_off"])
    logger.info("admissions with >1 open stay segment: %d", rep["multi_open_segments"])
    logger.info("discharged rows with NULL discharged_at (app enforces going forward; no backfill): %d",
                rep["discharged_null_date"])


def _ambiguous_list(conn, limit: int = 50) -> None:
    rows = conn.execute(text(
        "SELECT f.id, f.patient_id, f.form_id, f.status, f.created_at, "
        "(SELECT COUNT(*) FROM admissions a WHERE a.patient_id = f.patient_id) AS n_adm "
        "FROM ipd_form_submissions f WHERE f.admission_id IS NULL "
        "AND (SELECT COUNT(*) FROM admissions a WHERE a.patient_id = f.patient_id) > 1 "
        "ORDER BY f.created_at DESC LIMIT :lim"
    ), {"lim": limit}).mappings().all()
    if rows:
        logger.warning("ambiguous orphan forms for human review (NOT auto-linked):")
        for r in rows:
            logger.warning("  form=%s patient=%s form_id=%s status=%s created=%s admissions=%s",
                           r["id"], r["patient_id"], r["form_id"], r["status"], r["created_at"], r["n_adm"])


def _ensure_enum_value(engine) -> None:
    """Add the 'requested' enum label on an AUTOCOMMIT connection.

    Postgres forbids ALTER TYPE ... ADD VALUE inside a transaction block,
    so this must NOT share the transactional connection used by _apply.
    """
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        has = conn.execute(text(
            "SELECT 1 FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname = 'admission_status' AND e.enumlabel = 'requested'"
        )).first()
        if not has:
            conn.execute(text("ALTER TYPE admission_status ADD VALUE 'requested'"))
            logger.info("added enum value admission_status.requested")
        else:
            logger.info("enum value already present")


def _apply(conn) -> None:
    # 1. enum value handled separately (autocommit) — see _ensure_enum_value.

    # 2. nullable location
    for col in ("ward_id", "room_id", "bed_id"):
        conn.execute(text(f'ALTER TABLE admissions ALTER COLUMN {col} DROP NOT NULL'))
    logger.info("admissions ward/room/bed now nullable")

    # 3+4+5. rebuild partial unique indexes with the widened predicates
    conn.execute(text("DROP INDEX IF EXISTS uq_admissions_active_bed"))
    conn.execute(text("DROP INDEX IF EXISTS uq_admissions_active_patient"))
    conn.execute(text(
        "CREATE UNIQUE INDEX uq_admissions_active_bed ON admissions (hospital_id, bed_id) "
        f"WHERE status IN {OPEN}"
    ))
    conn.execute(text(
        "CREATE UNIQUE INDEX uq_admissions_active_patient ON admissions (hospital_id, patient_id) "
        f"WHERE status IN {OPEN}"
    ))
    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_admission_source_appointment "
        "ON admissions (hospital_id, source_appointment_id) WHERE source_appointment_id IS NOT NULL"
    ))
    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_open_bed_stay_segment_per_admission "
        "ON bed_stay_segments (hospital_id, admission_id) WHERE ended_at IS NULL"
    ))
    logger.info("partial unique indexes rebuilt")

    # 6. FK with orphan guard (NOT VALID + VALIDATE, same convention as add_missing_foreign_keys.py).
    # Idempotent: skip ADD when the constraint already exists.
    fk = conn.execute(text(
        "SELECT convalidated FROM pg_constraint "
        "WHERE conname = 'fk_admissions_source_appointment_id'"
    )).first()
    if fk is not None:
        logger.info(
            "SKIP FK admissions.source_appointment_id: already exists (validated=%s)",
            fk[0],
        )
        if not fk[0]:
            conn.execute(text("ALTER TABLE admissions VALIDATE CONSTRAINT fk_admissions_source_appointment_id"))
            logger.info("validated FK admissions.source_appointment_id")
    else:
        orphans = conn.execute(text(
            "SELECT COUNT(*) FROM admissions t WHERE t.source_appointment_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM appointments r WHERE r.id = t.source_appointment_id)"
        )).scalar_one()
        if orphans:
            logger.warning("SKIP FK admissions.source_appointment_id: %d orphaned row(s); fix data then re-run", orphans)
        else:
            conn.execute(text(
                'ALTER TABLE admissions ADD CONSTRAINT fk_admissions_source_appointment_id '
                'FOREIGN KEY (source_appointment_id) REFERENCES appointments (id) ON DELETE SET NULL NOT VALID'
            ))
            conn.execute(text("ALTER TABLE admissions VALIDATE CONSTRAINT fk_admissions_source_appointment_id"))
            logger.info("added FK admissions.source_appointment_id")

    # 7. deterministic orphan linking: exact-one-admission only
    linked = conn.execute(text(
        "UPDATE ipd_form_submissions f SET admission_id = "
        "(SELECT a.id FROM admissions a WHERE a.patient_id = f.patient_id LIMIT 1) "
        "WHERE f.admission_id IS NULL "
        "AND (SELECT COUNT(*) FROM admissions a WHERE a.patient_id = f.patient_id) = 1"
    )).rowcount
    logger.info("linked %d orphan form(s) with exactly one candidate admission", linked)
    conn.commit()


def main(dry_run: bool = True) -> None:
    engine = get_transitional_sync_engine()
    if engine.dialect.name != "postgresql":
        logger.warning("non-postgresql dialect (%s): report-only mode", engine.dialect.name)
        dry_run = True
    if dry_run:
        # Dry-run is strictly read-only: no DDL/UPDATE, no _ensure_enum_value.
        with engine.connect() as conn:
            rep = _report(conn)
            conn.rollback()
            _print_report(rep)
            _ambiguous_list(conn)
            conn.rollback()
            logger.info("dry-run only; re-run with --apply to migrate")
            return
    # --apply path: ensure the enum label exists BEFORE any status::text report,
    # so _report() runs on pre- and post-migration DBs.
    _ensure_enum_value(engine)
    with engine.connect() as conn:
        rep = _report(conn)
        _print_report(rep)
        _ambiguous_list(conn)
        if rep["duplicate_open_patients"]:
            logger.error("ABORT: resolve duplicate open episodes before rebuilding unique indexes")
            conn.rollback()
            return
        _apply(conn)
        logger.info("apply complete; re-run dry-run to verify zeros")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="perform the migration (default is dry-run)")
    args = parser.parse_args()
    main(dry_run=not args.apply)
