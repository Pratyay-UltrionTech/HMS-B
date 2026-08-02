import base64
from datetime import datetime, timezone
from io import BytesIO
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import (
    Admission,
    IpdFormSubmission,
    IpdFormSubmissionStatus,
    MedicalRecord,
    Patient,
    PatientDocument,
    PatientDocumentCategory,
)
from app.schemas_ipd import (
    IpdFormSubmissionCreate,
    IpdFormSubmissionResponse,
    IpdFormSubmissionUpdate,
)
from app.utils.audit import write_audit
from app.utils.auth import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/ipd", tags=["ipd"])


def _actor_name(user: dict) -> str:
    return str(user.get("name") or user.get("sub") or "Staff")


def _actor_role(user: dict) -> str:
    return str(user.get("staff_role_name") or user.get("role") or "")


def _actor_user_id(user: dict) -> UUID | None:
    raw = user.get("user_id")
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, TypeError):
        return None


def _get_patient(db: Session, patient_id: UUID, hospital_id: UUID) -> Patient:
    patient = (
        db.query(Patient)
        .filter(Patient.id == patient_id, Patient.hospital_id == hospital_id)
        .first()
    )
    if not patient:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
    return patient


def _validate_admission(
    db: Session, admission_id: UUID | None, patient_id: UUID, hospital_id: UUID
) -> None:
    if admission_id is None:
        return
    adm = (
        db.query(Admission)
        .filter(
            Admission.id == admission_id,
            Admission.hospital_id == hospital_id,
            Admission.patient_id == patient_id,
        )
        .first()
    )
    if not adm:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid admission for patient")


def _html_to_data_url(html: str) -> str:
    b64 = base64.b64encode(html.encode("utf-8")).decode("ascii")
    return f"data:text/html;base64,{b64}"


def _doc_category_for_title(title: str) -> PatientDocumentCategory:
    t = title.lower()
    if "consent" in t:
        return PatientDocumentCategory.consent
    if "discharge" in t:
        return PatientDocumentCategory.discharge_summary
    if "insurance" in t:
        return PatientDocumentCategory.insurance
    return PatientDocumentCategory.other


def _sync_final_to_patient_record(db: Session, sub: IpdFormSubmission, user: dict) -> None:
    """Mirror a finalized IPD form into DMS patient documents (+ medical record when doctor)."""
    notes = f"IPD form `{sub.form_id}` · status {sub.status.value} · submission {sub.id}"
    file_data = _html_to_data_url(sub.html_snapshot) if sub.html_snapshot else None
    file_name = f"{sub.form_id}-{sub.id}.html" if sub.html_snapshot else None
    category = _doc_category_for_title(sub.form_title)

    if sub.patient_document_id:
        doc = db.query(PatientDocument).filter(PatientDocument.id == sub.patient_document_id).first()
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
        db.add(doc)
        db.flush()
        sub.patient_document_id = doc.id

    doctor_id = sub.filled_by_id or _actor_user_id(user)
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
        db.add(rec)
        db.flush()
        sub.medical_record_id = rec.id
    elif sub.medical_record_id:
        rec = db.query(MedicalRecord).filter(MedicalRecord.id == sub.medical_record_id).first()
        if rec:
            rec.title = sub.form_title
            rec.notes = notes
            rec.file_name = file_name
            rec.file_data = file_data


def _to_response(sub: IpdFormSubmission) -> IpdFormSubmissionResponse:
    return IpdFormSubmissionResponse(
        id=sub.id,
        hospital_id=sub.hospital_id,
        patient_id=sub.patient_id,
        admission_id=sub.admission_id,
        form_id=sub.form_id,
        form_title=sub.form_title,
        form_data=sub.form_data or {},
        status=sub.status,
        filled_by_id=sub.filled_by_id,
        filled_by_name=sub.filled_by_name,
        filled_by_role=sub.filled_by_role,
        medical_record_id=sub.medical_record_id,
        patient_document_id=sub.patient_document_id,
        has_html_snapshot=bool(sub.html_snapshot),
        created_at=sub.created_at,
        updated_at=sub.updated_at,
        patient_name=sub.patient.name if sub.patient else None,
        patient_uhid=sub.patient.uhid if sub.patient else None,
    )


