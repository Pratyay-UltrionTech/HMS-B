"""
DMS domain database repository.

Handles data access queries for patients, patient documents, and medical records.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from hms_migration.modules.appointments.entities.appointment import Appointment
from hms_migration.modules.clinical_records.entities.clinical_record import (
    MedicalRecord,
    PatientDocument,
    PatientDocumentCategory,
    Prescription,
)
from hms_migration.modules.inpatient.entities.admission import Admission, AdmissionStatus
from hms_migration.modules.patients.entities.patient import Patient, PatientStatus


class DmsRepository:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    def get_patient(self, patient_id: UUID) -> Patient | None:
        return (
            self.db.query(Patient)
            .filter(Patient.id == patient_id, Patient.hospital_id == self.hospital_id)
            .first()
        )

    def list_patients(
        self,
        search: str | None = None,
        doctor_id: UUID | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> list[Patient]:
        q = self.db.query(Patient).filter(Patient.hospital_id == self.hospital_id)
        if search:
            like = f"%{search.strip()}%"
            q = q.filter(
                or_(
                    Patient.uhid.ilike(like),
                    Patient.name.ilike(like),
                    Patient.mobile.ilike(like),
                    Patient.first_name.ilike(like),
                    Patient.last_name.ilike(like),
                )
            )
        if doctor_id:
            q = q.filter(
                or_(
                    Patient.id.in_(
                        self.db.query(Appointment.patient_id).filter(
                            Appointment.hospital_id == self.hospital_id,
                            Appointment.doctor_id == doctor_id,
                        )
                    ),
                    Patient.id.in_(
                        self.db.query(Prescription.patient_id).filter(
                            Prescription.hospital_id == self.hospital_id,
                            Prescription.doctor_id == doctor_id,
                        )
                    ),
                )
            )
        if date_from:
            start = datetime.combine(date_from, time.min).replace(tzinfo=timezone.utc)
            q = q.filter(Patient.created_at >= start)
        if date_to:
            end = datetime.combine(date_to, time.max).replace(tzinfo=timezone.utc)
            q = q.filter(Patient.created_at <= end)

        return q.order_by(Patient.created_at.desc()).limit(500).all()

    def bulk_open_admission_patient_ids(self, patient_ids: list[UUID]) -> set[UUID]:
        if not patient_ids:
            return set()
        rows = (
            self.db.query(Admission.patient_id)
            .filter(
                Admission.hospital_id == self.hospital_id,
                Admission.patient_id.in_(patient_ids),
                Admission.status == AdmissionStatus.admitted,
            )
            .distinct()
            .all()
        )
        return {r[0] for r in rows}

    def bulk_last_visit_dates(self, patient_ids: list[UUID]) -> dict[UUID, date]:
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
                Appointment.hospital_id == self.hospital_id,
                Appointment.patient_id.in_(patient_ids),
            )
            .subquery()
        )
        rows = (
            self.db.query(ranked.c.patient_id, ranked.c.appointment_date)
            .filter(ranked.c.rn == 1)
            .all()
        )
        return {r[0]: r[1] for r in rows}

    def get_document(self, document_id: UUID) -> PatientDocument | None:
        return (
            self.db.query(PatientDocument)
            .filter(
                PatientDocument.id == document_id,
                PatientDocument.hospital_id == self.hospital_id,
            )
            .first()
        )

    def get_medical_record(self, record_id: UUID) -> MedicalRecord | None:
        return (
            self.db.query(MedicalRecord)
            .filter(
                MedicalRecord.id == record_id,
                MedicalRecord.hospital_id == self.hospital_id,
            )
            .first()
        )

    def create_document(
        self,
        *,
        patient_id: UUID,
        category: PatientDocumentCategory,
        title: str,
        notes: str | None,
        file_name: str | None,
        file_data: str | None,
        uploaded_by_name: str,
        uploaded_by_role: str,
    ) -> PatientDocument:
        doc = PatientDocument(
            hospital_id=self.hospital_id,
            patient_id=patient_id,
            category=category,
            title=title.strip(),
            notes=notes.strip() if notes else None,
            file_name=file_name,
            file_data=file_data,
            uploaded_by_name=uploaded_by_name,
            uploaded_by_role=uploaded_by_role,
        )
        self.db.add(doc)
        self.db.flush()
        self.db.commit()
        self.db.refresh(doc)
        return doc

    def delete_document(self, doc: PatientDocument) -> None:
        self.db.delete(doc)
        self.db.commit()
