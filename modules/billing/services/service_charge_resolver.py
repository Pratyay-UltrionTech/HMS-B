"""Stable service ↔ charge linkage for billable clinical services.

Canonical charge identity (``BillingCharge.source_type`` + ``source_id``):

- Prescription-originated diagnostics: the prescription **request** id
  (``LabPrescriptionRequest`` / ``RadPrescriptionRequest``). The key never moves:
  orders created from the request resolve their charge through
  ``order.prescription_request_id``. One request → one charge, however many orders
  (radiology multi-scan, re-accession after an order cancel) fulfil it.
- Walk-in / direct / order-set orders (no request): the order id.
- Every other service type: the source id as given.

Legacy rows whose charge was re-pointed from the request to an order id still
resolve: a request also looks at the ids of its orders, and an order at its request.

Selection rule among candidates: an active (non-cancelled) charge wins, canonical key
first, newest first; when every candidate is cancelled the newest cancelled charge is
returned so callers can report ``cancelled``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingSourceType,
)

DIAGNOSTIC_SOURCE_TYPES = frozenset({BillingSourceType.laboratory, BillingSourceType.radiology})


@dataclass(frozen=True)
class ServiceChargeKey:
    """``canonical_source_id`` is where a new charge must be created; ``candidate_ids``
    (canonical first) is where an existing charge may live."""

    source_id: UUID
    canonical_source_id: UUID
    candidate_ids: tuple[UUID, ...]


def _diagnostic_models(source_type: BillingSourceType) -> tuple[Any, Any] | None:
    if source_type == BillingSourceType.laboratory:
        from modules.laboratory.entities.lab_entities import LabOrder, LabPrescriptionRequest

        return LabOrder, LabPrescriptionRequest
    if source_type == BillingSourceType.radiology:
        from modules.radiology.entities.radiology_entities import RadiologyOrder, RadPrescriptionRequest

        return RadiologyOrder, RadPrescriptionRequest
    return None


def service_charge_keys(
    db: Session,
    hospital_id: UUID,
    source_type: BillingSourceType,
    source_ids: list[UUID],
) -> dict[UUID, ServiceChargeKey]:
    """Bulk key resolution (at most four queries regardless of list length).

    A request's candidates are itself plus all its orders (newest first); an order of
    a request resolves like its request, with itself ranked right after the request."""
    ids = list(dict.fromkeys(sid for sid in source_ids if sid is not None))
    if not ids:
        return {}
    models = _diagnostic_models(source_type)
    if models is None:
        return {sid: ServiceChargeKey(sid, sid, (sid,)) for sid in ids}
    order_model, request_model = models

    order_parent: dict[UUID, UUID | None] = dict(
        db.query(order_model.id, order_model.prescription_request_id)
        .filter(order_model.hospital_id == hospital_id, order_model.id.in_(ids))
        .all()
    )
    request_ids = {sid for sid in ids if sid not in order_parent}
    request_ids.update(rid for rid in order_parent.values() if rid is not None)

    request_children: dict[UUID, list[tuple[Any, UUID]]] = {}
    legacy_link: dict[UUID, UUID] = {}
    if request_ids:
        for oid, rid, created in (
            db.query(order_model.id, order_model.prescription_request_id, order_model.ordered_at)
            .filter(
                order_model.hospital_id == hospital_id,
                order_model.prescription_request_id.in_(list(request_ids)),
            )
            .all()
        ):
            request_children.setdefault(rid, []).append((created, oid))
        if hasattr(request_model, "lab_order_id"):
            for rid, linked in (
                db.query(request_model.id, request_model.lab_order_id)
                .filter(request_model.hospital_id == hospital_id, request_model.id.in_(list(request_ids)))
                .all()
            ):
                if linked is not None:
                    legacy_link[rid] = linked

    def _children(rid: UUID) -> list[UUID]:
        out = [
            oid
            for _, oid in sorted(
                request_children.get(rid, []),
                key=lambda t: (t[0] is not None, t[0].timestamp() if t[0] is not None else 0.0),
                reverse=True,
            )
        ]
        if rid in legacy_link and legacy_link[rid] not in out:
            out.append(legacy_link[rid])
        return out

    keys: dict[UUID, ServiceChargeKey] = {}
    for sid in ids:
        if sid in order_parent:
            parent = order_parent[sid]
            if parent is None:
                keys[sid] = ServiceChargeKey(sid, sid, (sid,))
            else:
                siblings = [oid for oid in _children(parent) if oid != sid]
                keys[sid] = ServiceChargeKey(sid, parent, (parent, sid, *siblings))
            continue
        keys[sid] = ServiceChargeKey(sid, sid, tuple([sid, *_children(sid)]))
    return keys


def service_charge_key(
    db: Session, hospital_id: UUID, source_type: BillingSourceType, source_id: UUID
) -> ServiceChargeKey:
    return service_charge_keys(db, hospital_id, source_type, [source_id])[source_id]


def canonical_charge_source_id(
    db: Session, hospital_id: UUID, source_type: BillingSourceType, source_id: UUID
) -> UUID:
    """Stable identity under which the charge for this service is (or will be) created."""
    return service_charge_key(db, hospital_id, source_type, source_id).canonical_source_id


def _pick(charges: list[BillingCharge], candidate_ids: tuple[UUID, ...]) -> BillingCharge | None:
    if not charges:
        return None
    rank = {cid: i for i, cid in enumerate(candidate_ids)}
    active = [c for c in charges if c.status != BillingChargeStatus.cancelled]
    if active:
        active.sort(key=lambda c: (rank.get(c.source_id, len(rank)), -_ts(c)))
        return active[0]
    return max(charges, key=_ts)


def _ts(charge: BillingCharge) -> float:
    created = getattr(charge, "created_at", None)
    return created.timestamp() if created is not None else 0.0


def resolve_service_charges(
    db: Session,
    hospital_id: UUID,
    source_type: BillingSourceType,
    source_ids: list[UUID],
) -> dict[UUID, BillingCharge | None]:
    """Bulk ``resolve_service_charge``: one charge query for the whole list."""
    keys = service_charge_keys(db, hospital_id, source_type, source_ids)
    all_candidates = {cid for key in keys.values() for cid in key.candidate_ids}
    by_source: dict[UUID, list[BillingCharge]] = {}
    if all_candidates:
        for charge in (
            db.query(BillingCharge)
            .filter(
                BillingCharge.hospital_id == hospital_id,
                BillingCharge.source_type == source_type,
                BillingCharge.source_id.in_(list(all_candidates)),
            )
            .all()
        ):
            by_source.setdefault(charge.source_id, []).append(charge)
    result: dict[UUID, BillingCharge | None] = {}
    for sid, key in keys.items():
        pool = [c for cid in key.candidate_ids for c in by_source.get(cid, [])]
        result[sid] = _pick(pool, key.candidate_ids)
    return result


def resolve_service_charge(
    db: Session,
    hospital_id: UUID,
    source_type: BillingSourceType,
    source_id: UUID,
) -> BillingCharge | None:
    """The charge that bills this service (order or request id accepted)."""
    return resolve_service_charges(db, hospital_id, source_type, [source_id]).get(source_id)


def resolve_active_service_charge(
    db: Session,
    hospital_id: UUID,
    source_type: BillingSourceType,
    source_id: UUID,
) -> BillingCharge | None:
    charge = resolve_service_charge(db, hospital_id, source_type, source_id)
    if charge is None or charge.status == BillingChargeStatus.cancelled:
        return None
    return charge


def ensure_service_charge(
    db: Session,
    *,
    hospital_id: UUID,
    patient_id: UUID,
    source_type: BillingSourceType,
    source_id: UUID,
    description: str,
    charge_amount: float,
    account_id: UUID | None = None,
    created_by_name: str = "System",
) -> BillingCharge:
    """Idempotent by stable key: return the active charge of the service, else create
    one on its canonical key. Never re-points an existing charge. Does not commit."""
    from modules.billing.services.billing_service import ensure_charge

    key = service_charge_key(db, hospital_id, source_type, source_id)
    existing = resolve_active_service_charge(db, hospital_id, source_type, source_id)
    if existing is not None:
        return existing
    return ensure_charge(
        db,
        hospital_id=hospital_id,
        patient_id=patient_id,
        source_type=source_type,
        source_id=key.canonical_source_id,
        description=description,
        charge_amount=charge_amount,
        account_id=account_id,
        created_by_name=created_by_name,
    )


def cancel_service_charges_or_400(
    db: Session,
    hospital_id: UUID,
    source_type: BillingSourceType,
    source_id: UUID,
    *,
    reason: str,
    actor: dict[str, Any] | None = None,
    performed: bool = False,
) -> list:
    """Cancel the charge(s) owned by a service: a prescription request (its canonical
    charge plus any legacy copy re-pointed to one of its orders) or a walk-in order.
    An order fulfilling a request owns no charge of its own, so nothing is cancelled
    for it. Refusal (service already performed) surfaces as HTTP 400."""
    from fastapi import HTTPException, status

    from modules.billing.services.charge_reversal_service import cancel_charge
    from shared.exceptions.base import ValidationError

    key = service_charge_key(db, hospital_id, source_type, source_id)
    if key.canonical_source_id != source_id:
        return []
    charges = (
        db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.source_type == source_type,
            BillingCharge.source_id.in_(list(key.candidate_ids)),
            BillingCharge.status != BillingChargeStatus.cancelled,
        )
        .order_by(BillingCharge.created_at.asc())
        .all()
    )
    try:
        return [cancel_charge(db, c, reason=reason, actor=actor, performed=performed) for c in charges]
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc


def sync_service_charge_amount(
    db: Session,
    *,
    hospital_id: UUID,
    patient_id: UUID,
    source_type: BillingSourceType,
    source_id: UUID,
    charge_amount: float,
    description: str,
    reason: str,
    actor: dict[str, Any] | None = None,
    account_id: UUID | None = None,
    created_by_name: str = "System",
) -> BillingCharge | None:
    """Make the service's single canonical charge bill ``charge_amount``.

    - no active charge and amount > 0 → create it on the canonical key;
    - amount <= 0 → cancel it canonically (paid money → credit / back to deposit);
    - otherwise re-price in place, keeping discount/tax, and release anything applied
      beyond the new net into credit (``reconcile_charge_after_reprice``).
    An active charge with no account adopts ``account_id`` (episode account repair).
    """
    from modules.billing.services.billing_service import compute_net
    from modules.billing.services.charge_reversal_service import (
        cancel_charge,
        reconcile_charge_after_reprice,
    )

    amount = round(max(0.0, float(charge_amount or 0)), 2)
    charge = resolve_active_service_charge(db, hospital_id, source_type, source_id)
    if charge is None:
        if amount <= 0:
            return None
        return ensure_service_charge(
            db,
            hospital_id=hospital_id,
            patient_id=patient_id,
            source_type=source_type,
            source_id=source_id,
            description=description,
            charge_amount=amount,
            account_id=account_id,
            created_by_name=created_by_name,
        )
    if amount <= 0:
        cancel_charge(db, charge, reason=reason, actor=actor)
        return charge

    if charge.account_id is None and account_id is not None:
        charge.account_id = account_id
    charge.description = description.strip()[:512]
    old_net = round(float(charge.net_amount or 0), 2)
    if round(float(charge.charge_amount or 0), 2) == amount:
        return charge
    _, net = compute_net(
        amount,
        float(charge.discount_amount or 0),
        charge.discount_percent,
        float(charge.tax_amount or 0),
    )
    qty = max(1.0, float(charge.quantity or 1))
    charge.charge_amount = amount
    charge.unit_price = round(amount / qty, 2)
    charge.net_amount = net
    reconcile_charge_after_reprice(
        db,
        charge,
        reason=f"{reason} (₹{old_net:.2f} → ₹{net:.2f})",
        actor=actor,
    )
    return charge
