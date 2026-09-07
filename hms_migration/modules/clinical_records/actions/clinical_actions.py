"""
Actions for Clinical Records, Prescriptions, and Document rendering.

Conforms to UltrionTech-Backend-Template modules/clinical_records/actions/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import String, column, func, select, table
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from hms_migration.modules.appointments.entities.appointment import Appointment
from hms_migration.modules.appointments.entities.enums import AppointmentStatus
from hms_migration.modules.clinical_records.contracts.clinical_contracts import (
    MedicalRecordCreate,
    MedicalRecordResponse,
    PrescriptionCreate,
    PrescriptionResponse,
    PrescriptionUpdate,
)
from hms_migration.modules.clinical_records.db.clinical_records_repository import ClinicalRecordsRepository
from hms_migration.modules.clinical_records.entities.clinical_record import (
    MedicalRecord,
    Prescription,
)
from hms_migration.modules.clinical_records.services.prescription_html_service import PrescriptionHtmlService
from hms_migration.modules.patients.entities.patient import Patient
from hms_migration.modules.vitals.entities.vital_reading import VitalReading
from hms_migration.shared.audit.service import write_audit_log


def to_prescription_response(
    p: Prescription, appt_status: AppointmentStatus | None = None
) -> PrescriptionResponse:
    return PrescriptionResponse(
        id=p.id,
        hospital_id=p.hospital_id,
        doctor_id=p.doctor_id,
        patient_id=p.patient_id,
        appointment_id=p.appointment_id,
        symptoms=p.symptoms,
        diagnosis=p.diagnosis,
        medicines=p.medicines,
        dosage=p.dosage,
        advice=p.advice,
        follow_up_date=p.follow_up_date,
        signature_data=p.signature_data,
        has_signature=bool(p.signature_data),
        created_at=p.created_at,
        patient_name=p.patient.name if p.patient else None,
        patient_mobile=p.patient.mobile if p.patient else None,
        doctor_name=p.doctor.name if p.doctor else None,
        appointment_status=appt_status,
    )


def to_record_response(r: MedicalRecord) -> MedicalRecordResponse:
    return MedicalRecordResponse(
        id=r.id,
        hospital_id=r.hospital_id,
        doctor_id=r.doctor_id,
        patient_id=r.patient_id,
        appointment_id=r.appointment_id,
        lab_order_id=r.lab_order_id,
        radiology_order_id=r.radiology_order_id,
        report_type=r.report_type,
        title=r.title,
        notes=r.notes,
        file_name=r.file_name,
        has_file=bool(r.file_data),
        created_at=r.created_at,
        patient_name=r.patient.name if r.patient else None,
        doctor_name=r.doctor.name if r.doctor else None,
    )


class CreatePrescriptionAction:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = ClinicalRecordsRepository(db)

    def execute(
        self,
        hospital_id: UUID,
        doctor_id: UUID,
        payload: PrescriptionCreate,
        actor: dict[str, Any],
    ) -> PrescriptionResponse:
        patient = (
            self.db.query(Patient)
            .filter(Patient.id == payload.patient_id, Patient.hospital_id == hospital_id)
            .first()
        )
        if not patient:
            raise HTTPException(status_code=404, detail="Patient not found")

        appt_status: AppointmentStatus | None = None
        if payload.appointment_id:
            appt = (
                self.db.query(Appointment)
                .filter(
                    Appointment.id == payload.appointment_id,
                    Appointment.hospital_id == hospital_id,
                )
                .first()
            )
            if appt:
                appt_status = appt.status
                if appt.status in (AppointmentStatus.scheduled, AppointmentStatus.waiting):
                    appt.status = AppointmentStatus.in_progress

        rx = Prescription(
            hospital_id=hospital_id,
            doctor_id=doctor_id,
            patient_id=payload.patient_id,
            appointment_id=payload.appointment_id,
            symptoms=payload.symptoms.strip(),
            diagnosis=payload.diagnosis.strip(),
            medicines=payload.medicines.strip(),
            dosage=payload.dosage.strip(),
            advice=payload.advice.strip() if payload.advice else None,
            follow_up_date=payload.follow_up_date,
            signature_data=payload.signature_data,
        )
        self.db.add(rx)
        self.db.flush()

        # Link lab / radiology orders if requested via legacy helper
        if payload.test_ids or payload.panel_ids or payload.scan_ids:
            try:
                from hms_migration.modules.laboratory.services.lab_prescription_service import (
                    create_investigation_requests_for_prescription,
                )
                create_investigation_requests_for_prescription(
                    self.db,
                    hospital_id=hospital_id,
                    doctor_id=doctor_id,
                    patient_id=payload.patient_id,
                    appointment_id=payload.appointment_id,
                    prescription_id=rx.id,
                    test_ids=payload.test_ids,
                    panel_ids=payload.panel_ids,
                    scan_ids=payload.scan_ids,
                    clinical_notes=f"Ordered with prescription · {rx.diagnosis[:120]}",
                )
            except Exception:
                pass

        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="create",
            entity_type="prescription",
            entity_id=str(rx.id),
            summary=f"Created prescription for {patient.name} ({rx.diagnosis})",
        )
        self.db.commit()
        refreshed = self.repo.get_prescription(hospital_id, doctor_id, rx.id)
        return to_prescription_response(refreshed or rx, appt_status)


class UpdatePrescriptionAction:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = ClinicalRecordsRepository(db)

    def execute(
        self,
        hospital_id: UUID,
        doctor_id: UUID,
        prescription_id: UUID,
        payload: PrescriptionUpdate,
        actor: dict[str, Any],
    ) -> PrescriptionResponse:
        rx = self.repo.get_prescription(hospital_id, doctor_id, prescription_id)
        if not rx:
            raise HTTPException(status_code=404, detail="Prescription not found")

        appt_status: AppointmentStatus | None = None
        target_appt_id = payload.appointment_id or rx.appointment_id
        if target_appt_id:
            appt = (
                self.db.query(Appointment)
                .filter(Appointment.id == target_appt_id, Appointment.hospital_id == hospital_id)
                .first()
            )
            if appt:
                appt_status = appt.status
                if appt.status in (
                    AppointmentStatus.completed,
                    AppointmentStatus.transferred_to_inpatient,
                ):
                    raise HTTPException(
                        status_code=400,
                        detail="Cannot edit a prescription after the visit is marked completed or transferred to inpatient",
                    )

        if payload.symptoms is not None:
            rx.symptoms = payload.symptoms.strip()
        if payload.diagnosis is not None:
            rx.diagnosis = payload.diagnosis.strip()
        if payload.medicines is not None:
            rx.medicines = payload.medicines.strip()
        if payload.dosage is not None:
            rx.dosage = payload.dosage.strip()
        if payload.advice is not None:
            rx.advice = payload.advice.strip() if payload.advice else None
        if payload.follow_up_date is not None:
            rx.follow_up_date = payload.follow_up_date
        if payload.signature_data is not None:
            rx.signature_data = payload.signature_data

        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="update",
            entity_type="prescription",
            entity_id=str(rx.id),
            summary=f"Updated prescription {rx.id} for {rx.patient.name if rx.patient else 'patient'}",
        )
        self.db.commit()
        refreshed = self.repo.get_prescription(hospital_id, doctor_id, rx.id)
        return to_prescription_response(refreshed or rx, appt_status)


class ListPrescriptionsAction:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = ClinicalRecordsRepository(db)

    def execute(
        self,
        hospital_id: UUID,
        doctor_id: UUID,
        patient_id: UUID | None = None,
    ) -> list[PrescriptionResponse]:
        rows = self.repo.list_prescriptions(hospital_id, doctor_id, patient_id)
        appt_ids = [p.appointment_id for p in rows if p.appointment_id]
        status_map: dict[UUID, AppointmentStatus] = {}
        if appt_ids:
            appts = (
                self.db.query(Appointment.id, Appointment.status)
                .filter(Appointment.hospital_id == hospital_id, Appointment.id.in_(appt_ids))
                .all()
            )
            status_map = {row[0]: row[1] for row in appts}
        return [
            to_prescription_response(p, status_map.get(p.appointment_id) if p.appointment_id else None)
            for p in rows
        ]


class StreamPrescriptionPdfAction:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = ClinicalRecordsRepository(db)

    def execute(
        self,
        hospital_id: UUID,
        doctor_id: UUID,
        prescription_id: UUID,
    ) -> StreamingResponse:
        rx = self.repo.get_prescription(hospital_id, doctor_id, prescription_id)
        if not rx:
            raise HTTPException(status_code=404, detail="Prescription not found")

        # Hospital info
        _hospitals = table(
            "hospitals",
            column("id", PG_UUID(as_uuid=True)),
            column("name", String),
            column("address", String),
            column("phone", String),
            column("email", String),
        )
        hosp_row = self.db.execute(
            select(_hospitals.c.name, _hospitals.c.address, _hospitals.c.phone, _hospitals.c.email)
            .where(_hospitals.c.id == hospital_id)
        ).first()
        hospital_info = {
            "name": hosp_row[0] if hosp_row else "Hospital",
            "address": hosp_row[1] if hosp_row else "—",
            "phone": hosp_row[2] if hosp_row else "—",
            "email": hosp_row[3] if hosp_row else "—",
        }

        # Vitals
        vitals_q = (
            self.db.query(VitalReading)
            .filter(VitalReading.hospital_id == hospital_id, VitalReading.patient_id == rx.patient_id)
            .order_by(VitalReading.created_at.asc())
        )
        if rx.appointment_id:
            vitals_q = vitals_q.filter(VitalReading.appointment_id == rx.appointment_id)
        elif rx.created_at:
            vitals_q = vitals_q.filter(func.date(VitalReading.recorded_at) == rx.created_at.date())
        else:
            vitals_q = vitals_q.filter(False)
        vital_rows = vitals_q.all()

        # Investigations
        lab_names, rad_names = [], []
        try:
            from hms_migration.modules.laboratory.services.lab_prescription_service import (
                prescription_investigation_names,
            )
            lab_names, rad_names = prescription_investigation_names(self.db, rx)
        except Exception:
            pass

        html = PrescriptionHtmlService.render(
            rx,
            hospital_info=hospital_info,
            lab_names=lab_names,
            rad_names=rad_names,
            vitals=[(v.name, v.result) for v in vital_rows],
        )
        return StreamingResponse(
            BytesIO(html.encode("utf-8")),
            media_type="text/html",
            headers={"Content-Disposition": f'inline; filename="prescription-{prescription_id}.html"'},
        )


class CreateMedicalRecordAction:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = ClinicalRecordsRepository(db)

    def execute(
        self,
        hospital_id: UUID,
        doctor_id: UUID,
        payload: MedicalRecordCreate,
        actor: dict[str, Any],
    ) -> MedicalRecordResponse:
        patient = (
            self.db.query(Patient)
            .filter(Patient.id == payload.patient_id, Patient.hospital_id == hospital_id)
            .first()
        )
        if not patient:
            raise HTTPException(status_code=404, detail="Patient not found")

        file_data = payload.file_data
        if file_data and len(file_data) > 2_500_000:
            raise HTTPException(status_code=400, detail="File too large (max ~1.5MB)")

        record = MedicalRecord(
            hospital_id=hospital_id,
            doctor_id=doctor_id,
            patient_id=payload.patient_id,
            appointment_id=payload.appointment_id,
            report_type=payload.report_type.strip(),
            title=payload.title.strip(),
            notes=payload.notes.strip() if payload.notes else None,
            file_name=payload.file_name,
            file_data=file_data,
        )
        self.db.add(record)
        self.db.flush()

        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="create",
            entity_type="medical_record",
            entity_id=str(record.id),
            summary=f"Added {payload.report_type} for {patient.name}",
        )
        self.db.commit()
        refreshed = self.repo.get_medical_record(hospital_id, doctor_id, record.id)
        return to_record_response(refreshed or record)


class ListMedicalRecordsAction:
    def __init__(self, db: Session) -> None:
        self.repo = ClinicalRecordsRepository(db)

    def execute(
        self,
        hospital_id: UUID,
        doctor_id: UUID,
        patient_id: UUID | None = None,
    ) -> list[MedicalRecordResponse]:
        rows = self.repo.list_medical_records(hospital_id, doctor_id, patient_id)
        return [to_record_response(r) for r in rows]


class GetRecordFileAction:
    def __init__(self, db: Session) -> None:
        self.repo = ClinicalRecordsRepository(db)

    def execute(
        self,
        hospital_id: UUID,
        doctor_id: UUID,
        record_id: UUID,
    ) -> dict[str, Any]:
        rec = self.repo.get_medical_record(hospital_id, doctor_id, record_id)
        if not rec or not rec.file_data:
            raise HTTPException(status_code=404, detail="File not found")
        return {"file_name": rec.file_name, "file_data": rec.file_data}
