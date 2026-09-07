"""
Laboratory domain use case actions (Command / Query handlers).

Strictly adheres to UltrionTech-Backend-Template action conventions:
- Orchestrates business logic, repository calls, audit trails, and domain events.
- Employs target billing and appointment lifecycle services.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from hms_migration.modules.appointments.services.appointment_lifecycle import (
    sync_appointment_after_clinical_change,
)
from hms_migration.modules.billing.entities.billing_entities import BillingSourceType
from hms_migration.modules.billing.services.billing_service import (
    cancel_charge_for_source,
    ensure_charge,
)
from hms_migration.modules.clinical_records.entities.clinical_record import Prescription
from hms_migration.modules.doctors.entities.doctor import HospitalUser
from hms_migration.modules.laboratory.contracts.lab_contracts import (
    ItemStatusUpdate,
    LabCatalogueSeedResult,
    LabDashboardResponse,
    LabOrderCreate,
    LabOrderItemResponse,
    LabOrderResponse,
    LabPanelCreate,
    LabPanelResponse,
    LabPanelTestMember,
    LabPanelUpdate,
    LabPrescriptionRequestItemResponse,
    LabPrescriptionRequestResponse,
    LabReportSaveRequest,
    LabResultResponse,
    LabTestCreate,
    LabTestResponse,
    LabTestUpdate,
    SampleCollectRequest,
)
from hms_migration.modules.laboratory.db.laboratory_repository import LaboratoryRepository
from hms_migration.modules.laboratory.entities.lab_entities import (
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
)
from hms_migration.modules.laboratory.services.lab_panels_service import (
    DEFAULT_PANEL_SEEDS,
    STANDARD_LAB_TESTS,
    panel_to_response_dict,
    prefer_sample_type,
    resolve_lab_selection,
)
from hms_migration.modules.laboratory.services.lab_prescription_service import (
    assert_request_fulfillable,
    get_prescription_request,
    request_to_response_dict,
    sync_request_after_order_change,
)
from hms_migration.modules.laboratory.services.lab_report_service import (
    generate_lab_report_html,
    sync_lab_order_medical_record,
)
from hms_migration.modules.patients.entities.patient import Patient
from hms_migration.modules.tenancy.entities.hospital import Hospital


def _actor_name(user: dict) -> str:
    return str(user.get("name") or user.get("sub") or "Staff")


def _actor_role(user: dict) -> str:
    return str(user.get("staff_role_name") or user.get("role") or "")


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
        patient_mobile=getattr(order.patient, "mobile", None) if order.patient else None,
        doctor_name=order.doctor.name if order.doctor else None,
        test_names=", ".join(i.test_name for i in items) if items else None,
        panel_names=", ".join(panel_names) if panel_names else None,
        items=[
            LabOrderItemResponse(
                id=i.id,
                test_id=i.test_id,
                panel_id=i.panel_id,
                panel_name=i.panel_name,
                test_code=i.test_code,
                test_name=i.test_name,
                department=i.department,
                price=i.price,
                status=i.status,
            )
            for i in items
        ],
        results=[
            LabResultResponse(
                id=r.id,
                order_item_id=r.order_item_id,
                parameter_name=r.parameter_name,
                result_value=r.result_value,
                unit=r.unit,
                reference_range=r.reference_range,
                remarks=r.remarks,
                sort_order=r.sort_order,
            )
            for r in (order.results or [])
        ],
    )


def _sync_order_status_from_items(order: LabOrder) -> None:
    items = order.items or []
    if not items or order.status == LabOrderStatus.cancelled:
        return
    statuses = {i.status for i in items}
    if statuses == {LabItemStatus.completed}:
        order.status = LabOrderStatus.completed
        if not order.completed_at:
            order.completed_at = datetime.now(timezone.utc)
    elif LabItemStatus.completed in statuses or LabItemStatus.processing in statuses:
        order.status = LabOrderStatus.in_progress


class GetLabDashboardAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = LaboratoryRepository(db, hospital_id)

    def execute(self) -> LabDashboardResponse:
        metrics = self.repo.get_dashboard_metrics()
        panels = self.repo.list_panels(is_active=True)
        top_panels = [
            {"id": str(p.id), "panel_code": p.panel_code, "panel_name": p.panel_name, "price": float(p.price or 0)}
            for p in panels[:5]
        ]
        pending_reqs = self.repo.list_prescription_requests(
            status=LabPrescriptionRequestStatus.pending
        )[:10]
        pending_req_responses = [
            LabPrescriptionRequestResponse(**request_to_response_dict(r))
            for r in pending_reqs
        ]

        return LabDashboardResponse(
            todays_orders=metrics["todays_orders"],
            pending=metrics["pending"],
            completed=metrics["completed"],
            cancelled=metrics["cancelled"],
            sample_collected=metrics["sample_collected"],
            in_progress=metrics["in_progress"],
            panels_count=metrics["panels_count"],
            tests_count=metrics["tests_count"],
            seeded_tests_estimate=len(STANDARD_LAB_TESTS),
            top_panels=top_panels,
            pending_doctor_requests=metrics["pending_doctor_requests"],
            doctor_prescribed_orders=metrics["doctor_prescribed_orders"],
            self_requested_orders=metrics["self_requested_orders"],
            pending_requests=pending_req_responses,
        )


class ListLabTestsAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = LaboratoryRepository(db, hospital_id)

    def execute(
        self,
        department: str | None = None,
        is_active: bool | None = None,
        search: str | None = None,
    ) -> list[LabTestResponse]:
        rows = self.repo.list_tests(department=department, is_active=is_active, search=search)
        return [LabTestResponse.model_validate(r) for r in rows]


class CreateLabTestAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = LaboratoryRepository(db, hospital_id)

    def execute(self, payload: LabTestCreate, user: dict) -> LabTestResponse:
        existing = self.repo.get_test_by_code(payload.test_code)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Test with code '{payload.test_code.upper()}' already exists",
            )
        row = LabTestCatalog(
            hospital_id=self.hospital_id,
            test_code=payload.test_code.strip().upper(),
            test_name=payload.test_name.strip(),
            department=payload.department.strip(),
            price=payload.price,
            sample_type=payload.sample_type,
            tat_hours=payload.tat_hours,
            description=payload.description.strip() if payload.description else None,
            is_active=payload.is_active,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return LabTestResponse.model_validate(row)


class UpdateLabTestAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = LaboratoryRepository(db, hospital_id)

    def execute(self, test_id: UUID, payload: LabTestUpdate, user: dict) -> LabTestResponse:
        row = self.repo.get_test(test_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lab test not found")
        if payload.test_code:
            code = payload.test_code.strip().upper()
            existing = self.repo.get_test_by_code(code)
            if existing and existing.id != row.id:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Test code already in use")
            row.test_code = code
        if payload.test_name is not None:
            row.test_name = payload.test_name.strip()
        if payload.department is not None:
            row.department = payload.department.strip()
        if payload.price is not None:
            row.price = payload.price
        if payload.sample_type is not None:
            row.sample_type = payload.sample_type
        if payload.tat_hours is not None:
            row.tat_hours = payload.tat_hours
        if payload.description is not None:
            row.description = payload.description.strip() if payload.description else None
        if payload.is_active is not None:
            row.is_active = payload.is_active
        self.db.commit()
        self.db.refresh(row)
        return LabTestResponse.model_validate(row)


class DeleteLabTestAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = LaboratoryRepository(db, hospital_id)

    def execute(self, test_id: UUID, user: dict) -> None:
        row = self.repo.get_test(test_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lab test not found")
        row.is_active = False
        self.db.commit()


class SeedStandardCatalogueAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    def execute(self, user: dict) -> LabCatalogueSeedResult:
        tests_added = 0
        tests_existed = 0
        created_test_codes: list[str] = []

        for item in STANDARD_LAB_TESTS:
            code = item["test_code"].strip().upper()
            existing = (
                self.db.query(LabTestCatalog)
                .filter(LabTestCatalog.hospital_id == self.hospital_id, LabTestCatalog.test_code == code)
                .first()
            )
            if existing:
                tests_existed += 1
                continue
            t = LabTestCatalog(
                hospital_id=self.hospital_id,
                test_code=code,
                test_name=item["test_name"],
                department=item["department"],
                price=float(item["price"]),
                sample_type=item.get("sample_type", LabSampleType.blood),
                tat_hours=int(item.get("tat_hours", 24)),
                description=item.get("description"),
                is_active=True,
            )
            self.db.add(t)
            created_test_codes.append(code)
            tests_added += 1

        self.db.flush()

        # Seed default panels
        panels_added = 0
        panels_existed = 0
        created_panel_codes: list[str] = []
        all_tests = (
            self.db.query(LabTestCatalog)
            .filter(LabTestCatalog.hospital_id == self.hospital_id)
            .all()
        )

        for p_def in DEFAULT_PANEL_SEEDS:
            p_code = p_def["panel_code"].strip().upper()
            p_exist = (
                self.db.query(LabTestPanel)
                .filter(LabTestPanel.hospital_id == self.hospital_id, LabTestPanel.panel_code == p_code)
                .first()
            )
            if p_exist:
                panels_existed += 1
                continue

            matches = p_def["match"]
            matched_tests = []
            for t in all_tests:
                if any(m in t.test_code.upper() or m in t.test_name.upper() for m in matches):
                    if t not in matched_tests:
                        matched_tests.append(t)

            panel = LabTestPanel(
                hospital_id=self.hospital_id,
                panel_code=p_code,
                panel_name=p_def["panel_name"],
                description=p_def.get("description"),
                price=round(sum(t.price for t in matched_tests) * 0.85, 2) if matched_tests else 0.0,
                is_active=True,
            )
            self.db.add(panel)
            self.db.flush()

            for sort_idx, t in enumerate(matched_tests):
                self.db.add(
                    LabPanelTest(
                        hospital_id=self.hospital_id,
                        panel_id=panel.id,
                        test_id=t.id,
                        sort_order=sort_idx,
                    )
                )
            created_panel_codes.append(p_code)
            panels_added += 1

        self.db.commit()
        return LabCatalogueSeedResult(
            template_pack="standard",
            tests_added=tests_added,
            tests_already_existed=tests_existed,
            panels_added=panels_added,
            panels_already_existed=panels_existed,
            created_test_codes=created_test_codes,
            created_panel_codes=created_panel_codes,
        )


class ListLabPanelsAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = LaboratoryRepository(db, hospital_id)

    def execute(self, is_active: bool | None = None) -> list[LabPanelResponse]:
        panels = self.repo.list_panels(is_active=is_active)
        return [LabPanelResponse(**panel_to_response_dict(p)) for p in panels]


class GetLabPanelAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = LaboratoryRepository(db, hospital_id)

    def execute(self, panel_id: UUID) -> LabPanelResponse:
        panel = self.repo.get_panel(panel_id)
        if not panel:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Panel not found")
        return LabPanelResponse(**panel_to_response_dict(panel))


class CreateLabPanelAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = LaboratoryRepository(db, hospital_id)

    def execute(self, payload: LabPanelCreate, user: dict) -> LabPanelResponse:
        code = payload.panel_code.strip().upper()
        existing = self.repo.get_panel_by_code(code)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Panel with code '{code}' already exists",
            )
        panel = LabTestPanel(
            hospital_id=self.hospital_id,
            panel_code=code,
            panel_name=payload.panel_name.strip(),
            description=payload.description.strip() if payload.description else None,
            price=payload.price,
            is_active=payload.is_active,
        )
        self.db.add(panel)
        self.db.flush()

        if payload.test_ids:
            unique_tids = list(dict.fromkeys(payload.test_ids))
            tests = (
                self.db.query(LabTestCatalog)
                .filter(
                    LabTestCatalog.hospital_id == self.hospital_id,
                    LabTestCatalog.id.in_(unique_tids),
                )
                .all()
            )
            by_id = {t.id: t for t in tests}
            for sort_idx, tid in enumerate(unique_tids):
                if tid in by_id:
                    self.db.add(
                        LabPanelTest(
                            hospital_id=self.hospital_id,
                            panel_id=panel.id,
                            test_id=tid,
                            sort_order=sort_idx,
                        )
                    )

        self.db.commit()
        return GetLabPanelAction(self.db, self.hospital_id).execute(panel.id)


class UpdateLabPanelAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = LaboratoryRepository(db, hospital_id)

    def execute(self, panel_id: UUID, payload: LabPanelUpdate, user: dict) -> LabPanelResponse:
        panel = self.repo.get_panel(panel_id)
        if not panel:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Panel not found")

        if payload.panel_code:
            code = payload.panel_code.strip().upper()
            existing = self.repo.get_panel_by_code(code)
            if existing and existing.id != panel.id:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Panel code in use")
            panel.panel_code = code
        if payload.panel_name is not None:
            panel.panel_name = payload.panel_name.strip()
        if payload.description is not None:
            panel.description = payload.description.strip() if payload.description else None
        if payload.price is not None:
            panel.price = payload.price
        if payload.is_active is not None:
            panel.is_active = payload.is_active

        if payload.test_ids is not None:
            self.db.query(LabPanelTest).filter(LabPanelTest.panel_id == panel.id).delete()
            unique_tids = list(dict.fromkeys(payload.test_ids))
            for sort_idx, tid in enumerate(unique_tids):
                self.db.add(
                    LabPanelTest(
                        hospital_id=self.hospital_id,
                        panel_id=panel.id,
                        test_id=tid,
                        sort_order=sort_idx,
                    )
                )

        self.db.commit()
        return GetLabPanelAction(self.db, self.hospital_id).execute(panel.id)


class DeleteLabPanelAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = LaboratoryRepository(db, hospital_id)

    def execute(self, panel_id: UUID, user: dict) -> None:
        panel = self.repo.get_panel(panel_id)
        if not panel:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Panel not found")
        panel.is_active = False
        self.db.commit()


class ListLabPrescriptionRequestsAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = LaboratoryRepository(db, hospital_id)

    def execute(
        self,
        status: LabPrescriptionRequestStatus | None = None,
        patient_id: UUID | None = None,
        doctor_id: UUID | None = None,
    ) -> list[LabPrescriptionRequestResponse]:
        reqs = self.repo.list_prescription_requests(
            status=status, patient_id=patient_id, doctor_id=doctor_id
        )
        return [LabPrescriptionRequestResponse(**request_to_response_dict(r)) for r in reqs]


class GetLabPrescriptionRequestAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = LaboratoryRepository(db, hospital_id)

    def execute(self, request_id: UUID) -> LabPrescriptionRequestResponse:
        req = self.repo.get_prescription_request(request_id)
        if not req:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Prescription lab request not found",
            )
        return LabPrescriptionRequestResponse(**request_to_response_dict(req))


class CancelLabPrescriptionRequestAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = LaboratoryRepository(db, hospital_id)

    def execute(self, request_id: UUID, reason: str | None, user: dict) -> LabPrescriptionRequestResponse:
        req = get_prescription_request(self.db, request_id, self.hospital_id)
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
        self.db.commit()
        if req.appointment_id:
            sync_appointment_after_clinical_change(self.db, self.hospital_id, req.appointment_id)
            self.db.commit()
        return LabPrescriptionRequestResponse(**request_to_response_dict(req))


class MarkLabRequestItemUnavailableAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    def execute(self, request_id: UUID, item_id: UUID, user: dict) -> LabPrescriptionRequestResponse:
        req = get_prescription_request(self.db, request_id, self.hospital_id)
        if req.status in {LabPrescriptionRequestStatus.completed, LabPrescriptionRequestStatus.cancelled}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Request cannot be modified")
        item = next((i for i in (req.items or []) if i.id == item_id), None)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
        item.status = LabRequestItemStatus.unavailable

        pending = [i for i in req.items if i.status == LabRequestItemStatus.pending]
        if not pending:
            req.status = LabPrescriptionRequestStatus.cancelled
            req.cancel_reason = "All requested tests marked unavailable"

        self.db.commit()
        if req.appointment_id:
            sync_appointment_after_clinical_change(self.db, self.hospital_id, req.appointment_id)
            self.db.commit()
        return LabPrescriptionRequestResponse(**request_to_response_dict(req))


class ListLabOrdersAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = LaboratoryRepository(db, hospital_id)

    def execute(
        self,
        status: LabOrderStatus | None = None,
        patient_id: UUID | None = None,
        doctor_id: UUID | None = None,
        order_date: Any | None = None,
        order_source: LabOrderSource | None = None,
        search: str | None = None,
    ) -> list[LabOrderResponse]:
        orders = self.repo.list_orders(
            status=status,
            patient_id=patient_id,
            doctor_id=doctor_id,
            order_date=order_date,
            order_source=order_source,
            search=search,
        )
        return [_order_to_response(o) for o in orders]


class GetLabOrderAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = LaboratoryRepository(db, hospital_id)

    def execute(self, order_id: UUID) -> LabOrderResponse:
        order = self.repo.get_order(order_id)
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lab order not found")
        return _order_to_response(order)


class CreateLabOrderAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = LaboratoryRepository(db, hospital_id)

    def execute(self, payload: LabOrderCreate, user: dict) -> LabOrderResponse:
        patient = (
            self.db.query(Patient)
            .filter(Patient.id == payload.patient_id, Patient.hospital_id == self.hospital_id)
            .first()
        )
        if not patient:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

        doctor = None
        if payload.doctor_id:
            doctor = (
                self.db.query(HospitalUser)
                .filter(HospitalUser.id == payload.doctor_id, HospitalUser.hospital_id == self.hospital_id)
                .first()
            )
            if not doctor:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Doctor not found")

        rx_request: LabPrescriptionRequest | None = None
        order_source = LabOrderSource.self_requested
        prescription_id = None
        appointment_id = payload.appointment_id
        clinical_notes = payload.clinical_notes.strip() if payload.clinical_notes else None
        item_rows: list[tuple] = []

        if payload.prescription_request_id:
            rx_request = get_prescription_request(self.db, payload.prescription_request_id, self.hospital_id)
            if rx_request.patient_id != patient.id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Prescription request does not belong to the selected patient",
                )
            if payload.test_ids or payload.panel_ids:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Doctor-prescribed orders cannot add or change tests; fulfill the prescription as written",
                )
            fulfill_items = assert_request_fulfillable(self.db, rx_request)
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
            tids = [i.test_id for i in fulfill_items if i.test_id]
            if tids:
                cats = self.db.query(LabTestCatalog).filter(LabTestCatalog.id.in_(tids)).all()
                if cats:
                    sample_type = cats[0].sample_type
                    for c in cats:
                        if c.sample_type == LabSampleType.blood:
                            sample_type = LabSampleType.blood
                            break
        else:
            resolved = resolve_lab_selection(
                self.db,
                self.hospital_id,
                payload.test_ids,
                payload.panel_ids,
            )
            sample_type = prefer_sample_type(resolved)
            for r in resolved:
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
            hospital_id=self.hospital_id,
            order_no=self.repo.next_order_no(),
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
        self.db.add(order)
        self.db.flush()

        for test_id, panel_id, panel_name, code, name, dept, price in item_rows:
            self.db.add(
                LabOrderItem(
                    hospital_id=self.hospital_id,
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

        # Auto-create ledger billing charge
        panel_labels = sorted({row[2] for row in item_rows if row[2]})
        lab_total = round(sum(float(row[6] or 0) for row in item_rows), 2)
        desc_bits = panel_labels or [row[4] for row in item_rows[:3]]
        ensure_charge(
            self.db,
            hospital_id=self.hospital_id,
            patient_id=patient.id,
            source_type=BillingSourceType.laboratory,
            source_id=order.id,
            description=f"Lab {order.order_no} — {', '.join(desc_bits)}"[:512],
            charge_amount=lab_total,
            created_by_name=_actor_name(user),
        )

        self.db.commit()

        if appointment_id:
            sync_appointment_after_clinical_change(self.db, self.hospital_id, appointment_id)
            self.db.commit()

        return GetLabOrderAction(self.db, self.hospital_id).execute(order.id)


class CancelLabOrderAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = LaboratoryRepository(db, hospital_id)

    def execute(self, order_id: UUID, user: dict) -> LabOrderResponse:
        order = self.repo.get_order(order_id)
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lab order not found")
        if order.status == LabOrderStatus.completed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Completed orders cannot be cancelled",
            )
        order.status = LabOrderStatus.cancelled
        sync_request_after_order_change(self.db, order)
        cancel_charge_for_source(self.db, self.hospital_id, BillingSourceType.laboratory, order.id)

        self.db.commit()
        if order.appointment_id:
            sync_appointment_after_clinical_change(self.db, self.hospital_id, order.appointment_id)
            self.db.commit()

        return GetLabOrderAction(self.db, self.hospital_id).execute(order_id)


class CollectSampleAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = LaboratoryRepository(db, hospital_id)

    def execute(self, order_id: UUID, payload: SampleCollectRequest, user: dict) -> LabOrderResponse:
        order = self.repo.get_order(order_id)
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lab order not found")
        if order.status in {LabOrderStatus.cancelled, LabOrderStatus.completed}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot collect sample for this order",
            )
        order.collected_at = payload.collected_at or datetime.now(timezone.utc)
        order.collected_by = payload.collected_by.strip()
        order.collection_remarks = (
            payload.collection_remarks.strip() if payload.collection_remarks else None
        )
        if payload.sample_type:
            order.sample_type = payload.sample_type
        order.status = LabOrderStatus.sample_collected
        sync_request_after_order_change(self.db, order)

        self.db.commit()
        return GetLabOrderAction(self.db, self.hospital_id).execute(order_id)


class UpdateItemStatusAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = LaboratoryRepository(db, hospital_id)

    def execute(self, order_id: UUID, item_id: UUID, payload: ItemStatusUpdate, user: dict) -> LabOrderResponse:
        order = self.repo.get_order(order_id)
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lab order not found")
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

        self.db.commit()
        order = self.repo.get_order(order_id)
        if order.status == LabOrderStatus.completed and order.appointment_id:
            sync_appointment_after_clinical_change(self.db, self.hospital_id, order.appointment_id)
            self.db.commit()

        return GetLabOrderAction(self.db, self.hospital_id).execute(order_id)


class SaveLabResultsAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = LaboratoryRepository(db, hospital_id)

    def execute(self, order_id: UUID, payload: LabReportSaveRequest, user: dict) -> LabOrderResponse:
        order = self.repo.get_order(order_id)
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lab order not found")
        if order.status == LabOrderStatus.cancelled:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Order is cancelled")

        self.db.query(LabResult).filter(LabResult.order_id == order.id).delete()
        for idx, row in enumerate(payload.results):
            self.db.add(
                LabResult(
                    hospital_id=self.hospital_id,
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

        sync_request_after_order_change(self.db, order)
        self.db.commit()

        order = self.repo.get_order(order_id)
        sync_lab_order_medical_record(self.db, order)
        if order.status == LabOrderStatus.completed and order.appointment_id:
            sync_appointment_after_clinical_change(self.db, self.hospital_id, order.appointment_id)
        self.db.commit()

        return GetLabOrderAction(self.db, self.hospital_id).execute(order_id)


class GetLabReportHtmlAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = LaboratoryRepository(db, hospital_id)

    def execute(self, order_id: UUID) -> tuple[str, str]:
        order = self.repo.get_order(order_id)
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lab order not found")
        hospital = self.db.query(Hospital).filter(Hospital.id == self.hospital_id).first()
        hospital_name = hospital.name if hospital else "Hospital"
        html = generate_lab_report_html(order, hospital_name)
        filename = f"{order.order_no}-report.html"
        return html, filename
