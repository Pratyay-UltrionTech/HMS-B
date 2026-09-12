"""
Pydantic contracts for Ambulance Management domain.

Conforms to UltrionTech-Backend-Template modules/ambulance/contracts/ specification.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from hms_migration.modules.ambulance.entities.ambulance_entities import (
    AmbulanceOperationalStatus,
    AmbulanceType,
    DispatchPriority,
    DispatchStatus,
)


class AmbulanceVehicleCreate(BaseModel):
    registration_number: str = Field(min_length=3, max_length=32)
    vehicle_type: AmbulanceType = AmbulanceType.bls
    model: str = Field(min_length=2, max_length=128)
    operational_status: AmbulanceOperationalStatus = AmbulanceOperationalStatus.available
    onboard_equipment: dict | None = None
    current_driver_name: str | None = None
    current_paramedic_name: str | None = None


class AmbulanceVehicleUpdate(BaseModel):
    operational_status: AmbulanceOperationalStatus | None = None
    onboard_equipment: dict | None = None
    current_driver_name: str | None = None
    current_paramedic_name: str | None = None
    is_active: bool | None = None


class AmbulanceVehicleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    hospital_id: UUID
    registration_number: str
    vehicle_type: AmbulanceType
    model: str
    operational_status: AmbulanceOperationalStatus
    onboard_equipment: dict | None
    current_driver_name: str | None
    current_paramedic_name: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class AmbulanceDispatchCreate(BaseModel):
    ambulance_id: UUID
    patient_id: UUID | None = None
    emergency_encounter_id: UUID | None = None
    caller_name: str = Field(min_length=2)
    caller_phone: str = Field(min_length=7)
    pickup_location: str = Field(min_length=3)
    drop_location: str = Field(min_length=3)
    priority: DispatchPriority = DispatchPriority.emergency
    driver_name: str = Field(min_length=2)
    paramedic_name: str | None = None
    odometer_start: float | None = None
    remarks: str | None = None


class AmbulanceDispatchUpdateStatus(BaseModel):
    status: DispatchStatus
    odometer_end: float | None = None
    remarks: str | None = None


class AmbulanceDispatchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    hospital_id: UUID
    dispatch_code: str
    ambulance_id: UUID
    patient_id: UUID | None
    emergency_encounter_id: UUID | None
    caller_name: str
    caller_phone: str
    pickup_location: str
    drop_location: str
    priority: DispatchPriority
    driver_name: str
    paramedic_name: str | None
    status: DispatchStatus
    requested_at: datetime
    dispatched_at: datetime | None
    arrived_pickup_at: datetime | None
    departed_pickup_at: datetime | None
    arrived_destination_at: datetime | None
    completed_at: datetime | None
    odometer_start: float | None
    odometer_end: float | None
    remarks: str | None
