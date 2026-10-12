"""
Radiology prescription requests fulfillment and lifecycle service.

Mirrors the laboratory prescription-request flow (get/pending/assert/respond/
sync) for doctor-prescribed imaging investigations: a prescription carrying
scan_ids produces a RadPrescriptionRequest whose items are accessioned into
per-scan RadiologyOrders via the fulfillment branch of CreateOrdersAction.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, joinedload

from modules.radiology.entities.radiology_entities import (
    RadiologyOrder,
    RadiologyOrderStatus,
    RadPrescriptionRequest,
    RadPrescriptionRequestItem,
    RadPrescriptionRequestStatus,
    RadiologyScanCatalog,
    RadRequestItemStatus,
)


OPEN_ORDER_STATUSES = {
    RadiologyOrderStatus.ordered,
    RadiologyOrderStatus.scheduled,
    RadiologyOrderStatus.in_progress,
}


def get_prescription_request(
    db: Session, request_id: UUID, hospital_id: UUID
) -> RadPrescriptionRequest:
    req = (
        db.query(RadPrescriptionRequest)
        .options(
            joinedload(RadPrescriptionRequest.patient),
            joinedload(RadPrescriptionRequest.doctor),
            joinedload(RadPrescriptionRequest.items),
            joinedload(RadPrescriptionRequest.prescription),
        )
        .filter(
            RadPrescriptionRequest.id == request_id,
            RadPrescriptionRequest.hospital_id == hospital_id,
        )
        .first()
    )
    if not req:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prescription radiology request not found",
        )
    return req


def apply_cancel_rad_prescription_request(
    db: Session,
    request_id: UUID,
    hospital_id: UUID,
    reason: str | None,
) -> RadPrescriptionRequest:
    """Cancel a radiology prescription request in the current session without committing."""
    req = get_prescription_request(db, request_id, hospital_id)
    if req.status == RadPrescriptionRequestStatus.cancelled:
        return req
    if req.status == RadPrescriptionRequestStatus.completed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Completed prescription requests cannot be cancelled",
        )
    assert_rad_request_cancellable(db, req)
    req.status = RadPrescriptionRequestStatus.cancelled
    req.cancel_reason = reason.strip() if reason else "Cancelled by staff"
    for item in req.items or []:
        if item.status == RadRequestItemStatus.pending:
            item.status = RadRequestItemStatus.cancelled
    return req


def request_scan_summary(req: RadPrescriptionRequest) -> str:
    return ", ".join(i.scan_code for i in (req.items or []))


def pending_fulfillable_items(
    req: RadPrescriptionRequest,
) -> list[RadPrescriptionRequestItem]:
    return [i for i in (req.items or []) if i.status == RadRequestItemStatus.pending]


def resolve_rad_selection(
    db: Session,
    hospital_id: UUID,
    scan_ids: list[UUID] | None,
) -> list[RadiologyScanCatalog]:
    """Resolve scan_ids against the active hospital catalogue, strict."""
    unique_ids = list(dict.fromkeys(scan_ids or []))
    if not unique_ids:
        return []
    scans = (
        db.query(RadiologyScanCatalog)
        .filter(
            RadiologyScanCatalog.hospital_id == hospital_id,
            RadiologyScanCatalog.id.in_(unique_ids),
            RadiologyScanCatalog.is_active.is_(True),
        )
        .all()
    )
    by_id = {s.id: s for s in scans}
    if len(by_id) != len(unique_ids):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="One or more scans are invalid or inactive",
        )
    return [by_id[sid] for sid in unique_ids]


def assert_request_fulfillable(
    db: Session, req: RadPrescriptionRequest
) -> list[RadPrescriptionRequestItem]:
    """Pending scans of the request that can be accessioned now.

    A multi-scan request is fulfilled by one order per scan; cancelling one of them
    returns only that scan to pending, so its siblings being live does not block
    re-accessioning it. A scan that already has a live order is never accessioned twice.
    """
    if req.status == RadPrescriptionRequestStatus.cancelled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This prescription request was cancelled",
        )
    if req.status == RadPrescriptionRequestStatus.completed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This prescription has already been fulfilled",
        )

    live = rad_request_live_orders(db, req)
    live_scans = {o.scan_id for o in live}
    pending = pending_fulfillable_items(req)
    items = [i for i in pending if i.scan_id not in live_scans]
    if not items:
        if live:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Prescription already attached to order {live[0].order_no}",
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No pending scans remain on this prescription (all unavailable or already ordered)",
        )
    return items


def recompute_rad_request_status(db: Session, req: RadPrescriptionRequest) -> None:
    """Derive the request status from its items and the orders fulfilling it."""
    if req.status == RadPrescriptionRequestStatus.cancelled:
        return
    items = req.items or []
    pending = [i for i in items if i.status == RadRequestItemStatus.pending]
    ordered = [i for i in items if i.status == RadRequestItemStatus.ordered]
    live = rad_request_live_orders(db, req)
    if not pending and not ordered and not live:
        req.status = RadPrescriptionRequestStatus.cancelled
        req.cancel_reason = req.cancel_reason or "No requested scans remain"
    elif not pending and live and all(o.status == RadiologyOrderStatus.completed for o in live):
        req.status = RadPrescriptionRequestStatus.completed
    elif live:
        req.status = RadPrescriptionRequestStatus.partially_processed
    else:
        req.status = RadPrescriptionRequestStatus.pending


def sync_request_after_order_change(db: Session, order: RadiologyOrder) -> None:
    if not order.prescription_request_id:
        return
    req = (
        db.query(RadPrescriptionRequest)
        .options(joinedload(RadPrescriptionRequest.items))
        .filter(RadPrescriptionRequest.id == order.prescription_request_id)
        .first()
    )
    if not req or req.status == RadPrescriptionRequestStatus.cancelled:
        return
    db.flush()

    if order.status == RadiologyOrderStatus.cancelled:
        # Only the cancelled order's scan returns to pending (unless another live
        # order already covers that scan).
        live_scans = {o.scan_id for o in rad_request_live_orders(db, req)}
        if order.scan_id not in live_scans:
            for item in req.items or []:
                if item.status == RadRequestItemStatus.ordered and item.scan_id == order.scan_id:
                    item.status = RadRequestItemStatus.pending
                    break
    recompute_rad_request_status(db, req)


# ── Canonical request charge ─────────────────────────────────────────────────
# One charge per request (source_id = request.id) billing its pending + ordered scans;
# the per-scan orders resolve it through ``service_charge_resolver``.

BILLABLE_REQUEST_ITEM_STATUSES = frozenset({RadRequestItemStatus.pending, RadRequestItemStatus.ordered})


def rad_request_billable_items(req: RadPrescriptionRequest) -> list[RadPrescriptionRequestItem]:
    return [i for i in (req.items or []) if i.status in BILLABLE_REQUEST_ITEM_STATUSES]


def rad_request_billable_total(req: RadPrescriptionRequest) -> float:
    return round(sum(float(i.price or 0.0) for i in rad_request_billable_items(req)), 2)


def rad_request_charge_description(req: RadPrescriptionRequest) -> str:
    names = [i.scan_name for i in rad_request_billable_items(req) if i.scan_name]
    return f"Radiology Investigation — {', '.join(names)}"[:512]


def rad_request_account_id(db: Session, req: RadPrescriptionRequest, created_by_name: str = "Prescription Order") -> UUID | None:
    from modules.billing.services.episode_account_service import resolve_episode_account

    return resolve_episode_account(
        db,
        req.hospital_id,
        req.patient_id,
        admission_id=req.admission_id,
        created_by_name=created_by_name,
    ).account_id


def ensure_rad_request_charge(db: Session, req: RadPrescriptionRequest, *, created_by_name: str = "Prescription Order"):
    """Create the request's canonical charge if it has no active one (create-only)."""
    from modules.billing.entities.billing_entities import BillingSourceType
    from modules.billing.services.service_charge_resolver import (
        ensure_service_charge,
        resolve_active_service_charge,
    )

    existing = resolve_active_service_charge(db, req.hospital_id, BillingSourceType.radiology, req.id)
    if existing is not None:
        return existing
    if not rad_request_billable_items(req):
        return None
    return ensure_service_charge(
        db,
        hospital_id=req.hospital_id,
        patient_id=req.patient_id,
        source_type=BillingSourceType.radiology,
        source_id=req.id,
        description=rad_request_charge_description(req),
        charge_amount=rad_request_billable_total(req),
        account_id=rad_request_account_id(db, req, created_by_name),
        created_by_name=created_by_name,
    )


