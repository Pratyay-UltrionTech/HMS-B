"""
HTTP API router for the Appointments module.

Conforms to UltrionTech-Backend-Template modules/appointments/api/ specification.
Thin controller delegates to business actions, validates inputs, and returns typed responses.
"""

from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres import get_transitional_sync_session as get_db
from hms_migration.modules.appointments.actions.appointment_status_actions import (
    AdmitIpdAction,
    AssignNurseAction,
    CancelAppointmentAction,
    CheckInAction,
    CompleteAppointmentAction,
    NoShowAction,
    RescheduleAction,
)
from hms_migration.modules.appointments.actions.book_appointment_action import (
    BookAppointmentAction,
)
from hms_migration.modules.appointments.actions.check_availability_action import (
    CheckAvailabilityAction,
)
from hms_migration.modules.appointments.actions.fee_preview_action import (
    FeePreviewAction,
)
from hms_migration.modules.appointments.actions.list_appointments_actions import (
    ListAppointmentsActions,
)
from hms_migration.modules.appointments.contracts.appointments_contracts import (
    AdmitIpdRequest,
    AppointmentListItem,
    AssignNurseRequest,
    BookAppointmentRequest,
    DoctorAvailability,
    FeePreviewResponse,
    QueueGroup,
    RescheduleRequest,
)
from hms_migration.modules.appointments.db.appointments_repository import AppointmentsRepository
from hms_migration.modules.appointments.db.availability_reader import AvailabilityReader
from hms_migration.modules.appointments.db.pricing_reader import PricingReader
from hms_migration.modules.appointments.entities.enums import AppointmentStatus
from hms_migration.shared.auth import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/appointments", tags=["appointments"])


