from datetime import date, datetime, time, timezone
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
    Patient,
    RadiologyOrder,
    RadiologyOrderStatus,
    RadiologyScanCatalog,
)
from app.schemas_radiology import (
    RadDashboardResponse,
    RadOrderCreate,
    RadOrderResponse,
    RadReportRequest,
    RadScanCreate,
    RadScanResponse,
    RadScanUpdate,
    RadScheduleRequest,
)
from app.utils.audit import write_audit
from app.utils.auth import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/radiology", tags=["radiology"])


def _actor_name(user: dict) -> str:
    return str(user.get("name") or user.get("sub") or "Staff")


def _actor_role(user: dict) -> str:
    return str(user.get("staff_role_name") or user.get("role") or "")


def _next_order_no(db: Session, hospital_id: UUID) -> str:
    count = db.query(func.count(RadiologyOrder.id)).filter(RadiologyOrder.hospital_id == hospital_id).scalar() or 0
    return f"RAD{int(count) + 1:04d}"


def _order_to_response(order: RadiologyOrder) -> RadOrderResponse:
    return RadOrderResponse(
        id=order.id,
        hospital_id=order.hospital_id,
        order_no=order.order_no,
        patient_id=order.patient_id,
        doctor_id=order.doctor_id,
        appointment_id=order.appointment_id,
        scan_id=order.scan_id,
        scan_code=order.scan_code,
        scan_name=order.scan_name,
        category=order.category,
        price=order.price,
        ordered_by_name=order.ordered_by_name,
        ordered_by_role=order.ordered_by_role,
        status=order.status,
        clinical_notes=order.clinical_notes,
        scheduled_at=order.scheduled_at,
        machine=order.machine,
        technician_name=order.technician_name,
        started_at=order.started_at,
        completed_at=order.completed_at,
        findings=order.findings,
        impression=order.impression,
        remarks=order.remarks,
        report_file_name=order.report_file_name,
        has_report_file=bool(order.report_file_data),
        image_file_name=order.image_file_name,
        has_image_file=bool(order.image_file_data),
        report_uploaded_by=order.report_uploaded_by,
        report_date=order.report_date,
        ordered_at=order.ordered_at,
        patient_name=order.patient.name if order.patient else None,
        patient_uhid=order.patient.uhid if order.patient else None,
        patient_mobile=order.patient.mobile if order.patient else None,
        doctor_name=order.doctor.name if order.doctor else None,
    )


def _get_order(db: Session, order_id: UUID, hospital_id: UUID) -> RadiologyOrder:
    order = (
        db.query(RadiologyOrder)
        .options(joinedload(RadiologyOrder.patient), joinedload(RadiologyOrder.doctor))
        .filter(RadiologyOrder.id == order_id, RadiologyOrder.hospital_id == hospital_id)
        .first()
    )
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Radiology order not found")
    return order


