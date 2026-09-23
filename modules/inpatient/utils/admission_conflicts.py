"""Structured 409 conflict contract for admission / bed contention.

Every duplicate-admission or bed-taken site raises
``fastapi.HTTPException(status_code=409, detail={...})`` with this shape::

    {"message": str, "code": "ADMISSION_CONFLICT" | "BED_OCCUPIED" | "WRONG_STATE",
     "admission_id": str | None, "status": str | None,
     "bed_id": str | None, "doctor_id": str | None}

The global ``ConflictError`` → 400 mapping is intentionally left unchanged.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status

ADMISSION_CONFLICT = "ADMISSION_CONFLICT"
BED_OCCUPIED = "BED_OCCUPIED"
WRONG_STATE = "WRONG_STATE"


def admission_conflict(
    message: str,
    admission_id: UUID | str | None = None,
    admission_status: Any | None = None,
    bed_id: UUID | str | None = None,
    doctor_id: UUID | str | None = None,
    code: str = ADMISSION_CONFLICT,
) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "message": message,
            "code": code,
            "admission_id": str(admission_id) if admission_id else None,
            "status": (
                admission_status.value
                if hasattr(admission_status, "value")
                else (str(admission_status) if admission_status else None)
            ),
            "bed_id": str(bed_id) if bed_id else None,
            "doctor_id": str(doctor_id) if doctor_id else None,
        },
    )


def bed_conflict(
    message: str,
    bed_id: UUID | str | None = None,
    admission_id: UUID | str | None = None,
    admission_status: Any | None = None,
) -> HTTPException:
    return admission_conflict(
        message,
        admission_id=admission_id,
        admission_status=admission_status,
        bed_id=bed_id,
        code=BED_OCCUPIED,
    )
