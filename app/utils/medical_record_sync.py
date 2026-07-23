"""Create or update doctor medical records when lab / radiology results are finalized."""

from uuid import UUID

from sqlalchemy.orm import Session

from app.models import LabOrder, LabOrderStatus, MedicalRecord, RadiologyOrder, RadiologyOrderStatus


def _doctor_for_record(order_doctor_id: UUID | None, patient_id: UUID, db: Session, hospital_id: UUID) -> UUID | None:
    if order_doctor_id:
        return order_doctor_id
    return None


def sync_lab_order_medical_record(db: Session, order: LabOrder) -> None:
    if order.status != LabOrderStatus.completed or not order.doctor_id:
        return
    existing = (
        db.query(MedicalRecord)
        .filter(MedicalRecord.hospital_id == order.hospital_id, MedicalRecord.lab_order_id == order.id)
        .first()
    )
    test_names = ", ".join(i.test_name for i in (order.items or [])) or "Laboratory tests"
    panel_names = sorted({i.panel_name for i in (order.items or []) if i.panel_name})
    title_bits = f"Lab Report — {order.order_no}"
    if panel_names:
        title_bits = f"Lab Report — {order.order_no} ({', '.join(panel_names)})"
    lines = []
    for r in order.results or []:
        lines.append(f"{r.parameter_name}: {r.result_value} {r.unit or ''}".strip())
    notes = "\n".join(lines) if lines else order.clinical_notes
    if panel_names:
        panel_line = f"Panels: {', '.join(panel_names)}\nTests: {test_names}"
        notes = f"{panel_line}\n\n{notes}" if notes else panel_line

    if existing:
        existing.title = title_bits
        existing.notes = notes
        existing.appointment_id = order.appointment_id
        return

    db.add(
        MedicalRecord(
            hospital_id=order.hospital_id,
            doctor_id=order.doctor_id,
            patient_id=order.patient_id,
            appointment_id=order.appointment_id,
            lab_order_id=order.id,
            report_type="Blood Report",
            title=title_bits,
            notes=notes,
            file_name=None,
            file_data=None,
        )
    )


def sync_radiology_order_medical_record(db: Session, order: RadiologyOrder) -> None:
    if order.status != RadiologyOrderStatus.completed or not order.doctor_id:
        return
    if not (order.findings or order.impression or order.report_file_data):
        return

    existing = (
        db.query(MedicalRecord)
        .filter(MedicalRecord.hospital_id == order.hospital_id, MedicalRecord.radiology_order_id == order.id)
        .first()
    )
    parts = []
    if order.findings:
        parts.append(f"Findings:\n{order.findings}")
    if order.impression:
        parts.append(f"Impression:\n{order.impression}")
    if order.remarks:
        parts.append(f"Remarks:\n{order.remarks}")
    notes = "\n\n".join(parts)

    report_type = "X-Ray"
    cat = (order.category or "").lower()
    if "mri" in cat:
        report_type = "MRI"
    elif "ct" in cat:
        report_type = "Other"

    if existing:
        existing.title = f"Radiology — {order.scan_name} ({order.order_no})"
        existing.notes = notes
        existing.appointment_id = order.appointment_id
        existing.file_name = order.report_file_name
        existing.file_data = order.report_file_data
        return

    db.add(
        MedicalRecord(
            hospital_id=order.hospital_id,
            doctor_id=order.doctor_id,
            patient_id=order.patient_id,
            appointment_id=order.appointment_id,
            radiology_order_id=order.id,
            report_type=report_type,
            title=f"Radiology — {order.scan_name} ({order.order_no})",
            notes=notes,
            file_name=order.report_file_name,
            file_data=order.report_file_data,
        )
    )


def sync_ot_surgery_medical_record(db: Session, surgery) -> None:
    """Mirror OT operation notes into the surgeon's medical records for the patient."""
    from app.models import OtSurgery

    if not isinstance(surgery, OtSurgery):
        return
    if not surgery.surgeon_id:
        return
    if not (surgery.pre_op_diagnosis and surgery.procedure_performed):
        return

    title = f"Operation Notes — {surgery.surgery_no}"
    existing = (
        db.query(MedicalRecord)
        .filter(
            MedicalRecord.hospital_id == surgery.hospital_id,
            MedicalRecord.doctor_id == surgery.surgeon_id,
            MedicalRecord.patient_id == surgery.patient_id,
            MedicalRecord.title == title,
        )
        .first()
    )
    parts = [
        f"Pre-operative diagnosis:\n{surgery.pre_op_diagnosis}",
        f"Procedure performed:\n{surgery.procedure_performed}",
    ]
    if surgery.findings:
        parts.append(f"Findings:\n{surgery.findings}")
    if surgery.implants_used:
        parts.append(f"Implants:\n{surgery.implants_used}")
    if surgery.complications:
        parts.append(f"Complications:\n{surgery.complications}")
    if surgery.post_op_instructions:
        parts.append(f"Post-op instructions:\n{surgery.post_op_instructions}")
    if surgery.follow_up_notes:
        parts.append(f"Follow-up:\n{surgery.follow_up_notes}")
    notes = "\n\n".join(parts)

    if existing:
        existing.notes = notes
        existing.report_type = "Other"
        if surgery.ot_report_file_data:
            existing.file_name = surgery.ot_report_file_name
            existing.file_data = surgery.ot_report_file_data
        return

    db.add(
        MedicalRecord(
            hospital_id=surgery.hospital_id,
            doctor_id=surgery.surgeon_id,
            patient_id=surgery.patient_id,
            appointment_id=None,
            report_type="Other",
            title=title,
            notes=notes,
            file_name=surgery.ot_report_file_name,
            file_data=surgery.ot_report_file_data,
        )
    )
