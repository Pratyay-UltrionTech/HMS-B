"""Repository for Inpatient Doctor Medication Orders and eMAR synchronization."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy.orm import Session, joinedload

from modules.inpatient.entities.medication_order import IpdMedicationOrder, MedicationOrderStatus
from modules.inpatient.entities.nursing_entities import (
    MedicationAdminStatus,
    MedicationAdministrationRecord,
)


from shared.exceptions.base import ValidationError

class MedicationOrderRepository:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    def list_orders(
        self, admission_id: UUID, status: MedicationOrderStatus | None = None
    ) -> list[IpdMedicationOrder]:
        q = (
            self.db.query(IpdMedicationOrder)
            .options(joinedload(IpdMedicationOrder.doctor))
            .filter(
                IpdMedicationOrder.hospital_id == self.hospital_id,
                IpdMedicationOrder.admission_id == admission_id,
            )
        )
        if status:
            q = q.filter(IpdMedicationOrder.status == status)
        return q.order_by(IpdMedicationOrder.created_at.desc()).all()

    def get_order_by_id(self, order_id: UUID) -> IpdMedicationOrder | None:
        return (
            self.db.query(IpdMedicationOrder)
            .options(joinedload(IpdMedicationOrder.doctor))
            .filter(
                IpdMedicationOrder.id == order_id,
                IpdMedicationOrder.hospital_id == self.hospital_id,
            )
            .first()
        )

    def create_order(
        self,
        *,
        admission_id: UUID,
        patient_id: UUID,
        doctor_id: UUID,
        doctor_name: str,
        medicine_name: str,
        dose: str,
        dosage_unit: str = "mg",
        route: str = "oral",
        frequency: str = "od",
        schedule_timing: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        duration_days: int | None = None,
        instructions: str | None = None,
        is_prn: bool = False,
        prn_indication: str | None = None,
        medicine_id: UUID | None = None,
    ) -> IpdMedicationOrder:
        now = datetime.now(timezone.utc)
        effective_start = start_time or now
        order = IpdMedicationOrder(
            hospital_id=self.hospital_id,
            admission_id=admission_id,
            patient_id=patient_id,
            doctor_id=doctor_id,
            doctor_name=doctor_name,
            medicine_id=medicine_id,
            medicine_name=medicine_name,
            dose=dose,
            dosage_unit=dosage_unit,
            route=route,
            frequency=frequency,
            schedule_timing=schedule_timing,
            start_time=effective_start,
            end_time=end_time,
            duration_days=duration_days,
            instructions=instructions,
            is_prn=is_prn,
            prn_indication=prn_indication,
            status=MedicationOrderStatus.active,
        )
        self.db.add(order)
        self.db.flush()

        # Section 5: PRN medication orders must NOT generate routine timed MAR administrations
        if not order.is_prn:
            self._populate_emar_schedules(order)
        self.db.flush()
        return order

    def _populate_emar_schedules(self, order: IpdMedicationOrder) -> None:
        """Create scheduled administration slots in eMAR deterministically without unsafe fallbacks."""
        freq_raw = (order.frequency or "od").strip().lower()
        
        interval_hours: int | None = None
        if freq_raw in ("od", "daily", "once daily", "q24h"):
            interval_hours = 24
        elif freq_raw in ("bd", "bid", "twice daily", "q12h", "1-0-1"):
            interval_hours = 12
        elif freq_raw in ("tds", "tid", "thrice daily", "q8h", "1-1-1"):
            interval_hours = 8
        elif freq_raw in ("qid", "four times daily", "q6h", "1-1-1-1"):
            interval_hours = 6
        elif freq_raw in ("hs", "bedtime"):
            interval_hours = 24
        elif freq_raw in ("stat", "immediately"):
            interval_hours = None
        else:
            raise ValidationError(
                f"Unsupported medication frequency '{order.frequency}'. "
                f"Supported structured frequencies: OD, BD, TDS, QID, STAT, HS, 1-0-1, 1-1-1, 1-1-1-1, q6h, q8h, q12h, q24h. "
                f"Never guess unknown schedules."
            )

        # Determine scheduling horizon (up to 7 days rolling)
        if order.duration_days and order.duration_days > 0:
            horizon_hours = min(order.duration_days * 24, 7 * 24)
        elif order.end_time:
            diff_hours = int((order.end_time - order.start_time).total_seconds() / 3600)
            horizon_hours = max(24, min(diff_hours, 7 * 24))
        else:
            horizon_hours = 48  # 48-hour standard rolling window

        if interval_hours is None:
            intervals_hours = [0]
        else:
            intervals_hours = list(range(0, horizon_hours, interval_hours))

        dose_label = f"{order.dose} {order.dosage_unit}".strip()
        for offset in intervals_hours:
            sched_time = order.start_time + timedelta(hours=offset)
            if order.end_time and sched_time > order.end_time:
                break

            # Idempotency guard: avoid duplicate MAR records if scheduler runs again
            existing = (
                self.db.query(MedicationAdministrationRecord.id)
                .filter(
                    MedicationAdministrationRecord.order_id == order.id,
                    MedicationAdministrationRecord.scheduled_time == sched_time,
                )
                .first()
            )
            if existing:
                continue

            emar_entry = MedicationAdministrationRecord(
                hospital_id=self.hospital_id,
                admission_id=order.admission_id,
                patient_id=order.patient_id,
                medicine_id=order.medicine_id,
                medicine_name=order.medicine_name,
                dose=dose_label,
                route=order.route,
                scheduled_time=sched_time,
                status=MedicationAdminStatus.scheduled,
                order_id=order.id,
                notes_or_reason=f"Doctor Order: {order.doctor_name} ({order.frequency})" + (f" | {order.instructions}" if order.instructions else ""),
            )
            self.db.add(emar_entry)

    def discontinue_order(
        self,
        order_id: UUID,
        discontinued_by_id: UUID,
        discontinued_by_name: str,
        reason: str,
    ) -> IpdMedicationOrder | None:
        """Discontinue order, preserving past administered records while withholding ALL still-pending scheduled doses."""
        order = self.get_order_by_id(order_id)
        if not order:
            return None
        now = datetime.now(timezone.utc)
        order.status = MedicationOrderStatus.discontinued
        order.discontinued_at = now
        order.discontinued_by_id = discontinued_by_id
        order.discontinued_by_name = discontinued_by_name
        order.discontinued_reason = reason

        # Section 7: Withhold ALL still-pending scheduled doses (including overdue ones)
        pending_emar = (
            self.db.query(MedicationAdministrationRecord)
            .filter(
                MedicationAdministrationRecord.order_id == order.id,
                MedicationAdministrationRecord.status == MedicationAdminStatus.scheduled,
            )
            .all()
        )
        for entry in pending_emar:
            entry.status = MedicationAdminStatus.withheld
            entry.notes_or_reason = f"Discontinued by Dr. {discontinued_by_name}: {reason}"

        self.db.flush()
        return order

    def record_prn_administration(
        self,
        *,
        order_id: UUID,
        administered_by_id: UUID,
        administered_by_name: str,
        dose_administered: str,
        route: str,
        indication_reason: str,
        notes: str | None = None,
        administered_at: datetime | None = None,
    ) -> MedicationAdministrationRecord:
        """Administer PRN medication order on-demand with explicit clinical indication."""
        order = self.get_order_by_id(order_id)
        if not order:
            raise ValidationError("Medication order not found")
        if order.status != MedicationOrderStatus.active:
            raise ValidationError(f"Cannot administer PRN dose: order is {order.status.value}")
        if not order.is_prn:
            raise ValidationError("Cannot administer via PRN workflow: order is not prescribed as PRN")

        admin_time = administered_at or datetime.now(timezone.utc)
        record = MedicationAdministrationRecord(
            hospital_id=self.hospital_id,
            admission_id=order.admission_id,
            patient_id=order.patient_id,
            medicine_id=order.medicine_id,
            medicine_name=order.medicine_name,
            dose=dose_administered or f"{order.dose} {order.dosage_unit}".strip(),
            route=route or order.route,
            scheduled_time=admin_time,
            administered_at=admin_time,
            administering_nurse_id=administered_by_id,
            administering_nurse_name=administered_by_name,
            status=MedicationAdminStatus.administered,
            order_id=order.id,
            notes_or_reason=f"PRN Administered: {indication_reason}" + (f" | {notes}" if notes else ""),
        )
        self.db.add(record)
        self.db.flush()
        return record
