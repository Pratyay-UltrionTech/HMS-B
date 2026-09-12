"""
Database repository for Ambulance Fleet and Dispatch.

Conforms to UltrionTech-Backend-Template modules/ambulance/db/ specification.
All operations scoped strictly to hospital_id.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from hms_migration.modules.ambulance.entities.ambulance_entities import (
    AmbulanceDispatch,
    AmbulanceOperationalStatus,
    AmbulanceVehicle,
    DispatchStatus,
)


class AmbulanceRepository:
    """Encapsulated data access for Ambulance fleet and dispatch."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    def generate_next_dispatch_code(self) -> str:
        """Generate dispatch code: DISP-YYYY-NNNNN."""
        year = date.today().year
        prefix = f"DISP-{year}-"
        latest = (
            self.db.query(func.max(AmbulanceDispatch.dispatch_code))
            .filter(
                AmbulanceDispatch.hospital_id == self.hospital_id,
                AmbulanceDispatch.dispatch_code.like(f"{prefix}%"),
            )
            .scalar()
        )
        seq = 0
        if latest:
            try:
                seq = int(str(latest).rsplit("-", 1)[-1])
            except ValueError:
                seq = 0
        return f"{prefix}{seq + 1:05d}"

    def get_vehicle_by_id(self, vehicle_id: UUID) -> AmbulanceVehicle | None:
        return (
            self.db.query(AmbulanceVehicle)
            .filter(
                AmbulanceVehicle.id == vehicle_id,
                AmbulanceVehicle.hospital_id == self.hospital_id,
            )
            .first()
        )

    def get_vehicle_by_reg(self, reg_no: str) -> AmbulanceVehicle | None:
        return (
            self.db.query(AmbulanceVehicle)
            .filter(
                AmbulanceVehicle.registration_number == reg_no.strip(),
                AmbulanceVehicle.hospital_id == self.hospital_id,
            )
            .first()
        )

    def list_vehicles(self, available_only: bool = False) -> list[AmbulanceVehicle]:
        q = self.db.query(AmbulanceVehicle).filter(
            AmbulanceVehicle.hospital_id == self.hospital_id,
            AmbulanceVehicle.is_active.is_(True),
        )
        if available_only:
            q = q.filter(AmbulanceVehicle.operational_status == AmbulanceOperationalStatus.available)
        return q.order_by(AmbulanceVehicle.registration_number.asc()).all()

    def get_dispatch_by_id(self, dispatch_id: UUID) -> AmbulanceDispatch | None:
        return (
            self.db.query(AmbulanceDispatch)
            .options(
                joinedload(AmbulanceDispatch.ambulance),
                joinedload(AmbulanceDispatch.patient),
                joinedload(AmbulanceDispatch.emergency_encounter),
            )
            .filter(
                AmbulanceDispatch.id == dispatch_id,
                AmbulanceDispatch.hospital_id == self.hospital_id,
            )
            .first()
        )

    def list_dispatches(self, active_only: bool = False) -> list[AmbulanceDispatch]:
        q = (
            self.db.query(AmbulanceDispatch)
            .options(
                joinedload(AmbulanceDispatch.ambulance),
                joinedload(AmbulanceDispatch.patient),
            )
            .filter(AmbulanceDispatch.hospital_id == self.hospital_id)
        )
        if active_only:
            q = q.filter(AmbulanceDispatch.status.in_([
                DispatchStatus.requested,
                DispatchStatus.dispatched,
                DispatchStatus.at_scene,
                DispatchStatus.transporting,
            ]))
        return q.order_by(AmbulanceDispatch.requested_at.desc()).all()

    def add(self, entity: object) -> None:
        self.db.add(entity)

    def commit(self) -> None:
        self.db.commit()

    def flush(self) -> None:
        self.db.flush()

    def refresh(self, entity: object) -> None:
        self.db.refresh(entity)
