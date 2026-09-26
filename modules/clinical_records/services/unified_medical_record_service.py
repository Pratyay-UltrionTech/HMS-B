"""
Unified Patient Medical Record Query & Aggregation Layer.

Assembles longitudinal patient clinical history into episode-based encounters
(IPD admissions, OPD consultations) without duplicating canonical records into
redundant tables.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, joinedload

from modules.appointments.entities.appointment import Appointment
from modules.clinical_records.contracts.unified_record_contracts import (
    ClinicalRecordTimelineItem,
    EpisodeSummary,
    UnifiedPatientMedicalRecordResponse,
)
from modules.clinical_records.entities.clinical_record import MedicalRecord, Prescription
from modules.inpatient.entities.admission import Admission
from modules.inpatient.services.admission_chart_service import AdmissionChartService
from modules.laboratory.entities.lab_entities import LabOrder
from modules.patients.entities.patient import Patient
from modules.radiology.entities.radiology_entities import RadiologyOrder
from shared.exceptions.base import NotFoundError


class UnifiedMedicalRecordService:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    def get_patient_medical_record(self, patient_id: UUID) -> UnifiedPatientMedicalRecordResponse:
        patient = (
            self.db.query(Patient)
            .filter(Patient.id == patient_id, Patient.hospital_id == self.hospital_id)
            .first()
        )
        if not patient:
            raise NotFoundError("Patient not found")

        episodes: list[EpisodeSummary] = []
        bound_lab_ids: set[UUID] = set()
        bound_rad_ids: set[UUID] = set()
        bound_rx_ids: set[UUID] = set()
        bound_med_rec_ids: set[UUID] = set()

        # ── 1. Inpatient Admissions (IPD Episodes) ───────────────────────────
        admissions = (
            self.db.query(Admission)
            .filter(Admission.hospital_id == self.hospital_id, Admission.patient_id == patient_id)
            .order_by(Admission.admitted_at.desc())
            .all()
        )

        chart_svc = AdmissionChartService(self.db, self.hospital_id)
        for adm in admissions:
            try:
                chart = chart_svc.get(adm.id)
                chart_data = chart.model_dump(mode="json")
            except Exception:
                chart_data = {}

            # Collect diagnostics linked to this admission
            adm_labs = (
                self.db.query(LabOrder)
                .filter(LabOrder.hospital_id == self.hospital_id, LabOrder.admission_id == adm.id)
                .order_by(LabOrder.ordered_at.desc())
                .all()
            )
            diagnostics: list[dict[str, Any]] = []
            for lab in adm_labs:
                bound_lab_ids.add(lab.id)
                diagnostics.append({
                    "id": str(lab.id),
                    "type": "laboratory",
                    "order_no": lab.order_no,
                    "status": lab.status.value if hasattr(lab.status, "value") else str(lab.status),
                    "ordered_at": lab.ordered_at.isoformat() if lab.ordered_at else None,
                    "doctor_name": lab.doctor.name if lab.doctor else None,
                    "items": [
                        {
                            "test_name": it.test_name,
                            "test_code": it.test_code,
                            "status": it.status.value if hasattr(it.status, "value") else str(it.status),
                        }
                        for it in (lab.items or [])
                    ],
                    "results": [
                        {
                            "parameter_name": r.parameter_name,
                            "result_value": r.result_value,
                            "unit": r.unit,
                            "reference_range": r.reference_range,
                            "is_panic": r.is_panic,
                        }
                        for r in (lab.results or [])
                    ],
                })

            adm_rads = (
                self.db.query(RadiologyOrder)
                .filter(RadiologyOrder.hospital_id == self.hospital_id, RadiologyOrder.admission_id == adm.id)
                .order_by(RadiologyOrder.ordered_at.desc())
                .all()
            )
            for rad in adm_rads:
                bound_rad_ids.add(rad.id)
                diagnostics.append({
                    "id": str(rad.id),
                    "type": "radiology",
                    "order_no": rad.order_no,
                    "status": rad.status.value if hasattr(rad.status, "value") else str(rad.status),
                    "ordered_at": rad.ordered_at.isoformat() if rad.ordered_at else None,
                    "scan_name": rad.scan_name,
                    "scan_code": rad.scan_code,
                    "findings": rad.findings,
                    "impression": rad.impression,
                    "has_report_file": bool(rad.report_file_name),
                    "has_image_file": bool(rad.image_file_name),
                })

            # Forms and Documents with deduplication (Requirement 19)
            forms_docs = chart_data.get("forms") or []

            # Clinical notes split
            all_notes = chart_data.get("clinical_notes") or []
            doctor_notes = [n for n in all_notes if n.get("note_type") in ("admission", "progress", "consultation", "doctor")]
            nursing_notes = [n for n in all_notes if n.get("note_type") not in ("admission", "progress", "consultation", "doctor")]

            start_dt_str = adm.admitted_at.isoformat() if adm.admitted_at else datetime.now(timezone.utc).isoformat()
            doc_name = adm.doctor.name if adm.doctor else None
            dept_name = adm.department_name if hasattr(adm, "department_name") else None
            bed_str = (adm.bed.bed_code if adm.bed else None) or chart_data.get("admission", {}).get("bed_code")
            ward_str = adm.ward.name if adm.ward else getattr(adm, "ward_name", None)

            episodes.append(
                EpisodeSummary(
                    episode_id=adm.id,
                    encounter_type="ipd",
                    identifier=adm.ip_id or f"IP-{str(adm.id)[:8]}",
                    start_date=start_dt_str,
                    end_date=adm.discharged_at.isoformat() if adm.discharged_at else None,
                    status=adm.status.value if hasattr(adm.status, "value") else str(adm.status),
                    attending_doctor_name=doc_name,
                    department_name=dept_name,
                    diagnosis=adm.notes,
                    room_or_bed=bed_str,
                    overview={
                        "admission_id": str(adm.id),
                        "ip_id": adm.ip_id,
                        "status": adm.status.value if hasattr(adm.status, "value") else str(adm.status),
                        "admission_date": start_dt_str,
                        "discharged_at": adm.discharged_at.isoformat() if adm.discharged_at else None,
                        "admitting_doctor": doc_name,
                        "department": dept_name,
                        "ward": ward_str,
                        "bed": bed_str,
                        "care_team": chart_data.get("care_team") or [],
                        "bed_stays": chart_data.get("bed_stay_history") or [],
                    },
                    clinical_notes=doctor_notes,
                    nursing_notes=nursing_notes,
                    care_plans=chart_data.get("care_plans") or [],
                    shift_handovers=chart_data.get("shift_handovers") or [],
                    vitals=chart_data.get("vitals") or [],
                    medications=chart_data.get("medication_orders") or [],
                    emar=chart_data.get("medication_administrations") or [],
                    diagnostics=diagnostics,
                    procedures=chart_data.get("surgeries") or [],
                    forms_and_documents=forms_docs,
                    discharge_summary=chart_data.get("discharge_summary"),
                )
            )

        # ── 2. Outpatient Consultations (OPD Episodes) ───────────────────────
        appointments = (
            self.db.query(Appointment)
            .options(joinedload(Appointment.doctor))
            .filter(Appointment.hospital_id == self.hospital_id, Appointment.patient_id == patient_id)
            .order_by(Appointment.appointment_date.desc(), Appointment.appointment_time.desc())
            .all()
        )

        for appt in appointments:
            appt_rxs = (
                self.db.query(Prescription)
                .filter(Prescription.hospital_id == self.hospital_id, Prescription.appointment_id == appt.id)
                .order_by(Prescription.created_at.desc())
                .all()
            )
            rx_list: list[dict[str, Any]] = []
            for rx in appt_rxs:
                bound_rx_ids.add(rx.id)
                rx_list.append({
                    "id": str(rx.id),
                    "medicines": rx.medicines,
                    "dosage": rx.dosage,
                    "advice": rx.advice,
                    "diagnosis": rx.diagnosis,
                    "symptoms": rx.symptoms,
                    "status": rx.status,
                    "created_at": rx.created_at.isoformat() if rx.created_at else None,
                })

            appt_labs = (
                self.db.query(LabOrder)
                .filter(LabOrder.hospital_id == self.hospital_id, LabOrder.appointment_id == appt.id)
                .order_by(LabOrder.ordered_at.desc())
                .all()
            )
            opd_diagnostics: list[dict[str, Any]] = []
            for lab in appt_labs:
                bound_lab_ids.add(lab.id)
                opd_diagnostics.append({
                    "id": str(lab.id),
                    "type": "laboratory",
                    "order_no": lab.order_no,
                    "status": lab.status.value if hasattr(lab.status, "value") else str(lab.status),
                    "ordered_at": lab.ordered_at.isoformat() if lab.ordered_at else None,
                    "items": [it.test_name for it in (lab.items or [])],
                    "results": [
                        {
                            "parameter": r.parameter_name,
                            "value": r.result_value,
                            "unit": r.unit,
                        }
                        for r in (lab.results or [])
                    ],
                })

            appt_rads = (
                self.db.query(RadiologyOrder)
                .filter(RadiologyOrder.hospital_id == self.hospital_id, RadiologyOrder.appointment_id == appt.id)
                .order_by(RadiologyOrder.ordered_at.desc())
                .all()
            )
            for rad in appt_rads:
                bound_rad_ids.add(rad.id)
                opd_diagnostics.append({
                    "id": str(rad.id),
                    "type": "radiology",
                    "order_no": rad.order_no,
                    "status": rad.status.value if hasattr(rad.status, "value") else str(rad.status),
                    "ordered_at": rad.ordered_at.isoformat() if rad.ordered_at else None,
                    "scan_name": rad.scan_name,
                    "findings": rad.findings,
                    "impression": rad.impression,
                })

            start_dt = f"{appt.appointment_date.isoformat()}T{appt.appointment_time.isoformat() if appt.appointment_time else '00:00:00'}"
            diag_str = ", ".join(r["diagnosis"] for r in rx_list if r.get("diagnosis")) or getattr(appt, "purpose", None) or getattr(appt, "reason", None)

            episodes.append(
                EpisodeSummary(
                    episode_id=appt.id,
                    encounter_type="opd",
                    identifier=appt.op_id or f"OP-{str(appt.id)[:8]}",
                    start_date=start_dt,
                    end_date=start_dt,
                    status=appt.status.value if hasattr(appt.status, "value") else str(appt.status),
                    attending_doctor_name=appt.doctor.name if appt.doctor else None,
                    department_name=getattr(appt.doctor, "specialization", None) if appt.doctor else None,
                    diagnosis=diag_str,
                    overview={
                        "appointment_id": str(appt.id),
                        "op_id": appt.op_id,
                        "type": str(getattr(appt, "appointment_type_id", "") or "consultation"),
                        "visit_type": getattr(appt, "visit_type", "first_visit"),
                        "reason": getattr(appt, "purpose", None) or getattr(appt, "notes", None),
                        "notes": appt.notes,
                    },
                    clinical_notes=[
                        {
                            "id": str(appt.id),
                            "note_type": "opd_consultation",
                            "note_text": appt.notes or appt.reason or "OPD Consultation",
                            "created_at": start_dt,
                            "doctor_name": appt.doctor.name if appt.doctor else None,
                        }
                    ] if appt.notes else [],
                    medications=rx_list,
                    diagnostics=opd_diagnostics,
                )
            )

        # Sort all episodes chronologically descending
        episodes.sort(key=lambda ep: str(ep.start_date), reverse=True)

        # ── 3. Standalone Records (Unlinked to an admission or appointment) ─
        standalone: list[ClinicalRecordTimelineItem] = []

        unbound_labs = (
            self.db.query(LabOrder)
            .filter(
                LabOrder.hospital_id == self.hospital_id,
                LabOrder.patient_id == patient_id,
                LabOrder.id.notin_(bound_lab_ids) if bound_lab_ids else True,
            )
            .all()
        )
        for lab in unbound_labs:
            standalone.append(
                ClinicalRecordTimelineItem(
                    id=lab.id,
                    source_type="lab",
                    title=f"Lab Order {lab.order_no}",
                    occurred_at=lab.ordered_at,
                    author_name=lab.doctor.name if lab.doctor else lab.ordered_by_name,
                    author_role="doctor" if lab.doctor else "lab_tech",
                    status=lab.status.value if hasattr(lab.status, "value") else str(lab.status),
                    summary=", ".join(it.test_name for it in (lab.items or [])),
                    is_finalized=lab.status.value == "completed" if hasattr(lab.status, "value") else lab.status == "completed",
                )
            )

        unbound_rads = (
            self.db.query(RadiologyOrder)
            .filter(
                RadiologyOrder.hospital_id == self.hospital_id,
                RadiologyOrder.patient_id == patient_id,
                RadiologyOrder.id.notin_(bound_rad_ids) if bound_rad_ids else True,
            )
            .all()
        )
        for rad in unbound_rads:
            standalone.append(
                ClinicalRecordTimelineItem(
                    id=rad.id,
                    source_type="radiology",
                    title=f"Radiology {rad.scan_name}",
                    occurred_at=rad.ordered_at,
                    author_name=rad.doctor.name if rad.doctor else rad.ordered_by_name,
                    author_role="doctor" if rad.doctor else "radiologist",
                    status=rad.status.value if hasattr(rad.status, "value") else str(rad.status),
                    summary=rad.impression or rad.findings,
                    is_finalized=rad.status.value == "completed" if hasattr(rad.status, "value") else rad.status == "completed",
                )
            )

        unbound_rxs = (
            self.db.query(Prescription)
            .filter(
                Prescription.hospital_id == self.hospital_id,
                Prescription.patient_id == patient_id,
                Prescription.id.notin_(bound_rx_ids) if bound_rx_ids else True,
            )
            .all()
        )
        for rx in unbound_rxs:
            standalone.append(
                ClinicalRecordTimelineItem(
                    id=rx.id,
                    source_type="medication",
                    title="Prescription",
                    occurred_at=rx.created_at,
                    author_name=rx.doctor.name if rx.doctor else None,
                    author_role="doctor",
                    status=rx.status,
                    summary=f"{rx.medicines} — {rx.dosage}",
                    is_finalized=rx.status != "draft",
                )
            )

        # External / Uploaded Medical Records (deduplicating mirrored IPD forms per Requirement 19)
        med_records = (
            self.db.query(MedicalRecord)
            .filter(MedicalRecord.hospital_id == self.hospital_id, MedicalRecord.patient_id == patient_id)
            .order_by(MedicalRecord.created_at.desc())
            .all()
        )
        for mr in med_records:
            # Suppress if mirrored from IPD form
            if mr.report_type == "IPD Form" or (mr.notes and "IPD form" in mr.notes):
                continue
            standalone.append(
                ClinicalRecordTimelineItem(
                    id=mr.id,
                    source_type="external_record",
                    title=mr.title,
                    occurred_at=mr.created_at,
                    author_name=mr.doctor.name if mr.doctor else None,
                    author_role="doctor",
                    status="finalized",
                    summary=mr.notes,
                    is_finalized=True,
                    metadata={"has_file": bool(mr.file_name), "file_name": mr.file_name},
                )
            )

        standalone.sort(key=lambda item: item.occurred_at, reverse=True)

        return UnifiedPatientMedicalRecordResponse(
            patient_id=patient.id,
            uhid=patient.uhid,
            name=patient.name,
            gender=patient.gender.value if hasattr(patient.gender, "value") else str(patient.gender) if patient.gender else None,
            date_of_birth=patient.date_of_birth,
            blood_group=patient.blood_group,
            phone=getattr(patient, "mobile", None) or getattr(patient, "phone", None),
            episodes=episodes,
            standalone_records=standalone,
        )
