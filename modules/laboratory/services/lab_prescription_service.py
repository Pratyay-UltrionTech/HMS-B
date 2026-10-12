"""
Laboratory prescription requests fulfillment and cross-domain sync service.

Handles doctor-prescribed laboratory investigation workflows, fulfillment validations,
and lifecycle synchronization.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, joinedload

from modules.laboratory.entities.lab_entities import (
    LabOrder,
    LabOrderItem,
    LabOrderStatus,
    LabPrescriptionRequest,
    LabPrescriptionRequestItem,
    LabPrescriptionRequestStatus,
    LabRequestItemStatus,
    LabSampleType,
)
from modules.laboratory.services.lab_panels_service import (
    prefer_sample_type,
    resolve_lab_selection,
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prescription lab request not found",
        )
    return req


def apply_cancel_lab_prescription_request(
    db: Session,
    request_id: UUID,
    hospital_id: UUID,
    reason: str | None,
) -> LabPrescriptionRequest:
    """Cancel a lab prescription request in the current session without committing."""
    req = get_prescription_request(db, request_id, hospital_id)
    if req.status == LabPrescriptionRequestStatus.cancelled:
        return req
    if req.status == LabPrescriptionRequestStatus.completed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Completed prescription requests cannot be cancelled",
        )
    assert_lab_request_cancellable(db, req)
    req.status = LabPrescriptionRequestStatus.cancelled
    req.cancel_reason = reason.strip() if reason else "Cancelled by staff"
    for item in req.items or []:
        if item.status == LabRequestItemStatus.pending:
            item.status = LabRequestItemStatus.cancelled
    return req


def request_panel_names(req: LabPrescriptionRequest) -> list[str]:
    return sorted({i.panel_name for i in (req.items or []) if i.panel_name})


def request_test_summary(req: LabPrescriptionRequest) -> str:
    return ", ".join(i.test_code for i in (req.items or []))


def pending_fulfillable_items(
    req: LabPrescriptionRequest,
) -> list[LabPrescriptionRequestItem]:
    return [i for i in (req.items or []) if i.status == LabRequestItemStatus.pending]


def assert_request_fulfillable(
    db: Session, req: LabPrescriptionRequest
) -> list[LabPrescriptionRequestItem]:
    if req.status == LabPrescriptionRequestStatus.cancelled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This prescription request was cancelled",
        )
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
                pass
    else:
        req.status = LabPrescriptionRequestStatus.partially_processed


# ── Canonical request charge ─────────────────────────────────────────────────
# A prescription request owns exactly one charge (source_id = request.id), billing
# its pending + ordered items. Orders created from the request resolve that charge
# through ``service_charge_resolver``; the key is never re-pointed.

BILLABLE_REQUEST_ITEM_STATUSES = frozenset({LabRequestItemStatus.pending, LabRequestItemStatus.ordered})


def lab_request_billable_items(req: LabPrescriptionRequest) -> list[LabPrescriptionRequestItem]:
    return [i for i in (req.items or []) if i.status in BILLABLE_REQUEST_ITEM_STATUSES]


def lab_request_billable_total(req: LabPrescriptionRequest) -> float:
    # Panel prices are not applied: a panel bills the sum of its member test prices.
    return round(sum(float(i.price or 0.0) for i in lab_request_billable_items(req)), 2)


def lab_request_charge_description(req: LabPrescriptionRequest) -> str:
    names = [i.test_name for i in lab_request_billable_items(req) if i.test_name]
    return f"Lab Investigation — {', '.join(names)}"[:512]


def lab_request_account_id(db: Session, req: LabPrescriptionRequest, created_by_name: str = "Prescription Order") -> UUID | None:
    from modules.billing.services.episode_account_service import resolve_episode_account

    return resolve_episode_account(
        db,
        req.hospital_id,
        req.patient_id,
        admission_id=req.admission_id,
        created_by_name=created_by_name,
    ).account_id


def ensure_lab_request_charge(db: Session, req: LabPrescriptionRequest, *, created_by_name: str = "Prescription Order"):
    """Create the request's canonical charge if it has no active one (create-only)."""
    from modules.billing.entities.billing_entities import BillingSourceType
    from modules.billing.services.service_charge_resolver import (
        ensure_service_charge,
        resolve_active_service_charge,
    )

    existing = resolve_active_service_charge(db, req.hospital_id, BillingSourceType.laboratory, req.id)
    if existing is not None:
        return existing
    total = lab_request_billable_total(req)
    if total <= 0 and not lab_request_billable_items(req):
        return None
    return ensure_service_charge(
        db,
        hospital_id=req.hospital_id,
        patient_id=req.patient_id,
        source_type=BillingSourceType.laboratory,
        source_id=req.id,
        description=lab_request_charge_description(req),
        charge_amount=total,
        account_id=lab_request_account_id(db, req, created_by_name),
        created_by_name=created_by_name,
    )


