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

from modules.critical_care.contracts.critical_care_contracts import (
    IcuAdmitRequest,
    IcuBoardPatientResponse,
    IcuProfileUpdate,
    IcuStepDownRequest,
    IcuTransferRequest,
)
from modules.critical_care.db.critical_care_repository import CriticalCareRepository
from modules.critical_care.entities.critical_care_entities import IcuPatientProfile
from shared.audit.service import write_audit_log


class GetIcuBoardAction:
    """Action to construct live intensivist critical care patient board."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = CriticalCareRepository(db, hospital_id)

    def _build_patient_response(
        self, adm: Any, profile: IcuPatientProfile | None, latest_flowsheet: Any, now: datetime
    ) -> IcuBoardPatientResponse:
        los_days = 0
        if adm.admitted_at:
            adm_time = adm.admitted_at if adm.admitted_at.tzinfo else adm.admitted_at.replace(tzinfo=timezone.utc)
            los_days = max(0, int((now - adm_time).total_seconds() // 86400))

        indicators = profile.care_indicators if profile and profile.care_indicators else {}
        diag = indicators.get("diagnosis") or getattr(adm, "notes", None) or "Critical Care Admission"

        return IcuBoardPatientResponse(
            admission_id=adm.id,
            patient_id=adm.patient_id,
            patient_name=adm.patient.name if adm.patient else "Unknown",
            patient_uhid=adm.patient.uhid if adm.patient else "",
            patient_age=adm.patient.age if adm.patient else None,
            patient_gender=adm.patient.gender if adm.patient else None,
            ward_name=adm.ward.name if adm.ward else "ICU",
            bed_code=adm.bed.bed_code if adm.bed else "",
            room_code=adm.room.room_code if getattr(adm, "room", None) else None,
            attending_doctor_name=adm.doctor.name if adm.doctor else None,
            length_of_stay_days=los_days,
            ventilator_mode=profile.ventilator_mode if profile else None,
            peep=profile.peep if profile else None,
            fio2_percent=profile.fio2_percent if profile else None,
            invasive_line_days=profile.invasive_line_days if profile else None,
            inotrope_support=profile.inotrope_support if profile else False,
            inotrope_details=profile.inotrope_details if profile else None,
            gcs_score=profile.gcs_score if profile else None,
            sofa_score=profile.sofa_score if profile else None,
            care_indicators=indicators if profile else None,
            diagnosis=diag,
            latest_heart_rate=latest_flowsheet.heart_rate if latest_flowsheet else None,
            latest_spo2=latest_flowsheet.spo2 if latest_flowsheet else None,
            latest_map=latest_flowsheet.mean_arterial_pressure if latest_flowsheet else None,
            latest_hourly_balance_ml=latest_flowsheet.hourly_balance_ml if latest_flowsheet else None,
        )

    def execute(self) -> list[IcuBoardPatientResponse]:
        admissions = self.repo.get_active_icu_admissions()
        now = datetime.now(timezone.utc)
        results: list[IcuBoardPatientResponse] = []

        admission_ids = [adm.id for adm in admissions]
        profile_map = self.repo.get_icu_profiles_for_admissions(admission_ids)
        flowsheet_map = self.repo.get_latest_flowsheets_for_admissions(admission_ids)

        for adm in admissions:
            profile = profile_map.get(adm.id)
            latest_flowsheet = flowsheet_map.get(adm.id)
            results.append(self._build_patient_response(adm, profile, latest_flowsheet, now))

        return results

    def execute_for_admission(self, admission_id: UUID) -> IcuBoardPatientResponse:
        from modules.inpatient.entities.admission import Admission
        from sqlalchemy.orm import joinedload

        adm = (
            self.db.query(Admission)
            .options(
                joinedload(Admission.patient),
                joinedload(Admission.ward),
                joinedload(Admission.room),
                joinedload(Admission.bed),
                joinedload(Admission.doctor),
            )
            .filter(Admission.id == admission_id, Admission.hospital_id == self.hospital_id)
            .first()
        )
        if not adm:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admission not found")

        profile = self.repo.get_icu_profile(admission_id)
        latest_flowsheet = self.repo.get_latest_flowsheets_for_admissions([admission_id]).get(admission_id)
        now = datetime.now(timezone.utc)
        return self._build_patient_response(adm, profile, latest_flowsheet, now)


class UpdateIcuProfileAction:
    """Update or initialize critical care indicators for an admitted ICU patient."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = CriticalCareRepository(db, hospital_id)

    def execute(self, admission_id: UUID, payload: IcuProfileUpdate, actor: dict[str, Any]) -> IcuPatientProfile:
        from modules.inpatient.entities.admission import Admission
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


