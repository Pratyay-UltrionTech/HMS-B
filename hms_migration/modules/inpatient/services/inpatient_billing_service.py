"""
Billing and encounter ID integration service for Inpatient domain.

Conforms to UltrionTech-Backend-Template modules/inpatient/services/ specification.
Calculates bed stay days, records admission and bed charges, and computes ledger totals.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import math
from typing import Any
from uuid import UUID

from sqlalchemy import column, func, select, table
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from hms_migration.modules.inpatient.entities.admission import Admission

# Lightweight Core tables for billing records without circular ORM ties
_billing_charges = table(
    "billing_charges",
    column("id", PG_UUID(as_uuid=True)),
    column("hospital_id", PG_UUID(as_uuid=True)),
    column("patient_id", PG_UUID(as_uuid=True)),
    column("source_type", PG_UUID(as_uuid=True)),
    column("source_id", PG_UUID(as_uuid=True)),
    column("description", PG_UUID(as_uuid=True)),
    column("charge_amount", PG_UUID(as_uuid=True)),
    column("discount_amount", PG_UUID(as_uuid=True)),
    column("net_amount", PG_UUID(as_uuid=True)),
    column("amount_paid", PG_UUID(as_uuid=True)),
    column("status", PG_UUID(as_uuid=True)),
    column("notes", PG_UUID(as_uuid=True)),
    column("created_by_name", PG_UUID(as_uuid=True)),
    column("created_at", PG_UUID(as_uuid=True)),
)


def next_ip_encounter_id(db: Session, hospital_id: UUID, year: int | None = None) -> str:
    """Generate next sequential IP encounter ID: IP-YYYY-NNNNN."""
    year = year or date.today().year
    prefix = f"IP-{year}-"
    current = (
        db.query(func.max(Admission.ip_id))
        .filter(Admission.hospital_id == hospital_id, Admission.ip_id.like(f"{prefix}%"))
        .scalar()
    )
    seq = 0
    if current:
        try:
            seq = int(str(current).rsplit("-", 1)[-1])
        except ValueError:
            seq = 0
    return f"{prefix}{seq + 1:05d}"


def calculate_bed_stay_days(admitted_at: datetime, discharged_at: datetime) -> int:
    """Calculate billable bed days: ceil(hours/24), minimum 1 day."""
    start = admitted_at if admitted_at.tzinfo else admitted_at.replace(tzinfo=timezone.utc)
    end = discharged_at if discharged_at.tzinfo else discharged_at.replace(tzinfo=timezone.utc)
    hours = max(0.0, (end - start).total_seconds() / 3600.0)
    if hours <= 0:
        return 1
    return max(1, int(math.ceil(hours / 24.0)))


class InpatientBillingService:
    """Service providing financial checks and charge recording for inpatient admissions."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def ensure_admission_charge(
        self,
        hospital_id: UUID,
        patient_id: UUID,
        admission_id: UUID,
        ward_name: str | None,
        admission_fee: float,
        created_by_name: str = "System",
    ) -> None:
        """Create admission fee charge using legacy billing bridge if available, or Core insert."""
        from hms_migration.modules.billing.services.billing_service import ensure_admission_charge
        ensure_admission_charge(
            self.db,
            hospital_id=hospital_id,
            patient_id=patient_id,
            admission_id=admission_id,
            ward_name=ward_name,
            admission_fee=admission_fee,
            created_by_name=created_by_name,
        )

    def ensure_bed_charge(
        self,
        hospital_id: UUID,
        patient_id: UUID,
        admission_id: UUID,
        admitted_at: datetime,
        discharged_at: datetime,
        ward_name: str | None,
        room_code: str | None,
        bed_code: str | None,
        bed_charge_per_day: float,
        created_by_name: str = "System",
    ) -> None:
        """Create bed stay fee charge using target billing service."""
        from hms_migration.modules.billing.services.billing_service import ensure_bed_charge_for_admission
        ensure_bed_charge_for_admission(
            self.db,
            hospital_id=hospital_id,
            patient_id=patient_id,
            admission_id=admission_id,
            admitted_at=admitted_at,
            discharged_at=discharged_at,
            ward_name=ward_name,
            room_code=room_code,
            bed_code=bed_code,
            bed_charge_per_day=bed_charge_per_day,
            created_by_name=created_by_name,
        )

    def get_ledger_totals(self, hospital_id: UUID, patient_id: UUID) -> dict[str, Any]:
        """Compute patient financial totals: charges, payments, outstanding balance."""
        from hms_migration.modules.billing.services.billing_service import patient_ledger_totals
        return patient_ledger_totals(self.db, hospital_id, patient_id)
