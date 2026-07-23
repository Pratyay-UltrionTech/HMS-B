import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings
from app.database import Base, engine
from app.routers import auth, hospitals, masters, admin, doctors, registration, appointment, beds, mis, analytics, laboratory, radiology, ot, dms, equipment, billing
import app.models  # noqa: F401 — register all ORM tables for create_all

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("hms.api")

settings = get_settings()


def _migrate_shift_types_for_department() -> None:
    """Recreate shift_types if it predates department-scoped shifts."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        if "shift_types" not in insp.get_table_names():
            return
        cols = {c["name"] for c in insp.get_columns("shift_types")}
        if "department_id" in cols:
            return
        logger.info("Recreating shift_types to add department_id (existing global shifts will be cleared)")
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS shift_types CASCADE"))
    except Exception as exc:
        logger.warning("shift_types migration skipped: %s", exc)


def _migrate_rooms_bed_count() -> None:
    """Add bed_count to rooms if missing."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        if "rooms" not in insp.get_table_names():
            return
        cols = {c["name"] for c in insp.get_columns("rooms")}
        if "bed_count" in cols:
            return
        logger.info("Adding bed_count column to rooms")
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE rooms ADD COLUMN bed_count INTEGER NOT NULL DEFAULT 1"))
    except Exception as exc:
        logger.warning("rooms bed_count migration skipped: %s", exc)


def _migrate_patients_registration_fields() -> None:
    """Add UHID / registration columns to patients if the table already exists."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        if "patients" not in insp.get_table_names():
            return
        cols = {c["name"] for c in insp.get_columns("patients")}
        alters: list[str] = []
        if "uhid" not in cols:
            alters.append("ADD COLUMN uhid VARCHAR(32)")
        if "first_name" not in cols:
            alters.append("ADD COLUMN first_name VARCHAR(128) NOT NULL DEFAULT ''")
        if "last_name" not in cols:
            alters.append("ADD COLUMN last_name VARCHAR(128) NOT NULL DEFAULT ''")
        if "email" not in cols:
            alters.append("ADD COLUMN email VARCHAR(255)")
        if "date_of_birth" not in cols:
            alters.append("ADD COLUMN date_of_birth DATE")
        if "emergency_contact" not in cols:
            alters.append("ADD COLUMN emergency_contact VARCHAR(64)")
        if "emergency_contact_name" not in cols:
            alters.append("ADD COLUMN emergency_contact_name VARCHAR(128)")
        if "emergency_contact_relation" not in cols:
            alters.append("ADD COLUMN emergency_contact_relation VARCHAR(32)")
        if "blood_group" not in cols:
            alters.append("ADD COLUMN blood_group VARCHAR(16)")
        if "has_insurance" not in cols:
            alters.append("ADD COLUMN has_insurance BOOLEAN NOT NULL DEFAULT FALSE")
        if "insurance_provider" not in cols:
            alters.append("ADD COLUMN insurance_provider VARCHAR(255)")
        if "insurance_details" not in cols:
            alters.append("ADD COLUMN insurance_details JSONB")
        if alters:
            logger.info("Migrating patients registration columns")
            with engine.begin() as conn:
                for clause in alters:
                    conn.execute(text(f"ALTER TABLE patients {clause}"))
                conn.execute(
                    text(
                        """
                        UPDATE patients
                        SET first_name = CASE
                              WHEN COALESCE(first_name, '') = '' THEN split_part(name, ' ', 1)
                              ELSE first_name END,
                            last_name = CASE
                              WHEN COALESCE(last_name, '') = '' THEN
                                CASE WHEN position(' ' in name) > 0 THEN substring(name from position(' ' in name) + 1) ELSE '' END
                              ELSE last_name END
                        WHERE COALESCE(first_name, '') = '' OR COALESCE(last_name, '') = ''
                        """
                    )
                )
                conn.execute(
                    text(
                        """
                        WITH numbered AS (
                          SELECT id, 'P' || lpad(ROW_NUMBER() OVER (PARTITION BY hospital_id ORDER BY created_at)::text, 4, '0') AS new_uhid
                          FROM patients
                          WHERE uhid IS NULL OR uhid = ''
                        )
                        UPDATE patients p SET uhid = n.new_uhid FROM numbered n WHERE p.id = n.id
                        """
                    )
                )
                conn.execute(text("ALTER TABLE patients ALTER COLUMN uhid SET NOT NULL"))
                try:
                    conn.execute(
                        text(
                            "CREATE UNIQUE INDEX IF NOT EXISTS uq_patient_hospital_uhid ON patients (hospital_id, uhid)"
                        )
                    )
                except Exception:
                    pass

        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    DO $$ BEGIN
                      CREATE TYPE patient_status AS ENUM ('active', 'inactive', 'admitted');
                    EXCEPTION WHEN duplicate_object THEN NULL;
                    END $$;
                    """
                )
            )
        cols2 = {c["name"] for c in inspect(engine).get_columns("patients")}
        if "status" not in cols2:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE patients ADD COLUMN status patient_status NOT NULL DEFAULT 'active'"
                    )
                )
    except Exception as exc:
        logger.warning("patients registration migration skipped: %s", exc)


