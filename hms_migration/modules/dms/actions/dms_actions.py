"""
DMS domain use case actions (Command / Query handlers).

Strictly adheres to UltrionTech-Backend-Template action conventions:
- Orchestrates business workflows across DMS document repositories.
- Employs target repository and audit logging.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from hms_migration.modules.clinical_records.entities.clinical_record import PatientDocumentCategory
from hms_migration.modules.dms.contracts.dms_contracts import (
    DmsDocumentCreate,
    DmsDocumentResponse,
    DmsPatientFile,
    DmsPatientItem,
    DmsTimelineEvent,
)
from hms_migration.modules.dms.db.dms_repository import DmsRepository
from hms_migration.modules.dms.services.dms_service import (
    build_documents,
    build_patient_file,
    build_timeline,
    complete_file_html,
    stream_data_url,
)
from hms_migration.modules.patients.entities.patient import Patient, PatientStatus
from hms_migration.modules.tenancy.entities.hospital import Hospital


def _actor_name(user: dict) -> str:
    return str(user.get("name") or user.get("sub") or "Staff")


def _actor_role(user: dict) -> str:
    return str(user.get("staff_role_name") or user.get("role") or "")


def _care_status(patient: Patient, has_open_admission: bool) -> str:
    if patient.status == PatientStatus.inactive:
        return "Inactive"
    if patient.status == PatientStatus.admitted or has_open_admission:
        return "IPD"
    return "OPD"


def _patient_item_from_maps(
    patient: Patient,
    open_admission_ids: set[UUID],
    last_visits: dict[UUID, date],
) -> DmsPatientItem:
    return DmsPatientItem(
        id=patient.id,
        uhid=patient.uhid,
        name=patient.name,
        mobile=patient.mobile,
        email=patient.email,
        gender=patient.gender,
        age=patient.age,
        care_status=_care_status(patient, patient.id in open_admission_ids),
        status=patient.status.value if hasattr(patient.status, "value") else str(patient.status),
        last_visit=last_visits.get(patient.id),
        created_at=patient.created_at,
        emergency_contact=getattr(patient, "emergency_contact", None),
        emergency_contact_name=getattr(patient, "emergency_contact_name", None),
        emergency_contact_relation=getattr(patient, "emergency_contact_relation", None),
        has_insurance=bool(getattr(patient, "has_insurance", False)),
        insurance_provider=getattr(patient, "insurance_provider", None),
    )


class ListDmsPatientsAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = DmsRepository(db, hospital_id)

    def execute(
        self,
        search: str | None = None,
        care_status: str | None = None,
        doctor_id: UUID | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> list[DmsPatientItem]:
        patients = self.repo.list_patients(
            search=search,
            doctor_id=doctor_id,
            date_from=date_from,
            date_to=date_to,
        )
        if not patients:
            return []
        ids = [p.id for p in patients]
        open_ids = self.repo.bulk_open_admission_patient_ids(ids)
        last_visits = self.repo.bulk_last_visit_dates(ids)

        items = [_patient_item_from_maps(p, open_ids, last_visits) for p in patients]
        if care_status:
            key = care_status.strip().upper()
            items = [i for i in items if i.care_status.upper() == key]
        return items


class GetDmsPatientFileAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = DmsRepository(db, hospital_id)

    def execute(self, patient_id: UUID) -> DmsPatientFile:
        patient = self.repo.get_patient(patient_id)
        if not patient:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
        open_ids = self.repo.bulk_open_admission_patient_ids([patient.id])
        last_visits = self.repo.bulk_last_visit_dates([patient.id])
        patient_item = _patient_item_from_maps(patient, open_ids, last_visits)
        return build_patient_file(self.db, patient_item, patient, self.hospital_id)


class GetDmsTimelineAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = DmsRepository(db, hospital_id)

    def execute(self, patient_id: UUID) -> list[DmsTimelineEvent]:
        patient = self.repo.get_patient(patient_id)
        if not patient:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
        return build_timeline(self.db, patient, self.hospital_id)


class ListDmsDocumentsAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = DmsRepository(db, hospital_id)

    def execute(self, patient_id: UUID) -> list[DmsDocumentResponse]:
        patient = self.repo.get_patient(patient_id)
        if not patient:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
        return build_documents(self.db, patient_id, self.hospital_id)


class UploadDmsDocumentAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = DmsRepository(db, hospital_id)

    def execute(self, patient_id: UUID, payload: DmsDocumentCreate, user: dict) -> DmsDocumentResponse:
        patient = self.repo.get_patient(patient_id)
        if not patient:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

        doc = self.repo.create_document(
            patient_id=patient_id,
            category=payload.category,
            title=payload.title,
            notes=payload.notes,
            file_name=payload.file_name,
            file_data=payload.file_data,
            uploaded_by_name=_actor_name(user),
            uploaded_by_role=_actor_role(user),
        )
        return DmsDocumentResponse(
            id=doc.id,
            patient_id=doc.patient_id,
            category=doc.category,
            title=doc.title,
            notes=doc.notes,
            file_name=doc.file_name,
            has_file=bool(doc.file_data),
            uploaded_by_name=doc.uploaded_by_name,
            uploaded_by_role=doc.uploaded_by_role,
            created_at=doc.created_at,
            source="dms",
        )


class DeleteDmsDocumentAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = DmsRepository(db, hospital_id)

    def execute(self, document_id: UUID, user: dict) -> None:
        doc = self.repo.get_document(document_id)
        if not doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
        self.repo.delete_document(doc)


class DownloadDmsFileAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = DmsRepository(db, hospital_id)

    def execute(self, document_id: UUID) -> StreamingResponse:
        doc = self.repo.get_document(document_id)
        if not doc or not doc.file_data:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
        return stream_data_url(doc.file_data, doc.file_name or "document")


class DownloadMedicalRecordFileAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = DmsRepository(db, hospital_id)

    def execute(self, record_id: UUID) -> StreamingResponse:
        rec = self.repo.get_medical_record(record_id)
        if not rec or not rec.file_data:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
        return stream_data_url(rec.file_data, rec.file_name or "medical-record")


class GetCompletePatientFileHtmlAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = DmsRepository(db, hospital_id)

    def execute(self, patient_id: UUID) -> tuple[str, str]:
        patient = self.repo.get_patient(patient_id)
        if not patient:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
        open_ids = self.repo.bulk_open_admission_patient_ids([patient.id])
        last_visits = self.repo.bulk_last_visit_dates([patient.id])
        patient_item = _patient_item_from_maps(patient, open_ids, last_visits)
        file_obj = build_patient_file(self.db, patient_item, patient, self.hospital_id)

        hospital = self.db.query(Hospital).filter(Hospital.id == self.hospital_id).first()
        hospital_name = hospital.name if hospital else "Hospital"
        html = complete_file_html(file_obj, hospital_name)
        filename = f"{patient.uhid}-medical-file.html"
        return html, filename
