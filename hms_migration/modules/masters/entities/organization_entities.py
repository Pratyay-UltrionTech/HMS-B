"""
Target-native SQLAlchemy entities for Masters Organization domain.

Tables:
- wings
- departments
"""

from __future__ import annotations

from datetime import date, datetime
import uuid

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hms_migration.infrastructure.postgres.base import Base


class Wing(Base):
    """Hospital physical wing / building block."""

    __tablename__ = "wings"
    __table_args__ = (
        UniqueConstraint("hospital_id", "name", name="uq_wing_hospital_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    head_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    desk_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mobile: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    departments: Mapped[list["Department"]] = relationship(back_populates="wing")


class Department(Base):
    """Clinical or administrative department within hospital."""

    __tablename__ = "departments"
    __table_args__ = (
        UniqueConstraint("hospital_id", "name", name="uq_dept_hospital_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    wing_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wings.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    head_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    desk_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mobile: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    wing: Mapped["Wing | None"] = relationship(back_populates="departments")


class Supplier(Base):
    """General hospital goods / services supplier.

    This is the enterprise-canonical vendor master. Feature 53 (Vendor Master,
    modules/procurement) does NOT create a separate vendor/supplier table — all
    procurement entities (PurchaseOrder, GoodsReceivedNote) reference this
    `Supplier` row via FK. The fields below (payment_terms, tax_id, contract
    dates, supplied_categories) were added additively as nullable columns to
    support procurement use cases without altering or breaking any existing
    row or caller of this entity. `modules/pharmacy`'s `PharmacySupplier` and
    this `Supplier` remain intentionally separate (pharmacy-scoped vs general
    hospital procurement) — consolidating them is a larger follow-up not
    undertaken here."""

    __tablename__ = "suppliers"
    __table_args__ = (
        UniqueConstraint("hospital_id", "name", name="uq_supplier_hospital_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_person: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # ── Additive columns for Feature 53 (Vendor Master / procurement) ───────
    payment_terms: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tax_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # GSTIN / tax registration no.
    contract_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    contract_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    supplied_categories: Mapped[str | None] = mapped_column(Text, nullable=True)  # comma-separated free text
