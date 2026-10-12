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
from modules.billing.services.financial_authorization import (
    OverrideActor,
    OverrideScope,
    authorize_emergency_override,
    authorize_exception_approval,
    require_override_reason,
    resolve_actor,
)
from shared.audit.service import write_audit_log

ENTITY_TYPE = "financial_clearance_exception"


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def exception_audit_details(exc: FinancialClearanceException, **extra: Any) -> dict[str, Any]:
    """Audit / sync payload for an exception row (patient, admission, account, scope)."""
    details: dict[str, Any] = {
        "exception_id": str(exc.id) if exc.id else None,
        "patient_id": str(exc.patient_id) if exc.patient_id else None,
        "admission_id": str(exc.admission_id) if exc.admission_id else None,
        "account_id": str(exc.financial_account_id) if exc.financial_account_id else None,
        "service_type": exc.service_type,
        "source_type": exc.service_type,
        "action_type": exc.action_type,
        "source_id": str(exc.source_id) if exc.source_id else None,
        "is_emergency": bool(exc.is_emergency),
        "status": exc.status,
        "is_consumed": bool(exc.is_consumed),
        "reason_category": exc.reason_category,
        "reason": exc.reason,
        "required_amount": float(exc.required_amount or 0),
        "amount_covered": float(exc.amount_covered or 0),
        "shortfall_amount": float(exc.shortfall_amount or 0),
    }
    details.update(extra)
    return details


def consume_exception(exc: FinancialClearanceException, now: datetime | None = None) -> None:
    """Mark an approved exception as used; status and flag always move together."""
    exc.is_consumed = True
    exc.consumed_at = now or datetime.now(timezone.utc)
    exc.status = FinancialExceptionStatus.consumed.value


def _validate_scope_targets(db: Session, hospital_id: UUID, scope: OverrideScope) -> None:
    """Admission / charge referenced by the scope must belong to this hospital and patient."""
    if scope.admission_id:
        from modules.inpatient.entities.admission import Admission

        adm = (
            db.query(Admission)
            .filter(Admission.id == scope.admission_id, Admission.hospital_id == hospital_id)
            .first()
        )
        if adm is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admission not found.")
        if scope.patient_id and adm.patient_id != scope.patient_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Admission does not belong to the selected patient.",
            )
    if scope.source_id and not scope.is_admission_scope:
        from modules.billing.entities.billing_entities import BillingCharge

        charge = (
            db.query(BillingCharge)
            .filter(
                BillingCharge.hospital_id == hospital_id,
                BillingCharge.source_type == scope.service_type,
                BillingCharge.source_id == scope.source_id,
            )
            .order_by(BillingCharge.created_at.desc())
            .first()
        )
        if charge is not None and scope.patient_id and charge.patient_id != scope.patient_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The referenced service does not belong to the selected patient.",
            )


def _resolve_account_id(
    db: Session, hospital_id: UUID, scope: OverrideScope, actor_name: str
) -> UUID | None:
    if scope.admission_id and scope.patient_id:
        from modules.inpatient.services.inpatient_billing_service import InpatientBillingService

        acc = InpatientBillingService(db).ensure_financial_account(
            hospital_id=hospital_id,
            patient_id=scope.patient_id,
            admission_id=scope.admission_id,
            created_by_name=actor_name,
        )
        return acc.id if acc else None
    if scope.source_id and not scope.is_admission_scope:
        from modules.billing.entities.billing_entities import BillingCharge

        charge = (
            db.query(BillingCharge)
            .filter(
                BillingCharge.hospital_id == hospital_id,
                BillingCharge.source_type == scope.service_type,
                BillingCharge.source_id == scope.source_id,
            )
            .order_by(BillingCharge.created_at.desc())
            .first()
        )
        return charge.account_id if charge else None
    return None


