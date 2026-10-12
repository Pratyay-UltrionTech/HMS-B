"""Tenancy entities."""

from modules.tenancy.entities.callback_request import CallbackRequest
from modules.tenancy.entities.hospital import Hospital, PlanType
from modules.tenancy.entities.hospital_registration_request import (
    HospitalRegistrationRequest,
    RegistrationStatus,
)

__all__ = [
    "Hospital",
    "PlanType",
    "HospitalRegistrationRequest",
    "RegistrationStatus",
    "CallbackRequest",
]


