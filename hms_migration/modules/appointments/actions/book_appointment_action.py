"""
Action to book a new appointment or auto-register patient and book.

Conforms to UltrionTech-Backend-Template modules/appointments/actions/ specification.
"""

from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from hms_migration.modules.appointments.contracts.appointments_contracts import (
    AppointmentListItem,
    BookAppointmentRequest,
)
from hms_migration.modules.appointments.db.appointments_repository import AppointmentsRepository
from hms_migration.modules.appointments.db.availability_reader import AvailabilityReader
from hms_migration.modules.appointments.db.pricing_reader import PricingReader
from hms_migration.modules.appointments.entities.appointment import Appointment
from hms_migration.modules.appointments.entities.enums import AppointmentStatus
from hms_migration.modules.appointments.exceptions.appointments_exceptions import (
    AppointmentConflictError,
    AppointmentNotFoundError,
    AppointmentValidationError,
)
from hms_migration.modules.appointments.validators.appointments_validator import AppointmentsValidator
from hms_migration.modules.patients.db.patients_repository import PatientRepository
from hms_migration.shared.audit import write_audit_log


class BookAppointmentAction:
    """Orchestrates appointment booking workflow."""

    def __init__(
        self,
        db: Session,
        hospital_id: UUID,
        repo: AppointmentsRepository,
        avail_reader: AvailabilityReader,
        pricing_reader: PricingReader,
    ) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = repo
        self.avail_reader = avail_reader
        self.pricing_reader = pricing_reader

    def execute(
        self,
        payload: BookAppointmentRequest,
        user: dict[str, Any],
    ) -> AppointmentListItem:
        # 1. Past slot validation
        AppointmentsValidator.assert_not_past_slot(
            payload.appointment_date, payload.appointment_time
        )

        # 2. Holiday guard
        holiday = self.avail_reader.get_holiday(payload.appointment_date)
        if holiday:
            raise AppointmentValidationError(
                f"Hospital is closed on {payload.appointment_date} ({holiday['name']})"
            )

        # 3. Doctor exists check
        doctor = self.avail_reader.get_doctor(payload.doctor_id)
        if not doctor:
            raise AppointmentNotFoundError("Doctor not found")

        # 4. Slot conflict guard
        if self.avail_reader.has_slot_conflict(
            payload.doctor_id, payload.appointment_date, payload.appointment_time
        ):
            raise AppointmentConflictError("Doctor already has an appointment at this time")

        # 5. Resolve patient (existing or auto-register)
        patient_repo = PatientRepository(db=self.db, hospital_id=self.hospital_id)
        if payload.patient_id:
            patient = patient_repo.get_by_id(payload.patient_id)
            if not patient:
                raise AppointmentNotFoundError("Patient not found")
        else:
            mobile = payload.mobile or ""
            first = (payload.first_name or "").strip()
            last = (payload.last_name or "").strip()
            requested_name = f"{first} {last}".strip()

            existing = patient_repo.find_by_mobile(mobile)
            if existing:
                existing_name = (existing.name or "").strip()
                if requested_name and existing_name and requested_name.casefold() != existing_name.casefold():
                    raise AppointmentValidationError(
                        f"Mobile {mobile} already belongs to {existing_name} ({existing.uhid or ''}). "
                        "Select that patient under Existing patient, or use a different mobile number."
                    )
                patient = existing
            else:
                uhid = patient_repo.generate_next_uhid()
                name = requested_name
                from hms_migration.modules.patients.entities.patient import Patient, PatientStatus

                patient = Patient(
                    hospital_id=self.hospital_id,
                    uhid=uhid,
                    first_name=first,
                    last_name=last,
                    name=name,
                    mobile=mobile,
                    email=str(payload.email).lower() if payload.email else None,
                    age=payload.age,
                    date_of_birth=payload.date_of_birth,
                    gender=(payload.gender or "other").strip(),
                    address=payload.address.strip() if payload.address else None,
                    emergency_contact=payload.emergency_contact.strip() if payload.emergency_contact else None,
                    blood_group=payload.blood_group,
                    status=PatientStatus.active,
                )
                self.db.add(patient)
                self.db.flush()
                write_audit_log(
                    self.db,
                    hospital_id=self.hospital_id,
                    actor=user,
                    action="create",
                    entity_type="patient",
                    entity_id=patient.id,
                    summary=f"Auto-registered patient {patient.uhid} {patient.name} via appointment booking",
                )

        # 6. Resolve wing and department
        wing_id, dept_id = self.pricing_reader.resolve_wing_department_for_doctor(
            doctor_id=payload.doctor_id,
            wing_id=payload.wing_id,
            department_id=payload.department_id,
            appointment_type_id=payload.appointment_type_id,
        )

        # 7. Resolve consultation fee
        pricing = self.pricing_reader.find_pricing(
            doctor_id=payload.doctor_id,
            appt_type_id=payload.appointment_type_id,
            wing_id=wing_id,
            department_id=dept_id,
        )
        if pricing:
            base_fee = float(pricing.get("consultation_fee") or 0.0)
            followup_eligibility = None
            last_date = self.pricing_reader.get_last_completed_visit_date(
                patient.id, payload.doctor_id
            )
            free_days = pricing.get("followup_free_days")
            if last_date and free_days is not None and (payload.appointment_date - last_date).days <= free_days:
                fee = 0.0
                followup_eligibility = "eligible"
            else:
                fee = base_fee
                followup_eligibility = "expired" if (last_date and free_days) else None
        else:
            fee = self.pricing_reader.get_doctor_fallback_fee(payload.doctor_id)
            followup_eligibility = None

        # 8. Create appointment record
        op_id = self.repo.next_encounter_id("OP")
        visit_label = payload.visit_type.strip() if payload.visit_type else "OPD"
        purpose = payload.purpose.strip() if payload.purpose else visit_label

        appt = Appointment(
            hospital_id=self.hospital_id,
            doctor_id=payload.doctor_id,
            patient_id=patient.id,
            appointment_date=payload.appointment_date,
            appointment_time=payload.appointment_time,
            purpose=purpose,
            visit_type=visit_label,
            appointment_type_id=payload.appointment_type_id,
            wing_id=wing_id,
            department_id=dept_id,
            consultation_fee=fee,
            followup_eligibility=followup_eligibility,
            status=AppointmentStatus.scheduled,
            booking_kind=payload.booking_kind,
            notes=payload.notes.strip() if payload.notes else None,
            op_id=op_id,
            nurse_id=payload.nurse_id,
        )
        self.db.add(appt)
        self.db.flush()

        # Audit log
        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=user,
            action="create",
            entity_type="appointment",
            entity_id=appt.id,
            summary=(
                f"Booked ({'Walk-in' if payload.booking_kind == 'walk_in' else 'Future'} → Scheduled) "
                f"{patient.name} with {doctor['name']} on {payload.appointment_date} "
                f"{payload.appointment_time} fee={appt.consultation_fee}"
            ),
        )
        self.db.commit()
        self.db.refresh(appt)

        items = self.repo.hydrate_appointment_items([appt])
        return items[0]
