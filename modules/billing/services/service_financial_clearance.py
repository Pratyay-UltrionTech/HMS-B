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
                    # If source_id is LabPrescriptionRequest, check linked LabOrder (either via req.lab_order_id or LabOrder.prescription_request_id)
                    req = db.query(LabPrescriptionRequest).filter(LabPrescriptionRequest.id == source_id, LabPrescriptionRequest.hospital_id == hospital_id).first()
                    linked_lab_order_id = req.lab_order_id if req else None
                    if not linked_lab_order_id:
                        lab_o = db.query(LabOrder).filter(LabOrder.prescription_request_id == source_id, LabOrder.hospital_id == hospital_id).first()
                        if lab_o:
                            linked_lab_order_id = lab_o.id
                    if linked_lab_order_id:
                        charge = (
                            db.query(BillingCharge)
                            .filter(
                                BillingCharge.hospital_id == hospital_id,
                                BillingCharge.source_type == source_type,
                                BillingCharge.source_id == linked_lab_order_id,
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


def _state_from_charge(charge: BillingCharge | None) -> ServiceFinancialState:
    """
    Convert a single BillingCharge ORM row (or None) into a ServiceFinancialState.

    Encapsulates the clearance business rules so they are applied identically by
    both check_service_financial_clearance() and bulk_check_service_financial_clearance().
    This function must remain in exact parity with the logic in
    check_service_financial_clearance() — any rule change must be applied to both.
    """
    if charge is None:
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


def bulk_check_service_financial_clearance(
    db: Session,
    hospital_id: UUID,
    source_type: BillingSourceType,
    source_ids: list[UUID],
) -> dict[UUID, ServiceFinancialState]:
    """
    Bulk-evaluate financial clearance for a list of source_ids in a minimal number
    of DB round-trips (2–4 queries total regardless of list length).

    Returns a mapping of source_id -> ServiceFinancialState for every supplied ID.
    IDs with no direct charge are resolved via cross-link fallback (prescription
    request ↔ radiology order) using additional bulk queries — never per-item
    queries.  IDs that remain unresolved are returned as "unbilled" / not cleared.

    All clearance business rules are identical to check_service_financial_clearance().
    The 'latest applicable charge' (ORDER BY created_at DESC) semantics are
    preserved: for each source_id, only the most-recently created charge is used.

    The existing single-item check_service_financial_clearance() is unchanged.
    """
    if not source_ids:
        return {}

    id_list = list(source_ids)

    # ── Step 1: Fetch latest charge per source_id in one bulk query ───────────
    # Fetch all matching charges, then keep only the most-recent per source_id
    # in Python.  This is equivalent to DISTINCT ON (source_id) ORDER BY
    # source_id, created_at DESC and preserves the "latest charge" semantics.
    all_charges = (
        db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.source_type == source_type,
            BillingCharge.source_id.in_(id_list),
        )
        .order_by(BillingCharge.source_id, BillingCharge.created_at.desc())
        .all()
    )

    # Keep only the first (latest) charge seen per source_id
    charges_by_source: dict[UUID, BillingCharge] = {}
    for charge in all_charges:
        if charge.source_id not in charges_by_source:
            charges_by_source[charge.source_id] = charge

    # ── Step 2: Identify source_ids without a direct charge ───────────────────
    missing_ids = [sid for sid in id_list if sid not in charges_by_source]

    # ── Step 3: Bulk cross-link fallback resolution ────────────────────────────
    # For laboratory & radiology: a source_id may be either an Order.id (whose charge
    # was recorded against its prescription_request_id) or a
    # PrescriptionRequest.id (whose charge was recorded against a linked
    # Order.id). We resolve all missing IDs in bulk queries:
    # one for Order/Request rows and one for BillingCharge on counterparts.
    if missing_ids and source_type == BillingSourceType.laboratory:
        try:
            from modules.laboratory.entities.lab_entities import LabOrder, LabPrescriptionRequest
            from sqlalchemy import or_ as sa_or

            # Fetch all lab orders whose .id OR .prescription_request_id appears in missing_ids
            linked_orders = (
                db.query(LabOrder)
                .filter(
                    LabOrder.hospital_id == hospital_id,
                    sa_or(
                        LabOrder.id.in_(missing_ids),
                        LabOrder.prescription_request_id.in_(missing_ids),
                    ),
                )
                .all()
            )

            orders_by_id: dict[UUID, LabOrder] = {o.id: o for o in linked_orders}
            orders_by_req: dict[UUID, LabOrder] = {}
            for o in linked_orders:
                if o.prescription_request_id and o.prescription_request_id not in orders_by_req:
                    orders_by_req[o.prescription_request_id] = o

            # For remaining missing IDs that weren't resolved as LabOrder.id,
            # query LabPrescriptionRequest to find their linked lab_order_id
            still_unresolved = [sid for sid in missing_ids if sid not in orders_by_id and sid not in orders_by_req]
            reqs_by_id: dict[UUID, LabPrescriptionRequest] = {}
            if still_unresolved:
                linked_reqs = (
                    db.query(LabPrescriptionRequest)
                    .filter(
                        LabPrescriptionRequest.hospital_id == hospital_id,
                        LabPrescriptionRequest.id.in_(still_unresolved),
                    )
                    .all()
                )
                reqs_by_id = {r.id: r for r in linked_reqs}

            missing_to_counterpart: dict[UUID, UUID] = {}
            counterpart_ids: list[UUID] = []
            for sid in missing_ids:
                counterpart: UUID | None = None
                if sid in orders_by_id:
                    # sid is a LabOrder.id — look up charge by its prescription_request_id
                    req_id = orders_by_id[sid].prescription_request_id
                    if req_id:
                        counterpart = req_id
                elif sid in orders_by_req:
                    # sid is a LabPrescriptionRequest.id — look up charge by the order's id
                    counterpart = orders_by_req[sid].id
                elif sid in reqs_by_id:
                    # sid is a LabPrescriptionRequest.id without a matching LabOrder from query 1
                    ord_id = reqs_by_id[sid].lab_order_id
                    if ord_id:
                        counterpart = ord_id

                if counterpart:
                    missing_to_counterpart[sid] = counterpart
                    counterpart_ids.append(counterpart)

            if counterpart_ids:
                fallback_charges = (
                    db.query(BillingCharge)
                    .filter(
                        BillingCharge.hospital_id == hospital_id,
                        BillingCharge.source_type == source_type,
                        BillingCharge.source_id.in_(counterpart_ids),
                    )
                    .order_by(BillingCharge.source_id, BillingCharge.created_at.desc())
                    .all()
                )
                fallback_by_counterpart: dict[UUID, BillingCharge] = {}
                for charge in fallback_charges:
                    if charge.source_id not in fallback_by_counterpart:
                        fallback_by_counterpart[charge.source_id] = charge

                for sid, counterpart in missing_to_counterpart.items():
                    if counterpart in fallback_by_counterpart:
                        charges_by_source[sid] = fallback_by_counterpart[counterpart]

        except Exception:
            pass

    elif missing_ids and source_type == BillingSourceType.radiology:
        try:
            from modules.radiology.entities.radiology_entities import RadiologyOrder
            from sqlalchemy import or_ as sa_or

            # Fetch all radiology orders whose .id OR .prescription_request_id
            # appears in missing_ids — covers both cross-link directions at once.
            linked_orders = (
                db.query(RadiologyOrder)
                .filter(
                    RadiologyOrder.hospital_id == hospital_id,
                    sa_or(
                        RadiologyOrder.id.in_(missing_ids),
                        RadiologyOrder.prescription_request_id.in_(missing_ids),
                    ),
                )
                .all()
            )

            # Index for O(1) lookup in both directions
            orders_by_id: dict[UUID, RadiologyOrder] = {o.id: o for o in linked_orders}
            # Keep first order per prescription_request_id (consistent with the
            # original single-item fallback which uses .first())
            orders_by_req: dict[UUID, RadiologyOrder] = {}
            for o in linked_orders:
                if o.prescription_request_id and o.prescription_request_id not in orders_by_req:
                    orders_by_req[o.prescription_request_id] = o

            # Map each missing source_id to its counterpart charge-lookup ID
            missing_to_counterpart: dict[UUID, UUID] = {}
            counterpart_ids: list[UUID] = []
            for sid in missing_ids:
                counterpart: UUID | None = None
                if sid in orders_by_id:
                    # sid is a RadiologyOrder.id — look up charge by its prescription_request_id
                    req_id = orders_by_id[sid].prescription_request_id
                    if req_id:
                        counterpart = req_id
                elif sid in orders_by_req:
                    # sid is a RadPrescriptionRequest.id — look up charge by the order's id
                    counterpart = orders_by_req[sid].id

                if counterpart:
                    missing_to_counterpart[sid] = counterpart
                    counterpart_ids.append(counterpart)

            # One bulk charge query for all resolved counterpart IDs
            if counterpart_ids:
                fallback_charges = (
                    db.query(BillingCharge)
                    .filter(
                        BillingCharge.hospital_id == hospital_id,
                        BillingCharge.source_type == source_type,
                        BillingCharge.source_id.in_(counterpart_ids),
                    )
                    .order_by(BillingCharge.source_id, BillingCharge.created_at.desc())
                    .all()
                )
                fallback_by_counterpart: dict[UUID, BillingCharge] = {}
                for charge in fallback_charges:
                    if charge.source_id not in fallback_by_counterpart:
                        fallback_by_counterpart[charge.source_id] = charge

                # Map resolved charges back to original source_ids
                for sid, counterpart in missing_to_counterpart.items():
                    if counterpart in fallback_by_counterpart:
                        charges_by_source[sid] = fallback_by_counterpart[counterpart]

        except Exception:
            # Fallback resolution failed — missing IDs stay missing (unbilled / not cleared)
            pass

    # ── Step 4: Build and return result map ────────────────────────────────────
    return {
        sid: _state_from_charge(charges_by_source.get(sid))
        for sid in id_list
    }


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
