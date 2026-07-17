from datetime import datetime, time, timezone
from io import BytesIO
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import (
    Hospital,
    HospitalUser,
    LabItemStatus,
    LabOrder,
    LabOrderItem,
    LabOrderStatus,
    LabResult,
    LabSampleType,
    LabTestCatalog,
    Patient,
)
from app.schemas_laboratory import (
    ItemStatusUpdate,
    LabDashboardResponse,
    LabOrderCreate,
    LabOrderItemResponse,
    LabOrderResponse,
    LabReportSaveRequest,
    LabResultResponse,
    LabTestCreate,
    LabTestResponse,
    LabTestUpdate,
    SampleCollectRequest,
)
from app.utils.audit import write_audit
from app.utils.auth import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/laboratory", tags=["laboratory"])


def _actor_name(user: dict) -> str:
    return str(user.get("name") or user.get("sub") or "Staff")


def _actor_role(user: dict) -> str:
    return str(user.get("staff_role_name") or user.get("role") or "")


def _next_order_no(db: Session, hospital_id: UUID) -> str:
    count = db.query(func.count(LabOrder.id)).filter(LabOrder.hospital_id == hospital_id).scalar() or 0
    return f"LAB{int(count) + 1:04d}"


def _order_to_response(order: LabOrder) -> LabOrderResponse:
    items = order.items or []
    return LabOrderResponse(
        id=order.id,
        hospital_id=order.hospital_id,
        order_no=order.order_no,
        patient_id=order.patient_id,
        doctor_id=order.doctor_id,
        appointment_id=order.appointment_id,
        ordered_by_name=order.ordered_by_name,
        ordered_by_role=order.ordered_by_role,
        status=order.status,
        clinical_notes=order.clinical_notes,
        sample_type=order.sample_type,
        collected_at=order.collected_at,
        collected_by=order.collected_by,
        collection_remarks=order.collection_remarks,
        ordered_at=order.ordered_at,
        completed_at=order.completed_at,
        patient_name=order.patient.name if order.patient else None,
        patient_uhid=order.patient.uhid if order.patient else None,
        patient_mobile=order.patient.mobile if order.patient else None,
        doctor_name=order.doctor.name if order.doctor else None,
        test_names=", ".join(i.test_code for i in items) if items else None,
        items=[LabOrderItemResponse.model_validate(i) for i in items],
        results=[LabResultResponse.model_validate(r) for r in (order.results or [])],
    )


def _get_order(db: Session, order_id: UUID, hospital_id: UUID) -> LabOrder:
    order = (
        db.query(LabOrder)
        .options(
            joinedload(LabOrder.patient),
            joinedload(LabOrder.doctor),
            joinedload(LabOrder.items),
            joinedload(LabOrder.results),
        )
        .filter(LabOrder.id == order_id, LabOrder.hospital_id == hospital_id)
        .first()
    )
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lab order not found")
    return order


def _sync_order_status_from_items(order: LabOrder) -> None:
    if order.status in {LabOrderStatus.cancelled, LabOrderStatus.ordered}:
        return
    items = order.items or []
    if not items:
        return
    if all(i.status == LabItemStatus.completed for i in items):
        order.status = LabOrderStatus.completed
        if not order.completed_at:
            order.completed_at = datetime.now(timezone.utc)
    elif any(i.status == LabItemStatus.processing for i in items) or any(
        i.status == LabItemStatus.completed for i in items
    ):
        if order.status != LabOrderStatus.completed:
            order.status = LabOrderStatus.in_progress


