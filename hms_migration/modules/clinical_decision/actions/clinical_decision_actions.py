"""
Business actions for Clinical Decision Support and Medication Safety.

Covers:
- Feature 19: Medication Reconciliation workflow (Correction #1 applied: strictly reconciliation, no duplicate order engine)
- Feature 20: Multi-parameter Clinical Alert evaluation (Correction #2 applied: composite rule evaluation, no hardcoded medical rules)
- Feature 21: Clinical Order Set creation and orchestration (Correction #3 applied: typed references, delegates to existing orders)
"""

from __future__ import annotations

from datetime import datetime, timezone
import operator
from typing import Any
import uuid
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from hms_migration.modules.clinical_decision.contracts.clinical_decision_contracts import (
    AcknowledgeAlertRequest,
    ApplyOrderSetRequest,
    ApplyOrderSetResult,
    ClinicalAlertResponse,
    ClinicalOrderSetCreate,
    ClinicalOrderSetResponse,
    ClinicalRuleCreate,
    ClinicalRuleResponse,
    ClinicalRuleUpdate,
    EvaluateAlertsRequest,
    MedicationReconciliationCreate,
    MedicationReconciliationResponse,
    UpdateReconciliationItemsRequest,
)
from hms_migration.modules.clinical_decision.db.clinical_decision_repository import ClinicalDecisionRepository
from hms_migration.modules.clinical_decision.entities.clinical_decision_entities import (
    ClinicalAlert,
    ClinicalAlertStatus,
    ClinicalOrderSet,
    ClinicalOrderSetItem,
    ClinicalOrderType,
    ClinicalRule,
    MedicationDiscrepancyType,
    MedicationReconciliation,
    MedicationReconciliationItem,
    MedicationReconciliationStatus,
)
from hms_migration.modules.laboratory.entities.lab_entities import (
    LabItemStatus,
    LabOrder,
    LabOrderItem,
    LabOrderSource,
    LabOrderStatus,
    LabTestCatalog,
)
from hms_migration.modules.patients.entities.patient import Patient
from hms_migration.modules.radiology.entities.radiology_entities import (
    RadiologyOrder,
    RadiologyOrderStatus,
    RadiologyScanCatalog,
)
from hms_migration.modules.vitals.entities.vital_reading import VitalReading


