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


def is_lab_request_released(db: Session, req: LabPrescriptionRequest) -> bool:
    """Check if a LabPrescriptionRequest is operationally released to the lab queue."""
    if req.status == LabPrescriptionRequestStatus.cancelled:
        return False
    is_stat = bool(req.clinical_notes and "STAT" in req.clinical_notes.upper())
    if is_stat:
        return True
    from modules.billing.entities.billing_entities import BillingSourceType
    from modules.billing.services.service_financial_clearance import check_service_financial_clearance
    fin = check_service_financial_clearance(db, req.hospital_id, BillingSourceType.laboratory, req.id)
    return fin.is_cleared


def request_to_response_dict(req: LabPrescriptionRequest, db: Session | None = None) -> dict[str, Any]:
    items = sorted(req.items or [], key=lambda i: (i.sort_order, str(i.id)))
    panels = request_panel_names(req)
    payment_status = "pending"
    is_financially_cleared = False
    outstanding_amount = 0.0

    target_db = db or (req._sa_instance_state.session if hasattr(req, "_sa_instance_state") else None)
    if target_db:
        try:
            from modules.billing.entities.billing_entities import BillingSourceType
            from modules.billing.services.service_financial_clearance import check_service_financial_clearance
            fin = check_service_financial_clearance(target_db, req.hospital_id, BillingSourceType.laboratory, req.id)
            payment_status = fin.status
            is_stat = bool(req.clinical_notes and "STAT" in req.clinical_notes.upper())
            is_financially_cleared = fin.is_cleared or is_stat
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