def sync_rad_request_charge(
    db: Session,
    req: RadPrescriptionRequest,
    *,
    reason: str,
    actor: dict | None = None,
    created_by_name: str = "Prescription Order",
):
    """Re-price the request's canonical charge to its billable scans (create if missing,
    cancel canonically when nothing billable remains)."""
    from modules.billing.entities.billing_entities import BillingSourceType
    from modules.billing.services.service_charge_resolver import sync_service_charge_amount

    db.flush()
    db.refresh(req, attribute_names=["items"])
    return sync_service_charge_amount(
        db,
        hospital_id=req.hospital_id,
        patient_id=req.patient_id,
        source_type=BillingSourceType.radiology,
        source_id=req.id,
        charge_amount=rad_request_billable_total(req),
        description=rad_request_charge_description(req),
        reason=reason,
        actor=actor,
        account_id=rad_request_account_id(db, req, created_by_name),
        created_by_name=created_by_name,
    )


def rad_request_live_orders(db: Session, req: RadPrescriptionRequest) -> list[RadiologyOrder]:
    """Non-cancelled (open or completed) orders fulfilling this request, newest first."""
    return (
        db.query(RadiologyOrder)
        .filter(
            RadiologyOrder.hospital_id == req.hospital_id,
            RadiologyOrder.prescription_request_id == req.id,
            RadiologyOrder.status != RadiologyOrderStatus.cancelled,
        )
        .order_by(RadiologyOrder.ordered_at.desc())
        .all()
    )


