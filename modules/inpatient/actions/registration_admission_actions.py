"""
Registration Inpatient admission and discharge actions.

Separated from admission_actions.py to conform to the UltrionTech-Backend-Template
rule that every file must remain strictly under 500 lines.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from shared.exceptions.base import NotFoundError, ValidationError
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from modules.beds.db.beds_repository import BedsRepository
from modules.doctors.entities.doctor import HospitalUser
from modules.inpatient.contracts.inpatient_contracts import (
    AdmissionSummary,
    AdmitPatientRequest,
    DischargeResponse,
)
from modules.inpatient.db.admissions_repository import AdmissionsRepository
from modules.inpatient.entities.admission import Admission, AdmissionStatus
from modules.inpatient.services.inpatient_billing_service import (
    InpatientBillingService,
    next_ip_encounter_id,
)
from modules.patients.entities.patient import Patient, PatientStatus
from shared.audit.service import write_audit_log


class RegistrationAdmitAction:
    """Action for /api/registration/patients/{patient_id}/admit endpoint."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.admissions_repo = AdmissionsRepository(db)
        self.beds_repo = BedsRepository(db)
        self.billing_svc = InpatientBillingService(db)

    def execute(
        self,
        hospital_id: UUID,
        patient_id: UUID,
        payload: AdmitPatientRequest,
        actor: dict[str, Any],
    ) -> AdmissionSummary:
        patient = (
            self.db.query(Patient)
            .filter(Patient.id == patient_id, Patient.hospital_id == hospital_id)
            .first()
        )
        if not patient:
            raise NotFoundError("Patient not found")

        from modules.inpatient.utils.admission_conflicts import (
            admission_conflict as _reg_conflict,
            bed_conflict as _reg_bed_conflict,
        )

        # Locked open-episode check (Invariant 1: requested/admitted/
        # discharge_requested all block a second episode).
        active = self.admissions_repo.get_open_admission(
            hospital_id, patient_id, for_update=True
        )
        if active:
            raise _reg_conflict(
                f"Patient already has an {active.status.value} admission episode",
                admission_id=active.id,
                admission_status=active.status,
                bed_id=active.bed_id,
                doctor_id=active.doctor_id,
            )

        from modules.beds.entities.bed import Bed
        bed = (
            self.db.query(Bed)
            .filter(Bed.id == payload.bed_id, Bed.hospital_id == hospital_id)
            .with_for_update()
            .first()
        )
        if not bed:
            raise NotFoundError("Bed not found")
        if bed.is_occupied:
            raise _reg_bed_conflict("Bed is already occupied", bed_id=bed.id)
        if bed.ward_id != payload.ward_id or bed.room_id != payload.room_id:
            raise ValidationError("Ward/Room does not match selected bed")

        if payload.doctor_id:
            doc = (
                self.db.query(HospitalUser)
                .filter(HospitalUser.id == payload.doctor_id, HospitalUser.hospital_id == hospital_id)
                .first()
            )
            if not doc:
                raise NotFoundError("Doctor not found")

        ip_id = next_ip_encounter_id(self.db, hospital_id)
        admission = self.admissions_repo.create_admission(
            hospital_id=hospital_id,
            patient=patient,
            bed=bed,
            ward_id=payload.ward_id,
            room_id=payload.room_id,
            doctor_id=payload.doctor_id,
            ip_id=ip_id,
            notes=payload.notes,
        )

        actor_name = str(actor.get("name") or "System")
        self.billing_svc.ensure_financial_account(
            hospital_id=hospital_id,
            patient_id=patient.id,
            admission_id=admission.id,
            created_by_name=actor_name,
        )
        self.billing_svc.ensure_admission_charge(
            hospital_id=hospital_id,
            patient_id=patient.id,
            admission_id=admission.id,
            ward_name=bed.ward.name if bed.ward else None,
            admission_fee=float(getattr(bed.ward, "admission_fee", 0) or 0) if bed.ward else 0.0,
            created_by_name=actor_name,
        )

        if payload.doctor_id:
            from modules.inpatient.db.care_team_repository import CareTeamRepository
            from modules.inpatient.entities.care_team import AdmissionCareTeamRole
            actor_id_str = actor.get("id") or actor.get("sub")
            actor_id = None
            if actor_id_str:
                try:
                    actor_id = UUID(str(actor_id_str))
                except (ValueError, TypeError):
                    actor_id = None
            ct_repo = CareTeamRepository(self.db, hospital_id)
            ct_repo.add_member(
                admission_id=admission.id,
                doctor_id=payload.doctor_id,
                role=AdmissionCareTeamRole.admitting_doctor,
                assigned_by_id=actor_id,
                assigned_by_name=actor_name,
                notes="Admitting doctor preserved at registration admission",
            )
            ct_repo.add_member(
                admission_id=admission.id,
                doctor_id=payload.doctor_id,
                role=AdmissionCareTeamRole.primary_consultant,
                assigned_by_id=actor_id,
                assigned_by_name=actor_name,
                notes="Primary consultant assigned at registration admission",
            )

        from modules.inpatient.entities.admission import BedStaySegment
        initial_segment = BedStaySegment(
            hospital_id=hospital_id,
            admission_id=admission.id,
            ward_id=bed.ward_id,
            room_id=bed.room_id,
            bed_id=bed.id,
            rate_per_day=float(getattr(bed.ward, "bed_charge_per_day", 0) or 0) if bed.ward else 0.0,
            started_at=admission.admitted_at or datetime.now(timezone.utc),
            ended_at=None,
        )
        self.db.add(initial_segment)

        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="create",
            entity_type="admission",
            entity_id=str(admission.id),
            summary=f"Admitted {patient.uhid} {patient.name} ({admission.ip_id})",
        )
        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="update",
            entity_type="bed",
            entity_id=str(bed.id),
            summary=f"Bed {bed.bed_code} occupied by admission {admission.id}",
        )
        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="update",
            entity_type="patient",
            entity_id=str(patient.id),
            summary="Patient admitted via registration",
        )
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise _reg_conflict(
                "Patient already admitted or bed just taken",
                bed_id=payload.bed_id,
                doctor_id=payload.doctor_id,
            ) from exc
        refreshed = self.admissions_repo.get_admission_by_id(hospital_id, admission.id)
        adm = refreshed or admission
        return AdmissionSummary(
            id=adm.id,
            ward_id=adm.ward_id,
            room_id=adm.room_id,
            bed_id=adm.bed_id,
            ward_name=adm.ward.name if adm.ward else None,
            room_code=adm.room.room_code if adm.room else None,
            bed_code=adm.bed.bed_code if adm.bed else None,
            doctor_name=adm.doctor.name if adm.doctor else None,
            status=adm.status,
            admitted_at=adm.admitted_at,
            discharged_at=adm.discharged_at,
            notes=adm.notes,
            ip_id=getattr(adm, "ip_id", None),
        )