# ── Dashboard ──────────────────────────────────────────────────────────────────
@router.get("/dashboard", response_model=LabDashboardResponse)
def dashboard(
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    today = datetime.now(timezone.utc).date()
    day_start = datetime.combine(today, time.min, tzinfo=timezone.utc)
    day_end = datetime.combine(today, time.max, tzinfo=timezone.utc)

    base = db.query(LabOrder).filter(LabOrder.hospital_id == hospital_id)
    todays = (
        base.filter(LabOrder.ordered_at >= day_start, LabOrder.ordered_at <= day_end).count()
    )
    pending = base.filter(
        LabOrder.status.in_([LabOrderStatus.ordered, LabOrderStatus.sample_collected])
    ).count()
    completed = base.filter(LabOrder.status == LabOrderStatus.completed).count()
    cancelled = base.filter(LabOrder.status == LabOrderStatus.cancelled).count()
    sample_collected = base.filter(LabOrder.status == LabOrderStatus.sample_collected).count()
    in_progress = base.filter(LabOrder.status == LabOrderStatus.in_progress).count()
    return LabDashboardResponse(
        todays_orders=todays,
        pending=pending,
        completed=completed,
        cancelled=cancelled,
        sample_collected=sample_collected,
        in_progress=in_progress,
    )


# ── Catalogue ──────────────────────────────────────────────────────────────────
@router.get("/tests", response_model=list[LabTestResponse])
def list_tests(
    active_only: bool = Query(default=False),
    search: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = db.query(LabTestCatalog).filter(LabTestCatalog.hospital_id == hospital_id)
    if active_only:
        q = q.filter(LabTestCatalog.is_active.is_(True))
    if search and search.strip():
        term = f"%{search.strip()}%"
        q = q.filter(
            (LabTestCatalog.test_name.ilike(term))
            | (LabTestCatalog.test_code.ilike(term))
            | (LabTestCatalog.department.ilike(term))
        )
    return q.order_by(LabTestCatalog.test_code.asc()).all()


@router.post("/tests", response_model=LabTestResponse, status_code=status.HTTP_201_CREATED)
def create_test(
    payload: LabTestCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    code = payload.test_code.strip().upper()
    exists = (
        db.query(LabTestCatalog.id)
        .filter(LabTestCatalog.hospital_id == hospital_id, LabTestCatalog.test_code == code)
        .first()
    )
    if exists:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Test code already exists")

    item = LabTestCatalog(
        hospital_id=hospital_id,
        test_code=code,
        test_name=payload.test_name.strip(),
        department=payload.department.strip(),
        price=float(payload.price),
        sample_type=payload.sample_type,
        tat_hours=payload.tat_hours,
        description=payload.description.strip() if payload.description else None,
        is_active=payload.is_active,
    )
    db.add(item)
    db.flush()
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="lab_test",
        entity_id=item.id,
        summary=f"Added lab test {item.test_code} — {item.test_name}",
    )
    db.commit()
    db.refresh(item)
    return item


@router.put("/tests/{test_id}", response_model=LabTestResponse)
def update_test(
    test_id: UUID,
    payload: LabTestUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    item = (
        db.query(LabTestCatalog)
        .filter(LabTestCatalog.id == test_id, LabTestCatalog.hospital_id == hospital_id)
        .first()
    )
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Test not found")

    data = payload.model_dump(exclude_unset=True)
    if "test_code" in data and data["test_code"]:
        data["test_code"] = data["test_code"].strip().upper()
        clash = (
            db.query(LabTestCatalog.id)
            .filter(
                LabTestCatalog.hospital_id == hospital_id,
                LabTestCatalog.test_code == data["test_code"],
                LabTestCatalog.id != test_id,
            )
            .first()
        )
        if clash:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Test code already exists")
    for key in ("test_name", "department", "description"):
        if key in data and isinstance(data[key], str):
            data[key] = data[key].strip() if data[key] else None if key == "description" else data[key].strip()
    for k, v in data.items():
        setattr(item, k, v)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="lab_test",
        entity_id=item.id,
        summary=f"Updated lab test {item.test_code}",
    )
    db.commit()
    db.refresh(item)
    return item


@router.delete("/tests/{test_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_test(
    test_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    item = (
        db.query(LabTestCatalog)
        .filter(LabTestCatalog.id == test_id, LabTestCatalog.hospital_id == hospital_id)
        .first()
    )
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Test not found")
    code = item.test_code
    db.delete(item)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="delete",
        entity_type="lab_test",
        entity_id=test_id,
        summary=f"Deleted lab test {code}",
    )
    db.commit()


# ── Orders ─────────────────────────────────────────────────────────────────────
@router.get("/orders", response_model=list[LabOrderResponse])
def list_orders(
    status_filter: LabOrderStatus | None = Query(default=None, alias="status"),
    patient_id: UUID | None = Query(default=None),
    search: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = (
        db.query(LabOrder)
        .options(
            joinedload(LabOrder.patient),
            joinedload(LabOrder.doctor),
            joinedload(LabOrder.items),
            joinedload(LabOrder.results),
        )
        .filter(LabOrder.hospital_id == hospital_id)
    )
    if status_filter:
        q = q.filter(LabOrder.status == status_filter)
    if patient_id:
        q = q.filter(LabOrder.patient_id == patient_id)
    if search and search.strip():
        term = f"%{search.strip()}%"
        q = q.join(Patient).filter(
            (LabOrder.order_no.ilike(term))
            | (Patient.name.ilike(term))
            | (Patient.uhid.ilike(term))
        )
    rows = q.order_by(LabOrder.ordered_at.desc()).limit(200).all()
    return [_order_to_response(o) for o in rows]


@router.get("/orders/{order_id}", response_model=LabOrderResponse)
def get_order(
    order_id: UUID,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return _order_to_response(_get_order(db, order_id, hospital_id))


@router.post("/orders", response_model=LabOrderResponse, status_code=status.HTTP_201_CREATED)
def create_order(
    payload: LabOrderCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    patient = db.query(Patient).filter(Patient.id == payload.patient_id, Patient.hospital_id == hospital_id).first()
    if not patient:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

    doctor = None
    if payload.doctor_id:
        doctor = (
            db.query(HospitalUser)
            .filter(HospitalUser.id == payload.doctor_id, HospitalUser.hospital_id == hospital_id)
            .first()
        )
        if not doctor:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Doctor not found")

    tests = (
        db.query(LabTestCatalog)
        .filter(
            LabTestCatalog.hospital_id == hospital_id,
            LabTestCatalog.id.in_(payload.test_ids),
            LabTestCatalog.is_active.is_(True),
        )
        .all()
    )
    if len(tests) != len(set(payload.test_ids)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="One or more tests are invalid or inactive")

    # Prefer blood if mixed, else first test sample type
    sample_type = tests[0].sample_type
    for t in tests:
        if t.sample_type == LabSampleType.blood:
            sample_type = LabSampleType.blood
            break

    order = LabOrder(
        hospital_id=hospital_id,
        order_no=_next_order_no(db, hospital_id),
        patient_id=patient.id,
        doctor_id=doctor.id if doctor else None,
        appointment_id=payload.appointment_id,
        ordered_by_name=_actor_name(user),
        ordered_by_role=_actor_role(user),
        status=LabOrderStatus.ordered,
        clinical_notes=payload.clinical_notes.strip() if payload.clinical_notes else None,
        sample_type=sample_type,
    )
    db.add(order)
    db.flush()
    for t in tests:
        db.add(
            LabOrderItem(
                hospital_id=hospital_id,
                order_id=order.id,
                test_id=t.id,
                test_code=t.test_code,
                test_name=t.test_name,
                department=t.department,
                price=t.price,
                status=LabItemStatus.pending,
            )
        )
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="lab_order",
        entity_id=order.id,
        summary=f"Lab order {order.order_no} for {patient.name} ({len(tests)} tests)",
        details={"doctor": doctor.name if doctor else None, "tests": [t.test_code for t in tests]},
    )
    db.commit()
    return _order_to_response(_get_order(db, order.id, hospital_id))


@router.post("/orders/{order_id}/cancel", response_model=LabOrderResponse)
def cancel_order(
    order_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    order = _get_order(db, order_id, hospital_id)
    if order.status == LabOrderStatus.completed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Completed orders cannot be cancelled")
    order.status = LabOrderStatus.cancelled
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="lab_order",
        entity_id=order.id,
        summary=f"Cancelled lab order {order.order_no}",
    )
    db.commit()
    return _order_to_response(_get_order(db, order_id, hospital_id))


# ── Sample collection ──────────────────────────────────────────────────────────
@router.post("/orders/{order_id}/collect-sample", response_model=LabOrderResponse)
def collect_sample(
    order_id: UUID,
    payload: SampleCollectRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    order = _get_order(db, order_id, hospital_id)
    if order.status in {LabOrderStatus.cancelled, LabOrderStatus.completed}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot collect sample for this order")
    order.collected_at = payload.collected_at or datetime.now(timezone.utc)
    order.collected_by = payload.collected_by.strip()
    order.collection_remarks = payload.collection_remarks.strip() if payload.collection_remarks else None
    if payload.sample_type:
        order.sample_type = payload.sample_type
    order.status = LabOrderStatus.sample_collected
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="lab_order",
        entity_id=order.id,
        summary=f"Sample collected for {order.order_no} by {order.collected_by}",
    )
    db.commit()
    return _order_to_response(_get_order(db, order_id, hospital_id))


# ── Processing ─────────────────────────────────────────────────────────────────
@router.put("/orders/{order_id}/items/{item_id}/status", response_model=LabOrderResponse)
def update_item_status(
    order_id: UUID,
    item_id: UUID,
    payload: ItemStatusUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    order = _get_order(db, order_id, hospital_id)
    if order.status == LabOrderStatus.cancelled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Order is cancelled")
    if order.status == LabOrderStatus.ordered:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Collect sample before processing")

    item = next((i for i in order.items if i.id == item_id), None)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order item not found")
    item.status = payload.status
    if payload.status in {LabItemStatus.processing, LabItemStatus.completed}:
        if order.status == LabOrderStatus.sample_collected:
            order.status = LabOrderStatus.in_progress
    _sync_order_status_from_items(order)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="lab_order_item",
        entity_id=item.id,
        summary=f"{item.test_code} → {payload.status.value} ({order.order_no})",
    )
    db.commit()
    return _order_to_response(_get_order(db, order_id, hospital_id))


# ── Reports ────────────────────────────────────────────────────────────────────
@router.post("/orders/{order_id}/results", response_model=LabOrderResponse)
def save_results(
    order_id: UUID,
    payload: LabReportSaveRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    order = _get_order(db, order_id, hospital_id)
    if order.status == LabOrderStatus.cancelled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Order is cancelled")

    # Replace existing results
    db.query(LabResult).filter(LabResult.order_id == order.id).delete()
    for idx, row in enumerate(payload.results):
        db.add(
            LabResult(
                hospital_id=hospital_id,
                order_id=order.id,
                order_item_id=row.order_item_id,
                parameter_name=row.parameter_name.strip(),
                result_value=row.result_value.strip(),
                unit=row.unit.strip() if row.unit else None,
                reference_range=row.reference_range.strip() if row.reference_range else None,
                remarks=row.remarks.strip() if row.remarks else None,
                sort_order=row.sort_order if row.sort_order else idx,
            )
        )

    if payload.mark_completed:
        for item in order.items:
            item.status = LabItemStatus.completed
        order.status = LabOrderStatus.completed
        order.completed_at = datetime.now(timezone.utc)
    elif order.status in {LabOrderStatus.ordered, LabOrderStatus.sample_collected}:
        order.status = LabOrderStatus.in_progress

    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="lab_report",
        entity_id=order.id,
        summary=f"Saved lab results for {order.order_no} ({len(payload.results)} parameters)",
    )
    db.commit()
    order = _get_order(db, order_id, hospital_id)
    from app.utils.medical_record_sync import sync_lab_order_medical_record

    sync_lab_order_medical_record(db, order)
    db.commit()
    return _order_to_response(order)


@router.get("/orders/{order_id}/report")
def report_html(
    order_id: UUID,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    order = _get_order(db, order_id, hospital_id)
    hospital = db.query(Hospital).filter(Hospital.id == hospital_id).first()
    html = _report_html(order, hospital)
    return StreamingResponse(
        BytesIO(html.encode("utf-8")),
        media_type="text/html",
        headers={"Content-Disposition": f'inline; filename="{order.order_no}-report.html"'},
    )


def _report_html(order: LabOrder, hospital: Hospital | None) -> str:
    hosp = hospital.name if hospital else "Hospital"
    rows = "".join(
        f"<tr><td>{r.parameter_name}</td><td><strong>{r.result_value}</strong>"
        f"{(' ' + r.unit) if r.unit else ''}</td><td>{r.reference_range or '—'}</td>"
        f"<td>{r.remarks or '—'}</td></tr>"
        for r in (order.results or [])
    )
    if not rows:
        rows = "<tr><td colspan='4'>No results entered yet.</td></tr>"
    tests = ", ".join(f"{i.test_name} ({i.test_code})" for i in (order.items or []))
    collected = order.collected_at.strftime("%d %b %Y %H:%M") if order.collected_at else "—"
    ordered = order.ordered_at.strftime("%d %b %Y %H:%M") if order.ordered_at else "—"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>{order.order_no} Lab Report</title>
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; max-width: 800px; margin: 32px auto; color: #0f172a; }}
  h1 {{ color: #4f46e5; margin-bottom: 4px; }}
  .meta {{ color: #64748b; font-size: 13px; margin-bottom: 20px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
  th, td {{ border: 1px solid #e2e8f0; padding: 8px 10px; text-align: left; font-size: 13px; }}
  th {{ background: #eef2ff; }}
  @media print {{ body {{ margin: 16px; }} }}
</style></head><body>
  <h1>{hosp}</h1>
  <p class="meta">Laboratory Report · {order.order_no} · Status: {order.status.value.replace('_', ' ').title()}</p>
  <p><strong>Patient:</strong> {order.patient.name if order.patient else '—'}
     ({order.patient.uhid if order.patient else ''}) · Age: {order.patient.age if order.patient and order.patient.age is not None else '—'}</p>
  <p><strong>Referred by:</strong> {order.doctor.name if order.doctor else order.ordered_by_name} · <strong>Ordered:</strong> {ordered}</p>
  <p><strong>Tests:</strong> {tests}</p>
  <p><strong>Sample:</strong> {(order.sample_type.value if order.sample_type else '—').title()} · Collected: {collected} by {order.collected_by or '—'}</p>
  <table>
    <thead><tr><th>Test / Parameter</th><th>Result</th><th>Reference Range</th><th>Remarks</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <script>window.onload=function(){{window.print();}}</script>
</body></html>"""