def sync_lab_request_charge(
    db: Session,
    req: LabPrescriptionRequest,
    *,
    reason: str,
    actor: dict | None = None,
    created_by_name: str = "Prescription Order",
):
    """Re-price the request's single canonical charge to its billable items (create it
    if missing, cancel it canonically when nothing billable remains)."""
    from modules.billing.entities.billing_entities import BillingSourceType
    from modules.billing.services.service_charge_resolver import sync_service_charge_amount

    db.flush()
    db.refresh(req, attribute_names=["items"])
    return sync_service_charge_amount(
        db,
        hospital_id=req.hospital_id,
        patient_id=req.patient_id,
        source_type=BillingSourceType.laboratory,
        source_id=req.id,
        charge_amount=lab_request_billable_total(req),
        description=lab_request_charge_description(req),
        reason=reason,
        actor=actor,
        account_id=lab_request_account_id(db, req, created_by_name),
        created_by_name=created_by_name,
    )


def lab_request_live_order(db: Session, req: LabPrescriptionRequest) -> LabOrder | None:
    """A non-cancelled (open or completed) order fulfilling this request, if any."""
    return (
        db.query(LabOrder)
        .filter(
            LabOrder.hospital_id == req.hospital_id,
            LabOrder.prescription_request_id == req.id,
            LabOrder.status != LabOrderStatus.cancelled,
        )
        .order_by(LabOrder.ordered_at.desc())
        .first()
    )


def assert_lab_request_cancellable(db: Session, req: LabPrescriptionRequest) -> None:
    """A request whose order is still live can't be cancelled as a whole: its charge
    also bills that order. Cancel the order first (the request returns to pending)."""
    live = lab_request_live_order(db, req)
    if live is not None and req.status != LabPrescriptionRequestStatus.cancelled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Lab request is being fulfilled by order {live.order_no} ({live.status.value}). "
                "Cancel the order first, or mark individual pending tests unavailable."
            ),
        )


def is_lab_request_released(db: Session, req: LabPrescriptionRequest) -> bool:
    """Check if a LabPrescriptionRequest is operationally released to the lab queue."""
    if req.status == LabPrescriptionRequestStatus.cancelled:
        return False
    if bool(getattr(req, "is_emergency", False)):
        return True
    from modules.billing.entities.billing_entities import BillingSourceType
    from modules.billing.services.service_financial_clearance import check_service_financial_clearance
    fin = check_service_financial_clearance(db, req.hospital_id, BillingSourceType.laboratory, req.id)
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
    req: LabPrescriptionRequest,
    db: Session | None = None,
    financial_state: Any | None = None,
    exception_cleared: bool | None = None,
) -> dict[str, Any]:
    items = sorted(req.items or [], key=lambda i: (i.sort_order, str(i.id)))
    panels = request_panel_names(req)
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
                fin = check_service_financial_clearance(target_db, req.hospital_id, BillingSourceType.laboratory, req.id)
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
        "prescribed_test_ids": [
            UUID(x) if not isinstance(x, UUID) else x
            for x in (req.prescribed_test_ids or [])
        ],
        "prescribed_panel_ids": [
            UUID(x) if not isinstance(x, UUID) else x
            for x in (req.prescribed_panel_ids or [])
        ],
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
        "pending_test_count": len(
            [i for i in items if i.status == LabRequestItemStatus.pending]
        ),
        "appointment_label": None,
        "is_financially_cleared": is_financially_cleared,
        "payment_status": payment_status,
        "outstanding_amount": outstanding_amount,
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


