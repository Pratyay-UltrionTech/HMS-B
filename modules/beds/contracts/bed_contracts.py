"""
Pydantic contracts for Beds, Wards, and Rooms.

Conforms to UltrionTech-Backend-Template modules/beds/contracts/ specification.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class BedDashboardRow(BaseModel):
    bed_id: UUID
    ward_id: UUID
    room_id: UUID
    ward_name: str | None = None
    room_code: str | None = None
    bed_code: str
    status: str  # Available | Occupied
    is_occupied: bool
    patient_id: UUID | None = None
    patient_name: str | None = None
    patient_uhid: str | None = None
    admission_id: UUID | None = None
    doctor_name: str | None = None

    model_config = ConfigDict(from_attributes=True)


class OccupancyReport(BaseModel):
    total_beds: int
    occupied_beds: int
    available_beds: int
    occupancy_percent: float
    by_ward: list[dict[str, Any]] = Field(default_factory=list)


class WardRoomOption(BaseModel):
    id: UUID
    name: str
    ward_type: str | None = None
    admission_fee: float = 0.0
    bed_charge_per_day: float = 0.0

    model_config = ConfigDict(from_attributes=True)


class RoomOption(BaseModel):
    id: UUID
    ward_id: UUID
    room_code: str
    name: str | None = None
    bed_count: int

    model_config = ConfigDict(from_attributes=True)


class BedOption(BaseModel):
    id: UUID
    bed_code: str
    room_id: UUID
    room_code: str | None = None
    ward_id: UUID
    ward_name: str | None = None
    is_occupied: bool

    model_config = ConfigDict(from_attributes=True)


class WardsRoomsCatalog(BaseModel):
    wards: list[dict[str, Any]]
    rooms: list[dict[str, Any]]
