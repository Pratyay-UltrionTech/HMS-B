"""Service for managing reusable, audited Financial Clearance Exceptions and Emergency Overrides."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from modules.billing.contracts.exception_contracts import (
    EmergencyOverrideRequest,
    FinancialExceptionDecision,
    FinancialExceptionRequest,
    FinancialExceptionResponse,
)
from modules.billing.entities.financial_exception import (
    FinancialClearanceException,
    FinancialExceptionStatus,
)
from modules.inpatient.services.inpatient_billing_service import InpatientBillingService
from shared.audit.service import write_audit_log


class FinancialExceptionService:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    def request_exception(
        self,
        payload: FinancialExceptionRequest,
        actor: dict[str, Any],
    ) -> FinancialExceptionResponse:
        """Create a pending financial clearance exception. Requester cannot self-approve."""
        actor_id_str = actor.get("id") or actor.get("sub") or actor.get("user_id")
        if not actor_id_str:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User identifier missing from auth token.",
            )
        try:
            actor_id = UUID(str(actor_id_str))
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid user ID in auth context.",
            )

        actor_name = str(actor.get("name") or "Staff")

        # Resolve financial account if admission_id is present
        fin_account_id = None
        if payload.admission_id:
            billing_svc = InpatientBillingService(self.db)
            acc = billing_svc.ensure_financial_account(
                hospital_id=self.hospital_id,
                patient_id=payload.patient_id,
                admission_id=payload.admission_id,
                created_by_name=actor_name,
            )
            fin_account_id = acc.id if acc else None

        exc = FinancialClearanceException(
            hospital_id=self.hospital_id,
            patient_id=payload.patient_id,
            admission_id=payload.admission_id,
            financial_account_id=fin_account_id,
            service_type=payload.service_type,
            action_type=payload.action_type,
            source_id=payload.source_id,
            is_emergency=False,
            required_amount=Decimal(str(payload.required_amount)),
            amount_covered=Decimal("0.00"),
            shortfall_amount=Decimal(str(payload.required_amount)),
            reason_category=payload.reason_category,
            reason=payload.reason.strip(),
            status=FinancialExceptionStatus.pending.value,
            requested_by_id=actor_id,
            requested_by_name=actor_name,
        )
        self.db.add(exc)
        self.db.commit()
        self.db.refresh(exc)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="create",
            entity_type="financial_clearance_exception",
            entity_id=exc.id,
            summary=f"Requested financial exception for {payload.service_type}/{payload.action_type}: {payload.reason}",
        )

        return FinancialExceptionResponse.model_validate(exc)

    def decide_exception(
        self,
        exception_id: UUID,
        payload: FinancialExceptionDecision,
        actor: dict[str, Any],
    ) -> FinancialExceptionResponse:
        """
        Approve or reject a financial exception.
        Enforces separation of duties: requester cannot approve their own request!
        """
        actor_id_str = actor.get("id") or actor.get("sub") or actor.get("user_id")
        if not actor_id_str:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User identifier missing from auth token.",
            )
        try:
            actor_id = UUID(str(actor_id_str))
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid user ID in auth context.",
            )

        exc = (
            self.db.query(FinancialClearanceException)
            .filter(
                FinancialClearanceException.id == exception_id,
                FinancialClearanceException.hospital_id == self.hospital_id,
            )
            .with_for_update()
            .first()
        )
        if not exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Financial clearance exception not found.",
            )

        if exc.status != FinancialExceptionStatus.pending.value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Exception is already {exc.status} and cannot be modified.",
            )

        # Enforce separation of duties
        if payload.status == "approved" and exc.requested_by_id == actor_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Separation of duties violation: the requester cannot approve their own financial exception.",
            )

        now = datetime.now(timezone.utc)
        exc.status = payload.status
        exc.approved_by_id = actor_id
        exc.approved_by_name = str(actor.get("name") or "Authorized Approver")
        exc.approved_at = now
        exc.approval_remarks = payload.approval_remarks

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="update",
            entity_type="financial_clearance_exception",
            entity_id=exc.id,
            summary=f"Financial exception {payload.status} by {exc.approved_by_name}: {payload.approval_remarks or ''}",
        )
        self.db.commit()
        self.db.refresh(exc)
        return FinancialExceptionResponse.model_validate(exc)

    def emergency_override(
        self,
        payload: EmergencyOverrideRequest,
        actor: dict[str, Any],
    ) -> FinancialExceptionResponse:
        """
        Record an emergency clinical financial override.
        Immediately authorized without approval delay; does NOT forgive debt.
        """
        actor_id_str = actor.get("id") or actor.get("sub") or actor.get("user_id")
        actor_id = None
        if actor_id_str:
            try:
                actor_id = UUID(str(actor_id_str))
            except (ValueError, TypeError):
                actor_id = None

        actor_name = str(actor.get("name") or "Emergency Clinician")

        fin_account_id = None
        if payload.admission_id:
            billing_svc = InpatientBillingService(self.db)
            acc = billing_svc.ensure_financial_account(
                hospital_id=self.hospital_id,
                patient_id=payload.patient_id,
                admission_id=payload.admission_id,
                created_by_name=actor_name,
            )
            fin_account_id = acc.id if acc else None

        now = datetime.now(timezone.utc)
        exc = FinancialClearanceException(
            hospital_id=self.hospital_id,
            patient_id=payload.patient_id,
            admission_id=payload.admission_id,
            financial_account_id=fin_account_id,
            service_type=payload.service_type,
            action_type=payload.action_type,
            source_id=payload.source_id,
            is_emergency=True,
            required_amount=Decimal(str(payload.required_amount)),
            amount_covered=Decimal("0.00"),
            shortfall_amount=Decimal(str(payload.required_amount)),
            reason_category="emergency_life_safety",
            reason=payload.reason.strip(),
            status=FinancialExceptionStatus.approved.value,
            requested_by_id=actor_id or UUID("00000000-0000-0000-0000-000000000000"),
            requested_by_name=actor_name,
            approved_by_id=actor_id,
            approved_by_name=actor_name,
            approved_at=now,
            approval_remarks="Auto-authorized under emergency life-safety clinical override protocol",
        )
        self.db.add(exc)
        self.db.commit()
        self.db.refresh(exc)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="emergency_override",
            entity_type="financial_clearance_exception",
            entity_id=exc.id,
            summary=f"Emergency financial override recorded for {payload.service_type}/{payload.action_type}: {payload.reason}",
        )
        return FinancialExceptionResponse.model_validate(exc)

    def list_exceptions(
        self,
        patient_id: UUID | None = None,
        admission_id: UUID | None = None,
        status_filter: str | None = None,
        limit: int = 100,
    ) -> list[FinancialExceptionResponse]:
        """List financial exceptions with optional filtering."""
        q = self.db.query(FinancialClearanceException).filter(
            FinancialClearanceException.hospital_id == self.hospital_id
        )
        if patient_id:
            q = q.filter(FinancialClearanceException.patient_id == patient_id)
        if admission_id:
            q = q.filter(FinancialClearanceException.admission_id == admission_id)
        if status_filter:
            q = q.filter(FinancialClearanceException.status == status_filter)

        rows = q.order_by(FinancialClearanceException.created_at.desc()).limit(limit).all()
        return [FinancialExceptionResponse.model_validate(r) for r in rows]