@router.get("/dashboard", response_model=RadDashboardResponse)
def dashboard(
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    today = datetime.now(timezone.utc).date()
    day_start = datetime.combine(today, time.min, tzinfo=timezone.utc)
    day_end = datetime.combine(today, time.max, tzinfo=timezone.utc)
    base = db.query(RadiologyOrder).filter(RadiologyOrder.hospital_id == hospital_id)
    todays = base.filter(RadiologyOrder.ordered_at >= day_start, RadiologyOrder.ordered_at <= day_end).count()
    pending = base.filter(
        RadiologyOrder.status.in_([RadiologyOrderStatus.ordered, RadiologyOrderStatus.scheduled])
    ).count()
    completed = base.filter(RadiologyOrder.status == RadiologyOrderStatus.completed).count()
    cancelled = base.filter(RadiologyOrder.status == RadiologyOrderStatus.cancelled).count()
    reports_pending = base.filter(
        RadiologyOrder.status.in_([RadiologyOrderStatus.in_progress, RadiologyOrderStatus.completed]),
        (RadiologyOrder.findings.is_(None)) | (RadiologyOrder.findings == ""),
    ).count()
    return RadDashboardResponse(
        todays_orders=todays,
        pending_scans=pending,
        completed_scans=completed,
        reports_pending=reports_pending,
        cancelled_orders=cancelled,
    )


# ── Catalogue ──────────────────────────────────────────────────────────────────
@router.get("/scans", response_model=list[RadScanResponse])
def list_scans(
    active_only: bool = Query(default=False),
    search: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = db.query(RadiologyScanCatalog).filter(RadiologyScanCatalog.hospital_id == hospital_id)
    if active_only:
        q = q.filter(RadiologyScanCatalog.is_active.is_(True))
    if search and search.strip():
        term = f"%{search.strip()}%"
        q = q.filter(
            (RadiologyScanCatalog.scan_name.ilike(term))
            | (RadiologyScanCatalog.scan_code.ilike(term))
            | (RadiologyScanCatalog.category.ilike(term))
        )
    return q.order_by(RadiologyScanCatalog.scan_code.asc()).all()


@router.post("/scans", response_model=RadScanResponse, status_code=status.HTTP_201_CREATED)
def create_scan(
    payload: RadScanCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    code = payload.scan_code.strip().upper()
    if (
        db.query(RadiologyScanCatalog.id)
        .filter(RadiologyScanCatalog.hospital_id == hospital_id, RadiologyScanCatalog.scan_code == code)
        .first()
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Scan code already exists")
    item = RadiologyScanCatalog(
        hospital_id=hospital_id,
        scan_code=code,
        scan_name=payload.scan_name.strip(),
        category=payload.category.strip(),
        department=payload.department.strip(),
        price=float(payload.price),
        duration_minutes=payload.duration_minutes,
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
        entity_type="radiology_scan",
        entity_id=item.id,
        summary=f"Added radiology scan {item.scan_code} — {item.scan_name}",
    )
    db.commit()
    db.refresh(item)
    return item


@router.put("/scans/{scan_id}", response_model=RadScanResponse)
def update_scan(
    scan_id: UUID,
    payload: RadScanUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    item = (
        db.query(RadiologyScanCatalog)
        .filter(RadiologyScanCatalog.id == scan_id, RadiologyScanCatalog.hospital_id == hospital_id)
        .first()
    )
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scan not found")
    data = payload.model_dump(exclude_unset=True)
    if "scan_code" in data and data["scan_code"]:
        data["scan_code"] = data["scan_code"].strip().upper()
        clash = (
            db.query(RadiologyScanCatalog.id)
            .filter(
                RadiologyScanCatalog.hospital_id == hospital_id,
                RadiologyScanCatalog.scan_code == data["scan_code"],
                RadiologyScanCatalog.id != scan_id,
            )
            .first()
        )
        if clash:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Scan code already exists")
    for key in ("scan_name", "category", "department", "description"):
        if key in data and isinstance(data[key], str):
            data[key] = data[key].strip() if data[key] else (None if key == "description" else data[key].strip())
    for k, v in data.items():
        setattr(item, k, v)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="radiology_scan",
        entity_id=item.id,
        summary=f"Updated radiology scan {item.scan_code}",
    )
    db.commit()
    db.refresh(item)
    return item


@router.delete("/scans/{scan_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_scan(
    scan_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    item = (
        db.query(RadiologyScanCatalog)
        .filter(RadiologyScanCatalog.id == scan_id, RadiologyScanCatalog.hospital_id == hospital_id)
        .first()
    )
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scan not found")
    code = item.scan_code
    db.delete(item)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="delete",
        entity_type="radiology_scan",
        entity_id=scan_id,
        summary=f"Deleted radiology scan {code}",
    )
    db.commit()


# ── Orders ─────────────────────────────────────────────────────────────────────
@router.get("/orders", response_model=list[RadOrderResponse])
def list_orders(
    status_filter: RadiologyOrderStatus | None = Query(default=None, alias="status"),
    patient_id: UUID | None = Query(default=None),
    search: str | None = Query(default=None),
    scheduled_only: bool = Query(default=False),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = (
        db.query(RadiologyOrder)
        .options(joinedload(RadiologyOrder.patient), joinedload(RadiologyOrder.doctor))
        .filter(RadiologyOrder.hospital_id == hospital_id)
    )
    if status_filter:
        q = q.filter(RadiologyOrder.status == status_filter)
    if patient_id:
        q = q.filter(RadiologyOrder.patient_id == patient_id)
    if scheduled_only:
        q = q.filter(
            RadiologyOrder.status.in_(
                [
                    RadiologyOrderStatus.scheduled,
                    RadiologyOrderStatus.in_progress,
                ]
            )
        )
    if search and search.strip():
        term = f"%{search.strip()}%"
        q = q.join(Patient).filter(
            (RadiologyOrder.order_no.ilike(term))
            | (Patient.name.ilike(term))
            | (Patient.uhid.ilike(term))
            | (RadiologyOrder.scan_name.ilike(term))
        )
    rows = q.order_by(RadiologyOrder.ordered_at.desc()).limit(300).all()
    return [_order_to_response(o) for o in rows]


@router.get("/orders/{order_id}", response_model=RadOrderResponse)
def get_order(
    order_id: UUID,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return _order_to_response(_get_order(db, order_id, hospital_id))


@router.post("/orders", response_model=list[RadOrderResponse], status_code=status.HTTP_201_CREATED)
def create_orders(
    payload: RadOrderCreate,
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

    scans = (
        db.query(RadiologyScanCatalog)
        .filter(
            RadiologyScanCatalog.hospital_id == hospital_id,
            RadiologyScanCatalog.id.in_(payload.scan_ids),
            RadiologyScanCatalog.is_active.is_(True),
        )
        .all()
    )
    if len(scans) != len(set(payload.scan_ids)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="One or more scans are invalid or inactive")

    created_ids: list[UUID] = []
    for scan in scans:
        order = RadiologyOrder(
            hospital_id=hospital_id,
            order_no=_next_order_no(db, hospital_id),
            patient_id=patient.id,
            doctor_id=doctor.id if doctor else None,
            appointment_id=payload.appointment_id,
            scan_id=scan.id,
            scan_code=scan.scan_code,
            scan_name=scan.scan_name,
            category=scan.category,
            price=scan.price,
            ordered_by_name=_actor_name(user),
            ordered_by_role=_actor_role(user),
            status=RadiologyOrderStatus.ordered,
            clinical_notes=payload.clinical_notes.strip() if payload.clinical_notes else None,
        )
        db.add(order)
        db.flush()
        created_ids.append(order.id)
        write_audit(
            db,
            hospital_id=hospital_id,
            actor=user,
            action="create",
            entity_type="radiology_order",
            entity_id=order.id,
            summary=f"Radiology order {order.order_no} for {patient.name}: {scan.scan_name}",
        )
    db.commit()
    return [_order_to_response(_get_order(db, oid, hospital_id)) for oid in created_ids]


@router.post("/orders/{order_id}/cancel", response_model=RadOrderResponse)
def cancel_order(
    order_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    order = _get_order(db, order_id, hospital_id)
    if order.status == RadiologyOrderStatus.completed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Completed orders cannot be cancelled")
    order.status = RadiologyOrderStatus.cancelled
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="radiology_order",
        entity_id=order.id,
        summary=f"Cancelled radiology order {order.order_no}",
    )
    db.commit()
    return _order_to_response(_get_order(db, order_id, hospital_id))


@router.post("/orders/{order_id}/schedule", response_model=RadOrderResponse)
def schedule_order(
    order_id: UUID,
    payload: RadScheduleRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    order = _get_order(db, order_id, hospital_id)
    if order.status in {RadiologyOrderStatus.cancelled, RadiologyOrderStatus.completed}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot schedule this order")
    order.scheduled_at = payload.scheduled_at
    order.machine = payload.machine.strip()
    order.technician_name = payload.technician_name.strip()
    order.status = RadiologyOrderStatus.scheduled
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="radiology_order",
        entity_id=order.id,
        summary=f"Scheduled {order.order_no} on {order.machine} at {payload.scheduled_at.isoformat()}",
    )
    db.commit()
    return _order_to_response(_get_order(db, order_id, hospital_id))


@router.post("/orders/{order_id}/start", response_model=RadOrderResponse)
def start_scan(
    order_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    order = _get_order(db, order_id, hospital_id)
    if order.status not in {RadiologyOrderStatus.scheduled, RadiologyOrderStatus.ordered}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Order must be scheduled (or ordered) to start")
    order.status = RadiologyOrderStatus.in_progress
    order.started_at = datetime.now(timezone.utc)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="radiology_order",
        entity_id=order.id,
        summary=f"Started scan for {order.order_no}",
    )
    db.commit()
    return _order_to_response(_get_order(db, order_id, hospital_id))


@router.post("/orders/{order_id}/complete-scan", response_model=RadOrderResponse)
def complete_scan(
    order_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    order = _get_order(db, order_id, hospital_id)
    if order.status not in {RadiologyOrderStatus.in_progress, RadiologyOrderStatus.scheduled}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Scan is not in progress")
    order.status = RadiologyOrderStatus.completed
    order.completed_at = datetime.now(timezone.utc)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="radiology_order",
        entity_id=order.id,
        summary=f"Completed scan for {order.order_no} (report may still be pending)",
    )
    db.commit()
    return _order_to_response(_get_order(db, order_id, hospital_id))


@router.post("/orders/{order_id}/report", response_model=RadOrderResponse)
def upload_report(
    order_id: UUID,
    payload: RadReportRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    order = _get_order(db, order_id, hospital_id)
    if order.status == RadiologyOrderStatus.cancelled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Order is cancelled")
    if payload.report_file_data and len(payload.report_file_data) > 2_500_000:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Report file too large (max ~1.5MB)")
    if payload.image_file_data and len(payload.image_file_data) > 2_500_000:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Image file too large (max ~1.5MB)")
    if not payload.image_file_data and not order.image_file_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Scan image is required")

    order.findings = payload.findings.strip()
    order.impression = payload.impression.strip()
    order.remarks = payload.remarks.strip() if payload.remarks else None
    order.report_date = payload.report_date or date.today()
    order.report_uploaded_by = _actor_name(user)
    if payload.image_file_data:
        order.image_file_name = payload.image_file_name
        order.image_file_data = payload.image_file_data
    if order.status != RadiologyOrderStatus.completed:
        order.status = RadiologyOrderStatus.completed
        order.completed_at = order.completed_at or datetime.now(timezone.utc)

    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="radiology_report",
        entity_id=order.id,
        summary=f"Uploaded radiology report for {order.order_no}",
    )
    db.commit()
    order = _get_order(db, order_id, hospital_id)
    from app.utils.medical_record_sync import sync_radiology_order_medical_record

    sync_radiology_order_medical_record(db, order)
    db.commit()
    return _order_to_response(order)


@router.get("/orders/{order_id}/report-view")
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
        headers={"Content-Disposition": f'inline; filename="{order.order_no}-radiology.html"'},
    )


@router.get("/orders/{order_id}/file/{kind}")
def download_file(
    order_id: UUID,
    kind: str,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    order = _get_order(db, order_id, hospital_id)
    if kind == "report":
        data, name = order.report_file_data, order.report_file_name or "report.pdf"
    elif kind == "image":
        data, name = order.image_file_data, order.image_file_name or "image.jpg"
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="kind must be report or image")
    if not data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    # data URL: data:mime;base64,...
    if data.startswith("data:"):
        header, b64 = data.split(",", 1)
        mime = header.split(";")[0].replace("data:", "") or "application/octet-stream"
        import base64

        raw = base64.b64decode(b64)
        return StreamingResponse(
            BytesIO(raw),
            media_type=mime,
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid file data")


def _report_html(order: RadiologyOrder, hospital: Hospital | None) -> str:
    hosp = hospital.name if hospital else "Hospital"
    report_date = order.report_date.isoformat() if order.report_date else "—"
    ordered = order.ordered_at.strftime("%d %b %Y %H:%M") if order.ordered_at else "—"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>{order.order_no} Radiology Report</title>
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; max-width: 800px; margin: 32px auto; color: #0f172a; }}
  h1 {{ color: #0d9488; margin-bottom: 4px; }}
  .meta {{ color: #64748b; font-size: 13px; margin-bottom: 20px; }}
  .box {{ border: 1px solid #e2e8f0; border-radius: 8px; padding: 14px; margin: 12px 0; background: #f8fafc; }}
  .label {{ font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: #64748b; }}
  .value {{ margin-top: 6px; white-space: pre-wrap; font-size: 14px; }}
  @media print {{ body {{ margin: 16px; }} }}
</style></head><body>
  <h1>{hosp}</h1>
  <p class="meta">Radiology Report · {order.order_no} · {order.scan_name} ({order.scan_code})</p>
  <p><strong>Patient:</strong> {order.patient.name if order.patient else '—'}
     ({order.patient.uhid if order.patient else ''}) · Age: {order.patient.age if order.patient and order.patient.age is not None else '—'}</p>
  <p><strong>Referred by:</strong> {order.doctor.name if order.doctor else order.ordered_by_name} · Ordered: {ordered}</p>
  <p><strong>Report Date:</strong> {report_date} · <strong>Uploaded by:</strong> {order.report_uploaded_by or '—'}</p>
  <div class="box"><div class="label">Findings</div><div class="value">{order.findings or '—'}</div></div>
  <div class="box"><div class="label">Impression</div><div class="value">{order.impression or '—'}</div></div>
  <div class="box"><div class="label">Remarks</div><div class="value">{order.remarks or '—'}</div></div>
  <script>window.onload=function(){{window.print();}}</script>
</body></html>"""
