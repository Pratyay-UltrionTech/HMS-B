"""
Action to preview consultation fee for an appointment booking.

Conforms to UltrionTech-Backend-Template modules/appointments/actions/ specification.
"""

from datetime import date
from uuid import UUID

from sqlalchemy.orm import Session

from hms_migration.modules.appointments.contracts.appointments_contracts import (
    FeePreviewResponse,
)
from hms_migration.modules.appointments.db.availability_reader import AvailabilityReader
from hms_migration.modules.appointments.db.pricing_reader import PricingReader
from hms_migration.modules.appointments.entities.appointment_type import AppointmentType
from hms_migration.modules.appointments.exceptions.appointments_exceptions import (
    AppointmentNotFoundError,
)
from hms_migration.modules.patients.db.patients_repository import PatientRepository


class FeePreviewAction:
    """Calculates fee preview for doctor consultation."""

    def __init__(
        self,
        db: Session,
        hospital_id: UUID,
        avail_reader: AvailabilityReader,
        pricing_reader: PricingReader,
    ) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.avail_reader = avail_reader
        self.pricing_reader = pricing_reader

    def execute(
        self,
        doctor_id: UUID,
        appointment_type_id: UUID,
        wing_id: UUID | None = None,
        department_id: UUID | None = None,
        patient_id: UUID | None = None,
        appointment_date: date | None = None,
    ) -> FeePreviewResponse:
        doctor = self.avail_reader.get_doctor(doctor_id)
        if not doctor:
            raise AppointmentNotFoundError("Doctor not found")

        appt_type = (
            self.db.query(AppointmentType)
            .filter(
                AppointmentType.id == appointment_type_id,
                AppointmentType.hospital_id == self.hospital_id,
                AppointmentType.is_active.is_(True),
            )
            .first()
        )
        if not appt_type:
            raise AppointmentNotFoundError("Appointment type not found")

        # Resolve wing and department
        w, d = self.pricing_reader.resolve_wing_department_for_doctor(
            doctor_id=doctor_id,
            wing_id=wing_id,
            department_id=department_id,
        )

        pricing = self.pricing_reader.find_pricing(
            doctor_id=doctor_id,
            appt_type_id=appointment_type_id,
            wing_id=w,
            department_id=d,
        )

        on_date = appointment_date or date.today()
        if pricing:
            base_fee = float(pricing.get("consultation_fee") or 0.0)
            pricing_found = True
            free_days = pricing.get("followup_free_days")

            is_follow_up = False
            followup_eligibility = None
            followup_message = None
            last_date = None

            if patient_id:
                last_date = self.pricing_reader.get_last_completed_visit_date(
                    patient_id, doctor_id
                )
                if last_date and free_days is not None:
                    days_since = (on_date - last_date).days
                    if 0 <= days_since <= free_days:
                        is_follow_up = True
                        fee = 0.0
                        followup_eligibility = "eligible"
                        followup_message = f"Eligible for free follow-up (valid upto {free_days} days)"
                    else:
                        fee = base_fee
                        followup_eligibility = "expired"
                        followup_message = f"Follow-up validity expired ({days_since} days since last visit; valid upto {free_days} days)"
                else:
                    fee = base_fee
            else:
                fee = base_fee

            return FeePreviewResponse(
                consultation_fee=fee,
                base_fee=base_fee,
                pricing_found=pricing_found,
                followup_free_days=free_days,
                is_follow_up=is_follow_up,
                followup_eligibility=followup_eligibility,
                followup_message=followup_message,
                last_completed_visit_date=last_date,
                appointment_type_name=appt_type.name,
                doctor_name=doctor["name"],
                wing_id=w,
                department_id=d,
            )

        fallback = self.pricing_reader.get_doctor_fallback_fee(doctor_id)
        return FeePreviewResponse(
            consultation_fee=fallback,
            base_fee=fallback,
            pricing_found=False,
            followup_free_days=None,
            is_follow_up=False,
            followup_eligibility=None,
            followup_message=None,
            last_completed_visit_date=None,
            appointment_type_name=appt_type.name,
            doctor_name=doctor["name"],
            wing_id=w,
            department_id=d,
        )
