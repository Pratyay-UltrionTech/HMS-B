from datetime import date, datetime, time, timezone
from io import BytesIO
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import (
    Admission,
    AdmissionStatus,
    Appointment,
    Hospital,
    LabOrder,
    MedicalRecord,
    OtSurgery,
    Patient,
    PatientDocument,
    PatientDocumentCategory,
    PatientStatus,
    Prescription,
    RadiologyOrder,
)
from app.schemas_dms import (
    DmsAdmissionItem,
    DmsBillingItem,
    DmsDocumentCreate,
    DmsDocumentResponse,
    DmsLabItem,
    DmsOtItem,
    DmsPatientFile,
    DmsPatientItem,
    DmsPrescriptionItem,
    DmsRadiologyItem,
    DmsTimelineEvent,
)
from app.utils.audit import write_audit
from app.utils.auth import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/dms", tags=["dms"])


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


def _get_patient(db: Session, patient_id: UUID, hospital_id: UUID) -> Patient:
    patient = (
        db.query(Patient)
        .filter(Patient.id == patient_id, Patient.hospital_id == hospital_id)
        .first()
    )
    if not patient:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
    return patient


def _patient_item(db: Session, patient: Patient) -> DmsPatientItem:
    open_adm = (
        db.query(Admission.id)
        .filter(
            Admission.patient_id == patient.id,
            Admission.hospital_id == patient.hospital_id,
            Admission.status == AdmissionStatus.admitted,
        )
        .first()
    )
    last_appt = (
        db.query(Appointment)
        .filter(Appointment.patient_id == patient.id, Appointment.hospital_id == patient.hospital_id)
        .order_by(Appointment.appointment_date.desc())
        .first()
    )
    return DmsPatientItem(
        id=patient.id,
        uhid=patient.uhid,
        name=patient.name,
        mobile=patient.mobile,
        email=patient.email,
        gender=patient.gender,
        age=patient.age,
        care_status=_care_status(patient, bool(open_adm)),
        status=patient.status.value if hasattr(patient.status, "value") else str(patient.status),
        last_visit=last_appt.appointment_date if last_appt else None,
        created_at=patient.created_at,
    )


def _lab_test_names(order: LabOrder) -> str | None:
    if not order.items:
        return None
    names = [i.test_name for i in order.items if i.test_name]
    return ", ".join(names) if names else None