def _new_lab_request(
    db: Session,
    *,
    hospital_id: UUID,
    prescription_id: UUID,
    patient_id: UUID,
    doctor_id: UUID,
    appointment_id: UUID | None,
    admission_id: UUID | None,
    tests_resolved: list,
    test_ids: list[UUID],
    panel_ids: list[UUID],
    clinical_notes: str | None,
) -> LabPrescriptionRequest:
    lab_req = LabPrescriptionRequest(
        hospital_id=hospital_id,
        prescription_id=prescription_id,
        patient_id=patient_id,
        doctor_id=doctor_id,
        appointment_id=appointment_id,
        admission_id=admission_id,
        status=LabPrescriptionRequestStatus.pending,
        prescribed_test_ids=[str(tid) for tid in test_ids],
        prescribed_panel_ids=[str(pid) for pid in panel_ids],
        clinical_notes=clinical_notes,
    )
    db.add(lab_req)
    db.flush()
    for idx, r in enumerate(tests_resolved):
        t = r.test
        db.add(
            LabPrescriptionRequestItem(
                hospital_id=hospital_id,
                request_id=lab_req.id,
                test_id=t.id,
                panel_id=r.panel.id if r.panel else None,
                panel_name=r.panel.panel_name if r.panel else None,
                test_code=t.test_code,
                test_name=t.test_name,
                department=t.department,
                price=t.price,
                sort_order=idx,
                status=LabRequestItemStatus.pending,
            )
        )
    db.flush()
    db.refresh(lab_req, attribute_names=["items"])
    ensure_lab_request_charge(db, lab_req)
    return lab_req


def _new_rad_request(
    db: Session,
    *,
    hospital_id: UUID,
    prescription_id: UUID,
    patient_id: UUID,
    doctor_id: UUID,
    appointment_id: UUID | None,
    admission_id: UUID | None,
    scans_resolved: list,
    clinical_notes: str | None,
):
    from modules.radiology.entities.radiology_entities import (
        RadPrescriptionRequest,
        RadPrescriptionRequestItem,
        RadPrescriptionRequestStatus,
        RadRequestItemStatus,
    )
    from modules.radiology.services.rad_prescription_service import ensure_rad_request_charge

    rad_req = RadPrescriptionRequest(
        hospital_id=hospital_id,
        prescription_id=prescription_id,
        patient_id=patient_id,
        doctor_id=doctor_id,
        appointment_id=appointment_id,
        admission_id=admission_id,
        status=RadPrescriptionRequestStatus.pending,
        prescribed_scan_ids=[str(s.id) for s in scans_resolved],
        clinical_notes=clinical_notes,
    )
    db.add(rad_req)
    db.flush()
    for idx, s in enumerate(scans_resolved):
        db.add(
            RadPrescriptionRequestItem(
                hospital_id=hospital_id,
                request_id=rad_req.id,
                scan_id=s.id,
                scan_code=s.scan_code,
                scan_name=s.scan_name,
                category=s.category,
                price=s.price,
                sort_order=idx,
                status=RadRequestItemStatus.pending,
            )
        )
    db.flush()
    db.refresh(rad_req, attribute_names=["items"])
    ensure_rad_request_charge(db, rad_req)
    return rad_req


