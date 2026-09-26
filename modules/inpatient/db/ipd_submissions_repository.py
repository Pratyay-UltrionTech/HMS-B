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

from modules.clinical_records.entities.clinical_record import (
    MedicalRecord,
    PatientDocument,
    PatientDocumentCategory,
)
from modules.inpatient.entities.admission import (
    IpdFormSubmission,
    IpdFormSubmissionStatus,
)
from modules.inpatient.entities.form_addendum import IpdFormAddendum
from sqlalchemy import func


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

    def addenda_counts(self, hospital_id: UUID, submission_ids: list[UUID]) -> dict[UUID, int]:
        if not submission_ids:
            return {}
        rows = (
            self.db.query(IpdFormAddendum.submission_id, func.count(IpdFormAddendum.id))
            .filter(
                IpdFormAddendum.hospital_id == hospital_id,
                IpdFormAddendum.submission_id.in_(submission_ids),
            )
            .group_by(IpdFormAddendum.submission_id)
            .all()
        )
        return {submission_id: count for submission_id, count in rows}

    def list_submissions(
        self,
        hospital_id: UUID,
        patient_id: UUID | None = None,
        admission_id: UUID | None = None,
        form_id: str | None = None,
        status_filter: IpdFormSubmissionStatus | None = None,
        limit: int = 200,
        offset: int = 0,
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
        return q.order_by(IpdFormSubmission.updated_at.desc(), IpdFormSubmission.id.desc()).offset(offset).limit(limit).all()

    def sync_final_to_patient_record(
        self, sub: IpdFormSubmission, user_id: UUID | None = None
    ) -> None:
        """
        Update historical mirrored patient document / medical record if one was already linked.
        Per audit requirement 19, do NOT manufacture new physical duplicate rows; the canonical
        IpdFormSubmission is surfaced directly via the unified medical-record layer.
        """
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

        if sub.medical_record_id:
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

    def create_addendum(
        self,
        *,
        hospital_id: UUID,
        submission_id: UUID,
        admission_id: UUID | None,
        patient_id: UUID,
        author_id: UUID,
        author_name: str,
        author_role: str,
        reason: str,
        content: str,
    ) -> IpdFormAddendum:
        addendum = IpdFormAddendum(
            hospital_id=hospital_id,
            submission_id=submission_id,
            admission_id=admission_id,
            patient_id=patient_id,
            author_id=author_id,
            author_name=author_name,
            author_role=author_role,
            reason=reason,
            content=content,
        )
        self.db.add(addendum)
        self.db.flush()
        return addendum

    def list_addenda(self, hospital_id: UUID, submission_id: UUID) -> list[IpdFormAddendum]:
        return (
            self.db.query(IpdFormAddendum)
            .filter(
                IpdFormAddendum.hospital_id == hospital_id,
                IpdFormAddendum.submission_id == submission_id,
            )
            .order_by(IpdFormAddendum.created_at.asc())
            .all()
        )

