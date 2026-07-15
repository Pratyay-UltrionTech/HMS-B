from datetime import date
from uuid import UUID

from pydantic import BaseModel


class MetricRow(BaseModel):
    metric: str
    count: int | float | str


class NamedCountRow(BaseModel):
    name: str
    count: int
    extra: dict | None = None


class WardOccupancyRow(BaseModel):
    ward_name: str
    occupied: int
    available: int
    total: int
    occupancy_percent: float


class DoctorPerfRow(BaseModel):
    doctor_id: UUID
    doctor_name: str
    patients_seen: int
    appointments_completed: int
    appointments_total: int
    average_consultation_minutes: float | None = None
    revenue: float


class PatientReportResponse(BaseModel):
    metrics: list[MetricRow]
    generated_at: str
    filters: dict


class AppointmentReportResponse(BaseModel):
    metrics: list[MetricRow]
    by_doctor: list[NamedCountRow]
    generated_at: str
    filters: dict


class BedReportResponse(BaseModel):
    metrics: list[MetricRow]
    by_ward: list[WardOccupancyRow]
    generated_at: str
    filters: dict


class DoctorReportResponse(BaseModel):
    doctors: list[DoctorPerfRow]
    metrics: list[MetricRow]
    generated_at: str
    filters: dict


class DailySummaryResponse(BaseModel):
    summary_date: date
    new_patients: int
    appointments: int
    admissions: int
    discharges: int
    revenue: float
    occupied_beds: int
    metrics: list[MetricRow]
    generated_at: str
    filters: dict
