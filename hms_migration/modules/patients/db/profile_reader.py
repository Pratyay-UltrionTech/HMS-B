"""
Cross-domain profile reader adapter for the Patient domain.

Conforms to UltrionTech-Backend-Template domain boundary guidelines.
Isolates cross-domain queries (visits, prescriptions, reports, admissions, ledger)
from core PatientRepository while preserving exact database queries, eager loading,
and sorting behavior of OG HMS-B.
"""

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from hms_migration.modules.appointments.entities.appointment import (
    Appointment,
    AppointmentStatus,
)
from hms_migration.modules.clinical_records.entities.clinical_record import (
    MedicalRecord,
    Prescription,
)
from hms_migration.modules.inpatient.entities.admission import Admission
from hms_migration.modules.billing.services.billing_service import (
    build_ledger_entries,
    patient_ledger_totals,
)


class PatientProfileReader:
    """Read adapter for cross-domain clinical and financial records related to a patient."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    def get_last_visit(self, patient_id: UUID) -> date | None:
        """Fetch the most recent non-cancelled appointment date for a patient."""
        row = (
            self.db.query(Appointment.appointment_date)
            .filter(
                Appointment.patient_id == patient_id,
                Appointment.status != AppointmentStatus.cancelled,
            )
            .order_by(Appointment.appointment_date.desc())
            .first()
        )
        return row[0] if row else None

    def get_bulk_last_visits(self, patient_ids: list[UUID]) -> dict[UUID, date]:
        """Fetch latest non-cancelled appointment_date per patient across a list of IDs."""
        if not patient_ids:
            return {}
        ranked = (
            self.db.query(
                Appointment.patient_id.label("patient_id"),
                Appointment.appointment_date.label("appointment_date"),
                func.row_number()
                .over(
                    partition_by=Appointment.patient_id,
                    order_by=Appointment.appointment_date.desc(),
                )
                .label("rn"),
            )
            .filter(
                Appointment.patient_id.in_(patient_ids),
                Appointment.status != AppointmentStatus.cancelled,
            )
            .subquery()
        )
        rows = (
            self.db.query(ranked.c.patient_id, ranked.c.appointment_date)
            .filter(ranked.c.rn == 1)
            .all()
        )
        return {r[0]: r[1] for r in rows}

    def get_visits(self, patient_id: UUID) -> list[Appointment]:
        """Fetch all visits for a patient with doctor details, descending by date and time."""
        return (
            self.db.query(Appointment)
            .options(joinedload(Appointment.doctor))
            .filter(
                Appointment.patient_id == patient_id,
                Appointment.hospital_id == self.hospital_id,
            )
            .order_by(
                Appointment.appointment_date.desc(),
                Appointment.appointment_time.desc(),
            )
            .all()
        )

    def get_prescriptions(self, patient_id: UUID) -> list[Prescription]:
        """Fetch all prescriptions for a patient with doctor details, descending by created_at."""
        return (
            self.db.query(Prescription)
            .options(joinedload(Prescription.doctor))
            .filter(
                Prescription.patient_id == patient_id,
                Prescription.hospital_id == self.hospital_id,
            )
            .order_by(Prescription.created_at.desc())
            .all()
        )

    def get_medical_reports(self, patient_id: UUID) -> list[MedicalRecord]:
        """Fetch all medical records for a patient with doctor details, descending by created_at."""
        return (
            self.db.query(MedicalRecord)
            .options(joinedload(MedicalRecord.doctor))
            .filter(
                MedicalRecord.patient_id == patient_id,
                MedicalRecord.hospital_id == self.hospital_id,
            )
            .order_by(MedicalRecord.created_at.desc())
            .all()
        )

    def get_admissions(self, patient_id: UUID) -> list[Admission]:
        """Fetch all admissions for a patient with ward/room/bed/doctor loaded, descending by admitted_at."""
        return (
            self.db.query(Admission)
            .options(
                joinedload(Admission.ward),
                joinedload(Admission.room),
                joinedload(Admission.bed),
                joinedload(Admission.doctor),
            )
            .filter(
                Admission.patient_id == patient_id,
                Admission.hospital_id == self.hospital_id,
            )
            .order_by(Admission.admitted_at.desc())
            .all()
        )

    def get_ledger_bills(self, patient_id: UUID, limit: int = 50) -> list[dict[str, Any]]:
        """Fetch billing ledger entries formatted for patient profile response."""
        entries = build_ledger_entries(self.db, self.hospital_id, patient_id)
        return [
            {
                "id": str(e["ref_id"]),
                "type": e["entry_type"],
                "description": e["description"],
                "debit": e["debit"],
                "credit": e["credit"],
                "status": e["status"],
                "occurred_at": e["occurred_at"].isoformat() if e.get("occurred_at") else None,
            }
            for e in entries[:limit]
        ]

    def get_financial_summary(self, patient_id: UUID) -> dict[str, Any] | None:
        """Fetch patient ledger summary totals."""
        return patient_ledger_totals(self.db, self.hospital_id, patient_id)
