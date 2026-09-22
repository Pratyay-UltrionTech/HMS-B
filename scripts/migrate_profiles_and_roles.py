"""
Migration script for HMS Hospital User Profiles, Roles, Multiple-Role Support,
Permissions, Department/Location Scope, and 26-Module RBAC Actions.

Run with:
    python -m scripts.migrate_profiles_and_roles
"""

import logging
import uuid
from sqlalchemy import text
from infrastructure.postgres.engine import get_transitional_sync_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hms.scripts.migrate_profiles_and_roles")

DDL_STATEMENTS = [
    # 1. New direct department column on hospital_users
    """
    ALTER TABLE hospital_users
    ADD COLUMN IF NOT EXISTS department_id UUID REFERENCES departments(id) ON DELETE SET NULL;
    """,
    # 2. Extended action permission flags on role_permissions
    """
    ALTER TABLE role_permissions
    ADD COLUMN IF NOT EXISTS can_create BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS can_delete BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS can_approve BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS can_validate BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS can_release BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS can_dispense BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS can_refund BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS can_cancel BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS can_administer BOOLEAN NOT NULL DEFAULT FALSE;
    """,
    # 3. hospital_user_roles table
    """
    CREATE TABLE IF NOT EXISTS hospital_user_roles (
        id UUID PRIMARY KEY,
        hospital_id UUID NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
        user_id UUID NOT NULL REFERENCES hospital_users(id) ON DELETE CASCADE,
        role_id UUID NOT NULL REFERENCES staff_roles(id) ON DELETE RESTRICT,
        is_primary BOOLEAN NOT NULL DEFAULT FALSE,
        assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        assigned_by UUID REFERENCES hospital_users(id) ON DELETE SET NULL,
        expires_at TIMESTAMPTZ NULL,
        CONSTRAINT uq_user_role UNIQUE (user_id, role_id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_hospital_user_roles_user ON hospital_user_roles(hospital_id, user_id);",
    # 4. hospital_user_departments table
    """
    CREATE TABLE IF NOT EXISTS hospital_user_departments (
        id UUID PRIMARY KEY,
        hospital_id UUID NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
        user_id UUID NOT NULL REFERENCES hospital_users(id) ON DELETE CASCADE,
        department_id UUID NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
        is_primary BOOLEAN NOT NULL DEFAULT FALSE,
        can_supervise BOOLEAN NOT NULL DEFAULT FALSE,
        assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_user_dept UNIQUE (user_id, department_id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_hospital_user_departments_user ON hospital_user_departments(hospital_id, user_id);",
    # 5. hospital_user_locations table
    """
    CREATE TABLE IF NOT EXISTS hospital_user_locations (
        id UUID PRIMARY KEY,
        hospital_id UUID NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
        user_id UUID NOT NULL REFERENCES hospital_users(id) ON DELETE CASCADE,
        ward_id UUID NULL REFERENCES wards(id) ON DELETE CASCADE,
        wing_id UUID NULL REFERENCES wings(id) ON DELETE CASCADE,
        assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_user_ward UNIQUE (user_id, ward_id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_hospital_user_locations_user ON hospital_user_locations(hospital_id, user_id);",
    # 6. practitioners table
    """
    CREATE TABLE IF NOT EXISTS practitioners (
        id UUID PRIMARY KEY,
        hospital_id UUID NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
        user_id UUID NOT NULL REFERENCES hospital_users(id) ON DELETE CASCADE,
        designation VARCHAR(128) NOT NULL,
        medical_registration_number VARCHAR(64) NULL,
        qualification VARCHAR(255) NULL,
        years_of_experience INTEGER NULL,
        digital_signature TEXT NULL,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_practitioner_user UNIQUE (hospital_id, user_id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_practitioners_hospital_user ON practitioners(hospital_id, user_id);"
]

# Standard role definitions with default permissions for all 26 modules
STANDARD_ROLES_PERMISSIONS = {
    "doctor": {
        "description": "Attending Physician / Specialist",
        "modules": {
            "dashboard": {"view": True, "edit": False, "create": False, "delete": False},
            "patients": {"view": True, "edit": True, "create": True, "delete": False},
            "appointments": {"view": True, "edit": True, "create": True, "delete": True, "cancel": True},
            "doctors": {"view": True, "edit": True, "create": False, "delete": False},
            "clinical": {"view": True, "edit": True, "create": True, "delete": False, "approve": True},
            "emergency": {"view": True, "edit": True, "create": True, "delete": False},
            "critical_care": {"view": True, "edit": True, "create": True, "delete": False},
            "pharmacy": {"view": True, "edit": False, "create": False, "delete": False},
            "laboratory": {"view": True, "edit": False, "create": False, "delete": False},
            "radiology": {"view": True, "edit": False, "create": False, "delete": False},
            "blood_bank": {"view": True, "edit": False, "create": False, "delete": False},
            "clinical_decision": {"view": True, "edit": True, "create": True, "delete": False},
            "ipd": {"view": True, "edit": True, "create": True, "delete": False},
            "ot": {"view": True, "edit": True, "create": True, "delete": False},
        }
    },
    "nurse": {
        "description": "Ward / Staff Nurse",
        "modules": {
            "dashboard": {"view": True, "edit": False, "create": False, "delete": False},
            "patients": {"view": True, "edit": True, "create": False, "delete": False},
            "ipd": {"view": True, "edit": True, "create": True, "delete": False, "administer": True},
            "emergency": {"view": True, "edit": True, "create": True, "delete": False, "administer": True},
            "critical_care": {"view": True, "edit": True, "create": True, "delete": False, "administer": True},
            "pharmacy": {"view": True, "edit": False, "create": False, "delete": False},
            "laboratory": {"view": True, "edit": False, "create": False, "delete": False},
            "blood_bank": {"view": True, "edit": False, "create": False, "delete": False},
            "cssd": {"view": True, "edit": False, "create": False, "delete": False},
        }
    },
    "pharmacist": {
        "description": "Pharmacist / Dispensary In-Charge",
        "modules": {
            "dashboard": {"view": True, "edit": False, "create": False, "delete": False},
            "pharmacy": {"view": True, "edit": True, "create": True, "delete": False, "dispense": True, "approve": True},
            "inventory": {"view": True, "edit": True, "create": True, "delete": False},
            "procurement": {"view": True, "edit": False, "create": True, "delete": False},
        }
    },
    "lab_technician": {
        "description": "Pathology / Laboratory Technician",
        "modules": {
            "dashboard": {"view": True, "edit": False, "create": False, "delete": False},
            "laboratory": {"view": True, "edit": True, "create": True, "delete": False, "validate": True},
            "blood_bank": {"view": True, "edit": True, "create": True, "delete": False, "validate": True},
        }
    },
    "radiologist": {
        "description": "Radiology Specialist / Radiographer",
        "modules": {
            "dashboard": {"view": True, "edit": False, "create": False, "delete": False},
            "radiology": {"view": True, "edit": True, "create": True, "delete": False, "release": True, "approve": True},
        }
    },
    "receptionist": {
        "description": "Front Desk / Registration Executive",
        "modules": {
            "dashboard": {"view": True, "edit": False, "create": False, "delete": False},
            "patients": {"view": True, "edit": True, "create": True, "delete": False},
            "appointments": {"view": True, "edit": True, "create": True, "delete": True, "cancel": True},
            "queue": {"view": True, "edit": True, "create": True, "delete": False},
            "emergency": {"view": True, "edit": True, "create": True, "delete": False},
            "billing": {"view": True, "edit": False, "create": False, "delete": False},
        }
    },
    "cashier": {
        "description": "Billing & Cash Counter Officer",
        "modules": {
            "dashboard": {"view": True, "edit": False, "create": False, "delete": False},
            "billing": {"view": True, "edit": True, "create": True, "delete": False, "refund": True, "cancel": True},
            "insurance": {"view": True, "edit": True, "create": True, "delete": False},
            "patients": {"view": True, "edit": False, "create": False, "delete": False},
        }
    },
    "store_keeper": {
        "description": "Material & Inventory Store Keeper",
        "modules": {
            "dashboard": {"view": True, "edit": False, "create": False, "delete": False},
            "inventory": {"view": True, "edit": True, "create": True, "delete": False},
            "procurement": {"view": True, "edit": True, "create": True, "delete": False},
            "cssd": {"view": True, "edit": True, "create": True, "delete": False},
        }
    }
}


def main():
    engine = get_transitional_sync_engine()
    with engine.begin() as conn:
        logger.info("Executing DDL statements...")
        for stmt in DDL_STATEMENTS:
            conn.execute(text(stmt))
        logger.info("DDL applied successfully.")

        # 1. Backfill hospital_users.role_id into hospital_user_roles
        logger.info("Backfilling hospital_user_roles from hospital_users.role_id...")
        backfill_roles_sql = """
        INSERT INTO hospital_user_roles (id, hospital_id, user_id, role_id, is_primary, assigned_at)
        SELECT 
            gen_random_uuid(),
            u.hospital_id,
            u.id,
            u.role_id,
            TRUE,
            NOW()
        FROM hospital_users u
        WHERE u.role_id IS NOT NULL
        ON CONFLICT (user_id, role_id) DO NOTHING;
        """
        conn.execute(text(backfill_roles_sql))

        # 2. Backfill hospital_user_departments from shifts if user has no department assigned
        logger.info("Backfilling departments from shifts...")
        backfill_dept_sql = """
        UPDATE hospital_users u
        SET department_id = s.department_id
        FROM shift_types s
        WHERE s.id = u.shift_id AND s.department_id IS NOT NULL AND u.department_id IS NULL;
        """
        conn.execute(text(backfill_dept_sql))

        # Also insert primary department into hospital_user_departments
        backfill_user_dept_sql = """
        INSERT INTO hospital_user_departments (id, hospital_id, user_id, department_id, is_primary, can_supervise, assigned_at)
        SELECT 
            gen_random_uuid(),
            u.hospital_id,
            u.id,
            u.department_id,
            TRUE,
            FALSE,
            NOW()
        FROM hospital_users u
        WHERE u.department_id IS NOT NULL
        ON CONFLICT (user_id, department_id) DO NOTHING;
        """
        conn.execute(text(backfill_user_dept_sql))

        # 3. Synchronize action permission flags for existing role_permissions
        logger.info("Backfilling action flags on existing role_permissions...")
        action_backfill_sql = """
        UPDATE role_permissions
        SET 
            can_create = CASE WHEN can_create = FALSE AND can_edit = TRUE THEN TRUE ELSE can_create END,
            can_dispense = CASE WHEN module_key = 'pharmacy' AND can_edit = TRUE THEN TRUE ELSE can_dispense END,
            can_validate = CASE WHEN module_key IN ('laboratory', 'blood_bank') AND can_edit = TRUE THEN TRUE ELSE can_validate END,
            can_release = CASE WHEN module_key = 'radiology' AND can_edit = TRUE THEN TRUE ELSE can_release END,
            can_administer = CASE WHEN module_key IN ('ipd', 'emergency', 'critical_care') AND can_edit = TRUE THEN TRUE ELSE can_administer END,
            can_refund = CASE WHEN module_key = 'billing' AND can_edit = TRUE THEN TRUE ELSE can_refund END,
            can_cancel = CASE WHEN module_key IN ('billing', 'appointments') AND can_edit = TRUE THEN TRUE ELSE can_cancel END,
            can_approve = CASE WHEN module_key IN ('clinical', 'procurement', 'pharmacy') AND can_edit = TRUE THEN TRUE ELSE can_approve END
        WHERE can_edit = TRUE;
        """
        conn.execute(text(action_backfill_sql))

        # 4. Seed standard roles for existing hospitals if missing
        logger.info("Ensuring standard roles and permissions exist across all hospitals in staff_roles...")
        hospitals_res = conn.execute(text("SELECT id FROM hospitals;")).fetchall()
        for h_row in hospitals_res:
            h_id = h_row[0]
            for role_name, role_data in STANDARD_ROLES_PERMISSIONS.items():
                existing_role = conn.execute(
                    text("SELECT id FROM staff_roles WHERE hospital_id = :h_id AND name = :name;"),
                    {"h_id": h_id, "name": role_name}
                ).fetchone()
                
                if existing_role:
                    role_id = existing_role[0]
                else:
                    role_id = uuid.uuid4()
                    conn.execute(
                        text("""
                        INSERT INTO staff_roles (id, hospital_id, name, description, is_active, created_at)
                        VALUES (:id, :h_id, :name, :desc, TRUE, NOW());
                        """),
                        {"id": role_id, "h_id": h_id, "name": role_name, "desc": role_data["description"]}
                    )
                
                # Seed module permissions
                for mod_key, perms in role_data["modules"].items():
                    conn.execute(
                        text("""
                        INSERT INTO role_permissions (
                            id, hospital_id, role_id, module_key, can_view, can_edit,
                            can_create, can_delete, can_approve, can_validate, can_release,
                            can_dispense, can_refund, can_cancel, can_administer
                        )
                        VALUES (
                            gen_random_uuid(), :h_id, :role_id, :mod_key, :can_view, :can_edit,
                            :can_create, :can_delete, :can_approve, :can_validate, :can_release,
                            :can_dispense, :can_refund, :can_cancel, :can_administer
                        )
                        ON CONFLICT (role_id, module_key) DO UPDATE SET
                            can_view = EXCLUDED.can_view,
                            can_edit = EXCLUDED.can_edit,
                            can_create = EXCLUDED.can_create,
                            can_delete = EXCLUDED.can_delete,
                            can_approve = EXCLUDED.can_approve,
                            can_validate = EXCLUDED.can_validate,
                            can_release = EXCLUDED.can_release,
                            can_dispense = EXCLUDED.can_dispense,
                            can_refund = EXCLUDED.can_refund,
                            can_cancel = EXCLUDED.can_cancel,
                            can_administer = EXCLUDED.can_administer;
                        """),
                        {
                            "h_id": h_id,
                            "role_id": role_id,
                            "mod_key": mod_key,
                            "can_view": perms.get("view", False),
                            "can_edit": perms.get("edit", False),
                            "can_create": perms.get("create", False),
                            "can_delete": perms.get("delete", False),
                            "can_approve": perms.get("approve", False),
                            "can_validate": perms.get("validate", False),
                            "can_release": perms.get("release", False),
                            "can_dispense": perms.get("dispense", False),
                            "can_refund": perms.get("refund", False),
                            "can_cancel": perms.get("cancel", False),
                            "can_administer": perms.get("administer", False),
                        }
                    )

    logger.info("Migration complete!")


if __name__ == "__main__":
    main()