def _migrate_appointments_extra_fields() -> None:
    """Add visit_type / queue fields and waiting status."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        if "appointments" not in insp.get_table_names():
            return
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    DO $$ BEGIN
                      ALTER TYPE appointment_status ADD VALUE IF NOT EXISTS 'waiting';
                    EXCEPTION WHEN others THEN NULL;
                    END $$;
                    """
                )
            )
        cols = {c["name"] for c in inspect(engine).get_columns("appointments")}
        with engine.begin() as conn:
            if "visit_type" not in cols:
                conn.execute(
                    text("ALTER TABLE appointments ADD COLUMN visit_type VARCHAR(64) NOT NULL DEFAULT 'OPD'")
                )
            if "queue_token" not in cols:
                conn.execute(text("ALTER TABLE appointments ADD COLUMN queue_token INTEGER"))
            if "checked_in_at" not in cols:
                conn.execute(text("ALTER TABLE appointments ADD COLUMN checked_in_at TIMESTAMPTZ"))
    except Exception as exc:
        logger.warning("appointments fields migration skipped: %s", exc)


def _migrate_lab_prescription_requests() -> None:
    """Lab order source / prescription links; request tables via create_all."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        tables = set(insp.get_table_names())
        if "lab_orders" not in tables:
            return
        cols = {c["name"] for c in insp.get_columns("lab_orders")}
        with engine.begin() as conn:
            # Ensure enum type exists
            conn.execute(
                text(
                    """
                    DO $$ BEGIN
                      CREATE TYPE lab_order_source AS ENUM ('doctor_prescribed', 'self_requested');
                    EXCEPTION WHEN duplicate_object THEN NULL;
                    END $$;
                    """
                )
            )
            if "order_source" not in cols:
                conn.execute(
                    text(
                        "ALTER TABLE lab_orders ADD COLUMN order_source lab_order_source "
                        "NOT NULL DEFAULT 'self_requested'"
                    )
                )
            if "prescription_id" not in cols:
                conn.execute(text("ALTER TABLE lab_orders ADD COLUMN prescription_id UUID"))
            if "prescription_request_id" not in cols:
                conn.execute(text("ALTER TABLE lab_orders ADD COLUMN prescription_request_id UUID"))
        # Optional FKs
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        DO $$ BEGIN
                          IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint WHERE conname = 'lab_orders_prescription_id_fkey'
                          ) THEN
                            ALTER TABLE lab_orders
                              ADD CONSTRAINT lab_orders_prescription_id_fkey
                              FOREIGN KEY (prescription_id) REFERENCES prescriptions(id) ON DELETE SET NULL;
                          END IF;
                          IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint WHERE conname = 'lab_orders_prescription_request_id_fkey'
                          ) THEN
                            ALTER TABLE lab_orders
                              ADD CONSTRAINT lab_orders_prescription_request_id_fkey
                              FOREIGN KEY (prescription_request_id)
                              REFERENCES lab_prescription_requests(id) ON DELETE SET NULL;
                          END IF;
                        END $$;
                        """
                    )
                )
        except Exception as fk_exc:
            logger.warning("lab order prescription FKs skipped: %s", fk_exc)
    except Exception as exc:
        logger.warning("lab prescription request migration skipped: %s", exc)