def record_emergency_override(
    db: Session,
    hospital_id: UUID,
    actor: OverrideActor,
    scope: OverrideScope,
    reason: str,
    *,
    required_amount: float | Decimal = 0,
    amount_covered: float | Decimal = 0,
    consumed: bool,
    financial_account_id: UUID | None = None,
    approval_remarks: str | None = None,
) -> FinancialClearanceException:
    """Persist an authorized emergency override and stage its audit + sync event.

    ``actor`` must come from ``authorize_emergency_override``. ``consumed=True`` for
    inline overrides applied immediately; ``False`` for a standalone override that the
    next matching gate (same source / admission) will consume. The caller commits.
    """
    if not scope.patient_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Emergency override requires a registered patient.",
        )
    reason_text = require_override_reason(reason)
    account_id = financial_account_id or _resolve_account_id(db, hospital_id, scope, actor.name)
    required = _money(required_amount)
    covered = _money(amount_covered)
    now = datetime.now(timezone.utc)

    exc = FinancialClearanceException(
        hospital_id=hospital_id,
        patient_id=scope.patient_id,
        admission_id=scope.admission_id,
        financial_account_id=account_id,
        service_type=scope.service_type,
        action_type=scope.action_type,
        source_id=scope.source_id,
        is_emergency=True,
        required_amount=required,
        amount_covered=covered,
        shortfall_amount=max(Decimal("0.00"), required - covered),
        reason_category="emergency_life_safety",
        reason=reason_text,
        status=FinancialExceptionStatus.approved.value,
        requested_by_id=actor.user_id,
        requested_by_name=actor.name,
        approved_by_id=actor.user_id,
        approved_by_name=actor.name,
        approved_at=now,
        approval_remarks=approval_remarks
        or f"Emergency override authorized via {actor.granted_by or 'emergency override permission'}",
    )
    if consumed:
        consume_exception(exc, now)
    db.add(exc)
    db.flush()

    scope_label = str(scope.source_id or scope.admission_id)
    write_audit_log(
        db,
        hospital_id=hospital_id,
        actor=actor.claims,
        action="emergency_override",
        entity_type=ENTITY_TYPE,
        entity_id=exc.id,
        summary=f"Emergency financial override for {scope.service_type} ({scope_label}) by {actor.name}: {reason_text}",
        details=exception_audit_details(exc, **actor.audit_fields()),
    )
    return exc


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
        scope = OverrideScope(
            service_type=payload.service_type,
            patient_id=payload.patient_id,
            source_id=payload.source_id,
            admission_id=payload.admission_id,
            action_type=payload.action_type,
        )
        scope.validate()
        reason_text = require_override_reason(payload.reason, label="Financial exception reason")
        requester = resolve_actor(self.db, self.hospital_id, actor)
        _validate_scope_targets(self.db, self.hospital_id, scope)
        account_id = _resolve_account_id(self.db, self.hospital_id, scope, requester.name)

        required = _money(payload.required_amount)
        exc = FinancialClearanceException(
            hospital_id=self.hospital_id,
            patient_id=payload.patient_id,
            admission_id=payload.admission_id,
            financial_account_id=account_id,
            service_type=payload.service_type,
            action_type=payload.action_type,
            source_id=payload.source_id,
            is_emergency=False,
            required_amount=required,
            amount_covered=Decimal("0.00"),
            shortfall_amount=required,
            reason_category=payload.reason_category,
            reason=reason_text,
            status=FinancialExceptionStatus.pending.value,
            requested_by_id=requester.user_id,
            requested_by_name=requester.name,
        )
        self.db.add(exc)
        self.db.flush()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=requester.claims,
            action="create",
            entity_type=ENTITY_TYPE,
            entity_id=exc.id,
            summary=f"Requested financial exception for {payload.service_type}/{payload.action_type}: {reason_text}",
            details=exception_audit_details(exc, **requester.audit_fields()),
        )
        self.db.commit()
        self.db.refresh(exc)
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
        approver = authorize_exception_approval(self.db, self.hospital_id, actor)

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

        if payload.status == "approved" and exc.requested_by_id == approver.user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Separation of duties violation: the requester cannot approve their own financial exception.",
            )

        exc.status = payload.status
        exc.approved_by_id = approver.user_id
        exc.approved_by_name = approver.name
        exc.approved_at = datetime.now(timezone.utc)
        exc.approval_remarks = payload.approval_remarks

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=approver.claims,
            action="update",
            entity_type=ENTITY_TYPE,
            entity_id=exc.id,
            summary=f"Financial exception {payload.status} by {approver.name}: {payload.approval_remarks or ''}",
            details=exception_audit_details(
                exc,
                decision=payload.status,
                approval_remarks=payload.approval_remarks,
                requested_by_id=str(exc.requested_by_id),
                **approver.audit_fields(),
            ),
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
        Record an authorized emergency financial override scoped to one source
        (service_type + source_id) or one admission (bed / admission service types).
        The next matching gate consumes it. Does NOT forgive debt.
        """
        scope = OverrideScope(
            service_type=payload.service_type,
            patient_id=payload.patient_id,
            source_id=payload.source_id,
            admission_id=payload.admission_id,
            action_type=payload.action_type,
        )
        override_actor = authorize_emergency_override(
            self.db, self.hospital_id, actor, scope=scope, reason=payload.reason
        )
        _validate_scope_targets(self.db, self.hospital_id, scope)

        exc = record_emergency_override(
            self.db,
            self.hospital_id,
            override_actor,
            scope,
            payload.reason,
            required_amount=payload.required_amount,
            consumed=False,
        )
        self.db.commit()
        self.db.refresh(exc)
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
