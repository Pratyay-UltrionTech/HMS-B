"""
SQLAlchemy declarative entities for Clinical Decision Support and Medication Safety.

Covers:
- Feature 19: Medication Reconciliation (medication_reconciliations, medication_reconciliation_items)
- Feature 20: Clinical Alerts (clinical_rules, clinical_alerts)
- Feature 21: Clinical Order Sets (clinical_order_sets, clinical_order_set_items)

Conforms to UltrionTech-Backend-Template and targets hms_migration Base.
"""

from __future__ import annotations

from datetime import datetime
import enum
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hms_migration.infrastructure.postgres.base import Base


# ---------------------------------------------------------------------------
# Feature 19: Medication Reconciliation Enums & Entities
# ---------------------------------------------------------------------------

class MedicationReconciliationStage(str, enum.Enum):
    admission = "admission"
    transfer = "transfer"
    discharge = "discharge"


class MedicationReconciliationStatus(str, enum.Enum):
    in_progress = "in_progress"
    completed = "completed"
    discrepancy_flagged = "discrepancy_flagged"


class MedicationReconciliationSource(str, enum.Enum):
    home_medication = "home_medication"
    active_inpatient = "active_inpatient"
    discharge_planned = "discharge_planned"


class MedicationReconciliationAction(str, enum.Enum):
    continue_med = "continue"
    discontinue = "discontinue"
    modify = "modify"
    hold = "hold"
    substitute = "substitute"


class MedicationDiscrepancyType(str, enum.Enum):
    none = "none"
    omission = "omission"
    duplication = "duplication"
    dose_mismatch = "dose_mismatch"
    contraindication = "contraindication"


class MedicationReconciliation(Base):
    """Encapsulates a structured medication reconciliation protocol session."""

    __tablename__ = "medication_reconciliations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    admission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admissions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    stage: Mapped[MedicationReconciliationStage] = mapped_column(
        Enum(MedicationReconciliationStage, name="medication_reconciliation_stage"),
        nullable=False,
        default=MedicationReconciliationStage.admission,
    )
    status: Mapped[MedicationReconciliationStatus] = mapped_column(
        Enum(MedicationReconciliationStatus, name="medication_reconciliation_status"),
        nullable=False,
        default=MedicationReconciliationStatus.in_progress,
    )
    reconciled_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    items: Mapped[list[MedicationReconciliationItem]] = relationship(
        "MedicationReconciliationItem",
        back_populates="reconciliation",
        cascade="all, delete-orphan",
        order_by="MedicationReconciliationItem.created_at",
    )


class MedicationReconciliationItem(Base):
    """Line-item representing a medication evaluated in a reconciliation session."""

    __tablename__ = "medication_reconciliation_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    reconciliation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("medication_reconciliations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    medicine_name: Mapped[str] = mapped_column(String(255), nullable=False)
    dosage: Mapped[str] = mapped_column(String(128), nullable=False)
    frequency: Mapped[str] = mapped_column(String(64), nullable=False)
    route: Mapped[str] = mapped_column(String(64), nullable=False, default="oral")
    source: Mapped[MedicationReconciliationSource] = mapped_column(
        Enum(MedicationReconciliationSource, name="medication_reconciliation_source"),
        nullable=False,
        default=MedicationReconciliationSource.home_medication,
    )
    action: Mapped[MedicationReconciliationAction] = mapped_column(
        Enum(MedicationReconciliationAction, name="medication_reconciliation_action"),
        nullable=False,
        default=MedicationReconciliationAction.continue_med,
    )
    discrepancy_type: Mapped[MedicationDiscrepancyType] = mapped_column(
        Enum(MedicationDiscrepancyType, name="medication_discrepancy_type"),
        nullable=False,
        default=MedicationDiscrepancyType.none,
    )
    discrepancy_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    clinical_override_justification: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    reconciliation: Mapped[MedicationReconciliation] = relationship(
        "MedicationReconciliation", back_populates="items"
    )


# ---------------------------------------------------------------------------
# Feature 20: Clinical Alerts Enums & Entities
# ---------------------------------------------------------------------------

