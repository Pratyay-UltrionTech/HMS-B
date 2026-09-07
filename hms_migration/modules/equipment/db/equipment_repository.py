"""
Equipment repository handling database interactions for equipment domain.
"""

from __future__ import annotations

from typing import Sequence
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from hms_migration.modules.equipment.entities.equipment_entities import (
    EquipmentAssignment,
    EquipmentCategory,
    EquipmentItem,
    EquipmentMaintenance,
    EquipmentRequest,
    EquipmentRequestStatus,
    EquipmentServiceLog,
    EquipmentStatus,
)

DEFAULT_CATEGORIES = [
    "Diagnostic",
    "Surgical",
    "Monitoring",
    "Laboratory",
    "Radiology",
    "ICU",
    "General",
    "Furniture",
]


class EquipmentRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ── Categories ────────────────────────────────────────────────────────────

    def ensure_default_categories(self, hospital_id: UUID) -> None:
        existing = {
            r.name.lower()
            for r in self.db.query(EquipmentCategory.name).filter(EquipmentCategory.hospital_id == hospital_id).all()
        }
        added = False
        for name in DEFAULT_CATEGORIES:
            if name.lower() not in existing:
                self.db.add(EquipmentCategory(hospital_id=hospital_id, name=name, is_active=True))
                added = True
        if added:
            self.db.commit()

    def list_categories(self, hospital_id: UUID) -> Sequence[EquipmentCategory]:
        self.ensure_default_categories(hospital_id)
        return (
            self.db.query(EquipmentCategory)
            .filter(EquipmentCategory.hospital_id == hospital_id)
            .order_by(EquipmentCategory.name.asc())
            .all()
        )

    def get_category_by_id(self, category_id: UUID, hospital_id: UUID) -> EquipmentCategory | None:
        return (
            self.db.query(EquipmentCategory)
            .filter(EquipmentCategory.id == category_id, EquipmentCategory.hospital_id == hospital_id)
            .first()
        )

    def get_category_by_name(self, hospital_id: UUID, name: str) -> EquipmentCategory | None:
        return (
            self.db.query(EquipmentCategory)
            .filter(EquipmentCategory.hospital_id == hospital_id, EquipmentCategory.name.ilike(name))
            .first()
        )

    def get_equipment_counts_for_categories(self, hospital_id: UUID, cat_ids: list[UUID]) -> dict[UUID, int]:
        if not cat_ids:
            return {}
        rows = (
            self.db.query(EquipmentItem.category_id, func.count(EquipmentItem.id))
            .filter(EquipmentItem.hospital_id == hospital_id, EquipmentItem.category_id.in_(cat_ids))
            .group_by(EquipmentItem.category_id)
            .all()
        )
        return {cid: int(cnt) for cid, cnt in rows}

    def count_items_for_category(self, hospital_id: UUID, category_id: UUID) -> int:
        return (
            self.db.query(func.count(EquipmentItem.id))
            .filter(EquipmentItem.hospital_id == hospital_id, EquipmentItem.category_id == category_id)
            .scalar()
            or 0
        )

    # ── Items ─────────────────────────────────────────────────────────────────

    def next_asset_id(self, hospital_id: UUID) -> str:
        count = self.db.query(func.count(EquipmentItem.id)).filter(EquipmentItem.hospital_id == hospital_id).scalar() or 0
        return f"EQ{int(count) + 1:03d}"

    def get_item_by_asset_id(self, hospital_id: UUID, asset_id: str) -> EquipmentItem | None:
        return (
            self.db.query(EquipmentItem)
            .filter(EquipmentItem.hospital_id == hospital_id, EquipmentItem.asset_id == asset_id)
            .first()
        )

    def get_item_by_id(self, item_id: UUID, hospital_id: UUID) -> EquipmentItem | None:
        return (
            self.db.query(EquipmentItem)
            .options(
                joinedload(EquipmentItem.category),
                joinedload(EquipmentItem.assignments),
            )
            .filter(EquipmentItem.id == item_id, EquipmentItem.hospital_id == hospital_id)
            .first()
        )

    def list_items(
        self,
        hospital_id: UUID,
        search: str | None = None,
        status_filter: EquipmentStatus | None = None,
        category_id: UUID | None = None,
        amc_only: bool | None = None,
    ) -> Sequence[EquipmentItem]:
        q = (
            self.db.query(EquipmentItem)
            .options(joinedload(EquipmentItem.category), joinedload(EquipmentItem.assignments))
            .filter(EquipmentItem.hospital_id == hospital_id)
        )
        if search:
            like = f"%{search.strip()}%"
            q = q.filter(
                or_(
                    EquipmentItem.asset_id.ilike(like),
                    EquipmentItem.name.ilike(like),
                    EquipmentItem.department.ilike(like),
                    EquipmentItem.serial_number.ilike(like),
                    EquipmentItem.vendor.ilike(like),
                )
            )
        if status_filter:
            q = q.filter(EquipmentItem.status == status_filter)
        if category_id:
            q = q.filter(EquipmentItem.category_id == category_id)
        if amc_only:
            q = q.filter(
                or_(
                    EquipmentItem.vendor.isnot(None),
                    EquipmentItem.warranty_end.isnot(None),
                    EquipmentItem.amc_end.isnot(None),
                )
            )
        return q.order_by(EquipmentItem.asset_id.asc()).limit(500).all()

    def get_dashboard_counts(self, hospital_id: UUID) -> dict[str, int]:
        q = self.db.query(EquipmentItem).filter(EquipmentItem.hospital_id == hospital_id)
        total = q.count()
        available = q.filter(EquipmentItem.status == EquipmentStatus.available).count()
        in_use = q.filter(EquipmentItem.status == EquipmentStatus.in_use).count()
        under_maintenance = q.filter(EquipmentItem.status == EquipmentStatus.under_maintenance).count()
        out_of_service = q.filter(EquipmentItem.status == EquipmentStatus.out_of_service).count()
        return {
            "total": total,
            "available": available,
            "in_use": in_use,
            "under_maintenance": under_maintenance,
            "out_of_service": out_of_service,
        }

    # ── Assignments ───────────────────────────────────────────────────────────

    def list_assignments(self, hospital_id: UUID, active_only: bool | None = True) -> Sequence[EquipmentAssignment]:
        q = (
            self.db.query(EquipmentAssignment)
            .options(joinedload(EquipmentAssignment.equipment))
            .filter(EquipmentAssignment.hospital_id == hospital_id)
        )
        if active_only:
            q = q.filter(EquipmentAssignment.is_active.is_(True))
        return q.order_by(EquipmentAssignment.assigned_at.desc()).limit(300).all()

    def get_assignment_by_id(self, assignment_id: UUID, hospital_id: UUID) -> EquipmentAssignment | None:
        return (
            self.db.query(EquipmentAssignment)
            .options(joinedload(EquipmentAssignment.equipment))
            .filter(EquipmentAssignment.id == assignment_id, EquipmentAssignment.hospital_id == hospital_id)
            .first()
        )

    # ── Maintenance ───────────────────────────────────────────────────────────

    def list_maintenances(self, hospital_id: UUID) -> Sequence[EquipmentMaintenance]:
        return (
            self.db.query(EquipmentMaintenance)
            .options(joinedload(EquipmentMaintenance.equipment))
            .filter(EquipmentMaintenance.hospital_id == hospital_id)
            .order_by(EquipmentMaintenance.next_service_date.asc())
            .limit(300)
            .all()
        )

    def get_maintenance_by_id(self, maintenance_id: UUID, hospital_id: UUID) -> EquipmentMaintenance | None:
        return (
            self.db.query(EquipmentMaintenance)
            .options(joinedload(EquipmentMaintenance.equipment))
            .filter(EquipmentMaintenance.id == maintenance_id, EquipmentMaintenance.hospital_id == hospital_id)
            .first()
        )

    # ── Service Logs ──────────────────────────────────────────────────────────

    def list_service_logs(self, hospital_id: UUID, equipment_id: UUID | None = None) -> Sequence[EquipmentServiceLog]:
        q = (
            self.db.query(EquipmentServiceLog)
            .options(joinedload(EquipmentServiceLog.equipment))
            .filter(EquipmentServiceLog.hospital_id == hospital_id)
        )
        if equipment_id:
            q = q.filter(EquipmentServiceLog.equipment_id == equipment_id)
        return q.order_by(EquipmentServiceLog.service_date.desc()).limit(300).all()

    # ── Requests ──────────────────────────────────────────────────────────────

    def next_request_no(self, hospital_id: UUID) -> str:
        count = (
            self.db.query(func.count(EquipmentRequest.id)).filter(EquipmentRequest.hospital_id == hospital_id).scalar()
            or 0
        )
        return f"ER{int(count) + 1:04d}"

    def list_requests(
        self, hospital_id: UUID, status_filter: EquipmentRequestStatus | None = None
    ) -> Sequence[EquipmentRequest]:
        q = self.db.query(EquipmentRequest).filter(EquipmentRequest.hospital_id == hospital_id)
        if status_filter:
            q = q.filter(EquipmentRequest.status == status_filter)
        return q.order_by(EquipmentRequest.created_at.desc()).limit(300).all()

    def get_request_by_id(self, request_id: UUID, hospital_id: UUID) -> EquipmentRequest | None:
        return (
            self.db.query(EquipmentRequest)
            .filter(EquipmentRequest.id == request_id, EquipmentRequest.hospital_id == hospital_id)
            .first()
        )
