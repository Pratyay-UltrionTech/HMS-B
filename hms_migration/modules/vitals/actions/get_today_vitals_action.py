"""
Action to retrieve today's appointments with recorded vitals.

Enforces OPD queue business rule: appointments marked 'waiting' that do not
actually have recorded vital readings are automatically reverted to 'scheduled'.
"""

from datetime import date
from uuid import UUID

from hms_migration.modules.patients.entities.patient import Patient
from hms_migration.modules.vitals.entities.vital_reading import VitalReading

from hms_migration.modules.vitals.contracts.vitals_contracts import (
    VitalsTodayItem,
    serialize_vital_reading,
)
from hms_migration.modules.vitals.db.vitals_repository import VitalsRepository


class GetTodayVitalsAction:
    """Action that fetches today's bookings with vitals and manages waiting-reversion."""

    def __init__(self, repo: VitalsRepository) -> None:
        self.repo = repo

    def execute(self, on_date: date | None = None) -> list[VitalsTodayItem]:
        target_date = on_date or date.today()
        rows = self.repo.appointment_reader.get_today_appointments(target_date)
        if not rows:
            return []

        # Business rule: Revert 'waiting' visits back to 'scheduled' if no vitals exist
        waiting_appts = [a for a in rows if a.status.value == "waiting" or str(a.status) == "waiting"]
        if waiting_appts:
            with_vitals = self.repo.get_appointment_ids_with_vitals([a.id for a in waiting_appts])
            self.repo.appointment_reader.revert_orphaned_waiting_appointments(waiting_appts, with_vitals)

        appt_ids = [a.id for a in rows]
        vitals = self.repo.get_vitals_for_appointments(appt_ids)
        by_appt: dict[UUID, list[VitalReading]] = {}
        for v in vitals:
            by_appt.setdefault(v.appointment_id, []).append(v)

        out: list[VitalsTodayItem] = []
        for a in rows:
            patient: Patient | None = a.patient
            items = by_appt.get(a.id, [])
            out.append(
                VitalsTodayItem(
                    appointment_id=a.id,
                    patient_id=a.patient_id,
                    patient_name=patient.name if patient else "—",
                    patient_uhid=getattr(patient, "uhid", None) if patient else None,
                    patient_mobile=patient.mobile if patient else None,
                    doctor_id=a.doctor_id,
                    doctor_name=a.doctor.name if a.doctor else None,
                    appointment_date=a.appointment_date,
                    appointment_time=a.appointment_time,
                    purpose=a.purpose,
                    status=a.status.value if hasattr(a.status, "value") else str(a.status),
                    vitals_count=len(items),
                    vitals=[serialize_vital_reading(v) for v in items],
                )
            )
        return out
