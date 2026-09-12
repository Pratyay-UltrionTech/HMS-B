"""
Emergency treatment orders actions (STAT medications, verbal orders, resuscitation procedures).

Conforms to UltrionTech-Backend-Template modules/emergency/actions/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from hms_migration.modules.emergency.contracts.emergency_contracts import (
    EmergencyOrderCreate,
    EmergencyOrderExecute,
    EmergencyOrderResponse,
)
from hms_migration.modules.emergency.db.emergency_repository import EmergencyRepository
from hms_migration.modules.emergency.entities.emergency_entities import (
    EmergencyOrderStatus,
    EmergencyStatus,
    EmergencyTreatmentOrder,
)
from hms_migration.modules.emergency.validators.emergency_validators import assert_can_order_treatment
from hms_migration.shared.audit.service import write_audit_log


class CreateEmergencyOrderAction:
    """Action for Feature 4: Create Emergency Orders & Treatment."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = EmergencyRepository(db, hospital_id)

    def execute(
        self,
        encounter_id: UUID,
        payload: EmergencyOrderCreate,
        actor: dict[str, Any],
    ) -> EmergencyOrderResponse:
        encounter = self.repo.get_encounter_by_id(encounter_id)
        if not encounter:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Emergency encounter not found")

        assert_can_order_treatment(encounter)

        doctor_name = str(actor.get("name") or "Emergency Doctor")
        doc_id_raw = actor.get("doctor_id") or actor.get("user_id")
        doctor_id = UUID(str(doc_id_raw)) if doc_id_raw else None

        order = EmergencyTreatmentOrder(
            hospital_id=self.hospital_id,
            encounter_id=encounter_id,
            order_type=payload.order_type,
            description=payload.description.strip(),
            dosage=payload.dosage.strip() if payload.dosage else None,
            route=payload.route.strip() if payload.route else None,
            is_stat=payload.is_stat,
            is_verbal=payload.is_verbal,
            ordered_by_doctor_id=doctor_id,
            ordered_by_name=doctor_name,
            execution_status=EmergencyOrderStatus.pending,
            clinical_notes=payload.clinical_notes.strip() if payload.clinical_notes else None,
            created_at=datetime.now(timezone.utc),
        )
        self.repo.add(order)

        # Transition encounter to in_treatment if not already there
        if encounter.status in (EmergencyStatus.registered, EmergencyStatus.triaged):
            encounter.status = EmergencyStatus.in_treatment

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="order_treatment",
            entity_type="emergency_order",
            entity_id=order.id,
            summary=f"Placed emergency order {order.description} (STAT={order.is_stat})",
            details={"encounter_id": str(encounter_id), "type": payload.order_type.value, "is_stat": payload.is_stat},
        )
        self.repo.commit()
        self.repo.refresh(order)
        return EmergencyOrderResponse.model_validate(order)


class ExecuteEmergencyOrderAction:
    """Action for executing/completing an emergency treatment order by nurse."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = EmergencyRepository(db, hospital_id)

    def execute(
        self,
        order_id: UUID,
        payload: EmergencyOrderExecute,
        actor: dict[str, Any],
    ) -> EmergencyOrderResponse:
        order = (
            self.db.query(EmergencyTreatmentOrder)
            .filter(
                EmergencyTreatmentOrder.id == order_id,
                EmergencyTreatmentOrder.hospital_id == self.hospital_id,
            )
            .first()
        )
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Emergency order not found")

        if order.execution_status == EmergencyOrderStatus.completed:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Order is already completed")

        nurse_name = str(actor.get("name") or "Emergency Nurse")
        nurse_id_raw = actor.get("user_id")
        nurse_id = UUID(str(nurse_id_raw)) if nurse_id_raw else None

        order.execution_status = EmergencyOrderStatus.completed
        order.executed_by_nurse_id = nurse_id
        order.executed_by_name = nurse_name
        order.executed_at = datetime.now(timezone.utc)
        if payload.clinical_notes:
            order.clinical_notes = (
                f"{order.clinical_notes}\nExecution Note: {payload.clinical_notes.strip()}"
                if order.clinical_notes
                else f"Execution Note: {payload.clinical_notes.strip()}"
            )

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="execute_order",
            entity_type="emergency_order",
            entity_id=order.id,
            summary=f"Executed emergency order {order.description}",
            details={"status": order.execution_status.value},
        )
        self.repo.commit()
        self.repo.refresh(order)
        return EmergencyOrderResponse.model_validate(order)


class ListEmergencyOrdersAction:
    """List orders for an emergency encounter."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    def execute(self, encounter_id: UUID) -> list[EmergencyOrderResponse]:
        orders = (
            self.db.query(EmergencyTreatmentOrder)
            .filter(
                EmergencyTreatmentOrder.encounter_id == encounter_id,
                EmergencyTreatmentOrder.hospital_id == self.hospital_id,
            )
            .order_by(EmergencyTreatmentOrder.created_at.desc())
            .all()
        )
        return [EmergencyOrderResponse.model_validate(o) for o in orders]