@router.get("/patients", response_model=list[DmsPatientItem])
def list_patients(
    search: str | None = None,
    care_status: str | None = Query(None, description="OPD | IPD | Inactive"),
    doctor_id: UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = db.query(Patient).filter(Patient.hospital_id == hospital_id)
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
                    db.query(Appointment.patient_id).filter(
                        Appointment.hospital_id == hospital_id, Appointment.doctor_id == doctor_id
                    )
                ),
                Patient.id.in_(
                    db.query(Prescription.patient_id).filter(
                        Prescription.hospital_id == hospital_id, Prescription.doctor_id == doctor_id
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

    patients = q.order_by(Patient.created_at.desc()).limit(500).all()
    items = [_patient_item(db, p) for p in patients]
    if care_status:
        key = care_status.strip().upper()
        items = [i for i in items if i.care_status.upper() == key]
    return items


@router.get("/patients/{patient_id}/file", response_model=DmsPatientFile)
def get_patient_file(
    patient_id: UUID,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    patient = _get_patient(db, patient_id, hospital_id)
    return _build_patient_file(db, patient, hospital_id)


@router.get("/patients/{patient_id}/timeline", response_model=list[DmsTimelineEvent])
def get_timeline(
    patient_id: UUID,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    patient = _get_patient(db, patient_id, hospital_id)
    return _build_timeline(db, patient, hospital_id)


@router.get("/patients/{patient_id}/documents", response_model=list[DmsDocumentResponse])
def list_documents(
    patient_id: UUID,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    _get_patient(db, patient_id, hospital_id)
    return _build_documents(db, patient_id, hospital_id)


@router.post(
    "/patients/{patient_id}/documents",
    response_model=DmsDocumentResponse,
    status_code=status.HTTP_201_CREATED,
)
def upload_document(
    patient_id: UUID,
    payload: DmsDocumentCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    _get_patient(db, patient_id, hospital_id)
    doc = PatientDocument(
        hospital_id=hospital_id,
        patient_id=patient_id,
        category=payload.category,
        title=payload.title.strip(),
        notes=payload.notes.strip() if payload.notes else None,
        file_name=payload.file_name,
        file_data=payload.file_data,
        uploaded_by_name=_actor_name(user),
        uploaded_by_role=_actor_role(user),
    )
    db.add(doc)
    db.flush()
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="patient_document",
        entity_id=doc.id,
        summary=f"Uploaded DMS document '{doc.title}' ({doc.category.value})",
    )
    db.commit()
    db.refresh(doc)
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


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    document_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    doc = (
        db.query(PatientDocument)
        .filter(PatientDocument.id == document_id, PatientDocument.hospital_id == hospital_id)
        .first()
    )
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    title = doc.title
    db.delete(doc)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="delete",
        entity_type="patient_document",
        entity_id=document_id,
        summary=f"Deleted DMS document '{title}'",
    )
    db.commit()
    return None


@router.get("/documents/{document_id}/file")
def download_dms_file(
    document_id: UUID,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    doc = (
        db.query(PatientDocument)
        .filter(PatientDocument.id == document_id, PatientDocument.hospital_id == hospital_id)
        .first()
    )
    if not doc or not doc.file_data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return _stream_data_url(doc.file_data, doc.file_name or "document")


@router.get("/medical-records/{record_id}/file")
def download_medical_record_file(
    record_id: UUID,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    rec = (
        db.query(MedicalRecord)
        .filter(MedicalRecord.id == record_id, MedicalRecord.hospital_id == hospital_id)
        .first()
    )
    if not rec or not rec.file_data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return _stream_data_url(rec.file_data, rec.file_name or "medical-record")


@router.get("/patients/{patient_id}/complete-file")
def complete_patient_file_html(
    patient_id: UUID,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    patient = _get_patient(db, patient_id, hospital_id)
    file = _build_patient_file(db, patient, hospital_id)
    hospital = db.query(Hospital).filter(Hospital.id == hospital_id).first()
    html = _complete_file_html(file, hospital)
    return StreamingResponse(
        BytesIO(html.encode("utf-8")),
        media_type="text/html",
        headers={"Content-Disposition": f'inline; filename="{patient.uhid}-medical-file.html"'},
    )


def _stream_data_url(data: str, name: str):
    if not data.startswith("data:"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid file data")
    header, b64 = data.split(",", 1)
    mime = header.split(";")[0].replace("data:", "") or "application/octet-stream"
    import base64

    raw = base64.b64decode(b64)
    return StreamingResponse(
        BytesIO(raw),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


def _build_documents(db: Session, patient_id: UUID, hospital_id: UUID) -> list[DmsDocumentResponse]:
    docs: list[DmsDocumentResponse] = []
    for d in (
        db.query(PatientDocument)
        .filter(PatientDocument.hospital_id == hospital_id, PatientDocument.patient_id == patient_id)
        .order_by(PatientDocument.created_at.desc())
        .all()
    ):
        docs.append(
            DmsDocumentResponse(
                id=d.id,
                patient_id=d.patient_id,
                category=d.category,
                title=d.title,
                notes=d.notes,
                file_name=d.file_name,
                has_file=bool(d.file_data),
                uploaded_by_name=d.uploaded_by_name,
                uploaded_by_role=d.uploaded_by_role,
                created_at=d.created_at,
                source="dms",
            )
        )
    for r in (
        db.query(MedicalRecord)
        .options(joinedload(MedicalRecord.doctor))
        .filter(MedicalRecord.hospital_id == hospital_id, MedicalRecord.patient_id == patient_id)
        .order_by(MedicalRecord.created_at.desc())
        .all()
    ):
        cat = PatientDocumentCategory.other
        rt = (r.report_type or "").lower()
        if "consent" in rt:
            cat = PatientDocumentCategory.consent
        elif "discharge" in rt:
            cat = PatientDocumentCategory.discharge_summary
        elif "insurance" in rt:
            cat = PatientDocumentCategory.insurance
        elif "aadhaar" in rt or "aadhar" in rt:
            cat = PatientDocumentCategory.aadhaar
        elif "referral" in rt:
            cat = PatientDocumentCategory.referral
        docs.append(
            DmsDocumentResponse(
                id=r.id,
                patient_id=r.patient_id,
                category=cat,
                title=r.title,
                notes=r.notes,
                file_name=r.file_name,
                has_file=bool(r.file_data),
                uploaded_by_name=r.doctor.name if r.doctor else "",
                uploaded_by_role="doctor",
                created_at=r.created_at,
                source="medical_record",
                doctor_name=r.doctor.name if r.doctor else None,
                report_type=r.report_type,
            )
        )
    docs.sort(key=lambda x: x.created_at, reverse=True)
    return docs


def _build_timeline(db: Session, patient: Patient, hospital_id: UUID) -> list[DmsTimelineEvent]:
    events: list[DmsTimelineEvent] = []
    events.append(
        DmsTimelineEvent(
            id=f"reg-{patient.id}",
            event_type="registration",
            title="Registration Completed",
            detail=f"UHID {patient.uhid}",
            occurred_at=patient.created_at,
            source_module="registration",
            entity_id=str(patient.id),
        )
    )

    appts = (
        db.query(Appointment)
        .options(joinedload(Appointment.doctor))
        .filter(Appointment.hospital_id == hospital_id, Appointment.patient_id == patient.id)
        .all()
    )
    for a in appts:
        when = datetime.combine(a.appointment_date, a.appointment_time)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        events.append(
            DmsTimelineEvent(
                id=f"appt-{a.id}",
                event_type="appointment",
                title=f"Appointment with {a.doctor.name if a.doctor else 'Doctor'}",
                detail=f"{a.purpose} · {a.status.value}",
                occurred_at=when,
                source_module="appointment",
                entity_id=str(a.id),
            )
        )

    for p in (
        db.query(Prescription)
        .options(joinedload(Prescription.doctor))
        .filter(Prescription.hospital_id == hospital_id, Prescription.patient_id == patient.id)
        .all()
    ):
        events.append(
            DmsTimelineEvent(
                id=f"rx-{p.id}",
                event_type="prescription",
                title="Prescription Added",
                detail=f"{p.diagnosis} · Dr. {p.doctor.name if p.doctor else '—'}",
                occurred_at=p.created_at,
                source_module="doctors",
                entity_id=str(p.id),
                view_path=f"/api/doctors/{p.doctor_id}/prescriptions/{p.id}/pdf",
            )
        )

    for o in (
        db.query(LabOrder)
        .options(joinedload(LabOrder.items), joinedload(LabOrder.doctor))
        .filter(LabOrder.hospital_id == hospital_id, LabOrder.patient_id == patient.id)
        .all()
    ):
        names = _lab_test_names(o) or "Lab tests"
        events.append(
            DmsTimelineEvent(
                id=f"lab-order-{o.id}",
                event_type="lab_order",
                title="Laboratory Ordered",
                detail=f"{o.order_no} · {names}",
                occurred_at=o.ordered_at,
                source_module="laboratory",
                entity_id=str(o.id),
            )
        )
        if o.status.value == "completed" and o.completed_at:
            events.append(
                DmsTimelineEvent(
                    id=f"lab-report-{o.id}",
                    event_type="lab_report",
                    title=f"{names} Report Uploaded",
                    detail=o.order_no,
                    occurred_at=o.completed_at,
                    source_module="laboratory",
                    entity_id=str(o.id),
                    view_path=f"/api/laboratory/orders/{o.id}/report",
                )
            )

    for o in (
        db.query(RadiologyOrder)
        .options(joinedload(RadiologyOrder.doctor))
        .filter(RadiologyOrder.hospital_id == hospital_id, RadiologyOrder.patient_id == patient.id)
        .all()
    ):
        events.append(
            DmsTimelineEvent(
                id=f"rad-order-{o.id}",
                event_type="radiology_order",
                title=f"{o.scan_name} Ordered",
                detail=o.order_no,
                occurred_at=o.ordered_at,
                source_module="radiology",
                entity_id=str(o.id),
            )
        )
        if o.report_date or (o.status.value == "completed" and o.completed_at):
            at = (
                datetime.combine(o.report_date, time.min).replace(tzinfo=timezone.utc)
                if o.report_date
                else o.completed_at
            )
            if at:
                events.append(
                    DmsTimelineEvent(
                        id=f"rad-report-{o.id}",
                        event_type="radiology_report",
                        title=f"{o.scan_name} Report Uploaded",
                        detail=o.order_no,
                        occurred_at=at,
                        source_module="radiology",
                        entity_id=str(o.id),
                        view_path=f"/api/radiology/orders/{o.id}/report-view",
                    )
                )

    for a in (
        db.query(Admission)
        .options(joinedload(Admission.ward), joinedload(Admission.bed), joinedload(Admission.doctor))
        .filter(Admission.hospital_id == hospital_id, Admission.patient_id == patient.id)
        .all()
    ):
        events.append(
            DmsTimelineEvent(
                id=f"adm-{a.id}",
                event_type="admission",
                title="Patient Admitted",
                detail=f"{a.ward.name if a.ward else 'Ward'} · Bed {a.bed.bed_code if a.bed else '—'}",
                occurred_at=a.admitted_at,
                source_module="bed",
                entity_id=str(a.id),
            )
        )
        if a.discharged_at:
            events.append(
                DmsTimelineEvent(
                    id=f"dis-{a.id}",
                    event_type="discharge",
                    title="Discharged",
                    detail=a.discharge_notes or "Discharge completed",
                    occurred_at=a.discharged_at,
                    source_module="bed",
                    entity_id=str(a.id),
                )
            )

    for s in (
        db.query(OtSurgery)
        .options(joinedload(OtSurgery.surgeon))
        .filter(OtSurgery.hospital_id == hospital_id, OtSurgery.patient_id == patient.id)
        .all()
    ):
        events.append(
            DmsTimelineEvent(
                id=f"ot-book-{s.id}",
                event_type="ot_booking",
                title="Surgery Booked",
                detail=f"{s.surgery_type} · {s.surgery_no}",
                occurred_at=s.created_at,
                source_module="ot",
                entity_id=str(s.id),
            )
        )
        if s.completed_at or s.status.value == "completed":
            events.append(
                DmsTimelineEvent(
                    id=f"ot-done-{s.id}",
                    event_type="ot_completed",
                    title="Operation Performed",
                    detail=f"{s.surgery_type} · {s.surgeon.name if s.surgeon else '—'}",
                    occurred_at=s.completed_at or s.scheduled_at,
                    source_module="ot",
                    entity_id=str(s.id),
                    view_path=f"/api/ot/surgeries/{s.id}/summary-view",
                )
            )

    for d in (
        db.query(PatientDocument)
        .filter(PatientDocument.hospital_id == hospital_id, PatientDocument.patient_id == patient.id)
        .all()
    ):
        events.append(
            DmsTimelineEvent(
                id=f"doc-{d.id}",
                event_type="document",
                title=f"Document Uploaded — {d.title}",
                detail=d.category.value.replace("_", " ").title(),
                occurred_at=d.created_at,
                source_module="dms",
                entity_id=str(d.id),
            )
        )

    events.sort(key=lambda e: e.occurred_at or datetime.min.replace(tzinfo=timezone.utc))
    return events


def _build_patient_file(db: Session, patient: Patient, hospital_id: UUID) -> DmsPatientFile:
    prescriptions = [
        DmsPrescriptionItem(
            id=p.id,
            doctor_id=p.doctor_id,
            doctor_name=p.doctor.name if p.doctor else None,
            diagnosis=p.diagnosis,
            medicines=p.medicines,
            dosage=p.dosage,
            advice=p.advice,
            created_at=p.created_at,
            pdf_path=f"/api/doctors/{p.doctor_id}/prescriptions/{p.id}/pdf",
        )
        for p in (
            db.query(Prescription)
            .options(joinedload(Prescription.doctor))
            .filter(Prescription.hospital_id == hospital_id, Prescription.patient_id == patient.id)
            .order_by(Prescription.created_at.desc())
            .all()
        )
    ]

    lab_reports = [
        DmsLabItem(
            id=o.id,
            order_no=o.order_no,
            status=o.status.value,
            test_names=_lab_test_names(o),
            ordered_at=o.ordered_at,
            completed_at=o.completed_at,
            doctor_name=o.doctor.name if o.doctor else None,
            report_path=f"/api/laboratory/orders/{o.id}/report",
        )
        for o in (
            db.query(LabOrder)
            .options(joinedload(LabOrder.items), joinedload(LabOrder.doctor))
            .filter(LabOrder.hospital_id == hospital_id, LabOrder.patient_id == patient.id)
            .order_by(LabOrder.ordered_at.desc())
            .all()
        )
    ]

    radiology_reports = [
        DmsRadiologyItem(
            id=o.id,
            order_no=o.order_no,
            scan_name=o.scan_name,
            scan_code=o.scan_code,
            status=o.status.value,
            ordered_at=o.ordered_at,
            has_report_file=bool(o.report_file_data),
            has_image_file=bool(o.image_file_data),
            doctor_name=o.doctor.name if o.doctor else None,
            report_view_path=f"/api/radiology/orders/{o.id}/report-view",
            report_file_path=f"/api/radiology/orders/{o.id}/file/report" if o.report_file_data else None,
            image_file_path=f"/api/radiology/orders/{o.id}/file/image" if o.image_file_data else None,
        )
        for o in (
            db.query(RadiologyOrder)
            .options(joinedload(RadiologyOrder.doctor))
            .filter(RadiologyOrder.hospital_id == hospital_id, RadiologyOrder.patient_id == patient.id)
            .order_by(RadiologyOrder.ordered_at.desc())
            .all()
        )
    ]

    ot_records = [
        DmsOtItem(
            id=s.id,
            surgery_no=s.surgery_no,
            surgery_type=s.surgery_type,
            surgeon_name=s.surgeon.name if s.surgeon else None,
            scheduled_at=s.scheduled_at,
            status=s.status.value,
            has_notes=bool(s.pre_op_diagnosis and s.procedure_performed),
            has_ot_report=bool(s.ot_report_file_data),
            has_consent=bool(s.consent_file_data),
            summary_path=f"/api/ot/surgeries/{s.id}/summary-view",
            report_file_path=f"/api/ot/surgeries/{s.id}/file/report" if s.ot_report_file_data else None,
        )
        for s in (
            db.query(OtSurgery)
            .options(joinedload(OtSurgery.surgeon))
            .filter(OtSurgery.hospital_id == hospital_id, OtSurgery.patient_id == patient.id)
            .order_by(OtSurgery.scheduled_at.desc())
            .all()
        )
    ]

    admissions = [
        DmsAdmissionItem(
            id=a.id,
            status=a.status.value,
            ward_name=a.ward.name if a.ward else None,
            room_name=a.room.name if a.room else None,
            bed_code=a.bed.bed_code if a.bed else None,
            doctor_name=a.doctor.name if a.doctor else None,
            notes=a.notes,
            discharge_notes=a.discharge_notes,
            admitted_at=a.admitted_at,
            discharged_at=a.discharged_at,
        )
        for a in (
            db.query(Admission)
            .options(
                joinedload(Admission.ward),
                joinedload(Admission.room),
                joinedload(Admission.bed),
                joinedload(Admission.doctor),
            )
            .filter(Admission.hospital_id == hospital_id, Admission.patient_id == patient.id)
            .order_by(Admission.admitted_at.desc())
            .all()
        )
    ]

    billing_documents: list[DmsBillingItem] = [
        DmsBillingItem(
            id="stub",
            doc_type="info",
            title="Billing module not configured",
            status="pending",
            note="Bills, invoices, and receipts will appear here once Billing is enabled.",
        )
    ]

    return DmsPatientFile(
        patient=_patient_item(db, patient),
        timeline=_build_timeline(db, patient, hospital_id),
        documents=_build_documents(db, patient.id, hospital_id),
        prescriptions=prescriptions,
        lab_reports=lab_reports,
        radiology_reports=radiology_reports,
        ot_records=ot_records,
        admissions=admissions,
        billing_documents=billing_documents,
    )


def _complete_file_html(file: DmsPatientFile, hospital: Hospital | None) -> str:
    hosp = hospital.name if hospital else "Hospital"
    p = file.patient

    def esc(s: str | None) -> str:
        return (s or "—").replace("<", "&lt;").replace(">", "&gt;")

    timeline_html = "".join(
        f"<li><strong>{e.occurred_at.strftime('%d %b %Y %H:%M')}</strong> — {esc(e.title)}"
        f"{(' · ' + esc(e.detail)) if e.detail else ''}</li>"
        for e in file.timeline
    )
    rx_html = "".join(
        f"<tr><td>{r.created_at.strftime('%d %b %Y')}</td><td>{esc(r.doctor_name)}</td>"
        f"<td>{esc(r.diagnosis)}</td><td>{esc(r.medicines)}</td></tr>"
        for r in file.prescriptions
    ) or "<tr><td colspan='4'>None</td></tr>"
    lab_html = "".join(
        f"<tr><td>{l.ordered_at.strftime('%d %b %Y')}</td><td>{esc(l.test_names)}</td>"
        f"<td>{esc(l.status)}</td></tr>"
        for l in file.lab_reports
    ) or "<tr><td colspan='3'>None</td></tr>"
    rad_html = "".join(
        f"<tr><td>{r.ordered_at.strftime('%d %b %Y')}</td><td>{esc(r.scan_name)}</td>"
        f"<td>{esc(r.status)}</td></tr>"
        for r in file.radiology_reports
    ) or "<tr><td colspan='3'>None</td></tr>"
    ot_html = "".join(
        f"<tr><td>{o.scheduled_at.strftime('%d %b %Y')}</td><td>{esc(o.surgery_type)}</td>"
        f"<td>{esc(o.surgeon_name)}</td><td>{esc(o.status)}</td></tr>"
        for o in file.ot_records
    ) or "<tr><td colspan='4'>None</td></tr>"
    adm_html = "".join(
        f"<tr><td>{a.admitted_at.strftime('%d %b %Y')}</td>"
        f"<td>{esc(a.ward_name)} / {esc(a.bed_code)}</td><td>{esc(a.status)}</td>"
        f"<td>{esc(a.discharge_notes)}</td></tr>"
        for a in file.admissions
    ) or "<tr><td colspan='4'>None</td></tr>"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>{esc(p.uhid)} Medical File</title>
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; max-width: 900px; margin: 28px auto; color: #0f172a; }}
  h1 {{ color: #4338ca; margin-bottom: 4px; }}
  h2 {{ color: #312e81; border-bottom: 1px solid #e2e8f0; padding-bottom: 6px; margin-top: 28px; }}
  .meta {{ color: #64748b; font-size: 13px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ border: 1px solid #e2e8f0; padding: 8px; text-align: left; }}
  th {{ background: #eef2ff; }}
  ul.timeline {{ list-style: none; padding: 0; }}
  ul.timeline li {{ padding: 8px 0; border-left: 3px solid #6366f1; padding-left: 12px; margin: 6px 0; }}
  @media print {{ body {{ margin: 12px; }} }}
</style></head><body>
  <h1>{esc(hosp)}</h1>
  <p class="meta">Complete Patient Medical File · Generated {datetime.now(timezone.utc).strftime('%d %b %Y %H:%M')} UTC</p>
  <p><strong>{esc(p.name)}</strong> ({esc(p.uhid)}) · {esc(p.mobile)} · {esc(p.care_status)} · Age {p.age if p.age is not None else '—'}</p>

  <h2>Clinical Timeline</h2>
  <ul class="timeline">{timeline_html or '<li>No events</li>'}</ul>

  <h2>Prescriptions</h2>
  <table><thead><tr><th>Date</th><th>Doctor</th><th>Diagnosis</th><th>Medicines</th></tr></thead>
  <tbody>{rx_html}</tbody></table>

  <h2>Laboratory</h2>
  <table><thead><tr><th>Date</th><th>Test</th><th>Status</th></tr></thead>
  <tbody>{lab_html}</tbody></table>

  <h2>Radiology</h2>
  <table><thead><tr><th>Date</th><th>Scan</th><th>Status</th></tr></thead>
  <tbody>{rad_html}</tbody></table>

  <h2>OT Records</h2>
  <table><thead><tr><th>Date</th><th>Surgery</th><th>Surgeon</th><th>Status</th></tr></thead>
  <tbody>{ot_html}</tbody></table>

  <h2>Admissions</h2>
  <table><thead><tr><th>Admitted</th><th>Ward / Bed</th><th>Status</th><th>Discharge Notes</th></tr></thead>
  <tbody>{adm_html}</tbody></table>
</body></html>"""
