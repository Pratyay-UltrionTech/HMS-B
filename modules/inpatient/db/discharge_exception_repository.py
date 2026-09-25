"""Repository for Discharge Financial Exceptions and authorization decisions."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.orm import Session, joinedload

from modules.inpatient.entities.discharge_exception import (
    DischargeFinancialException,
    ExceptionStatus,
)
from shared.exceptions.base import ValidationError


class DischargeExceptionRepository:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    def list_exceptions(
        self,
        *,
        admission_id: UUID | None = None,
        status: ExceptionStatus | None = None,
    ) -> list[DischargeFinancialException]:
        q = (
            self.db.query(DischargeFinancialException)
            .options(joinedload(DischargeFinancialException.patient))
            .filter(DischargeFinancialException.hospital_id == self.hospital_id)
        )
        if admission_id:
            q = q.filter(DischargeFinancialException.admission_id == admission_id)
        if status:
            q = q.filter(DischargeFinancialException.status == status)
        return q.order_by(DischargeFinancialException.requested_at.desc()).all()

    def get_exception_by_id(
        self, exception_id: UUID
    ) -> DischargeFinancialException | None:
        return (
            self.db.query(DischargeFinancialException)
            .options(joinedload(DischargeFinancialException.patient))
            .filter(
                DischargeFinancialException.id == exception_id,
                DischargeFinancialException.hospital_id == self.hospital_id,
            )
            .first()
        )

    def get_active_approved_exception(
        self, admission_id: UUID
    ) -> DischargeFinancialException | None:
        return (
            self.db.query(DischargeFinancialException)
            .filter(
                DischargeFinancialException.hospital_id == self.hospital_id,
                DischargeFinancialException.admission_id == admission_id,
                DischargeFinancialException.status == ExceptionStatus.approved,
            )
            .first()
        )

    def get_latest_exception(
        self, admission_id: UUID
    ) -> DischargeFinancialException | None:
        return (
            self.db.query(DischargeFinancialException)
            .filter(
                DischargeFinancialException.hospital_id == self.hospital_id,
                DischargeFinancialException.admission_id == admission_id,
            )
            .order_by(DischargeFinancialException.created_at.desc())
            .first()
        )

    def get_active_exception(
        self, admission_id: UUID
    ) -> DischargeFinancialException | None:
        """Alias returning the latest exception or active approved exception."""
        return self.get_latest_exception(admission_id)

    def create_exception(
        self,
        *,
        admission_id: UUID,
        financial_account_id: UUID,
        patient_id: UUID,
        outstanding_amount: float,
        reason: str,
        recommendation: str | None,
        requested_by_id: UUID,
        requested_by_name: str,
    ) -> DischargeFinancialException:
        # Check if already has a pending exception
        pending = (
            self.db.query(DischargeFinancialException)
            .filter(
                DischargeFinancialException.hospital_id == self.hospital_id,
                DischargeFinancialException.admission_id == admission_id,
                DischargeFinancialException.status == ExceptionStatus.pending,
            )
            .first()
        )
        if pending:
            raise ValidationError("A financial exception request is already pending for this admission")

        exc = DischargeFinancialException(
            hospital_id=self.hospital_id,
            admission_id=admission_id,
            financial_account_id=financial_account_id,
            patient_id=patient_id,
            outstanding_amount_at_request=outstanding_amount,
            reason=reason,
            recommendation=recommendation,
            requested_by_id=requested_by_id,
            requested_by_name=requested_by_name,
            status=ExceptionStatus.pending,
        )
        self.db.add(exc)
        self.db.flush()
        return exc

    def decide_exception(
        self,
        *,
        exception_id: UUID,
        decided_by_id: UUID,
        decided_by_name: str,
        decision: ExceptionStatus,
        remarks: str | None = None,
    ) -> DischargeFinancialException:
        exc = self.get_exception_by_id(exception_id)
        if not exc:
            raise ValidationError("Discharge financial exception not found")
        if exc.status != ExceptionStatus.pending:
            raise ValidationError(f"Exception is already {exc.status.value}")

        # Strict Separation of Duties: Requester cannot approve their own exception
        if str(exc.requested_by_id) == str(decided_by_id) and decision == ExceptionStatus.approved:
            raise ValidationError(
                "Separation of duties violation: The requester cannot approve their own discharge exception"
            )

        now = datetime.now(timezone.utc)
        exc.status = decision
        exc.approved_by_id = decided_by_id
        exc.approved_by_name = decided_by_name
        exc.approved_at = now
        exc.approval_remarks = remarks
        if decision == ExceptionStatus.approved:
            exc.approved_amount = exc.outstanding_amount_at_request
            exc.remaining_receivable = exc.outstanding_amount_at_request
        else:
            exc.approved_amount = None
            exc.remaining_receivable = None
        self.db.flush()
        return exc
