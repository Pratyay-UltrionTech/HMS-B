"""
HTTP API router and route handlers for the Patient domain.

Conforms to UltrionTech-Backend-Template modules/patients/api/ specification.
Thin controllers that validate input parameters, resolve hospital/user dependencies,
delegate to business actions, and return typed responses.
Consumes shared foundation: shared auth, shared exceptions, and postgres infrastructure.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from infrastructure.postgres.session import (
    get_transitional_sync_session as get_db,
)
from modules.patients.actions.get_patient_profile_action import (
    GetPatientProfileAction,
)
from modules.patients.actions.list_patients_action import (
    ListPatientsAction,
)
from modules.patients.actions.register_patient_action import (
    RegisterPatientAction,
)
from modules.patients.actions.update_patient_action import (
    UpdatePatientAction,
)
from modules.patients.contracts.patients_contracts import (
    PatientDirectoryItem,
    PatientProfile,
    PatientRegister,
    PatientRegisterUpdate,
    PatientStatus,
)
from modules.patients.db.patients_repository import PatientRepository
from shared.auth import get_hospital_context, require_hospital_user, require_permission

router = APIRouter(prefix="/registration/patients", tags=["patients"])


@router.post(
    "",
    response_model=PatientDirectoryItem,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new patient",

    dependencies=[Depends(require_permission("registration", "edit"))])
def register_patient(
    payload: PatientRegister,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> PatientDirectoryItem:
    """Register a new patient within the current hospital tenant."""
    repo = PatientRepository(db=db, hospital_id=hospital_id)
    return RegisterPatientAction(repo).execute(payload=payload, user=user)


@router.get(
    "",
    response_model=list[PatientDirectoryItem],
    summary="List or search patients",
)
def list_patients(
    search: str | None = Query(default=None),
    status_filter: PatientStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[PatientDirectoryItem]:
    """Search and list patients within the current hospital tenant."""
    repo = PatientRepository(db=db, hospital_id=hospital_id)
    return ListPatientsAction(repo).execute(
        search=search, status_filter=status_filter, limit=limit, offset=offset
    )


@router.get(
    "/{patient_id}",
    response_model=PatientProfile,
    summary="Get patient 360 profile",
)
def get_patient_profile(
    patient_id: UUID,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> PatientProfile:
    """Retrieve full clinical and demographic profile for a patient."""
    repo = PatientRepository(db=db, hospital_id=hospital_id)
    return GetPatientProfileAction(repo).execute(patient_id=patient_id)


@router.put(
    "/{patient_id}",
    response_model=PatientDirectoryItem,
    summary="Update patient record",

    dependencies=[Depends(require_permission("registration", "edit"))])
def update_patient(
    patient_id: UUID,
    payload: PatientRegisterUpdate,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> PatientDirectoryItem:
    """Update patient demographic and administrative details."""
    repo = PatientRepository(db=db, hospital_id=hospital_id)
    return UpdatePatientAction(repo).execute(
        patient_id=patient_id,
        payload=payload,
        user=user,
    )


@router.get(
    "/{patient_id}/label",
    summary="Get printable patient identification label",
)
def print_patient_label(
    patient_id: UUID,
    encounter_id: str | None = Query(default=None),
    format_type: str = Query(default="standard", alias="format"),
    download: bool = Query(default=False),
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Generate printable HTML patient identification label / wristband."""
    from io import BytesIO
    from fastapi.responses import StreamingResponse
    from modules.patients.services.patient_label_service import render_patient_label_html
    from modules.tenancy.entities.hospital import Hospital
    from shared.audit.service import write_audit_log

    repo = PatientRepository(db=db, hospital_id=hospital_id)
    patient = repo.get_by_id(patient_id)
    if not patient:
        from fastapi import HTTPException
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

    hospital = db.query(Hospital).filter(Hospital.id == hospital_id).first()

    appointment_ctx = None
    if encounter_id:
        from fastapi import HTTPException
        from modules.appointments.entities.appointment import Appointment
        from modules.doctors.entities.doctor import HospitalUser
        from modules.masters.entities.organization_entities import Department
        from modules.patients.services.patient_label_service import LabelAppointmentContext

        appt = (
            db.query(Appointment)
            .filter(
                Appointment.hospital_id == hospital_id,
                Appointment.patient_id == patient_id,
                Appointment.op_id == encounter_id,
            )
            .first()
        )
        if not appt:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Appointment not found for this patient",
            )

        doctor_name: str | None = None
        if appt.doctor_id:
            doctor = db.query(HospitalUser).filter(HospitalUser.id == appt.doctor_id).first()
            doctor_name = doctor.name if doctor else None

        department_name: str | None = None
        if appt.department_id:
            dept = db.query(Department).filter(Department.id == appt.department_id).first()
            department_name = dept.name if dept else None

        appointment_ctx = LabelAppointmentContext(
            op_id=appt.op_id or encounter_id,
            appointment_date=appt.appointment_date,
            appointment_time=appt.appointment_time,
            doctor_name=doctor_name,
            department_name=department_name,
        )

    html = render_patient_label_html(
        patient=patient,
        hospital=hospital,
        encounter_id=encounter_id,
        format_type=format_type,
        auto_print=not download,
        appointment=appointment_ctx,
    )
    write_audit_log(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="print_label",
        entity_type="patient",
        entity_id=patient.id,
        summary=f"Printed patient label for {patient.name} ({patient.uhid})",
    )
    db.commit()

    disposition = "attachment" if download else "inline"
    filename = f"label_{patient.uhid}.html"
    return StreamingResponse(
        BytesIO(html.encode("utf-8")),
        media_type="text/html",
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )

