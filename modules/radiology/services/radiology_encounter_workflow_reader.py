"""Radiology Encounter Workflow Reader.

Authoritative domain service providing diagnostic imaging lifecycle interpretation
for appointments and inpatient encounters without leaking internal ORM rows.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from modules.radiology.entities.radiology_entities import (
    RadiologyOrder,
    RadiologyOrderStatus,
    RadPrescriptionRequest,
    RadPrescriptionRequestItem,
    RadPrescriptionRequestStatus,
    RadRequestItemStatus,
)
from modules.radiology.services.report_status import is_radiology_report_finalized
from shared.diagnostics.contracts import DiagnosticDomainSummary

_OPEN_RAD_ORDER_STATUSES = {
    RadiologyOrderStatus.ordered,
    RadiologyOrderStatus.scheduled,
    RadiologyOrderStatus.in_progress,
}
_OPEN_RAD_REQUEST_STATUSES = {
    RadPrescriptionRequestStatus.pending,
    RadPrescriptionRequestStatus.partially_processed,
}


class RadiologyEncounterWorkflowReader:
    """Evaluates radiology investigation lifecycle for encounters in bulk."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get_for_appointment(
        self, hospital_id: UUID, appointment_id: UUID
    ) -> DiagnosticDomainSummary:
        res = self.bulk_get_for_appointments(hospital_id, [appointment_id])
        return res.get(appointment_id, DiagnosticDomainSummary(domain="radiology"))

    def bulk_get_for_appointments(
        self, hospital_id: UUID, appointment_ids: list[UUID]
    ) -> dict[UUID, DiagnosticDomainSummary]:
        if not appointment_ids:
            return {}

        summaries: dict[UUID, DiagnosticDomainSummary] = {
            a_id: DiagnosticDomainSummary(domain="radiology") for a_id in appointment_ids
        }

        # 1. Orders
        orders = (
            self.db.query(
                RadiologyOrder.appointment_id,
                RadiologyOrder.order_no,
                RadiologyOrder.status,
                RadiologyOrder.findings,
                RadiologyOrder.impression,
                RadiologyOrder.report_file_name,
            )
            .filter(
                RadiologyOrder.hospital_id == hospital_id,
                RadiologyOrder.appointment_id.in_(appointment_ids),
            )
            .all()
        )
        for appt_id, order_no, st, findings, impression, r_file_name in orders:
            s = summaries.get(appt_id)
            if not s:
                continue
            if st in _OPEN_RAD_ORDER_STATUSES:
                s.active_order_count += 1
                status_label = (st.value if hasattr(st, "value") else str(st)).replace("_", " ")
                s.blocker_descriptions.append(f"Radiology order {order_no} is {status_label}")
            elif st == RadiologyOrderStatus.completed:
                if is_radiology_report_finalized(
                    findings=findings,
                    impression=impression,
                    report_file_name=r_file_name,
                ):
                    s.finalized_result_count += 1
                else:
                    s.report_pending_count += 1
                    status_label = "completed (report pending)"
                    s.blocker_descriptions.append(f"Radiology scan {order_no} {status_label}")
            elif st == RadiologyOrderStatus.cancelled:
                s.cancelled_count += 1

        # 2. Prescription Requests
        requests = (
            self.db.query(
                RadPrescriptionRequest.id,
                RadPrescriptionRequest.appointment_id,
                RadPrescriptionRequest.status,
            )
            .filter(
                RadPrescriptionRequest.hospital_id == hospital_id,
                RadPrescriptionRequest.appointment_id.in_(appointment_ids),
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
            if st in _OPEN_RAD_REQUEST_STATUSES:
                s.open_request_count += 1
                status_label = (st.value if hasattr(st, "value") else str(st)).replace("_", " ")
                s.blocker_descriptions.append(f"Doctor radiology request pending ({status_label})")
            elif st == RadPrescriptionRequestStatus.cancelled:
                s.cancelled_count += 1

        # 3. Check items for unavailable/cancelled sub-states
        if req_ids:
            items = (
                self.db.query(
                    RadPrescriptionRequest.appointment_id,
                    RadPrescriptionRequestItem.status,
                )
                .join(RadPrescriptionRequest, RadPrescriptionRequestItem.request_id == RadPrescriptionRequest.id)
                .filter(
                    RadPrescriptionRequest.hospital_id == hospital_id,
                    RadPrescriptionRequest.id.in_(req_ids),
                )
                .all()
            )
            for appt_id, item_st in items:
                s = summaries.get(appt_id)
                if not s:
                    continue
                if item_st == RadRequestItemStatus.unavailable:
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
