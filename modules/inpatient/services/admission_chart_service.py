"""Admission-scoped read model assembled from existing IPD source entities."""

from uuid import UUID

from sqlalchemy.orm import Session

from modules.inpatient.actions.admission_actions import to_admission_detail
from modules.inpatient.contracts.chart_contracts import AdmissionChartResponse, BedStayHistoryItem
from modules.inpatient.contracts.nursing_contracts import (
    IpdClinicalNoteResponse,
    IpdVitalSignResponse,
    MedicationAdminResponse,
    NursingCarePlanResponse,
    NursingShiftHandoverResponse,
)
from modules.inpatient.db.admissions_repository import AdmissionsRepository
from modules.inpatient.db.inpatient_vitals_repository import InpatientVitalsRepository
from modules.inpatient.db.nursing_repository import NursingRepository
from modules.inpatient.entities.admission import BedStaySegment
from modules.inpatient.entities.nursing_entities import ClinicalNoteType
from shared.exceptions.base import NotFoundError


class AdmissionChartService:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.admissions = AdmissionsRepository(db)
        self.nursing = NursingRepository(db, hospital_id)
        self.vitals_repo = InpatientVitalsRepository(db, hospital_id)

    def get(self, admission_id: UUID) -> AdmissionChartResponse:
        admission = self.admissions.get_admission_by_id(self.hospital_id, admission_id)
        if not admission:
            raise NotFoundError("Admission not found")
        notes = self.nursing.list_clinical_notes(admission_id)
        segments = (
            self.db.query(BedStaySegment)
            .filter(BedStaySegment.hospital_id == self.hospital_id, BedStaySegment.admission_id == admission_id)
            .order_by(BedStaySegment.started_at.desc())
            .all()
        )
        vitals = self.vitals_repo.list_vitals(admission_id, limit=50)
        io_summary = self.vitals_repo.get_intake_output_summary(admission_id, hours=24)
        latest_vitals_list = (
            [IpdVitalSignResponse.model_validate(vitals[0]).model_dump()]
            if vitals
            else []
        )

        from modules.inpatient.db.care_team_repository import CareTeamRepository
        from modules.inpatient.db.discharge_exception_repository import DischargeExceptionRepository
        from modules.inpatient.db.medication_order_repository import MedicationOrderRepository
        from modules.inpatient.entities.discharge_exception import ExceptionStatus
        from modules.inpatient.services.inpatient_billing_service import InpatientBillingService
        from modules.ot.entities.ot_entities import OtSurgery

        care_team_members = CareTeamRepository(self.db, self.hospital_id).list_care_team(admission_id)
        care_team_list = [
            {
                "id": str(m.id),
                "doctor_id": str(m.doctor_id),
                "doctor_name": m.doctor.name if m.doctor else None,
                "role": m.role.value,
                "is_active": m.is_active,
                "assigned_at": m.assigned_at.isoformat() if m.assigned_at else None,
                "ended_at": m.ended_at.isoformat() if m.ended_at else None,
                "assigned_by_name": m.assigned_by_name,
                "notes": m.notes,
            }
            for m in care_team_members
        ]

        med_orders = MedicationOrderRepository(self.db, self.hospital_id).list_orders(admission_id)
        med_orders_list = [
            {
                "id": str(o.id),
                "medicine_name": o.medicine_name,
                "dose": o.dose,
                "route": o.route,
                "frequency": o.frequency,
                "duration_days": o.duration_days,
                "instructions": o.instructions,
                "ordered_by_name": o.doctor_name,
                "status": o.status.value,
                "ordered_at": o.created_at.isoformat() if o.created_at else None,
                "discontinued_at": o.discontinued_at.isoformat() if o.discontinued_at else None,
                "discontinued_reason": o.discontinued_reason,
            }
            for o in med_orders
        ]

        surgeries = (
            self.db.query(OtSurgery)
            .filter(
                OtSurgery.hospital_id == self.hospital_id,
                (OtSurgery.admission_id == admission_id) | (OtSurgery.patient_id == admission.patient_id),
            )
            .order_by(OtSurgery.scheduled_at.desc())
            .all()
        )
        surgeries_list = [
            {
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
                "assistant_surgeon": s.assistant_surgeon,
                "anaesthetist": s.anaesthetist,
                "shifted_to": s.shifted_to,
                "remarks": s.remarks,
                "pre_op_diagnosis": s.pre_op_diagnosis,
                "procedure_performed": s.procedure_performed,
            }
            for s in surgeries
        ]

        billing_svc = InpatientBillingService(self.db)
        totals = billing_svc.get_ledger_totals(self.hospital_id, admission.patient_id, admission_id=admission.id)
        exc_repo = DischargeExceptionRepository(self.db, self.hospital_id)
        active_exc = exc_repo.get_latest_exception(admission.id)

        net_bal = float(totals.get("net_patient_balance") if "net_patient_balance" in totals else (totals.get("outstanding") or 0))
        has_approved_exc = bool(active_exc and active_exc.status == ExceptionStatus.approved)
        is_cleared = (net_bal <= 0.009) or has_approved_exc

        fin_clearance = {
            "total_charges": totals.get("total_charges", 0.0),
            "total_payments": totals.get("total_payments", 0.0),
            "total_deposits_collected": totals.get("total_deposits_collected", 0.0),
            "deposits_applied": totals.get("deposits_applied", 0.0),
            "deposits_available": totals.get("deposits_available", 0.0),
            "outstanding_balance": totals.get("outstanding", 0.0),
            "net_patient_balance": net_bal,
            "is_cleared": is_cleared,
            "active_exception": {
                "id": str(active_exc.id),
                "status": active_exc.status.value,
                "outstanding_amount_at_request": active_exc.outstanding_amount_at_request,
                "reason": active_exc.reason,
                "requested_by_name": active_exc.requested_by_name,
                "requested_at": active_exc.requested_at.isoformat() if active_exc.requested_at else None,
            } if active_exc else None,
        }

        return AdmissionChartResponse(
            admission=to_admission_detail(admission),
            doctor_notes=[IpdClinicalNoteResponse.model_validate(note) for note in notes if note.note_type == ClinicalNoteType.doctor_progress],
            nursing_notes=[IpdClinicalNoteResponse.model_validate(note) for note in notes if note.note_type != ClinicalNoteType.doctor_progress],
            care_plans=[NursingCarePlanResponse.model_validate(item) for item in self.nursing.list_care_plans(admission_id)],
            handovers=[NursingShiftHandoverResponse.model_validate(item) for item in self.nursing.list_handovers(admission_id)],
            medication_administrations=[MedicationAdminResponse.model_validate(item) for item in self.nursing.list_emar_records(admission_id)],
            bed_history=[BedStayHistoryItem.model_validate(item) for item in segments],
            vitals_history=[IpdVitalSignResponse.model_validate(v) for v in vitals],
            intake_output_summary=io_summary,
            latest_vitals=latest_vitals_list,
            care_team=care_team_list,
            medication_orders=med_orders_list,
            surgeries=surgeries_list,
            financial_clearance=fin_clearance,
        )
