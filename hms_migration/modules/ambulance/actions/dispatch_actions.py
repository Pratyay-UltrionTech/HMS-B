"""
Actions for Feature 11: Emergency Ambulance Dispatch & Trip Tracking.

Conforms to UltrionTech-Backend-Template modules/ambulance/actions/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from hms_migration.modules.ambulance.contracts.ambulance_contracts import (
    AmbulanceDispatchCreate,
    AmbulanceDispatchResponse,
    AmbulanceDispatchUpdateStatus,
)
from hms_migration.modules.ambulance.db.ambulance_repository import AmbulanceRepository
from hms_migration.modules.ambulance.entities.ambulance_entities import (
    AmbulanceDispatch,
    AmbulanceOperationalStatus,
    DispatchStatus,
)
from hms_migration.shared.audit.service import write_audit_log


class CreateAmbulanceDispatchAction:
    """Assign available ambulance to an emergency request and initiate dispatch."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = AmbulanceRepository(db, hospital_id)

    def execute(self, payload: AmbulanceDispatchCreate, actor: dict[str, Any]) -> AmbulanceDispatchResponse:
        vehicle = self.repo.get_vehicle_by_id(payload.ambulance_id)
        if not vehicle:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ambulance not found")

        if vehicle.operational_status != AmbulanceOperationalStatus.available:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Ambulance {vehicle.registration_number} is currently {vehicle.operational_status.value}",
            )

        code = self.repo.generate_next_dispatch_code()
        now = datetime.now(timezone.utc)

        dispatch = AmbulanceDispatch(
            hospital_id=self.hospital_id,
            dispatch_code=code,
            ambulance_id=payload.ambulance_id,
            patient_id=payload.patient_id,
            emergency_encounter_id=payload.emergency_encounter_id,
            caller_name=payload.caller_name.strip(),
            caller_phone=payload.caller_phone.strip(),
            pickup_location=payload.pickup_location.strip(),
            drop_location=payload.drop_location.strip(),
            priority=payload.priority,
            driver_name=payload.driver_name.strip(),
            paramedic_name=payload.paramedic_name.strip() if payload.paramedic_name else None,
            status=DispatchStatus.dispatched,
            requested_at=now,
            dispatched_at=now,
            odometer_start=payload.odometer_start,
            remarks=payload.remarks.strip() if payload.remarks else None,
        )
        self.repo.add(dispatch)

        # Mark vehicle on trip
        vehicle.operational_status = AmbulanceOperationalStatus.on_trip

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="dispatch_ambulance",
            entity_type="ambulance_dispatch",
            entity_id=dispatch.id,
            summary=f"Dispatched ambulance {vehicle.registration_number} under {code}",
            details={"code": code, "vehicle": vehicle.registration_number},
        )
        self.repo.commit()
        self.repo.refresh(dispatch)
        return AmbulanceDispatchResponse.model_validate(dispatch)


class UpdateAmbulanceDispatchStatusAction:
    """Transition dispatch status (at_scene, transporting, completed, cancelled)."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = AmbulanceRepository(db, hospital_id)

    def execute(
        self,
        dispatch_id: UUID,
        payload: AmbulanceDispatchUpdateStatus,
        actor: dict[str, Any],
    ) -> AmbulanceDispatchResponse:
        dispatch = self.repo.get_dispatch_by_id(dispatch_id)
        if not dispatch:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dispatch record not found")

        if dispatch.status in (DispatchStatus.completed, DispatchStatus.cancelled):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Dispatch is already finished")

        now = datetime.now(timezone.utc)
        dispatch.status = payload.status

        if payload.status == DispatchStatus.at_scene:
            dispatch.arrived_pickup_at = now
        elif payload.status == DispatchStatus.transporting:
            dispatch.departed_pickup_at = now
        elif payload.status in (DispatchStatus.completed, DispatchStatus.cancelled):
            dispatch.completed_at = now
            if payload.odometer_end is not None:
                dispatch.odometer_end = payload.odometer_end
            if payload.remarks:
                dispatch.remarks = (
                    f"{dispatch.remarks}\nCompletion Note: {payload.remarks.strip()}"
                    if dispatch.remarks
                    else payload.remarks.strip()
                )
            # Release ambulance vehicle back to available status
            if dispatch.ambulance:
                dispatch.ambulance.operational_status = AmbulanceOperationalStatus.available

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="update_dispatch_status",
            entity_type="ambulance_dispatch",
            entity_id=dispatch.id,
            summary=f"Updated dispatch {dispatch.dispatch_code} to {payload.status.value}",
        )
        self.repo.commit()
        self.repo.refresh(dispatch)
        return AmbulanceDispatchResponse.model_validate(dispatch)
