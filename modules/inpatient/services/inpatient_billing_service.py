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
        discharged_at: datetime | None = None,
        as_of: datetime | None = None,
        ward_name: str | None = None,
        room_code: str | None = None,
        bed_code: str | None = None,
        bed_charge_per_day: float = 0.0,
        created_by_name: str = "System",
    ) -> None:
        """Create or reconcile bed stay fee charge using target billing service."""
        from modules.billing.services.billing_service import ensure_bed_charge_for_admission
        ensure_bed_charge_for_admission(
            self.db,
            hospital_id=hospital_id,
            patient_id=patient_id,
            admission_id=admission_id,
            admitted_at=admitted_at,
            discharged_at=discharged_at,
            as_of=as_of,
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

    def ensure_bed_charges_bulk(
        self,
        hospital_id: UUID,
        admissions: list[Any],
        now: datetime,
    ) -> None:
        """Bulk reconcile bed charges for admissions upon discharge queue read."""
        if not admissions:
            return
        from collections import defaultdict
        from modules.billing.entities.billing_entities import (
            BillingCharge,
            BillingChargeStatus,
            BillingSourceType,
            FinancialAccount,
            FinancialAccountStatus,
            FinancialAccountType,
        )
        from modules.inpatient.entities.admission import BedStaySegment
        from modules.billing.services.billing_service import (
            bed_stay_days,
            compute_net,
            next_financial_account_number,
        )

        admission_ids = [a.id for a in admissions]

        # 1. Bulk load existing bed charges
        existing_charges = (
            self.db.query(BillingCharge)
            .filter(
                BillingCharge.hospital_id == hospital_id,
                BillingCharge.source_type == BillingSourceType.bed,
                BillingCharge.source_id.in_(admission_ids),
                BillingCharge.status != BillingChargeStatus.cancelled,
            )
            .all()
        )
        existing_by_adm = {c.source_id: c for c in existing_charges}

        # 2. Bulk load bed stay segments
        segments = (
            self.db.query(BedStaySegment)
            .filter(
                BedStaySegment.hospital_id == hospital_id,
                BedStaySegment.admission_id.in_(admission_ids),
            )
            .order_by(BedStaySegment.started_at.asc())
            .all()
        )
        segments_by_adm = defaultdict(list)
        for s in segments:
            segments_by_adm[s.admission_id].append(s)

        to_create = []
        to_update = []
        for a in admissions:
            adm_segments = segments_by_adm.get(a.id, [])
            expected_amount = 0.0
            total_days = 0
            notes_parts = []
            if adm_segments:
                for s in adm_segments:
                    seg_start = s.started_at
                    seg_end = s.ended_at or now
                    seg_days = bed_stay_days(seg_start, seg_end)
                    seg_rate = float(s.rate_per_day or 0.0)
                    seg_amount = round(seg_rate * seg_days, 2)
                    expected_amount += seg_amount
                    total_days += seg_days
                    notes_parts.append(f"seg(rate={seg_rate}, days={seg_days}, amt={seg_amount})")
            else:
                ward = getattr(a, "ward", None)
                bed_charge_rate = float(getattr(ward, "bed_charge_per_day", 0) or 0) if ward else 0.0
                days = bed_stay_days(a.admitted_at, now)
                expected_amount = round(bed_charge_rate * days, 2)
                total_days = days

            if expected_amount <= 0:
                continue

            days = max(1, total_days)
            day_label = "Day" if days == 1 else "Days"
            place = " / ".join(
                p
                for p in [
                    getattr(getattr(a, "ward", None), "name", None),
                    getattr(getattr(a, "room", None), "room_code", None),
                    getattr(getattr(a, "bed", None), "bed_code", None),
                ]
                if p
            ) or "Bed"
            description = f"Bed Charges ({days} {day_label}) — {place}"[:512]
            notes = f"multi_segment; days={days}; {'; '.join(notes_parts)}"[:512] if notes_parts else None

            existing = existing_by_adm.get(a.id)
            if existing:
                if abs(float(existing.net_amount or 0) - expected_amount) >= 0.01:
                    to_update.append((existing, expected_amount, days, description, notes))
            else:
                to_create.append((a, expected_amount, days, description, notes))

        if not to_create and not to_update:
            return

        for existing, expected_amount, days, description, notes in to_update:
            existing.charge_amount = expected_amount
            disc, net = compute_net(
                expected_amount,
                existing.discount_amount or 0.0,
                existing.discount_percent,
                existing.tax_amount or 0.0,
            )
            existing.net_amount = net
            existing.quantity = float(days)
            existing.unit_price = round(expected_amount / days, 2) if days > 0 else 0.0
            existing.description = description
            if notes:
                existing.notes = notes

        if to_create:
            needed_adm_ids = [a.id for a, _, _, _, _ in to_create]
            existing_accounts = (
                self.db.query(FinancialAccount)
                .filter(
                    FinancialAccount.hospital_id == hospital_id,
                    FinancialAccount.account_type == FinancialAccountType.ipd,
                    FinancialAccount.admission_id.in_(needed_adm_ids),
                )
                .all()
            )
            account_map = {acc.admission_id: acc for acc in existing_accounts if acc.admission_id}

            new_charges = []
            new_accounts = []
            for a, expected_amount, days, description, notes in to_create:
                acc = account_map.get(a.id)
                if not acc:
                    acc_num = next_financial_account_number(self.db, hospital_id)
                    acc = FinancialAccount(
                        hospital_id=hospital_id,
                        patient_id=a.patient_id,
                        account_number=acc_num,
                        account_type=FinancialAccountType.ipd,
                        status=FinancialAccountStatus.open,
                        admission_id=a.id,
                        created_by_name="System",
                    )
                    new_accounts.append(acc)
                    account_map[a.id] = acc

            if new_accounts:
                self.db.add_all(new_accounts)
                self.db.flush()

            for a, expected_amount, days, description, notes in to_create:
                acc = account_map[a.id]
                disc, net = compute_net(expected_amount, 0.0, None, 0.0)
                status = BillingChargeStatus.paid if net <= 0 else BillingChargeStatus.pending
                actual_unit_price = round(expected_amount / days, 2) if days > 0 else 0.0

                chg = BillingCharge(
                    hospital_id=hospital_id,
                    patient_id=a.patient_id,
                    account_id=acc.id,
                    source_type=BillingSourceType.bed,
                    source_id=a.id,
                    description=description,
                    quantity=float(days),
                    unit_price=actual_unit_price,
                    charge_amount=round(float(expected_amount), 2),
                    discount_amount=disc,
                    discount_percent=None,
                    discount_reason=None,
                    tax_amount=0.0,
                    gst_rate=0.0,
                    hsn_sac_code=None,
                    net_amount=net,
                    amount_paid=net if status == BillingChargeStatus.paid else 0.0,
                    status=status,
                    notes=notes,
                    created_by_name="System",
                )
                new_charges.append(chg)

            if new_charges:
                self.db.add_all(new_charges)
                self.db.flush()

    def get_ledger_totals_bulk(
        self,
        hospital_id: UUID,
        patient_ids: list[UUID],
        admission_ids: list[UUID] | None = None,
    ) -> dict[UUID, dict[str, Any]]:
        """Compute financial totals for many patients in bulk queries."""
        from modules.billing.services.billing_service import patient_ledger_totals_bulk

        if not admission_ids:
            return patient_ledger_totals_bulk(self.db, hospital_id, patient_ids)

        from sqlalchemy import case, func, or_
        from modules.billing.entities.billing_entities import (
            BillingCharge,
            BillingChargeStatus,
            BillingDeposit,
            BillingPayment,
            DepositStatus,
            FinancialAccount,
            FinancialAccountType,
        )

        res: dict[UUID, dict[str, Any]] = {}
        if not patient_ids:
            return res

        # 1. Bulk resolve FinancialAccount for all admissions (read-only, no write mutations on read path)
        existing_accounts = (
            self.db.query(FinancialAccount)
            .filter(
                FinancialAccount.hospital_id == hospital_id,
                FinancialAccount.admission_id.in_(admission_ids),
            )
            .all()
        )
        acc_by_adm = {acc.admission_id: acc for acc in existing_accounts if acc.admission_id}

        account_ids: list[UUID] = []
        adm_to_acc_id: dict[UUID, UUID] = {}
        for pid, aid in zip(patient_ids, admission_ids):
            acc = acc_by_adm.get(aid)
            if acc:
                adm_to_acc_id[aid] = acc.id
                account_ids.append(acc.id)

        # 2. Bulk aggregate charges by account_id in a single query
        chg_data: dict[UUID, dict[str, Any]] = {}
        if account_ids:
            chg_rows = (
                self.db.query(
                    BillingCharge.account_id,
                    func.coalesce(func.sum(BillingCharge.net_amount), 0).label("tot_net"),
                    func.coalesce(func.sum(BillingCharge.amount_paid), 0).label("tot_paid_alloc"),
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    BillingCharge.net_amount > BillingCharge.amount_paid,
                                    BillingCharge.net_amount - BillingCharge.amount_paid,
                                ),
                                else_=0.0,
                            )
                        ),
                        0,
                    ).label("tot_out"),
                    func.count(BillingCharge.id).label("cnt"),
                )
                .filter(
                    BillingCharge.hospital_id == hospital_id,
                    BillingCharge.account_id.in_(account_ids),
                    BillingCharge.status != BillingChargeStatus.cancelled,
                )
                .group_by(BillingCharge.account_id)
                .all()
            )
            for r in chg_rows:
                chg_data[r.account_id] = {
                    "total_charges": round(float(r.tot_net or 0), 2),
                    "total_paid_alloc": round(float(r.tot_paid_alloc or 0), 2),
                    "outstanding": round(float(r.tot_out or 0), 2),
                    "charge_count": int(r.cnt or 0),
                }

        # 3. Bulk aggregate payments by account_id in a single query
        pay_data: dict[UUID, dict[str, Any]] = {}
        if account_ids:
            pay_rows = (
                self.db.query(
                    BillingPayment.account_id,
                    func.coalesce(func.sum(BillingPayment.amount), 0).label("tot_tender"),
                    func.count(BillingPayment.id).label("cnt"),
                )
                .filter(
                    BillingPayment.hospital_id == hospital_id,
                    BillingPayment.account_id.in_(account_ids),
                )
                .group_by(BillingPayment.account_id)
                .all()
            )
            for r in pay_rows:
                pay_data[r.account_id] = {
                    "total_paid_tender": round(float(r.tot_tender or 0), 2),
                    "payment_count": int(r.cnt or 0),
                }

        # 4. Bulk aggregate deposits (by account_id and by admission_id) in a single query
        dep_rows = (
            self.db.query(
                BillingDeposit.account_id,
                BillingDeposit.admission_id,
                func.coalesce(func.sum(BillingDeposit.available_amount), 0).label("tot_dep"),
            )
            .filter(
                BillingDeposit.hospital_id == hospital_id,
                BillingDeposit.status.in_([DepositStatus.available, DepositStatus.partially_allocated]),
                or_(
                    BillingDeposit.account_id.in_(account_ids),
                    BillingDeposit.admission_id.in_(admission_ids),
                ),
            )
            .group_by(BillingDeposit.account_id, BillingDeposit.admission_id)
            .all()
        )
        dep_by_acc: dict[UUID, float] = {}
        extra_dep_by_adm: dict[UUID, float] = {}
        for r in dep_rows:
            tot_dep = round(float(r.tot_dep or 0), 2)
            if r.account_id:
                dep_by_acc[r.account_id] = round(dep_by_acc.get(r.account_id, 0.0) + tot_dep, 2)
            if r.admission_id and r.account_id != adm_to_acc_id.get(r.admission_id):
                extra_dep_by_adm[r.admission_id] = round(extra_dep_by_adm.get(r.admission_id, 0.0) + tot_dep, 2)

        # 5. Assemble dictionary per patient
        for pid, aid in zip(patient_ids, admission_ids):
            acc_id = adm_to_acc_id.get(aid)
            c_inf = chg_data.get(acc_id, {})
            p_inf = pay_data.get(acc_id, {})
            total_charges = c_inf.get("total_charges", 0.0)
            total_paid = c_inf.get("total_paid_alloc", 0.0)
            outstanding = c_inf.get("outstanding", 0.0)
            charge_count = c_inf.get("charge_count", 0)
            payment_count = p_inf.get("payment_count", 0)

            acc_dep = dep_by_acc.get(acc_id, 0.0)
            extra_dep = extra_dep_by_adm.get(aid, 0.0)
            total_deposits_avail = round(acc_dep + extra_dep, 2)
            net_patient_balance = round(outstanding - total_deposits_avail, 2)

            res[pid] = {
                "total_charges": total_charges,
                "total_paid": total_paid,
                "total_deposits_available": total_deposits_avail,
                "outstanding": outstanding,
                "net_patient_balance": net_patient_balance,
                "charge_count": charge_count,
                "payment_count": payment_count,
            }
        return res
