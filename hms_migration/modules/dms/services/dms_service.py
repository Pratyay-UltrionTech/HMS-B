"""
DMS business logic and document assembly service.

Orchestrates multi-module clinical timelines, cross-domain medical records,
patient documents, billing records, and printable HTML complete files.
"""

from __future__ import annotations

import base64
from datetime import date, datetime, time, timezone
from io import BytesIO
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload

from hms_migration.modules.appointments.entities.appointment import Appointment
from hms_migration.modules.billing.entities.billing_entities import (
    BillingInvoice,
    BillingInvoiceStatus,
    BillingReceipt,
    BillingReceiptStatus,
)
from hms_migration.modules.billing.services.billing_service import (
    build_ledger_entries,
    patient_ledger_totals,
)
from hms_migration.modules.clinical_records.entities.clinical_record import (
    MedicalRecord,
    PatientDocument,
    PatientDocumentCategory,
    Prescription,
)
from hms_migration.modules.dms.contracts.dms_contracts import (
    DmsAdmissionItem,
    DmsBillingItem,
    DmsDocumentResponse,
    DmsLabItem,
    DmsOtItem,
    DmsPatientFile,
    DmsPatientItem,
    DmsPharmacyItem,
    DmsPrescriptionItem,
    DmsRadiologyItem,
    DmsTimelineEvent,
)
from hms_migration.modules.doctors.entities.doctor import HospitalUser
from hms_migration.modules.inpatient.entities.admission import (
    Admission,
    IpdFormSubmission,
    IpdFormSubmissionStatus,
)
from hms_migration.modules.laboratory.entities.lab_entities import LabOrder
from hms_migration.modules.patients.entities.patient import Patient


def _normalize_dt(val: Any) -> datetime:
    if not val:
        return datetime.min.replace(tzinfo=timezone.utc)
    if isinstance(val, date) and not isinstance(val, datetime):
        val = datetime.combine(val, time.min)
    if val.tzinfo is None:
        return val.replace(tzinfo=timezone.utc)
    return val.astimezone(timezone.utc)


