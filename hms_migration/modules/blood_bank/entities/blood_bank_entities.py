"""
Target-native SQLAlchemy entities for Blood Bank Foundation.

Features covered:
- Feature 32: Donor Registry & Donation Collection (BloodDonor, BloodDonation)
- Feature 33: Blood Unit Inventory (Single unified BloodUnit model - Correction #7 & #8)
- Feature 34: Component Management & Separation Lineage (BloodComponentSeparation - Correction #9)
- Feature 35: Cross-Match Management (BloodCrossMatch - authorized recorded lab result, Correction #10)
- Feature 36: Blood Issue & Return Lifecycle (BloodIssue, BloodReturn - Correction #11)

- Feature 37: Transfusion Record (BloodTransfusion)
- Feature 38: Blood Traceability & Biovigilance (read-only query layer over the chain above,
  see actions/blood_bank_traceability_actions.py - no new tables for this feature)
"""

from __future__ import annotations

from datetime import date, datetime
import enum
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hms_migration.infrastructure.postgres.base import Base

if TYPE_CHECKING:
    from hms_migration.modules.patients.entities.patient import Patient
    from hms_migration.modules.inpatient.entities.admission import Admission


class BloodGroup(str, enum.Enum):
    A_pos = "A+"
    A_neg = "A-"
    B_pos = "B+"
    B_neg = "B-"
    AB_pos = "AB+"
    AB_neg = "AB-"
    O_pos = "O+"
    O_neg = "O-"


class BloodComponentType(str, enum.Enum):
    whole_blood = "whole_blood"
    packed_red_blood_cells = "prbc"
    fresh_frozen_plasma = "ffp"
    platelets = "platelets"
    cryoprecipitate = "cryoprecipitate"


class BloodUnitStatus(str, enum.Enum):
    quarantine = "quarantine"
    available = "available"
    reserved = "reserved"
    issued = "issued"
    separated = "separated"
    transfused = "transfused"
    discarded = "discarded"


class BloodIssueStatus(str, enum.Enum):
    issued = "issued"
    transfused = "transfused"
    returned = "returned"


class BloodReturnDisposition(str, enum.Enum):
    restocked = "restocked"
    discarded = "discarded"


class BloodTransfusionStatus(str, enum.Enum):
    in_progress = "in_progress"
    completed = "completed"
    aborted = "aborted"
    reaction = "reaction"


class TransfusionAdverseReactionType(str, enum.Enum):
    none = "none"
    febrile = "febrile"
    allergic = "allergic"
    hemolytic = "hemolytic"
    other = "other"


