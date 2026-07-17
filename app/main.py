import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings
from app.database import Base, engine
from app.routers import auth, hospitals, masters, admin, doctors, registration, appointment, beds, mis, analytics, laboratory, radiology, ot, dms, equipment
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
        if "blood_group" not in cols:
            alters.append("ADD COLUMN blood_group VARCHAR(16)")
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
    _migrate_departments_optional_wing()
    Base.metadata.create_all(bind=engine)
    _migrate_ot_rooms_and_surgery_links()
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


@app.get("/")
def root():
    return {"service": "Ultrion HMS API", "docs": "/docs", "health": "/api/health"}


@app.get("/api/health")
@app.get("/health")
def health_check():
    return {"status": "ok"}
