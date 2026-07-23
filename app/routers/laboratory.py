from datetime import datetime, time, timezone
from io import BytesIO
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import (
    Appointment,
    Hospital,
    HospitalUser,
    LabItemStatus,
    LabOrder,
    LabOrderItem,
    LabOrderSource,
    LabOrderStatus,
    LabPanelTest,
    LabPrescriptionRequest,
    LabPrescriptionRequestItem,
    LabPrescriptionRequestStatus,
    LabRequestItemStatus,
    LabResult,
    LabSampleType,
    LabTestCatalog,
    LabTestPanel,
    Patient,
)
from app.schemas_laboratory import (
    ItemStatusUpdate,
    LabCatalogueSeedResult,
    LabDashboardResponse,
    LabOrderCreate,
    LabOrderItemResponse,
    LabOrderResponse,
    LabPanelCreate,
    LabPanelResponse,
    LabPanelUpdate,
    LabPrescriptionRequestResponse,
    LabReportSaveRequest,
    LabRequestCancelBody,
    LabResultResponse,
    LabTestCreate,
    LabTestResponse,
    LabTestUpdate,
    SampleCollectRequest,
)
from app.utils.audit import write_audit
from app.utils.auth import get_hospital_context, require_hospital_user
from app.utils.catalogue_templates import get_template_pack
from app.utils.lab_panels import (
    DEFAULT_PANEL_SEEDS,
    panel_to_response_dict,
    prefer_sample_type,
    resolve_lab_selection,
)
from app.utils.lab_prescription_requests import (
    ACTIVE_REQUEST_STATUSES,
    assert_request_fulfillable,
    get_prescription_request,
    request_to_response_dict,
    sync_request_after_order_change,
)

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
    panel_names = sorted({i.panel_name for i in items if i.panel_name})
    source = getattr(order, "order_source", None) or LabOrderSource.self_requested
    return LabOrderResponse(
        id=order.id,
        hospital_id=order.hospital_id,
        order_no=order.order_no,
        patient_id=order.patient_id,
        doctor_id=order.doctor_id,
        appointment_id=order.appointment_id,
        prescription_id=getattr(order, "prescription_id", None),
        prescription_request_id=getattr(order, "prescription_request_id", None),
        order_source=source,
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
        panel_names=", ".join(panel_names) if panel_names else None,
        items=[LabOrderItemResponse.model_validate(i) for i in items],
        results=[LabResultResponse.model_validate(r) for r in (order.results or [])],
    )


def _enrich_request_response(db: Session, req: LabPrescriptionRequest) -> LabPrescriptionRequestResponse:
    data = request_to_response_dict(req)
    if req.appointment_id:
        appt = (
            db.query(Appointment)
            .filter(Appointment.id == req.appointment_id, Appointment.hospital_id == req.hospital_id)
            .first()
        )
        if appt:
            data["appointment_label"] = (
                f"{appt.appointment_date} {str(appt.appointment_time)[:5] if appt.appointment_time else ''}"
                f" — {appt.purpose or 'Visit'}"
            ).strip()
    # Normalize UUID lists from JSONB
    data["prescribed_test_ids"] = [UUID(str(x)) for x in (req.prescribed_test_ids or [])]
    data["prescribed_panel_ids"] = [UUID(str(x)) for x in (req.prescribed_panel_ids or [])]
    return LabPrescriptionRequestResponse.model_validate(data)


def _get_panel(db: Session, panel_id: UUID, hospital_id: UUID) -> LabTestPanel:
    panel = (
        db.query(LabTestPanel)
        .options(joinedload(LabTestPanel.tests).joinedload(LabPanelTest.test))
        .filter(LabTestPanel.id == panel_id, LabTestPanel.hospital_id == hospital_id)
        .first()
    )
    if not panel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lab panel not found")
    return panel