def assert_rad_request_cancellable(db: Session, req: RadPrescriptionRequest) -> None:
    live = rad_request_live_orders(db, req)
    if live and req.status != RadPrescriptionRequestStatus.cancelled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Radiology request is being fulfilled by order {live[0].order_no} ({live[0].status.value}). "
                "Cancel its orders first, or mark individual pending scans unavailable."
            ),
        )


def is_rad_request_released(db: Session, req: RadPrescriptionRequest) -> bool:
    """Check if a RadPrescriptionRequest is operationally released to the radiology queue."""
    if req.status == RadPrescriptionRequestStatus.cancelled:
        return False
    if bool(getattr(req, "is_emergency", False)):
        return True
    from modules.billing.entities.billing_entities import BillingSourceType
    from modules.billing.services.service_financial_clearance import check_service_financial_clearance
    fin = check_service_financial_clearance(db, req.hospital_id, BillingSourceType.radiology, req.id)
    if fin.is_cleared:
        return True
    from modules.billing.entities.financial_exception import FinancialClearanceException, FinancialExceptionStatus
    exc = db.query(FinancialClearanceException).filter(
        FinancialClearanceException.hospital_id == req.hospital_id,
        FinancialClearanceException.source_id == req.id,
        FinancialClearanceException.status == FinancialExceptionStatus.approved.value,
        FinancialClearanceException.is_consumed == False,
    ).first()
    return bool(exc)


def request_to_response_dict(
    req: RadPrescriptionRequest,
    db: Session | None = None,
    financial_state: Any | None = None,
    exception_cleared: bool | None = None,
) -> dict[str, Any]:
    items = sorted(req.items or [], key=lambda i: (i.sort_order, str(i.id)))
    payment_status = "pending"
    is_financially_cleared = False
    outstanding_amount = 0.0

    if financial_state is not None:
        payment_status = getattr(financial_state, "status", "pending")
        is_em = bool(getattr(req, "is_emergency", False))
        is_fin_cleared = bool(getattr(financial_state, "is_cleared", False))
        is_financially_cleared = is_fin_cleared or is_em or bool(exception_cleared)
        outstanding_amount = float(getattr(financial_state, "outstanding_amount", 0.0) or 0.0)
    else:
        target_db = db or (req._sa_instance_state.session if hasattr(req, "_sa_instance_state") else None)
        if target_db:
            try:
                from modules.billing.entities.billing_entities import BillingSourceType
                from modules.billing.services.service_financial_clearance import check_service_financial_clearance
                fin = check_service_financial_clearance(target_db, req.hospital_id, BillingSourceType.radiology, req.id)
                payment_status = fin.status
                is_em = bool(getattr(req, "is_emergency", False))
                is_financially_cleared = fin.is_cleared or is_em
                if not is_financially_cleared:
                    from modules.billing.entities.financial_exception import FinancialClearanceException, FinancialExceptionStatus
                    exc = target_db.query(FinancialClearanceException).filter(
                        FinancialClearanceException.hospital_id == req.hospital_id,
                        FinancialClearanceException.source_id == req.id,
                        FinancialClearanceException.status == FinancialExceptionStatus.approved.value,
                        FinancialClearanceException.is_consumed == False,
                    ).first()
                    if exc:
                        is_financially_cleared = True
                outstanding_amount = fin.outstanding_amount
            except Exception:
                pass

    return {
        "id": req.id,
        "hospital_id": req.hospital_id,
        "prescription_id": req.prescription_id,
        "patient_id": req.patient_id,
        "doctor_id": req.doctor_id,
        "appointment_id": req.appointment_id,
        "admission_id": getattr(req, "admission_id", None),
        "status": req.status,
        "prescribed_scan_ids": [str(sid) for sid in (req.prescribed_scan_ids or [])],
        "clinical_notes": req.clinical_notes,
        "cancel_reason": req.cancel_reason,
        "created_at": req.created_at,
        "updated_at": req.updated_at,
        "patient_name": req.patient.name if req.patient else None,
        "patient_uhid": req.patient.uhid if req.patient else None,
        "doctor_name": req.doctor.name if req.doctor else None,
        "scan_names": request_scan_summary(req) or None,
        "scan_count": len(items),
        "pending_scan_count": len(
            [i for i in items if i.status == RadRequestItemStatus.pending]
        ),
        "appointment_label": None,
        "is_financially_cleared": is_financially_cleared,
        "payment_status": payment_status,
        "outstanding_amount": outstanding_amount,
        "items": [
            {
                "id": i.id,
                "scan_id": i.scan_id,
                "scan_code": i.scan_code,
                "scan_name": i.scan_name,
                "category": i.category,
                "price": i.price,
                "sort_order": i.sort_order,
                "status": i.status,
            }
            for i in items
        ],
    }
