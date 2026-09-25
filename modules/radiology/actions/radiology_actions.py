"""
Business use-case actions for Radiology domain.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from modules.appointments.services.appointment_lifecycle import (
    sync_appointment_after_clinical_change,
)
from modules.billing.entities.billing_entities import BillingSourceType
from modules.billing.services.billing_service import (
    cancel_charge_for_source,
    ensure_charge,
    find_charge_by_source,
)
from modules.billing.services.service_financial_clearance import (
    assert_service_financially_cleared,
)
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
    get_prescription_request,
    is_rad_request_released,
    request_to_response_dict,
    sync_request_after_order_change,
)
from shared.audit import write_audit_log


def _actor_name(user: dict[str, Any]) -> str:
    return str(user.get("name") or user.get("sub") or "Staff")


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
            fulfill_items = assert_request_fulfillable(self.db, rad_request)
            is_stat = bool(
                (rad_request.clinical_notes and "STAT" in rad_request.clinical_notes.upper())
                or (clinical_notes and "STAT" in clinical_notes.upper())
            )
            assert_service_financially_cleared(
                self.db,
                self.hospital_id,
                BillingSourceType.radiology,
                rad_request.id,
                action_description="accession doctor-prescribed radiology request",
                is_emergency_override=is_stat,
            )
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

        actor_name = _actor_name(self.user)
        actor_role = _actor_role(self.user)
        created_ids: list[UUID] = []

        req_charge = None
        if rad_request:
            req_charge = find_charge_by_source(self.db, self.hospital_id, BillingSourceType.radiology, rad_request.id)

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

            if req_charge and len(lines) == 1:
                # Re-point single charge to this order so order.id owns the paid ledger charge
                req_charge.source_id = order.id
                req_charge.description = f"Radiology {order.order_no} — {scan_name}"
            elif not rad_request:
                rad_account_id = None
                if admission_id:
                    from modules.billing.entities.billing_entities import FinancialAccountType
                    from modules.billing.services.billing_service import get_or_create_financial_account
                    rad_acc = get_or_create_financial_account(
                        self.db,
                        hospital_id=self.hospital_id,
                        patient_id=patient.id,
                        account_type=FinancialAccountType.ipd,
                        admission_id=admission_id,
                        created_by_name=actor_name,
                    )
                    rad_account_id = rad_acc.id

                # Target-native billing integration for direct orders
                ensure_charge(
                    self.db,
                    hospital_id=self.hospital_id,
                    patient_id=patient.id,
                    source_type=BillingSourceType.radiology,
                    source_id=order.id,
                    description=f"Radiology {order.order_no} — {scan_name}",
                    charge_amount=float(price or 0),
                    account_id=rad_account_id,
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

        if rad_request:
            for it in fulfill_items:
                it.status = RadRequestItemStatus.ordered
            rad_request.status = RadPrescriptionRequestStatus.partially_processed

        self.db.commit()

        if appointment_id:
            sync_appointment_after_clinical_change(self.db, self.hospital_id, appointment_id)
            self.db.commit()

        return [order_to_response(self.repo.get_order(oid)) for oid in created_ids]


class ListRadPrescriptionRequestsAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
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

        apply_release_filter = released_only is True or (
            released_only is None
            and status == RadPrescriptionRequestStatus.pending
            and not patient_id
        )

        if not apply_release_filter:
            # No release filter — simple batch path: bulk-fetch billing states and build responses.
            return [RadPrescriptionRequestResponse(**request_to_response_dict(r, self.db)) for r in reqs]

        # Release filter is active.  Bulk-fetch clearance for all request IDs so that
        # is_rad_request_released() does not issue one DB query per request.
        # STAT requests are always released without a billing query.
        from modules.billing.entities.billing_entities import BillingSourceType
        from modules.billing.services.service_financial_clearance import (
            bulk_check_service_financial_clearance,
        )

        stat_reqs: list = []
        non_stat_reqs: list = []
        for r in reqs:
            is_stat = bool(r.clinical_notes and "STAT" in r.clinical_notes.upper())
            if is_stat:
                stat_reqs.append(r)  # always released; skip billing lookup
            else:
                non_stat_reqs.append(r)

        # Single bulk query for all non-STAT requests
        fin_states: dict = {}
        if non_stat_reqs:
            if non_stat_reqs[0].hospital_id:
                fin_states = bulk_check_service_financial_clearance(
                    self.db,
                    non_stat_reqs[0].hospital_id,
                    BillingSourceType.radiology,
                    [r.id for r in non_stat_reqs],
                )

        # Filter non-STAT requests: only keep those whose charge is cleared
        released_non_stat = [
            r for r in non_stat_reqs
            if fin_states.get(r.id) and fin_states[r.id].is_cleared
        ]

        released = stat_reqs + released_non_stat

        # Build responses — request_to_response_dict will call check_service_financial_clearance
        # per request for the payment fields, but we pass the pre-fetched fin_states dict
        # via a local wrapper to avoid re-querying.
        from modules.radiology.services.rad_prescription_service import request_to_response_dict as _base_rtrd

        def _build_response(r) -> dict:
            is_stat = bool(r.clinical_notes and "STAT" in r.clinical_notes.upper())
            if is_stat:
                # For STAT, use normal single-request path (charge may or may not exist)
                return _base_rtrd(r, self.db)
            # Inject pre-fetched fin_state to avoid a second billing query per request
            fin = fin_states.get(r.id)
            if fin is None:
                return _base_rtrd(r, self.db)
            items = sorted(r.items or [], key=lambda i: (i.sort_order, str(i.id)))
            return {
                "id": r.id,
                "hospital_id": r.hospital_id,
                "prescription_id": r.prescription_id,
                "patient_id": r.patient_id,
                "doctor_id": r.doctor_id,
                "appointment_id": r.appointment_id,
                "status": r.status,
                "prescribed_scan_ids": [str(sid) for sid in (r.prescribed_scan_ids or [])],
                "clinical_notes": r.clinical_notes,
                "cancel_reason": r.cancel_reason,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
                "patient_name": r.patient.name if r.patient else None,
                "patient_uhid": r.patient.uhid if r.patient else None,
                "doctor_name": r.doctor.name if r.doctor else None,
                "scan_names": ", ".join(i.scan_code for i in (r.items or [])) or None,
                "scan_count": len(items),
                "pending_scan_count": len(
                    [i for i in items if i.status == RadRequestItemStatus.pending]
                ),
                "appointment_label": None,
                "is_financially_cleared": fin.is_cleared,
                "payment_status": fin.status,
                "outstanding_amount": fin.outstanding_amount,
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

        return [RadPrescriptionRequestResponse(**_build_response(r)) for r in released]


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
        cancel_charge_for_source(self.db, self.hospital_id, BillingSourceType.radiology, request_id)
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
        item.status = RadRequestItemStatus.unavailable

        pending = [i for i in req.items if i.status == RadRequestItemStatus.pending]
        if not pending:
            req.status = RadPrescriptionRequestStatus.cancelled
            req.cancel_reason = "All requested scans marked unavailable"

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

        order.status = RadiologyOrderStatus.cancelled
        cancel_charge_for_source(self.db, self.hospital_id, BillingSourceType.radiology, order.id)

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

        # Enforce financial clearance (with emergency STAT override)
        from modules.billing.entities.billing_entities import BillingSourceType
        from modules.billing.services.service_financial_clearance import assert_service_financially_cleared
        is_stat = bool(order.clinical_notes and "STAT" in order.clinical_notes.upper())
        assert_service_financially_cleared(
            self.db,
            self.hospital_id,
            BillingSourceType.radiology,
            order.id,
            action_description="schedule scan",
            is_emergency_override=is_stat,
        )

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
        self.db.commit()
        return order_to_response(self.repo.get_order(order_id), self.repo.db)


class StartScanAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = RadiologyRepository(db, hospital_id)

    def execute(self, order_id: UUID) -> RadOrderResponse:
        order = self.repo.get_order(order_id)
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Radiology order not found")
        if order.status not in {RadiologyOrderStatus.scheduled, RadiologyOrderStatus.ordered}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Order must be scheduled (or ordered) to start",
            )

        # Enforce financial clearance (with emergency STAT override)
        from modules.billing.entities.billing_entities import BillingSourceType
        from modules.billing.services.service_financial_clearance import assert_service_financially_cleared
        is_stat = bool(order.clinical_notes and "STAT" in order.clinical_notes.upper())
        assert_service_financially_cleared(
            self.db,
            self.hospital_id,
            BillingSourceType.radiology,
            order.id,
            action_description="start scan acquisition",
            is_emergency_override=is_stat,
        )

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
        self.db.commit()
        return order_to_response(self.repo.get_order(order_id), self.repo.db)


class CompleteScanAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = RadiologyRepository(db, hospital_id)

    def execute(self, order_id: UUID) -> RadOrderResponse:
        order = self.repo.get_order(order_id)
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Radiology order not found")
        if order.status not in {RadiologyOrderStatus.in_progress, RadiologyOrderStatus.scheduled}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Scan is not in progress",
            )

        # Enforce financial clearance
        from modules.billing.entities.billing_entities import BillingSourceType
        from modules.billing.services.service_financial_clearance import assert_service_financially_cleared
        assert_service_financially_cleared(
            self.db,
            self.hospital_id,
            BillingSourceType.radiology,
            order.id,
            action_description="complete scan acquisition",
        )

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

        # Enforce financial clearance before uploading/signing report (with emergency STAT override)
        from modules.billing.entities.billing_entities import BillingSourceType
        from modules.billing.services.service_financial_clearance import assert_service_financially_cleared
        is_stat = bool(order.clinical_notes and "STAT" in order.clinical_notes.upper())
        assert_service_financially_cleared(
            self.db,
            self.hospital_id,
            BillingSourceType.radiology,
            order.id,
            action_description="finalize and sign off radiology report",
            is_emergency_override=is_stat,
        )

        if payload.report_file_data and len(payload.report_file_data) > 2_500_000:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Report file too large (max ~1.5MB)")
        if payload.image_file_data and len(payload.image_file_data) > 2_500_000:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Image file too large (max ~1.5MB)")
        # Scan can be satisfied by payload image, legacy image_file_data, OR uploaded attachments
        has_attachments = bool(order.attachments and len(order.attachments) > 0)
        if not payload.image_file_data and not order.image_file_data and not has_attachments:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Scan image or attachment is required")

        # FLAW-009: Diagnostic immutability & amendment tracking
        is_prior_completed = bool(order.findings or order.impression)
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
        storage_path: str,
        attachment_type: str = "scan_plate",
    ) -> RadAttachmentResponse:
        order = self.repo.get_order(order_id)
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Radiology order not found")
        if order.status == RadiologyOrderStatus.cancelled:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Order is cancelled")

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

        self.db.commit()
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