def create_investigation_requests_for_prescription(
    db: Session,
    *,
    hospital_id: UUID,
    doctor_id: UUID,
    patient_id: UUID,
    appointment_id: UUID | None,
    prescription_id: UUID,
    admission_id: UUID | None = None,
    test_ids: list[UUID] | None = None,
    panel_ids: list[UUID] | None = None,
    scan_ids: list[UUID] | None = None,
    clinical_notes: str | None = None,
) -> tuple[LabPrescriptionRequest | None, Any | None]:
    """
    Create lab / radiology investigation requests when a doctor prescribes tests.

    Each request gets its single canonical BillingCharge (source_id = request.id) so the
    investigation enters the billing queue immediately. Work belonging to an admission
    (explicit ``admission_id``, or an appointment transferred into one) is linked to the
    admission and billed on its IPD FinancialAccount.
    """
    from modules.billing.services.episode_account_service import resolve_episode_admission_id

    episode_admission_id = None
    if test_ids or panel_ids or scan_ids:
        episode_admission_id = resolve_episode_admission_id(
            db, hospital_id, patient_id, admission_id=admission_id, appointment_id=appointment_id
        )

    lab_req = None
    if test_ids or panel_ids:
        tests_resolved = resolve_lab_selection(
            db, hospital_id, test_ids, panel_ids, require_non_empty=False
        )
        if tests_resolved:
            lab_req = _new_lab_request(
                db,
                hospital_id=hospital_id,
                prescription_id=prescription_id,
                patient_id=patient_id,
                doctor_id=doctor_id,
                appointment_id=appointment_id,
                admission_id=episode_admission_id,
                tests_resolved=tests_resolved,
                test_ids=list(test_ids or []),
                panel_ids=list(panel_ids or []),
                clinical_notes=clinical_notes,
            )

    rad_req = None
    if scan_ids:
        from modules.radiology.services.rad_prescription_service import resolve_rad_selection

        scans_resolved = resolve_rad_selection(db, hospital_id, scan_ids)
        if scans_resolved:
            rad_req = _new_rad_request(
                db,
                hospital_id=hospital_id,
                prescription_id=prescription_id,
                patient_id=patient_id,
                doctor_id=doctor_id,
                appointment_id=appointment_id,
                admission_id=episode_admission_id,
                scans_resolved=scans_resolved,
                clinical_notes=clinical_notes,
            )

    return lab_req, rad_req


def prescription_investigation_names(
    db: Session, rx: Any
) -> tuple[list[str], list[str]]:
    """Resolve lab and radiology test/scan names ordered with this prescription."""
    lab_names: list[str] = []
    rad_names: list[str] = []

    reqs = (
        db.query(LabPrescriptionRequest)
        .options(joinedload(LabPrescriptionRequest.items))
        .filter(LabPrescriptionRequest.prescription_id == rx.id)
        .all()
    )
    for req in reqs:
        for item in req.items or []:
            if item.test_name and item.test_name not in lab_names:
                lab_names.append(item.test_name)

    lab_orders = (
        db.query(LabOrder)
        .options(joinedload(LabOrder.items))
        .filter(
            LabOrder.hospital_id == rx.hospital_id,
            LabOrder.patient_id == rx.patient_id,
            LabOrder.doctor_id == rx.doctor_id,
        )
    )
    if rx.appointment_id:
        lab_orders = lab_orders.filter(LabOrder.appointment_id == rx.appointment_id)
    for order in lab_orders.all():
        for item in order.items or []:
            if item.test_name and item.test_name not in lab_names:
                lab_names.append(item.test_name)

    # Radiology investigations
    try:
        from modules.radiology.entities.radiology_entities import (
            RadiologyOrder,
            RadPrescriptionRequest,
        )

        rad_reqs = (
            db.query(RadPrescriptionRequest)
            .options(joinedload(RadPrescriptionRequest.items))
            .filter(RadPrescriptionRequest.prescription_id == rx.id)
            .all()
        )
        for req in rad_reqs:
            for item in req.items or []:
                if item.scan_name and item.scan_name not in rad_names:
                    rad_names.append(item.scan_name)

        rad_orders = (
            db.query(RadiologyOrder)
            .filter(
                RadiologyOrder.hospital_id == rx.hospital_id,
                RadiologyOrder.patient_id == rx.patient_id,
            )
        )
        if hasattr(RadiologyOrder, "prescription_id"):
            rad_rx_orders = rad_orders.filter(RadiologyOrder.prescription_id == rx.id).all()
            for order in rad_rx_orders:
                if order.scan_name and order.scan_name not in rad_names:
                    rad_names.append(order.scan_name)
        if rx.appointment_id:
            rad_appt_orders = (
                rad_orders.filter(
                    RadiologyOrder.appointment_id == rx.appointment_id,
                    RadiologyOrder.doctor_id == rx.doctor_id,
                ).all()
            )
            for order in rad_appt_orders:
                if order.scan_name and order.scan_name not in rad_names:
                    rad_names.append(order.scan_name)
    except Exception:
        pass

    return lab_names, rad_names


