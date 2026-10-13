"""
Action to book a new appointment or auto-register patient and book.

Conforms to UltrionTech-Backend-Template modules/appointments/actions/ specification.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from modules.appointments.contracts.appointments_contracts import (
    AppointmentListItem,
    BookAppointmentRequest,
)
from modules.appointments.db.appointments_repository import AppointmentsRepository
from modules.appointments.db.availability_reader import AvailabilityReader
from modules.appointments.db.pricing_reader import PricingReader
from modules.appointments.entities.appointment import Appointment
from modules.appointments.entities.enums import AppointmentStatus
from modules.appointments.exceptions.appointments_exceptions import (
    AppointmentConflictError,
    AppointmentNotFoundError,
    AppointmentValidationError,
)
from modules.appointments.validators.appointments_validator import AppointmentsValidator
from modules.billing.services.idempotency_service import IdempotencyService
from modules.patients.db.patients_repository import PatientRepository
from shared.audit import write_audit_log
from shared.hospital_clock import get_hospital_local_date, hospital_today
from shared.sync import stage_sync_event


class BookAppointmentAction:
    """Orchestrates appointment booking workflow."""

    def __init__(
        self,
        db: Session,
        hospital_id: UUID,
        repo: AppointmentsRepository,
        avail_reader: AvailabilityReader,
        pricing_reader: PricingReader,
        *,
        clock_now: datetime | None = None,
    ) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = repo
        self.avail_reader = avail_reader
        self.pricing_reader = pricing_reader
        self.clock_now = clock_now

    def execute(
        self,
        payload: BookAppointmentRequest,
        user: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> AppointmentListItem:
        idemp_svc = IdempotencyService(self.db)
        scope = "appointment_booking"
        clean_key = idempotency_key.strip() if idempotency_key else None
        req_hash = idemp_svc.compute_request_hash(payload) if clean_key else ""

        if clean_key:
            claimed, cached = idemp_svc.claim_or_replay(
                self.hospital_id, scope, clean_key, req_hash
            )
            if not claimed and cached is not None:
                return AppointmentListItem.model_validate(cached[1])

        # 1. Past slot validation
        AppointmentsValidator.assert_not_past_slot(
            payload.appointment_date, payload.appointment_time
        )

        # 2. Canonical Doctor Availability & Concurrency Guard
        from modules.doctors.services.effective_availability_service import (
            EffectiveDoctorAvailabilityService,
        )
        avail_svc = EffectiveDoctorAvailabilityService(self.db, self.hospital_id)
        duration = 15
        if payload.appointment_type_id:
            from modules.appointments.entities.appointment_type import AppointmentType
            appt_type = (
                self.db.query(AppointmentType)
                .filter(
                    AppointmentType.id == payload.appointment_type_id,
                    AppointmentType.hospital_id == self.hospital_id,
                )
                .first()
            )
            if appt_type and appt_type.slot_duration_minutes:
                duration = appt_type.slot_duration_minutes
        else:
            duration = avail_svc.get_slot_duration()

        ok, reason, code = avail_svc.check_slot_availability(
            payload.doctor_id,
            payload.appointment_date,
            payload.appointment_time,
            duration,
            for_update=True,
        )
        if not ok:
            if code == "DOCTOR_NOT_FOUND":
                raise AppointmentNotFoundError(reason or "Doctor not found")
            elif code in ("HOLIDAY", "OUTSIDE_SHIFT"):
                raise AppointmentValidationError(reason or "Invalid appointment slot")
            else:
                raise AppointmentConflictError(reason or "Doctor is not available at this time")

        doctor_user = avail_svc.get_doctor(payload.doctor_id)
        doctor_name = doctor_user.name if doctor_user else "Doctor"

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
                from modules.patients.entities.patient import Patient, PatientStatus

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

        # Generate consultation billing charge and payment settlement (Phase B)
        settlement_metadata: dict[str, Any] = {}
        if fee > 0:
            from modules.billing.entities.billing_entities import (
                BillingPaymentMethod,
                BillingSourceType,
            )
            from modules.billing.services.billing_service import create_payment, ensure_charge
            from modules.billing.services.invoice_service import issue_receipt_for_payment

            charge = ensure_charge(
                self.db,
                hospital_id=self.hospital_id,
                patient_id=patient.id,
                source_type=BillingSourceType.consultation,
                source_id=appt.id,
                description=f"Consultation Fee - Dr. {doctor_name} ({appt.op_id})",
                charge_amount=fee,
                created_by_name=str(user.get("name") or "System"),
            )
            settlement_metadata["charge_id"] = charge.id

            if payload.payment_method:
                # Phase D Closure 2: Explicit financial mutation authorization.
                # Collecting consultation payments and creating financial records (BillingPayment,
                # PaymentAllocation, BillingReceipt) requires explicit billing.create permission.
                from shared.auth.service import authorization
                auth_user = dict(user) if isinstance(user, dict) else {}
                if not auth_user.get("hospital_uuid"):
                    auth_user["hospital_uuid"] = str(self.hospital_id)
                try:
                    authorization.require(auth_user, ("billing", "create"), db=self.db)
                except Exception:
                    self.db.rollback()
                    raise

                try:
                    tender_method = BillingPaymentMethod(payload.payment_method.lower())
                except ValueError as err:
                    self.db.rollback()
                    raise AppointmentValidationError(
                        f"Invalid payment method: {payload.payment_method}. Allowed: cash, card, upi, bank_transfer, other"
                    ) from err

                try:
                    ref = payload.payment_reference.strip() if payload.payment_reference else None
                    settlement_date = get_hospital_local_date(
                        self.db, self.hospital_id, self.clock_now
                    )
                    pay = create_payment(
                        self.db,
                        hospital_id=self.hospital_id,
                        patient_id=patient.id,
                        amount=fee,
                        payment_date=settlement_date,
                        payment_method=tender_method,
                        notes=f"Consultation fee settlement for {appt.op_id}",
                        received_by_name=str(user.get("name") or "Staff"),
                        account_id=charge.account_id,
                        reference_number=ref,
                        idempotency_key=f"appt_pay_{appt.id}",
                        allocations_plan=[{"charge_id": charge.id, "amount": fee}],
                        allocate=True,
                    )
                    receipt = issue_receipt_for_payment(
                        self.db,
                        pay,
                        reference_number=ref,
                    )

                    write_audit_log(
                        self.db,
                        hospital_id=self.hospital_id,
                        actor=user,
                        action="create",
                        entity_type="payment",
                        entity_id=pay.id,
                        summary=f"Settled consultation payment ₹{fee:.2f} via {tender_method.value} for {appt.op_id}",
                    )
                    write_audit_log(
                        self.db,
                        hospital_id=self.hospital_id,
                        actor=user,
                        action="create",
                        entity_type="receipt",
                        entity_id=receipt.id,
                        summary=f"Issued receipt {receipt.receipt_number} for consultation {appt.op_id}",
                    )

                    settlement_metadata.update({
                        "payment_method": tender_method.value,
                        "payment_amount": fee,
                        "payment_reference": ref,
                        "receipt_number": receipt.receipt_number,
                        "receipt_id": receipt.id,
                        "remaining_balance": 0.0,
                    })
                except Exception as err:
                    self.db.rollback()
                    raise AppointmentValidationError(f"Payment settlement failed: {err}") from err

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=user,
            action="create",
            entity_type="appointment",
            entity_id=appt.id,
            summary=(
                f"Booked ({'Walk-in' if payload.booking_kind == 'walk_in' else 'Future'} → Scheduled) "
                f"{patient.name} with {doctor_name} on {payload.appointment_date} "
                f"{payload.appointment_time} fee={appt.consultation_fee}"
            ),
        )
        stage_sync_event(
            self.db,
            hospital_id=self.hospital_id,
            entity_type="appointment",
            action="create",
            entity_id=appt.id,
            details={"patient_id": str(patient.id), "doctor_id": str(payload.doctor_id)},
        )
        if "receipt_id" in settlement_metadata:
            stage_sync_event(
                self.db,
                hospital_id=self.hospital_id,
                entity_type="payment",
                action="create",
                entity_id=settlement_metadata.get("receipt_id"),
            )
        items = self.repo.hydrate_appointment_items([appt])
        item = items[0]
        if settlement_metadata:
            item = item.model_copy(update=settlement_metadata)

        if clean_key:
            idemp_svc.finalize_response(
                hospital_id=self.hospital_id,
                scope=scope,
                idempotency_key=clean_key,
                response_code=201,
                response_payload=item.model_dump(mode="json"),
            )

        try:
            self.db.commit()
            self.db.refresh(appt)
        except IntegrityError:
            self.db.rollback()
            if clean_key:
                cached = idemp_svc.get_recorded_response(self.hospital_id, scope, clean_key, req_hash)
                if cached is not None:
                    return AppointmentListItem.model_validate(cached[1])
            raise

        return item
