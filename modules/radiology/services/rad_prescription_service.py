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


def request_scan_summary(req: RadPrescriptionRequest) -> str:
    return ", ".join(i.scan_code for i in (req.items or []))


def pending_fulfillable_items(
    req: RadPrescriptionRequest,
) -> list[RadPrescriptionRequestItem]:
    return [i for i in (req.items or []) if i.status == RadRequestItemStatus.pending]


def assert_request_fulfillable(
    db: Session, req: RadPrescriptionRequest
) -> list[RadPrescriptionRequestItem]:
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

    active = (
        db.query(RadiologyOrder)
        .filter(
            RadiologyOrder.hospital_id == req.hospital_id,
            RadiologyOrder.prescription_request_id == req.id,
            RadiologyOrder.status.in_(list(OPEN_ORDER_STATUSES) + [RadiologyOrderStatus.completed]),
        )
        .first()
    )
    if active and active.status != RadiologyOrderStatus.cancelled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Prescription already attached to order {active.order_no}",
        )

    items = pending_fulfillable_items(req)
    if not items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No pending scans remain on this prescription (all unavailable or already ordered)",
        )
    return items


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

    if order.status == RadiologyOrderStatus.cancelled:
        for item in req.items or []:
            if item.status == RadRequestItemStatus.ordered:
                item.status = RadRequestItemStatus.pending
        pending = [i for i in (req.items or []) if i.status == RadRequestItemStatus.pending]
        unavailable = [i for i in (req.items or []) if i.status == RadRequestItemStatus.unavailable]
        if pending:
            req.status = RadPrescriptionRequestStatus.pending
        elif unavailable and len(unavailable) == len(req.items or []):
            req.status = RadPrescriptionRequestStatus.cancelled
        else:
            req.status = RadPrescriptionRequestStatus.pending
        return

    if order.status == RadiologyOrderStatus.completed:
        siblings = (
            db.query(RadiologyOrder)
            .filter(
                RadiologyOrder.hospital_id == req.hospital_id,
                RadiologyOrder.prescription_request_id == req.id,
                RadiologyOrder.status != RadiologyOrderStatus.cancelled,
            )
            .all()
        )
        if siblings and all(s.status == RadiologyOrderStatus.completed for s in siblings):
            req.status = RadPrescriptionRequestStatus.completed
            return
    req.status = RadPrescriptionRequestStatus.partially_processed


def request_to_response_dict(req: RadPrescriptionRequest) -> dict[str, Any]:
    items = sorted(req.items or [], key=lambda i: (i.sort_order, str(i.id)))
    return {
        "id": req.id,
        "hospital_id": req.hospital_id,
        "prescription_id": req.prescription_id,
        "patient_id": req.patient_id,
        "doctor_id": req.doctor_id,
        "appointment_id": req.appointment_id,
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
