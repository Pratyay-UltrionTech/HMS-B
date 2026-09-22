"""
Service Financial Clearance infrastructure.

Determines whether a specific billable service (e.g. LabOrder, RadiologyOrder)
is financially cleared based on its linked BillingCharge (source_type, source_id).
Conforms to healthcare service gating requirements without using simplistic patient-level flags.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingSourceType,
)


@dataclass(frozen=True)
class ServiceFinancialState:
    charge_exists: bool
    charge_id: UUID | None
    status: str  # "paid", "partially_paid", "pending", "cancelled", "unbilled"
    net_amount: float
    amount_paid: float
    outstanding_amount: float
    is_cleared: bool
    reason: str


def check_service_financial_clearance(
    db: Session,
    hospital_id: UUID,
    source_type: BillingSourceType,
    source_id: UUID,
) -> ServiceFinancialState:
    """
    Evaluate the authoritative financial clearance state of a specific service.
    
    Rules:
    - If no active non-cancelled charge exists:
        treated as unbilled / not cleared.
    - If charge is cancelled:
        not cleared, service cancelled.
    - If net_amount <= 0.0 or charge.status == BillingChargeStatus.paid:
        cleared (e.g. 0-cost, 100% discount, or fully paid).
    - If charge.status == BillingChargeStatus.partially_paid:
        NOT automatically cleared; requires full settlement unless net <= amount_paid.
    - If charge.status == BillingChargeStatus.pending:
        NOT cleared.
    """
    charge = (
        db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.source_type == source_type,
            BillingCharge.source_id == source_id,
        )
        .order_by(BillingCharge.created_at.desc())
        .first()
    )

    if not charge:
        # Fallback cross-link resolution between orders and prescription requests
        try:
            if source_type == BillingSourceType.laboratory:
                from modules.laboratory.entities.lab_entities import LabOrder, LabPrescriptionRequest
                # If source_id is LabOrder, check linked prescription request
                order = db.query(LabOrder).filter(LabOrder.id == source_id, LabOrder.hospital_id == hospital_id).first()
                if order and order.prescription_request_id:
                    charge = (
                        db.query(BillingCharge)
                        .filter(
                            BillingCharge.hospital_id == hospital_id,
                            BillingCharge.source_type == source_type,
                            BillingCharge.source_id == order.prescription_request_id,
                        )
                        .order_by(BillingCharge.created_at.desc())
                        .first()
                    )
                elif not order:
                    # If source_id is LabPrescriptionRequest, check linked LabOrder
                    req = db.query(LabPrescriptionRequest).filter(LabPrescriptionRequest.id == source_id, LabPrescriptionRequest.hospital_id == hospital_id).first()
                    if req and req.lab_order_id:
                        charge = (
                            db.query(BillingCharge)
                            .filter(
                                BillingCharge.hospital_id == hospital_id,
                                BillingCharge.source_type == source_type,
                                BillingCharge.source_id == req.lab_order_id,
                            )
                            .order_by(BillingCharge.created_at.desc())
                            .first()
                        )
            elif source_type == BillingSourceType.radiology:
                from modules.radiology.entities.radiology_entities import RadiologyOrder, RadPrescriptionRequest
                # If source_id is RadiologyOrder, check linked prescription request
                rad_order = db.query(RadiologyOrder).filter(RadiologyOrder.id == source_id, RadiologyOrder.hospital_id == hospital_id).first()
                if rad_order and rad_order.prescription_request_id:
                    charge = (
                        db.query(BillingCharge)
                        .filter(
                            BillingCharge.hospital_id == hospital_id,
                            BillingCharge.source_type == source_type,
                            BillingCharge.source_id == rad_order.prescription_request_id,
                        )
                        .order_by(BillingCharge.created_at.desc())
                        .first()
                    )
                elif not rad_order:
                    # If source_id is RadPrescriptionRequest, check any created RadiologyOrder
                    rad_o = db.query(RadiologyOrder).filter(RadiologyOrder.prescription_request_id == source_id, RadiologyOrder.hospital_id == hospital_id).first()
                    if rad_o:
                        charge = (
                            db.query(BillingCharge)
                            .filter(
                                BillingCharge.hospital_id == hospital_id,
                                BillingCharge.source_type == source_type,
                                BillingCharge.source_id == rad_o.id,
                            )
                            .order_by(BillingCharge.created_at.desc())
                            .first()
                        )
        except Exception:
            pass

    if not charge:
        return ServiceFinancialState(
            charge_exists=False,
            charge_id=None,
            status="unbilled",
            net_amount=0.0,
            amount_paid=0.0,
            outstanding_amount=0.0,
            is_cleared=False,
            reason="No billing charge found for this service order.",
        )

    if charge.status == BillingChargeStatus.cancelled:
        return ServiceFinancialState(
            charge_exists=True,
            charge_id=charge.id,
            status="cancelled",
            net_amount=float(charge.net_amount or 0.0),
            amount_paid=float(charge.amount_paid or 0.0),
            outstanding_amount=0.0,
            is_cleared=False,
            reason="The billing charge for this service is cancelled.",
        )

    net = round(float(charge.net_amount or 0.0), 2)
    paid = round(float(charge.amount_paid or 0.0), 2)
    outstanding = max(0.0, round(net - paid, 2))

    # Fully cleared if net is 0 (or negative) or status is paid or paid covers net
    if net <= 0.0 or charge.status == BillingChargeStatus.paid or paid >= net > 0.0:
        return ServiceFinancialState(
            charge_exists=True,
            charge_id=charge.id,
            status="paid",
            net_amount=net,
            amount_paid=paid,
            outstanding_amount=0.0,
            is_cleared=True,
            reason="Service is financially cleared.",
        )

    if charge.status == BillingChargeStatus.partially_paid or paid > 0.0:
        return ServiceFinancialState(
            charge_exists=True,
            charge_id=charge.id,
            status="partially_paid",
            net_amount=net,
            amount_paid=paid,
            outstanding_amount=outstanding,
            is_cleared=False,
            reason=f"Service is partially paid. Outstanding amount: ₹{outstanding:.2f}.",
        )

    return ServiceFinancialState(
        charge_exists=True,
        charge_id=charge.id,
        status="pending",
        net_amount=net,
        amount_paid=paid,
        outstanding_amount=outstanding,
        is_cleared=False,
        reason=f"Payment pending. Outstanding amount: ₹{outstanding:.2f}.",
    )


def assert_service_financially_cleared(
    db: Session,
    hospital_id: UUID,
    source_type: BillingSourceType,
    source_id: UUID,
    action_description: str = "proceed with service",
    is_emergency_override: bool = False,
) -> ServiceFinancialState:
    """
    Assert that the service charge is financially cleared.
    Raises HTTPException 402 PAYMENT_REQUIRED (or 400 if cancelled) if blocked.
    If is_emergency_override is True, emergency/STAT clinical workflow is permitted to proceed.
    """
    state = check_service_financial_clearance(db, hospital_id, source_type, source_id)
    if state.is_cleared:
        return state

    if is_emergency_override:
        # Permitted under clinical life-safety / emergency STAT exception protocol
        return ServiceFinancialState(
            charge_exists=state.charge_exists,
            charge_id=state.charge_id,
            status=state.status,
            net_amount=state.net_amount,
            amount_paid=state.amount_paid,
            outstanding_amount=state.outstanding_amount,
            is_cleared=True,
            reason="Emergency / STAT clinical override active.",
        )

    if state.status == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot {action_description}: billing charge is cancelled.",
        )

    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={
            "message": f"Payment required to {action_description}.",
            "status": state.status,
            "net_amount": state.net_amount,
            "amount_paid": state.amount_paid,
            "outstanding_amount": state.outstanding_amount,
            "reason": state.reason,
        },
    )