def _parse_uuid_list(values) -> list[UUID]:
    out: list[UUID] = []
    for v in values or []:
        try:
            out.append(UUID(str(v)))
        except (TypeError, ValueError):
            continue
    return out


def get_prescription_investigation_ids(
    db: Session, rx_id: UUID
) -> tuple[list[UUID], list[UUID], list[UUID]]:
    """Current test_ids, panel_ids and scan_ids across every live request of a prescription
    (an edit can add a supplementary request next to one that is already in the lab)."""
    from modules.radiology.entities.radiology_entities import (
        RadPrescriptionRequest,
        RadPrescriptionRequestStatus,
    )

    test_ids: list[UUID] = []
    panel_ids: list[UUID] = []
    scan_ids: list[UUID] = []

    lab_reqs = (
        db.query(LabPrescriptionRequest)
        .filter(
            LabPrescriptionRequest.prescription_id == rx_id,
            LabPrescriptionRequest.status != LabPrescriptionRequestStatus.cancelled,
        )
        .order_by(LabPrescriptionRequest.created_at.asc())
        .all()
    )
    for req in lab_reqs:
        for tid in _parse_uuid_list(req.prescribed_test_ids):
            if tid not in test_ids:
                test_ids.append(tid)
        for pid in _parse_uuid_list(req.prescribed_panel_ids):
            if pid not in panel_ids:
                panel_ids.append(pid)

    rad_reqs = (
        db.query(RadPrescriptionRequest)
        .filter(
            RadPrescriptionRequest.prescription_id == rx_id,
            RadPrescriptionRequest.status != RadPrescriptionRequestStatus.cancelled,
        )
        .order_by(RadPrescriptionRequest.created_at.asc())
        .all()
    )
    for req in rad_reqs:
        for sid in _parse_uuid_list(req.prescribed_scan_ids):
            if sid not in scan_ids:
                scan_ids.append(sid)

    return test_ids, panel_ids, scan_ids


def _refresh_lab_prescribed_ids(req: LabPrescriptionRequest) -> None:
    items = [i for i in (req.items or []) if i.status != LabRequestItemStatus.cancelled]
    req.prescribed_test_ids = list(dict.fromkeys(str(i.test_id) for i in items if i.panel_id is None))
    req.prescribed_panel_ids = list(dict.fromkeys(str(i.panel_id) for i in items if i.panel_id is not None))


