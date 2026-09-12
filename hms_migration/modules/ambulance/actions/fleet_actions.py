"""
Actions for Feature 10: Ambulance Fleet Management.

Conforms to UltrionTech-Backend-Template modules/ambulance/actions/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from hms_migration.modules.ambulance.contracts.ambulance_contracts import (
    AmbulanceVehicleCreate,
    AmbulanceVehicleResponse,
    AmbulanceVehicleUpdate,
)
from hms_migration.modules.ambulance.db.ambulance_repository import AmbulanceRepository
from hms_migration.modules.ambulance.entities.ambulance_entities import AmbulanceVehicle
from hms_migration.shared.audit.service import write_audit_log


class CreateAmbulanceVehicleAction:
    """Register a new ambulance vehicle in hospital fleet."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = AmbulanceRepository(db, hospital_id)

    def execute(self, payload: AmbulanceVehicleCreate, actor: dict[str, Any]) -> AmbulanceVehicleResponse:
        reg = payload.registration_number.strip().upper()
        existing = self.repo.get_vehicle_by_reg(reg)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Ambulance with registration number {reg} already exists",
            )

        vehicle = AmbulanceVehicle(
            hospital_id=self.hospital_id,
            registration_number=reg,
            vehicle_type=payload.vehicle_type,
            model=payload.model.strip(),
            operational_status=payload.operational_status,
            onboard_equipment=payload.onboard_equipment or {},
            current_driver_name=payload.current_driver_name.strip() if payload.current_driver_name else None,
            current_paramedic_name=payload.current_paramedic_name.strip() if payload.current_paramedic_name else None,
        )
        self.repo.add(vehicle)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="create_ambulance",
            entity_type="ambulance_vehicle",
            entity_id=vehicle.id,
            summary=f"Registered ambulance {vehicle.registration_number} ({vehicle.vehicle_type.value})",
        )
        self.repo.commit()
        self.repo.refresh(vehicle)
        return AmbulanceVehicleResponse.model_validate(vehicle)


class UpdateAmbulanceVehicleAction:
    """Update operational status, onboard equipment, or assigned crew for an ambulance."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = AmbulanceRepository(db, hospital_id)

    def execute(self, vehicle_id: UUID, payload: AmbulanceVehicleUpdate, actor: dict[str, Any]) -> AmbulanceVehicleResponse:
        vehicle = self.repo.get_vehicle_by_id(vehicle_id)
        if not vehicle:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ambulance not found")

        if payload.operational_status is not None:
            vehicle.operational_status = payload.operational_status
        if payload.onboard_equipment is not None:
            vehicle.onboard_equipment = payload.onboard_equipment
        if payload.current_driver_name is not None:
            vehicle.current_driver_name = payload.current_driver_name.strip() if payload.current_driver_name else None
        if payload.current_paramedic_name is not None:
            vehicle.current_paramedic_name = payload.current_paramedic_name.strip() if payload.current_paramedic_name else None
        if payload.is_active is not None:
            vehicle.is_active = payload.is_active

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="update_ambulance",
            entity_type="ambulance_vehicle",
            entity_id=vehicle.id,
            summary=f"Updated ambulance {vehicle.registration_number} status to {vehicle.operational_status.value}",
        )
        self.repo.commit()
        self.repo.refresh(vehicle)
        return AmbulanceVehicleResponse.model_validate(vehicle)
