"""
Actions for Feature 6: Intensivist ICU Patient Board.

Conforms to UltrionTech-Backend-Template modules/critical_care/actions/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from hms_migration.modules.critical_care.contracts.critical_care_contracts import (
    IcuBoardPatientResponse,
    IcuProfileUpdate,
)
from hms_migration.modules.critical_care.db.critical_care_repository import CriticalCareRepository
from hms_migration.modules.critical_care.entities.critical_care_entities import IcuPatientProfile
from hms_migration.shared.audit.service import write_audit_log


class GetIcuBoardAction:
    """Action to construct live intensivist critical care patient board."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = CriticalCareRepository(db, hospital_id)

    def execute(self) -> list[IcuBoardPatientResponse]:
        admissions = self.repo.get_active_icu_admissions()
        now = datetime.now(timezone.utc)
        results: list[IcuBoardPatientResponse] = []

        for adm in admissions:
            profile = self.repo.get_icu_profile(adm.id)
            latest_flowsheet = self.repo.get_latest_flowsheet(adm.id)

            # Calculate Length of Stay (LOS) in days
            los_days = 0
            if adm.admitted_at:
                adm_time = adm.admitted_at if adm.admitted_at.tzinfo else adm.admitted_at.replace(tzinfo=timezone.utc)
                los_days = max(0, int((now - adm_time).total_seconds() // 86400))

            results.append(
                IcuBoardPatientResponse(
                    admission_id=adm.id,
                    patient_id=adm.patient_id,
                    patient_name=adm.patient.name if adm.patient else "Unknown",
                    patient_uhid=adm.patient.uhid if adm.patient else "",
                    patient_age=adm.patient.age if adm.patient else None,
                    patient_gender=adm.patient.gender if adm.patient else None,
                    ward_name=adm.ward.name if adm.ward else "ICU",
                    bed_code=adm.bed.bed_code if adm.bed else "",
                    attending_doctor_name=adm.doctor.name if adm.doctor else None,
                    length_of_stay_days=los_days,
                    ventilator_mode=profile.ventilator_mode if profile else None,
                    peep=profile.peep if profile else None,
                    fio2_percent=profile.fio2_percent if profile else None,
                    invasive_line_days=profile.invasive_line_days if profile else None,
                    inotrope_support=profile.inotrope_support if profile else False,
                    inotrope_details=profile.inotrope_details if profile else None,
                    gcs_score=profile.gcs_score if profile else None,
                    latest_heart_rate=latest_flowsheet.heart_rate if latest_flowsheet else None,
                    latest_spo2=latest_flowsheet.spo2 if latest_flowsheet else None,
                    latest_map=latest_flowsheet.mean_arterial_pressure if latest_flowsheet else None,
                    latest_hourly_balance_ml=latest_flowsheet.hourly_balance_ml if latest_flowsheet else None,
                )
            )

        return results


class UpdateIcuProfileAction:
    """Update or initialize critical care indicators for an admitted ICU patient."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = CriticalCareRepository(db, hospital_id)

    def execute(self, admission_id: UUID, payload: IcuProfileUpdate, actor: dict[str, Any]) -> IcuPatientProfile:
        from hms_migration.modules.inpatient.entities.admission import Admission
        adm = (
            self.db.query(Admission)
            .filter(Admission.id == admission_id, Admission.hospital_id == self.hospital_id)
            .first()
        )
        if not adm:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admission not found")

        profile = self.repo.get_icu_profile(admission_id)
        if not profile:
            profile = IcuPatientProfile(
                hospital_id=self.hospital_id,
                admission_id=admission_id,
                patient_id=adm.patient_id,
                ventilator_mode=payload.ventilator_mode,
                peep=payload.peep,
                fio2_percent=payload.fio2_percent,
                invasive_line_days=payload.invasive_line_days or {},
                inotrope_support=payload.inotrope_support,
                inotrope_details=payload.inotrope_details,
                gcs_score=payload.gcs_score,
                care_indicators=payload.care_indicators or {},
            )
            self.repo.add(profile)
        else:
            if payload.ventilator_mode is not None:
                profile.ventilator_mode = payload.ventilator_mode
            if payload.peep is not None:
                profile.peep = payload.peep
            if payload.fio2_percent is not None:
                profile.fio2_percent = payload.fio2_percent
            if payload.invasive_line_days is not None:
                profile.invasive_line_days = payload.invasive_line_days
            profile.inotrope_support = payload.inotrope_support
            if payload.inotrope_details is not None:
                profile.inotrope_details = payload.inotrope_details
            if payload.gcs_score is not None:
                profile.gcs_score = payload.gcs_score
            if payload.care_indicators is not None:
                profile.care_indicators = payload.care_indicators

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="update_icu_profile",
            entity_type="icu_patient_profile",
            entity_id=profile.id,
            summary=f"Updated ICU profile for admission {admission_id}",
        )
        self.repo.commit()
        self.repo.refresh(profile)
        return profile
