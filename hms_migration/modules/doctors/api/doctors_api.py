"""
FastAPI router for Doctors, Doctor Schedules, Patient Consultations, and Clinical Records.

Conforms to UltrionTech-Backend-Template modules/doctors/api/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.appointments.entities.enums import AppointmentStatus
from hms_migration.modules.clinical_records.actions.clinical_actions import (
    CreateMedicalRecordAction,
    CreatePrescriptionAction,
    GetRecordFileAction,
    ListMedicalRecordsAction,
    ListPrescriptionsAction,
    StreamPrescriptionPdfAction,
    UpdatePrescriptionAction,
)
from hms_migration.modules.clinical_records.contracts.clinical_contracts import (
    MedicalRecordCreate,
    MedicalRecordResponse,
    PrescriptionCreate,
    PrescriptionResponse,
    PrescriptionUpdate,
)
from hms_migration.modules.doctors.actions.doctor_actions import (
    CreateDoctorAppointmentAction,
    CreateDoctorPatientAction,
    GetDoctorCalendarAction,
    GetDoctorPatientHistoryAction,
    GetDoctorScheduleContextAction,
    GetHospitalProfileAction,
    ListDoctorAppointmentsAction,
    ListDoctorPatientsAction,
    ListDoctorsAction,
    SearchPatientsAction,
    TransferAppointmentToInpatientAction,
    UpdateDoctorAppointmentAction,
    UpdateDoctorPatientAction,
)
from hms_migration.modules.doctors.actions.doctor_leave_actions import (
    CreateDoctorLeaveAction,
    CreateDoctorLeaveRangeAction,
    DeleteDoctorLeaveAction,
    ListDoctorLeavesAction,
)
from hms_migration.modules.doctors.contracts.doctor_contracts import (
    DoctorAppointmentCreate,
    DoctorAppointmentResponse,
    DoctorAppointmentUpdate,
    DoctorLeaveCreate,
    DoctorLeaveRangeCreate,
    DoctorLeaveResponse,
    DoctorPatientCreate,
    DoctorPatientResponse,
    DoctorPatientUpdate,
    DoctorScheduleContext,
    DoctorSummary,
    HospitalClinicProfile,
    PatientHistoryResponse,
    TransferToInpatientRequest,
)
from hms_migration.modules.doctors.permissions.doctor_permissions import resolve_doctor_id
from hms_migration.modules.inpatient.actions.admission_actions import to_admission_detail
from hms_migration.modules.inpatient.contracts.inpatient_contracts import AdmissionDetail
from hms_migration.modules.inpatient.db.admissions_repository import AdmissionsRepository
from hms_migration.shared.auth.dependencies import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/doctors", tags=["doctors"])


# ── Doctor Profile / Directory ───────────────────────────────────────────────

@router.get("", response_model=list[DoctorSummary])
def list_doctors(
    specialization: str | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return ListDoctorsAction(db).execute(hospital_id, specialization)


@router.get("/hospital-profile", response_model=HospitalClinicProfile)
def hospital_profile(
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return GetHospitalProfileAction(db).execute(hospital_id)


# ── Doctor-Patient Management ───────────────────────────────────────────────

@router.get("/patients/search", response_model=list[DoctorPatientResponse])
def search_patients(
    q: str | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return SearchPatientsAction(db).execute(hospital_id, q)


@router.put("/patients/{patient_id}", response_model=DoctorPatientResponse)
def update_patient(
    patient_id: UUID,
    payload: DoctorPatientUpdate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return UpdateDoctorPatientAction(db).execute(hospital_id, patient_id, payload, user)


@router.post("/patients", response_model=DoctorPatientResponse, status_code=status.HTTP_201_CREATED)
def create_patient(
    payload: DoctorPatientCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return CreateDoctorPatientAction(db).execute(hospital_id, payload, user)


@router.get("/{doctor_id}/patients", response_model=list[DoctorPatientResponse])
def list_doctor_patients(
    doctor_id: UUID,
    search: str | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = resolve_doctor_id(user, doctor_id, hospital_id, db)
    return ListDoctorPatientsAction(db).execute(hospital_id, resolved, search)


@router.get("/{doctor_id}/patients/{patient_id}", response_model=PatientHistoryResponse)
def doctor_patient_history(
    doctor_id: UUID,
    patient_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = resolve_doctor_id(user, doctor_id, hospital_id, db)
    return GetDoctorPatientHistoryAction(db).execute(hospital_id, resolved, patient_id, user)


# ── Doctor Appointments & Calendar ──────────────────────────────────────────

@router.post(
    "/{doctor_id}/appointments",
    response_model=DoctorAppointmentResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_doctor_appointment(
    doctor_id: UUID,
    payload: DoctorAppointmentCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = resolve_doctor_id(user, doctor_id, hospital_id, db)
    return CreateDoctorAppointmentAction(db).execute(hospital_id, resolved, payload, user)


@router.get("/{doctor_id}/appointments", response_model=list[DoctorAppointmentResponse])
def list_doctor_appointments(
    doctor_id: UUID,
    on_date: date | None = Query(default=None, alias="date"),
    status_filter: AppointmentStatus | None = Query(default=None, alias="status"),
    visit_type: str | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = resolve_doctor_id(user, doctor_id, hospital_id, db)
    return ListDoctorAppointmentsAction(db).execute(
        hospital_id, resolved, on_date, status_filter, visit_type
    )


@router.get("/{doctor_id}/calendar", response_model=list[DoctorAppointmentResponse])
def doctor_calendar(
    doctor_id: UUID,
    week_start: date = Query(...),
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = resolve_doctor_id(user, doctor_id, hospital_id, db)
    return GetDoctorCalendarAction(db).execute(hospital_id, resolved, week_start)


@router.get("/{doctor_id}/admissions/active", response_model=list[AdmissionDetail])
def list_active_admissions_for_doctor(
    doctor_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = resolve_doctor_id(user, doctor_id, hospital_id, db)
    rows = AdmissionsRepository(db).list_active_admissions(hospital_id, doctor_id=resolved)
    return [to_admission_detail(a) for a in rows]


@router.get("/{doctor_id}/schedule-context", response_model=DoctorScheduleContext)
def get_schedule_context(
    doctor_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = resolve_doctor_id(user, doctor_id, hospital_id, db)
    return GetDoctorScheduleContextAction(db).execute(hospital_id, resolved)


# ── Doctor Leaves ───────────────────────────────────────────────────────────

@router.get("/{doctor_id}/leaves", response_model=list[DoctorLeaveResponse])
def list_doctor_leaves(
    doctor_id: UUID,
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = resolve_doctor_id(user, doctor_id, hospital_id, db)
    return ListDoctorLeavesAction(db).execute(hospital_id, resolved, start_date, end_date)


@router.post(
    "/{doctor_id}/leaves",
    response_model=DoctorLeaveResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_doctor_leave(
    doctor_id: UUID,
    payload: DoctorLeaveCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = resolve_doctor_id(user, doctor_id, hospital_id, db)
    return CreateDoctorLeaveAction(db).execute(hospital_id, resolved, payload, user)


@router.post(
    "/{doctor_id}/leaves/range",
    response_model=list[DoctorLeaveResponse],
    status_code=status.HTTP_201_CREATED,
)
def create_doctor_leave_range(
    doctor_id: UUID,
    payload: DoctorLeaveRangeCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = resolve_doctor_id(user, doctor_id, hospital_id, db)
    return CreateDoctorLeaveRangeAction(db).execute(hospital_id, resolved, payload, user)


@router.delete("/{doctor_id}/leaves/{leave_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_doctor_leave(
    doctor_id: UUID,
    leave_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = resolve_doctor_id(user, doctor_id, hospital_id, db)
    DeleteDoctorLeaveAction(db).execute(hospital_id, resolved, leave_id, user)


# ── Appointment Modifications & Transfer ───────────────────────────────────

@router.put("/{doctor_id}/appointments/{appointment_id}", response_model=DoctorAppointmentResponse)
def update_doctor_appointment(
    doctor_id: UUID,
    appointment_id: UUID,
    payload: DoctorAppointmentUpdate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = resolve_doctor_id(user, doctor_id, hospital_id, db)
    return UpdateDoctorAppointmentAction(db).execute(
        hospital_id, resolved, appointment_id, payload, user
    )


@router.post(
    "/{doctor_id}/appointments/{appointment_id}/transfer-to-inpatient",
    response_model=DoctorAppointmentResponse,
)
def transfer_appointment_to_inpatient(
    doctor_id: UUID,
    appointment_id: UUID,
    payload: TransferToInpatientRequest,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = resolve_doctor_id(user, doctor_id, hospital_id, db)
    return TransferAppointmentToInpatientAction(db).execute(
        hospital_id, resolved, appointment_id, payload, user
    )


# ── Prescriptions ──────────────────────────────────────────────────────────

@router.post(
    "/{doctor_id}/prescriptions",
    response_model=PrescriptionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_prescription(
    doctor_id: UUID,
    payload: PrescriptionCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = resolve_doctor_id(user, doctor_id, hospital_id, db)
    return CreatePrescriptionAction(db).execute(hospital_id, resolved, payload, user)


@router.put("/{doctor_id}/prescriptions/{prescription_id}", response_model=PrescriptionResponse)
def update_prescription(
    doctor_id: UUID,
    prescription_id: UUID,
    payload: PrescriptionUpdate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = resolve_doctor_id(user, doctor_id, hospital_id, db)
    return UpdatePrescriptionAction(db).execute(
        hospital_id, resolved, prescription_id, payload, user
    )


@router.get("/{doctor_id}/prescriptions", response_model=list[PrescriptionResponse])
def list_prescriptions(
    doctor_id: UUID,
    patient_id: UUID | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = resolve_doctor_id(user, doctor_id, hospital_id, db)
    return ListPrescriptionsAction(db).execute(hospital_id, resolved, patient_id)


@router.get("/{doctor_id}/prescriptions/{prescription_id}/pdf")
def prescription_pdf(
    doctor_id: UUID,
    prescription_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = resolve_doctor_id(user, doctor_id, hospital_id, db)
    return StreamPrescriptionPdfAction(db).execute(hospital_id, resolved, prescription_id)


# ── Medical Records ────────────────────────────────────────────────────────

@router.post(
    "/{doctor_id}/records",
    response_model=MedicalRecordResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_medical_record(
    doctor_id: UUID,
    payload: MedicalRecordCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = resolve_doctor_id(user, doctor_id, hospital_id, db)
    return CreateMedicalRecordAction(db).execute(hospital_id, resolved, payload, user)


@router.get("/{doctor_id}/records", response_model=list[MedicalRecordResponse])
def list_medical_records(
    doctor_id: UUID,
    patient_id: UUID | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = resolve_doctor_id(user, doctor_id, hospital_id, db)
    return ListMedicalRecordsAction(db).execute(hospital_id, resolved, patient_id)


@router.get("/{doctor_id}/records/{record_id}/file")
def get_record_file(
    doctor_id: UUID,
    record_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = resolve_doctor_id(user, doctor_id, hospital_id, db)
    return GetRecordFileAction(db).execute(hospital_id, resolved, record_id)
