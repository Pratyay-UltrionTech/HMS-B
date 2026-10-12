"""
Canonical authorization for financial emergency overrides and financial exceptions.

Every path that bypasses a financial gate (standalone emergency-override endpoint,
inline Lab / Radiology / OT / Pharmacy overrides, bed accept / allocate overrides,
emergency / ICU admissions) must authorize through this module. Authorization is
based only on real role permissions evaluated by ``shared.auth.service.authorization``
(admin bypass included); display names, staff-role labels and raw request booleans
never grant anything.

Permission model:
- Emergency financial override: any of ``EMERGENCY_OVERRIDE_PERMISSIONS``.
  ``emergency:approve`` is the dedicated key; ``all_ipd:approve`` (IPD clinical
  approver) and ``billing:approve`` (financial approver) also qualify.
- Approving / rejecting a financial exception: ``billing:approve`` plus
  requester != approver (separation of duties).

Every actor must resolve to an active ``HospitalUser`` of the same hospital, because
exception rows reference ``hospital_users`` and must be attributable to a person.
Institutional hospital-admin logins (no staff user id) are therefore refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

EMERGENCY_OVERRIDE_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("emergency", "approve"),
    ("all_ipd", "approve"),
    ("billing", "approve"),
)
EXCEPTION_APPROVAL_PERMISSION: tuple[str, str] = ("billing", "approve")
MIN_OVERRIDE_REASON_LENGTH = 5

# Service types answered by bed-allocation clearance; subject to the admission policy
# flag ``emergency_bypass_allowed``.
BED_SCOPE_SERVICE_TYPES: frozenset[str] = frozenset(
    {"admission_bed", "ipd_bed_allocation", "bed_allocation", "admission"}
)
# Service types whose scope is the admission episode (admission_id required).
ADMISSION_SCOPE_SERVICE_TYPES: frozenset[str] = BED_SCOPE_SERVICE_TYPES | {"discharge"}


def _service_scope_types() -> frozenset[str]:
    from modules.billing.entities.billing_entities import BillingSourceType

    return frozenset(st.value for st in BillingSourceType) - {"admission"}


@dataclass(frozen=True)
class OverrideScope:
    """What an override / exception unlocks.

    Service scope: one billable source (``service_type`` + ``source_id``, e.g. a lab
    request or OT surgery). Admission scope: one admission episode (``admission_id``).
    A scope never covers "any source of this service type".
    """

    service_type: str
    patient_id: UUID | None = None
    source_id: UUID | None = None
    admission_id: UUID | None = None
    action_type: str = "emergency_execution"

    @classmethod
    def service(
        cls,
        service_type: str,
        source_id: UUID | None,
        *,
        patient_id: UUID | None = None,
        admission_id: UUID | None = None,
        action_type: str = "emergency_execution",
    ) -> "OverrideScope":
        return cls(
            service_type=service_type,
            patient_id=patient_id,
            source_id=source_id,
            admission_id=admission_id,
            action_type=action_type,
        )

    @classmethod
    def admission(
        cls,
        admission_id: UUID | None,
        *,
        patient_id: UUID | None = None,
        service_type: str = "admission_bed",
        action_type: str = "bed_allocation",
        source_id: UUID | None = None,
    ) -> "OverrideScope":
        return cls(
            service_type=service_type,
            patient_id=patient_id,
            source_id=source_id,
            admission_id=admission_id,
            action_type=action_type,
        )

    @property
    def is_admission_scope(self) -> bool:
        return self.service_type in ADMISSION_SCOPE_SERVICE_TYPES

    @property
    def is_bed_scope(self) -> bool:
        return self.service_type in BED_SCOPE_SERVICE_TYPES

    def validate(self) -> None:
        """Raise 400 unless the scope is explicit enough to never match unrelated work."""
        service_type = (self.service_type or "").strip()
        if not service_type:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Financial override scope requires a service_type.",
            )
        if self.is_admission_scope:
            if not self.admission_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"'{service_type}' overrides and exceptions must be scoped to an admission_id.",
                )
            return
        if service_type not in _service_scope_types():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown financial override service_type '{service_type}'.",
            )
        if not self.source_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"'{service_type}' overrides and exceptions must be scoped to a specific "
                    "order / request (source_id is required)."
                ),
            )


@dataclass(frozen=True)
class OverrideActor:
    """An authenticated, attributable staff user and the permission that authorized them."""

    user_id: UUID
    name: str
    claims: dict[str, Any] = field(default_factory=dict)
    granted_by: str | None = None

    def audit_fields(self) -> dict[str, Any]:
        return {
            "actor_user_id": str(self.user_id),
            "actor_name": self.name,
            "actor_role": self.claims.get("role"),
            "authorized_by_permission": self.granted_by,
        }


def require_override_reason(reason: str | None, *, label: str = "Emergency override reason") -> str:
    text = (reason or "").strip()
    if len(text) < MIN_OVERRIDE_REASON_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{label} is mandatory (at least {MIN_OVERRIDE_REASON_LENGTH} characters).",
        )
    return text


def _claimed_user_id(user: dict[str, Any]) -> UUID | None:
    for key in ("user_id", "id", "sub"):
        raw = user.get(key)
        if not raw:
            continue
        try:
            return UUID(str(raw))
        except (ValueError, TypeError):
            continue
    return None


def _normalized_claims(user: dict[str, Any], hospital_id: UUID) -> dict[str, Any]:
    claims = dict(user)
    claimed_hospital = claims.get("hospital_uuid")
    if claimed_hospital and str(claimed_hospital) != str(hospital_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: actor does not belong to this hospital.",
        )
    claims["hospital_uuid"] = str(hospital_id)
    perms = claims.get("permissions")
    if isinstance(perms, (list, tuple, set)):
        # Legacy "module:action" string lists → the {module: {flag: bool}} claim shape.
        from shared.auth.registry import parse_action

        mapped: dict[str, dict[str, bool]] = {}
        for spec in perms:
            if isinstance(spec, str) and spec.strip():
                module_key, _action, flag = parse_action(spec)
                mapped.setdefault(module_key, {})[flag] = True
        claims["permissions"] = mapped
    elif perms is not None and not isinstance(perms, dict):
        claims["permissions"] = {}
    return claims


def resolve_actor(db: Session, hospital_id: UUID, user: dict[str, Any] | None) -> OverrideActor:
    """Resolve the caller to an active staff user of ``hospital_id`` (401 / 403 otherwise)."""
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authenticated actor required for financial exceptions and overrides.",
        )
    user_id = _claimed_user_id(user)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Financial exceptions and overrides must be performed by an identified staff "
                "user account; this login has no staff user id."
            ),
        )
    from modules.doctors.entities.doctor import HospitalUser

    staff = (
        db.query(HospitalUser)
        .filter(HospitalUser.id == user_id, HospitalUser.hospital_id == hospital_id)
        .first()
    )
    if staff is None or not staff.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: actor is not an active staff user of this hospital.",
        )
    claims = _normalized_claims(user, hospital_id)
    claims.setdefault("user_id", str(user_id))
    return OverrideActor(user_id=user_id, name=staff.name or str(user.get("name") or "Staff"), claims=claims)


def _first_granted_permission(
    db: Session, claims: dict[str, Any], specs: tuple[tuple[str, str], ...]
) -> str | None:
    from shared.auth.service import AuthReasonCode, authorization

    for spec in specs:
        decision = authorization.evaluate(claims, spec, resource=None, db=db)
        if decision.allowed:
            if decision.reason_code == AuthReasonCode.ADMIN_BYPASS:
                return str(claims.get("role") or "admin")
            return f"{spec[0]}:{spec[1]}"
    return None


def has_emergency_override_permission(
    db: Session, hospital_id: UUID, user: dict[str, Any] | None
) -> bool:
    """Non-raising check used where emergency priority may imply an override request."""
    try:
        actor = resolve_actor(db, hospital_id, user)
    except HTTPException:
        return False
    return _first_granted_permission(db, actor.claims, EMERGENCY_OVERRIDE_PERMISSIONS) is not None


def assert_emergency_bypass_allowed(db: Session, hospital_id: UUID, scope: OverrideScope) -> None:
    """Enforce the facility ``emergency_bypass_allowed`` flag for bed / admission overrides.

    Service overrides (life-safety diagnostics, medication, surgery) are governed by
    the permission model only; the flag belongs to the admission financial policy.
    """
    if not scope.is_bed_scope:
        return
    from modules.billing.services.service_financial_clearance import get_admission_financial_policy

    policy = get_admission_financial_policy(db, hospital_id)
    if not policy.emergency_bypass_allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Emergency financial bypass for admissions is disabled by this hospital's "
                "admission financial policy. Collect the advance or request a financial exception."
            ),
        )


def authorize_emergency_override(
    db: Session,
    hospital_id: UUID,
    user: dict[str, Any] | None,
    *,
    scope: OverrideScope,
    reason: str | None,
) -> OverrideActor:
    """Authorize an emergency financial override for ``scope``.

    Raises 400 (missing reason / unscoped), 401/403 (unidentified or unauthorized
    actor) or 403 (facility policy forbids emergency bed bypass).
    """
    scope.validate()
    require_override_reason(reason)
    actor = resolve_actor(db, hospital_id, user)
    granted = _first_granted_permission(db, actor.claims, EMERGENCY_OVERRIDE_PERMISSIONS)
    if granted is None:
        allowed = ", ".join(f"{m}:{a}" for m, a in EMERGENCY_OVERRIDE_PERMISSIONS)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Forbidden: '{actor.name}' is not authorized to grant emergency financial "
                f"overrides (requires one of: {allowed})."
            ),
        )
    assert_emergency_bypass_allowed(db, hospital_id, scope)
    return OverrideActor(user_id=actor.user_id, name=actor.name, claims=actor.claims, granted_by=granted)


def authorize_exception_approval(
    db: Session, hospital_id: UUID, user: dict[str, Any] | None
) -> OverrideActor:
    """Authorize approving / rejecting a financial exception (``billing:approve``)."""
    actor = resolve_actor(db, hospital_id, user)
    granted = _first_granted_permission(db, actor.claims, (EXCEPTION_APPROVAL_PERMISSION,))
    if granted is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: approving financial exceptions requires billing:approve.",
        )
    return OverrideActor(user_id=actor.user_id, name=actor.name, claims=actor.claims, granted_by=granted)