class RegistrationDischargeAction:
    """Action for /api/registration/admissions/{admission_id}/discharge endpoint."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.admissions_repo = AdmissionsRepository(db)
        self.billing_svc = InpatientBillingService(db)

    def execute(
        self,
        hospital_id: UUID,
        admission_id: UUID,
        actor: dict[str, Any],
    ) -> DischargeResponse:
        admission = (
            self.db.query(Admission)
            .filter(
                Admission.id == admission_id,
                Admission.hospital_id == hospital_id,
            )
            .with_for_update()
            .first()
        )
        if not admission:
            raise NotFoundError("Admission not found")
        if admission.status != AdmissionStatus.discharge_requested:
            if admission.status == AdmissionStatus.discharged:
                raise ValidationError("Admission is already discharged")
            if admission.status == AdmissionStatus.cancelled:
                raise ValidationError("Cannot discharge a cancelled admission")
            if admission.status == AdmissionStatus.requested:
                raise ValidationError(
                    "Admission has not been accepted yet — accept it before discharge"
                )
            raise ValidationError(
                f"Cannot discharge admission in '{admission.status.value}' state: discharge must be requested first"
            )

        now = datetime.now(timezone.utc)
        ward = admission.ward
        self.billing_svc.ensure_bed_charge(
            hospital_id=hospital_id,
            patient_id=admission.patient_id,
            admission_id=admission.id,
            admitted_at=admission.admitted_at,
            discharged_at=now,
            ward_name=ward.name if ward else None,
            room_code=admission.room.room_code if admission.room else None,
            bed_code=admission.bed.bed_code if admission.bed else None,
            bed_charge_per_day=float(getattr(ward, "bed_charge_per_day", 0) or 0) if ward else 0.0,
        )
        # Discharge Prescription Gate
        if not getattr(admission, "no_discharge_meds", False):
            from modules.clinical_records.entities.clinical_record import Prescription
            has_rx = (
                self.db.query(Prescription)
                .filter(
                    Prescription.hospital_id == hospital_id,
                    Prescription.admission_id == admission.id,
                    Prescription.status.in_(["issued", "dispensed", "completed"]),
                )
                .first()
            )
            if not has_rx:
                raise ValidationError(
                    "Discharge prescription required: Please prescribe discharge medications or record 'No discharge medicines required' before discharge."
                )

        # Reconcile unallocated payments & draw down available deposits before financial clearance check
        from modules.billing.entities.billing_entities import (
            BillingCharge,
            BillingChargeStatus,
            BillingDeposit,
            BillingInvoice,
            BillingInvoiceStatus,
            BillingPayment,
            BillingPaymentAllocation,
            DepositStatus,
            FinancialAccount,
        )
        from modules.billing.services.billing_service import (
            draw_down_deposit_for_charges,
            allocate_payment_to_charges,
        )
        from sqlalchemy import or_

        acc = (
            self.db.query(FinancialAccount)
            .filter(
                FinancialAccount.hospital_id == hospital_id,
                FinancialAccount.admission_id == admission.id,
            )
            .first()
        )
        if acc:
            # Final Billing Gate: An admission-scoped final generated invoice must exist
            latest_invoice = (
                self.db.query(BillingInvoice)
                .filter(
                    BillingInvoice.hospital_id == hospital_id,
                    BillingInvoice.account_id == acc.id,
                    BillingInvoice.status.in_([BillingInvoiceStatus.generated, BillingInvoiceStatus.paid]),
                )
                .order_by(BillingInvoice.created_at.desc())
                .first()
            )
            if not latest_invoice:
                raise ValidationError(
                    "Final billing required: A generated final invoice must exist for the admission account before discharge."
                )

            # Avoid Stale Final Bills: Check if any charges were posted after invoice generation
            stale_charges = (
                self.db.query(BillingCharge)
                .filter(
                    BillingCharge.hospital_id == hospital_id,
                    BillingCharge.account_id == acc.id,
                    BillingCharge.created_at > latest_invoice.created_at,
                )
                .all()
            )
            if stale_charges:
                raise ValidationError(
                    "Final bill out of date: New billable charges were added after the final invoice was generated. Please regenerate the final bill before discharge."
                )

            acc_payments = (
                self.db.query(BillingPayment)
                .filter(
                    BillingPayment.hospital_id == hospital_id,
                    BillingPayment.account_id == acc.id,
                )
                .all()
            )
            for pay in acc_payments:
                alloc_sum = sum(
                    float(a.allocated_amount or 0)
                    for a in self.db.query(BillingPaymentAllocation).filter(BillingPaymentAllocation.payment_id == pay.id).all()
                )
                unalloc_tender = round(float(pay.amount or 0) - alloc_sum, 2)
                if unalloc_tender > 0:
                    allocate_payment_to_charges(
                        self.db,
                        hospital_id,
                        admission.patient_id,
                        unalloc_tender,
                        payment_id=pay.id,
                        created_by_name=actor.get("name") or "Registration Discharge",
                        account_id=acc.id,
                    )

            available_deposits = (
                self.db.query(BillingDeposit)
                .filter(
                    BillingDeposit.hospital_id == hospital_id,
                    or_(
                        BillingDeposit.admission_id == admission.id,
                        BillingDeposit.account_id == acc.id,
                    ),
                    BillingDeposit.status.in_([DepositStatus.available, DepositStatus.partially_allocated]),
                    BillingDeposit.available_amount > 0,
                )
                .order_by(BillingDeposit.created_at.asc())
                .with_for_update()
                .all()
            )
            for dep in available_deposits:
                open_charges_due = sum(
                    max(0.0, float(c.net_amount or 0) - float(c.amount_paid or 0))
                    for c in self.db.query(BillingCharge)
                    .filter(
                        BillingCharge.hospital_id == hospital_id,
                        BillingCharge.account_id == acc.id,
                        BillingCharge.status.in_([BillingChargeStatus.pending, BillingChargeStatus.partially_paid]),
                    )
                    .all()
                )
                if open_charges_due <= 0.009:
                    break
                draw_down_deposit_for_charges(
                    self.db,
                    hospital_id=hospital_id,
                    deposit_id=dep.id,
                    max_amount=open_charges_due,
                    created_by_name=actor.get("name") or "Registration Discharge",
                )

        fin = self.billing_svc.get_ledger_totals(hospital_id, admission.patient_id, admission_id=admission.id)
        outstanding = float(fin.get("outstanding") or 0)
        if outstanding > 0.009:
            from modules.inpatient.db.discharge_exception_repository import DischargeExceptionRepository
            from modules.inpatient.entities.discharge_exception import ExceptionStatus
            exc_repo = DischargeExceptionRepository(self.db, hospital_id)
            active_exc = exc_repo.get_active_exception(hospital_id, admission.id)
            if not (active_exc and active_exc.status == ExceptionStatus.approved):
                raise ValidationError(
                    f"Cannot discharge — outstanding net balance ₹{outstanding:,.2f}. Clear all dues or obtain an approved financial exception before discharging."
                )

        admission.status = AdmissionStatus.discharged
        admission.discharged_at = now
        from modules.beds.entities.bed import Bed as _Bed

        if admission.bed_id:
            locked_bed = (
                self.db.query(_Bed).filter(_Bed.id == admission.bed_id).with_for_update().first()
            )
            if locked_bed:
                locked_bed.is_occupied = False
        elif admission.bed:
            admission.bed.is_occupied = False
        if admission.patient:
            admission.patient.status = PatientStatus.active

        # Close any open stay segment at discharge (Invariant 7); keep the
        # billing ledger gate above unchanged.
        from modules.inpatient.entities.admission import BedStaySegment as _Seg

        _open_seg = (
            self.db.query(_Seg)
            .filter(
                _Seg.hospital_id == hospital_id,
                _Seg.admission_id == admission.id,
                _Seg.ended_at.is_(None),
            )
            .with_for_update()
            .first()
        )
        if _open_seg:
            _open_seg.ended_at = now

        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="update",
            entity_type="admission",
            entity_id=str(admission.id),
            summary=f"Discharged patient admission {admission_id}",
        )
        if admission.bed_id:
            write_audit_log(
                self.db,
                hospital_id=hospital_id,
                actor=actor,
                action="update",
                entity_type="bed",
                entity_id=str(admission.bed_id),
                summary=f"Bed freed by registration discharge of admission {admission.id}",
            )
        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="update",
            entity_type="patient",
            entity_id=str(admission.patient_id),
            summary="Patient discharged via registration",
        )
        self.db.commit()
        return DischargeResponse(
            id=admission.id,
            status=admission.status,
            discharged_at=admission.discharged_at,
        )