def _migrate_lab_panels() -> None:
    """Add panel provenance columns on lab_order_items (tables created via create_all)."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        if "lab_order_items" not in set(insp.get_table_names()):
            return
        cols = {c["name"] for c in insp.get_columns("lab_order_items")}
        with engine.begin() as conn:
            if "panel_id" not in cols:
                conn.execute(text("ALTER TABLE lab_order_items ADD COLUMN panel_id UUID"))
            if "panel_name" not in cols:
                conn.execute(text("ALTER TABLE lab_order_items ADD COLUMN panel_name VARCHAR(255)"))
        # Optional FK for existing DBs (ignore if already present / unsupported)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        DO $$ BEGIN
                          IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint WHERE conname = 'lab_order_items_panel_id_fkey'
                          ) THEN
                            ALTER TABLE lab_order_items
                              ADD CONSTRAINT lab_order_items_panel_id_fkey
                              FOREIGN KEY (panel_id) REFERENCES lab_test_panels(id) ON DELETE SET NULL;
                          END IF;
                        END $$;
                        """
                    )
                )
        except Exception as fk_exc:
            logger.warning("lab_order_items panel_id FK skipped: %s", fk_exc)
    except Exception as exc:
        logger.warning("lab panels migration skipped: %s", exc)


def _migrate_consultation_pricing() -> None:
    """Add appointment type / fee snapshot columns and pricing-related flags."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        tables = set(insp.get_table_names())

        if "appointment_types" in tables:
            at_cols = {c["name"] for c in insp.get_columns("appointment_types")}
            with engine.begin() as conn:
                if "is_follow_up" not in at_cols:
                    conn.execute(
                        text(
                            "ALTER TABLE appointment_types "
                            "ADD COLUMN is_follow_up BOOLEAN NOT NULL DEFAULT FALSE"
                        )
                    )
                if "updated_at" not in at_cols:
                    conn.execute(
                        text(
                            "ALTER TABLE appointment_types "
                            "ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
                        )
                    )
                # Mark existing Follow-up named types
                conn.execute(
                    text(
                        """
                        UPDATE appointment_types
                        SET is_follow_up = TRUE
                        WHERE is_follow_up = FALSE
                          AND (
                            LOWER(REPLACE(REPLACE(REPLACE(name, ' ', ''), '-', ''), '_', ''))
                            LIKE '%followup%'
                            OR LOWER(name) IN ('fu', 'follow')
                          )
                        """
                    )
                )

        if "appointments" in tables:
            cols = {c["name"] for c in inspect(engine).get_columns("appointments")}
            with engine.begin() as conn:
                if "appointment_type_id" not in cols:
                    conn.execute(text("ALTER TABLE appointments ADD COLUMN appointment_type_id UUID"))
                    conn.execute(
                        text(
                            """
                            ALTER TABLE appointments
                            ADD CONSTRAINT fk_appointments_appointment_type_id
                            FOREIGN KEY (appointment_type_id) REFERENCES appointment_types(id)
                            ON DELETE SET NULL
                            """
                        )
                    )
                    conn.execute(
                        text(
                            "CREATE INDEX IF NOT EXISTS ix_appointments_appointment_type_id "
                            "ON appointments (appointment_type_id)"
                        )
                    )
                if "wing_id" not in cols:
                    conn.execute(text("ALTER TABLE appointments ADD COLUMN wing_id UUID"))
                    conn.execute(
                        text(
                            """
                            ALTER TABLE appointments
                            ADD CONSTRAINT fk_appointments_wing_id
                            FOREIGN KEY (wing_id) REFERENCES wings(id) ON DELETE SET NULL
                            """
                        )
                    )
                    conn.execute(
                        text("CREATE INDEX IF NOT EXISTS ix_appointments_wing_id ON appointments (wing_id)")
                    )
                if "department_id" not in cols:
                    conn.execute(text("ALTER TABLE appointments ADD COLUMN department_id UUID"))
                    conn.execute(
                        text(
                            """
                            ALTER TABLE appointments
                            ADD CONSTRAINT fk_appointments_department_id
                            FOREIGN KEY (department_id) REFERENCES departments(id) ON DELETE SET NULL
                            """
                        )
                    )
                    conn.execute(
                        text(
                            "CREATE INDEX IF NOT EXISTS ix_appointments_department_id "
                            "ON appointments (department_id)"
                        )
                    )
                if "consultation_fee" not in cols:
                    conn.execute(
                        text(
                            "ALTER TABLE appointments "
                            "ADD COLUMN consultation_fee DOUBLE PRECISION NOT NULL DEFAULT 0"
                        )
                    )
                if "followup_eligibility" not in cols:
                    conn.execute(
                        text("ALTER TABLE appointments ADD COLUMN followup_eligibility VARCHAR(64)")
                    )
    except Exception as exc:
        logger.warning("consultation pricing migration skipped: %s", exc)


def _migrate_admissions_discharge_notes() -> None:
    """Add discharge_notes to admissions if missing."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        if "admissions" not in insp.get_table_names():
            return
        cols = {c["name"] for c in insp.get_columns("admissions")}
        if "discharge_notes" in cols:
            return
        logger.info("Adding discharge_notes column to admissions")
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE admissions ADD COLUMN discharge_notes TEXT"))
    except Exception as exc:
        logger.warning("admissions discharge_notes migration skipped: %s", exc)