@router.get("/doctors")
def list_doctors(
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    repo = AppointmentsRepository(db, hospital_id)
    return ListAppointmentsActions(repo).list_doctors()


@router.get("/nurses")
def list_nurses(
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    repo = AppointmentsRepository(db, hospital_id)
    return ListAppointmentsActions(repo).list_nurses()


@router.get("/wings")
def list_booking_wings(
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    repo = AppointmentsRepository(db, hospital_id)
    return ListAppointmentsActions(repo).list_wings()


@router.get("/departments")
def list_booking_departments(
    wing_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    repo = AppointmentsRepository(db, hospital_id)
    depts = ListAppointmentsActions(repo).list_departments()
    if wing_id:
        depts = [d for d in depts if d.get("wing_id") == wing_id or d.get("wing_id") is None]
    return depts


@router.get("/visit-types")
def list_visit_types(
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    repo = AppointmentsRepository(db, hospital_id)
    return ListAppointmentsActions(repo).list_visit_types()


@router.get("/fee-preview", response_model=FeePreviewResponse)
def fee_preview(
    doctor_id: UUID = Query(...),
    appointment_type_id: UUID = Query(...),
    wing_id: UUID | None = Query(default=None),
    department_id: UUID | None = Query(default=None),
    patient_id: UUID | None = Query(default=None),
    appointment_date: date | None = Query(default=None, alias="date"),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> FeePreviewResponse:
    avail_reader = AvailabilityReader(db, hospital_id)
    pricing_reader = PricingReader(db, hospital_id)
    return FeePreviewAction(
        db, hospital_id, avail_reader, pricing_reader
    ).execute(
        doctor_id=doctor_id,
        appointment_type_id=appointment_type_id,
        wing_id=wing_id,
        department_id=department_id,
        patient_id=patient_id,
        appointment_date=appointment_date,
    )


@router.get("/availability", response_model=DoctorAvailability)
def check_doctor_availability(
    doctor_id: UUID = Query(...),
    check_date: date = Query(..., alias="date"),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> DoctorAvailability:
    avail_reader = AvailabilityReader(db, hospital_id)
    return CheckAvailabilityAction(avail_reader).execute(doctor_id, check_date)


@router.post("", response_model=AppointmentListItem, status_code=status.HTTP_201_CREATED)
def book_appointment(
    payload: BookAppointmentRequest,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> AppointmentListItem:
    repo = AppointmentsRepository(db, hospital_id)
    avail_reader = AvailabilityReader(db, hospital_id)
    pricing_reader = PricingReader(db, hospital_id)
    return BookAppointmentAction(
        db, hospital_id, repo, avail_reader, pricing_reader
    ).execute(payload=payload, user=user)


@router.get("/today", response_model=list[AppointmentListItem])
def todays_appointments(
    doctor_id: UUID | None = Query(default=None),
    nurse_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[AppointmentListItem]:
    repo = AppointmentsRepository(db, hospital_id)
    items = ListAppointmentsActions(repo).get_today(doctor_id=doctor_id)
    if nurse_id:
        items = [i for i in items if i.nurse_id == nurse_id]
    return items


@router.get("/calendar", response_model=list[AppointmentListItem])
def calendar_view(
    date_from: date = Query(...),
    date_to: date = Query(...),
    doctor_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[AppointmentListItem]:
    repo = AppointmentsRepository(db, hospital_id)
    return ListAppointmentsActions(repo).get_calendar(
        week_start=date_from, doctor_id=doctor_id
    )


@router.get("/queue", response_model=list[QueueGroup])
def doctor_queue(
    on_date: date | None = Query(default=None, alias="date"),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[QueueGroup]:
    repo = AppointmentsRepository(db, hospital_id)
    return ListAppointmentsActions(repo).get_queue()


@router.get("/history", response_model=list[AppointmentListItem])
def appointment_history(
    patient: str | None = Query(default=None),
    doctor_id: UUID | None = Query(default=None),
    on_date: date | None = Query(default=None, alias="date"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    status_filter: AppointmentStatus | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[AppointmentListItem]:
    repo = AppointmentsRepository(db, hospital_id)
    effective_from = on_date or date_from
    effective_to = on_date or date_to
    items = ListAppointmentsActions(repo).get_history(
        doctor_id=doctor_id,
        from_date=effective_from,
        to_date=effective_to,
        status=status_filter,
    )
    if patient:
        term = patient.strip().lower()
        items = [
            i for i in items
            if (i.patient_name and term in i.patient_name.lower())
            or (i.patient_uhid and term in i.patient_uhid.lower())
            or (i.patient_mobile and term in i.patient_mobile.lower())
        ]
    return items


@router.post("/{appointment_id}/check-in", response_model=AppointmentListItem)
def check_in(
    appointment_id: UUID,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> AppointmentListItem:
    repo = AppointmentsRepository(db, hospital_id)
    return CheckInAction(db, hospital_id, repo).execute(appointment_id, user)


@router.post("/{appointment_id}/complete", response_model=AppointmentListItem)
def complete_appointment(
    appointment_id: UUID,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> AppointmentListItem:
    repo = AppointmentsRepository(db, hospital_id)
    return CompleteAppointmentAction(db, hospital_id, repo).execute(appointment_id, user)


@router.post("/{appointment_id}/cancel", response_model=AppointmentListItem)
def cancel_appointment(
    appointment_id: UUID,
    payload: dict[str, Any] | None = None,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> AppointmentListItem:
    reason = payload.get("reason") if payload else None
    repo = AppointmentsRepository(db, hospital_id)
    return CancelAppointmentAction(db, hospital_id, repo).execute(
        appointment_id, reason, user
    )


@router.post("/{appointment_id}/no-show", response_model=AppointmentListItem)
def mark_no_show(
    appointment_id: UUID,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> AppointmentListItem:
    repo = AppointmentsRepository(db, hospital_id)
    return NoShowAction(db, hospital_id, repo).execute(appointment_id, user)


@router.put("/{appointment_id}/reschedule", response_model=AppointmentListItem)
def reschedule_appointment(
    appointment_id: UUID,
    payload: RescheduleRequest,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> AppointmentListItem:
    repo = AppointmentsRepository(db, hospital_id)
    avail_reader = AvailabilityReader(db, hospital_id)
    return RescheduleAction(db, hospital_id, repo, avail_reader).execute(
        appointment_id, payload, user
    )


@router.put("/{appointment_id}/nurse", response_model=AppointmentListItem)
def assign_nurse(
    appointment_id: UUID,
    payload: AssignNurseRequest,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> AppointmentListItem:
    repo = AppointmentsRepository(db, hospital_id)
    return AssignNurseAction(db, hospital_id, repo).execute(
        appointment_id, payload, user
    )


@router.get("/ipd-requests", response_model=list[AppointmentListItem])
def list_ipd_requests(
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[AppointmentListItem]:
    repo = AppointmentsRepository(db, hospital_id)
    return ListAppointmentsActions(repo).get_ipd_requests()


@router.post("/{appointment_id}/admit-ipd")
def admit_ipd_from_nurse(
    appointment_id: UUID,
    payload: AdmitIpdRequest,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> dict[str, Any]:
    repo = AppointmentsRepository(db, hospital_id)
    return AdmitIpdAction(db, hospital_id, repo).execute(
        appointment_id, payload, user
    )
