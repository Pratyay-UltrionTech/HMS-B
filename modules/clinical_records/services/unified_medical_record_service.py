"""
Unified Patient Medical Record Query & Aggregation Layer.

Assembles longitudinal patient clinical history into episode-based encounters
(IPD admissions, OPD consultations) without duplicating canonical records into
redundant tables.

Performance-hardened: Uses bounded bulk queries and in-memory aggregation
instead of N+1 per-admission/per-appointment query loops.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, joinedload, selectinload

from modules.appointments.entities.appointment import Appointment
from modules.clinical_records.contracts.unified_record_contracts import (
    ClinicalRecordTimelineItem,
    EpisodeSummary,
    UnifiedPatientMedicalRecordResponse,
)
from modules.clinical_records.entities.clinical_record import MedicalRecord, Prescription
from modules.inpatient.entities.admission import Admission, BedStaySegment
from modules.inpatient.entities.care_team import AdmissionCareTeamMember
from modules.inpatient.entities.medication_order import IpdMedicationOrder
from modules.inpatient.entities.nursing_entities import (
    ClinicalNoteType,
    IpdClinicalNote,
    IpdVitalSign,
    MedicationAdministrationRecord,
    NursingCarePlan,
    NursingShiftHandover,
)
from modules.laboratory.entities.lab_entities import LabOrder
from modules.ot.entities.ot_entities import OtSurgery
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
            .options(
                joinedload(Admission.doctor),
                joinedload(Admission.bed),
                joinedload(Admission.ward),
            )
            .filter(Admission.hospital_id == self.hospital_id, Admission.patient_id == patient_id)
            .order_by(Admission.admitted_at.desc())
            .all()
        )

        admission_ids = [adm.id for adm in admissions]

        # Bulk queries for all child collections of admissions
        care_team_by_adm: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
        bed_stays_by_adm: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
        doctor_notes_by_adm: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
        nursing_notes_by_adm: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
        vitals_by_adm: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
        med_orders_by_adm: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
        emar_by_adm: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
        care_plans_by_adm: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
        handovers_by_adm: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
        surgeries_by_adm: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
        diagnostics_by_adm: dict[UUID, list[dict[str, Any]]] = defaultdict(list)

        if admission_ids:
            # Care Team Members
            care_teams = (
                self.db.query(AdmissionCareTeamMember)
                .options(joinedload(AdmissionCareTeamMember.doctor))
                .filter(
                    AdmissionCareTeamMember.hospital_id == self.hospital_id,
                    AdmissionCareTeamMember.admission_id.in_(admission_ids),
                )
                .order_by(AdmissionCareTeamMember.assigned_at.desc())
                .all()
            )
            for m in care_teams:
                care_team_by_adm[m.admission_id].append({
                    "id": str(m.id),
                    "doctor_id": str(m.doctor_id),
                    "doctor_name": m.doctor.name if m.doctor else None,
                    "role": m.role.value if hasattr(m.role, "value") else str(m.role),
                    "is_active": m.is_active,
                    "assigned_at": m.assigned_at.isoformat() if m.assigned_at else None,
                    "ended_at": m.ended_at.isoformat() if m.ended_at else None,
                    "assigned_by_name": m.assigned_by_name,
                    "notes": m.notes,
                })

            # Bed Stays
            bed_stays = (
                self.db.query(BedStaySegment)
                .options(joinedload(BedStaySegment.bed), joinedload(BedStaySegment.ward))
                .filter(
                    BedStaySegment.hospital_id == self.hospital_id,
                    BedStaySegment.admission_id.in_(admission_ids),
                )
                .order_by(BedStaySegment.started_at.desc())
                .all()
            )
            for s in bed_stays:
                bed_stays_by_adm[s.admission_id].append({
                    "id": str(s.id),
                    "bed_id": str(s.bed_id),
                    "bed_code": s.bed.bed_code if s.bed else None,
                    "ward_name": s.ward.name if s.ward else None,
                    "started_at": s.started_at.isoformat() if s.started_at else None,
                    "ended_at": s.ended_at.isoformat() if s.ended_at else None,
                    "notes": s.notes,
                })

            # Clinical Notes
            clinical_notes = (
                self.db.query(IpdClinicalNote)
                .filter(
                    IpdClinicalNote.hospital_id == self.hospital_id,
                    IpdClinicalNote.admission_id.in_(admission_ids),
                )
                .order_by(IpdClinicalNote.created_at.desc())
                .all()
            )
            for n in clinical_notes:
                note_dict = {
                    "id": str(n.id),
                    "note_type": n.note_type.value if hasattr(n.note_type, "value") else str(n.note_type),
                    "note_text": getattr(n, "note_content", None) or getattr(n, "note_text", None),
                    "created_at": n.created_at.isoformat() if n.created_at else None,
                    "author_name": n.author_name,
                    "author_role": n.author_role.value if hasattr(n.author_role, "value") else str(n.author_role),
                }
                ntype_str = str(n.note_type.value if hasattr(n.note_type, "value") else n.note_type).lower()
                if "doctor" in ntype_str or ntype_str in ("admission", "progress", "consultation"):
                    doctor_notes_by_adm[n.admission_id].append(note_dict)
                else:
                    nursing_notes_by_adm[n.admission_id].append(note_dict)

            # Vitals
            vitals = (
                self.db.query(IpdVitalSign)
                .filter(
                    IpdVitalSign.hospital_id == self.hospital_id,
                    IpdVitalSign.admission_id.in_(admission_ids),
                )
                .order_by(IpdVitalSign.recorded_at.desc())
                .all()
            )
            for v in vitals:
                vitals_by_adm[v.admission_id].append({
                    "id": str(v.id),
                    "recorded_at": v.recorded_at.isoformat() if v.recorded_at else None,
                    "temperature": v.temperature,
                    "pulse": v.pulse,
                    "respiratory_rate": v.respiratory_rate,
                    "bp_systolic": v.bp_systolic,
                    "bp_diastolic": v.bp_diastolic,
                    "spo2": v.spo2,
                    "recorded_by_nurse_name": v.recorded_by_nurse_name,
                    "notes": v.notes,
                })

            # Medication Orders
            med_orders = (
                self.db.query(IpdMedicationOrder)
                .filter(
                    IpdMedicationOrder.hospital_id == self.hospital_id,
                    IpdMedicationOrder.admission_id.in_(admission_ids),
                )
                .order_by(IpdMedicationOrder.created_at.desc())
                .all()
            )
            for o in med_orders:
                med_orders_by_adm[o.admission_id].append({
                    "id": str(o.id),
                    "medicine_name": o.medicine_name,
                    "dose": o.dose,
                    "route": o.route,
                    "frequency": o.frequency,
                    "duration_days": o.duration_days,
                    "instructions": o.instructions,
                    "ordered_by_name": o.doctor_name,
                    "status": o.status.value if hasattr(o.status, "value") else str(o.status),
                    "ordered_at": o.created_at.isoformat() if o.created_at else None,
                    "discontinued_at": o.discontinued_at.isoformat() if o.discontinued_at else None,
                    "discontinued_reason": o.discontinued_reason,
                })

            # Medication Administrations (eMAR)
            emar_records = (
                self.db.query(MedicationAdministrationRecord)
                .filter(
                    MedicationAdministrationRecord.hospital_id == self.hospital_id,
                    MedicationAdministrationRecord.admission_id.in_(admission_ids),
                )
                .order_by(MedicationAdministrationRecord.administered_at.desc())
                .all()
            )
            for item in emar_records:
                emar_by_adm[item.admission_id].append({
                    "id": str(item.id),
                    "medication_order_id": str(item.medication_order_id),
                    "scheduled_time": item.scheduled_time.isoformat() if item.scheduled_time else None,
                    "administered_at": item.administered_at.isoformat() if item.administered_at else None,
                    "administered_by_nurse_name": item.administered_by_nurse_name,
                    "status": item.status.value if hasattr(item.status, "value") else str(item.status),
                    "dose_given": item.dose_given,
                    "route_used": item.route_used,
                    "notes": item.notes,
                })

            # Nursing Care Plans
            care_plans = (
                self.db.query(NursingCarePlan)
                .filter(
                    NursingCarePlan.hospital_id == self.hospital_id,
                    NursingCarePlan.admission_id.in_(admission_ids),
                )
                .order_by(NursingCarePlan.created_at.desc())
                .all()
            )
            for cp in care_plans:
                care_plans_by_adm[cp.admission_id].append({
                    "id": str(cp.id),
                    "nursing_diagnosis": cp.nursing_diagnosis,
                    "goals": cp.goals,
                    "interventions": cp.interventions,
                    "evaluation_frequency": cp.evaluation_frequency,
                    "status": cp.status.value if hasattr(cp.status, "value") else str(cp.status),
                    "created_by_nurse_name": cp.created_by_nurse_name,
                    "created_at": cp.created_at.isoformat() if cp.created_at else None,
                })

            # Nursing Shift Handovers
            handovers = (
                self.db.query(NursingShiftHandover)
                .filter(
                    NursingShiftHandover.hospital_id == self.hospital_id,
                    NursingShiftHandover.admission_id.in_(admission_ids),
                )
                .order_by(NursingShiftHandover.created_at.desc())
                .all()
            )
            for ho in handovers:
                handovers_by_adm[ho.admission_id].append({
                    "id": str(ho.id),
                    "shift_type": ho.shift_type.value if hasattr(ho.shift_type, "value") else str(ho.shift_type),
                    "shift_date": ho.shift_date.isoformat() if ho.shift_date else None,
                    "outgoing_nurse_name": ho.outgoing_nurse_name,
                    "incoming_nurse_name": ho.incoming_nurse_name,
                    "situation": ho.situation,
                    "background": ho.background,
                    "assessment_acuity": ho.assessment_acuity,
                })

            # Surgeries / Procedures
            surgeries = (
                self.db.query(OtSurgery)
                .options(joinedload(OtSurgery.surgeon))
                .filter(
                    OtSurgery.hospital_id == self.hospital_id,
                    OtSurgery.admission_id.in_(admission_ids),
                )
                .order_by(OtSurgery.scheduled_at.desc())
                .all()
            )
            for s in surgeries:
                surgeries_by_adm[s.admission_id].append({
                    "id": str(s.id),
                    "surgery_no": s.surgery_no,
                    "surgery_type": s.surgery_type,
                    "surgery_category": s.surgery_category,
                    "priority": s.priority.value if hasattr(s.priority, "value") else str(s.priority),
                    "ot_room": s.ot_room,
                    "scheduled_at": s.scheduled_at.isoformat() if s.scheduled_at else None,
                    "duration_minutes": s.duration_minutes,
                    "status": s.status.value if hasattr(s.status, "value") else str(s.status),
                    "surgeon_id": str(s.surgeon_id) if s.surgeon_id else None,
                    "surgeon_name": s.surgeon.name if hasattr(s, "surgeon") and s.surgeon else None,
                    "procedure_performed": s.procedure_performed,
                })

            # Inpatient Lab Orders
            adm_labs = (
                self.db.query(LabOrder)
                .options(
                    joinedload(LabOrder.doctor),
                    selectinload(LabOrder.items),
                    selectinload(LabOrder.results),
                )
                .filter(
                    LabOrder.hospital_id == self.hospital_id,
                    LabOrder.admission_id.in_(admission_ids),
                )
                .order_by(LabOrder.ordered_at.desc())
                .all()
            )
            for lab in adm_labs:
                bound_lab_ids.add(lab.id)
                diagnostics_by_adm[lab.admission_id].append({
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

            # Inpatient Radiology Orders
            adm_rads = (
                self.db.query(RadiologyOrder)
                .options(joinedload(RadiologyOrder.doctor))
                .filter(
                    RadiologyOrder.hospital_id == self.hospital_id,
                    RadiologyOrder.admission_id.in_(admission_ids),
                )
                .order_by(RadiologyOrder.ordered_at.desc())
                .all()
            )
            for rad in adm_rads:
                bound_rad_ids.add(rad.id)
                diagnostics_by_adm[rad.admission_id].append({
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

        for adm in admissions:
            start_dt_str = adm.admitted_at.isoformat() if adm.admitted_at else datetime.now(timezone.utc).isoformat()
            doc_name = adm.doctor.name if adm.doctor else None
            dept_name = adm.department_name if hasattr(adm, "department_name") else None
            bed_str = adm.bed.bed_code if adm.bed else None
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
                        "care_team": care_team_by_adm.get(adm.id, []),
                        "bed_stays": bed_stays_by_adm.get(adm.id, []),
                    },
                    clinical_notes=doctor_notes_by_adm.get(adm.id, []),
                    nursing_notes=nursing_notes_by_adm.get(adm.id, []),
                    care_plans=care_plans_by_adm.get(adm.id, []),
                    shift_handovers=handovers_by_adm.get(adm.id, []),
                    vitals=vitals_by_adm.get(adm.id, []),
                    medications=med_orders_by_adm.get(adm.id, []),
                    emar=emar_by_adm.get(adm.id, []),
                    diagnostics=diagnostics_by_adm.get(adm.id, []),
                    procedures=surgeries_by_adm.get(adm.id, []),
                    forms_and_documents=[],
                    discharge_summary=None,
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

        appointment_ids = [appt.id for appt in appointments]
        rx_by_appt: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
        opd_diag_by_appt: dict[UUID, list[dict[str, Any]]] = defaultdict(list)

        if appointment_ids:
            # Prescriptions
            all_rxs = (
                self.db.query(Prescription)
                .filter(
                    Prescription.hospital_id == self.hospital_id,
                    Prescription.appointment_id.in_(appointment_ids),
                )
                .order_by(Prescription.created_at.desc())
                .all()
            )
            for rx in all_rxs:
                bound_rx_ids.add(rx.id)
                rx_by_appt[rx.appointment_id].append({
                    "id": str(rx.id),
                    "medicines": rx.medicines,
                    "dosage": rx.dosage,
                    "advice": rx.advice,
                    "diagnosis": rx.diagnosis,
                    "symptoms": rx.symptoms,
                    "status": rx.status,
                    "created_at": rx.created_at.isoformat() if rx.created_at else None,
                })

            # OPD Lab Orders
            appt_labs = (
                self.db.query(LabOrder)
                .options(
                    joinedload(LabOrder.doctor),
                    selectinload(LabOrder.items),
                    selectinload(LabOrder.results),
                )
                .filter(
                    LabOrder.hospital_id == self.hospital_id,
                    LabOrder.appointment_id.in_(appointment_ids),
                )
                .order_by(LabOrder.ordered_at.desc())
                .all()
            )
            for lab in appt_labs:
                bound_lab_ids.add(lab.id)
                opd_diag_by_appt[lab.appointment_id].append({
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

            # OPD Radiology Orders
            appt_rads = (
                self.db.query(RadiologyOrder)
                .filter(
                    RadiologyOrder.hospital_id == self.hospital_id,
                    RadiologyOrder.appointment_id.in_(appointment_ids),
                )
                .order_by(RadiologyOrder.ordered_at.desc())
                .all()
            )
            for rad in appt_rads:
                bound_rad_ids.add(rad.id)
                opd_diag_by_appt[rad.appointment_id].append({
                    "id": str(rad.id),
                    "type": "radiology",
                    "order_no": rad.order_no,
                    "status": rad.status.value if hasattr(rad.status, "value") else str(rad.status),
                    "ordered_at": rad.ordered_at.isoformat() if rad.ordered_at else None,
                    "scan_name": rad.scan_name,
                    "findings": rad.findings,
                    "impression": rad.impression,
                })

        for appt in appointments:
            rx_list = rx_by_appt.get(appt.id, [])
            opd_diagnostics = opd_diag_by_appt.get(appt.id, [])

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
            .options(joinedload(LabOrder.doctor), selectinload(LabOrder.items))
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
            .options(joinedload(RadiologyOrder.doctor))
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
            .options(joinedload(Prescription.doctor))
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
            .options(joinedload(MedicalRecord.doctor))
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
