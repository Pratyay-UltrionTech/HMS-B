"""
Database repository for IPD Form Submissions and clinical document synchronization.

Conforms to UltrionTech-Backend-Template modules/inpatient/db/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

import base64
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, joinedload

from hms_migration.modules.clinical_records.entities.clinical_record import (
    MedicalRecord,
    PatientDocument,
    PatientDocumentCategory,
)
from hms_migration.modules.inpatient.entities.admission import (
    IpdFormSubmission,
    IpdFormSubmissionStatus,
)


def html_to_data_url(html: str) -> str:
    """Encode HTML document into base64 data URI."""
    b64 = base64.b64encode(html.encode("utf-8")).decode("ascii")
    return f"data:text/html;base64,{b64}"


def doc_category_for_title(title: str) -> PatientDocumentCategory:
    """Classify patient document based on form title."""
    t = title.lower()
    if "consent" in t:
        return PatientDocumentCategory.consent
    if "discharge" in t:
        return PatientDocumentCategory.discharge_summary
    if "insurance" in t:
        return PatientDocumentCategory.insurance
    return PatientDocumentCategory.other


class IpdSubmissionsRepository:
    """Repository handling IPD form submissions and automatic sync to DMS / Medical Records."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get_by_id(self, hospital_id: UUID, submission_id: UUID) -> IpdFormSubmission | None:
        """Fetch form submission by id."""
        return (
            self.db.query(IpdFormSubmission)
            .options(joinedload(IpdFormSubmission.patient))
            .filter(
                IpdFormSubmission.id == submission_id,
                IpdFormSubmission.hospital_id == hospital_id,
            )
            .first()
        )

    def list_submissions(
        self,
        hospital_id: UUID,
        patient_id: UUID | None = None,
        admission_id: UUID | None = None,
        form_id: str | None = None,
        status_filter: IpdFormSubmissionStatus | None = None,
        limit: int = 200,
    ) -> list[IpdFormSubmission]:
        """List IPD form submissions with filtering."""
        q = (
            self.db.query(IpdFormSubmission)
            .options(joinedload(IpdFormSubmission.patient))
            .filter(IpdFormSubmission.hospital_id == hospital_id)
        )
        if patient_id:
            q = q.filter(IpdFormSubmission.patient_id == patient_id)
        if admission_id:
            q = q.filter(IpdFormSubmission.admission_id == admission_id)
        if form_id:
            q = q.filter(IpdFormSubmission.form_id == form_id.strip())
        if status_filter:
            q = q.filter(IpdFormSubmission.status == status_filter)
        return q.order_by(IpdFormSubmission.updated_at.desc()).limit(limit).all()

    def sync_final_to_patient_record(
        self, sub: IpdFormSubmission, user_id: UUID | None = None
    ) -> None:
        """Mirror a finalized IPD form into DMS patient documents (+ medical record when doctor)."""
        notes = f"IPD form `{sub.form_id}` · status {sub.status.value} · submission {sub.id}"
        file_data = html_to_data_url(sub.html_snapshot) if sub.html_snapshot else None
        file_name = f"{sub.form_id}-{sub.id}.html" if sub.html_snapshot else None
        category = doc_category_for_title(sub.form_title)

        if sub.patient_document_id:
            doc = (
                self.db.query(PatientDocument)
                .filter(PatientDocument.id == sub.patient_document_id)
                .first()
            )
            if doc:
                doc.title = sub.form_title
                doc.notes = notes
                doc.category = category
                doc.file_name = file_name
                doc.file_data = file_data
                doc.uploaded_by_name = sub.filled_by_name
                doc.uploaded_by_role = sub.filled_by_role
            else:
                sub.patient_document_id = None

        if not sub.patient_document_id:
            doc = PatientDocument(
                hospital_id=sub.hospital_id,
                patient_id=sub.patient_id,
                category=category,
                title=sub.form_title,
                notes=notes,
                file_name=file_name,
                file_data=file_data,
                uploaded_by_name=sub.filled_by_name,
                uploaded_by_role=sub.filled_by_role,
            )
            self.db.add(doc)
            self.db.flush()
            sub.patient_document_id = doc.id

        doctor_id = sub.filled_by_id or user_id
        if doctor_id and not sub.medical_record_id:
            rec = MedicalRecord(
                hospital_id=sub.hospital_id,
                doctor_id=doctor_id,
                patient_id=sub.patient_id,
                report_type="IPD Form",
                title=sub.form_title,
                notes=notes,
                file_name=file_name,
                file_data=file_data,
            )
            self.db.add(rec)
            self.db.flush()
            sub.medical_record_id = rec.id
        elif sub.medical_record_id:
            rec = (
                self.db.query(MedicalRecord)
                .filter(MedicalRecord.id == sub.medical_record_id)
                .first()
            )
            if rec:
                rec.title = sub.form_title
                rec.notes = notes
                rec.file_name = file_name
                rec.file_data = file_data
