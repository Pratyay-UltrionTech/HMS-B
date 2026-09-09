"""
Actions for Doctor profiles, directory, appointments, and patient interactions.

Conforms to UltrionTech-Backend-Template modules/doctors/actions/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from hms_migration.modules.appointments.entities.appointment import Appointment
from hms_migration.modules.clinical_records.actions.clinical_actions import (
    to_prescription_response,
    to_record_response,
)
from hms_migration.modules.clinical_records.entities.clinical_record import (
    MedicalRecord,
    Prescription,
)
from hms_migration.modules.doctors.actions.doctor_appointment_actions import (
    CreateDoctorAppointmentAction,
    GetDoctorCalendarAction,
    GetDoctorScheduleContextAction,
    ListDoctorAppointmentsAction,
    TransferAppointmentToInpatientAction,
    UpdateDoctorAppointmentAction,
)
from hms_migration.modules.doctors.contracts.doctor_contracts import (
    DoctorAppointmentResponse,
    DoctorPatientCreate,
    DoctorPatientResponse,
    DoctorPatientUpdate,
    DoctorSummary,
    HospitalClinicProfile,
    IpdFormHistoryItem,
    PatientHistoryResponse,
)
from hms_migration.modules.doctors.db.doctors_repository import DoctorsRepository
from hms_migration.modules.doctors.entities.doctor import HospitalUser
from hms_migration.modules.inpatient.entities.admission import Admission, IpdFormSubmission
from hms_migration.modules.patients.entities.patient import Patient
from hms_migration.modules.vitals.contracts.vitals_contracts import VitalReadingResponse
from hms_migration.modules.vitals.entities.vital_reading import VitalReading
from hms_migration.shared.audit.service import write_audit_log


def to_doctor_patient_response(
    p: Patient, last_visit: date | None = None, last_diagnosis: str | None = None
) -> DoctorPatientResponse:
    return DoctorPatientResponse(
        id=p.id,
        hospital_id=p.hospital_id,
        name=p.name,
        mobile=p.mobile,
        age=p.age,
        gender=p.gender,
        address=p.address,
        created_at=p.created_at,
        last_visit=last_visit,
        last_diagnosis=last_diagnosis,
        uhid=getattr(p, "uhid", None),
        emergency_contact=getattr(p, "emergency_contact", None),
        emergency_contact_name=getattr(p, "emergency_contact_name", None),
        emergency_contact_relation=getattr(p, "emergency_contact_relation", None),
        has_insurance=bool(getattr(p, "has_insurance", False)),
        insurance_provider=getattr(p, "insurance_provider", None),
    )


def to_doctor_appointment_response(a: Appointment) -> DoctorAppointmentResponse:
    adm = getattr(a, "admission", None)
    return DoctorAppointmentResponse(
        id=a.id,
        hospital_id=a.hospital_id,
        doctor_id=a.doctor_id,
        patient_id=a.patient_id,
        appointment_date=a.appointment_date,
        appointment_time=a.appointment_time,
        purpose=a.purpose,
        status=a.status,
        notes=a.notes,
        created_at=a.created_at,
        patient_name=a.patient.name if a.patient else None,
        patient_mobile=a.patient.mobile if a.patient else None,
        doctor_name=a.doctor.name if a.doctor else None,
        patient_uhid=getattr(a.patient, "uhid", None) if a.patient else None,
        op_id=getattr(a, "op_id", None),
        admission_id=a.admission_id,
        ip_id=getattr(adm, "ip_id", None) if adm else None,
        admission_ward=adm.ward.name if adm and adm.ward else None,
        admission_bed=adm.bed.bed_code if adm and adm.bed else None,
        admission_status=adm.status.value if adm else None,
        nurse_id=a.nurse_id,
        nurse_name=None,
    )


def appt_load_options():
    return (
        joinedload(Appointment.patient),
        joinedload(Appointment.doctor),
        joinedload(Appointment.admission).joinedload(Admission.ward),
        joinedload(Appointment.admission).joinedload(Admission.bed),
    )


def generate_uhid(db: Session, hospital_id: UUID) -> str:
    count = db.query(func.count(Patient.id)).filter(Patient.hospital_id == hospital_id).scalar() or 0
    for i in range(1, 100_000):
        uhid = f"P{count + i:04d}"
        if not db.query(Patient.id).filter(Patient.hospital_id == hospital_id, Patient.uhid == uhid).first():
            return uhid
    raise HTTPException(status_code=500, detail="Unable to generate UHID")


def split_name(full: str) -> tuple[str, str]:
    parts = full.strip().split(None, 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


class ListDoctorsAction:
    def __init__(self, db: Session) -> None:
        self.repo = DoctorsRepository(db)

    def execute(self, hospital_id: UUID, specialization: str | None = None) -> list[DoctorSummary]:
        raw_list = self.repo.list_doctors(hospital_id, specialization)
        return [DoctorSummary(**d) for d in raw_list]


class GetHospitalProfileAction:
    def __init__(self, db: Session) -> None:
        self.repo = DoctorsRepository(db)

    def execute(self, hospital_id: UUID) -> HospitalClinicProfile:
        prof = self.repo.get_hospital_profile(hospital_id)
        if not prof:
            raise HTTPException(status_code=404, detail="Hospital not found")
        return HospitalClinicProfile(**prof)


class SearchPatientsAction:
    def __init__(self, db: Session) -> None:
        self.repo = DoctorsRepository(db)

    def execute(self, hospital_id: UUID, q: str | None = None) -> list[DoctorPatientResponse]:
        patients = self.repo.search_patients(hospital_id, q)
        return [to_doctor_patient_response(p) for p in patients]


class CreateDoctorPatientAction:
    def __init__(self, db: Session) -> None:
        self.db = db

    def execute(
        self, hospital_id: UUID, payload: DoctorPatientCreate, actor: dict[str, Any]
    ) -> DoctorPatientResponse:
        existing = (
            self.db.query(Patient)
            .filter(Patient.hospital_id == hospital_id, Patient.mobile == payload.mobile)
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Patient with mobile {payload.mobile} already exists ({existing.uhid})",
            )
        first_name, last_name = split_name(payload.name)
        uhid = generate_uhid(self.db, hospital_id)
        patient = Patient(
            hospital_id=hospital_id,
            uhid=uhid,
            first_name=first_name,
            last_name=last_name,
            name=payload.name.strip(),
            mobile=payload.mobile,
            age=payload.age,
            gender=payload.gender.strip() if payload.gender else None,
            address=payload.address.strip() if payload.address else None,
        )
        self.db.add(patient)
        self.db.flush()
        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="create",
            entity_type="patient",
            entity_id=str(patient.id),
            summary=f"Created patient {patient.uhid} {patient.name}",
        )
        self.db.commit()
        return to_doctor_patient_response(patient)


class UpdateDoctorPatientAction:
    def __init__(self, db: Session) -> None:
        self.db = db

    def execute(
        self,
        hospital_id: UUID,
        patient_id: UUID,
        payload: DoctorPatientUpdate,
        actor: dict[str, Any],
    ) -> DoctorPatientResponse:
        patient = (
            self.db.query(Patient)
            .filter(Patient.id == patient_id, Patient.hospital_id == hospital_id)
            .first()
        )
        if not patient:
            raise HTTPException(status_code=404, detail="Patient not found")

        if payload.name is not None:
            first_name, last_name = split_name(payload.name)
            patient.name = payload.name.strip()
            patient.first_name = first_name
            patient.last_name = last_name
        if payload.mobile is not None:
            patient.mobile = payload.mobile
        if payload.age is not None:
            patient.age = payload.age
        if payload.gender is not None:
            patient.gender = payload.gender.strip() if payload.gender else None
        if payload.address is not None:
            patient.address = payload.address.strip() if payload.address else None

        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="update",
            entity_type="patient",
            entity_id=str(patient.id),
            summary=f"Updated patient {patient.uhid} {patient.name}",
        )
        self.db.commit()
        return to_doctor_patient_response(patient)


class ListDoctorPatientsAction:
    def __init__(self, db: Session) -> None:
        self.repo = DoctorsRepository(db)

    def execute(
        self, hospital_id: UUID, doctor_id: UUID, search: str | None = None
    ) -> list[DoctorPatientResponse]:
        patients = self.repo.list_doctor_patients(hospital_id, doctor_id, search)
        return [to_doctor_patient_response(p) for p in patients]


class GetDoctorPatientHistoryAction:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = DoctorsRepository(db)

    def execute(
        self,
        hospital_id: UUID,
        doctor_id: UUID,
        patient_id: UUID,
        actor: dict[str, Any],
    ) -> PatientHistoryResponse:
        patient = (
            self.db.query(Patient)
            .filter(Patient.id == patient_id, Patient.hospital_id == hospital_id)
            .first()
        )
        if not patient:
            raise HTTPException(status_code=404, detail="Patient not found")

        linked = (
            self.db.query(Appointment.id)
            .filter(Appointment.doctor_id == doctor_id, Appointment.patient_id == patient_id)
            .first()
            or self.db.query(Prescription.id)
            .filter(Prescription.doctor_id == doctor_id, Prescription.patient_id == patient_id)
            .first()
            or self.db.query(MedicalRecord.id)
            .filter(MedicalRecord.doctor_id == doctor_id, MedicalRecord.patient_id == patient_id)
            .first()
        )
        if not linked and actor.get("role") != "hospital_admin":
            raise HTTPException(status_code=404, detail="Patient not found for this doctor")

        appointments = (
            self.db.query(Appointment)
            .options(*appt_load_options())
            .filter(Appointment.doctor_id == doctor_id, Appointment.patient_id == patient_id)
            .order_by(Appointment.appointment_date.desc(), Appointment.appointment_time.desc())
            .all()
        )
        prescriptions = (
            self.db.query(Prescription)
            .options(joinedload(Prescription.patient), joinedload(Prescription.doctor))
            .filter(Prescription.doctor_id == doctor_id, Prescription.patient_id == patient_id)
            .order_by(Prescription.created_at.desc())
            .all()
        )
        records = (
            self.db.query(MedicalRecord)
            .options(joinedload(MedicalRecord.patient), joinedload(MedicalRecord.doctor))
            .filter(MedicalRecord.doctor_id == doctor_id, MedicalRecord.patient_id == patient_id)
            .order_by(MedicalRecord.created_at.desc())
            .all()
        )
        vitals = (
            self.db.query(VitalReading)
            .filter(VitalReading.hospital_id == hospital_id, VitalReading.patient_id == patient_id)
            .order_by(VitalReading.recorded_at.desc())
            .all()
        )
        ipd_forms = (
            self.db.query(IpdFormSubmission)
            .filter(IpdFormSubmission.hospital_id == hospital_id, IpdFormSubmission.patient_id == patient_id)
            .order_by(IpdFormSubmission.updated_at.desc())
            .all()
        )

        last_appt = appointments[0] if appointments else None
        last_rx = prescriptions[0] if prescriptions else None

        doctor = self.repo.get_doctor(hospital_id, doctor_id)
        financial_summary = None
        if bool(getattr(doctor, "show_financial_details", True)):
            try:
                from hms_migration.modules.billing.services.billing_service import (
                    build_ledger_entries,
                    patient_ledger_totals,
                )
                fin = patient_ledger_totals(self.db, hospital_id, patient_id)
                recent = build_ledger_entries(self.db, hospital_id, patient_id)[:6]
                financial_summary = {**fin, "recent_entries": recent}
            except Exception:
                pass

        return PatientHistoryResponse(
            patient=to_doctor_patient_response(
                patient,
                last_visit=last_appt.appointment_date if last_appt else None,
                last_diagnosis=last_rx.diagnosis if last_rx else None,
            ),
            appointments=[to_doctor_appointment_response(a) for a in appointments],
            prescriptions=[
                to_prescription_response(
                    p, next((a.status for a in appointments if a.id == p.appointment_id), None)
                )
                for p in prescriptions
            ],
            medical_records=[to_record_response(r) for r in records],
            vitals=[VitalReadingResponse.model_validate(v) for v in vitals],
            ipd_forms=[
                IpdFormHistoryItem(
                    id=f.id,
                    admission_id=f.admission_id,
                    form_id=f.form_id,
                    form_title=f.form_title,
                    status=f.status,
                    has_html_snapshot=bool(f.html_snapshot),
                    filled_by_name=f.filled_by_name or "",
                    updated_at=f.updated_at,
                )
                for f in ipd_forms
            ],
            financial_summary=financial_summary,
        )




__all__ = [
    "to_doctor_patient_response",
    "ListDoctorsAction",
    "GetHospitalProfileAction",
    "SearchPatientsAction",
    "UpdateDoctorPatientAction",
    "CreateDoctorPatientAction",
    "ListDoctorPatientsAction",
    "GetDoctorPatientHistoryAction",
    "to_doctor_appointment_response",
    "CreateDoctorAppointmentAction",
    "ListDoctorAppointmentsAction",
    "GetDoctorCalendarAction",
    "UpdateDoctorAppointmentAction",
    "TransferAppointmentToInpatientAction",
    "GetDoctorScheduleContextAction",
]
