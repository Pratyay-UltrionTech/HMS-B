"""
Database repository for Radiology domain.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session, defer, joinedload, selectinload

from modules.doctors.entities.doctor import HospitalUser
from modules.patients.entities.patient import Patient
from modules.radiology.entities.radiology_entities import (
    RadiologyAttachment,
    RadiologyOrder,
    RadiologyOrderStatus,
    RadiologyScanCatalog,
    RadPrescriptionRequest,
    RadPrescriptionRequestStatus,
)


class RadiologyRepository:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    # ── Catalogue ─────────────────────────────────────────────────────────────

    def list_scans(
        self, active_only: bool = False, search: str | None = None
    ) -> list[RadiologyScanCatalog]:
        q = self.db.query(RadiologyScanCatalog).filter(
            RadiologyScanCatalog.hospital_id == self.hospital_id
        )
        if active_only:
            q = q.filter(RadiologyScanCatalog.is_active.is_(True))
        if search and search.strip():
            term = f"%{search.strip()}%"
            q = q.filter(
                or_(
                    RadiologyScanCatalog.scan_name.ilike(term),
                    RadiologyScanCatalog.scan_code.ilike(term),
                    RadiologyScanCatalog.category.ilike(term),
                )
            )
        return q.order_by(RadiologyScanCatalog.scan_code.asc()).all()

    def get_scan(self, scan_id: UUID) -> RadiologyScanCatalog | None:
        return (
            self.db.query(RadiologyScanCatalog)
            .filter(
                RadiologyScanCatalog.id == scan_id,
                RadiologyScanCatalog.hospital_id == self.hospital_id,
            )
            .first()
        )

    def get_scan_by_code(self, code: str) -> RadiologyScanCatalog | None:
        return (
            self.db.query(RadiologyScanCatalog)
            .filter(
                RadiologyScanCatalog.hospital_id == self.hospital_id,
                RadiologyScanCatalog.scan_code == code.strip().upper(),
            )
            .first()
        )

    def check_scan_code_conflict(self, code: str, exclude_id: UUID | None = None) -> bool:
        q = self.db.query(RadiologyScanCatalog.id).filter(
            RadiologyScanCatalog.hospital_id == self.hospital_id,
            RadiologyScanCatalog.scan_code == code.strip().upper(),
        )
        if exclude_id:
            q = q.filter(RadiologyScanCatalog.id != exclude_id)
        return q.first() is not None

    def create_scan(self, scan: RadiologyScanCatalog) -> RadiologyScanCatalog:
        self.db.add(scan)
        self.db.flush()
        return scan

    def delete_scan(self, scan: RadiologyScanCatalog) -> None:
        self.db.delete(scan)

    # ── Orders ────────────────────────────────────────────────────────────────

    def next_order_no(self) -> str:
        count = (
            self.db.query(func.count(RadiologyOrder.id))
            .filter(RadiologyOrder.hospital_id == self.hospital_id)
            .scalar()
            or 0
        )
        return f"RAD{int(count) + 1:04d}"

    def next_order_no_batch(self, n: int) -> list[str]:
        """Reserve n sequential order numbers with a single count query."""
        count = (
            self.db.query(func.count(RadiologyOrder.id))
            .filter(RadiologyOrder.hospital_id == self.hospital_id)
            .scalar()
            or 0
        )
        start = int(count) + 1
        return [f"RAD{i:04d}" for i in range(start, start + n)]

    def get_order(self, order_id: UUID) -> RadiologyOrder | None:
        return (
            self.db.query(RadiologyOrder)
            .options(
                joinedload(RadiologyOrder.patient),
                joinedload(RadiologyOrder.doctor),
                joinedload(RadiologyOrder.scan),
                selectinload(RadiologyOrder.attachments),
                defer(RadiologyOrder.report_file_data),
                defer(RadiologyOrder.image_file_data),
            )
            .filter(
                RadiologyOrder.id == order_id,
                RadiologyOrder.hospital_id == self.hospital_id,
            )
            .first()
        )

    def get_attachment(self, attachment_id: UUID) -> RadiologyAttachment | None:
        return (
            self.db.query(RadiologyAttachment)
            .filter(
                RadiologyAttachment.id == attachment_id,
                RadiologyAttachment.hospital_id == self.hospital_id,
            )
            .first()
        )

    def list_attachments_for_order(self, order_id: UUID) -> list[RadiologyAttachment]:
        return (
            self.db.query(RadiologyAttachment)
            .filter(
                RadiologyAttachment.order_id == order_id,
                RadiologyAttachment.hospital_id == self.hospital_id,
            )
            .order_by(RadiologyAttachment.uploaded_at.asc())
            .all()
        )

    def list_orders(
        self,
        status_filter: RadiologyOrderStatus | None = None,
        patient_id: UUID | None = None,
        search: str | None = None,
        scheduled_only: bool = False,
        limit: int = 300,
    ) -> list[RadiologyOrder]:
        q = (
            self.db.query(RadiologyOrder)
            .options(
                joinedload(RadiologyOrder.patient),
                joinedload(RadiologyOrder.doctor),
                selectinload(RadiologyOrder.attachments),
                defer(RadiologyOrder.report_file_data),
                defer(RadiologyOrder.image_file_data),
            )
            .filter(RadiologyOrder.hospital_id == self.hospital_id)
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
                or_(
                    RadiologyOrder.order_no.ilike(term),
                    Patient.name.ilike(term),
                    Patient.uhid.ilike(term),
                    RadiologyOrder.scan_name.ilike(term),
                )
            )
        return q.order_by(RadiologyOrder.ordered_at.desc()).limit(limit).all()

    def get_patient(self, patient_id: UUID) -> Patient | None:
        return (
            self.db.query(Patient)
            .filter(
                Patient.id == patient_id,
                Patient.hospital_id == self.hospital_id,
            )
            .first()
        )

    def get_doctor(self, doctor_id: UUID) -> HospitalUser | None:
        return (
            self.db.query(HospitalUser)
            .filter(
                HospitalUser.id == doctor_id,
                HospitalUser.hospital_id == self.hospital_id,
            )
            .first()
        )

    def get_active_scans_by_ids(self, scan_ids: list[UUID]) -> list[RadiologyScanCatalog]:
        return (
            self.db.query(RadiologyScanCatalog)
            .filter(
                RadiologyScanCatalog.hospital_id == self.hospital_id,
                RadiologyScanCatalog.id.in_(scan_ids),
                RadiologyScanCatalog.is_active.is_(True),
            )
            .all()
        )

    # ── Prescription requests ─────────────────────────────────────────────

    def list_prescription_requests(
        self,
        status: RadPrescriptionRequestStatus | None = None,
        patient_id: UUID | None = None,
        doctor_id: UUID | None = None,
    ) -> list[RadPrescriptionRequest]:
        q = (
            self.db.query(RadPrescriptionRequest)
            .options(
                joinedload(RadPrescriptionRequest.patient),
                joinedload(RadPrescriptionRequest.doctor),
                joinedload(RadPrescriptionRequest.items),
                joinedload(RadPrescriptionRequest.prescription),
            )
            .filter(RadPrescriptionRequest.hospital_id == self.hospital_id)
        )
        if status:
            q = q.filter(RadPrescriptionRequest.status == status)
        if patient_id:
            q = q.filter(RadPrescriptionRequest.patient_id == patient_id)
        if doctor_id:
            q = q.filter(RadPrescriptionRequest.doctor_id == doctor_id)
        return q.order_by(RadPrescriptionRequest.created_at.desc()).all()

    def get_prescription_request(self, request_id: UUID) -> RadPrescriptionRequest | None:
        return (
            self.db.query(RadPrescriptionRequest)
            .options(
                joinedload(RadPrescriptionRequest.patient),
                joinedload(RadPrescriptionRequest.doctor),
                joinedload(RadPrescriptionRequest.items),
                joinedload(RadPrescriptionRequest.prescription),
            )
            .filter(
                RadPrescriptionRequest.id == request_id,
                RadPrescriptionRequest.hospital_id == self.hospital_id,
            )
            .first()
        )

    def get_dashboard_metrics(self) -> dict[str, int]:
        today = datetime.now(timezone.utc).date()
        day_start = datetime.combine(today, time.min, tzinfo=timezone.utc)
        day_end = datetime.combine(today, time.max, tzinfo=timezone.utc)

        row = self.db.query(
            func.count(case((RadiologyOrder.ordered_at >= day_start, 1))),
            func.count(case((RadiologyOrder.status.in_([RadiologyOrderStatus.ordered, RadiologyOrderStatus.scheduled]), 1))),
            func.count(case((RadiologyOrder.status == RadiologyOrderStatus.completed, 1))),
            func.count(case((RadiologyOrder.status == RadiologyOrderStatus.cancelled, 1))),
            func.count(
                case(
                    (
                        RadiologyOrder.status.in_([RadiologyOrderStatus.in_progress, RadiologyOrderStatus.completed])
                        & or_(RadiologyOrder.findings.is_(None), RadiologyOrder.findings == ""),
                        1,
                    )
                )
            ),
        ).filter(RadiologyOrder.hospital_id == self.hospital_id).one()
        todays, pending, completed, cancelled, reports_pending = row

        scans_count = (
            self.db.query(func.count(RadiologyScanCatalog.id))
            .filter(
                RadiologyScanCatalog.hospital_id == self.hospital_id,
                RadiologyScanCatalog.is_active.is_(True),
            )
            .scalar()
            or 0
        )

        return {
            "todays_orders": int(todays or 0),
            "pending_scans": int(pending or 0),
            "completed_scans": int(completed or 0),
            "reports_pending": int(reports_pending or 0),
            "cancelled_orders": int(cancelled or 0),
            "scans_count": int(scans_count or 0),
        }
