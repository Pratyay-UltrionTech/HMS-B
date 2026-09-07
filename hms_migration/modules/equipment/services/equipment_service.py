"""
Equipment domain business services.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from hms_migration.modules.equipment.contracts.equipment_contracts import EquipmentResponse
from hms_migration.modules.equipment.entities.equipment_entities import (
    EquipmentItem,
    EquipmentMaintenance,
    MaintenanceStatus,
)


def actor_name(user: dict[str, Any]) -> str:
    return str(user.get("name") or user.get("sub") or "Staff")


def equip_to_response(item: EquipmentItem) -> EquipmentResponse:
    active = next((a for a in (item.assignments or []) if a.is_active), None)
    return EquipmentResponse(
        id=item.id,
        hospital_id=item.hospital_id,
        asset_id=item.asset_id,
        name=item.name,
        category_id=item.category_id,
        category_name=item.category.name if item.category else None,
        manufacturer=item.manufacturer,
        model=item.model,
        serial_number=item.serial_number,
        purchase_date=item.purchase_date,
        purchase_cost=item.purchase_cost,
        department=item.department,
        current_location=item.current_location,
        status=item.status,
        vendor=item.vendor,
        warranty_start=item.warranty_start,
        warranty_end=item.warranty_end,
        amc_start=item.amc_start,
        amc_end=item.amc_end,
        vendor_contact=item.vendor_contact,
        notes=item.notes,
        created_at=item.created_at,
        active_assignment=f"{active.target_type.value}: {active.target_name}" if active else None,
    )


def refresh_maintenance_status(row: EquipmentMaintenance) -> None:
    if row.status == MaintenanceStatus.completed:
        return
    today = date.today()
    if row.next_service_date < today:
        row.status = MaintenanceStatus.overdue
    elif row.next_service_date <= today:
        row.status = MaintenanceStatus.due
    else:
        row.status = MaintenanceStatus.ok if row.last_service_date else MaintenanceStatus.scheduled
