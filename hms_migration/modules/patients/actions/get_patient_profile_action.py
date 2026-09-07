"""
Action to retrieve a comprehensive 360-degree patient profile.

Conforms to UltrionTech-Backend-Template modules/patients/actions/ specification.
Handles:
- Tenant-scoped patient lookup (HTTP 404 if not found)
- Cross-domain aggregation of visits, prescriptions, reports, admissions, and bills
- Profile response serialization
"""

from uuid import UUID

from fastapi import HTTPException, status

from hms_migration.modules.patients.contracts.patients_contracts import (
    AdmissionSummary,
    PatientProfile,
    PrescriptionSummary,
    ReportSummary,
    VisitSummary,
)
from hms_migration.modules.patients.db.patients_repository import PatientRepository


class GetPatientProfileAction:
    """Action orchestrating patient profile retrieval."""

    def __init__(self, repo: PatientRepository) -> None:
        self.repo = repo

    def execute(self, patient_id: UUID) -> PatientProfile:
        """Fetch patient demographics and aggregate cross-domain clinical/financial history."""
        patient = self.repo.get_by_id(patient_id)
        if not patient:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Patient not found",
            )

        visits = self.repo.profile_reader.get_visits(patient_id)
        prescriptions = self.repo.profile_reader.get_prescriptions(patient_id)
        reports = self.repo.profile_reader.get_medical_reports(patient_id)
        admissions = self.repo.profile_reader.get_admissions(patient_id)
        bills = self.repo.profile_reader.get_ledger_bills(patient_id, limit=50)
        financial_summary = self.repo.profile_reader.get_financial_summary(patient_id)

        return PatientProfile(
            id=patient.id,
            uhid=patient.uhid,
            first_name=patient.first_name or "",
            last_name=patient.last_name or "",
            name=patient.name,
            mobile=patient.mobile,
            email=patient.email,
            gender=patient.gender,
            age=patient.age,
            date_of_birth=patient.date_of_birth,
            address=patient.address,
            emergency_contact=patient.emergency_contact,
            emergency_contact_name=getattr(patient, "emergency_contact_name", None),
            emergency_contact_relation=getattr(patient, "emergency_contact_relation", None),
            blood_group=patient.blood_group,
            has_insurance=bool(getattr(patient, "has_insurance", False)),
            insurance_provider=getattr(patient, "insurance_provider", None),
            insurance_details=getattr(patient, "insurance_details", None),
            status=patient.status,
            created_at=patient.created_at,
            visits=[
                VisitSummary(
                    id=v.id,
                    appointment_date=v.appointment_date,
                    appointment_time=(
                        v.appointment_time.strftime("%H:%M")
                        if hasattr(v.appointment_time, "strftime")
                        else str(v.appointment_time)
                    ),
                    doctor_name=v.doctor.name if v.doctor else None,
                    purpose=v.purpose,
                    visit_type=getattr(v, "visit_type", None) or "OPD",
                    status=v.status.value if hasattr(v.status, "value") else str(v.status),
                    op_id=getattr(v, "op_id", None),
                )
                for v in visits
            ],
            prescriptions=[
                PrescriptionSummary(
                    id=rx.id,
                    diagnosis=rx.diagnosis,
                    medicines=rx.medicines,
                    doctor_name=rx.doctor.name if rx.doctor else None,
                    created_at=rx.created_at,
                )
                for rx in prescriptions
            ],
            medical_reports=[
                ReportSummary(
                    id=r.id,
                    report_type=r.report_type,
                    title=r.title,
                    notes=r.notes,
                    created_at=r.created_at,
                    doctor_name=r.doctor.name if r.doctor else None,
                )
                for r in reports
            ],
            admissions=[
                AdmissionSummary(
                    id=a.id,
                    ward_id=a.ward_id,
                    room_id=a.room_id,
                    bed_id=a.bed_id,
                    ward_name=a.ward.name if a.ward else None,
                    room_code=a.room.room_code if a.room else None,
                    bed_code=a.bed.bed_code if a.bed else None,
                    doctor_name=a.doctor.name if a.doctor else None,
                    status=a.status,
                    admitted_at=a.admitted_at,
                    discharged_at=a.discharged_at,
                    notes=a.notes,
                    ip_id=getattr(a, "ip_id", None),
                )
                for a in admissions
            ],
            bills=bills,
            financial_summary=financial_summary,
        )
