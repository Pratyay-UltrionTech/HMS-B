"""Shared holiday matching and resolution service."""
from __future__ import annotations

import calendar
from datetime import date
from uuid import UUID

from sqlalchemy.orm import Session

from modules.doctors.entities.doctor import Holiday


def holiday_matches_date(
    holiday_or_date: Holiday | date,
    is_recurring_or_check_date: bool | date,
    check_date: date | None = None,
) -> bool:
    """Check if a holiday applies to the specified check_date.

    Supports:
    - holiday_matches_date(holiday_instance, check_date)
    - holiday_matches_date(holiday_date, is_recurring, check_date)

    Rules:
    - Direct match: holiday_date == check_date
    - Recurring match: is_recurring is True AND month/day match
    - Feb 29 recurring rule:
      Feb 29 matches only Feb 29 in leap years and does not automatically observe on Feb 28.
    """
    if isinstance(holiday_or_date, Holiday):
        h_date = holiday_or_date.holiday_date
        recurring = bool(holiday_or_date.is_recurring)
        chk_date = is_recurring_or_check_date  # type: ignore[assignment]
    else:
        h_date = holiday_or_date
        recurring = bool(is_recurring_or_check_date)
        chk_date = check_date  # type: ignore[assignment]

    if h_date == chk_date:
        return True
    if not recurring:
        return False
    if h_date.month == chk_date.month and h_date.day == chk_date.day:
        return True
    return False


def get_effective_holiday(db: Session, hospital_id: UUID, check_date: date) -> Holiday | None:
    """Fetch the effective holiday for the given date, accounting for recurring holidays."""
    holidays = (
        db.query(Holiday)
        .filter(Holiday.hospital_id == hospital_id)
        .order_by(Holiday.holiday_date.desc(), Holiday.name)
        .all()
    )
    for h in holidays:
        if holiday_matches_date(h.holiday_date, bool(h.is_recurring), check_date):
            return h
    return None
