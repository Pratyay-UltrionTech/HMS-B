"""Laboratory Encounter Workflow Reader.

Authoritative domain service providing diagnostic lifecycle interpretation
for appointments and inpatient encounters without leaking internal ORM rows.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from modules.laboratory.entities.lab_entities import (
    LabOrder,
    LabOrderItem,
    LabItemStatus,
    LabOrderStatus,
    LabPrescriptionRequest,
    LabPrescriptionRequestItem,
    LabPrescriptionRequestStatus,
    LabRequestItemStatus,
    LabResult,
)
from shared.diagnostics.contracts import DiagnosticDomainSummary

_OPEN_LAB_ORDER_STATUSES = {
    LabOrderStatus.ordered,
    LabOrderStatus.sample_collected,
    LabOrderStatus.in_progress,
}
_OPEN_LAB_REQUEST_STATUSES = {
    LabPrescriptionRequestStatus.pending,
    LabPrescriptionRequestStatus.partially_processed,
}


class LabEncounterWorkflowReader:
    """Evaluates laboratory investigation lifecycle for encounters in bulk."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get_for_appointment(
        self, hospital_id: UUID, appointment_id: UUID
    ) -> DiagnosticDomainSummary:
        res = self.bulk_get_for_appointments(hospital_id, [appointment_id])
        return res.get(appointment_id, DiagnosticDomainSummary(domain="laboratory"))

    def bulk_get_for_appointments(
        self, hospital_id: UUID, appointment_ids: list[UUID]
    ) -> dict[UUID, DiagnosticDomainSummary]:
        if not appointment_ids:
            return {}

        summaries: dict[UUID, DiagnosticDomainSummary] = {
            a_id: DiagnosticDomainSummary(domain="laboratory") for a_id in appointment_ids
        }

        # 1. Orders
        orders = (
            self.db.query(
                LabOrder.id,
                LabOrder.appointment_id,
                LabOrder.order_no,
                LabOrder.status,
            )
            .filter(
                LabOrder.hospital_id == hospital_id,
                LabOrder.appointment_id.in_(appointment_ids),
            )
            .all()
        )

        completed_order_ids = [
            order_id for order_id, _appt_id, _order_no, st in orders if st == LabOrderStatus.completed
        ]
        completed_orders_with_results: set[UUID] = set()
        if completed_order_ids:
            res_order_ids = (
                self.db.query(LabResult.order_id)
                .filter(
                    LabResult.hospital_id == hospital_id,
                    LabResult.order_id.in_(completed_order_ids),
                )
                .distinct()
                .all()
            )
            completed_orders_with_results = {row[0] for row in res_order_ids}

        for order_id, appt_id, order_no, st in orders:
            s = summaries.get(appt_id)
            if not s:
                continue
            if st in _OPEN_LAB_ORDER_STATUSES:
                s.active_order_count += 1
                status_label = (st.value if hasattr(st, "value") else str(st)).replace("_", " ")
                s.blocker_descriptions.append(f"Lab order {order_no} is {status_label}")
            elif st == LabOrderStatus.completed:
                if order_id in completed_orders_with_results:
                    s.finalized_result_count += 1
                else:
                    s.report_pending_count += 1
                    status_label = "completed processing (final result pending)"
                    s.blocker_descriptions.append(f"Lab order {order_no} {status_label}")
            elif st == LabOrderStatus.cancelled:
                s.cancelled_count += 1

        # 2. Prescription Requests
        requests = (
            self.db.query(
                LabPrescriptionRequest.id,
                LabPrescriptionRequest.appointment_id,
                LabPrescriptionRequest.status,
            )
            .filter(
                LabPrescriptionRequest.hospital_id == hospital_id,
                LabPrescriptionRequest.appointment_id.in_(appointment_ids),
            )
            .all()
        )
        req_ids: list[UUID] = []
        for req_id, appt_id, st in requests:
            req_ids.append(req_id)
            s = summaries.get(appt_id)
            if not s:
                continue
            s.requested_count += 1
            if st in _OPEN_LAB_REQUEST_STATUSES:
                s.open_request_count += 1
                status_label = (st.value if hasattr(st, "value") else str(st)).replace("_", " ")
                s.blocker_descriptions.append(f"Doctor lab request pending ({status_label})")
            elif st == LabPrescriptionRequestStatus.cancelled:
                s.cancelled_count += 1

        # 3. Check items for unavailable/cancelled sub-states
        if req_ids:
            items = (
                self.db.query(
                    LabPrescriptionRequest.appointment_id,
                    LabPrescriptionRequestItem.status,
                )
                .join(LabPrescriptionRequest, LabPrescriptionRequestItem.request_id == LabPrescriptionRequest.id)
                .filter(
                    LabPrescriptionRequest.hospital_id == hospital_id,
                    LabPrescriptionRequest.id.in_(req_ids),
                )
                .all()
            )
            for appt_id, item_st in items:
                s = summaries.get(appt_id)
                if not s:
                    continue
                if item_st == LabRequestItemStatus.unavailable:
                    s.unavailable_count += 1

        # 4. Synthesize semantic booleans
        for s in summaries.values():
            s.has_open_work = (
                s.open_request_count > 0
                or s.active_order_count > 0
                or s.report_pending_count > 0
            )
            s.has_final_result = (s.finalized_result_count > 0)
            has_any = (
                s.requested_count > 0
                or s.active_order_count > 0
                or s.report_pending_count > 0
                or s.finalized_result_count > 0
                or s.cancelled_count > 0
                or s.unavailable_count > 0
            )
            s.requires_doctor_attention = (
                has_any
                and not s.has_open_work
                and not s.has_final_result
                and (s.cancelled_count > 0 or s.unavailable_count > 0)
            )

        return summaries
