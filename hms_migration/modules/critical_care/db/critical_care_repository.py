"""
Database repository for Critical Care domain.

Conforms to UltrionTech-Backend-Template modules/critical_care/db/ specification.
All operations scoped strictly to hospital_id.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from hms_migration.modules.beds.entities.bed import Bed, Ward, WardType
from hms_migration.modules.critical_care.entities.critical_care_entities import (
    AlertStatus,
    ClinicalDeteriorationAlert,
    CodeBlueEvent,
    CodeBlueIncident,
    IcuFlowsheetEntry,
    IcuPatientProfile,
)
from hms_migration.modules.inpatient.entities.admission import Admission, AdmissionStatus


class CriticalCareRepository:
    """Encapsulated data access for ICU, Deterioration Alerts, and Code Blue."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    def generate_next_code_blue_code(self) -> str:
        """Generate Code Blue incident code: CB-YYYY-NNNNN."""
        year = date.today().year
        prefix = f"CB-{year}-"
        latest = (
            self.db.query(func.max(CodeBlueIncident.incident_code))
            .filter(
                CodeBlueIncident.hospital_id == self.hospital_id,
                CodeBlueIncident.incident_code.like(f"{prefix}%"),
            )
            .scalar()
        )
        seq = 0
        if latest:
            try:
                seq = int(str(latest).rsplit("-", 1)[-1])
            except ValueError:
                seq = 0
        return f"{prefix}{seq + 1:05d}"

    def get_active_icu_admissions(self) -> list[Admission]:
        """Fetch all currently admitted patients in wards of type 'icu'."""
        return (
            self.db.query(Admission)
            .join(Admission.ward)
            .options(
                joinedload(Admission.patient),
                joinedload(Admission.ward),
                joinedload(Admission.bed),
                joinedload(Admission.doctor),
            )
            .filter(
                Admission.hospital_id == self.hospital_id,
                Admission.status == AdmissionStatus.admitted,
                Ward.ward_type == WardType.icu,
            )
            .order_by(Admission.admitted_at.asc())
            .all()
        )

    def get_icu_profile(self, admission_id: UUID) -> IcuPatientProfile | None:
        return (
            self.db.query(IcuPatientProfile)
            .filter(
                IcuPatientProfile.admission_id == admission_id,
                IcuPatientProfile.hospital_id == self.hospital_id,
            )
            .first()
        )

    def get_latest_flowsheet(self, admission_id: UUID) -> IcuFlowsheetEntry | None:
        return (
            self.db.query(IcuFlowsheetEntry)
            .filter(
                IcuFlowsheetEntry.admission_id == admission_id,
                IcuFlowsheetEntry.hospital_id == self.hospital_id,
            )
            .order_by(IcuFlowsheetEntry.recorded_at.desc())
            .first()
        )

    def list_flowsheet_entries(self, admission_id: UUID) -> list[IcuFlowsheetEntry]:
        return (
            self.db.query(IcuFlowsheetEntry)
            .filter(
                IcuFlowsheetEntry.admission_id == admission_id,
                IcuFlowsheetEntry.hospital_id == self.hospital_id,
            )
            .order_by(IcuFlowsheetEntry.recorded_at.desc())
            .all()
        )

    def list_active_alerts(self) -> list[ClinicalDeteriorationAlert]:
        return (
            self.db.query(ClinicalDeteriorationAlert)
            .options(joinedload(ClinicalDeteriorationAlert.patient))
            .filter(
                ClinicalDeteriorationAlert.hospital_id == self.hospital_id,
                ClinicalDeteriorationAlert.alert_status.in_([AlertStatus.active, AlertStatus.escalated]),
            )
            .order_by(ClinicalDeteriorationAlert.total_score.desc())
            .all()
        )

    def get_alert_by_id(self, alert_id: UUID) -> ClinicalDeteriorationAlert | None:
        return (
            self.db.query(ClinicalDeteriorationAlert)
            .filter(
                ClinicalDeteriorationAlert.id == alert_id,
                ClinicalDeteriorationAlert.hospital_id == self.hospital_id,
            )
            .first()
        )

    def get_code_blue_by_id(self, incident_id: UUID) -> CodeBlueIncident | None:
        return (
            self.db.query(CodeBlueIncident)
            .options(
                joinedload(CodeBlueIncident.patient),
                joinedload(CodeBlueIncident.events),
            )
            .filter(
                CodeBlueIncident.id == incident_id,
                CodeBlueIncident.hospital_id == self.hospital_id,
            )
            .first()
        )

    def list_code_blue_incidents(self) -> list[CodeBlueIncident]:
        return (
            self.db.query(CodeBlueIncident)
            .options(
                joinedload(CodeBlueIncident.patient),
                joinedload(CodeBlueIncident.events),
            )
            .filter(CodeBlueIncident.hospital_id == self.hospital_id)
            .order_by(CodeBlueIncident.activated_at.desc())
            .all()
        )

    def add(self, entity: object) -> None:
        self.db.add(entity)

    def commit(self) -> None:
        self.db.commit()

    def flush(self) -> None:
        self.db.flush()

    def refresh(self, entity: object) -> None:
        self.db.refresh(entity)