def _update_lab_requests_for_prescription(
    db: Session,
    *,
    hospital_id: UUID,
    doctor_id: UUID,
    patient_id: UUID,
    appointment_id: UUID | None,
    admission_id: UUID | None,
    prescription_id: UUID,
    test_ids: list[UUID],
    panel_ids: list[UUID],
    clinical_notes: str | None,
    actor: dict | None,
) -> None:
    reqs = (
        db.query(LabPrescriptionRequest)
        .options(joinedload(LabPrescriptionRequest.items))
        .filter(
            LabPrescriptionRequest.hospital_id == hospital_id,
            LabPrescriptionRequest.prescription_id == prescription_id,
            LabPrescriptionRequest.status != LabPrescriptionRequestStatus.cancelled,
        )
        .order_by(LabPrescriptionRequest.created_at.asc())
        .all()
    )
    tests_resolved = resolve_lab_selection(db, hospital_id, test_ids, panel_ids, require_non_empty=False)
    target = {(r.test.id, r.panel.id if r.panel else None): r for r in tests_resolved}

    touched: dict[UUID, LabPrescriptionRequest] = {}
    existing_keys: set[tuple] = set()
    for req in reqs:
        for item in list(req.items or []):
            key = (item.test_id, item.panel_id)
            if item.status == LabRequestItemStatus.cancelled:
                continue
            if key in target:
                existing_keys.add(key)
                continue
            # Removed by the doctor: only not-yet-accessioned work is withdrawn; tests
            # already in the lab (ordered) keep their order and remain billed.
            if item.status == LabRequestItemStatus.pending:
                req.items.remove(item)
                touched[req.id] = req

    additions = [(key, r) for key, r in target.items() if key not in existing_keys]
    if additions:
        host = next(
            (
                r
                for r in reqs
                if r.status == LabPrescriptionRequestStatus.pending and lab_request_live_order(db, r) is None
            ),
            None,
        )
        if host is not None:
            start = len(host.items or [])
            for idx, (_key, r) in enumerate(additions):
                t = r.test
                host.items.append(
                    LabPrescriptionRequestItem(
                        hospital_id=hospital_id,
                        request_id=host.id,
                        test_id=t.id,
                        panel_id=r.panel.id if r.panel else None,
                        panel_name=r.panel.panel_name if r.panel else None,
                        test_code=t.test_code,
                        test_name=t.test_name,
                        department=t.department,
                        price=t.price,
                        sort_order=start + idx,
                        status=LabRequestItemStatus.pending,
                    )
                )
            touched[host.id] = host
        else:
            # Every existing request is already in the lab: bill the added tests on a
            # supplementary request so paid, in-flight orders keep their clearance.
            from modules.billing.services.episode_account_service import resolve_episode_admission_id

            added = [r for _key, r in additions]
            _new_lab_request(
                db,
                hospital_id=hospital_id,
                prescription_id=prescription_id,
                patient_id=patient_id,
                doctor_id=doctor_id,
                appointment_id=appointment_id,
                admission_id=resolve_episode_admission_id(
                    db, hospital_id, patient_id, admission_id=admission_id, appointment_id=appointment_id
                ),
                tests_resolved=added,
                test_ids=list(dict.fromkeys(r.test.id for r in added if r.panel is None)),
                panel_ids=list(dict.fromkeys(r.panel.id for r in added if r.panel is not None)),
                clinical_notes=clinical_notes,
            )

    for req in touched.values():
        if clinical_notes is not None:
            req.clinical_notes = clinical_notes
        db.flush()
        _refresh_lab_prescribed_ids(req)
        if not lab_request_billable_items(req):
            req.status = LabPrescriptionRequestStatus.cancelled
            req.cancel_reason = "All tests removed from prescription"
        sync_lab_request_charge(
            db,
            req,
            reason="Prescription edited: lab investigations changed",
            actor=actor,
        )