class AdmitToIcuAction:
    """Directly admit a registered patient into an ICU bed."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = CriticalCareRepository(db, hospital_id)

    def execute(self, payload: IcuAdmitRequest, actor: dict[str, Any]) -> IcuBoardPatientResponse:
        from modules.patients.entities.patient import Patient
        from modules.beds.entities.bed import Bed
        from modules.inpatient.entities.admission import Admission, AdmissionStatus, BedStaySegment
        from modules.inpatient.services.inpatient_billing_service import next_ip_encounter_id

        patient = (
            self.db.query(Patient)
            .filter(Patient.id == payload.patient_id, Patient.hospital_id == self.hospital_id)
            .first()
        )
        if not patient:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

        active = (
            self.db.query(Admission)
            .filter(
                Admission.patient_id == payload.patient_id,
                Admission.hospital_id == self.hospital_id,
                # Invariant 1: requested/admitted/discharge_requested all block.
                Admission.status.in_(
                    [
                        AdmissionStatus.requested,
                        AdmissionStatus.admitted,
                        AdmissionStatus.discharge_requested,
                    ]
                ),
            )
            .first()
        )
        if active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Patient is already admitted. Please use the Transfer to ICU action instead.",
            )

        bed = (
            self.db.query(Bed)
            .filter(Bed.id == payload.bed_id, Bed.hospital_id == self.hospital_id)
            .with_for_update()
            .first()
        )
        if not bed:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bed not found")
        if bed.is_occupied:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Selected bed is already occupied")

        now = datetime.now(timezone.utc)
        ip_id = next_ip_encounter_id(self.db, self.hospital_id)
        # Converged creation: snapshot + occupancy + appointment reconcile via
        # the canonical repository (spec §18). ICU profile follows below.
        from modules.inpatient.db.admissions_repository import AdmissionsRepository

        admission = AdmissionsRepository(self.db).create_admission(
            hospital_id=self.hospital_id,
            patient=patient,
            bed=bed,
            ward_id=bed.ward_id,
            room_id=bed.room_id,
            doctor_id=payload.doctor_id,
            ip_id=ip_id,
            notes=payload.diagnosis or payload.notes or "Direct ICU Admission",
            admitted_at=now,
        )

        segment = BedStaySegment(
            hospital_id=self.hospital_id,
            admission_id=admission.id,
            ward_id=bed.ward_id,
            room_id=bed.room_id,
            bed_id=bed.id,
            rate_per_day=float(getattr(bed.ward, "bed_charge_per_day", 0) or 0) if bed.ward else 0.0,
            started_at=now,
            ended_at=None,
        )
        self.db.add(segment)

        # Initial ICU Profile
        profile = IcuPatientProfile(
            hospital_id=self.hospital_id,
            admission_id=admission.id,
            patient_id=patient.id,
            ventilator_mode=payload.ventilator_mode,
            peep=payload.peep,
            fio2_percent=payload.fio2_percent,
            invasive_line_days=payload.invasive_lines or {},
            inotrope_support=payload.inotrope_support,
            inotrope_details=payload.inotrope_details,
            gcs_score=payload.gcs_score,
            care_indicators={
                "acuity": payload.acuity or "high",
                "diagnosis": payload.diagnosis,
                "notes": payload.notes,
            },
        )
        self.db.add(profile)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="admit_icu",
            entity_type="admission",
            entity_id=admission.id,
            summary=f"Direct ICU Admission for {patient.name} to Bed {bed.bed_code}",
        )
        self.db.commit()
        return GetIcuBoardAction(self.db, self.hospital_id).execute_for_admission(admission.id)


class TransferToIcuAction:
    """Transfer an active inpatient (from ER, Ward, or OT) into an ICU bed."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = CriticalCareRepository(db, hospital_id)

    def execute(self, payload: IcuTransferRequest, actor: dict[str, Any]) -> IcuBoardPatientResponse:
        from modules.beds.entities.bed import Bed
        from modules.inpatient.entities.admission import Admission, AdmissionStatus, BedStaySegment

        adm = (
            self.db.query(Admission)
            .filter(
                Admission.id == payload.admission_id,
                Admission.hospital_id == self.hospital_id,
                # Census-eligible episodes may transfer (spec §14).
                Admission.status.in_(
                    [AdmissionStatus.admitted, AdmissionStatus.discharge_requested]
                ),
            )
            .first()
        )
        if not adm:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Active admission not found")

        bed = (
            self.db.query(Bed)
            .filter(Bed.id == payload.to_bed_id, Bed.hospital_id == self.hospital_id)
            .with_for_update()
            .first()
        )
        if not bed:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target ICU bed not found")
        if bed.is_occupied and bed.id != adm.bed_id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Target bed is already occupied")

        old_bed = adm.bed
        if old_bed and old_bed.id != bed.id:
            old_bed.is_occupied = False

        adm.ward_id = bed.ward_id
        adm.room_id = bed.room_id
        adm.bed_id = bed.id
        bed.is_occupied = True

        now = datetime.now(timezone.utc)
        # Invariant 7: close the outgoing segment before opening the new one.
        _open = (
            self.db.query(BedStaySegment)
            .filter(
                BedStaySegment.hospital_id == self.hospital_id,
                BedStaySegment.admission_id == adm.id,
                BedStaySegment.ended_at.is_(None),
            )
            .order_by(BedStaySegment.started_at.desc())
            .first()
        )
        if _open:
            _open.ended_at = now
        segment = BedStaySegment(
            hospital_id=self.hospital_id,
            admission_id=adm.id,
            ward_id=bed.ward_id,
            room_id=bed.room_id,
            bed_id=bed.id,
            rate_per_day=float(getattr(bed.ward, "bed_charge_per_day", 0) or 0) if bed.ward else 0.0,
            started_at=now,
            ended_at=None,
        )
        self.db.add(segment)

        profile = self.repo.get_icu_profile(adm.id)
        if not profile:
            profile = IcuPatientProfile(
                hospital_id=self.hospital_id,
                admission_id=adm.id,
                patient_id=adm.patient_id,
                ventilator_mode=payload.ventilator_mode,
                peep=payload.peep,
                fio2_percent=payload.fio2_percent,
                invasive_line_days=payload.invasive_lines or {},
                inotrope_support=payload.inotrope_support,
                inotrope_details=payload.inotrope_details,
                gcs_score=payload.gcs_score,
                care_indicators={
                    "acuity": payload.acuity or "high",
                    "transfer_reason": payload.transfer_reason,
                },
            )
            self.db.add(profile)
        else:
            if payload.ventilator_mode is not None:
                profile.ventilator_mode = payload.ventilator_mode
            if payload.peep is not None:
                profile.peep = payload.peep
            if payload.fio2_percent is not None:
                profile.fio2_percent = payload.fio2_percent
            if payload.invasive_lines is not None:
                profile.invasive_line_days = payload.invasive_lines
            profile.inotrope_support = payload.inotrope_support
            if payload.inotrope_details is not None:
                profile.inotrope_details = payload.inotrope_details
            if payload.gcs_score is not None:
                profile.gcs_score = payload.gcs_score
            indicators = profile.care_indicators or {}
            indicators["acuity"] = payload.acuity or indicators.get("acuity", "high")
            if payload.transfer_reason:
                indicators["transfer_reason"] = payload.transfer_reason
            profile.care_indicators = indicators

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="transfer_icu",
            entity_type="admission",
            entity_id=adm.id,
            summary=f"Transferred patient to ICU Bed {bed.bed_code}",
        )
        self.db.commit()
        return GetIcuBoardAction(self.db, self.hospital_id).execute_for_admission(adm.id)