def _migrate_hospital_users_shift_id() -> None:
    """Add shift_id to hospital_users if missing."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        if "hospital_users" not in insp.get_table_names():
            return
        cols = {c["name"] for c in insp.get_columns("hospital_users")}
        if "shift_id" in cols:
            return
        logger.info("Adding shift_id column to hospital_users")
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE hospital_users ADD COLUMN shift_id UUID"))
            conn.execute(
                text(
                    """
                    ALTER TABLE hospital_users
                    ADD CONSTRAINT fk_hospital_users_shift_id
                    FOREIGN KEY (shift_id) REFERENCES shift_types(id) ON DELETE SET NULL
                    """
                )
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_hospital_users_shift_id ON hospital_users (shift_id)"))
    except Exception as exc:
        logger.warning("hospital_users shift_id migration skipped: %s", exc)


def _migrate_hospital_users_doctor_profile() -> None:
    """Add first-class doctor profile columns to hospital_users if missing."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        if "hospital_users" not in insp.get_table_names():
            return
        cols = {c["name"] for c in insp.get_columns("hospital_users")}
        alters: list[str] = []
        if "specialization" not in cols:
            alters.append("ADD COLUMN specialization VARCHAR(255)")
        if "medical_registration_number" not in cols:
            alters.append("ADD COLUMN medical_registration_number VARCHAR(64)")
        if "qualification" not in cols:
            alters.append("ADD COLUMN qualification VARCHAR(255)")
        if "years_of_experience" not in cols:
            alters.append("ADD COLUMN years_of_experience INTEGER")
        if "consultation_room" not in cols:
            alters.append("ADD COLUMN consultation_room VARCHAR(128)")
        if not alters:
            return
        logger.info("Migrating hospital_users doctor profile columns")
        with engine.begin() as conn:
            for clause in alters:
                conn.execute(text(f"ALTER TABLE hospital_users {clause}"))
    except Exception as exc:
        logger.warning("hospital_users doctor profile migration skipped: %s", exc)


