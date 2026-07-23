from datetime import date, datetime, time
from uuid import UUID

from pydantic import BaseModel, Field

from app.models import AdmissionStatus


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


class AdmitRequest(BaseModel):
    patient_id: UUID
    ward_id: UUID
    room_id: UUID
    bed_id: UUID
    doctor_id: UUID | None = None
    admission_date: date | None = None
    notes: str | None = None


class AllocateRequest(BaseModel):
    admission_id: UUID | None = None
    patient_id: UUID | None = None
    ward_id: UUID
    room_id: UUID
    bed_id: UUID


class TransferRequest(BaseModel):
    admission_id: UUID | None = None
    patient_id: UUID | None = None
    to_ward_id: UUID
    to_room_id: UUID
    to_bed_id: UUID


class DischargeRequest(BaseModel):
    admission_id: UUID | None = None
    patient_id: UUID | None = None
    discharge_date: date | None = None
    discharge_time: time | None = None
    discharge_notes: str | None = Field(default=None, max_length=2000)


class AdmissionDetail(BaseModel):
    id: UUID
    patient_id: UUID
    patient_name: str | None = None
    patient_uhid: str | None = None
    patient_mobile: str | None = None
    ward_id: UUID
    room_id: UUID
    bed_id: UUID
    ward_name: str | None = None
    room_code: str | None = None
    bed_code: str | None = None
    doctor_id: UUID | None = None
    doctor_name: str | None = None
    status: AdmissionStatus
    notes: str | None = None
    discharge_notes: str | None = None
    admitted_at: datetime
    discharged_at: datetime | None = None
    admission_fee: float = 0
    bed_charge_per_day: float = 0


class OccupancyReport(BaseModel):
    total_beds: int
    occupied_beds: int
    available_beds: int
    occupancy_percent: float
    by_ward: list[dict] = []


class WardRoomOption(BaseModel):
    id: UUID
    name: str
    ward_type: str | None = None
    admission_fee: float = 0
    bed_charge_per_day: float = 0


class RoomOption(BaseModel):
    id: UUID
    ward_id: UUID
    room_code: str
    name: str | None = None
    bed_count: int


class BedOption(BaseModel):
    id: UUID
    bed_code: str
    room_id: UUID
    room_code: str | None = None
    ward_id: UUID
    ward_name: str | None = None
    is_occupied: bool