class ClinicalRuleCategory(str, enum.Enum):
    vitals_alert = "vitals_alert"
    lab_critical = "lab_critical"
    preventive_care = "preventive_care"
    sepsis_screening = "sepsis_screening"
    medication_safety = "medication_safety"


class ClinicalAlertSeverity(str, enum.Enum):
    info = "info"
    warning = "warning"
    critical = "critical"


class ClinicalAlertStatus(str, enum.Enum):
    active = "active"
    acknowledged = "acknowledged"
    dismissed = "dismissed"
    resolved = "resolved"


class ClinicalRule(Base):
    """Hospital-configurable multi-parameter rule for Clinical Decision Support."""

    __tablename__ = "clinical_rules"
    __table_args__ = (
        UniqueConstraint("hospital_id", "rule_code", name="uq_clinical_rule_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    rule_code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[ClinicalRuleCategory] = mapped_column(
        Enum(ClinicalRuleCategory, name="clinical_rule_category"),
        nullable=False,
        default=ClinicalRuleCategory.vitals_alert,
    )
    severity: Mapped[ClinicalAlertSeverity] = mapped_column(
        Enum(ClinicalAlertSeverity, name="clinical_alert_severity"),
        nullable=False,
        default=ClinicalAlertSeverity.warning,
    )
    # Composite multi-parameter rule definition:
    # {"logic": "AND", "conditions": [{"parameter_type": "vital", "name": "heart_rate", "operator": "gte", "value": 120}]}
    rule_definition: Mapped[dict] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"), nullable=False
    )
    recommendation: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ClinicalAlert(Base):
    """Point-of-care alert triggered against a patient based on active clinical rules."""

    __tablename__ = "clinical_alerts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    admission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admissions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    rule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clinical_rules.id", ondelete="SET NULL"), nullable=True, index=True
    )
    severity: Mapped[ClinicalAlertSeverity] = mapped_column(
        Enum(ClinicalAlertSeverity, name="clinical_alert_severity", create_type=False),
        nullable=False,
        default=ClinicalAlertSeverity.warning,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ClinicalAlertStatus] = mapped_column(
        Enum(ClinicalAlertStatus, name="clinical_alert_status"),
        nullable=False,
        default=ClinicalAlertStatus.active,
        index=True,
    )
    acknowledged_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="SET NULL"), nullable=True
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    clinical_override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Feature 21: Clinical Order Sets Enums & Entities
# ---------------------------------------------------------------------------

class ClinicalOrderType(str, enum.Enum):
    lab = "lab"
    radiology = "radiology"
    medication = "medication"
    nursing = "nursing"


class ClinicalOrderSet(Base):
    """Standardized multi-disciplinary clinical order bundle template."""

    __tablename__ = "clinical_order_sets"
    __table_args__ = (
        UniqueConstraint("hospital_id", "code", name="uq_clinical_order_set_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False, default="General")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    items: Mapped[list[ClinicalOrderSetItem]] = relationship(
        "ClinicalOrderSetItem",
        back_populates="order_set",
        cascade="all, delete-orphan",
        order_by="ClinicalOrderSetItem.sort_order",
    )


class ClinicalOrderSetItem(Base):
    """Typed line item within a clinical order set bundle."""

    __tablename__ = "clinical_order_set_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    order_set_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clinical_order_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_type: Mapped[ClinicalOrderType] = mapped_column(
        Enum(ClinicalOrderType, name="clinical_order_type"), nullable=False
    )

    # Clean typed foreign keys preserving referential correctness (Correction #3)
    lab_test_catalog_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lab_test_catalog.id", ondelete="SET NULL"), nullable=True
    )
    radiology_scan_catalog_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("radiology_scan_catalog.id", ondelete="SET NULL"), nullable=True
    )
    medicine_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("medicines.id", ondelete="SET NULL"), nullable=True
    )

    item_name: Mapped[str] = mapped_column(String(255), nullable=False)
    dosage_or_instruction: Mapped[str | None] = mapped_column(String(255), nullable=True)
    frequency: Mapped[str | None] = mapped_column(String(64), nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_mandatory: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    order_set: Mapped[ClinicalOrderSet] = relationship(
        "ClinicalOrderSet", back_populates="items"
    )
