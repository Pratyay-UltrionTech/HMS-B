"""
Admission Financial Policy Contracts.

Configurable per-hospital policy controlling IPD admission advance deposits,
bed allocation financial gates, and calculation rules.
"""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class AdmissionFinancialPolicy(BaseModel):
    """Hospital-scoped admission financial and advance deposit policy."""

    financial_gate_enabled: bool = Field(
        default=True,
        description="Whether financial clearance is enforced before first bed allocation.",
    )
    advance_mode: Literal["mandatory", "optional", "waived"] = Field(
        default="mandatory",
        description="Deposit requirement mode: mandatory (blocks allocation if shortfall), optional (suggests advance), waived (zero advance required).",
    )
    minimum_advance_amount: float = Field(
        default=0.0,
        ge=0.0,
        description="Minimum fixed deposit required. If 0, uses computed tariff + fee.",
    )
    include_admission_fee: bool = Field(
        default=True,
        description="Whether to include the ward's admission fee in the advance calculation.",
    )
    include_first_day_bed_tariff: bool = Field(
        default=True,
        description="Whether to include the first day's bed tariff in the advance calculation.",
    )
    emergency_bypass_allowed: bool = Field(
        default=True,
        description="Whether emergency admissions and structured clinical overrides can bypass the deposit gate.",
    )

    model_config = ConfigDict(from_attributes=True)


class AdmissionFinancialPolicyUpdate(BaseModel):
    """Payload to update hospital-scoped admission financial policy."""

    financial_gate_enabled: bool | None = None
    advance_mode: Literal["mandatory", "optional", "waived"] | None = None
    minimum_advance_amount: float | None = Field(default=None, ge=0.0)
    include_admission_fee: bool | None = None
    include_first_day_bed_tariff: bool | None = None
    emergency_bypass_allowed: bool | None = None
