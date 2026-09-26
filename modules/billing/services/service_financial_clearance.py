"""
Service Financial Clearance infrastructure.

Determines whether a specific billable service (e.g. LabOrder, RadiologyOrder)
is financially cleared based on its linked BillingCharge (source_type, source_id).
Conforms to healthcare service gating requirements without using simplistic patient-level flags.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingSourceType,
)
from modules.billing.entities.financial_exception import (
    FinancialClearanceException,
    FinancialExceptionStatus,
)
from modules.billing.contracts.policy_contracts import (
    AdmissionFinancialPolicy,
    AdmissionFinancialPolicyUpdate,
)


def validate_emergency_override_actor(
    actor: dict[str, Any] | None,
    reason: str | None,
    service_type: str = "general",
) -> tuple[UUID | None, str]:
    """
    Validate that an emergency override is accompanied by a mandatory reason
    and that the actor is authorized (admin, physician/doctor/surgeon, clinical supervisor,
    or explicit override permission). Raises 400 or 403 on invalid invocation.
    """
    if not reason or not reason.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Emergency override reason is mandatory when executing an emergency financial override.",
        )

    actor_dict = actor or {}
    role = str(actor_dict.get("role") or "").lower()
    staff_role = str(actor_dict.get("staff_role_name") or "").lower()
    permissions = actor_dict.get("permissions") or []

    is_admin = role in {"super_admin", "hospital_admin"}
    actor_name_lower = str(actor_dict.get("name") or "").lower()
    is_doctor = (
        role in {"doctor", "physician", "surgeon"}
        or "doctor" in staff_role
        or "physician" in staff_role
        or "surgeon" in staff_role
        or actor_dict.get("is_doctor") is True
        or "doctor" in actor_name_lower
        or actor_name_lower.startswith("dr.")
        or actor_name_lower.startswith("dr ")
    )
    is_clinical_lead = (
        "supervisor" in staff_role
        or "in-charge" in staff_role
        or "charge" in staff_role
        or "head" in staff_role
        or role in {"nurse_supervisor", "clinical_supervisor"}
    )
    has_explicit_perm = any(
        p in permissions
        for p in [
            "billing:approve",
            "all_ipd:approve",
            "emergency:administer",
            "emergency:create",
            "emergency:edit",
            "doctors:edit",
        ]
    )

    if not (is_admin or is_doctor or is_clinical_lead or has_explicit_perm):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Forbidden: Actor '{actor_dict.get('name') or actor_dict.get('sub')}' with role '{staff_role or role}' is not authorized to grant emergency financial overrides.",
        )

    actor_id = None
    actor_id_str = actor_dict.get("id") or actor_dict.get("sub") or actor_dict.get("user_id")
    if actor_id_str:
        try:
            actor_id = UUID(str(actor_id_str))
        except (ValueError, TypeError):
            actor_id = None

    actor_name = str(actor_dict.get("name") or "Authorized Emergency Clinician")
    return actor_id, actor_name


def get_admission_financial_policy(db: Session, hospital_id: UUID) -> AdmissionFinancialPolicy:
    from modules.tenancy.entities.hospital import Hospital
    hosp = db.query(Hospital).filter(Hospital.id == hospital_id).first()
    if not hosp:
        raise HTTPException(status_code=404, detail="Hospital not found")
    settings = hosp.facility_settings or {}
    stored = settings.get("admission_financial_policy") or settings.get("admission_advance_policy") or {}
    return AdmissionFinancialPolicy(
        financial_gate_enabled=stored.get("financial_gate_enabled", stored.get("enabled", True)),
        advance_mode=stored.get("advance_mode", "mandatory"),
        minimum_advance_amount=float(stored.get("minimum_advance_amount", stored.get("fixed_minimum_amount", 0.0)) or 0.0),
        include_admission_fee=stored.get("include_admission_fee", True),
        include_first_day_bed_tariff=stored.get("include_first_day_bed_tariff", True),
        emergency_bypass_allowed=stored.get("emergency_bypass_allowed", True),
    )


def set_admission_financial_policy(
    db: Session,
    hospital_id: UUID,
    payload: AdmissionFinancialPolicyUpdate,
    actor: dict[str, Any],
) -> AdmissionFinancialPolicy:
    from modules.tenancy.entities.hospital import Hospital
    hosp = db.query(Hospital).filter(Hospital.id == hospital_id).first()
    if not hosp:
        raise HTTPException(status_code=404, detail="Hospital not found")
    settings = dict(hosp.facility_settings or {})
    current = dict(settings.get("admission_financial_policy") or settings.get("admission_advance_policy") or {})

    update_dict = payload.model_dump(exclude_unset=True)
    current.update(update_dict)
    settings["admission_financial_policy"] = current
    settings["admission_advance_policy"] = current
    hosp.facility_settings = settings
    db.commit()
    db.refresh(hosp)

    from shared.audit.service import write_audit_log
    write_audit_log(
        db,
        hospital_id=hospital_id,
        actor=actor,
        action="update",
        entity_type="admission_financial_policy",
        entity_id=hospital_id,
        summary=f"Updated admission financial policy: mode={current.get('advance_mode')}, min={current.get('minimum_advance_amount')}",
    )
    return get_admission_financial_policy(db, hospital_id)


@dataclass(frozen=True)
class BedAllocationClearanceState:
    is_cleared: bool
    status: str  # "financially_cleared", "payment_required", "exception_pending", "exception_approved", "emergency_override"
    required_advance: float
    admission_fee: float
    bed_charge_per_day: float
    paid_or_allocated_amount: float
    available_deposit: float
    shortfall: float
    ipd_account_id: UUID | None
    current_ipd_outstanding: float
    historical_patient_balance: float
    active_exception_id: UUID | None
    active_exception_status: str | None
    reason: str


@dataclass(frozen=True)
class ServiceFinancialState:
    charge_exists: bool
    charge_id: UUID | None
    status: str  # "paid", "partially_paid", "pending", "cancelled", "unbilled"
    net_amount: float
    amount_paid: float
    outstanding_amount: float
    is_cleared: bool
    reason: str


def check_service_financial_clearance(
    db: Session,
    hospital_id: UUID,
    source_type: BillingSourceType,
    source_id: UUID,
) -> ServiceFinancialState:
    """
    Evaluate the authoritative financial clearance state of a specific service.
    
    Rules:
    - If no active non-cancelled charge exists:
        treated as unbilled / not cleared.
    - If charge is cancelled:
        not cleared, service cancelled.
    - If net_amount <= 0.0 or charge.status == BillingChargeStatus.paid:
        cleared (e.g. 0-cost, 100% discount, or fully paid).
    - If charge.status == BillingChargeStatus.partially_paid:
        NOT automatically cleared; requires full settlement unless net <= amount_paid.
    - If charge.status == BillingChargeStatus.pending:
        NOT cleared.
    """
    charge = (
        db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.source_type == source_type,
            BillingCharge.source_id == source_id,
        )
        .order_by(BillingCharge.created_at.desc())
        .first()
    )

    if not charge:
        # Fallback cross-link resolution between orders and prescription requests
        try:
            if source_type == BillingSourceType.laboratory:
                from modules.laboratory.entities.lab_entities import LabOrder, LabPrescriptionRequest
                # If source_id is LabOrder, check linked prescription request
                order = db.query(LabOrder).filter(LabOrder.id == source_id, LabOrder.hospital_id == hospital_id).first()
                if order and order.prescription_request_id:
                    charge = (
                        db.query(BillingCharge)
                        .filter(
                            BillingCharge.hospital_id == hospital_id,
                            BillingCharge.source_type == source_type,
                            BillingCharge.source_id == order.prescription_request_id,
                        )
                        .order_by(BillingCharge.created_at.desc())
                        .first()
                    )
                elif not order:
                    # If source_id is LabPrescriptionRequest, check linked LabOrder (either via req.lab_order_id or LabOrder.prescription_request_id)
                    req = db.query(LabPrescriptionRequest).filter(LabPrescriptionRequest.id == source_id, LabPrescriptionRequest.hospital_id == hospital_id).first()
                    linked_lab_order_id = req.lab_order_id if req else None
                    if not linked_lab_order_id:
                        lab_o = db.query(LabOrder).filter(LabOrder.prescription_request_id == source_id, LabOrder.hospital_id == hospital_id).first()
                        if lab_o:
                            linked_lab_order_id = lab_o.id
                    if linked_lab_order_id:
                        charge = (
                            db.query(BillingCharge)
                            .filter(
                                BillingCharge.hospital_id == hospital_id,
                                BillingCharge.source_type == source_type,
                                BillingCharge.source_id == linked_lab_order_id,
                            )
                            .order_by(BillingCharge.created_at.desc())
                            .first()
                        )
            elif source_type == BillingSourceType.radiology:
                from modules.radiology.entities.radiology_entities import RadiologyOrder, RadPrescriptionRequest
                # If source_id is RadiologyOrder, check linked prescription request
                rad_order = db.query(RadiologyOrder).filter(RadiologyOrder.id == source_id, RadiologyOrder.hospital_id == hospital_id).first()
                if rad_order and rad_order.prescription_request_id:
                    charge = (
                        db.query(BillingCharge)
                        .filter(
                            BillingCharge.hospital_id == hospital_id,
                            BillingCharge.source_type == source_type,
                            BillingCharge.source_id == rad_order.prescription_request_id,
                        )
                        .order_by(BillingCharge.created_at.desc())
                        .first()
                    )
                elif not rad_order:
                    # If source_id is RadPrescriptionRequest, check any created RadiologyOrder
                    rad_o = db.query(RadiologyOrder).filter(RadiologyOrder.prescription_request_id == source_id, RadiologyOrder.hospital_id == hospital_id).first()
                    if rad_o:
                        charge = (
                            db.query(BillingCharge)
                            .filter(
                                BillingCharge.hospital_id == hospital_id,
                                BillingCharge.source_type == source_type,
                                BillingCharge.source_id == rad_o.id,
                            )
                            .order_by(BillingCharge.created_at.desc())
                            .first()
                        )
        except Exception:
            pass

    if not charge:
        return ServiceFinancialState(
            charge_exists=False,
            charge_id=None,
            status="unbilled",
            net_amount=0.0,
            amount_paid=0.0,
            outstanding_amount=0.0,
            is_cleared=False,
            reason="No billing charge found for this service order.",
        )

    if charge.status == BillingChargeStatus.cancelled:
        return ServiceFinancialState(
            charge_exists=True,
            charge_id=charge.id,
            status="cancelled",
            net_amount=float(charge.net_amount or 0.0),
            amount_paid=float(charge.amount_paid or 0.0),
            outstanding_amount=0.0,
            is_cleared=False,
            reason="The billing charge for this service is cancelled.",
        )

    net = round(float(charge.net_amount or 0.0), 2)
    paid = round(float(charge.amount_paid or 0.0), 2)
    outstanding = max(0.0, round(net - paid, 2))

    # Fully cleared if net is 0 (or negative) or status is paid or paid covers net
    if net <= 0.0 or charge.status == BillingChargeStatus.paid or paid >= net > 0.0:
        return ServiceFinancialState(
            charge_exists=True,
            charge_id=charge.id,
            status="paid",
            net_amount=net,
            amount_paid=paid,
            outstanding_amount=0.0,
            is_cleared=True,
            reason="Service is financially cleared.",
        )

    if charge.status == BillingChargeStatus.partially_paid or paid > 0.0:
        return ServiceFinancialState(
            charge_exists=True,
            charge_id=charge.id,
            status="partially_paid",
            net_amount=net,
            amount_paid=paid,
            outstanding_amount=outstanding,
            is_cleared=False,
            reason=f"Service is partially paid. Outstanding amount: ₹{outstanding:.2f}.",
        )

    return ServiceFinancialState(
        charge_exists=True,
        charge_id=charge.id,
        status="pending",
        net_amount=net,
        amount_paid=paid,
        outstanding_amount=outstanding,
        is_cleared=False,
        reason=f"Payment pending. Outstanding amount: ₹{outstanding:.2f}.",
    )


def _state_from_charge(charge: BillingCharge | None) -> ServiceFinancialState:
    """
    Convert a single BillingCharge ORM row (or None) into a ServiceFinancialState.

    Encapsulates the clearance business rules so they are applied identically by
    both check_service_financial_clearance() and bulk_check_service_financial_clearance().
    This function must remain in exact parity with the logic in
    check_service_financial_clearance() — any rule change must be applied to both.
    """
    if charge is None:
        return ServiceFinancialState(
            charge_exists=False,
            charge_id=None,
            status="unbilled",
            net_amount=0.0,
            amount_paid=0.0,
            outstanding_amount=0.0,
            is_cleared=False,
            reason="No billing charge found for this service order.",
        )

    if charge.status == BillingChargeStatus.cancelled:
        return ServiceFinancialState(
            charge_exists=True,
            charge_id=charge.id,
            status="cancelled",
            net_amount=float(charge.net_amount or 0.0),
            amount_paid=float(charge.amount_paid or 0.0),
            outstanding_amount=0.0,
            is_cleared=False,
            reason="The billing charge for this service is cancelled.",
        )

    net = round(float(charge.net_amount or 0.0), 2)
    paid = round(float(charge.amount_paid or 0.0), 2)
    outstanding = max(0.0, round(net - paid, 2))

    if net <= 0.0 or charge.status == BillingChargeStatus.paid or paid >= net > 0.0:
        return ServiceFinancialState(
            charge_exists=True,
            charge_id=charge.id,
            status="paid",
            net_amount=net,
            amount_paid=paid,
            outstanding_amount=0.0,
            is_cleared=True,
            reason="Service is financially cleared.",
        )

    if charge.status == BillingChargeStatus.partially_paid or paid > 0.0:
        return ServiceFinancialState(
            charge_exists=True,
            charge_id=charge.id,
            status="partially_paid",
            net_amount=net,
            amount_paid=paid,
            outstanding_amount=outstanding,
            is_cleared=False,
            reason=f"Service is partially paid. Outstanding amount: ₹{outstanding:.2f}.",
        )

    return ServiceFinancialState(
        charge_exists=True,
        charge_id=charge.id,
        status="pending",
        net_amount=net,
        amount_paid=paid,
        outstanding_amount=outstanding,
        is_cleared=False,
        reason=f"Payment pending. Outstanding amount: ₹{outstanding:.2f}.",
    )


def bulk_check_service_financial_clearance(
    db: Session,
    hospital_id: UUID,
    source_type: BillingSourceType,
    source_ids: list[UUID],
) -> dict[UUID, ServiceFinancialState]:
    """
    Bulk-evaluate financial clearance for a list of source_ids in a minimal number
    of DB round-trips (2–4 queries total regardless of list length).

    Returns a mapping of source_id -> ServiceFinancialState for every supplied ID.
    IDs with no direct charge are resolved via cross-link fallback (prescription
    request ↔ radiology order) using additional bulk queries — never per-item
    queries.  IDs that remain unresolved are returned as "unbilled" / not cleared.

    All clearance business rules are identical to check_service_financial_clearance().
    The 'latest applicable charge' (ORDER BY created_at DESC) semantics are
    preserved: for each source_id, only the most-recently created charge is used.

    The existing single-item check_service_financial_clearance() is unchanged.
    """
    if not source_ids:
        return {}

    id_list = list(source_ids)

    # ── Step 1: Fetch latest charge per source_id in one bulk query ───────────
    # Fetch all matching charges, then keep only the most-recent per source_id
    # in Python.  This is equivalent to DISTINCT ON (source_id) ORDER BY
    # source_id, created_at DESC and preserves the "latest charge" semantics.
    all_charges = (
        db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.source_type == source_type,
            BillingCharge.source_id.in_(id_list),
        )
        .order_by(BillingCharge.source_id, BillingCharge.created_at.desc())
        .all()
    )

    # Keep only the first (latest) charge seen per source_id
    charges_by_source: dict[UUID, BillingCharge] = {}
    for charge in all_charges:
        if charge.source_id not in charges_by_source:
            charges_by_source[charge.source_id] = charge

    # ── Step 2: Identify source_ids without a direct charge ───────────────────
    missing_ids = [sid for sid in id_list if sid not in charges_by_source]

    # ── Step 3: Bulk cross-link fallback resolution ────────────────────────────
    # For laboratory & radiology: a source_id may be either an Order.id (whose charge
    # was recorded against its prescription_request_id) or a
    # PrescriptionRequest.id (whose charge was recorded against a linked
    # Order.id). We resolve all missing IDs in bulk queries:
    # one for Order/Request rows and one for BillingCharge on counterparts.
    if missing_ids and source_type == BillingSourceType.laboratory:
        try:
            from modules.laboratory.entities.lab_entities import LabOrder, LabPrescriptionRequest
            from sqlalchemy import or_ as sa_or

            # Fetch all lab orders whose .id OR .prescription_request_id appears in missing_ids
            linked_orders = (
                db.query(LabOrder)
                .filter(
                    LabOrder.hospital_id == hospital_id,
                    sa_or(
                        LabOrder.id.in_(missing_ids),
                        LabOrder.prescription_request_id.in_(missing_ids),
                    ),
                )
                .all()
            )

            orders_by_id: dict[UUID, LabOrder] = {o.id: o for o in linked_orders}
            orders_by_req: dict[UUID, LabOrder] = {}
            for o in linked_orders:
                if o.prescription_request_id and o.prescription_request_id not in orders_by_req:
                    orders_by_req[o.prescription_request_id] = o

            # For remaining missing IDs that weren't resolved as LabOrder.id,
            # query LabPrescriptionRequest to find their linked lab_order_id
            still_unresolved = [sid for sid in missing_ids if sid not in orders_by_id and sid not in orders_by_req]
            reqs_by_id: dict[UUID, LabPrescriptionRequest] = {}
            if still_unresolved:
                linked_reqs = (
                    db.query(LabPrescriptionRequest)
                    .filter(
                        LabPrescriptionRequest.hospital_id == hospital_id,
                        LabPrescriptionRequest.id.in_(still_unresolved),
                    )
                    .all()
                )
                reqs_by_id = {r.id: r for r in linked_reqs}

            missing_to_counterpart: dict[UUID, UUID] = {}
            counterpart_ids: list[UUID] = []
            for sid in missing_ids:
                counterpart: UUID | None = None
                if sid in orders_by_id:
                    # sid is a LabOrder.id — look up charge by its prescription_request_id
                    req_id = orders_by_id[sid].prescription_request_id
                    if req_id:
                        counterpart = req_id
                elif sid in orders_by_req:
                    # sid is a LabPrescriptionRequest.id — look up charge by the order's id
                    counterpart = orders_by_req[sid].id
                elif sid in reqs_by_id:
                    # sid is a LabPrescriptionRequest.id without a matching LabOrder from query 1
                    ord_id = reqs_by_id[sid].lab_order_id
                    if ord_id:
                        counterpart = ord_id

                if counterpart:
                    missing_to_counterpart[sid] = counterpart
                    counterpart_ids.append(counterpart)

            if counterpart_ids:
                fallback_charges = (
                    db.query(BillingCharge)
                    .filter(
                        BillingCharge.hospital_id == hospital_id,
                        BillingCharge.source_type == source_type,
                        BillingCharge.source_id.in_(counterpart_ids),
                    )
                    .order_by(BillingCharge.source_id, BillingCharge.created_at.desc())
                    .all()
                )
                fallback_by_counterpart: dict[UUID, BillingCharge] = {}
                for charge in fallback_charges:
                    if charge.source_id not in fallback_by_counterpart:
                        fallback_by_counterpart[charge.source_id] = charge

                for sid, counterpart in missing_to_counterpart.items():
                    if counterpart in fallback_by_counterpart:
                        charges_by_source[sid] = fallback_by_counterpart[counterpart]

        except Exception:
            pass

    elif missing_ids and source_type == BillingSourceType.radiology:
        try:
            from modules.radiology.entities.radiology_entities import RadiologyOrder
            from sqlalchemy import or_ as sa_or

            # Fetch all radiology orders whose .id OR .prescription_request_id
            # appears in missing_ids — covers both cross-link directions at once.
            linked_orders = (
                db.query(RadiologyOrder)
                .filter(
                    RadiologyOrder.hospital_id == hospital_id,
                    sa_or(
                        RadiologyOrder.id.in_(missing_ids),
                        RadiologyOrder.prescription_request_id.in_(missing_ids),
                    ),
                )
                .all()
            )

            # Index for O(1) lookup in both directions
            orders_by_id: dict[UUID, RadiologyOrder] = {o.id: o for o in linked_orders}
            # Keep first order per prescription_request_id (consistent with the
            # original single-item fallback which uses .first())
            orders_by_req: dict[UUID, RadiologyOrder] = {}
            for o in linked_orders:
                if o.prescription_request_id and o.prescription_request_id not in orders_by_req:
                    orders_by_req[o.prescription_request_id] = o

            # Map each missing source_id to its counterpart charge-lookup ID
            missing_to_counterpart: dict[UUID, UUID] = {}
            counterpart_ids: list[UUID] = []
            for sid in missing_ids:
                counterpart: UUID | None = None
                if sid in orders_by_id:
                    # sid is a RadiologyOrder.id — look up charge by its prescription_request_id
                    req_id = orders_by_id[sid].prescription_request_id
                    if req_id:
                        counterpart = req_id
                elif sid in orders_by_req:
                    # sid is a RadPrescriptionRequest.id — look up charge by the order's id
                    counterpart = orders_by_req[sid].id

                if counterpart:
                    missing_to_counterpart[sid] = counterpart
                    counterpart_ids.append(counterpart)

            # One bulk charge query for all resolved counterpart IDs
            if counterpart_ids:
                fallback_charges = (
                    db.query(BillingCharge)
                    .filter(
                        BillingCharge.hospital_id == hospital_id,
                        BillingCharge.source_type == source_type,
                        BillingCharge.source_id.in_(counterpart_ids),
                    )
                    .order_by(BillingCharge.source_id, BillingCharge.created_at.desc())
                    .all()
                )
                fallback_by_counterpart: dict[UUID, BillingCharge] = {}
                for charge in fallback_charges:
                    if charge.source_id not in fallback_by_counterpart:
                        fallback_by_counterpart[charge.source_id] = charge

                # Map resolved charges back to original source_ids
                for sid, counterpart in missing_to_counterpart.items():
                    if counterpart in fallback_by_counterpart:
                        charges_by_source[sid] = fallback_by_counterpart[counterpart]

        except Exception:
            # Fallback resolution failed — missing IDs stay missing (unbilled / not cleared)
            pass

    # ── Step 4: Build and return result map ────────────────────────────────────
    return {
        sid: _state_from_charge(charges_by_source.get(sid))
        for sid in id_list
    }


def evaluate_bed_allocation_clearance(
    db: Session,
    hospital_id: UUID,
    admission_id: UUID,
    ward_id: UUID | None = None,
    bed_id: UUID | None = None,
) -> BedAllocationClearanceState:
    """
    Evaluate authoritative financial readiness for IPD bed allocation.
    Evaluates strictly against the current requested IPD episode's advance requirement
    (admission fee + selected bed/ward tariff, or configured hospital minimum),
    accounting for allocated payments and available deposits.
    Separates historical patient balance (informational only) without hard-blocking.
    Respects approved FinancialClearanceExceptions.
    """
    from modules.beds.entities.bed import Bed, Ward
    from modules.billing.services.billing_service import patient_ledger_totals
    from modules.inpatient.entities.admission import Admission
    from modules.inpatient.services.inpatient_billing_service import InpatientBillingService
    from modules.tenancy.entities.hospital import Hospital

    admission = (
        db.query(Admission)
        .filter(Admission.id == admission_id, Admission.hospital_id == hospital_id)
        .first()
    )
    if not admission:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Admission not found for financial clearance evaluation.",
        )

    # Resolve target ward & bed
    resolved_ward_id = ward_id
    if not resolved_ward_id and bed_id:
        target_bed = db.query(Bed).filter(Bed.id == bed_id, Bed.hospital_id == hospital_id).first()
        if target_bed:
            resolved_ward_id = target_bed.ward_id
    if not resolved_ward_id and admission.ward_id:
        resolved_ward_id = admission.ward_id

    ward = None
    if resolved_ward_id:
        ward = db.query(Ward).filter(Ward.id == resolved_ward_id, Ward.hospital_id == hospital_id).first()

    # Read hospital admission advance policy
    policy = get_admission_financial_policy(db, hospital_id)

    is_provisional_ward = False
    if not ward and policy.financial_gate_enabled and policy.advance_mode == "mandatory":
        from modules.beds.entities.bed import WardType
        default_ward = (
            db.query(Ward)
            .filter(Ward.hospital_id == hospital_id, Ward.is_active == True)
            .order_by((Ward.ward_type == WardType.general).desc(), Ward.admission_fee.asc())
            .first()
        )
        if default_ward:
            ward = default_ward
            is_provisional_ward = True

    admission_fee = float(getattr(ward, "admission_fee", 0.0) or 0.0) if ward else 0.0
    bed_charge_per_day = float(getattr(ward, "bed_charge_per_day", 0.0) or 0.0) if ward else 0.0

    if not policy.financial_gate_enabled or policy.advance_mode == "waived":
        required_advance = 0.0
    elif policy.advance_mode == "optional":
        required_advance = 0.0
    else:
        # Mandatory advance
        fee = admission_fee if policy.include_admission_fee else 0.0
        tariff = bed_charge_per_day if policy.include_first_day_bed_tariff else 0.0
        base_advance = round(fee + tariff, 2)
        required_advance = max(base_advance, policy.minimum_advance_amount) if policy.minimum_advance_amount > 0 else base_advance

    # Ensure IPD financial account exists
    billing_svc = InpatientBillingService(db)
    acc = billing_svc.ensure_financial_account(
        hospital_id=hospital_id,
        patient_id=admission.patient_id,
        admission_id=admission.id,
        created_by_name="System",
    )
    ipd_account_id = acc.id if acc else None

    # Compute episode-specific ledger totals
    ledger = billing_svc.get_ledger_totals(
        hospital_id, admission.patient_id, admission_id=admission.id
    )
    paid = round(float(ledger.get("total_paid", 0.0) or ledger.get("total_payments", 0.0) or 0.0), 2)
    deposit = round(
        float(ledger.get("deposits_available", 0.0) or ledger.get("total_deposits_available", 0.0) or 0.0),
        2,
    )
    current_ipd_outstanding = round(float(ledger.get("outstanding", 0.0) or 0.0), 2)

    # Compute historical patient balance (all episodes minus current IPD account)
    lifetime_ledger = patient_ledger_totals(db, hospital_id, admission.patient_id, account_id=None)
    lifetime_outstanding = round(float(lifetime_ledger.get("outstanding", 0.0) or 0.0), 2)
    historical_patient_balance = max(0.0, round(lifetime_outstanding - current_ipd_outstanding, 2))

    total_coverage = round(paid + deposit, 2)
    shortfall = max(0.0, round(required_advance - total_coverage, 2))

    # Check for active FinancialClearanceException for this admission
    active_exc = (
        db.query(FinancialClearanceException)
        .filter(
            FinancialClearanceException.hospital_id == hospital_id,
            FinancialClearanceException.admission_id == admission.id,
            FinancialClearanceException.service_type.in_(["admission_bed", "ipd_bed_allocation", "bed_allocation", "admission"]),
            FinancialClearanceException.is_consumed == False,
        )
        .order_by(FinancialClearanceException.created_at.desc())
        .first()
    )

    if active_exc and active_exc.status == FinancialExceptionStatus.approved.value:
        status_str = "emergency_override" if active_exc.is_emergency else "exception_approved"
        reason_str = (
            f"Emergency clinical override active: {active_exc.reason}"
            if active_exc.is_emergency
            else f"Approved financial exception ({active_exc.reason_category}): {active_exc.reason}"
        )
        return BedAllocationClearanceState(
            is_cleared=True,
            status=status_str,
            required_advance=required_advance,
            admission_fee=admission_fee,
            bed_charge_per_day=bed_charge_per_day,
            paid_or_allocated_amount=paid,
            available_deposit=deposit,
            shortfall=shortfall,
            ipd_account_id=ipd_account_id,
            current_ipd_outstanding=current_ipd_outstanding,
            historical_patient_balance=historical_patient_balance,
            active_exception_id=active_exc.id,
            active_exception_status=active_exc.status,
            reason=reason_str,
        )

    if active_exc and active_exc.status == FinancialExceptionStatus.pending.value:
        return BedAllocationClearanceState(
            is_cleared=False,
            status="exception_pending",
            required_advance=required_advance,
            admission_fee=admission_fee,
            bed_charge_per_day=bed_charge_per_day,
            paid_or_allocated_amount=paid,
            available_deposit=deposit,
            shortfall=shortfall,
            ipd_account_id=ipd_account_id,
            current_ipd_outstanding=current_ipd_outstanding,
            historical_patient_balance=historical_patient_balance,
            active_exception_id=active_exc.id,
            active_exception_status=active_exc.status,
            reason=f"Financial exception request pending review ({active_exc.reason_category}). Shortfall: ₹{shortfall:.2f}",
        )

    if shortfall <= 0.0:
        return BedAllocationClearanceState(
            is_cleared=True,
            status="financially_cleared",
            required_advance=required_advance,
            admission_fee=admission_fee,
            bed_charge_per_day=bed_charge_per_day,
            paid_or_allocated_amount=paid,
            available_deposit=deposit,
            shortfall=0.0,
            ipd_account_id=ipd_account_id,
            current_ipd_outstanding=current_ipd_outstanding,
            historical_patient_balance=historical_patient_balance,
            active_exception_id=None,
            active_exception_status=None,
            reason="Financially cleared for standard ward admission (pending bed allocation)." if is_provisional_ward else "Financially cleared for bed allocation.",
        )

    return BedAllocationClearanceState(
        is_cleared=False,
        status="payment_required",
        required_advance=required_advance,
        admission_fee=admission_fee,
        bed_charge_per_day=bed_charge_per_day,
        paid_or_allocated_amount=paid,
        available_deposit=deposit,
        shortfall=shortfall,
        ipd_account_id=ipd_account_id,
        current_ipd_outstanding=current_ipd_outstanding,
        historical_patient_balance=historical_patient_balance,
        active_exception_id=None,
        active_exception_status=None,
        reason=(
            f"Payment or deposit of ₹{shortfall:.2f} required (estimated for standard ward). Final amount adjusts upon specific bed allocation."
            if is_provisional_ward
            else f"Payment or deposit of ₹{shortfall:.2f} required for admission advance."
        ),
    )


def assert_service_financially_cleared(
    db: Session,
    hospital_id: UUID,
    source_type: BillingSourceType,
    source_id: UUID,
    action_description: str = "proceed with service",
    is_emergency_override: bool = False,
    actor: dict[str, Any] | None = None,
    emergency_reason: str | None = None,
    admission_id: UUID | None = None,
    patient_id: UUID | None = None,
) -> ServiceFinancialState:
    """
    Assert that the service charge is financially cleared.
    Raises HTTPException 402 PAYMENT_REQUIRED (or 400 if cancelled) if blocked.
    If an approved FinancialClearanceException exists, allows service to proceed.
    If is_emergency_override is True, structured emergency exception is recorded and care proceeds immediately.
    """
    state = check_service_financial_clearance(db, hospital_id, source_type, source_id)
    if state.is_cleared:
        return state

    # 1. Check for approved FinancialClearanceException strictly scoped to service_type & patient
    exc_q = (
        db.query(FinancialClearanceException)
        .filter(
            FinancialClearanceException.hospital_id == hospital_id,
            FinancialClearanceException.status == FinancialExceptionStatus.approved.value,
            FinancialClearanceException.is_consumed == False,
            FinancialClearanceException.service_type == source_type.value,
        )
    )
    if patient_id:
        exc_q = exc_q.filter(FinancialClearanceException.patient_id == patient_id)
    if source_id:
        exc_q = exc_q.filter(
            (FinancialClearanceException.source_id == source_id)
            | (FinancialClearanceException.source_id.is_(None))
        )
    if admission_id:
        exc_q = exc_q.filter(
            (FinancialClearanceException.admission_id == admission_id)
            | (FinancialClearanceException.admission_id.is_(None))
        )

    approved_exc = exc_q.order_by(FinancialClearanceException.created_at.desc()).first()
    if approved_exc:
        # Mark exception consumed to prevent unauthorized replay
        from datetime import datetime as _dt
        from datetime import timezone as _tz
        approved_exc.is_consumed = True
        approved_exc.consumed_at = _dt.now(_tz.utc)
        approved_exc.status = FinancialExceptionStatus.consumed.value
        db.flush()

        return ServiceFinancialState(
            charge_exists=state.charge_exists,
            charge_id=state.charge_id,
            status=state.status,
            net_amount=state.net_amount,
            amount_paid=state.amount_paid,
            outstanding_amount=state.outstanding_amount,
            is_cleared=True,
            reason=f"Approved financial exception ({approved_exc.reason_category}): {approved_exc.reason}",
        )

    # 2. Structured emergency override
    if is_emergency_override:
        actor_id, actor_name = validate_emergency_override_actor(
            actor, emergency_reason, service_type=source_type.value
        )
        reason_text = (emergency_reason or "").strip()

        # If patient_id is not given, derive from charge
        target_patient_id = patient_id
        if not target_patient_id and state.charge_id:
            chg = db.query(BillingCharge).filter(BillingCharge.id == state.charge_id).first()
            if chg:
                target_patient_id = chg.patient_id

        if target_patient_id and actor_id:
            try:
                from datetime import datetime as _dt
                from datetime import timezone as _tz
                now = _dt.now(_tz.utc)
                em_exc = FinancialClearanceException(
                    hospital_id=hospital_id,
                    patient_id=target_patient_id,
                    admission_id=admission_id,
                    service_type=source_type.value,
                    action_type="emergency_execution",
                    source_id=source_id,
                    is_emergency=True,
                    required_amount=Decimal(str(state.net_amount)),
                    amount_covered=Decimal(str(state.amount_paid)),
                    shortfall_amount=Decimal(str(state.outstanding_amount)),
                    reason_category="emergency_life_safety",
                    reason=reason_text,
                    status=FinancialExceptionStatus.consumed.value,
                    is_consumed=True,
                    consumed_at=now,
                    requested_by_id=actor_id,
                    requested_by_name=actor_name,
                    approved_by_id=actor_id,
                    approved_by_name=actor_name,
                    approval_remarks="Auto-authorized under emergency life-safety override protocol",
                )
                db.add(em_exc)
                db.flush()
                from shared.audit.service import write_audit_log
                write_audit_log(
                    db,
                    hospital_id=hospital_id,
                    actor=actor or {"id": str(actor_id), "name": actor_name},
                    action="emergency_override",
                    entity_type="financial_clearance_exception",
                    entity_id=em_exc.id,
                    summary=f"Emergency financial override applied for {source_type.value} ({source_id}): {reason_text}",
                )
            except Exception:
                pass

        return ServiceFinancialState(
            charge_exists=state.charge_exists,
            charge_id=state.charge_id,
            status=state.status,
            net_amount=state.net_amount,
            amount_paid=state.amount_paid,
            outstanding_amount=state.outstanding_amount,
            is_cleared=True,
            reason=f"Emergency clinical override active: {reason_text}",
        )

    if state.status == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot {action_description}: billing charge is cancelled.",
        )

    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={
            "message": f"Payment required to {action_description}.",
            "status": state.status,
            "net_amount": state.net_amount,
            "amount_paid": state.amount_paid,
            "outstanding_amount": state.outstanding_amount,
            "reason": state.reason,
        },
    )
