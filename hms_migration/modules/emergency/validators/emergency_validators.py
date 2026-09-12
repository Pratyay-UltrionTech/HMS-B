"""
Emergency domain input validators and state transition guards.

Conforms to UltrionTech-Backend-Template modules/emergency/validators/ specification.
"""

from __future__ import annotations

from fastapi import HTTPException, status

from hms_migration.modules.emergency.entities.emergency_entities import (
    EmergencyDispositionType,
    EmergencyEncounter,
    EmergencyStatus,
)


def assert_can_triage(encounter: EmergencyEncounter) -> None:
    """Ensure encounter is eligible for triage assessment."""
    if encounter.status in (EmergencyStatus.completed, EmergencyStatus.cancelled):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot triage encounter in terminal status: {encounter.status.value}",
        )


def assert_can_order_treatment(encounter: EmergencyEncounter) -> None:
    """Ensure encounter is open for treatment orders."""
    if encounter.status in (EmergencyStatus.completed, EmergencyStatus.cancelled):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot place orders on a closed emergency encounter",
        )


def assert_can_set_disposition(encounter: EmergencyEncounter) -> None:
    """Ensure encounter can be given a disposition."""
    if encounter.status in (EmergencyStatus.completed, EmergencyStatus.cancelled):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Disposition already finalized or encounter is cancelled",
        )


def validate_disposition_payload(
    disposition_type: EmergencyDispositionType,
    destination_ward_id: object,
    destination_bed_id: object,
    transfer_facility: str | None,
) -> None:
    """Validate required destination parameters based on disposition type."""
    if disposition_type in (EmergencyDispositionType.admit_ipd, EmergencyDispositionType.admit_icu):
        if not destination_ward_id or not destination_bed_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Destination ward and bed are required for IPD/ICU admission disposition",
            )
    elif disposition_type == EmergencyDispositionType.transfer_tertiary:
        if not transfer_facility or not transfer_facility.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Transfer facility name is required for tertiary hospital transfer",
            )