def _migrate_departments_optional_wing() -> None:
    """Allow departments without a wing."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        if "departments" not in insp.get_table_names():
            return
        cols = {c["name"]: c for c in insp.get_columns("departments")}
        wing_col = cols.get("wing_id")
        with engine.begin() as conn:
            if wing_col is not None and not wing_col.get("nullable", True):
                logger.info("Making departments.wing_id nullable")
                # Drop FK first so we can alter nullability / recreate with SET NULL.
                conn.execute(
                    text(
                        """
                        DO $$ DECLARE r record;
                        BEGIN
                          FOR r IN (
                            SELECT conname FROM pg_constraint
                            WHERE conrelid = 'departments'::regclass AND contype = 'f'
                              AND pg_get_constraintdef(oid) ILIKE '%wing_id%'
                          ) LOOP
                            EXECUTE format('ALTER TABLE departments DROP CONSTRAINT %I', r.conname);
                          END LOOP;
                        END $$;
                        """
                    )
                )
                conn.execute(text("ALTER TABLE departments ALTER COLUMN wing_id DROP NOT NULL"))
                conn.execute(
                    text(
                        """
                        ALTER TABLE departments
                        ADD CONSTRAINT fk_departments_wing_id
                        FOREIGN KEY (wing_id) REFERENCES wings(id) ON DELETE SET NULL
                        """
                    )
                )
            # Replace unique (hospital_id, wing_id, name) with (hospital_id, name).
            conn.execute(
                text(
                    """
                    DO $$ BEGIN
                      ALTER TABLE departments DROP CONSTRAINT IF EXISTS uq_dept_hospital_wing_name;
                    EXCEPTION WHEN undefined_object THEN NULL;
                    END $$;
                    """
                )
            )
            conn.execute(
                text(
                    """
                    DO $$ BEGIN
                      ALTER TABLE departments ADD CONSTRAINT uq_dept_hospital_name UNIQUE (hospital_id, name);
                    EXCEPTION WHEN duplicate_object THEN NULL;
                    END $$;
                    """
                )
            )
    except Exception as exc:
        logger.warning("departments optional wing migration skipped: %s", exc)


def _migrate_ot_rooms_and_surgery_links() -> None:
    """Add OT room master table columns onto existing ot_surgeries."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        if "ot_surgeries" not in insp.get_table_names():
            return
        cols = {c["name"] for c in insp.get_columns("ot_surgeries")}
        with engine.begin() as conn:
            if "department_id" not in cols:
                logger.info("Adding department_id to ot_surgeries")
                conn.execute(text("ALTER TABLE ot_surgeries ADD COLUMN department_id UUID"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ot_surgeries_department_id ON ot_surgeries (department_id)"))
            if "ot_room_id" not in cols:
                logger.info("Adding ot_room_id to ot_surgeries")
                conn.execute(text("ALTER TABLE ot_surgeries ADD COLUMN ot_room_id UUID"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ot_surgeries_ot_room_id ON ot_surgeries (ot_room_id)"))
            # FKs are created by create_all for new tables; for existing DBs add if missing.
            conn.execute(
                text(
                    """
                    DO $$ BEGIN
                      ALTER TABLE ot_surgeries
                        ADD CONSTRAINT fk_ot_surgeries_department_id
                        FOREIGN KEY (department_id) REFERENCES departments(id) ON DELETE SET NULL;
                    EXCEPTION WHEN duplicate_object THEN NULL;
                    END $$;
                    """
                )
            )
            conn.execute(
                text(
                    """
                    DO $$ BEGIN
                      ALTER TABLE ot_surgeries
                        ADD CONSTRAINT fk_ot_surgeries_ot_room_id
                        FOREIGN KEY (ot_room_id) REFERENCES ot_rooms(id) ON DELETE SET NULL;
                    EXCEPTION WHEN duplicate_object THEN NULL;
                    END $$;
                    """
                )
            )
    except Exception as exc:
        logger.warning("ot room/surgery link migration skipped: %s", exc)


def _migrate_ward_and_ot_pricing() -> None:
    """Add ward admission/bed rates, OT room base charge, and surgery charge snapshot."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        tables = set(insp.get_table_names())

        if "wards" in tables:
            cols = {c["name"] for c in insp.get_columns("wards")}
            alters: list[str] = []
            if "admission_fee" not in cols:
                alters.append("ADD COLUMN admission_fee DOUBLE PRECISION NOT NULL DEFAULT 0")
            if "bed_charge_per_day" not in cols:
                alters.append("ADD COLUMN bed_charge_per_day DOUBLE PRECISION NOT NULL DEFAULT 0")
            if alters:
                logger.info("Migrating wards pricing columns")
                with engine.begin() as conn:
                    for clause in alters:
                        conn.execute(text(f"ALTER TABLE wards {clause}"))

        if "ot_rooms" in tables:
            cols = {c["name"] for c in insp.get_columns("ot_rooms")}
            if "base_ot_charge" not in cols:
                logger.info("Adding base_ot_charge to ot_rooms")
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            "ALTER TABLE ot_rooms ADD COLUMN base_ot_charge DOUBLE PRECISION NOT NULL DEFAULT 0"
                        )
                    )

        if "ot_surgeries" in tables:
            cols = {c["name"] for c in insp.get_columns("ot_surgeries")}
            if "ot_charge_amount" not in cols:
                logger.info("Adding ot_charge_amount snapshot to ot_surgeries")
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            "ALTER TABLE ot_surgeries ADD COLUMN ot_charge_amount DOUBLE PRECISION NOT NULL DEFAULT 0"
                        )
                    )
    except Exception as exc:
        logger.warning("ward/OT pricing migration skipped: %s", exc)


def _migrate_billing_invoices_receipts() -> None:
    """Ensure invoice/receipt tables exist (create_all + enum safety for Postgres)."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        tables = set(insp.get_table_names())
        with engine.begin() as conn:
            for enum_name, values in (
                ("billing_invoice_status", ("draft", "generated", "paid", "cancelled")),
                ("billing_receipt_status", ("issued", "cancelled")),
            ):
                conn.execute(
                    text(
                        f"""
                        DO $$ BEGIN
                            CREATE TYPE {enum_name} AS ENUM ({", ".join(repr(v) for v in values)});
                        EXCEPTION
                            WHEN duplicate_object THEN null;
                        END $$;
                        """
                    )
                )
        # create_all in lifespan creates tables; re-run for late enum creation
        from app.models import Base as ModelsBase

        ModelsBase.metadata.create_all(
            bind=engine,
            tables=[
                t
                for name, t in ModelsBase.metadata.tables.items()
                if name in ("billing_invoices", "billing_invoice_lines", "billing_receipts")
            ],
        )
        logger.info(
            "Billing invoices/receipts ready (tables present: %s)",
            {n for n in ("billing_invoices", "billing_invoice_lines", "billing_receipts") if n in tables or True},
        )
    except Exception as exc:
        logger.warning("billing invoices/receipts migration skipped: %s", exc)


def _migrate_doctor_leaves_type_and_reason() -> None:
    from sqlalchemy import inspect, text

    from app.database import engine

    try:
        insp = inspect(engine)
        if "doctor_leaves" not in insp.get_table_names():
            return
        cols = {c["name"] for c in insp.get_columns("doctor_leaves")}
        with engine.begin() as conn:
            if "leave_type" not in cols:
                conn.execute(text("ALTER TABLE doctor_leaves ADD COLUMN leave_type VARCHAR(32)"))
            try:
                conn.execute(text("ALTER TABLE doctor_leaves ALTER COLUMN reason TYPE VARCHAR(500)"))
            except Exception:
                pass
    except Exception as exc:
        logger.warning("doctor_leaves leave_type/reason migration skipped: %s", exc)


def _migrate_appointment_linked_clinical() -> None:
    """Link lab/radiology orders and medical records to appointments."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        with engine.begin() as conn:
            if "lab_orders" in insp.get_table_names():
                cols = {c["name"] for c in insp.get_columns("lab_orders")}
                if "appointment_id" not in cols:
                    conn.execute(text("ALTER TABLE lab_orders ADD COLUMN appointment_id UUID"))
                    conn.execute(
                        text(
                            "ALTER TABLE lab_orders ADD CONSTRAINT fk_lab_orders_appointment_id "
                            "FOREIGN KEY (appointment_id) REFERENCES appointments(id) ON DELETE SET NULL"
                        )
                    )
            if "radiology_orders" in insp.get_table_names():
                cols = {c["name"] for c in insp.get_columns("radiology_orders")}
                if "appointment_id" not in cols:
                    conn.execute(text("ALTER TABLE radiology_orders ADD COLUMN appointment_id UUID"))
                    conn.execute(
                        text(
                            "ALTER TABLE radiology_orders ADD CONSTRAINT fk_radiology_orders_appointment_id "
                            "FOREIGN KEY (appointment_id) REFERENCES appointments(id) ON DELETE SET NULL"
                        )
                    )
            if "medical_records" in insp.get_table_names():
                cols = {c["name"] for c in insp.get_columns("medical_records")}
                if "appointment_id" not in cols:
                    conn.execute(text("ALTER TABLE medical_records ADD COLUMN appointment_id UUID"))
                if "lab_order_id" not in cols:
                    conn.execute(text("ALTER TABLE medical_records ADD COLUMN lab_order_id UUID"))
                if "radiology_order_id" not in cols:
                    conn.execute(text("ALTER TABLE medical_records ADD COLUMN radiology_order_id UUID"))
    except Exception as exc:
        logger.warning("appointment-linked clinical migration skipped: %s", exc)


class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        logger.info("→ %s %s", request.method, request.url.path)
        response = await call_next(request)
        logger.info("← %s %s %s", request.method, request.url.path, response.status_code)
        return response


@asynccontextmanager
async def lifespan(_: FastAPI):
    _migrate_shift_types_for_department()
    _migrate_rooms_bed_count()
    _migrate_patients_registration_fields()
    _migrate_appointments_extra_fields()
    _migrate_admissions_discharge_notes()
    _migrate_hospital_users_shift_id()
    _migrate_hospital_users_doctor_profile()
    _migrate_departments_optional_wing()
    Base.metadata.create_all(bind=engine)
    _migrate_appointment_linked_clinical()
    _migrate_doctor_leaves_type_and_reason()
    _migrate_ot_rooms_and_surgery_links()
    _migrate_ward_and_ot_pricing()
    _migrate_billing_invoices_receipts()
    _migrate_consultation_pricing()
    _migrate_lab_panels()
    _migrate_lab_prescription_requests()
    yield


app = FastAPI(
    title="Ultrion HMS API",
    description="Backend API for Ultrion Hospital Management System",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(RequestLogMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api")
app.include_router(hospitals.router, prefix="/api")
app.include_router(masters.router, prefix="/api")
app.include_router(admin.router, prefix="/api")
app.include_router(doctors.router, prefix="/api")
app.include_router(registration.router, prefix="/api")
app.include_router(appointment.router, prefix="/api")
app.include_router(beds.router, prefix="/api")
app.include_router(mis.router, prefix="/api")
app.include_router(analytics.router, prefix="/api")
app.include_router(laboratory.router, prefix="/api")
app.include_router(radiology.router, prefix="/api")
app.include_router(ot.router, prefix="/api")
app.include_router(dms.router, prefix="/api")
app.include_router(equipment.router, prefix="/api")
app.include_router(billing.router, prefix="/api")


@app.get("/")
def root():
    return {"service": "Ultrion HMS API", "docs": "/docs", "health": "/api/health"}


@app.get("/api/health")
@app.get("/health")
def health_check():
    return {"status": "ok"}