@router.post(
    "/form-submissions",
    response_model=IpdFormSubmissionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_form_submission(
    payload: IpdFormSubmissionCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    _get_patient(db, payload.patient_id, hospital_id)
    _validate_admission(db, payload.admission_id, payload.patient_id, hospital_id)

    sub = IpdFormSubmission(
        hospital_id=hospital_id,
        patient_id=payload.patient_id,
        admission_id=payload.admission_id,
        form_id=payload.form_id.strip(),
        form_title=payload.form_title.strip(),
        form_data=payload.form_data or {},
        html_snapshot=payload.html_snapshot,
        status=payload.status,
        filled_by_id=_actor_user_id(user),
        filled_by_name=_actor_name(user),
        filled_by_role=_actor_role(user),
    )
    db.add(sub)
    db.flush()

    if sub.status == IpdFormSubmissionStatus.final:
        _sync_final_to_patient_record(db, sub, user)

    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="ipd_form_submission",
        entity_id=sub.id,
        summary=f"Saved IPD form '{sub.form_title}' ({sub.status.value}) for patient",
        details={"form_id": sub.form_id, "patient_id": str(sub.patient_id)},
    )
    db.commit()
    sub = (
        db.query(IpdFormSubmission)
        .options(joinedload(IpdFormSubmission.patient))
        .filter(IpdFormSubmission.id == sub.id)
        .first()
    )
    return _to_response(sub)


@router.put("/form-submissions/{submission_id}", response_model=IpdFormSubmissionResponse)
def update_form_submission(
    submission_id: UUID,
    payload: IpdFormSubmissionUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    sub = (
        db.query(IpdFormSubmission)
        .options(joinedload(IpdFormSubmission.patient))
        .filter(IpdFormSubmission.id == submission_id, IpdFormSubmission.hospital_id == hospital_id)
        .first()
    )
    if not sub:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")

    if payload.admission_id is not None:
        _validate_admission(db, payload.admission_id, sub.patient_id, hospital_id)
        sub.admission_id = payload.admission_id
    if payload.form_data is not None:
        sub.form_data = payload.form_data
    if payload.html_snapshot is not None:
        sub.html_snapshot = payload.html_snapshot
    if payload.form_title is not None:
        sub.form_title = payload.form_title.strip()
    if payload.status is not None:
        sub.status = payload.status

    sub.filled_by_id = _actor_user_id(user) or sub.filled_by_id
    sub.filled_by_name = _actor_name(user)
    sub.filled_by_role = _actor_role(user)
    sub.updated_at = datetime.now(timezone.utc)

    if sub.status == IpdFormSubmissionStatus.final:
        _sync_final_to_patient_record(db, sub, user)

    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="ipd_form_submission",
        entity_id=sub.id,
        summary=f"Updated IPD form '{sub.form_title}' ({sub.status.value})",
        details={"form_id": sub.form_id, "patient_id": str(sub.patient_id)},
    )
    db.commit()
    db.refresh(sub)
    return _to_response(sub)


@router.get("/form-submissions", response_model=list[IpdFormSubmissionResponse])
def list_form_submissions(
    patient_id: UUID | None = Query(default=None),
    admission_id: UUID | None = Query(default=None),
    form_id: str | None = Query(default=None),
    status_filter: IpdFormSubmissionStatus | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = (
        db.query(IpdFormSubmission)
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
    rows = q.order_by(IpdFormSubmission.updated_at.desc()).limit(200).all()
    return [_to_response(r) for r in rows]


@router.get("/form-submissions/{submission_id}", response_model=IpdFormSubmissionResponse)
def get_form_submission(
    submission_id: UUID,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    sub = (
        db.query(IpdFormSubmission)
        .options(joinedload(IpdFormSubmission.patient))
        .filter(IpdFormSubmission.id == submission_id, IpdFormSubmission.hospital_id == hospital_id)
        .first()
    )
    if not sub:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")
    return _to_response(sub)


@router.get("/form-submissions/{submission_id}/view")
def view_form_submission_html(
    submission_id: UUID,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    sub = (
        db.query(IpdFormSubmission)
        .options(joinedload(IpdFormSubmission.patient))
        .filter(IpdFormSubmission.id == submission_id, IpdFormSubmission.hospital_id == hospital_id)
        .first()
    )
    if not sub:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")
    if not sub.html_snapshot:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No HTML snapshot for this form")
    patient_label = sub.patient.uhid if sub.patient else "patient"
    return StreamingResponse(
        BytesIO(sub.html_snapshot.encode("utf-8")),
        media_type="text/html",
        headers={
            "Content-Disposition": f'inline; filename="{sub.form_id}-{patient_label}.html"',
        },
    )