def _set_panel_tests(db: Session, panel: LabTestPanel, hospital_id: UUID, test_ids: list[UUID]) -> None:
    unique_ids = list(dict.fromkeys(test_ids))
    if unique_ids:
        tests = (
            db.query(LabTestCatalog)
            .filter(
                LabTestCatalog.hospital_id == hospital_id,
                LabTestCatalog.id.in_(unique_ids),
            )
            .all()
        )
        if len(tests) != len(unique_ids):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="One or more panel tests are invalid for this hospital",
            )
    db.query(LabPanelTest).filter(LabPanelTest.panel_id == panel.id).delete(synchronize_session=False)
    db.flush()
    for idx, tid in enumerate(unique_ids):
        db.add(
            LabPanelTest(
                hospital_id=hospital_id,
                panel_id=panel.id,
                test_id=tid,
                sort_order=idx,
            )
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
    panels_count = (
        db.query(func.count(LabTestPanel.id))
        .filter(LabTestPanel.hospital_id == hospital_id, LabTestPanel.is_active.is_(True))
        .scalar()
        or 0
    )
    tests_count = (
        db.query(func.count(LabTestCatalog.id))
        .filter(LabTestCatalog.hospital_id == hospital_id, LabTestCatalog.is_active.is_(True))
        .scalar()
        or 0
    )
    top_panels_rows = (
        db.query(LabOrderItem.panel_name, func.count(LabOrderItem.id))
        .filter(
            LabOrderItem.hospital_id == hospital_id,
            LabOrderItem.panel_name.isnot(None),
            LabOrderItem.panel_name != "",
        )
        .group_by(LabOrderItem.panel_name)
        .order_by(func.count(LabOrderItem.id).desc())
        .limit(5)
        .all()
    )
    pending_req_q = db.query(LabPrescriptionRequest).filter(
        LabPrescriptionRequest.hospital_id == hospital_id,
        LabPrescriptionRequest.status.in_(list(ACTIVE_REQUEST_STATUSES)),
    )
    pending_doctor_requests = pending_req_q.count()
    pending_req_rows = (
        pending_req_q.options(
            joinedload(LabPrescriptionRequest.patient),
            joinedload(LabPrescriptionRequest.doctor),
            joinedload(LabPrescriptionRequest.items),
        )
        .order_by(LabPrescriptionRequest.created_at.desc())
        .limit(20)
        .all()
    )
    doctor_prescribed_orders = base.filter(LabOrder.order_source == LabOrderSource.doctor_prescribed).count()
    self_requested_orders = base.filter(LabOrder.order_source == LabOrderSource.self_requested).count()
    return LabDashboardResponse(
        todays_orders=todays,
        pending=pending,
        completed=completed,
        cancelled=cancelled,
        sample_collected=sample_collected,
        in_progress=in_progress,
        panels_count=int(panels_count),
        tests_count=int(tests_count),
        seeded_tests_estimate=len(get_template_pack("standard").get("lab_tests") or []),
        top_panels=[{"panel_name": name, "order_items": int(cnt)} for name, cnt in top_panels_rows],
        pending_doctor_requests=int(pending_doctor_requests),
        doctor_prescribed_orders=int(doctor_prescribed_orders),
        self_requested_orders=int(self_requested_orders),
        pending_requests=[_enrich_request_response(db, r) for r in pending_req_rows],
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


def _seed_lab_tests_from_pack(
    db: Session,
    hospital_id: UUID,
    *,
    pack_id: str = "standard",
) -> tuple[list[str], int]:
    """Create missing lab tests from a template pack. Never overwrites existing rows/prices."""
    pack = get_template_pack(pack_id)
    added_codes: list[str] = []
    already = 0
    for seed in pack.get("lab_tests") or []:
        code = str(seed["test_code"]).strip().upper()
        exists = (
            db.query(LabTestCatalog.id)
            .filter(LabTestCatalog.hospital_id == hospital_id, LabTestCatalog.test_code == code)
            .first()
        )
        if exists:
            already += 1
            continue
        sample = seed.get("sample_type") or LabSampleType.blood
        if isinstance(sample, str):
            sample = LabSampleType(sample)
        db.add(
            LabTestCatalog(
                hospital_id=hospital_id,
                test_code=code,
                test_name=str(seed["test_name"]).strip(),
                department=str(seed.get("department") or "Laboratory").strip(),
                price=float(seed.get("price") or 0),
                sample_type=sample,
                tat_hours=int(seed.get("tat_hours") or 24),
                description=(str(seed["description"]).strip() if seed.get("description") else None),
                is_active=True,
            )
        )
        added_codes.append(code)
    return added_codes, already


def _seed_lab_panels_for_hospital(
    db: Session,
    hospital_id: UUID,
) -> tuple[list[LabTestPanel], int]:
    """Create missing default panels by matching catalogue codes/names. Skips existing panel codes."""
    catalog = (
        db.query(LabTestCatalog)
        .filter(LabTestCatalog.hospital_id == hospital_id, LabTestCatalog.is_active.is_(True))
        .all()
    )
    created: list[LabTestPanel] = []
    already = 0
    for seed in DEFAULT_PANEL_SEEDS:
        code = seed["panel_code"].upper()
        exists = (
            db.query(LabTestPanel.id)
            .filter(LabTestPanel.hospital_id == hospital_id, LabTestPanel.panel_code == code)
            .first()
        )
        if exists:
            already += 1
            continue
        match_keys = {m.upper() for m in seed["match"]}
        matched_ids: list[UUID] = []
        for t in catalog:
            keys = {t.test_code.upper(), t.test_name.upper()}
            if keys & match_keys or any(
                m in t.test_code.upper() or m in t.test_name.upper() for m in match_keys if len(m) >= 3
            ):
                if t.id not in matched_ids:
                    matched_ids.append(t.id)
        if not matched_ids:
            continue
        panel = LabTestPanel(
            hospital_id=hospital_id,
            panel_code=code,
            panel_name=seed["panel_name"],
            description=seed.get("description"),
            is_active=True,
        )
        db.add(panel)
        db.flush()
        _set_panel_tests(db, panel, hospital_id, matched_ids)
        created.append(panel)
    return created, already


@router.post("/catalogue/seed-standard", response_model=LabCatalogueSeedResult)
def seed_standard_lab_catalogue(
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Idempotent hospital-scoped seed of standard pathology tests + panels. Preserves existing pricing."""
    added_codes, tests_existed = _seed_lab_tests_from_pack(db, hospital_id, pack_id="standard")
    if added_codes:
        db.flush()
    created_panels, panels_existed = _seed_lab_panels_for_hospital(db, hospital_id)
    if added_codes or created_panels:
        write_audit(
            db,
            hospital_id=hospital_id,
            actor=user,
            action="create",
            entity_type="lab_catalogue",
            entity_id=None,
            summary=(
                f"Loaded standard lab catalogue: "
                f"{len(added_codes)} test(s), {len(created_panels)} panel(s) added"
            ),
            details={
                "template_pack": "standard",
                "tests_added": added_codes,
                "panels_added": [p.panel_code for p in created_panels],
            },
        )
        db.commit()
    return LabCatalogueSeedResult(
        template_pack="standard",
        tests_added=len(added_codes),
        tests_already_existed=tests_existed,
        panels_added=len(created_panels),
        panels_already_existed=panels_existed,
        created_test_codes=added_codes,
        created_panel_codes=[p.panel_code for p in created_panels],
    )


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


# ── Panels ─────────────────────────────────────────────────────────────────────
@router.get("/panels", response_model=list[LabPanelResponse])
def list_panels(
    active_only: bool = Query(default=False),
    search: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = (
        db.query(LabTestPanel)
        .options(joinedload(LabTestPanel.tests).joinedload(LabPanelTest.test))
        .filter(LabTestPanel.hospital_id == hospital_id)
    )
    if active_only:
        q = q.filter(LabTestPanel.is_active.is_(True))
    if search and search.strip():
        term = f"%{search.strip()}%"
        q = q.filter(
            (LabTestPanel.panel_name.ilike(term)) | (LabTestPanel.panel_code.ilike(term))
        )
    rows = q.order_by(LabTestPanel.panel_code.asc()).all()
    return [LabPanelResponse.model_validate(panel_to_response_dict(p)) for p in rows]


@router.post("/panels/seed-defaults", response_model=list[LabPanelResponse])
def seed_default_panels(
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Optional: create common panels by matching existing catalogue codes/names. Skips existing codes."""
    created, _already = _seed_lab_panels_for_hospital(db, hospital_id)
    if created:
        write_audit(
            db,
            hospital_id=hospital_id,
            actor=user,
            action="create",
            entity_type="lab_panel",
            entity_id=created[0].id,
            summary=f"Seeded {len(created)} default lab panel(s)",
        )
        db.commit()
    return [
        LabPanelResponse.model_validate(panel_to_response_dict(_get_panel(db, p.id, hospital_id)))
        for p in created
    ]


@router.get("/panels/{panel_id}", response_model=LabPanelResponse)
def get_panel(
    panel_id: UUID,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return LabPanelResponse.model_validate(panel_to_response_dict(_get_panel(db, panel_id, hospital_id)))


@router.post("/panels", response_model=LabPanelResponse, status_code=status.HTTP_201_CREATED)
def create_panel(
    payload: LabPanelCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    code = payload.panel_code.strip().upper()
    exists = (
        db.query(LabTestPanel.id)
        .filter(LabTestPanel.hospital_id == hospital_id, LabTestPanel.panel_code == code)
        .first()
    )
    if exists:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Panel code already exists")

    panel = LabTestPanel(
        hospital_id=hospital_id,
        panel_code=code,
        panel_name=payload.panel_name.strip(),
        description=payload.description.strip() if payload.description else None,
        is_active=payload.is_active,
    )
    db.add(panel)
    db.flush()
    _set_panel_tests(db, panel, hospital_id, payload.test_ids)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="lab_panel",
        entity_id=panel.id,
        summary=f"Created lab panel {panel.panel_code} — {panel.panel_name}",
        details={"test_count": len(payload.test_ids)},
    )
    db.commit()
    return LabPanelResponse.model_validate(panel_to_response_dict(_get_panel(db, panel.id, hospital_id)))


@router.put("/panels/{panel_id}", response_model=LabPanelResponse)
def update_panel(
    panel_id: UUID,
    payload: LabPanelUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    panel = _get_panel(db, panel_id, hospital_id)
    data = payload.model_dump(exclude_unset=True)
    test_ids = data.pop("test_ids", None)
    if "panel_code" in data and data["panel_code"]:
        data["panel_code"] = data["panel_code"].strip().upper()
        clash = (
            db.query(LabTestPanel.id)
            .filter(
                LabTestPanel.hospital_id == hospital_id,
                LabTestPanel.panel_code == data["panel_code"],
                LabTestPanel.id != panel_id,
            )
            .first()
        )
        if clash:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Panel code already exists")
    if "panel_name" in data and isinstance(data["panel_name"], str):
        data["panel_name"] = data["panel_name"].strip()
    if "description" in data and isinstance(data["description"], str):
        data["description"] = data["description"].strip() or None
    for k, v in data.items():
        setattr(panel, k, v)
    if test_ids is not None:
        _set_panel_tests(db, panel, hospital_id, test_ids)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="lab_panel",
        entity_id=panel.id,
        summary=f"Updated lab panel {panel.panel_code}",
    )
    db.commit()
    return LabPanelResponse.model_validate(panel_to_response_dict(_get_panel(db, panel_id, hospital_id)))


@router.delete("/panels/{panel_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_panel(
    panel_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    panel = _get_panel(db, panel_id, hospital_id)
    code = panel.panel_code
    db.delete(panel)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="delete",
        entity_type="lab_panel",
        entity_id=panel_id,
        summary=f"Deleted lab panel {code}",
    )
    db.commit()


# ── Prescription lab requests ──────────────────────────────────────────────────
@router.get("/prescription-requests", response_model=list[LabPrescriptionRequestResponse])
def list_prescription_requests(
    patient_id: UUID | None = Query(default=None),
    status_filter: LabPrescriptionRequestStatus | None = Query(default=None, alias="status"),
    pending_only: bool = Query(default=False),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = (
        db.query(LabPrescriptionRequest)
        .options(
            joinedload(LabPrescriptionRequest.patient),
            joinedload(LabPrescriptionRequest.doctor),
            joinedload(LabPrescriptionRequest.items),
        )
        .filter(LabPrescriptionRequest.hospital_id == hospital_id)
    )
    if patient_id:
        q = q.filter(LabPrescriptionRequest.patient_id == patient_id)
    if pending_only:
        q = q.filter(LabPrescriptionRequest.status.in_(list(ACTIVE_REQUEST_STATUSES)))
    elif status_filter:
        q = q.filter(LabPrescriptionRequest.status == status_filter)
    rows = q.order_by(LabPrescriptionRequest.created_at.desc()).limit(200).all()
    return [_enrich_request_response(db, r) for r in rows]


@router.get("/prescription-requests/{request_id}", response_model=LabPrescriptionRequestResponse)
def get_prescription_request(
    request_id: UUID,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return _enrich_request_response(db, get_prescription_request(db, request_id, hospital_id))


@router.post("/prescription-requests/{request_id}/cancel", response_model=LabPrescriptionRequestResponse)
def cancel_prescription_request(
    request_id: UUID,
    payload: LabRequestCancelBody,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    req = get_prescription_request(db, request_id, hospital_id)
    if req.status == LabPrescriptionRequestStatus.completed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Completed requests cannot be cancelled")
    if req.lab_order_id:
        linked = db.query(LabOrder).filter(LabOrder.id == req.lab_order_id).first()
        if linked and linked.status not in {LabOrderStatus.cancelled, LabOrderStatus.completed}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cancel or complete order {linked.order_no} before cancelling this request",
            )
    req.status = LabPrescriptionRequestStatus.cancelled
    req.cancel_reason = payload.reason.strip() if payload.reason else None
    for item in req.items or []:
        if item.status == LabRequestItemStatus.pending:
            item.status = LabRequestItemStatus.cancelled
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="lab_prescription_request",
        entity_id=req.id,
        summary=f"Cancelled doctor lab request for {req.patient.name if req.patient else 'patient'}",
    )
    db.commit()
    if req.appointment_id:
        from app.utils.appointment_lifecycle import sync_appointment_after_clinical_change

        sync_appointment_after_clinical_change(db, hospital_id, req.appointment_id)
        db.commit()
    return _enrich_request_response(db, get_prescription_request(db, request_id, hospital_id))


@router.post(
    "/prescription-requests/{request_id}/items/{item_id}/unavailable",
    response_model=LabPrescriptionRequestResponse,
)
def mark_request_item_unavailable(
    request_id: UUID,
    item_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    req = get_prescription_request(db, request_id, hospital_id)
    if req.status in {LabPrescriptionRequestStatus.cancelled, LabPrescriptionRequestStatus.completed}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Request is closed")
    item = next((i for i in (req.items or []) if i.id == item_id), None)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request item not found")
    if item.status != LabRequestItemStatus.pending:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only pending items can be marked unavailable")
    item.status = LabRequestItemStatus.unavailable
    pending_left = [i for i in (req.items or []) if i.status == LabRequestItemStatus.pending]
    if not pending_left and not req.lab_order_id:
        # All remaining items unavailable and nothing ordered → cancel request
        all_unavail = all(i.status == LabRequestItemStatus.unavailable for i in (req.items or []))
        if all_unavail:
            req.status = LabPrescriptionRequestStatus.cancelled
            req.cancel_reason = req.cancel_reason or "All prescribed tests marked unavailable"
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="lab_prescription_request",
        entity_id=req.id,
        summary=f"Marked {item.test_code} unavailable on doctor lab request",
    )
    db.commit()
    if req.appointment_id:
        from app.utils.appointment_lifecycle import sync_appointment_after_clinical_change

        sync_appointment_after_clinical_change(db, hospital_id, req.appointment_id)
        db.commit()
    return _enrich_request_response(db, get_prescription_request(db, request_id, hospital_id))


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

    rx_request: LabPrescriptionRequest | None = None
    order_source = LabOrderSource.self_requested
    prescription_id = None
    appointment_id = payload.appointment_id
    clinical_notes = payload.clinical_notes.strip() if payload.clinical_notes else None
    item_rows: list[tuple] = []  # (test_id, panel_id, panel_name, code, name, dept, price)

    if payload.prescription_request_id:
        rx_request = get_prescription_request(db, payload.prescription_request_id, hospital_id)
        if rx_request.patient_id != patient.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Prescription request does not belong to the selected patient",
            )
        # Doctor-prescribed: reject silent modification of tests/panels
        if payload.test_ids or payload.panel_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Doctor-prescribed orders cannot add or change tests; fulfill the prescription as written",
            )
        fulfill_items = assert_request_fulfillable(db, rx_request)
        order_source = LabOrderSource.doctor_prescribed
        prescription_id = rx_request.prescription_id
        appointment_id = rx_request.appointment_id or appointment_id
        doctor = rx_request.doctor or doctor
        if not clinical_notes:
            clinical_notes = rx_request.clinical_notes
        for it in fulfill_items:
            item_rows.append(
                (it.test_id, it.panel_id, it.panel_name, it.test_code, it.test_name, it.department, it.price)
            )
        sample_type = LabSampleType.blood
        test_ids = [i.test_id for i in fulfill_items if i.test_id]
        if test_ids:
            cats = db.query(LabTestCatalog).filter(LabTestCatalog.id.in_(test_ids)).all()
            if cats:
                sample_type = cats[0].sample_type
                for c in cats:
                    if c.sample_type == LabSampleType.blood:
                        sample_type = LabSampleType.blood
                        break
    else:
        tests_resolved = resolve_lab_selection(
            db,
            hospital_id,
            payload.test_ids,
            payload.panel_ids,
        )
        sample_type = prefer_sample_type(tests_resolved)
        for r in tests_resolved:
            t = r.test
            item_rows.append(
                (
                    t.id,
                    r.panel.id if r.panel else None,
                    r.panel.panel_name if r.panel else None,
                    t.test_code,
                    t.test_name,
                    t.department,
                    t.price,
                )
            )

    order = LabOrder(
        hospital_id=hospital_id,
        order_no=_next_order_no(db, hospital_id),
        patient_id=patient.id,
        doctor_id=doctor.id if doctor else None,
        appointment_id=appointment_id,
        prescription_id=prescription_id,
        prescription_request_id=rx_request.id if rx_request else None,
        order_source=order_source,
        ordered_by_name=_actor_name(user),
        ordered_by_role=_actor_role(user),
        status=LabOrderStatus.ordered,
        clinical_notes=clinical_notes,
        sample_type=sample_type,
    )
    db.add(order)
    db.flush()
    for test_id, panel_id, panel_name, code, name, dept, price in item_rows:
        db.add(
            LabOrderItem(
                hospital_id=hospital_id,
                order_id=order.id,
                test_id=test_id,
                panel_id=panel_id,
                panel_name=panel_name,
                test_code=code,
                test_name=name,
                department=dept,
                price=price,
                status=LabItemStatus.pending,
            )
        )

    if rx_request:
        for it in rx_request.items or []:
            if it.status == LabRequestItemStatus.pending:
                it.status = LabRequestItemStatus.ordered
        rx_request.lab_order_id = order.id
        rx_request.status = LabPrescriptionRequestStatus.partially_processed

    panel_labels = sorted({row[2] for row in item_rows if row[2]})
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="lab_order",
        entity_id=order.id,
        summary=f"Lab order {order.order_no} for {patient.name} ({len(item_rows)} tests) [{order_source.value}]",
        details={
            "doctor": doctor.name if doctor else None,
            "tests": [row[3] for row in item_rows],
            "panels": panel_labels,
            "prescription_request_id": str(rx_request.id) if rx_request else None,
        },
    )
    from app.models import BillingSourceType
    from app.utils.billing import ensure_charge

    lab_total = round(sum(float(row[6] or 0) for row in item_rows), 2)
    desc_bits = panel_labels or [row[4] for row in item_rows[:3]]
    ensure_charge(
        db,
        hospital_id=hospital_id,
        patient_id=patient.id,
        source_type=BillingSourceType.laboratory,
        source_id=order.id,
        description=f"Lab {order.order_no} — {', '.join(desc_bits)}"[:512],
        charge_amount=lab_total,
        created_by_name=_actor_name(user),
    )
    db.commit()
    if appointment_id:
        from app.utils.appointment_lifecycle import sync_appointment_after_clinical_change

        sync_appointment_after_clinical_change(db, hospital_id, appointment_id)
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
    sync_request_after_order_change(db, order)
    from app.models import BillingSourceType
    from app.utils.billing import cancel_charge_for_source

    cancel_charge_for_source(db, hospital_id, BillingSourceType.laboratory, order.id)
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
    if order.appointment_id:
        from app.utils.appointment_lifecycle import sync_appointment_after_clinical_change

        sync_appointment_after_clinical_change(db, hospital_id, order.appointment_id)
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
    sync_request_after_order_change(db, order)
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
    order = _get_order(db, order_id, hospital_id)
    if order.status == LabOrderStatus.completed and order.appointment_id:
        from app.utils.appointment_lifecycle import sync_appointment_after_clinical_change

        sync_appointment_after_clinical_change(db, hospital_id, order.appointment_id)
        db.commit()
    return _order_to_response(order)


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

    sync_request_after_order_change(db, order)
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
    from app.utils.appointment_lifecycle import sync_appointment_after_clinical_change

    sync_lab_order_medical_record(db, order)
    if order.status == LabOrderStatus.completed:
        sync_appointment_after_clinical_change(db, hospital_id, order.appointment_id)
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
    panels = sorted({i.panel_name for i in (order.items or []) if i.panel_name})
    panel_line = f"<p><strong>Panels:</strong> {', '.join(panels)}</p>" if panels else ""
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
  {panel_line}
  <p><strong>Tests:</strong> {tests}</p>
  <p><strong>Sample:</strong> {(order.sample_type.value if order.sample_type else '—').title()} · Collected: {collected} by {order.collected_by or '—'}</p>
  <table>
    <thead><tr><th>Test / Parameter</th><th>Result</th><th>Reference Range</th><th>Remarks</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <script>window.onload=function(){{window.print();}}</script>
</body></html>"""
