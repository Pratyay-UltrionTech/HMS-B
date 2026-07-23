"""Helpers for doctor-prescribed lab requests and fulfillment."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, joinedload

from app.models import (
    LabOrder,
    LabOrderStatus,
    LabPrescriptionRequest,
    LabPrescriptionRequestItem,
    LabPrescriptionRequestStatus,
    LabRequestItemStatus,
    LabSampleType,
)


OPEN_ORDER_STATUSES = {
    LabOrderStatus.ordered,
    LabOrderStatus.sample_collected,
    LabOrderStatus.in_progress,
}

ACTIVE_REQUEST_STATUSES = {
    LabPrescriptionRequestStatus.pending,
    LabPrescriptionRequestStatus.partially_processed,
}


def get_prescription_request(
    db: Session, request_id: UUID, hospital_id: UUID
) -> LabPrescriptionRequest:
    req = (
        db.query(LabPrescriptionRequest)
        .options(
            joinedload(LabPrescriptionRequest.patient),
            joinedload(LabPrescriptionRequest.doctor),
            joinedload(LabPrescriptionRequest.items),
            joinedload(LabPrescriptionRequest.prescription),
        )
        .filter(
            LabPrescriptionRequest.id == request_id,
            LabPrescriptionRequest.hospital_id == hospital_id,
        )
        .first()
    )
    if not req:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Prescription lab request not found")
    return req


def request_panel_names(req: LabPrescriptionRequest) -> list[str]:
    return sorted({i.panel_name for i in (req.items or []) if i.panel_name})


def request_test_summary(req: LabPrescriptionRequest) -> str:
    return ", ".join(i.test_code for i in (req.items or []))


def pending_fulfillable_items(req: LabPrescriptionRequest) -> list[LabPrescriptionRequestItem]:
    return [i for i in (req.items or []) if i.status == LabRequestItemStatus.pending]


def prefer_sample_type_from_items(items: list[LabPrescriptionRequestItem]) -> LabSampleType | None:
    if not items:
        return None
    # Default blood if any item is from blood panel context — sample type not on request item;
    # callers may override from catalogue. Use blood as practical default for panels.
    return LabSampleType.blood


def assert_request_fulfillable(db: Session, req: LabPrescriptionRequest) -> list[LabPrescriptionRequestItem]:
    if req.status == LabPrescriptionRequestStatus.cancelled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This prescription request was cancelled")
    if req.status == LabPrescriptionRequestStatus.completed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This prescription has already been fulfilled",
        )

    if req.lab_order_id:
        existing = (
            db.query(LabOrder)
            .filter(LabOrder.id == req.lab_order_id, LabOrder.hospital_id == req.hospital_id)
            .first()
        )
        if existing and existing.status != LabOrderStatus.cancelled:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Prescription already attached to active order {existing.order_no}",
            )

    active = (
        db.query(LabOrder)
        .filter(
            LabOrder.hospital_id == req.hospital_id,
            LabOrder.prescription_request_id == req.id,
            LabOrder.status.in_(list(OPEN_ORDER_STATUSES) + [LabOrderStatus.completed]),
        )
        .first()
    )
    if active and active.status != LabOrderStatus.cancelled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Prescription already attached to order {active.order_no}",
        )

    items = pending_fulfillable_items(req)
    if not items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No pending tests remain on this prescription (all unavailable or already ordered)",
        )
    return items


def sync_request_after_order_change(db: Session, order: LabOrder) -> None:
    if not order.prescription_request_id:
        return
    req = (
        db.query(LabPrescriptionRequest)
        .options(joinedload(LabPrescriptionRequest.items))
        .filter(LabPrescriptionRequest.id == order.prescription_request_id)
        .first()
    )
    if not req or req.status == LabPrescriptionRequestStatus.cancelled:
        return

    if order.status == LabOrderStatus.cancelled:
        req.lab_order_id = None
        for item in req.items or []:
            if item.status == LabRequestItemStatus.ordered:
                item.status = LabRequestItemStatus.pending
        # If any pending remain → pending; else if all unavailable → cancelled
        pending = [i for i in (req.items or []) if i.status == LabRequestItemStatus.pending]
        unavailable = [i for i in (req.items or []) if i.status == LabRequestItemStatus.unavailable]
        if pending:
            req.status = LabPrescriptionRequestStatus.pending
        elif unavailable and len(unavailable) == len(req.items or []):
            req.status = LabPrescriptionRequestStatus.cancelled
        else:
            req.status = LabPrescriptionRequestStatus.pending
        return

    req.lab_order_id = order.id
    if order.status == LabOrderStatus.completed:
        req.status = LabPrescriptionRequestStatus.completed
        for item in req.items or []:
            if item.status == LabRequestItemStatus.ordered:
                pass  # keep ordered
    else:
        req.status = LabPrescriptionRequestStatus.partially_processed


def request_to_response_dict(req: LabPrescriptionRequest) -> dict:
    items = sorted(req.items or [], key=lambda i: (i.sort_order, str(i.id)))
    panels = request_panel_names(req)
    appointment_label = None
    # appointment details filled by caller if needed
    return {
        "id": req.id,
        "hospital_id": req.hospital_id,
        "prescription_id": req.prescription_id,
        "patient_id": req.patient_id,
        "doctor_id": req.doctor_id,
        "appointment_id": req.appointment_id,
        "status": req.status,
        "prescribed_test_ids": [UUID(x) if not isinstance(x, UUID) else x for x in (req.prescribed_test_ids or [])],
        "prescribed_panel_ids": [UUID(x) if not isinstance(x, UUID) else x for x in (req.prescribed_panel_ids or [])],
        "clinical_notes": req.clinical_notes,
        "cancel_reason": req.cancel_reason,
        "lab_order_id": req.lab_order_id,
        "created_at": req.created_at,
        "updated_at": req.updated_at,
        "patient_name": req.patient.name if req.patient else None,
        "patient_uhid": req.patient.uhid if req.patient else None,
        "doctor_name": req.doctor.name if req.doctor else None,
        "panel_names": ", ".join(panels) if panels else None,
        "test_names": request_test_summary(req) or None,
        "test_count": len(items),
        "pending_test_count": len([i for i in items if i.status == LabRequestItemStatus.pending]),
        "appointment_label": appointment_label,
        "items": [
            {
                "id": i.id,
                "test_id": i.test_id,
                "panel_id": i.panel_id,
                "panel_name": i.panel_name,
                "test_code": i.test_code,
                "test_name": i.test_name,
                "department": i.department,
                "price": i.price,
                "sort_order": i.sort_order,
                "status": i.status,
            }
            for i in items
        ],
    }
