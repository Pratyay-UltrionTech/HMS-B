"""
Actions for Doctor Outpatient Appointments, Scheduling Context, and Calendar.

Separated from doctor_actions.py to maintain files strictly under 500 lines
as specified in the UltrionTech-Backend-Template guidelines.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session, joinedload

from hms_migration.modules.appointments.entities.appointment import Appointment
from hms_migration.modules.appointments.entities.enums import AppointmentStatus
from hms_migration.modules.appointments.services.appointment_lifecycle import TERMINAL
from hms_migration.modules.doctors.contracts.doctor_contracts import (
    DoctorAppointmentCreate,
    DoctorAppointmentResponse,
    DoctorAppointmentUpdate,
    DoctorScheduleContext,
    TransferToInpatientRequest,
)
from hms_migration.modules.doctors.db.doctors_repository import DoctorsRepository
from hms_migration.modules.doctors.services.doctor_schedule_service import (
    fmt_time_hhmm,
    get_shift_bounds,
    slot_duration_minutes,
)
from hms_migration.modules.patients.entities.patient import Patient
from hms_migration.shared.audit.service import write_audit_log


def appt_load_options():
    return [
        joinedload(Appointment.patient),
        joinedload(Appointment.doctor),
        joinedload(Appointment.nurse),
        joinedload(Appointment.appointment_type),
    ]


def to_doctor_appointment_response(a: Appointment) -> DoctorAppointmentResponse:
    patient = a.patient
    doctor = a.doctor
    nurse = a.nurse
    appt_type = getattr(a, "appointment_type", None)
    return DoctorAppointmentResponse(
        id=a.id,
        hospital_id=a.hospital_id,
        doctor_id=a.doctor_id,
        patient_id=a.patient_id,
        appointment_date=a.appointment_date,
        appointment_time=a.appointment_time,
        purpose=a.purpose,
        visit_type=a.visit_type or "OPD",
        status=a.status,
        notes=a.notes,
        created_at=a.created_at,
        patient_name=patient.name if patient else "",
        patient_mobile=patient.mobile if patient else "",
        patient_uhid=getattr(patient, "uhid", None) if patient else None,
        patient_age=patient.age if patient else None,
        patient_gender=patient.gender if patient else None,
        doctor_name=doctor.name if doctor else "",
        nurse_id=a.nurse_id,
        nurse_name=nurse.name if nurse else None,
        op_id=getattr(a, "op_id", None),
        queue_token=getattr(a, "queue_token", None),
        checked_in_at=getattr(a, "checked_in_at", None),
        appointment_type_id=getattr(a, "appointment_type_id", None),
        appointment_type_name=appt_type.name if appt_type else None,
        slot_duration_minutes=appt_type.slot_duration_minutes if appt_type else 15,
        consultation_fee=float(getattr(a, "consultation_fee", 0) or 0),
        followup_eligibility=getattr(a, "followup_eligibility", None),
    )


class CreateDoctorAppointmentAction:
    def __init__(self, db: Session) -> None:
        self.db = db

    def execute(
        self,
        hospital_id: UUID,
        doctor_id: UUID,
        payload: DoctorAppointmentCreate,
        actor: dict[str, Any],
    ) -> DoctorAppointmentResponse:
        patient = (
            self.db.query(Patient)
            .filter(Patient.id == payload.patient_id, Patient.hospital_id == hospital_id)
            .first()
        )
        if not patient:
            raise HTTPException(status_code=404, detail="Patient not found")

        appt = Appointment(
            hospital_id=hospital_id,
            doctor_id=doctor_id,
            patient_id=payload.patient_id,
            appointment_date=payload.appointment_date,
            appointment_time=payload.appointment_time,
            purpose=payload.purpose.strip(),
            notes=payload.notes.strip() if payload.notes else None,
            status=payload.status,
            nurse_id=payload.nurse_id,
        )
        self.db.add(appt)
        self.db.flush()
        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="create",
            entity_type="appointment",
            entity_id=str(appt.id),
            summary=f"Created appointment for {patient.name} on {payload.appointment_date}",
        )
        self.db.commit()
        refreshed = (
            self.db.query(Appointment)
            .options(*appt_load_options())
            .filter(Appointment.id == appt.id)
            .first()
        )
        return to_doctor_appointment_response(refreshed or appt)


class ListDoctorAppointmentsAction:
    def __init__(self, db: Session) -> None:
        self.db = db

    def execute(
        self,
        hospital_id: UUID,
        doctor_id: UUID,
        on_date: date | None = None,
        status_filter: AppointmentStatus | None = None,
        visit_type: str | None = None,
    ) -> list[DoctorAppointmentResponse]:
        q = (
            self.db.query(Appointment)
            .options(*appt_load_options())
            .filter(Appointment.doctor_id == doctor_id, Appointment.hospital_id == hospital_id)
        )
        if on_date:
            q = q.filter(Appointment.appointment_date == on_date)
        if status_filter:
            q = q.filter(Appointment.status == status_filter)
        if visit_type:
            q = q.filter(Appointment.visit_type == visit_type)
        rows = q.order_by(Appointment.appointment_time.asc()).all()
        return [to_doctor_appointment_response(a) for a in rows]


class GetDoctorCalendarAction:
    def __init__(self, db: Session) -> None:
        self.db = db

    def execute(
        self, hospital_id: UUID, doctor_id: UUID, week_start: date
    ) -> list[DoctorAppointmentResponse]:
        end = week_start + timedelta(days=6)
        rows = (
            self.db.query(Appointment)
            .options(*appt_load_options())
            .filter(
                Appointment.doctor_id == doctor_id,
                Appointment.hospital_id == hospital_id,
                Appointment.appointment_date >= week_start,
                Appointment.appointment_date <= end,
            )
            .order_by(Appointment.appointment_date.asc(), Appointment.appointment_time.asc())
            .all()
        )
        return [to_doctor_appointment_response(a) for a in rows]


class UpdateDoctorAppointmentAction:
    def __init__(self, db: Session) -> None:
        self.db = db

    def execute(
        self,
        hospital_id: UUID,
        doctor_id: UUID,
        appointment_id: UUID,
        payload: DoctorAppointmentUpdate,
        actor: dict[str, Any],
    ) -> DoctorAppointmentResponse:
        appt = (
            self.db.query(Appointment)
            .options(*appt_load_options())
            .filter(
                Appointment.id == appointment_id,
                Appointment.doctor_id == doctor_id,
                Appointment.hospital_id == hospital_id,
            )
            .first()
        )
        if not appt:
            raise HTTPException(status_code=404, detail="Appointment not found")

        if payload.appointment_date is not None:
            appt.appointment_date = payload.appointment_date
        if payload.appointment_time is not None:
            appt.appointment_time = payload.appointment_time
        if payload.purpose is not None:
            appt.purpose = payload.purpose.strip()
        if payload.notes is not None:
            appt.notes = payload.notes.strip() if payload.notes else None
        if payload.status is not None:
            appt.status = payload.status
        if payload.nurse_id is not None:
            appt.nurse_id = payload.nurse_id

        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="update",
            entity_type="appointment",
            entity_id=str(appt.id),
            summary=f"Updated appointment {appt.id} for {appt.patient.name if appt.patient else 'patient'}",
        )
        self.db.commit()
        refreshed = (
            self.db.query(Appointment)
            .options(*appt_load_options())
            .filter(Appointment.id == appt.id)
            .first()
        )
        return to_doctor_appointment_response(refreshed or appt)


class TransferAppointmentToInpatientAction:
    def __init__(self, db: Session) -> None:
        self.db = db

    def execute(
        self,
        hospital_id: UUID,
        doctor_id: UUID,
        appointment_id: UUID,
        payload: TransferToInpatientRequest,
        actor: dict[str, Any],
    ) -> DoctorAppointmentResponse:
        appt = (
            self.db.query(Appointment)
            .options(*appt_load_options())
            .filter(
                Appointment.id == appointment_id,
                Appointment.doctor_id == doctor_id,
                Appointment.hospital_id == hospital_id,
            )
            .first()
        )
        if not appt:
            raise HTTPException(status_code=404, detail="Appointment not found")
        if appt.status == AppointmentStatus.transferred_to_inpatient and appt.admission_id:
            raise HTTPException(
                status_code=400, detail="This visit is already transferred to inpatient"
            )
        if appt.status == AppointmentStatus.ipd_transfer_requested:
            return to_doctor_appointment_response(appt)
        if appt.status in TERMINAL:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot transfer a {appt.status.value} visit to inpatient",
            )

        appt.status = AppointmentStatus.ipd_transfer_requested
        if payload.notes:
            appt.notes = payload.notes.strip()

        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="update",
            entity_type="appointment",
            entity_id=str(appt.id),
            summary=(
                f"Requested IPD transfer for {appt.patient.name if appt.patient else 'patient'} "
                f"({getattr(appt, 'op_id', None) or 'OP'}) — waiting for nurse to book a bed"
            ),
        )
        self.db.commit()
        refreshed = (
            self.db.query(Appointment)
            .options(*appt_load_options())
            .filter(Appointment.id == appt.id)
            .first()
        )
        return to_doctor_appointment_response(refreshed or appt)


class GetDoctorScheduleContextAction:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = DoctorsRepository(db)

    def execute(self, hospital_id: UUID, doctor_id: UUID) -> DoctorScheduleContext:
        doctor = self.repo.get_doctor(hospital_id, doctor_id)
        if not doctor:
            raise HTTPException(status_code=404, detail="Doctor not found")
        shift_start, shift_end, shift_name = get_shift_bounds(doctor)
        uses_default = doctor.shift is None or not doctor.shift.is_active
        return DoctorScheduleContext(
            doctor_id=doctor_id,
            shift_name=shift_name,
            shift_start=fmt_time_hhmm(shift_start),
            shift_end=fmt_time_hhmm(shift_end),
            slot_duration_minutes=slot_duration_minutes(self.db, hospital_id),
            uses_default_shift=uses_default,
        )