def create_investigation_requests_for_prescription(
    db: Session,
    *,
    hospital_id: UUID,
    doctor_id: UUID,
    patient_id: UUID,
    appointment_id: UUID | None,
    prescription_id: UUID,
    test_ids: list[UUID] | None = None,
    panel_ids: list[UUID] | None = None,
    scan_ids: list[UUID] | None = None,
    clinical_notes: str | None = None,
) -> tuple[LabPrescriptionRequest | None, Any | None]:
    """
    Create lab investigation request (and order) when doctor prescribes tests.
    Also handles radiology scan order creation when scan_ids are present.
    Creates corresponding BillingCharge records so investigations enter billing queue immediately.
    """
    from modules.billing.entities.billing_entities import BillingSourceType
    from modules.billing.services.billing_service import ensure_charge

    lab_req = None
    if test_ids or panel_ids:
        tests_resolved = resolve_lab_selection(
            db, hospital_id, test_ids, panel_ids, require_non_empty=False
        )
        if tests_resolved:
            lab_req = LabPrescriptionRequest(
                hospital_id=hospital_id,
                prescription_id=prescription_id,
                patient_id=patient_id,
                doctor_id=doctor_id,
                appointment_id=appointment_id,
                status=LabPrescriptionRequestStatus.pending,
                prescribed_test_ids=[str(tid) for tid in (test_ids or [])],
                prescribed_panel_ids=[str(pid) for pid in (panel_ids or [])],
                clinical_notes=clinical_notes,
            )
            db.add(lab_req)
            db.flush()

            for idx, r in enumerate(tests_resolved):
                t = r.test
                item = LabPrescriptionRequestItem(
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
                db.add(item)
            db.flush()

            # Auto-create BillingCharge so billing receives investigation immediately
            lab_total = round(sum(float(r.test.price or 0.0) for r in tests_resolved), 2)
            desc_names = [r.test.test_name for r in tests_resolved]
            ensure_charge(
                db,
                hospital_id=hospital_id,
                patient_id=patient_id,
                source_type=BillingSourceType.laboratory,
                source_id=lab_req.id,
                description=f"Lab Investigation — {', '.join(desc_names)}"[:512],
                charge_amount=lab_total,
                created_by_name="Prescription Order",
            )

    rad_req = None
    if scan_ids:
        from modules.radiology.entities.radiology_entities import (
            RadPrescriptionRequest,
            RadPrescriptionRequestItem,
            RadPrescriptionRequestStatus,
            RadRequestItemStatus,
        )
        from modules.radiology.services.rad_prescription_service import (
            resolve_rad_selection,
        )

        scans_resolved = resolve_rad_selection(db, hospital_id, scan_ids)
        if scans_resolved:
            rad_req = RadPrescriptionRequest(
                hospital_id=hospital_id,
                prescription_id=prescription_id,
                patient_id=patient_id,
                doctor_id=doctor_id,
                appointment_id=appointment_id,
                status=RadPrescriptionRequestStatus.pending,
                prescribed_scan_ids=[str(sid) for sid in (scan_ids or [])],
                clinical_notes=clinical_notes,
            )
            db.add(rad_req)
            db.flush()

            for idx, s in enumerate(scans_resolved):
                item = RadPrescriptionRequestItem(
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
                db.add(item)
            db.flush()

            # Auto-create BillingCharge for radiology
            rad_total = round(sum(float(s.price or 0.0) for s in scans_resolved), 2)
            scan_names = [s.scan_name for s in scans_resolved]
            ensure_charge(
                db,
                hospital_id=hospital_id,
                patient_id=patient_id,
                source_type=BillingSourceType.radiology,
                source_id=rad_req.id,
                description=f"Radiology Investigation — {', '.join(scan_names)}"[:512],
                charge_amount=rad_total,
                created_by_name="Prescription Order",
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


def get_prescription_investigation_ids(
    db: Session, rx_id: UUID
) -> tuple[list[UUID], list[UUID], list[UUID]]:
    """Retrieve the current test_ids, panel_ids, and scan_ids for a prescription."""
    test_ids: list[UUID] = []
    panel_ids: list[UUID] = []
    scan_ids: list[UUID] = []

    lab_req = (
        db.query(LabPrescriptionRequest)
        .filter(LabPrescriptionRequest.prescription_id == rx_id)
        .first()
    )
    if lab_req:
        for tid_str in (lab_req.prescribed_test_ids or []):
            try:
                test_ids.append(UUID(str(tid_str)))
            except Exception:
                pass
        for pid_str in (lab_req.prescribed_panel_ids or []):
            try:
                panel_ids.append(UUID(str(pid_str)))
            except Exception:
                pass

    try:
        from modules.radiology.entities.radiology_entities import RadPrescriptionRequest
        rad_req = (
            db.query(RadPrescriptionRequest)
            .filter(RadPrescriptionRequest.prescription_id == rx_id)
            .first()
        )
        if rad_req:
            for sid_str in (rad_req.prescribed_scan_ids or []):
                try:
                    scan_ids.append(UUID(str(sid_str)))
                except Exception:
                    pass
    except Exception:
        pass

    return test_ids, panel_ids, scan_ids


def update_investigation_requests_for_prescription(
    db: Session,
    *,
    hospital_id: UUID,
    doctor_id: UUID,
    patient_id: UUID,
    appointment_id: UUID | None,
    prescription_id: UUID,
    test_ids: list[UUID] | None = None,
    panel_ids: list[UUID] | None = None,
    scan_ids: list[UUID] | None = None,
    clinical_notes: str | None = None,
) -> None:
    """
    Synchronize prescription investigations upon prescription edit.
    - If an investigation item was already accessioned into a departmental LabOrder or RadiologyOrder,
      it MUST NOT be deleted or modified; the departmental order and billing charge remain untouched.
    - If a pending request item is removed by the doctor, it is safely removed/cancelled.
    - If new investigations are added, pending request items are created.
    """
    lab_req = (
        db.query(LabPrescriptionRequest)
        .options(joinedload(LabPrescriptionRequest.items))
        .filter(LabPrescriptionRequest.prescription_id == prescription_id)
        .first()
    )

    if test_ids is not None or panel_ids is not None:
        target_tids = test_ids if test_ids is not None else []
        target_pids = panel_ids if panel_ids is not None else []
        tests_resolved = resolve_lab_selection(
            db, hospital_id, target_tids, target_pids, require_non_empty=False
        )

        if not lab_req and (target_tids or target_pids):
            create_investigation_requests_for_prescription(
                db,
                hospital_id=hospital_id,
                doctor_id=doctor_id,
                patient_id=patient_id,
                appointment_id=appointment_id,
                prescription_id=prescription_id,
                test_ids=target_tids,
                panel_ids=target_pids,
                clinical_notes=clinical_notes,
            )
        elif lab_req:
            lab_req.prescribed_test_ids = [str(tid) for tid in target_tids]
            lab_req.prescribed_panel_ids = [str(pid) for pid in target_pids]
            if clinical_notes is not None:
                lab_req.clinical_notes = clinical_notes

            # Map resolved target tests: (test_id, panel_id)
            target_tuples = {(r.test.id, r.panel.id if r.panel else None): r for r in tests_resolved}

            existing_items = lab_req.items or []
            # Keep track of which existing items match target
            for item in existing_items:
                key = (item.test_id, item.panel_id)
                if key in target_tuples:
                    # Item stays
                    pass
                else:
                    # Item removed from prescription
                    # RULE: If item is pending, cancel/remove it.
                    # If already ordered (departmental order exists), DO NOT delete or cancel order!
                    if item.status == LabRequestItemStatus.pending:
                        db.delete(item)

            # Add any newly added tests
            existing_keys = {(i.test_id, i.panel_id) for i in existing_items}
            idx_start = len(existing_items)
            for idx, ((tid, pid), r) in enumerate(target_tuples.items()):
                if (tid, pid) not in existing_keys:
                    t = r.test
                    new_item = LabPrescriptionRequestItem(
                        hospital_id=hospital_id,
                        request_id=lab_req.id,
                        test_id=t.id,
                        panel_id=r.panel.id if r.panel else None,
                        panel_name=r.panel.panel_name if r.panel else None,
                        test_code=t.test_code,
                        test_name=t.test_name,
                        department=t.department,
                        price=t.price,
                        sort_order=idx_start + idx,
                        status=LabRequestItemStatus.pending,
                    )
                    db.add(new_item)

            db.flush()

            # Synchronize billing charge with updated lab items
            from modules.billing.entities.billing_entities import BillingChargeStatus, BillingSourceType
            from modules.billing.services.billing_service import ensure_charge, find_charge_by_source

            current_items = db.query(LabPrescriptionRequestItem).filter(LabPrescriptionRequestItem.request_id == lab_req.id).all()
            charge = find_charge_by_source(db, hospital_id, BillingSourceType.laboratory, lab_req.id)
            if not current_items:
                if charge:
                    charge.status = BillingChargeStatus.cancelled
            else:
                new_total = round(sum(float(it.price or 0.0) for it in current_items), 2)
                desc_names = [it.test_name for it in current_items if it.test_name]
                if charge:
                    charge.charge_amount = new_total
                    charge.description = f"Lab Investigation — {', '.join(desc_names)}"[:512]
                    net = max(0.0, round(new_total - float(charge.discount_amount or 0.0) + float(charge.tax_amount or 0.0), 2))
                    charge.net_amount = net
                    paid = float(charge.amount_paid or 0.0)
                    if net <= 0.0 or paid >= net > 0.0:
                        charge.status = BillingChargeStatus.paid
                    elif paid > 0.0:
                        charge.status = BillingChargeStatus.partially_paid
                    else:
                        charge.status = BillingChargeStatus.pending
                else:
                    ensure_charge(
                        db,
                        hospital_id=hospital_id,
                        patient_id=patient_id,
                        source_type=BillingSourceType.laboratory,
                        source_id=lab_req.id,
                        description=f"Lab Investigation — {', '.join(desc_names)}"[:512],
                        charge_amount=new_total,
                        created_by_name="Prescription Order",
                    )

    # Handle Radiology Investigations
    if scan_ids is not None:
        try:
            from modules.radiology.entities.radiology_entities import (
                RadPrescriptionRequest,
                RadPrescriptionRequestItem,
                RadPrescriptionRequestStatus,
                RadRequestItemStatus,
            )
            from modules.radiology.services.rad_prescription_service import resolve_rad_selection

            rad_req = (
                db.query(RadPrescriptionRequest)
                .options(joinedload(RadPrescriptionRequest.items))
                .filter(RadPrescriptionRequest.prescription_id == prescription_id)
                .first()
            )

            target_sids = scan_ids
            scans_resolved = resolve_rad_selection(db, hospital_id, target_sids)

            if not rad_req and target_sids:
                create_investigation_requests_for_prescription(
                    db,
                    hospital_id=hospital_id,
                    doctor_id=doctor_id,
                    patient_id=patient_id,
                    appointment_id=appointment_id,
                    prescription_id=prescription_id,
                    scan_ids=target_sids,
                    clinical_notes=clinical_notes,
                )
            elif rad_req:
                rad_req.prescribed_scan_ids = [str(sid) for sid in target_sids]
                if clinical_notes is not None:
                    rad_req.clinical_notes = clinical_notes

                target_scan_ids = {s.id: s for s in scans_resolved}
                existing_items = rad_req.items or []

                for item in existing_items:
                    if item.scan_id in target_scan_ids:
                        pass
                    else:
                        if item.status == RadRequestItemStatus.pending:
                            db.delete(item)

                existing_sids = {i.scan_id for i in existing_items}
                idx_start = len(existing_items)
                for idx, (sid, s) in enumerate(target_scan_ids.items()):
                    if sid not in existing_sids:
                        new_item = RadPrescriptionRequestItem(
                            hospital_id=hospital_id,
                            request_id=rad_req.id,
                            scan_id=s.id,
                            scan_code=s.scan_code,
                            scan_name=s.scan_name,
                            category=s.category,
                            price=s.price,
                            sort_order=idx_start + idx,
                            status=RadRequestItemStatus.pending,
                        )
                        db.add(new_item)

                db.flush()

                # Synchronize billing charge with updated radiology items
                from modules.billing.entities.billing_entities import BillingChargeStatus, BillingSourceType
                from modules.billing.services.billing_service import ensure_charge, find_charge_by_source

                current_rad_items = db.query(RadPrescriptionRequestItem).filter(RadPrescriptionRequestItem.request_id == rad_req.id).all()
                rad_charge = find_charge_by_source(db, hospital_id, BillingSourceType.radiology, rad_req.id)
                if not current_rad_items:
                    if rad_charge:
                        rad_charge.status = BillingChargeStatus.cancelled
                else:
                    new_rad_total = round(sum(float(it.price or 0.0) for it in current_rad_items), 2)
                    desc_scans = [it.scan_name for it in current_rad_items if it.scan_name]
                    if rad_charge:
                        rad_charge.charge_amount = new_rad_total
                        rad_charge.description = f"Radiology Investigation — {', '.join(desc_scans)}"[:512]
                        net = max(0.0, round(new_rad_total - float(rad_charge.discount_amount or 0.0) + float(rad_charge.tax_amount or 0.0), 2))
                        rad_charge.net_amount = net
                        paid = float(rad_charge.amount_paid or 0.0)
                        if net <= 0.0 or paid >= net > 0.0:
                            rad_charge.status = BillingChargeStatus.paid
                        elif paid > 0.0:
                            rad_charge.status = BillingChargeStatus.partially_paid
                        else:
                            rad_charge.status = BillingChargeStatus.pending
                    else:
                        ensure_charge(
                            db,
                            hospital_id=hospital_id,
                            patient_id=patient_id,
                            source_type=BillingSourceType.radiology,
                            source_id=rad_req.id,
                            description=f"Radiology Investigation — {', '.join(desc_scans)}"[:512],
                            charge_amount=new_rad_total,
                            created_by_name="Prescription Order",
                        )
        except Exception:
            pass

