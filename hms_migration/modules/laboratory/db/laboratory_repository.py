"""
Laboratory domain database repository.

Encapsulates all SQLAlchemy queries and persistence for tests, panels,
prescription requests, orders, items, and results.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

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
    LabTestCatalog,
    LabTestPanel,
)


class LaboratoryRepository:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    # ── Test Catalogue ──────────────────────────────────────────────────────────
    def list_tests(
        self,
        department: str | None = None,
        is_active: bool | None = None,
        search: str | None = None,
    ) -> list[LabTestCatalog]:
        q = self.db.query(LabTestCatalog).filter(LabTestCatalog.hospital_id == self.hospital_id)
        if department:
            q = q.filter(LabTestCatalog.department.ilike(f"%{department}%"))
        if is_active is not None:
            q = q.filter(LabTestCatalog.is_active.is_(is_active))
        if search:
            term = f"%{search}%"
            q = q.filter(
                or_(
                    LabTestCatalog.test_code.ilike(term),
                    LabTestCatalog.test_name.ilike(term),
                    LabTestCatalog.department.ilike(term),
                )
            )
        return q.order_by(LabTestCatalog.test_name).all()

    def get_test(self, test_id: UUID) -> LabTestCatalog | None:
        return (
            self.db.query(LabTestCatalog)
            .filter(LabTestCatalog.id == test_id, LabTestCatalog.hospital_id == self.hospital_id)
            .first()
        )

    def get_test_by_code(self, test_code: str) -> LabTestCatalog | None:
        return (
            self.db.query(LabTestCatalog)
            .filter(
                LabTestCatalog.hospital_id == self.hospital_id,
                func.upper(LabTestCatalog.test_code) == test_code.strip().upper(),
            )
            .first()
        )

    # ── Panels ──────────────────────────────────────────────────────────────────
    def list_panels(self, is_active: bool | None = None) -> list[LabTestPanel]:
        q = (
            self.db.query(LabTestPanel)
            .options(joinedload(LabTestPanel.tests).joinedload(LabPanelTest.test))
            .filter(LabTestPanel.hospital_id == self.hospital_id)
        )
        if is_active is not None:
            q = q.filter(LabTestPanel.is_active.is_(is_active))
        return q.order_by(LabTestPanel.panel_name).all()

    def get_panel(self, panel_id: UUID) -> LabTestPanel | None:
        return (
            self.db.query(LabTestPanel)
            .options(joinedload(LabTestPanel.tests).joinedload(LabPanelTest.test))
            .filter(LabTestPanel.id == panel_id, LabTestPanel.hospital_id == self.hospital_id)
            .first()
        )

    def get_panel_by_code(self, panel_code: str) -> LabTestPanel | None:
        return (
            self.db.query(LabTestPanel)
            .filter(
                LabTestPanel.hospital_id == self.hospital_id,
                func.upper(LabTestPanel.panel_code) == panel_code.strip().upper(),
            )
            .first()
        )

    # ── Prescription Requests ──────────────────────────────────────────────────
    def list_prescription_requests(
        self,
        status: LabPrescriptionRequestStatus | None = None,
        patient_id: UUID | None = None,
        doctor_id: UUID | None = None,
    ) -> list[LabPrescriptionRequest]:
        q = (
            self.db.query(LabPrescriptionRequest)
            .options(
                joinedload(LabPrescriptionRequest.patient),
                joinedload(LabPrescriptionRequest.doctor),
                joinedload(LabPrescriptionRequest.items),
                joinedload(LabPrescriptionRequest.prescription),
            )
            .filter(LabPrescriptionRequest.hospital_id == self.hospital_id)
        )
        if status:
            q = q.filter(LabPrescriptionRequest.status == status)
        if patient_id:
            q = q.filter(LabPrescriptionRequest.patient_id == patient_id)
        if doctor_id:
            q = q.filter(LabPrescriptionRequest.doctor_id == doctor_id)
        return q.order_by(LabPrescriptionRequest.created_at.desc()).all()

    def get_prescription_request(self, request_id: UUID) -> LabPrescriptionRequest | None:
        return (
            self.db.query(LabPrescriptionRequest)
            .options(
                joinedload(LabPrescriptionRequest.patient),
                joinedload(LabPrescriptionRequest.doctor),
                joinedload(LabPrescriptionRequest.items),
                joinedload(LabPrescriptionRequest.prescription),
            )
            .filter(
                LabPrescriptionRequest.id == request_id,
                LabPrescriptionRequest.hospital_id == self.hospital_id,
            )
            .first()
        )

    # ── Orders ─────────────────────────────────────────────────────────────────
    def next_order_no(self) -> str:
        count = (
            self.db.query(func.count(LabOrder.id))
            .filter(LabOrder.hospital_id == self.hospital_id)
            .scalar()
            or 0
        )
        return f"LAB{int(count) + 1:04d}"

    def list_orders(
        self,
        status: LabOrderStatus | None = None,
        patient_id: UUID | None = None,
        doctor_id: UUID | None = None,
        order_date: date | None = None,
        order_source: LabOrderSource | None = None,
        search: str | None = None,
    ) -> list[LabOrder]:
        q = (
            self.db.query(LabOrder)
            .options(
                joinedload(LabOrder.patient),
                joinedload(LabOrder.doctor),
                joinedload(LabOrder.items),
                joinedload(LabOrder.results),
            )
            .filter(LabOrder.hospital_id == self.hospital_id)
        )
        if status:
            q = q.filter(LabOrder.status == status)
        if patient_id:
            q = q.filter(LabOrder.patient_id == patient_id)
        if doctor_id:
            q = q.filter(LabOrder.doctor_id == doctor_id)
        if order_source:
            q = q.filter(LabOrder.order_source == order_source)
        if order_date:
            start_dt = datetime.combine(order_date, time.min).replace(tzinfo=timezone.utc)
            end_dt = datetime.combine(order_date, time.max).replace(tzinfo=timezone.utc)
            q = q.filter(LabOrder.ordered_at >= start_dt, LabOrder.ordered_at <= end_dt)
        if search:
            term = f"%{search}%"
            q = q.filter(LabOrder.order_no.ilike(term))
        return q.order_by(LabOrder.ordered_at.desc()).all()

    def get_order(self, order_id: UUID) -> LabOrder | None:
        return (
            self.db.query(LabOrder)
            .options(
                joinedload(LabOrder.patient),
                joinedload(LabOrder.doctor),
                joinedload(LabOrder.items),
                joinedload(LabOrder.results),
            )
            .filter(LabOrder.id == order_id, LabOrder.hospital_id == self.hospital_id)
            .first()
        )

    # ── Dashboard metrics ──────────────────────────────────────────────────────
    def get_dashboard_metrics(self) -> dict[str, Any]:
        today_start = datetime.combine(date.today(), time.min).replace(tzinfo=timezone.utc)
        todays_orders = (
            self.db.query(func.count(LabOrder.id))
            .filter(LabOrder.hospital_id == self.hospital_id, LabOrder.ordered_at >= today_start)
            .scalar()
            or 0
        )
        status_counts = dict(
            self.db.query(LabOrder.status, func.count(LabOrder.id))
            .filter(LabOrder.hospital_id == self.hospital_id)
            .group_by(LabOrder.status)
            .all()
        )
        panels_count = (
            self.db.query(func.count(LabTestPanel.id))
            .filter(LabTestPanel.hospital_id == self.hospital_id, LabTestPanel.is_active.is_(True))
            .scalar()
            or 0
        )
        tests_count = (
            self.db.query(func.count(LabTestCatalog.id))
            .filter(LabTestCatalog.hospital_id == self.hospital_id, LabTestCatalog.is_active.is_(True))
            .scalar()
            or 0
        )
        pending_requests_count = (
            self.db.query(func.count(LabPrescriptionRequest.id))
            .filter(
                LabPrescriptionRequest.hospital_id == self.hospital_id,
                LabPrescriptionRequest.status.in_(
                    [LabPrescriptionRequestStatus.pending, LabPrescriptionRequestStatus.partially_processed]
                ),
            )
            .scalar()
            or 0
        )
        source_counts = dict(
            self.db.query(LabOrder.order_source, func.count(LabOrder.id))
            .filter(LabOrder.hospital_id == self.hospital_id)
            .group_by(LabOrder.order_source)
            .all()
        )
        return {
            "todays_orders": int(todays_orders),
            "pending": int(status_counts.get(LabOrderStatus.ordered, 0)),
            "completed": int(status_counts.get(LabOrderStatus.completed, 0)),
            "cancelled": int(status_counts.get(LabOrderStatus.cancelled, 0)),
            "sample_collected": int(status_counts.get(LabOrderStatus.sample_collected, 0)),
            "in_progress": int(status_counts.get(LabOrderStatus.in_progress, 0)),
            "panels_count": int(panels_count),
            "tests_count": int(tests_count),
            "pending_doctor_requests": int(pending_requests_count),
            "doctor_prescribed_orders": int(source_counts.get(LabOrderSource.doctor_prescribed, 0)),
            "self_requested_orders": int(source_counts.get(LabOrderSource.self_requested, 0)),
        }
