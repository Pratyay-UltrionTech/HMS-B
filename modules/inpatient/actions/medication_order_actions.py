"""Actions for IPD Medication Orders."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from modules.inpatient.contracts.inpatient_contracts import (
    AdministerPrnMedicationRequest,
    DiscontinueMedicationOrderRequest,
    IpdMedicationOrderCreate,
    IpdMedicationOrderResponse,
)
from modules.inpatient.contracts.nursing_contracts import MedicationAdminResponse
from modules.inpatient.db.admissions_repository import AdmissionsRepository
from modules.inpatient.db.medication_order_repository import MedicationOrderRepository
from modules.inpatient.entities.admission import ACTIVE_INPATIENT_STATUSES
from modules.inpatient.entities.medication_order import IpdMedicationOrder, MedicationOrderStatus
from shared.audit.service import write_audit_log
from shared.exceptions.base import NotFoundError, ValidationError


def _to_order_response(o: IpdMedicationOrder) -> IpdMedicationOrderResponse:
    return IpdMedicationOrderResponse(
        id=o.id,
        hospital_id=o.hospital_id,
        admission_id=o.admission_id,
        patient_id=o.patient_id,
        doctor_id=o.doctor_id,
        doctor_name=o.doctor_name,
        medicine_id=o.medicine_id,
        medicine_name=o.medicine_name,
        dose=o.dose,
        dosage_unit=o.dosage_unit,
        route=o.route,
        frequency=o.frequency,
        schedule_timing=o.schedule_timing,
        start_time=o.start_time,
        end_time=o.end_time,
        duration_days=o.duration_days,
        instructions=o.instructions,
        is_prn=o.is_prn,
        prn_indication=o.prn_indication,
        status=o.status,
        discontinued_at=o.discontinued_at,
        discontinued_by_id=o.discontinued_by_id,
        discontinued_by_name=o.discontinued_by_name,
        discontinued_reason=o.discontinued_reason,
        created_at=o.created_at,
        updated_at=o.updated_at,
    )


class ListMedicationOrdersAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = MedicationOrderRepository(db, hospital_id)

    def execute(
        self, admission_id: UUID, status: MedicationOrderStatus | None = None
    ) -> list[IpdMedicationOrderResponse]:
        orders = self.repo.list_orders(admission_id, status=status)
        return [_to_order_response(o) for o in orders]


class CreateMedicationOrderAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = MedicationOrderRepository(db, hospital_id)
        self.admissions_repo = AdmissionsRepository(db)

    def execute(
        self,
        admission_id: UUID,
        payload: IpdMedicationOrderCreate,
        actor: dict[str, Any],
    ) -> IpdMedicationOrderResponse:
        admission = self.admissions_repo.get_admission_by_id(self.hospital_id, admission_id)
        if not admission:
            raise NotFoundError("Admission not found")
        if admission.status.value not in ACTIVE_INPATIENT_STATUSES:
            raise ValidationError(
                f"Cannot prescribe medication for admission in '{admission.status.value}' state"
            )

        actor_id_str = actor.get("id") or actor.get("sub")
        doctor_id = UUID(str(actor_id_str)) if actor_id_str else admission.doctor_id
        doctor_name = str(actor.get("name") or "Doctor")

        order = self.repo.create_order(
            admission_id=admission_id,
            patient_id=admission.patient_id,
            doctor_id=doctor_id,
            doctor_name=doctor_name,
            medicine_name=payload.medicine_name,
            dose=payload.dose,
            dosage_unit=payload.dosage_unit,
            route=payload.route,
            frequency=payload.frequency,
            schedule_timing=payload.schedule_timing,
            start_time=payload.start_time,
            end_time=payload.end_time,
            duration_days=payload.duration_days,
            instructions=payload.instructions,
            is_prn=payload.is_prn,
            prn_indication=payload.prn_indication,
            medicine_id=payload.medicine_id,
        )
        self.db.commit()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="create",
            entity_type="ipd_medication_order",
            entity_id=str(order.id),
            summary=f"Prescribed {order.medicine_name} {order.dose} ({order.frequency}) for {admission.ip_id}",
        )
        self.db.commit()
        return _to_order_response(order)


class DiscontinueMedicationOrderAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = MedicationOrderRepository(db, hospital_id)

    def execute(
        self,
        order_id: UUID,
        payload: DiscontinueMedicationOrderRequest,
        actor: dict[str, Any],
    ) -> IpdMedicationOrderResponse:
        actor_id_str = actor.get("id") or actor.get("sub")
        discontinued_by_id = UUID(str(actor_id_str)) if actor_id_str else None
        discontinued_by_name = str(actor.get("name") or "Doctor")

        order = self.repo.discontinue_order(
            order_id=order_id,
            discontinued_by_id=discontinued_by_id,
            discontinued_by_name=discontinued_by_name,
            reason=payload.reason,
        )
        if not order:
            raise NotFoundError("Medication order not found")
        self.db.commit()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="update",
            entity_type="ipd_medication_order",
            entity_id=str(order.id),
            summary=f"Discontinued {order.medicine_name}: {payload.reason}",
        )
        self.db.commit()
        return _to_order_response(order)


class AdministerPrnMedicationAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = MedicationOrderRepository(db, hospital_id)

    def execute(
        self,
        order_id: UUID,
        payload: AdministerPrnMedicationRequest,
        actor: dict[str, Any],
    ) -> MedicationAdminResponse:
        actor_id_str = actor.get("id") or actor.get("sub") or actor.get("user_id")
        nurse_id = UUID(str(actor_id_str)) if actor_id_str else None
        nurse_name = str(actor.get("name") or actor.get("email") or "Nurse")

        record = self.repo.record_prn_administration(
            order_id=order_id,
            administered_by_id=nurse_id,
            administered_by_name=nurse_name,
            dose_administered=payload.dose or "",
            route=payload.route or "",
            indication_reason=payload.indication,
            notes=payload.notes,
            administered_at=payload.administered_at,
        )
        self.db.commit()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="PRN_ADMINISTER_MEDICATION",
            entity_type="medication_administration_record",
            entity_id=str(record.id),
            summary=f"PRN Administered {record.medicine_name} {record.dose} to patient: {payload.indication}",
        )
        self.db.commit()
        return MedicationAdminResponse.model_validate(record)

