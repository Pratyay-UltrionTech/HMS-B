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

from modules.inpatient.entities.admission import Admission

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
    """Generate next sequential IP encounter ID: IP-YYYY-NNNNN.

    Atomic under concurrency: values come from the tenant-scoped
    ``sequence_counters`` table (single upsert statement per caller, see
    :func:`shared.database.sequences.next_sequence_value`), so two
    simultaneous admissions can never receive the same number — unlike the
    previous MAX(ip_id)+1 read-modify-write. Legacy rows created before the
    counter existed are skipped rather than reused.
    """
    from shared.database.sequences import next_sequence_value

    year = year or date.today().year
    prefix = f"IP-{year}-"
    counter_name = f"ip_{year}"
    # Bound the skip loop: normally 1 iteration; legacy backfill at most a
    # handful since the counter only moves forward.
    for _ in range(1000):
        seq = next_sequence_value(db, hospital_id, counter_name)
        candidate = f"{prefix}{seq:05d}"
        exists = (
            db.query(Admission.id)
            .filter(Admission.hospital_id == hospital_id, Admission.ip_id == candidate)
            .first()
        )
        if not exists:
            return candidate
    raise RuntimeError("Unable to allocate unique IP encounter ID")


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
        from modules.billing.services.billing_service import ensure_admission_charge
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
        from modules.billing.services.billing_service import ensure_bed_charge_for_admission
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

    def ensure_financial_account(
        self,
        hospital_id: UUID,
        patient_id: UUID,
        admission_id: UUID,
        created_by_name: str = "System",
    ) -> Any:
        from modules.billing.entities.billing_entities import FinancialAccountType
        from modules.billing.services.billing_service import get_or_create_financial_account
        return get_or_create_financial_account(
            self.db,
            hospital_id=hospital_id,
            patient_id=patient_id,
            account_type=FinancialAccountType.ipd,
            admission_id=admission_id,
            created_by_name=created_by_name,
        )

    def get_ledger_totals(
        self,
        hospital_id: UUID,
        patient_id: UUID,
        admission_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Compute patient financial totals: charges, payments, outstanding balance.
        When admission_id is provided, scopes strictly to the admission's IPD FinancialAccount,
        preventing cross-episode debt leakage.
        """
        from modules.billing.entities.billing_entities import (
            BillingDeposit,
            DepositStatus,
            FinancialAccount,
            FinancialAccountType,
        )
        from modules.billing.services.billing_service import (
            get_or_create_financial_account,
            patient_ledger_totals,
        )

        account_id = None
        if admission_id:
            acc = (
                self.db.query(FinancialAccount)
                .filter(
                    FinancialAccount.hospital_id == hospital_id,
                    FinancialAccount.admission_id == admission_id,
                )
                .first()
            )
            if not acc:
                # Safe fallback / backfill for legacy admissions without an account
                acc = get_or_create_financial_account(
                    self.db,
                    hospital_id=hospital_id,
                    patient_id=patient_id,
                    account_type=FinancialAccountType.ipd,
                    admission_id=admission_id,
                    created_by_name="System",
                )
            account_id = acc.id

        totals = patient_ledger_totals(
            self.db, hospital_id, patient_id, account_id=account_id
        )

        if admission_id and account_id:
            extra_deps = (
                self.db.query(BillingDeposit)
                .filter(
                    BillingDeposit.hospital_id == hospital_id,
                    BillingDeposit.admission_id == admission_id,
                    BillingDeposit.account_id != account_id,
                    BillingDeposit.status.in_([DepositStatus.available, DepositStatus.partially_allocated]),
                )
                .all()
            )
            if extra_deps:
                extra_avail = round(sum(float(d.available_amount) for d in extra_deps), 2)
                totals["total_deposits_available"] = round(totals.get("total_deposits_available", 0.0) + extra_avail, 2)
                totals["net_patient_balance"] = round(totals.get("outstanding", 0.0) - totals["total_deposits_available"], 2)

        return totals

    def get_ledger_totals_bulk(
        self,
        hospital_id: UUID,
        patient_ids: list[UUID],
        admission_ids: list[UUID] | None = None,
    ) -> dict[UUID, dict[str, Any]]:
        """Compute financial totals for many patients in bulk queries."""
        from modules.billing.entities.billing_entities import FinancialAccount
        from modules.billing.services.billing_service import (
            patient_ledger_totals,
            patient_ledger_totals_bulk,
        )

        if admission_ids:
            # Per-admission scoping for discharge queue
            res: dict[UUID, dict[str, Any]] = {}
            for pid, aid in zip(patient_ids, admission_ids):
                res[pid] = self.get_ledger_totals(hospital_id, pid, admission_id=aid)
            return res

        return patient_ledger_totals_bulk(self.db, hospital_id, patient_ids)