def _update_rad_requests_for_prescription(
    db: Session,
    *,
    hospital_id: UUID,
    doctor_id: UUID,
    patient_id: UUID,
    appointment_id: UUID | None,
    admission_id: UUID | None,
    prescription_id: UUID,
    scan_ids: list[UUID],
    clinical_notes: str | None,
    actor: dict | None,
) -> None:
    from modules.radiology.entities.radiology_entities import (
        RadPrescriptionRequest,
        RadPrescriptionRequestItem,
        RadPrescriptionRequestStatus,
        RadRequestItemStatus,
    )
    from modules.radiology.services.rad_prescription_service import (
        rad_request_live_orders,
        resolve_rad_selection,
        sync_rad_request_charge,
    )

    reqs = (
        db.query(RadPrescriptionRequest)
        .options(joinedload(RadPrescriptionRequest.items))
        .filter(
            RadPrescriptionRequest.hospital_id == hospital_id,
            RadPrescriptionRequest.prescription_id == prescription_id,
            RadPrescriptionRequest.status != RadPrescriptionRequestStatus.cancelled,
        )
        .order_by(RadPrescriptionRequest.created_at.asc())
        .all()
    )
    target = {s.id: s for s in resolve_rad_selection(db, hospital_id, scan_ids)}

    touched: dict[UUID, Any] = {}
    existing: set[UUID] = set()
    for req in reqs:
        for item in list(req.items or []):
            if item.status == RadRequestItemStatus.cancelled:
                continue
            if item.scan_id in target:
                existing.add(item.scan_id)
                continue
            if item.status == RadRequestItemStatus.pending:
                req.items.remove(item)
                touched[req.id] = req

    additions = [s for sid, s in target.items() if sid not in existing]
    if additions:
        host = next(
            (
                r
                for r in reqs
                if r.status == RadPrescriptionRequestStatus.pending and not rad_request_live_orders(db, r)
            ),
            None,
        )
        if host is not None:
            start = len(host.items or [])
            for idx, s in enumerate(additions):
                host.items.append(
                    RadPrescriptionRequestItem(
                        hospital_id=hospital_id,
                        request_id=host.id,
                        scan_id=s.id,
                        scan_code=s.scan_code,
                        scan_name=s.scan_name,
                        category=s.category,
                        price=s.price,
                        sort_order=start + idx,
                        status=RadRequestItemStatus.pending,
                    )
                )
            touched[host.id] = host
        else:
            from modules.billing.services.episode_account_service import resolve_episode_admission_id

            _new_rad_request(
                db,
                hospital_id=hospital_id,
                prescription_id=prescription_id,
                patient_id=patient_id,
                doctor_id=doctor_id,
                appointment_id=appointment_id,
                admission_id=resolve_episode_admission_id(
                    db, hospital_id, patient_id, admission_id=admission_id, appointment_id=appointment_id
                ),
                scans_resolved=additions,
                clinical_notes=clinical_notes,
            )

    for req in touched.values():
        if clinical_notes is not None:
            req.clinical_notes = clinical_notes
        db.flush()
        remaining = [i for i in (req.items or []) if i.status != RadRequestItemStatus.cancelled]
        req.prescribed_scan_ids = list(dict.fromkeys(str(i.scan_id) for i in remaining))
        if not [i for i in remaining if i.status in (RadRequestItemStatus.pending, RadRequestItemStatus.ordered)]:
            req.status = RadPrescriptionRequestStatus.cancelled
            req.cancel_reason = "All scans removed from prescription"
        sync_rad_request_charge(
            db,
            req,
            reason="Prescription edited: radiology investigations changed",
            actor=actor,
        )


def update_investigation_requests_for_prescription(
    db: Session,
    *,
    hospital_id: UUID,
    doctor_id: UUID,
    patient_id: UUID,
    appointment_id: UUID | None,
    prescription_id: UUID,
    admission_id: UUID | None = None,
    test_ids: list[UUID] | None = None,
    panel_ids: list[UUID] | None = None,
    scan_ids: list[UUID] | None = None,
    clinical_notes: str | None = None,
    actor: dict | None = None,
) -> None:
    """
    Synchronize prescription investigations upon prescription edit (``None`` = untouched).

    - Tests/scans already accessioned into a departmental order are never deleted; their
      order and charge stay as they are.
    - Pending items the doctor removed are withdrawn and the request's canonical charge is
      re-priced (a reduction on a paid charge becomes a traceable credit).
    - Additions join a not-yet-accessioned pending request (re-priced), otherwise a new
      supplementary request with its own charge. New charges follow the prescription's
      episode: admission work is billed on the IPD account.
    """
    if test_ids is not None or panel_ids is not None:
        _update_lab_requests_for_prescription(
            db,
            hospital_id=hospital_id,
            doctor_id=doctor_id,
            patient_id=patient_id,
            appointment_id=appointment_id,
            admission_id=admission_id,
            prescription_id=prescription_id,
            test_ids=list(test_ids or []),
            panel_ids=list(panel_ids or []),
            clinical_notes=clinical_notes,
            actor=actor,
        )
    if scan_ids is not None:
        _update_rad_requests_for_prescription(
            db,
            hospital_id=hospital_id,
            doctor_id=doctor_id,
            patient_id=patient_id,
            appointment_id=appointment_id,
            admission_id=admission_id,
            prescription_id=prescription_id,
            scan_ids=list(scan_ids),
            clinical_notes=clinical_notes,
            actor=actor,
        )
