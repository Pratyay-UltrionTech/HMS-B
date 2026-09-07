"""
Equipment domain business action handlers.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from hms_migration.modules.equipment.contracts.equipment_contracts import (
    AmcUpdate,
    AssignmentCreate,
    AssignmentResponse,
    EquipCategoryCreate,
    EquipCategoryResponse,
    EquipCategoryUpdate,
    EquipDashboardResponse,
    EquipmentCreate,
    EquipmentResponse,
    EquipmentUpdate,
    MaintenanceComplete,
    MaintenanceCreate,
    MaintenanceResponse,
    RequestAction,
    RequestCreate,
    RequestResponse,
    ServiceLogCreate,
    ServiceLogResponse,
)
from hms_migration.modules.equipment.db.equipment_repository import EquipmentRepository
from hms_migration.modules.equipment.entities.equipment_entities import (
    EquipmentAssignTarget,
    EquipmentAssignment,
    EquipmentCategory,
    EquipmentItem,
    EquipmentMaintenance,
    EquipmentRequest,
    EquipmentRequestStatus,
    EquipmentServiceLog,
    EquipmentStatus,
    MaintenanceStatus,
)
from hms_migration.modules.equipment.services.equipment_service import (
    actor_name,
    equip_to_response,
    refresh_maintenance_status,
)
from hms_migration.shared.audit import write_audit_log


class EquipmentActions:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = EquipmentRepository(db)

    # ── Dashboard ─────────────────────────────────────────────────────────────

    def get_dashboard(self) -> EquipDashboardResponse:
        counts = self.repo.get_dashboard_counts(self.hospital_id)
        return EquipDashboardResponse(**counts)

    # ── Categories ────────────────────────────────────────────────────────────

    def list_categories(self) -> list[EquipCategoryResponse]:
        rows = self.repo.list_categories(self.hospital_id)
        cat_ids = [r.id for r in rows]
        counts = self.repo.get_equipment_counts_for_categories(self.hospital_id, cat_ids)
        return [
            EquipCategoryResponse(
                id=r.id,
                hospital_id=r.hospital_id,
                name=r.name,
                description=r.description,
                is_active=r.is_active,
                created_at=r.created_at,
                equipment_count=counts.get(r.id, 0),
            )
            for r in rows
        ]

    def create_category(self, payload: EquipCategoryCreate) -> EquipCategoryResponse:
        name = payload.name.strip()
        exists = self.repo.get_category_by_name(self.hospital_id, name)
        if exists:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Category already exists")

        row = EquipmentCategory(
            hospital_id=self.hospital_id,
            name=name,
            description=payload.description,
            is_active=payload.is_active,
        )
        self.db.add(row)
        self.db.flush()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="equipment_category",
            entity_id=row.id,
            summary=f"Added equipment category {name}",
        )
        self.db.commit()
        self.db.refresh(row)
        return EquipCategoryResponse(
            id=row.id,
            hospital_id=row.hospital_id,
            name=row.name,
            description=row.description,
            is_active=row.is_active,
            created_at=row.created_at,
            equipment_count=0,
        )

    def update_category(self, category_id: UUID, payload: EquipCategoryUpdate) -> EquipCategoryResponse:
        row = self.repo.get_category_by_id(category_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")

        data = payload.model_dump(exclude_unset=True)
        for k, v in data.items():
            if isinstance(v, str):
                v = v.strip()
            setattr(row, k, v)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="equipment_category",
            entity_id=row.id,
            summary=f"Updated equipment category {row.name}",
        )
        self.db.commit()
        cnt = self.repo.count_items_for_category(self.hospital_id, row.id)
        return EquipCategoryResponse(
            id=row.id,
            hospital_id=row.hospital_id,
            name=row.name,
            description=row.description,
            is_active=row.is_active,
            created_at=row.created_at,
            equipment_count=int(cnt),
        )

    def delete_category(self, category_id: UUID) -> None:
        row = self.repo.get_category_by_id(category_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")
        name = row.name
        self.db.delete(row)
        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="delete",
            entity_type="equipment_category",
            entity_id=category_id,
            summary=f"Deleted equipment category {name}",
        )
        self.db.commit()

    # ── Items ─────────────────────────────────────────────────────────────────

    def list_items(
        self,
        search: str | None = None,
        status_filter: str | None = None,
        category_id: UUID | None = None,
        amc_only: bool | None = None,
    ) -> list[EquipmentResponse]:
        status_enum = None
        if status_filter:
            try:
                status_enum = EquipmentStatus(status_filter)
            except ValueError:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid status")
        rows = self.repo.list_items(
            self.hospital_id,
            search=search,
            status_filter=status_enum,
            category_id=category_id,
            amc_only=amc_only,
        )
        return [equip_to_response(r) for r in rows]

    def create_item(self, payload: EquipmentCreate) -> EquipmentResponse:
        asset_id = (payload.asset_id or "").strip().upper() or self.repo.next_asset_id(self.hospital_id)
        exists = self.repo.get_item_by_asset_id(self.hospital_id, asset_id)
        if exists:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Asset ID already exists")
        if payload.category_id:
            cat = self.repo.get_category_by_id(payload.category_id, self.hospital_id)
            if not cat:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")

        item = EquipmentItem(
            hospital_id=self.hospital_id,
            asset_id=asset_id,
            name=payload.name.strip(),
            category_id=payload.category_id,
            manufacturer=payload.manufacturer.strip() if payload.manufacturer else None,
            model=payload.model.strip() if payload.model else None,
            serial_number=payload.serial_number.strip() if payload.serial_number else None,
            purchase_date=payload.purchase_date,
            purchase_cost=payload.purchase_cost,
            department=payload.department.strip() if payload.department else None,
            current_location=payload.current_location.strip() if payload.current_location else None,
            status=payload.status,
            vendor=payload.vendor.strip() if payload.vendor else None,
            warranty_start=payload.warranty_start,
            warranty_end=payload.warranty_end,
            amc_start=payload.amc_start,
            amc_end=payload.amc_end,
            vendor_contact=payload.vendor_contact.strip() if payload.vendor_contact else None,
            notes=payload.notes.strip() if payload.notes else None,
        )
        self.db.add(item)
        self.db.flush()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="equipment",
            entity_id=item.id,
            summary=f"Added equipment {item.asset_id} — {item.name}",
        )
        self.db.commit()
        created = self.repo.get_item_by_id(item.id, self.hospital_id)
        assert created is not None
        return equip_to_response(created)

    def update_item(self, item_id: UUID, payload: EquipmentUpdate) -> EquipmentResponse:
        item = self.repo.get_item_by_id(item_id, self.hospital_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Equipment not found")

        data = payload.model_dump(exclude_unset=True)
        for k, v in data.items():
            if isinstance(v, str):
                v = v.strip() or None
            setattr(item, k, v)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="equipment",
            entity_id=item.id,
            summary=f"Updated equipment {item.asset_id}",
        )
        self.db.commit()
        refreshed = self.repo.get_item_by_id(item_id, self.hospital_id)
        assert refreshed is not None
        return equip_to_response(refreshed)

    def delete_item(self, item_id: UUID) -> None:
        item = self.repo.get_item_by_id(item_id, self.hospital_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Equipment not found")
        code = item.asset_id
        self.db.delete(item)
        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="delete",
            entity_type="equipment",
            entity_id=item_id,
            summary=f"Deleted equipment {code}",
        )
        self.db.commit()

    def update_amc(self, item_id: UUID, payload: AmcUpdate) -> EquipmentResponse:
        item = self.repo.get_item_by_id(item_id, self.hospital_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Equipment not found")

        data = payload.model_dump(exclude_unset=True)
        for k, v in data.items():
            if isinstance(v, str):
                v = v.strip() or None
            setattr(item, k, v)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="equipment_amc",
            entity_id=item.id,
            summary=f"Updated AMC/warranty for {item.asset_id}",
        )
        self.db.commit()
        refreshed = self.repo.get_item_by_id(item_id, self.hospital_id)
        assert refreshed is not None
        return equip_to_response(refreshed)

    # ── Assignments ───────────────────────────────────────────────────────────

    def list_assignments(self, active_only: bool | None = True) -> list[AssignmentResponse]:
        rows = self.repo.list_assignments(self.hospital_id, active_only=active_only)
        return [
            AssignmentResponse(
                id=r.id,
                equipment_id=r.equipment_id,
                equipment_name=r.equipment.name if r.equipment else None,
                asset_id=r.equipment.asset_id if r.equipment else None,
                target_type=r.target_type,
                target_name=r.target_name,
                assigned_by_name=r.assigned_by_name,
                assigned_at=r.assigned_at,
                returned_at=r.returned_at,
                is_active=r.is_active,
                remarks=r.remarks,
            )
            for r in rows
        ]

    def create_assignment(self, payload: AssignmentCreate) -> AssignmentResponse:
        item = self.repo.get_item_by_id(payload.equipment_id, self.hospital_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Equipment not found")

        for prev in item.assignments:
            if prev.is_active:
                prev.is_active = False
                prev.returned_at = datetime.now(timezone.utc)

        row = EquipmentAssignment(
            hospital_id=self.hospital_id,
            equipment_id=item.id,
            target_type=payload.target_type,
            target_name=payload.target_name.strip(),
            assigned_by_name=actor_name(self.user),
            remarks=payload.remarks.strip() if payload.remarks else None,
            is_active=True,
        )
        item.status = EquipmentStatus.in_use
        item.current_location = payload.target_name.strip()
        if payload.target_type == EquipmentAssignTarget.department:
            item.department = payload.target_name.strip()

        self.db.add(row)
        self.db.flush()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="equipment_assignment",
            entity_id=row.id,
            summary=f"Assigned {item.asset_id} to {payload.target_type.value}: {payload.target_name}",
        )
        self.db.commit()
        self.db.refresh(row)
        return AssignmentResponse(
            id=row.id,
            equipment_id=row.equipment_id,
            equipment_name=item.name,
            asset_id=item.asset_id,
            target_type=row.target_type,
            target_name=row.target_name,
            assigned_by_name=row.assigned_by_name,
            assigned_at=row.assigned_at,
            returned_at=row.returned_at,
            is_active=row.is_active,
            remarks=row.remarks,
        )

    def return_assignment(self, assignment_id: UUID) -> AssignmentResponse:
        row = self.repo.get_assignment_by_id(assignment_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found")

        row.is_active = False
        row.returned_at = datetime.now(timezone.utc)
        if row.equipment and row.equipment.status == EquipmentStatus.in_use:
            row.equipment.status = EquipmentStatus.available

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="equipment_assignment",
            entity_id=row.id,
            summary=f"Returned equipment assignment {assignment_id}",
        )
        self.db.commit()
        return AssignmentResponse(
            id=row.id,
            equipment_id=row.equipment_id,
            equipment_name=row.equipment.name if row.equipment else None,
            asset_id=row.equipment.asset_id if row.equipment else None,
            target_type=row.target_type,
            target_name=row.target_name,
            assigned_by_name=row.assigned_by_name,
            assigned_at=row.assigned_at,
            returned_at=row.returned_at,
            is_active=row.is_active,
            remarks=row.remarks,
        )

    # ── Maintenance ───────────────────────────────────────────────────────────

    def list_maintenance(self) -> list[MaintenanceResponse]:
        rows = self.repo.list_maintenances(self.hospital_id)
        out: list[MaintenanceResponse] = []
        for r in rows:
            if r.status != MaintenanceStatus.completed:
                refresh_maintenance_status(r)
            out.append(
                MaintenanceResponse(
                    id=r.id,
                    equipment_id=r.equipment_id,
                    equipment_name=r.equipment.name if r.equipment else None,
                    asset_id=r.equipment.asset_id if r.equipment else None,
                    last_service_date=r.last_service_date,
                    next_service_date=r.next_service_date,
                    status=r.status,
                    remarks=r.remarks,
                    created_at=r.created_at,
                    completed_at=r.completed_at,
                )
            )
        self.db.commit()
        return out

    def schedule_maintenance(self, payload: MaintenanceCreate) -> MaintenanceResponse:
        item = self.repo.get_item_by_id(payload.equipment_id, self.hospital_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Equipment not found")

        row = EquipmentMaintenance(
            hospital_id=self.hospital_id,
            equipment_id=item.id,
            last_service_date=payload.last_service_date,
            next_service_date=payload.next_service_date,
            remarks=payload.remarks.strip() if payload.remarks else None,
            status=MaintenanceStatus.scheduled,
        )
        refresh_maintenance_status(row)
        if row.status in (MaintenanceStatus.due, MaintenanceStatus.overdue):
            item.status = EquipmentStatus.under_maintenance

        self.db.add(row)
        self.db.flush()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="equipment_maintenance",
            entity_id=row.id,
            summary=f"Scheduled maintenance for {item.asset_id}",
        )
        self.db.commit()
        return MaintenanceResponse(
            id=row.id,
            equipment_id=row.equipment_id,
            equipment_name=item.name,
            asset_id=item.asset_id,
            last_service_date=row.last_service_date,
            next_service_date=row.next_service_date,
            status=row.status,
            remarks=row.remarks,
            created_at=row.created_at,
            completed_at=row.completed_at,
        )

    def complete_maintenance(self, maintenance_id: UUID, payload: MaintenanceComplete) -> MaintenanceResponse:
        row = self.repo.get_maintenance_by_id(maintenance_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Maintenance record not found")

        today = date.today()
        row.status = MaintenanceStatus.completed
        row.completed_at = datetime.now(timezone.utc)
        row.last_service_date = today
        if payload.remarks:
            row.remarks = ((row.remarks or "") + "\n" + payload.remarks.strip()).strip()
        if payload.next_service_date:
            row.next_service_date = payload.next_service_date

        log = EquipmentServiceLog(
            hospital_id=self.hospital_id,
            equipment_id=row.equipment_id,
            service_date=today,
            work_done=payload.work_done.strip(),
            engineer=payload.engineer.strip() if payload.engineer else None,
            cost=payload.cost,
            remarks=payload.remarks.strip() if payload.remarks else None,
        )
        self.db.add(log)
        if row.equipment and row.equipment.status == EquipmentStatus.under_maintenance:
            row.equipment.status = EquipmentStatus.available

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="equipment_maintenance",
            entity_id=row.id,
            summary=f"Completed maintenance for {row.equipment.asset_id if row.equipment else maintenance_id}",
        )
        self.db.commit()
        return MaintenanceResponse(
            id=row.id,
            equipment_id=row.equipment_id,
            equipment_name=row.equipment.name if row.equipment else None,
            asset_id=row.equipment.asset_id if row.equipment else None,
            last_service_date=row.last_service_date,
            next_service_date=row.next_service_date,
            status=row.status,
            remarks=row.remarks,
            created_at=row.created_at,
            completed_at=row.completed_at,
        )

    # ── Service Logs ──────────────────────────────────────────────────────────

    def list_service_logs(self, equipment_id: UUID | None = None) -> list[ServiceLogResponse]:
        rows = self.repo.list_service_logs(self.hospital_id, equipment_id=equipment_id)
        return [
            ServiceLogResponse(
                id=r.id,
                equipment_id=r.equipment_id,
                equipment_name=r.equipment.name if r.equipment else None,
                asset_id=r.equipment.asset_id if r.equipment else None,
                service_date=r.service_date,
                work_done=r.work_done,
                engineer=r.engineer,
                cost=r.cost,
                remarks=r.remarks,
                created_at=r.created_at,
            )
            for r in rows
        ]

    def create_service_log(self, payload: ServiceLogCreate) -> ServiceLogResponse:
        item = self.repo.get_item_by_id(payload.equipment_id, self.hospital_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Equipment not found")

        row = EquipmentServiceLog(
            hospital_id=self.hospital_id,
            equipment_id=item.id,
            service_date=payload.service_date,
            work_done=payload.work_done.strip(),
            engineer=payload.engineer.strip() if payload.engineer else None,
            cost=payload.cost,
            remarks=payload.remarks.strip() if payload.remarks else None,
        )
        self.db.add(row)
        self.db.flush()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="equipment_service",
            entity_id=row.id,
            summary=f"Service log for {item.asset_id}: {payload.work_done[:80]}",
        )
        self.db.commit()
        self.db.refresh(row)
        return ServiceLogResponse(
            id=row.id,
            equipment_id=row.equipment_id,
            equipment_name=item.name,
            asset_id=item.asset_id,
            service_date=row.service_date,
            work_done=row.work_done,
            engineer=row.engineer,
            cost=row.cost,
            remarks=row.remarks,
            created_at=row.created_at,
        )

    # ── Requests ──────────────────────────────────────────────────────────────

    def list_requests(self, status_filter: str | None = None) -> list[EquipmentRequest]:
        status_enum = None
        if status_filter:
            try:
                status_enum = EquipmentRequestStatus(status_filter)
            except ValueError:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid status")
        return list(self.repo.list_requests(self.hospital_id, status_filter=status_enum))

    def create_request(self, payload: RequestCreate) -> EquipmentRequest:
        row = EquipmentRequest(
            hospital_id=self.hospital_id,
            request_no=self.repo.next_request_no(self.hospital_id),
            department=payload.department.strip(),
            equipment_name=payload.equipment_name.strip(),
            quantity=payload.quantity,
            remarks=payload.remarks.strip() if payload.remarks else None,
            requested_by_name=actor_name(self.user),
            status=EquipmentRequestStatus.pending,
        )
        self.db.add(row)
        self.db.flush()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="equipment_request",
            entity_id=row.id,
            summary=f"Equipment request {row.request_no}: {row.equipment_name} x{row.quantity}",
        )
        self.db.commit()
        self.db.refresh(row)
        return row

    def approve_request(self, request_id: UUID, payload: RequestAction | None = None) -> EquipmentRequest:
        row = self.repo.get_request_by_id(request_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
        if row.status != EquipmentRequestStatus.pending:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only pending requests can be approved")

        row.status = EquipmentRequestStatus.approved
        row.resolved_at = datetime.now(timezone.utc)
        if payload and payload.admin_remarks:
            row.admin_remarks = payload.admin_remarks.strip()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="equipment_request",
            entity_id=row.id,
            summary=f"Approved request {row.request_no}",
        )
        self.db.commit()
        self.db.refresh(row)
        return row

    def reject_request(self, request_id: UUID, payload: RequestAction | None = None) -> EquipmentRequest:
        row = self.repo.get_request_by_id(request_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
        if row.status not in (EquipmentRequestStatus.pending, EquipmentRequestStatus.approved):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Request cannot be rejected")

        row.status = EquipmentRequestStatus.rejected
        row.resolved_at = datetime.now(timezone.utc)
        if payload and payload.admin_remarks:
            row.admin_remarks = payload.admin_remarks.strip()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="equipment_request",
            entity_id=row.id,
            summary=f"Rejected request {row.request_no}",
        )
        self.db.commit()
        self.db.refresh(row)
        return row

    def assign_request(self, request_id: UUID, payload: RequestAction) -> EquipmentRequest:
        row = self.repo.get_request_by_id(request_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
        if row.status not in (EquipmentRequestStatus.pending, EquipmentRequestStatus.approved):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Request cannot be assigned")
        if not payload.equipment_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="equipment_id is required")

        item = self.repo.get_item_by_id(payload.equipment_id, self.hospital_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Equipment not found")
        if item.status not in (EquipmentStatus.available, EquipmentStatus.in_use):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Equipment not available to assign")

        for prev in item.assignments:
            if prev.is_active:
                prev.is_active = False
                prev.returned_at = datetime.now(timezone.utc)

        assignment = EquipmentAssignment(
            hospital_id=self.hospital_id,
            equipment_id=item.id,
            target_type=EquipmentAssignTarget.department,
            target_name=row.department,
            assigned_by_name=actor_name(self.user),
            remarks=f"Via request {row.request_no}",
            is_active=True,
        )
        self.db.add(assignment)
        item.status = EquipmentStatus.in_use
        item.department = row.department
        item.current_location = row.department
        row.status = EquipmentRequestStatus.assigned
        row.assigned_equipment_id = item.id
        row.resolved_at = datetime.now(timezone.utc)
        if payload.admin_remarks:
            row.admin_remarks = payload.admin_remarks.strip()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="equipment_request",
            entity_id=row.id,
            summary=f"Assigned {item.asset_id} for request {row.request_no}",
        )
        self.db.commit()
        self.db.refresh(row)
        return row