def stream_data_url(data: str, name: str) -> StreamingResponse:
    """Stream base64 data-URL payload as binary file attachment."""
    if not data.startswith("data:"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid file data")
    header, b64 = data.split(",", 1)
    mime = header.split(";")[0].replace("data:", "") or "application/octet-stream"
    raw = base64.b64decode(b64)
    return StreamingResponse(
        BytesIO(raw),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


def build_documents(db: Session, patient_id: UUID, hospital_id: UUID) -> list[DmsDocumentResponse]:
    """Collate documents uploaded directly to DMS and attached via MedicalRecord or IPD forms."""
    docs: list[DmsDocumentResponse] = []

    # 1. PatientDocument (DMS native)
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

    # 2. MedicalRecord (Doctor clinical uploads)
    for r in (
        db.query(MedicalRecord)
        .options(joinedload(MedicalRecord.doctor))
        .filter(MedicalRecord.hospital_id == hospital_id, MedicalRecord.patient_id == patient_id)
        .order_by(MedicalRecord.created_at.desc())
        .all()
    ):
        if (r.report_type or "").strip().lower() == "ipd form":
            continue
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

    # 3. IPD Form submissions
    for s in (
        db.query(IpdFormSubmission)
        .filter(
            IpdFormSubmission.hospital_id == hospital_id,
            IpdFormSubmission.patient_id == patient_id,
            IpdFormSubmission.status == IpdFormSubmissionStatus.final,
        )
        .order_by(IpdFormSubmission.updated_at.desc())
        .all()
    ):
        if s.patient_document_id:
            continue
        cat = (
            PatientDocumentCategory.consent
            if "consent" in (s.form_title or "").lower()
            else PatientDocumentCategory.other
        )
        docs.append(
            DmsDocumentResponse(
                id=s.id,
                patient_id=s.patient_id,
                category=cat,
                title=s.form_title,
                notes=f"IPD form · {s.form_id}",
                file_name=f"{s.form_id}.html" if s.html_snapshot else None,
                has_file=bool(s.html_snapshot),
                uploaded_by_name=s.filled_by_name or "",
                uploaded_by_role=s.filled_by_role or "staff",
                created_at=s.updated_at or s.created_at,
                source="ipd_form",
                doctor_name=s.filled_by_name or None,
                report_type="IPD Form",
            )
        )

    docs.sort(key=lambda x: _normalize_dt(x.created_at), reverse=True)
    return docs


def build_timeline(db: Session, patient: Patient, hospital_id: UUID) -> list[DmsTimelineEvent]:
    """Assemble chronological timeline of all patient events across modules."""
    events: list[DmsTimelineEvent] = []

    # 1. Registration
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

    # 2. Appointments
    for a in (
        db.query(Appointment)
        .filter(Appointment.hospital_id == hospital_id, Appointment.patient_id == patient.id)
        .all()
    ):
        when = (
            datetime.combine(a.appointment_date, a.appointment_time).replace(tzinfo=timezone.utc)
            if a.appointment_date and a.appointment_time
            else a.created_at
        )
        doctor = db.get(HospitalUser, a.doctor_id) if a.doctor_id else None
        doctor_name = doctor.name if doctor else "Doctor"
        events.append(
            DmsTimelineEvent(
                id=f"appt-{a.id}",
                event_type="appointment",
                title=f"Appointment with {doctor_name}",
                detail=f"{a.purpose} · {a.status.value}",
                occurred_at=when,
                source_module="appointment",
                entity_id=str(a.id),
            )
        )

    # 3. Prescriptions
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

    # 4. Laboratory Orders
    for o in (
        db.query(LabOrder)
        .options(joinedload(LabOrder.items), joinedload(LabOrder.doctor))
        .filter(LabOrder.hospital_id == hospital_id, LabOrder.patient_id == patient.id)
        .all()
    ):
        test_names = ", ".join(i.test_name for i in o.items if i.test_name) if o.items else "Lab tests"
        events.append(
            DmsTimelineEvent(
                id=f"lab-order-{o.id}",
                event_type="lab_order",
                title="Laboratory Ordered",
                detail=f"{o.order_no} · {test_names}",
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
                    title=f"{test_names} Report Uploaded",
                    detail=o.order_no,
                    occurred_at=o.completed_at,
                    source_module="laboratory",
                    entity_id=str(o.id),
                    view_path=f"/api/laboratory/orders/{o.id}/report",
                )
            )

    # 5. Radiology Orders (safe lookup)
    try:
        from hms_migration.modules.radiology.entities.radiology_entities import RadiologyOrder

        for o in (
            db.query(RadiologyOrder)
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
    except Exception:
        pass

    # 6. Admissions
    for a in (
        db.query(Admission)
        .options(joinedload(Admission.ward), joinedload(Admission.bed))
        .filter(Admission.hospital_id == hospital_id, Admission.patient_id == patient.id)
        .all()
    ):
        ward_name = a.ward.name if a.ward else "Ward"
        bed_code = a.bed.bed_code if a.bed else "—"
        events.append(
            DmsTimelineEvent(
                id=f"adm-{a.id}",
                event_type="admission",
                title="Patient Admitted",
                detail=f"{ward_name} · Bed {bed_code}",
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

    # 7. Patient Documents
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

    # 8. IPD Form Submissions
    for s in (
        db.query(IpdFormSubmission)
        .filter(
            IpdFormSubmission.hospital_id == hospital_id,
            IpdFormSubmission.patient_id == patient.id,
            IpdFormSubmission.status == IpdFormSubmissionStatus.final,
        )
        .all()
    ):
        events.append(
            DmsTimelineEvent(
                id=f"ipd-form-{s.id}",
                event_type="ipd_form",
                title=f"IPD Form — {s.form_title}",
                detail=f"Filled by {s.filled_by_name or 'Staff'}",
                occurred_at=s.updated_at or s.created_at,
                source_module="ipd",
                entity_id=str(s.id),
                view_path=f"/api/ipd/form-submissions/{s.id}/view" if s.html_snapshot else None,
            )
        )

    events.sort(key=lambda e: _normalize_dt(e.occurred_at))
    return events


def build_patient_file(
    db: Session,
    patient_item: DmsPatientItem,
    patient: Patient,
    hospital_id: UUID,
) -> DmsPatientFile:
    """Build unified patient electronic file containing records from all clinical & financial modules."""
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
            test_names=", ".join(i.test_name for i in o.items if i.test_name) if o.items else None,
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

    radiology_reports: list[DmsRadiologyItem] = []
    try:
        from hms_migration.modules.radiology.entities.radiology_entities import RadiologyOrder
        from hms_migration.modules.doctors.entities.doctor import HospitalUser

        rad_orders = (
            db.query(RadiologyOrder)
            .filter(RadiologyOrder.hospital_id == hospital_id, RadiologyOrder.patient_id == patient.id)
            .order_by(RadiologyOrder.ordered_at.desc())
            .all()
        )
        doc_ids = {o.doctor_id for o in rad_orders if getattr(o, "doctor_id", None)}
        doc_map = {d.id: d for d in db.query(HospitalUser).filter(HospitalUser.id.in_(doc_ids)).all()} if doc_ids else {}

        for o in rad_orders:
            doc = doc_map.get(getattr(o, "doctor_id", None))
            radiology_reports.append(
                DmsRadiologyItem(
                    id=o.id,
                    order_no=o.order_no,
                    scan_name=o.scan_name,
                    scan_code=o.scan_code,
                    status=o.status.value,
                    ordered_at=o.ordered_at,
                    has_report_file=bool(getattr(o, "report_file_data", None)),
                    has_image_file=bool(getattr(o, "image_file_data", None)),
                    doctor_name=doc.name if doc else None,
                    report_view_path=f"/api/radiology/orders/{o.id}/report-view",
                    report_file_path=f"/api/radiology/orders/{o.id}/file/report" if getattr(o, "report_file_data", None) else None,
                    image_file_path=f"/api/radiology/orders/{o.id}/file/image" if getattr(o, "image_file_data", None) else None,
                )
            )
    except Exception:
        pass

    ot_records: list[DmsOtItem] = []
    try:
        from hms_migration.modules.ot.entities.ot_entities import OtSurgery
        from hms_migration.modules.doctors.entities.doctor import HospitalUser

        surgeries = (
            db.query(OtSurgery)
            .filter(OtSurgery.hospital_id == hospital_id, OtSurgery.patient_id == patient.id)
            .order_by(OtSurgery.scheduled_at.desc())
            .all()
        )
        surg_ids = {s.surgeon_id for s in surgeries if getattr(s, "surgeon_id", None)}
        surg_map = {d.id: d for d in db.query(HospitalUser).filter(HospitalUser.id.in_(surg_ids)).all()} if surg_ids else {}

        for s in surgeries:
            surg = surg_map.get(getattr(s, "surgeon_id", None))
            ot_records.append(
                DmsOtItem(
                    id=s.id,
                    surgery_no=s.surgery_no,
                    surgery_type=s.surgery_type,
                    surgeon_name=s.surgeon.name if s.surgeon else None,
                    scheduled_at=s.scheduled_at,
                    status=s.status.value,
                    has_notes=bool(s.pre_op_diagnosis and s.procedure_performed),
                    has_ot_report=bool(getattr(s, "ot_report_file_data", None)),
                    has_consent=bool(getattr(s, "consent_file_data", None)),
                    summary_path=f"/api/ot/surgeries/{s.id}/summary-view",
                    report_file_path=f"/api/ot/surgeries/{s.id}/file/report" if getattr(s, "ot_report_file_data", None) else None,
                )
            )
    except Exception:
        pass

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

    billing_documents: list[DmsBillingItem] = []
    fin = patient_ledger_totals(db, hospital_id, patient.id)
    ledger_rows = build_ledger_entries(db, hospital_id, patient.id)
    billing_documents.append(
        DmsBillingItem(
            id="summary",
            doc_type="summary",
            title="Financial summary",
            amount=fin["outstanding"],
            status="outstanding" if fin["outstanding"] > 0 else "settled",
            note=f"Charges ₹{fin['total_charges']:.2f} · Paid ₹{fin['total_paid']:.2f} · Outstanding ₹{fin['outstanding']:.2f}",
        )
    )
    for e in ledger_rows[:40]:
        billing_documents.append(
            DmsBillingItem(
                id=str(e["ref_id"]),
                doc_type=e["entry_type"],
                title=e["description"],
                amount=e["debit"] if e["entry_type"] == "charge" else e["credit"],
                status=e["status"] or "recorded",
                created_at=e.get("occurred_at"),
                note=None,
            )
        )

    invoices = [
        DmsBillingItem(
            id=str(inv.id),
            doc_type="invoice",
            title=inv.invoice_number,
            amount=float(inv.grand_total),
            status=inv.status.value,
            created_at=datetime.combine(inv.invoice_date, time.min).replace(tzinfo=timezone.utc)
            if inv.invoice_date
            else inv.created_at,
            note=f"/api/billing/invoices/{inv.id}/print",
        )
        for inv in (
            db.query(BillingInvoice)
            .filter(
                BillingInvoice.hospital_id == hospital_id,
                BillingInvoice.patient_id == patient.id,
                BillingInvoice.status != BillingInvoiceStatus.cancelled,
            )
            .order_by(BillingInvoice.created_at.desc())
            .limit(50)
            .all()
        )
    ]

    receipts = [
        DmsBillingItem(
            id=str(rcpt.id),
            doc_type="receipt",
            title=rcpt.receipt_number,
            amount=float(rcpt.amount),
            status=rcpt.status.value,
            created_at=datetime.combine(rcpt.payment_date, time.min).replace(tzinfo=timezone.utc)
            if rcpt.payment_date
            else rcpt.created_at,
            note=f"/api/billing/receipts/{rcpt.id}/print",
        )
        for rcpt in (
            db.query(BillingReceipt)
            .filter(
                BillingReceipt.hospital_id == hospital_id,
                BillingReceipt.patient_id == patient.id,
                BillingReceipt.status != BillingReceiptStatus.cancelled,
            )
            .order_by(BillingReceipt.created_at.desc())
            .limit(50)
            .all()
        )
    ]

    pharmacy_sales: list[DmsPharmacyItem] = []
    try:
        from hms_migration.modules.pharmacy.entities.pharmacy_entities import PharmacySale, PharmacySaleStatus, PharmacySaleItem

        sales = (
            db.query(PharmacySale)
            .filter(
                PharmacySale.hospital_id == hospital_id,
                PharmacySale.patient_id == patient.id,
                PharmacySale.status != PharmacySaleStatus.cancelled,
            )
            .order_by(PharmacySale.sale_date.desc())
            .all()
        )
        sale_ids = {s.id for s in sales}
        items = db.query(PharmacySaleItem).filter(PharmacySaleItem.sale_id.in_(sale_ids)).all() if sale_ids else []
        item_count_map: dict[UUID, int] = {}
        for it in items:
            item_count_map[it.sale_id] = item_count_map.get(it.sale_id, 0) + 1

        for s in sales:
            pharmacy_sales.append(
                DmsPharmacyItem(
                    id=s.id,
                    invoice_number=s.invoice_number,
                    customer_name=s.customer_name,
                    net_amount=float(s.net_amount or 0),
                    payment_status=s.payment_status.value,
                    status=s.status.value,
                    sale_date=s.sale_date,
                    item_count=item_count_map.get(s.id, 0),
                )
            )
    except Exception:
        pass

    return DmsPatientFile(
        patient=patient_item,
        timeline=build_timeline(db, patient, hospital_id),
        documents=build_documents(db, patient.id, hospital_id),
        prescriptions=prescriptions,
        lab_reports=lab_reports,
        radiology_reports=radiology_reports,
        ot_records=ot_records,
        admissions=admissions,
        billing_documents=billing_documents,
        invoices=invoices,
        receipts=receipts,
        pharmacy_sales=pharmacy_sales,
    )


def complete_file_html(file: DmsPatientFile, hospital_name: str = "Hospital") -> str:
    """Generate printable HTML complete patient file."""
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
  <h1>{esc(hospital_name)}</h1>
  <p class="meta">Complete Patient Medical File · Generated {datetime.now(timezone.utc).strftime('%d %b %Y %H:%M')} UTC</p>
  <p><strong>{esc(p.name)}</strong> ({esc(p.uhid)}) · {esc(p.mobile)} · {esc(p.care_status)} · Age {p.age if p.age is not None else '—'}</p>
  <p class="meta">Insurance: {'Yes — ' + esc(p.insurance_provider) if p.has_insurance else 'No'}
  · Emergency: {esc(p.emergency_contact_name)}{(' (' + esc(p.emergency_contact_relation) + ')') if p.emergency_contact_relation else ''}{(' · ' + esc(p.emergency_contact)) if p.emergency_contact else ''}</p>

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