class BloodDonor(Base):
    """Blood Donor master record (Feature 32)."""

    __tablename__ = "blood_bank_donors"
    __table_args__ = (
        UniqueConstraint("hospital_id", "donor_number", name="uq_blood_donor_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    donor_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    gender: Mapped[str] = mapped_column(String(32), nullable=False)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    blood_group: Mapped[BloodGroup] = mapped_column(
        Enum(BloodGroup, name="blood_group"), nullable=False, index=True
    )
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_donation_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_eligible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    deferral_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    deferral_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    donations: Mapped[list[BloodDonation]] = relationship(
        "BloodDonation", back_populates="donor", cascade="all, delete-orphan"
    )


class BloodDonation(Base):
    """Blood donation collection record (Feature 32)."""

    __tablename__ = "blood_bank_donations"
    __table_args__ = (
        UniqueConstraint("hospital_id", "donation_number", name="uq_blood_donation_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    donation_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    donor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("blood_bank_donors.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    donation_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    donation_type: Mapped[str] = mapped_column(String(64), default="voluntary", nullable=False)
    bag_type: Mapped[str] = mapped_column(String(64), default="triple", nullable=False)
    volume_ml: Mapped[float] = mapped_column(Float, default=450.0, nullable=False)
    hemoglobin_g_dl: Mapped[float | None] = mapped_column(Float, nullable=True)
    blood_pressure: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pulse: Mapped[int | None] = mapped_column(Integer, nullable=True)
    collected_by: Mapped[str] = mapped_column(String(255), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    donor: Mapped[BloodDonor] = relationship("BloodDonor", back_populates="donations")
    units: Mapped[list[BloodUnit]] = relationship("BloodUnit", back_populates="donation")


class BloodUnit(Base):
    """
    Canonical unified Blood Unit inventory model (Features 33 & 34).
    Represents Whole Blood units and derived child components via parent_unit_id lineage.
    """

    __tablename__ = "blood_bank_units"
    __table_args__ = (
        UniqueConstraint("hospital_id", "unit_number", name="uq_blood_unit_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    unit_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    donation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("blood_bank_donations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    parent_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("blood_bank_units.id", ondelete="SET NULL"), nullable=True, index=True
    )
    component_type: Mapped[BloodComponentType] = mapped_column(
        Enum(BloodComponentType, name="blood_component_type"),
        nullable=False,
        default=BloodComponentType.whole_blood,
        index=True,
    )
    blood_group: Mapped[BloodGroup] = mapped_column(
        Enum(BloodGroup, name="blood_group", create_type=False), nullable=False, index=True
    )
    volume_ml: Mapped[float] = mapped_column(Float, nullable=False)
    collection_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expiry_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    storage_location: Mapped[str] = mapped_column(String(128), nullable=False, default="Refrigerated Storage")
    serology_tested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    serology_cleared: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[BloodUnitStatus] = mapped_column(
        Enum(BloodUnitStatus, name="blood_unit_status"),
        nullable=False,
        default=BloodUnitStatus.quarantine,
        index=True,
    )
    reserved_for_patient_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    reserved_for_admission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admissions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    donation: Mapped[BloodDonation | None] = relationship("BloodDonation", back_populates="units")
    parent_unit: Mapped[BloodUnit | None] = relationship(
        "BloodUnit",
        remote_side="BloodUnit.id",
        back_populates="child_components",
    )
    child_components: Mapped[list[BloodUnit]] = relationship(
        "BloodUnit",
        back_populates="parent_unit",
    )
    cross_matches: Mapped[list[BloodCrossMatch]] = relationship(
        "BloodCrossMatch", back_populates="unit", cascade="all, delete-orphan"
    )
    issues: Mapped[list[BloodIssue]] = relationship(
        "BloodIssue", back_populates="unit", cascade="all, delete-orphan"
    )


class BloodComponentSeparation(Base):
    """Record of separation of whole blood into specific components (Feature 34)."""

    __tablename__ = "blood_bank_separations"
    __table_args__ = (
        UniqueConstraint("hospital_id", "separation_number", name="uq_blood_separation_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    separation_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    parent_unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("blood_bank_units.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    separated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    separated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    method: Mapped[str] = mapped_column(String(128), default="centrifugation", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class BloodCrossMatch(Base):
    """Authorized cross-match and reservation record (Feature 35 - Correction #10)."""

    __tablename__ = "blood_bank_cross_matches"
    __table_args__ = (
        UniqueConstraint("hospital_id", "cross_match_number", name="uq_blood_cross_match_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cross_match_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    blood_unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("blood_bank_units.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    admission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admissions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    patient_blood_group: Mapped[BloodGroup] = mapped_column(
        Enum(BloodGroup, name="blood_group", create_type=False), nullable=False
    )
    donor_blood_group: Mapped[BloodGroup] = mapped_column(
        Enum(BloodGroup, name="blood_group", create_type=False), nullable=False
    )
    major_crossmatch_result: Mapped[str] = mapped_column(String(32), nullable=False)
    minor_crossmatch_result: Mapped[str] = mapped_column(String(32), default="not_performed", nullable=False)
    coombs_test_result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    antibody_screen_result: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_compatible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    tested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    authorized_by: Mapped[str] = mapped_column(String(255), nullable=False)
    tested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    unit: Mapped[BloodUnit] = relationship("BloodUnit", back_populates="cross_matches")


class BloodIssue(Base):
    """Dispensing of blood unit to clinical area (Feature 36 - Correction #11)."""

    __tablename__ = "blood_bank_issues"
    __table_args__ = (
        UniqueConstraint("hospital_id", "issue_number", name="uq_blood_issue_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    issue_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    blood_unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("blood_bank_units.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    admission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admissions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    requisition_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_to: Mapped[str] = mapped_column(String(255), nullable=False)
    issued_by: Mapped[str] = mapped_column(String(255), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    transport_box_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[BloodIssueStatus] = mapped_column(
        Enum(BloodIssueStatus, name="blood_issue_status"), nullable=False, default=BloodIssueStatus.issued
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    unit: Mapped[BloodUnit] = relationship("BloodUnit", back_populates="issues")
    return_record: Mapped[BloodReturn | None] = relationship(
        "BloodReturn", back_populates="issue", uselist=False
    )


class BloodReturn(Base):
    """Return and disposition of issued blood unit (Feature 36 - Correction #11)."""

    __tablename__ = "blood_bank_returns"
    __table_args__ = (
        UniqueConstraint("hospital_id", "return_number", name="uq_blood_return_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    return_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    issue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("blood_bank_issues.id", ondelete="RESTRICT"), nullable=False, index=True, unique=True
    )
    blood_unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("blood_bank_units.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    returned_by: Mapped[str] = mapped_column(String(255), nullable=False)
    received_by: Mapped[str] = mapped_column(String(255), nullable=False)
    returned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    return_reason: Mapped[str] = mapped_column(Text, nullable=False)
    cold_chain_maintained: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    bag_intact: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    disposition: Mapped[BloodReturnDisposition] = mapped_column(
        Enum(BloodReturnDisposition, name="blood_return_disposition"), nullable=False
    )
    disposition_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    issue: Mapped[BloodIssue] = relationship("BloodIssue", back_populates="return_record")


class BloodTransfusion(Base):
    """Clinical administration record of an already-issued blood unit to a patient (Feature 37)."""

    __tablename__ = "blood_bank_transfusions"
    __table_args__ = (
        UniqueConstraint("hospital_id", "transfusion_number", name="uq_blood_transfusion_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    transfusion_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    issue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("blood_bank_issues.id", ondelete="RESTRICT"), nullable=False, index=True, unique=True
    )
    blood_unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("blood_bank_units.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    admission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admissions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_by_staff_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    completed_by_staff_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    pre_transfusion_vitals: Mapped[str | None] = mapped_column(Text, nullable=True)
    observations: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[BloodTransfusionStatus] = mapped_column(
        Enum(BloodTransfusionStatus, name="blood_transfusion_status"),
        nullable=False,
        default=BloodTransfusionStatus.in_progress,
        index=True,
    )
    adverse_reaction_occurred: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    adverse_reaction_type: Mapped[TransfusionAdverseReactionType | None] = mapped_column(
        Enum(TransfusionAdverseReactionType, name="transfusion_adverse_reaction_type"), nullable=True
    )
    adverse_reaction_details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    issue: Mapped[BloodIssue] = relationship("BloodIssue")
    unit: Mapped[BloodUnit] = relationship("BloodUnit")
