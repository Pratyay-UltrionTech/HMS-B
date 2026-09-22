"""Prescription cancel / discard lifecycle (no hard-delete of issued rows)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, joinedload

from modules.clinical_records.actions.clinical_actions import to_prescription_response
from modules.clinical_records.contracts.clinical_contracts import (
    PrescriptionCancelRequest,
    PrescriptionResponse,
)
from modules.clinical_records.db.clinical_records_repository import ClinicalRecordsRepository
from modules.clinical_records.entities.clinical_record import Prescription
from modules.laboratory.actions.laboratory_actions import CancelLabPrescriptionRequestAction
from modules.laboratory.entities.lab_entities import (
    LabOrder,
    LabOrderStatus,
    LabPrescriptionRequest,
    LabPrescriptionRequestStatus,
)
from modules.pharmacy.entities.pharmacy_entities import PharmacyRxRequest, PharmacyRxRequestStatus
from modules.radiology.actions.radiology_actions import CancelRadPrescriptionRequestAction
from modules.radiology.entities.radiology_entities import (
    RadiologyOrder,
    RadiologyOrderStatus,
    RadPrescriptionRequest,
    RadPrescriptionRequestStatus,
)
from shared.audit.service import write_audit_log

TERMINAL_RX_STATUSES = frozenset({"cancelled", "discarded"})
BLOCKING_PHARMACY_STATUSES = frozenset(
    {
        PharmacyRxRequestStatus.partially_dispensed,
        PharmacyRxRequestStatus.dispensed,
        PharmacyRxRequestStatus.completed,
    }
)
ACTIVE_LAB_ORDER_STATUSES = frozenset(
    {
        LabOrderStatus.ordered,
        LabOrderStatus.sample_collected,
        LabOrderStatus.in_progress,
        LabOrderStatus.completed,
    }
)
ACTIVE_RAD_ORDER_STATUSES = frozenset(
    {
        RadiologyOrderStatus.ordered,
        RadiologyOrderStatus.scheduled,
        RadiologyOrderStatus.in_progress,
        RadiologyOrderStatus.completed,
    }
)
CANCELLABLE_LAB_REQUEST_STATUSES = frozenset({LabPrescriptionRequestStatus.pending})
CANCELLABLE_RAD_REQUEST_STATUSES = frozenset({RadPrescriptionRequestStatus.pending})


def _actor_label(actor: dict[str, Any]) -> str:
    name = str(actor.get("name") or actor.get("sub") or "Staff")
    role = str(actor.get("staff_role_name") or actor.get("role") or "")
    return f"{name} ({role})".strip() if role else name


class CancelPrescriptionAction:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = ClinicalRecordsRepository(db)

    def execute(
        self,
        hospital_id: UUID,
        doctor_id: UUID,
        prescription_id: UUID,
        payload: PrescriptionCancelRequest,
        actor: dict[str, Any],
    ) -> PrescriptionResponse:
        rx = self.repo.get_prescription(hospital_id, doctor_id, prescription_id)
        if not rx:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Prescription not found")

        if rx.doctor_id != doctor_id and actor.get("role") != "hospital_admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot edit a prescription written by another doctor",
            )

        current = (getattr(rx, "status", None) or "issued").strip().lower()
        if current in TERMINAL_RX_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Prescription is already cancelled or discarded",
            )
        if current not in {"draft", "issued"}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot cancel prescription in status '{current}'",
            )

        self._assert_pharmacy_allows_cancel(hospital_id, rx.id)
        if current == "draft":
            self._assert_no_active_departmental_orders(hospital_id, rx.id)

        reason = payload.reason
        self._cancel_pending_investigation_requests(hospital_id, rx.id, reason, actor)
        self._cancel_pending_pharmacy_requests(hospital_id, rx.id, reason)

        now = datetime.now(timezone.utc)
        actor_label = _actor_label(actor)
        target_status = "discarded" if current == "draft" else "cancelled"
        rx.status = target_status
        rx.cancelled_at = now
        rx.cancelled_by = actor_label
        rx.cancel_reason = reason

        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="discard" if target_status == "discarded" else "cancel",
            entity_type="prescription",
            entity_id=str(rx.id),
            summary=(
                f"{'Discarded draft' if target_status == 'discarded' else 'Cancelled'} "
                f"prescription {rx.id}: {reason}"
            ),
        )
        self.db.commit()
        refreshed = self.repo.get_prescription(hospital_id, doctor_id, rx.id)
        return to_prescription_response(refreshed or rx, db=self.db)

    def _assert_pharmacy_allows_cancel(self, hospital_id: UUID, prescription_id: UUID) -> None:
        rows = (
            self.db.query(PharmacyRxRequest)
            .options(joinedload(PharmacyRxRequest.items))
            .filter(
                PharmacyRxRequest.hospital_id == hospital_id,
                PharmacyRxRequest.prescription_id == prescription_id,
            )
            .all()
        )
        for rx_req in rows:
            status_val = (
                rx_req.status.value if hasattr(rx_req.status, "value") else str(rx_req.status or "")
            )
            if rx_req.status in BLOCKING_PHARMACY_STATUSES or status_val == "fully_dispensed":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Cannot cancel this prescription because pharmacy has already "
                        f"dispensed against it (status: {status_val})."
                    ),
                )
            if rx_req.status == PharmacyRxRequestStatus.cancelled:
                continue
            dispensed = sum(float(item.dispensed_quantity or 0) for item in (rx_req.items or []))
            if dispensed > 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Cannot cancel this prescription because pharmacy items have "
                        "already been partially dispensed."
                    ),
                )

    def _assert_no_active_departmental_orders(self, hospital_id: UUID, prescription_id: UUID) -> None:
        lab_order = (
            self.db.query(LabOrder)
            .filter(
                LabOrder.hospital_id == hospital_id,
                LabOrder.prescription_id == prescription_id,
                LabOrder.status.in_(list(ACTIVE_LAB_ORDER_STATUSES)),
            )
            .first()
        )
        if lab_order:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Cannot discard this draft: laboratory work is already in progress "
                    f"or completed (order {lab_order.order_no})."
                ),
            )
        rad_order = (
            self.db.query(RadiologyOrder)
            .filter(
                RadiologyOrder.hospital_id == hospital_id,
                RadiologyOrder.prescription_id == prescription_id,
                RadiologyOrder.status.in_(list(ACTIVE_RAD_ORDER_STATUSES)),
            )
            .first()
        )
        if rad_order:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Cannot discard this draft: radiology work is already in progress "
                    f"or completed (order {rad_order.order_no})."
                ),
            )

        lab_reqs = (
            self.db.query(LabPrescriptionRequest)
            .filter(
                LabPrescriptionRequest.hospital_id == hospital_id,
                LabPrescriptionRequest.prescription_id == prescription_id,
            )
            .all()
        )
        for lab_req in lab_reqs:
            if lab_req.status in {
                LabPrescriptionRequestStatus.partially_processed,
                LabPrescriptionRequestStatus.completed,
            }:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Cannot discard this draft: laboratory work is already in progress "
                        f"or completed (request {lab_req.id})."
                    ),
                )
        lab_req_ids = [r.id for r in lab_reqs]
        if lab_req_ids:
            linked_lab = (
                self.db.query(LabOrder)
                .filter(
                    LabOrder.hospital_id == hospital_id,
                    LabOrder.prescription_request_id.in_(lab_req_ids),
                    LabOrder.status.in_(list(ACTIVE_LAB_ORDER_STATUSES)),
                )
                .first()
            )
            if linked_lab:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Cannot discard this draft: laboratory work is already in progress "
                        f"or completed (order {linked_lab.order_no})."
                    ),
                )

        rad_reqs = (
            self.db.query(RadPrescriptionRequest)
            .filter(
                RadPrescriptionRequest.hospital_id == hospital_id,
                RadPrescriptionRequest.prescription_id == prescription_id,
            )
            .all()
        )
        for rad_req in rad_reqs:
            if rad_req.status in {
                RadPrescriptionRequestStatus.partially_processed,
                RadPrescriptionRequestStatus.completed,
            }:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Cannot discard this draft: radiology work is already in progress "
                        f"or completed (request {rad_req.id})."
                    ),
                )
        rad_req_ids = [r.id for r in rad_reqs]
        if rad_req_ids:
            linked_rad = (
                self.db.query(RadiologyOrder)
                .filter(
                    RadiologyOrder.hospital_id == hospital_id,
                    RadiologyOrder.prescription_request_id.in_(rad_req_ids),
                    RadiologyOrder.status.in_(list(ACTIVE_RAD_ORDER_STATUSES)),
                )
                .first()
            )
            if linked_rad:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Cannot discard this draft: radiology work is already in progress "
                        f"or completed (order {linked_rad.order_no})."
                    ),
                )

    def _cancel_pending_investigation_requests(
        self,
        hospital_id: UUID,
        prescription_id: UUID,
        reason: str,
        actor: dict[str, Any],
    ) -> None:
        lab_action = CancelLabPrescriptionRequestAction(self.db, hospital_id)
        for req in (
            self.db.query(LabPrescriptionRequest)
            .filter(
                LabPrescriptionRequest.hospital_id == hospital_id,
                LabPrescriptionRequest.prescription_id == prescription_id,
                LabPrescriptionRequest.status.in_(list(CANCELLABLE_LAB_REQUEST_STATUSES)),
            )
            .all()
        ):
            lab_action.execute(
                req.id, reason, actor, commit=False, sync_appointment=False
            )

        rad_action = CancelRadPrescriptionRequestAction(self.db, hospital_id)
        for req in (
            self.db.query(RadPrescriptionRequest)
            .filter(
                RadPrescriptionRequest.hospital_id == hospital_id,
                RadPrescriptionRequest.prescription_id == prescription_id,
                RadPrescriptionRequest.status.in_(list(CANCELLABLE_RAD_REQUEST_STATUSES)),
            )
            .all()
        ):
            rad_action.execute(
                req.id, reason, actor, commit=False, sync_appointment=False
            )

    def _cancel_pending_pharmacy_requests(
        self, hospital_id: UUID, prescription_id: UUID, reason: str
    ) -> None:
        rows = (
            self.db.query(PharmacyRxRequest)
            .options(joinedload(PharmacyRxRequest.items))
            .filter(
                PharmacyRxRequest.hospital_id == hospital_id,
                PharmacyRxRequest.prescription_id == prescription_id,
                PharmacyRxRequest.status == PharmacyRxRequestStatus.pending,
            )
            .all()
        )
        for rx_req in rows:
            dispensed = sum(float(item.dispensed_quantity or 0) for item in (rx_req.items or []))
            if dispensed > 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Cannot cancel this prescription because pharmacy items have "
                        "already been partially dispensed."
                    ),
                )
            rx_req.status = PharmacyRxRequestStatus.cancelled
            note = f"Cancelled with prescription: {reason}"
            rx_req.notes = f"{rx_req.notes}\n{note}".strip() if rx_req.notes else note
            for item in rx_req.items or []:
                if (item.status or "pending") == "pending":
                    item.status = PharmacyRxRequestStatus.cancelled.value


class DeletePrescriptionAction:
    """Hard-delete prescription record after checking clinical safety & dependent orders."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = ClinicalRecordsRepository(db)

    def execute(
        self,
        hospital_id: UUID,
        doctor_id: UUID,
        prescription_id: UUID,
        actor: dict[str, Any],
    ) -> dict[str, Any]:
        rx = self.repo.get_prescription(hospital_id, doctor_id, prescription_id)
        if not rx:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Prescription not found")

        if rx.doctor_id != doctor_id and actor.get("role") != "hospital_admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot delete a prescription written by another doctor",
            )

        cancel_helper = CancelPrescriptionAction(self.db)
        # Check if medications were already dispensed by pharmacy
        cancel_helper._assert_pharmacy_allows_cancel(hospital_id, rx.id)

        # Cancel any pending investigation or pharmacy requests before deletion
        reason = "Prescription deleted by physician"
        cancel_helper._cancel_pending_investigation_requests(hospital_id, rx.id, reason, actor)
        cancel_helper._cancel_pending_pharmacy_requests(hospital_id, rx.id, reason)

        patient_id = rx.patient_id
        diagnosis = rx.diagnosis or "General Rx"

        # Record audit log (this also stages the 'prescription' 'delete' sync event)
        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="delete",
            entity_type="prescription",
            entity_id=str(rx.id),
            summary=f"Deleted prescription {rx.id} ({diagnosis}) for patient {patient_id}",
        )

        self.db.delete(rx)
        self.db.commit()

        return {
            "success": True,
            "message": "Prescription deleted successfully",
            "prescription_id": str(prescription_id),
            "patient_id": str(patient_id),
        }

