"""
MIS API endpoints adhering to UltrionTech-Backend-Template and legacy route parity.

Prefix: /mis
Tags: ["mis"]
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.mis.actions.mis_actions import MisActions
from hms_migration.modules.mis.contracts.mis_contracts import (
    AppointmentReportResponse,
    BedReportResponse,
    DailySummaryResponse,
    DoctorReportResponse,
    FilterItem,
    PatientReportResponse,
)
from hms_migration.shared.auth import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/mis", tags=["mis"])


@router.get("/filters/doctors", response_model=list[FilterItem])
def filter_doctors(
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[FilterItem]:
    return MisActions(db, hospital_id, user).get_filter_doctors()


@router.get("/filters/departments", response_model=list[FilterItem])
def filter_departments(
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[FilterItem]:
    return MisActions(db, hospital_id, user).get_filter_departments()


@router.get("/patients", response_model=PatientReportResponse)
def patient_reports(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    department_id: UUID | None = Query(default=None),
    doctor_id: UUID | None = Query(default=None),
    patient_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> PatientReportResponse:
    return MisActions(db, hospital_id, user).get_patient_reports(
        date_from=date_from,
        date_to=date_to,
        department_id=department_id,
        doctor_id=doctor_id,
        patient_id=patient_id,
        status_filter=status,
    )


@router.get("/appointments", response_model=AppointmentReportResponse)
def appointment_reports(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    department_id: UUID | None = Query(default=None),
    doctor_id: UUID | None = Query(default=None),
    patient_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> AppointmentReportResponse:
    return MisActions(db, hospital_id, user).get_appointment_reports(
        date_from=date_from,
        date_to=date_to,
        department_id=department_id,
        doctor_id=doctor_id,
        patient_id=patient_id,
        status_filter=status,
    )


@router.get("/beds", response_model=BedReportResponse)
def bed_reports(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    department_id: UUID | None = Query(default=None),
    doctor_id: UUID | None = Query(default=None),
    patient_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> BedReportResponse:
    return MisActions(db, hospital_id, user).get_bed_reports(
        date_from=date_from,
        date_to=date_to,
        department_id=department_id,
        doctor_id=doctor_id,
        patient_id=patient_id,
        status_filter=status,
    )


@router.get("/doctors", response_model=DoctorReportResponse)
def doctor_reports(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    department_id: UUID | None = Query(default=None),
    doctor_id: UUID | None = Query(default=None),
    patient_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> DoctorReportResponse:
    return MisActions(db, hospital_id, user).get_doctor_reports(
        date_from=date_from,
        date_to=date_to,
        department_id=department_id,
        doctor_id=doctor_id,
        patient_id=patient_id,
        status_filter=status,
    )


@router.get("/daily-summary", response_model=DailySummaryResponse)
def daily_summary(
    on_date: date | None = Query(default=None, alias="date"),
    department_id: UUID | None = Query(default=None),
    doctor_id: UUID | None = Query(default=None),
    patient_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> DailySummaryResponse:
    return MisActions(db, hospital_id, user).get_daily_summary(
        on_date=on_date,
        department_id=department_id,
        doctor_id=doctor_id,
        patient_id=patient_id,
        status_filter=status,
    )