class ClinicalDecisionActions:
    """Orchestrates use cases for Features 19, 20, and 21."""

    def __init__(self, db: Session, hospital_id: UUID, actor: dict[str, Any] | None = None) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.actor = actor or {}
        self.repo = ClinicalDecisionRepository(db, hospital_id)

    # -----------------------------------------------------------------------
    # Feature 19: Medication Reconciliation
    # -----------------------------------------------------------------------

    def create_reconciliation(self, payload: MedicationReconciliationCreate) -> MedicationReconciliationResponse:
        # Verify patient exists in tenant
        patient = self.db.query(Patient).filter(
            Patient.id == payload.patient_id, Patient.hospital_id == self.hospital_id
        ).first()
        if not patient:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

        has_discrepancy = any(
            item.discrepancy_type != MedicationDiscrepancyType.none and not item.resolved
            for item in payload.items
        )

        actor_user_id = self.actor.get("user_id") or self.actor.get("id")
        reconciliation = MedicationReconciliation(
            hospital_id=self.hospital_id,
            patient_id=payload.patient_id,
            admission_id=payload.admission_id,
            stage=payload.stage,
            status=(
                MedicationReconciliationStatus.discrepancy_flagged
                if has_discrepancy
                else MedicationReconciliationStatus.in_progress
            ),
            reconciled_by_id=UUID(str(actor_user_id)) if actor_user_id else None,
            reconciled_at=datetime.now(timezone.utc),
            notes=payload.notes,
        )

        for item_in in payload.items:
            rec_item = MedicationReconciliationItem(
                medicine_name=item_in.medicine_name.strip(),
                dosage=item_in.dosage.strip(),
                frequency=item_in.frequency.strip(),
                route=item_in.route.strip(),
                source=item_in.source,
                action=item_in.action,
                discrepancy_type=item_in.discrepancy_type,
                discrepancy_reason=item_in.discrepancy_reason,
                clinical_override_justification=item_in.clinical_override_justification,
                resolved=item_in.resolved,
            )
            reconciliation.items.append(rec_item)

        saved = self.repo.create_reconciliation(reconciliation)
        self.db.commit()
        return MedicationReconciliationResponse.model_validate(saved)

    def get_reconciliation(self, rec_id: UUID) -> MedicationReconciliationResponse:
        rec = self.repo.get_reconciliation_by_id(rec_id)
        if not rec:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Medication reconciliation not found")
        return MedicationReconciliationResponse.model_validate(rec)

    def list_reconciliations_for_admission(self, admission_id: UUID) -> list[MedicationReconciliationResponse]:
        records = self.repo.list_reconciliations_for_admission(admission_id)
        return [MedicationReconciliationResponse.model_validate(r) for r in records]

    def list_reconciliations_for_patient(self, patient_id: UUID) -> list[MedicationReconciliationResponse]:
        records = self.repo.list_reconciliations_for_patient(patient_id)
        return [MedicationReconciliationResponse.model_validate(r) for r in records]

    def update_reconciliation_items(
        self, rec_id: UUID, payload: UpdateReconciliationItemsRequest
    ) -> MedicationReconciliationResponse:
        rec = self.repo.get_reconciliation_by_id(rec_id)
        if not rec:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Medication reconciliation not found")

        # Clear existing items and replace with updated decisions
        rec.items.clear()
        has_unresolved_discrepancy = False

        for item_in in payload.items:
            if item_in.discrepancy_type != MedicationDiscrepancyType.none and not item_in.resolved:
                has_unresolved_discrepancy = True
            rec_item = MedicationReconciliationItem(
                medicine_name=item_in.medicine_name.strip(),
                dosage=item_in.dosage.strip(),
                frequency=item_in.frequency.strip(),
                route=item_in.route.strip(),
                source=item_in.source,
                action=item_in.action,
                discrepancy_type=item_in.discrepancy_type,
                discrepancy_reason=item_in.discrepancy_reason,
                clinical_override_justification=item_in.clinical_override_justification,
                resolved=item_in.resolved,
            )
            rec.items.append(rec_item)

        if payload.reconciliation_notes:
            rec.notes = payload.reconciliation_notes

        actor_user_id = self.actor.get("user_id") or self.actor.get("id")
        if actor_user_id:
            rec.reconciled_by_id = UUID(str(actor_user_id))
        rec.reconciled_at = datetime.now(timezone.utc)

        if payload.mark_completed:
            if has_unresolved_discrepancy:
                rec.status = MedicationReconciliationStatus.discrepancy_flagged
            else:
                rec.status = MedicationReconciliationStatus.completed
        elif has_unresolved_discrepancy:
            rec.status = MedicationReconciliationStatus.discrepancy_flagged
        else:
            rec.status = MedicationReconciliationStatus.in_progress

        self.db.commit()
        return MedicationReconciliationResponse.model_validate(rec)

    # -----------------------------------------------------------------------
    # Feature 20: Clinical Rules & Alerts
    # -----------------------------------------------------------------------

    def create_rule(self, payload: ClinicalRuleCreate) -> ClinicalRuleResponse:
        existing = self.repo.get_rule_by_code(payload.rule_code.strip())
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Clinical rule code '{payload.rule_code}' already exists in this hospital",
            )

        rule = ClinicalRule(
            hospital_id=self.hospital_id,
            rule_code=payload.rule_code.strip().upper(),
            name=payload.name.strip(),
            category=payload.category,
            severity=payload.severity,
            rule_definition=payload.rule_definition,
            recommendation=payload.recommendation,
            is_active=payload.is_active,
        )
        saved = self.repo.create_rule(rule)
        self.db.commit()
        return ClinicalRuleResponse.model_validate(saved)

    def list_rules(self) -> list[ClinicalRuleResponse]:
        rules = self.repo.list_active_rules()
        return [ClinicalRuleResponse.model_validate(r) for r in rules]

    def evaluate_patient_alerts(
        self, patient_id: UUID, payload: EvaluateAlertsRequest | None = None
    ) -> list[ClinicalAlertResponse]:
        patient = self.db.query(Patient).filter(
            Patient.id == patient_id, Patient.hospital_id == self.hospital_id
        ).first()
        if not patient:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

        active_rules = self.repo.list_active_rules()
        if not active_rules:
            return []

        # Gather patient observation snapshot
        obs_map: dict[str, Any] = {}
        if patient.age is not None:
            obs_map["age"] = float(patient.age)
        if patient.gender:
            obs_map["gender"] = patient.gender.lower()

        # Query latest vital readings
        vitals = self.db.query(VitalReading).filter(
            VitalReading.patient_id == patient_id,
            VitalReading.hospital_id == self.hospital_id,
        ).order_by(VitalReading.recorded_at.desc()).limit(10).all()

        for v in vitals:
            key = v.name.lower().replace(" ", "_")
            try:
                obs_map[key] = float(v.result)
            except (ValueError, TypeError):
                obs_map[key] = v.result

        # Merge live additional observations passed in evaluate payload
        if payload and payload.additional_observations:
            for k, val in payload.additional_observations.items():
                norm_key = k.lower().replace(" ", "_")
                try:
                    obs_map[norm_key] = float(val)
                except (ValueError, TypeError):
                    obs_map[norm_key] = val

        triggered_alerts: list[ClinicalAlert] = []
        now = datetime.now(timezone.utc)

        # Evaluate rules dynamically without hardcoded medical cutoffs (Correction #2)
        for rule in active_rules:
            definition = rule.rule_definition or {}
            conditions = definition.get("conditions", [])
            logic = str(definition.get("logic", "AND")).upper()

            if not conditions:
                continue

            matches: list[bool] = []
            for cond in conditions:
                param_name = str(cond.get("name", "")).lower().replace(" ", "_")
                op_str = str(cond.get("operator", "eq")).lower()
                target_val = cond.get("value")

                if param_name not in obs_map:
                    matches.append(False)
                    continue

                actual_val = obs_map[param_name]
                matches.append(self._compare(actual_val, op_str, target_val))

            rule_triggered = all(matches) if logic == "AND" else any(matches)
            if rule_triggered:
                # Avoid duplicate active alert if already active
                existing_active = self.db.query(ClinicalAlert).filter(
                    ClinicalAlert.patient_id == patient_id,
                    ClinicalAlert.rule_id == rule.id,
                    ClinicalAlert.status == ClinicalAlertStatus.active,
                    ClinicalAlert.hospital_id == self.hospital_id,
                ).first()

                if not existing_active:
                    alert = ClinicalAlert(
                        hospital_id=self.hospital_id,
                        patient_id=patient_id,
                        admission_id=payload.admission_id if payload else None,
                        rule_id=rule.id,
                        severity=rule.severity,
                        title=rule.name,
                        message=rule.recommendation or f"Clinical rule '{rule.name}' triggered based on patient parameters.",
                        status=ClinicalAlertStatus.active,
                        triggered_at=now,
                    )
                    self.db.add(alert)
                    triggered_alerts.append(alert)

        self.db.commit()

        all_patient_alerts = self.repo.list_alerts_for_patient(patient_id)
        return [ClinicalAlertResponse.model_validate(a) for a in all_patient_alerts]

    def _compare(self, actual: Any, op_str: str, target: Any) -> bool:
        """Safe dynamic comparison supporting numeric and string operators."""
        try:
            act_num = float(actual)
            tar_num = float(target)
            if op_str in (">", "gt"):
                return act_num > tar_num
            if op_str in (">=", "gte"):
                return act_num >= tar_num
            if op_str in ("<", "lt"):
                return act_num < tar_num
            if op_str in ("<=", "lte"):
                return act_num <= tar_num
            if op_str in ("==", "eq"):
                return act_num == tar_num
            if op_str in ("!=", "neq"):
                return act_num != tar_num
        except (ValueError, TypeError):
            pass

        act_str = str(actual).strip().lower()
        tar_str = str(target).strip().lower()
        if op_str in ("==", "eq"):
            return act_str == tar_str
        if op_str in ("!=", "neq"):
            return act_str != tar_str
        if op_str in ("contains", "in"):
            return tar_str in act_str
        return False

    def list_alerts(
        self, patient_id: UUID | None = None, status_filter: str | None = None
    ) -> list[ClinicalAlertResponse]:
        """List hospital alerts with optional patient/status filters."""
        from hms_migration.modules.clinical_decision.entities.clinical_decision_entities import (
            ClinicalAlertStatus as AlertStatus,
        )

        parsed_status: AlertStatus | None = None
        if status_filter:
            try:
                parsed_status = AlertStatus(str(status_filter).strip().lower())
            except ValueError:
                parsed_status = None
        alerts = self.repo.list_alerts(patient_id, parsed_status)
        return [ClinicalAlertResponse.model_validate(a) for a in alerts]

    def acknowledge_alert(self, alert_id: UUID, payload: AcknowledgeAlertRequest) -> ClinicalAlertResponse:
        alert = self.repo.get_alert_by_id(alert_id)
        if not alert:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clinical alert not found")

        # Enforce valid action-level lifecycle state transitions (Finding F-02)
        valid_transitions: dict[ClinicalAlertStatus, set[ClinicalAlertStatus]] = {
            ClinicalAlertStatus.active: {
                ClinicalAlertStatus.acknowledged,
                ClinicalAlertStatus.dismissed,
                ClinicalAlertStatus.resolved,
            },
            ClinicalAlertStatus.acknowledged: {
                ClinicalAlertStatus.resolved,
                ClinicalAlertStatus.dismissed,
            },
            ClinicalAlertStatus.dismissed: set(),
            ClinicalAlertStatus.resolved: set(),
        }
        allowed = valid_transitions.get(alert.status, set())
        if payload.status not in allowed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid alert state transition: cannot transition alert from '{alert.status.value}' to '{payload.status.value}'.",
            )

        actor_user_id = self.actor.get("user_id") or self.actor.get("id")
        alert.status = payload.status
        # Accept both backend (`clinical_override_reason`) and frontend (`notes`) keys.
        alert.clinical_override_reason = (
            payload.clinical_override_reason if payload.clinical_override_reason is not None else payload.notes
        )
        alert.acknowledged_by_id = UUID(str(actor_user_id)) if actor_user_id else None
        alert.acknowledged_at = datetime.now(timezone.utc)

        self.db.commit()
        return ClinicalAlertResponse.model_validate(alert)

    # -----------------------------------------------------------------------
    # Feature 21: Clinical Order Sets
    # -----------------------------------------------------------------------

    def create_order_set(self, payload: ClinicalOrderSetCreate) -> ClinicalOrderSetResponse:
        existing = self.repo.get_order_set_by_code(payload.code.strip())
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Clinical order set code '{payload.code}' already exists in this hospital",
            )

        order_set = ClinicalOrderSet(
            hospital_id=self.hospital_id,
            code=payload.code.strip().upper(),
            name=payload.name.strip(),
            category=payload.category.strip(),
            description=payload.description,
            department_id=payload.department_id,
            is_active=payload.is_active,
        )

        for item_in in payload.items:
            set_item = ClinicalOrderSetItem(
                order_type=item_in.order_type,
                lab_test_catalog_id=item_in.lab_test_catalog_id,
                radiology_scan_catalog_id=item_in.radiology_scan_catalog_id,
                medicine_id=item_in.medicine_id,
                item_name=item_in.item_name.strip(),
                dosage_or_instruction=item_in.dosage_or_instruction,
                frequency=item_in.frequency,
                instructions=item_in.instructions,
                is_mandatory=item_in.is_mandatory,
                sort_order=item_in.sort_order,
            )
            order_set.items.append(set_item)

        saved = self.repo.create_order_set(order_set)
        self.db.commit()
        return ClinicalOrderSetResponse.model_validate(saved)

    def list_order_sets(self, category: str | None = None) -> list[ClinicalOrderSetResponse]:
        sets = self.repo.list_order_sets(category)
        return [ClinicalOrderSetResponse.model_validate(s) for s in sets]

    def get_order_set(self, set_id: UUID) -> ClinicalOrderSetResponse:
        set_obj = self.repo.get_order_set_by_id(set_id)
        if not set_obj:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clinical order set not found")
        return ClinicalOrderSetResponse.model_validate(set_obj)

    def apply_order_set(self, set_id: UUID, payload: ApplyOrderSetRequest) -> ApplyOrderSetResult:
        order_set = self.repo.get_order_set_by_id(set_id)
        if not order_set:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clinical order set not found")

        patient = self.db.query(Patient).filter(
            Patient.id == payload.patient_id, Patient.hospital_id == self.hospital_id
        ).first()
        if not patient:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

        selected_items = order_set.items
        if payload.selected_item_ids:
            selected_set = set(payload.selected_item_ids)
            selected_items = [it for it in order_set.items if it.id in selected_set or it.is_mandatory]

        lab_items = [it for it in selected_items if it.order_type == ClinicalOrderType.lab]
        rad_items = [it for it in selected_items if it.order_type == ClinicalOrderType.radiology]
        med_items = [it for it in selected_items if it.order_type == ClinicalOrderType.medication]
        nursing_items = [it for it in selected_items if it.order_type == ClinicalOrderType.nursing]

        created_lab_order_id: UUID | None = None
        created_rad_ids: list[UUID] = []

        now = datetime.now(timezone.utc)
        actor_id = self.actor.get("user_id") or self.actor.get("id")
        doctor_uuid = UUID(str(actor_id)) if actor_id else None

        # Reuses existing LabOrder engine (Correction #3)
        if lab_items:
            lab_order_no = f"LO-{uuid.uuid4().hex[:8].upper()}"
            actor_name = str(self.actor.get("email") or self.actor.get("username") or "System")
            lab_order = LabOrder(
                hospital_id=self.hospital_id,
                order_no=lab_order_no,
                patient_id=patient.id,
                doctor_id=doctor_uuid,
                order_source=LabOrderSource.doctor_prescribed,
                ordered_by_name=actor_name,
                ordered_by_role="doctor" if doctor_uuid else "system",
                status=LabOrderStatus.ordered,
                clinical_notes=f"Order Set: {order_set.name}. {payload.clinical_notes or ''}",
                ordered_at=now,
            )
            self.db.add(lab_order)
            self.db.flush()
            created_lab_order_id = lab_order.id

            for li in lab_items:
                if li.lab_test_catalog_id:
                    cat_test = self.db.query(LabTestCatalog).filter(
                        LabTestCatalog.id == li.lab_test_catalog_id,
                        LabTestCatalog.hospital_id == self.hospital_id,
                    ).first()
                    lo_item = LabOrderItem(
                        hospital_id=self.hospital_id,
                        order_id=lab_order.id,
                        test_id=li.lab_test_catalog_id,
                        test_code=cat_test.test_code if cat_test else "UNKNOWN",
                        test_name=cat_test.test_name if cat_test else li.instructions or "Lab Test",
                        department=cat_test.department if cat_test else "General",
                        price=cat_test.price if cat_test else 0.0,
                        status=LabItemStatus.pending,
                        created_at=now,
                    )
                    self.db.add(lo_item)

        # Reuses existing RadiologyOrder engine (Correction #3)
        for ri in rad_items:
            if ri.radiology_scan_catalog_id:
                scan = self.db.query(RadiologyScanCatalog).filter(
                    RadiologyScanCatalog.id == ri.radiology_scan_catalog_id,
                    RadiologyScanCatalog.hospital_id == self.hospital_id,
                ).first()
                rad_order_no = f"RO-{uuid.uuid4().hex[:8].upper()}"
                actor_name = str(self.actor.get("email") or self.actor.get("username") or "System")
                rad_order = RadiologyOrder(
                    hospital_id=self.hospital_id,
                    order_no=rad_order_no,
                    patient_id=patient.id,
                    scan_id=ri.radiology_scan_catalog_id,
                    scan_code=scan.scan_code if scan else "UNKNOWN",
                    scan_name=scan.scan_name if scan else "Radiology Scan",
                    category=scan.category if scan else "General",
                    price=scan.price if scan else 0.0,
                    ordered_by_name=actor_name,
                    ordered_by_role="doctor" if doctor_uuid else "system",
                    doctor_id=doctor_uuid,
                    status=RadiologyOrderStatus.ordered,
                    clinical_notes=f"Order Set: {order_set.name}. {payload.clinical_notes or ''}",
                    scheduled_at=now,
                )
                self.db.add(rad_order)
                self.db.flush()
                created_rad_ids.append(rad_order.id)

        # Reuses canonical Prescription workflow when physician context is present (Finding F-01)
        created_prescription_id: UUID | None = None
        if med_items and doctor_uuid:
            from hms_migration.modules.clinical_records.entities.clinical_record import Prescription
            med_lines: list[str] = []
            dosage_lines: list[str] = []
            for m in med_items:
                med_lines.append(f"{m.item_name} {m.dosage_or_instruction or ''}".strip())
                dosage_lines.append(f"{m.frequency or 'daily'} - {m.instructions or 'standard'}".strip())

            rx = Prescription(
                hospital_id=self.hospital_id,
                doctor_id=doctor_uuid,
                patient_id=patient.id,
                appointment_id=None,
                symptoms=f"Order Set: {order_set.name}",
                diagnosis=payload.clinical_notes or f"Order Set Protocol: {order_set.name}",
                medicines="; ".join(med_lines),
                dosage="; ".join(dosage_lines),
                advice=f"Automated prescription from order set bundle {order_set.name}",
            )
            self.db.add(rx)
            self.db.flush()
            created_prescription_id = rx.id

        self.db.commit()

        msg = f"Order set '{order_set.name}' applied successfully with {len(selected_items)} total orders orchestrated."
        if med_items and not doctor_uuid:
            msg = f"Order set '{order_set.name}' applied ({len(selected_items)} items). Note: {len(med_items)} medication item(s) require physician sign-off and were not auto-prescribed."

        return ApplyOrderSetResult(
            order_set_id=order_set.id,
            order_set_name=order_set.name,
            patient_id=patient.id,
            admission_id=payload.admission_id,
            lab_order_id=created_lab_order_id,
            radiology_order_ids=created_rad_ids,
            prescription_id=created_prescription_id,
            nursing_instructions_logged=len(nursing_items),
            message=msg,
        )
