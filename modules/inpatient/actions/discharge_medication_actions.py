"""Actions for IPD Discharge / Take-Home Medication management."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from modules.clinical_records.entities.clinical_record import Prescription
from modules.inpatient.contracts.inpatient_contracts import (
    DischargeMedicationStatusResponse,
    NoDischargeMedsRequest,
    SuggestedDischargeMedication,
)
from modules.inpatient.db.admissions_repository import AdmissionsRepository
from modules.inpatient.entities.admission import Admission
from modules.inpatient.entities.medication_order import (
    IpdMedicationOrder,
    MedicationOrderStatus,
)
from modules.inpatient.entities.nursing_entities import MedicationAdministrationRecord as IpdMedicationAdministration
from shared.audit.service import write_audit_log
from shared.exceptions.base import NotFoundError, ValidationError


class GetDischargeMedicationStatusAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.admissions_repo = AdmissionsRepository(db)

    def execute(self, admission_id: UUID) -> DischargeMedicationStatusResponse:
        admission = self.admissions_repo.get_admission_by_id(self.hospital_id, admission_id)
        if not admission:
            raise NotFoundError("Admission not found")

        rx = (
            self.db.query(Prescription)
            .filter(
                Prescription.hospital_id == self.hospital_id,
                Prescription.admission_id == admission_id,
                Prescription.status.in_(["issued", "dispensed", "completed"]),
            )
            .order_by(Prescription.created_at.desc())
            .first()
        )

        has_rx = rx is not None
        no_meds = bool(getattr(admission, "no_discharge_meds", False))

        doc_name = None
        if getattr(admission, "no_discharge_meds_doctor_id", None):
            from modules.doctors.entities.doctor import HospitalUser
            doc = self.db.query(HospitalUser).filter(HospitalUser.id == admission.no_discharge_meds_doctor_id).first()
            if doc:
                doc_name = getattr(doc, "name", None) or getattr(doc, "full_name", None)

        return DischargeMedicationStatusResponse(
            admission_id=admission_id,
            has_discharge_prescription=has_rx,
            prescription_id=rx.id if rx else None,
            no_discharge_meds=no_meds,
            no_discharge_meds_reason=getattr(admission, "no_discharge_meds_reason", None),
            no_discharge_meds_doctor_id=getattr(admission, "no_discharge_meds_doctor_id", None),
            no_discharge_meds_doctor_name=doc_name,
            can_discharge_medically=has_rx or no_meds,
        )


class RecordNoDischargeMedsAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.admissions_repo = AdmissionsRepository(db)

    def execute(
        self,
        admission_id: UUID,
        payload: NoDischargeMedsRequest,
        actor: dict[str, Any],
    ) -> DischargeMedicationStatusResponse:
        admission = (
            self.db.query(Admission)
            .filter(
                Admission.id == admission_id,
                Admission.hospital_id == self.hospital_id,
            )
            .with_for_update()
            .first()
        )
        if not admission:
            raise NotFoundError("Admission not found")

        actor_id_str = actor.get("doctor_id") or actor.get("id") or actor.get("sub")
        actor_id = None
        if actor_id_str:
            try:
                actor_id = UUID(str(actor_id_str))
            except (ValueError, TypeError):
                actor_id = None

        admission.no_discharge_meds = True
        admission.no_discharge_meds_reason = payload.reason
        admission.no_discharge_meds_doctor_id = actor_id
        self.db.commit()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="update",
            entity_type="admission_discharge_medication",
            entity_id=str(admission_id),
            summary=f"Recorded 'No discharge medicines required' for admission {admission.ip_id or admission.id}",
        )
        self.db.commit()

        return GetDischargeMedicationStatusAction(self.db, self.hospital_id).execute(admission_id)


class GetSuggestedDischargeMedsAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    def execute(self, admission_id: UUID) -> list[SuggestedDischargeMedication]:
        orders = (
            self.db.query(IpdMedicationOrder)
            .filter(
                IpdMedicationOrder.hospital_id == self.hospital_id,
                IpdMedicationOrder.admission_id == admission_id,
            )
            .order_by(IpdMedicationOrder.created_at.desc())
            .all()
        )

        out: list[SuggestedDischargeMedication] = []
        for o in orders:
            # Find last administration time if available
            last_admin = (
                self.db.query(IpdMedicationAdministration)
                .filter(
                    IpdMedicationAdministration.hospital_id == self.hospital_id,
                    IpdMedicationAdministration.order_id == o.id,
                )
                .order_by(IpdMedicationAdministration.administered_at.desc())
                .first()
            )
            out.append(
                SuggestedDischargeMedication(
                    id=o.id,
                    medicine_id=o.medicine_id,
                    medicine_name=o.medicine_name,
                    dose=o.dose,
                    dosage_unit=o.dosage_unit or "mg",
                    route=o.route or "oral",
                    frequency=o.frequency or "od",
                    instructions=o.instructions,
                    is_prn=bool(o.is_prn),
                    prn_indication=o.prn_indication,
                    status=o.status.value if hasattr(o.status, "value") else str(o.status),
                    last_administered_at=last_admin.administered_at if last_admin else None,
                )
            )
        return out
