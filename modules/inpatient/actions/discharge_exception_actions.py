"""Actions for Discharge Financial Exceptions."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from modules.inpatient.contracts.inpatient_contracts import (
    DischargeFinancialExceptionCreate,
    DischargeFinancialExceptionDecision,
    DischargeFinancialExceptionResponse,
)
from modules.inpatient.db.admissions_repository import AdmissionsRepository
from modules.inpatient.db.discharge_exception_repository import DischargeExceptionRepository
from modules.inpatient.entities.admission import AdmissionStatus
from modules.inpatient.entities.discharge_exception import DischargeFinancialException, ExceptionStatus
from modules.inpatient.services.inpatient_billing_service import InpatientBillingService
from shared.audit.service import write_audit_log
from shared.exceptions.base import NotFoundError, ValidationError


def _to_exception_response(exc: DischargeFinancialException) -> DischargeFinancialExceptionResponse:
    pat = getattr(exc, "patient", None)
    return DischargeFinancialExceptionResponse(
        id=exc.id,
        hospital_id=exc.hospital_id,
        admission_id=exc.admission_id,
        financial_account_id=exc.financial_account_id,
        patient_id=exc.patient_id,
        patient_name=getattr(pat, "name", None),
        patient_uhid=getattr(pat, "uhid", None),
        outstanding_amount_at_request=exc.outstanding_amount_at_request,
        reason=exc.reason,
        recommendation=exc.recommendation,
        requested_by_id=exc.requested_by_id,
        requested_by_name=exc.requested_by_name,
        requested_at=exc.requested_at,
        status=exc.status,
        approved_by_id=exc.approved_by_id,
        approved_by_name=exc.approved_by_name,
        approved_at=exc.approved_at,
        approval_remarks=exc.approval_remarks,
        approved_amount=float(exc.approved_amount) if exc.approved_amount is not None else None,
        remaining_receivable=float(exc.remaining_receivable) if exc.remaining_receivable is not None else None,
        created_at=exc.created_at,
    )


class ListDischargeExceptionsAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = DischargeExceptionRepository(db, hospital_id)

    def execute(
        self,
        admission_id: UUID | None = None,
        status: ExceptionStatus | None = None,
    ) -> list[DischargeFinancialExceptionResponse]:
        exceptions = self.repo.list_exceptions(admission_id=admission_id, status=status)
        return [_to_exception_response(e) for e in exceptions]


class RequestDischargeExceptionAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = DischargeExceptionRepository(db, hospital_id)
        self.admissions_repo = AdmissionsRepository(db)
        self.billing_svc = InpatientBillingService(db)

    def execute(
        self,
        admission_id: UUID,
        payload: DischargeFinancialExceptionCreate,
        actor: dict[str, Any],
    ) -> DischargeFinancialExceptionResponse:
        admission = self.admissions_repo.get_admission_by_id(self.hospital_id, admission_id)
        if not admission:
            raise NotFoundError("Admission not found")
        if admission.status != AdmissionStatus.discharge_requested:
            raise ValidationError(
                f"Cannot request financial exception in '{admission.status.value}' state; discharge must be requested first"
            )

        # Get admission financial account and current outstanding
        acc = self.billing_svc.ensure_financial_account(
            self.hospital_id, admission.patient_id, admission.id
        )
        totals = self.billing_svc.get_ledger_totals(
            self.hospital_id, admission.patient_id, admission_id=admission.id
        )
        net_payable = float(totals.get("net_patient_balance") if totals.get("net_patient_balance") is not None else totals.get("outstanding") or 0.0)
        if net_payable <= 0.009:
            raise ValidationError("No outstanding balance requires financial exception")

        actor_id_str = actor.get("id") or actor.get("sub")
        requested_by_id = UUID(str(actor_id_str)) if actor_id_str else admission.doctor_id
        requested_by_name = str(actor.get("name") or "Staff")

        exc = self.repo.create_exception(
            admission_id=admission.id,
            financial_account_id=acc.id,
            patient_id=admission.patient_id,
            outstanding_amount=net_payable,
            reason=payload.reason,
            recommendation=payload.recommendation,
            requested_by_id=requested_by_id,
            requested_by_name=requested_by_name,
        )
        self.db.commit()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="create",
            entity_type="discharge_financial_exception",
            entity_id=str(exc.id),
            summary=f"Requested financial discharge exception for {admission.ip_id} (₹{net_payable:.2f}): {payload.reason}",
        )
        self.db.commit()
        return _to_exception_response(exc)


class DecideDischargeExceptionAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = DischargeExceptionRepository(db, hospital_id)

    def execute(
        self,
        exception_id: UUID,
        payload: DischargeFinancialExceptionDecision,
        actor: dict[str, Any],
    ) -> DischargeFinancialExceptionResponse:
        actor_id_str = actor.get("id") or actor.get("sub")
        if not actor_id_str:
            raise ValidationError("Actor ID required for exception decision")
        decided_by_id = UUID(str(actor_id_str))
        decided_by_name = str(actor.get("name") or "Authorizer")

        exc = self.repo.decide_exception(
            exception_id=exception_id,
            decided_by_id=decided_by_id,
            decided_by_name=decided_by_name,
            decision=payload.status,
            remarks=payload.approval_remarks,
        )
        self.db.commit()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="update",
            entity_type="discharge_financial_exception",
            entity_id=str(exc.id),
            summary=f"Financial exception {exc.id} {payload.status.value} by {decided_by_name}",
        )
        self.db.commit()
        return _to_exception_response(exc)