class StepDownIcuAction:
    """Step down an ICU patient to a general or recovery ward bed."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = CriticalCareRepository(db, hospital_id)

    def execute(self, payload: IcuStepDownRequest, actor: dict[str, Any]) -> dict[str, Any]:
        from modules.beds.entities.bed import Bed
        from modules.inpatient.entities.admission import Admission, AdmissionStatus, BedStaySegment

        adm = (
            self.db.query(Admission)
            .filter(
                Admission.id == payload.admission_id,
                Admission.hospital_id == self.hospital_id,
                Admission.status.in_(
                    [AdmissionStatus.admitted, AdmissionStatus.discharge_requested]
                ),
            )
            .first()
        )
        if not adm:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Active admission not found")

        bed = (
            self.db.query(Bed)
            .filter(Bed.id == payload.to_bed_id, Bed.hospital_id == self.hospital_id)
            .with_for_update()
            .first()
        )
        if not bed:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target ward bed not found")
        if bed.is_occupied and bed.id != adm.bed_id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Target bed is already occupied")

        old_bed = adm.bed
        if old_bed:
            old_bed.is_occupied = False

        adm.ward_id = bed.ward_id
        adm.room_id = bed.room_id
        adm.bed_id = bed.id
        bed.is_occupied = True

        now = datetime.now(timezone.utc)
        _open = (
            self.db.query(BedStaySegment)
            .filter(
                BedStaySegment.hospital_id == self.hospital_id,
                BedStaySegment.admission_id == adm.id,
                BedStaySegment.ended_at.is_(None),
            )
            .order_by(BedStaySegment.started_at.desc())
            .first()
        )
        if _open:
            _open.ended_at = now
        segment = BedStaySegment(
            hospital_id=self.hospital_id,
            admission_id=adm.id,
            ward_id=bed.ward_id,
            room_id=bed.room_id,
            bed_id=bed.id,
            rate_per_day=float(getattr(bed.ward, "bed_charge_per_day", 0) or 0) if bed.ward else 0.0,
            started_at=now,
            ended_at=None,
        )
        self.db.add(segment)

        profile = self.repo.get_icu_profile(adm.id)
        if profile:
            indicators = profile.care_indicators or {}
            indicators["stepped_down"] = True
            indicators["stepped_down_at"] = now.isoformat()
            if payload.step_down_notes:
                indicators["step_down_notes"] = payload.step_down_notes
            profile.care_indicators = indicators

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="step_down_icu",
            entity_type="admission",
            entity_id=adm.id,
            summary=f"Stepped down patient from ICU to Bed {bed.bed_code}",
        )
        self.db.commit()
        return {"status": "ok", "message": f"Patient stepped down to {bed.bed_code}"}
