"""Shared phone / mobile number validation (exactly 10 digits)."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator

_PHONE_RE = re.compile(r"^\d{10}$")
_NON_DIGIT = re.compile(r"\D+")


def normalize_phone(value: str) -> str:
    digits = _NON_DIGIT.sub("", (value or "").strip())
    if not _PHONE_RE.fullmatch(digits):
        raise ValueError("must be exactly 10 digits")
    return digits


def normalize_optional_phone(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return normalize_phone(stripped)


PhoneNumber = Annotated[str, AfterValidator(normalize_phone)]
OptionalPhoneNumber = Annotated[str | None, AfterValidator(normalize_optional_phone)]