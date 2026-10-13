"""
Business use-case actions for Radiology domain.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from modules.appointments.services.appointment_lifecycle import (
    sync_appointment_after_clinical_change,
)
from modules.billing.entities.billing_entities import BillingSourceType
from modules.billing.services.episode_account_service import resolve_episode_account
from modules.billing.services.service_charge_resolver import (
    cancel_service_charges_or_400,
    ensure_service_charge,
)
from modules.billing.services.billing_service import auto_apply_eligible_deposits_to_charge
from modules.billing.services.service_financial_clearance import (
    assert_order_financially_cleared,
    assert_service_financially_cleared,
)
from modules.appointments.entities.appointment import Appointment
from modules.inpatient.entities.admission import Admission
from modules.radiology.contracts.radiology_contracts import (
    RadAttachmentResponse,
    RadCatalogueSeedResult,
    RadDashboardResponse,
    RadOrderCreate,
    RadOrderResponse,
    RadPrescriptionRequestResponse,
    RadReportRequest,
    RadRequestCancelBody,
    RadScanCreate,
    RadScanResponse,
    RadScanUpdate,
    RadScheduleRequest,
)
from modules.radiology.db.radiology_repository import RadiologyRepository
from modules.radiology.entities.radiology_entities import (
    RadiologyAttachment,
    RadiologyOrder,
    RadiologyOrderStatus,
    RadiologyScanCatalog,
    RadPrescriptionRequestStatus,
    RadRequestItemStatus,
)
from modules.radiology.services.radiology_service import (
    STANDARD_RADIOLOGY_SCANS,
    order_to_response,
    orders_to_responses,
    sync_radiology_order_medical_record,
)
from modules.radiology.services.rad_prescription_service import (
    apply_cancel_rad_prescription_request,
    assert_request_fulfillable,
    ensure_rad_request_charge,
    get_prescription_request,
    is_rad_request_released,
    recompute_rad_request_status,
    request_to_response_dict,
    sync_rad_request_charge,
    sync_request_after_order_change,
)
from shared.audit import write_audit_log
from shared.sync import stage_sync_event


def _actor_name(user: dict[str, Any]) -> str:
    return str(user.get("name") or user.get("sub") or "Staff")


def _gate_order(
    db: Session,
    hospital_id: UUID,
    order: RadiologyOrder,
    user: dict[str, Any],
    action_description: str,
    payload: Any | None = None,
) -> None:
    """Financial gate for every radiology execution step (schedule, start, attachment
    upload, scan completion, first report). An emergency override is scoped to the order:
    once used, it keeps unlocking that order's later steps."""
    assert_order_financially_cleared(
        db,
        hospital_id,
        BillingSourceType.radiology,
        order,
        user,
        action_description=action_description,
        is_emergency_override=bool(payload is not None and getattr(payload, "is_emergency_override", False)),
        emergency_reason=getattr(payload, "emergency_override_reason", None) if payload is not None else None,
    )


from modules.radiology.services.report_status import is_radiology_report_finalized


def _is_report_finalized(order: RadiologyOrder) -> bool:
    return is_radiology_report_finalized(order)


def _actor_role(user: dict[str, Any]) -> str:
    return str(user.get("staff_role_name") or user.get("role") or "")


class ListScansAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = RadiologyRepository(db, hospital_id)

    def execute(self, active_only: bool = False, search: str | None = None) -> list[RadiologyScanCatalog]:
        return self.repo.list_scans(active_only=active_only, search=search)


class SeedStandardCatalogueAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = RadiologyRepository(db, hospital_id)

    def execute(self) -> RadCatalogueSeedResult:
        added_codes: list[str] = []
        already = 0
        for seed in STANDARD_RADIOLOGY_SCANS:
            code = str(seed["scan_code"]).strip().upper()
            if self.repo.get_scan_by_code(code):
                already += 1
                continue
            item = RadiologyScanCatalog(
                hospital_id=self.hospital_id,
                scan_code=code,
                scan_name=str(seed["scan_name"]).strip(),
                category=str(seed.get("category") or "General").strip(),
                department=str(seed.get("department") or "Radiology").strip(),
                price=float(seed.get("price") or 0.0),
                duration_minutes=int(seed.get("duration_minutes") or 30),
                description=(str(seed["description"]).strip() if seed.get("description") else None),
                is_active=True,
            )
            self.repo.create_scan(item)
            added_codes.append(code)

        if added_codes:
            write_audit_log(
                self.db,
                hospital_id=self.hospital_id,
                actor=self.user,
                action="create",
                entity_type="radiology_catalogue",
                entity_id=None,
                summary=f"Loaded standard radiology catalogue: {len(added_codes)} scan(s) added",
                details={"template_pack": "standard", "scans_added": added_codes},
            )
            self.db.commit()

        return RadCatalogueSeedResult(
            template_pack="standard",
            added=len(added_codes),
            already_existed=already,
            created_codes=added_codes,
        )


class CreateScanAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = RadiologyRepository(db, hospital_id)

    def execute(self, payload: RadScanCreate) -> RadiologyScanCatalog:
        code = payload.scan_code.strip().upper()
        if self.repo.check_scan_code_conflict(code):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Scan code already exists")

        item = RadiologyScanCatalog(
            hospital_id=self.hospital_id,
            scan_code=code,
            scan_name=payload.scan_name.strip(),
            category=payload.category.strip(),
            department=payload.department.strip(),
            price=float(payload.price),
            duration_minutes=payload.duration_minutes,
            description=payload.description.strip() if payload.description else None,
            is_active=payload.is_active,
        )
        self.repo.create_scan(item)
        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="radiology_scan",
            entity_id=item.id,
            summary=f"Added radiology scan {item.scan_code} — {item.scan_name}",
        )
        self.db.commit()
        self.db.refresh(item)
        return item


class UpdateScanAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = RadiologyRepository(db, hospital_id)

    def execute(self, scan_id: UUID, payload: RadScanUpdate) -> RadiologyScanCatalog:
        item = self.repo.get_scan(scan_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scan not found")

        data = payload.model_dump(exclude_unset=True)
        if "scan_code" in data and data["scan_code"]:
            code = data["scan_code"].strip().upper()
            data["scan_code"] = code
            if self.repo.check_scan_code_conflict(code, exclude_id=scan_id):
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Scan code already exists")

        for key in ("scan_name", "category", "department", "description"):
            if key in data and isinstance(data[key], str):
                data[key] = data[key].strip() if data[key] else (None if key == "description" else data[key].strip())

        for k, v in data.items():
            setattr(item, k, v)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="radiology_scan",
            entity_id=item.id,
            summary=f"Updated radiology scan {item.scan_code}",
        )
        self.db.commit()
        self.db.refresh(item)
        return item


class DeleteScanAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = RadiologyRepository(db, hospital_id)

    def execute(self, scan_id: UUID) -> None:
        item = self.repo.get_scan(scan_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scan not found")

        code = item.scan_code
        self.repo.delete_scan(item)
        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="delete",
            entity_type="radiology_scan",
            entity_id=scan_id,
            summary=f"Deleted radiology scan {code}",
        )
        self.db.commit()


class ListOrdersAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = RadiologyRepository(db, hospital_id)

    def execute(
        self,
        status_filter: RadiologyOrderStatus | None = None,
        patient_id: UUID | None = None,
        search: str | None = None,
        scheduled_only: bool = False,
    ) -> list[RadOrderResponse]:
        orders = self.repo.list_orders(
            status_filter=status_filter,
            patient_id=patient_id,
            search=search,
            scheduled_only=scheduled_only,
        )
        # Use batch billing clearance (1-3 queries total) instead of N per-order queries.
        return orders_to_responses(orders, self.repo.db)


class GetOrderAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = RadiologyRepository(db, hospital_id)

    def execute(self, order_id: UUID) -> RadOrderResponse:
        order = self.repo.get_order(order_id)
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Radiology order not found")
        return order_to_response(order, self.repo.db)


class CreateOrdersAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = RadiologyRepository(db, hospital_id)

    def execute(self, payload: RadOrderCreate) -> list[RadOrderResponse]:
        patient = self.repo.get_patient(payload.patient_id)
        if not patient:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

        doctor = None
        if payload.doctor_id:
            doctor = self.repo.get_doctor(payload.doctor_id)
            if not doctor:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Doctor not found")

        appointment_id = payload.appointment_id
        clinical_notes = payload.clinical_notes.strip() if payload.clinical_notes else None
        rad_request = None
        # (scan_id, scan_code, scan_name, category, price) lines to accession.
        lines: list[tuple] = []

        if payload.prescription_request_id:
            rad_request = get_prescription_request(self.db, payload.prescription_request_id, self.hospital_id)
            if rad_request.patient_id != patient.id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Prescription request does not belong to the selected patient",
                )
            if payload.scan_ids:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Doctor-prescribed orders cannot add or change scans; fulfill the prescription as written",
                )

            # Legacy requests (or ones whose charge was cancelled at the billing desk) get
            # their canonical charge back before the clearance check.
            rad_charge = ensure_rad_request_charge(self.db, rad_request, created_by_name=_actor_name(self.user))
            if rad_charge:
                auto_apply_eligible_deposits_to_charge(
                    self.db,
                    hospital_id=self.hospital_id,
                    charge=rad_charge,
                    created_by_name=_actor_name(self.user),
                )

            # Enforce financial clearance when accessioning doctor-prescribed radiology requests
            # A clinical priority flag (payload.is_emergency) never bypasses billing on its own.
            is_em = bool(getattr(payload, "is_emergency_override", False))
            em_reason = getattr(payload, "emergency_override_reason", None) or getattr(payload, "emergency_reason", None)
            assert_service_financially_cleared(
                self.db,
                self.hospital_id,
                BillingSourceType.radiology,
                rad_request.id,
                action_description="accession radiology prescription request",
                is_emergency_override=is_em,
                actor=self.user,
                emergency_reason=em_reason,
                admission_id=rad_request.admission_id,
                patient_id=rad_request.patient_id,
            )

            fulfill_items = assert_request_fulfillable(self.db, rad_request)
            appointment_id = rad_request.appointment_id or appointment_id
            doctor = rad_request.doctor or doctor
            if not clinical_notes:
                clinical_notes = rad_request.clinical_notes
            for it in fulfill_items:
                lines.append((it.scan_id, it.scan_code, it.scan_name, it.category, it.price))
        else:
            scans = self.repo.get_active_scans_by_ids(payload.scan_ids)
            if len(scans) != len(set(payload.scan_ids)):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="One or more scans are invalid or inactive",
                )
            for scan in scans:
                lines.append((scan.id, scan.scan_code, scan.scan_name, scan.category, scan.price))

        admission_id = payload.admission_id
        if rad_request and rad_request.admission_id:
            admission_id = rad_request.admission_id

        if admission_id:
            admission = (
                self.db.query(Admission)
                .filter(Admission.id == admission_id, Admission.hospital_id == self.hospital_id)
                .first()
            )
            if not admission:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admission not found")
            if admission.patient_id != patient.id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Admission does not belong to the selected patient",
                )

        if appointment_id:
            appt = (
                self.db.query(Appointment)
                .filter(Appointment.id == appointment_id, Appointment.hospital_id == self.hospital_id)
                .first()
            )
            if not appt:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appointment not found")
            if appt.patient_id != patient.id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Appointment does not belong to the selected patient",
                )

        actor_name = _actor_name(self.user)
        actor_role = _actor_role(self.user)
        created_ids: list[UUID] = []

        direct_account_id = None
        if not rad_request:
            direct_account_id = resolve_episode_account(
                self.db,
                self.hospital_id,
                patient.id,
                admission_id=admission_id,
                created_by_name=actor_name,
            ).account_id

        order_nos = iter(self.repo.next_order_no_batch(len(lines)))
        for scan_id, scan_code, scan_name, category, price in lines:
            order_no = next(order_nos)
            order = RadiologyOrder(
                hospital_id=self.hospital_id,
                order_no=order_no,
                patient_id=patient.id,
                doctor_id=doctor.id if doctor else None,
                appointment_id=appointment_id,
                admission_id=admission_id,
                prescription_id=rad_request.prescription_id if rad_request else None,
                prescription_request_id=rad_request.id if rad_request else None,
                scan_id=scan_id,
                scan_code=scan_code,
                scan_name=scan_name,
                category=category,
                price=price,
                ordered_by_name=actor_name,
                ordered_by_role=actor_role,
                status=RadiologyOrderStatus.ordered,
                clinical_notes=clinical_notes,
            )
            self.db.add(order)
            self.db.flush()
            created_ids.append(order.id)

            # Prescription orders are billed by the request's canonical charge (resolved
            # via order.prescription_request_id); direct orders own a charge on order.id.
            if not rad_request:
                rad_charge = ensure_service_charge(
                    self.db,
                    hospital_id=self.hospital_id,
                    patient_id=patient.id,
                    source_type=BillingSourceType.radiology,
                    source_id=order.id,
                    description=f"Radiology {order.order_no} — {scan_name}",
                    charge_amount=float(price or 0),
                    account_id=direct_account_id,
                    created_by_name=actor_name,
                )
                if rad_charge:
                    auto_apply_eligible_deposits_to_charge(
                        self.db,
                        hospital_id=self.hospital_id,
                        charge=rad_charge,
                        created_by_name=actor_name,
                    )

            write_audit_log(
                self.db,
                hospital_id=self.hospital_id,
                actor=self.user,
                action="create",
                entity_type="radiology_order",
                entity_id=order.id,
                summary=f"Radiology order {order.order_no} for {patient.name}: {scan_name}",
                details={"order_id": str(order.id), "order_no": order.order_no, "patient_id": str(order.patient_id)},
            )
            stage_sync_event(
                self.db,
                hospital_id=self.hospital_id,
                entity_type="radiology_order",
                action="create",
                entity_id=order.id,
            )

        if rad_request:
            for it in fulfill_items:
                it.status = RadRequestItemStatus.ordered
            self.db.flush()
            recompute_rad_request_status(self.db, rad_request)

        self.db.commit()

        if appointment_id:
            sync_appointment_after_clinical_change(self.db, self.hospital_id, appointment_id)
            self.db.commit()

        return [order_to_response(self.repo.get_order(oid)) for oid in created_ids]


class ListRadPrescriptionRequestsAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = RadiologyRepository(db, hospital_id)

    def execute(
        self,
        status: RadPrescriptionRequestStatus | None = None,
        patient_id: UUID | None = None,
        doctor_id: UUID | None = None,
        released_only: bool | None = None,
    ) -> list[RadPrescriptionRequestResponse]:
        reqs = self.repo.list_prescription_requests(
            status=status, patient_id=patient_id, doctor_id=doctor_id
        )
        if not reqs:
            return []

        apply_release_filter = released_only is True

        from modules.billing.entities.billing_entities import BillingSourceType
        from modules.billing.services.service_financial_clearance import (
            bulk_check_service_financial_clearance,
        )
        from modules.billing.entities.financial_exception import (
            FinancialClearanceException,
            FinancialExceptionStatus,
        )

        req_ids = [r.id for r in reqs]
        fin_states: dict = {}
        if req_ids:
            fin_states = bulk_check_service_financial_clearance(
                self.db,
                self.hospital_id,
                BillingSourceType.radiology,
                req_ids,
            )

        # Bulk fetch active approved unconsumed financial clearance exceptions
        exc_set: set[UUID] = set()
        if req_ids:
            excs = (
                self.db.query(FinancialClearanceException.source_id)
                .filter(
                    FinancialClearanceException.hospital_id == self.hospital_id,
                    FinancialClearanceException.source_id.in_(req_ids),
                    FinancialClearanceException.status == FinancialExceptionStatus.approved.value,
                    FinancialClearanceException.is_consumed == False,
                )
                .all()
            )
            exc_set = {row[0] for row in excs}

        if apply_release_filter:
            filtered_reqs = []
            for r in reqs:
                is_stat = bool(getattr(r, "is_emergency", False))
                fin = fin_states.get(r.id)
                is_fin_cleared = bool(fin and fin.is_cleared)
                has_exc = r.id in exc_set
                if is_stat or is_fin_cleared or has_exc:
                    filtered_reqs.append(r)
        else:
            filtered_reqs = reqs

        return [
            RadPrescriptionRequestResponse(
                **request_to_response_dict(
                    r,
                    db=None,
                    financial_state=fin_states.get(r.id),
                    exception_cleared=(r.id in exc_set),
                )
            )
            for r in filtered_reqs
        ]


class GetRadPrescriptionRequestAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = RadiologyRepository(db, hospital_id)

    def execute(self, request_id: UUID) -> RadPrescriptionRequestResponse:
        req = self.repo.get_prescription_request(request_id)
        if not req:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Prescription radiology request not found",
            )
        return RadPrescriptionRequestResponse(**request_to_response_dict(req, self.repo.db))


class CancelRadPrescriptionRequestAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = RadiologyRepository(db, hospital_id)

    def execute(
        self,
        request_id: UUID,
        reason: str | None,
        user: dict,
        *,
        commit: bool = True,
        sync_appointment: bool = True,
    ) -> RadPrescriptionRequestResponse:
        req = apply_cancel_rad_prescription_request(self.db, request_id, self.hospital_id, reason)
        cancel_service_charges_or_400(
            self.db,
            self.hospital_id,
            BillingSourceType.radiology,
            request_id,
            reason=reason or "Radiology prescription request cancelled",
            actor=user,
        )
        if commit:
            self.db.commit()
        if sync_appointment and req.appointment_id:
            sync_appointment_after_clinical_change(self.db, self.hospital_id, req.appointment_id)
            if commit:
                self.db.commit()
        return RadPrescriptionRequestResponse(**request_to_response_dict(req, self.db))


class MarkRadRequestItemUnavailableAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    def execute(self, request_id: UUID, item_id: UUID, user: dict) -> RadPrescriptionRequestResponse:
        req = get_prescription_request(self.db, request_id, self.hospital_id)
        if req.status in {RadPrescriptionRequestStatus.completed, RadPrescriptionRequestStatus.cancelled}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Request cannot be modified")
        item = next((i for i in (req.items or []) if i.id == item_id), None)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
        if item.status != RadRequestItemStatus.pending:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only pending scans can be marked unavailable",
            )
        item.status = RadRequestItemStatus.unavailable
        self.db.flush()
        if not [i for i in req.items if i.status in (RadRequestItemStatus.pending, RadRequestItemStatus.ordered)]:
            req.cancel_reason = "All requested scans marked unavailable"
        recompute_rad_request_status(self.db, req)
        # The request's single charge now bills only its remaining pending + ordered scans.
        sync_rad_request_charge(
            self.db,
            req,
            reason=f"Radiology scan {item.scan_code} marked unavailable",
            actor=user,
        )

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=user,
            action="cancel_item",
            entity_type="rad_prescription_request",
            entity_id=request_id,
            summary="Marked radiology request item unavailable",
            details={"request_id": str(request_id), "item_id": str(item_id)},
        )
        self.db.commit()
        if req.appointment_id:
            sync_appointment_after_clinical_change(self.db, self.hospital_id, req.appointment_id)
            self.db.commit()
        return RadPrescriptionRequestResponse(**request_to_response_dict(req, self.db))


class CancelOrderAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = RadiologyRepository(db, hospital_id)

    def execute(self, order_id: UUID) -> RadOrderResponse:
        order = self.repo.get_order(order_id)
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Radiology order not found")
        if order.status == RadiologyOrderStatus.completed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Completed orders cannot be cancelled",
            )

        # A walk-in order's own charge is cancelled with it. An order fulfilling a
        # prescription request owns no charge: the request's charge stays active and the
        # scan returns to pending on the request so it can be re-accessioned.
        if not order.prescription_request_id:
            cancel_service_charges_or_400(
                self.db,
                self.hospital_id,
                BillingSourceType.radiology,
                order.id,
                reason=f"Radiology order {order.order_no} cancelled",
                actor=self.user,
                performed=order.status == RadiologyOrderStatus.in_progress,
            )
        order.status = RadiologyOrderStatus.cancelled

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="radiology_order",
            entity_id=order.id,
            summary=f"Cancelled radiology order {order.order_no}",
            details={"order_id": str(order.id), "order_no": order.order_no, "status": "cancelled", "patient_id": str(order.patient_id)},
        )
        stage_sync_event(
            self.db,
            hospital_id=self.hospital_id,
            entity_type="radiology_order",
            action="cancel",
            entity_id=order.id,
        )
        sync_request_after_order_change(self.db, order)
        self.db.commit()

        if order.appointment_id:
            sync_appointment_after_clinical_change(self.db, self.hospital_id, order.appointment_id)
            self.db.commit()

        return order_to_response(self.repo.get_order(order_id))


class ScheduleOrderAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = RadiologyRepository(db, hospital_id)

    def execute(self, order_id: UUID, payload: RadScheduleRequest) -> RadOrderResponse:
        order = self.repo.get_order(order_id)
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Radiology order not found")
        if order.status in {RadiologyOrderStatus.cancelled, RadiologyOrderStatus.completed}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot schedule this order")

        _gate_order(self.db, self.hospital_id, order, self.user, "schedule scan", payload)

        order.scheduled_at = payload.scheduled_at
        order.machine = payload.machine.strip()
        order.technician_name = payload.technician_name.strip()
        order.status = RadiologyOrderStatus.scheduled

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="radiology_order",
            entity_id=order.id,
            summary=f"Scheduled {order.order_no} on {order.machine} at {payload.scheduled_at.isoformat()}",
            details={"order_id": str(order.id), "status": "scheduled", "patient_id": str(order.patient_id)},
        )
        stage_sync_event(
            self.db,
            hospital_id=self.hospital_id,
            entity_type="radiology_order",
            action="schedule",
            entity_id=order.id,
        )
        self.db.commit()
        return order_to_response(self.repo.get_order(order_id), self.repo.db)


class StartScanAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = RadiologyRepository(db, hospital_id)

    def execute(self, order_id: UUID, payload: Any | None = None) -> RadOrderResponse:
        order = self.repo.get_order(order_id)
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Radiology order not found")
        if order.status not in {RadiologyOrderStatus.scheduled, RadiologyOrderStatus.ordered}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Order must be scheduled (or ordered) to start",
            )

        _gate_order(self.db, self.hospital_id, order, self.user, "start scan acquisition", payload)

        order.status = RadiologyOrderStatus.in_progress
        order.started_at = datetime.now(timezone.utc)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="radiology_order",
            entity_id=order.id,
            summary=f"Started scan for {order.order_no}",
            details={"order_id": str(order.id), "status": "in_progress", "patient_id": str(order.patient_id)},
        )
        stage_sync_event(
            self.db,
            hospital_id=self.hospital_id,
            entity_type="radiology_order",
            action="in_progress",
            entity_id=order.id,
        )
        self.db.commit()
        return order_to_response(self.repo.get_order(order_id), self.repo.db)


class CompleteScanAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = RadiologyRepository(db, hospital_id)

    def execute(self, order_id: UUID, payload: Any | None = None) -> RadOrderResponse:
        order = self.repo.get_order(order_id)
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Radiology order not found")
        if order.status not in {RadiologyOrderStatus.in_progress, RadiologyOrderStatus.scheduled}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Scan is not in progress",
            )
        _gate_order(self.db, self.hospital_id, order, self.user, "complete scan", payload)

        order.status = RadiologyOrderStatus.completed
        order.completed_at = datetime.now(timezone.utc)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="radiology_order",
            entity_id=order.id,
            summary=f"Completed scan for {order.order_no} (report may still be pending)",
            details={"order_id": str(order.id), "status": "completed", "patient_id": str(order.patient_id)},
        )
        stage_sync_event(
            self.db,
            hospital_id=self.hospital_id,
            entity_type="radiology_order",
            action="completed",
            entity_id=order.id,
        )
        sync_request_after_order_change(self.db, order)
        self.db.commit()

        if order.appointment_id:
            sync_appointment_after_clinical_change(self.db, self.hospital_id, order.appointment_id)
            self.db.commit()

        return order_to_response(self.repo.get_order(order_id), self.repo.db)


class UploadReportAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = RadiologyRepository(db, hospital_id)

    def execute(self, order_id: UUID, payload: RadReportRequest) -> RadOrderResponse:
        order = self.repo.get_order(order_id)
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Radiology order not found")
        if order.status == RadiologyOrderStatus.cancelled:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Order is cancelled")

        if payload.report_file_data and len(payload.report_file_data) > 2_500_000:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Report file too large (max ~1.5MB)")
        if payload.image_file_data and len(payload.image_file_data) > 2_500_000:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Image file too large (max ~1.5MB)")
        # Scan can be satisfied by payload image, legacy image_file_data, OR uploaded attachments
        has_attachments = bool(order.attachments and len(order.attachments) > 0)
        if not payload.image_file_data and not order.image_file_data and not has_attachments:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Scan image or attachment is required")

        # A completed order already passed the gate (scan completion / first report);
        # amending its report is not re-gated.
        was_completed = order.status == RadiologyOrderStatus.completed
        if not was_completed:
            _gate_order(self.db, self.hospital_id, order, self.user, "upload radiology report", payload)

        # FLAW-009: Diagnostic immutability & amendment tracking
        is_prior_completed = _is_report_finalized(order)
        if is_prior_completed:
            if not payload.amendment_reason or not payload.amendment_reason.strip():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Finalized radiology report cannot be modified without providing an amendment reason.",
                )
            order.is_amended = True
            order.amendment_reason = payload.amendment_reason.strip()

        order.findings = payload.findings.strip()
        order.impression = payload.impression.strip()
        order.remarks = payload.remarks.strip() if payload.remarks else None
        order.report_date = payload.report_date or date.today()
        order.report_uploaded_by = _actor_name(self.user)
        if payload.report_file_name:
            order.report_file_name = payload.report_file_name
        if payload.report_file_data:
            order.report_file_data = payload.report_file_data
        if payload.image_file_data:
            order.image_file_name = payload.image_file_name
            order.image_file_data = payload.image_file_data

        if order.status != RadiologyOrderStatus.completed:
            order.status = RadiologyOrderStatus.completed
            order.completed_at = order.completed_at or datetime.now(timezone.utc)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="radiology_report",
            entity_id=order.id,
            summary=f"Uploaded radiology report for {order.order_no}",
            details={"order_id": str(order.id), "status": order.status.value, "patient_id": str(order.patient_id)},
        )
        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="complete",
            entity_type="radiology_order",
            entity_id=order.id,
            summary=f"Radiology order {order.order_no} finalized with report",
            details={"order_id": str(order.id), "status": "completed", "patient_id": str(order.patient_id)},
        )
        stage_sync_event(
            self.db,
            hospital_id=self.hospital_id,
            entity_type="radiology_report",
            action="complete",
            entity_id=order.id,
        )
        if not was_completed:
            sync_request_after_order_change(self.db, order)
        self.db.commit()

        order = self.repo.get_order(order_id)
        sync_radiology_order_medical_record(self.db, order)
        if order.appointment_id:
            sync_appointment_after_clinical_change(self.db, self.hospital_id, order.appointment_id)
        self.db.commit()

        return order_to_response(self.repo.get_order(order_id))


class AddAttachmentAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = RadiologyRepository(db, hospital_id)

    def execute(
        self,
        order_id: UUID,
        file_name: str,
        mime_type: str,
        file_size: int,
        storage_path: str | None = None,
        attachment_type: str = "scan_plate",
        *,
        file_bytes: bytes | None = None,
        is_emergency_override: bool = False,
        emergency_override_reason: str | None = None,
    ) -> RadAttachmentResponse:
        """Attach an acquisition file. With ``file_bytes`` the file is written only after
        the order passed its checks and financial gate (and removed if the commit fails)."""
        order = self.repo.get_order(order_id)
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Radiology order not found")
        if order.status == RadiologyOrderStatus.cancelled:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Order is cancelled")
        if _is_report_finalized(order):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot add attachments after the report has been finalized.",
            )
        if file_bytes is None and not storage_path:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Attachment file is required")

        _gate_order(
            self.db,
            self.hospital_id,
            order,
            self.user,
            "upload scan attachment",
            SimpleNamespace(
                is_emergency_override=is_emergency_override,
                emergency_override_reason=emergency_override_reason,
            ),
        )

        written_path = None
        if file_bytes is not None:
            import uuid as _uuid

            from shared.storage.local_storage import save_attachment_file

            written_path = save_attachment_file(
                module="radiology",
                hospital_id=self.hospital_id,
                order_id=order.id,
                file_id=_uuid.uuid4(),
                file_name=file_name,
                data=file_bytes,
            )
            storage_path = written_path

        att = RadiologyAttachment(
            hospital_id=self.hospital_id,
            order_id=order.id,
            file_name=file_name,
            mime_type=mime_type,
            file_size=file_size,
            storage_path=storage_path,
            attachment_type=attachment_type,
            uploaded_by=_actor_name(self.user),
        )
        self.db.add(att)

        # Transition order to in_progress or update technician if scheduled/ordered
        if order.status == RadiologyOrderStatus.ordered:
            order.status = RadiologyOrderStatus.in_progress
            order.started_at = order.started_at or datetime.now(timezone.utc)
            if not order.technician_name:
                order.technician_name = _actor_name(self.user)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="upload_attachment",
            entity_type="radiology_scan",
            entity_id=att.id,
            summary=f"Uploaded attachment {file_name} for order {order.order_no}",
            details={"order_id": str(order.id), "attachment_id": str(att.id)},
        )
        if order.status == RadiologyOrderStatus.in_progress:
            write_audit_log(
                self.db,
                hospital_id=self.hospital_id,
                actor=self.user,
                action="status_change",
                entity_type="radiology_order",
                entity_id=order.id,
                summary=f"Radiology order {order.order_no} status changed to in_progress",
                details={"order_id": str(order.id), "status": "in_progress"},
            )

        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            if written_path:
                from shared.storage.local_storage import delete_attachment_file

                delete_attachment_file(written_path)
            raise
        self.db.refresh(att)
        return RadAttachmentResponse.model_validate(att)


class DeleteAttachmentAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = RadiologyRepository(db, hospital_id)

    def execute(self, order_id: UUID, attachment_id: UUID) -> None:
        order = self.repo.get_order(order_id)
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Radiology order not found")
        if order.status == RadiologyOrderStatus.completed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot delete attachments after report has been finalized and released.",
            )

        att = self.repo.get_attachment(attachment_id)
        if not att or att.order_id != order_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found on order")

        # Delete physical file from disk storage if exists
        from shared.storage.local_storage import delete_attachment_file
        delete_attachment_file(att.storage_path)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="delete_attachment",
            entity_type="radiology_scan",
            entity_id=attachment_id,
            summary=f"Deleted attachment {att.file_name} from order {order.order_no}",
            details={"order_id": str(order.id), "attachment_id": str(attachment_id)},
        )
        self.db.delete(att)
        self.db.commit()


class GetDashboardAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = RadiologyRepository(db, hospital_id)

    def execute(self) -> RadDashboardResponse:
        metrics = self.repo.get_dashboard_metrics()
        return RadDashboardResponse(
            todays_orders=metrics["todays_orders"],
            pending_scans=metrics["pending_scans"],
            completed_scans=metrics["completed_scans"],
            reports_pending=metrics["reports_pending"],
            cancelled_orders=metrics["cancelled_orders"],
            scans_count=metrics["scans_count"],
            seeded_scans_estimate=len(STANDARD_RADIOLOGY_SCANS),
        )
