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
from sqlalchemy.orm import Session, joinedload

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


def get_admission_financial_policy(db: Session, hospital_id: UUID) -> AdmissionFinancialPolicy:
    from modules.tenancy.entities.hospital import Hospital
    hosp = db.query(Hospital).filter(Hospital.id == hospital_id).first()
    if not hosp:
        return AdmissionFinancialPolicy()
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
    previous = dict(settings.get("admission_financial_policy") or settings.get("admission_advance_policy") or {})
    current = dict(previous)

    update_dict = payload.model_dump(exclude_unset=True)
    current.update(update_dict)
    settings["admission_financial_policy"] = current
    settings["admission_advance_policy"] = current
    hosp.facility_settings = settings

    from shared.audit.service import write_audit_log
    write_audit_log(
        db,
        hospital_id=hospital_id,
        actor=actor,
        action="update",
        entity_type="admission_financial_policy",
        entity_id=hospital_id,
        summary=f"Updated admission financial policy: mode={current.get('advance_mode')}, min={current.get('minimum_advance_amount')}",
        details={"previous": previous, "updated": current, "changed_fields": sorted(update_dict.keys())},
    )
    db.commit()
    db.refresh(hosp)
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

    The charge is found through the stable service ↔ charge resolver
    (``service_charge_resolver.resolve_service_charge``): an order id and its
    prescription request id resolve to the same canonical charge, and an active charge
    always wins over a cancelled one.

    Rules:
    - No charge: unbilled / not cleared.
    - Cancelled charge (and no active one): not cleared, service cancelled.
    - net_amount <= 0, status paid, or amount_paid covers net: cleared.
    - Partially paid or pending: NOT cleared.
    """
    from modules.billing.services.service_charge_resolver import resolve_service_charge

    return _state_from_charge(resolve_service_charge(db, hospital_id, source_type, source_id))


def _state_from_charge(charge: BillingCharge | None) -> ServiceFinancialState:
    """Convert a single BillingCharge ORM row (or None) into a ServiceFinancialState.

    Shared by check_service_financial_clearance() and bulk_check_service_financial_clearance()
    so both apply identical clearance rules.
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
    """Bulk ``check_service_financial_clearance`` in a constant number of queries.

    Returns a mapping of source_id -> ServiceFinancialState for every supplied ID,
    resolved through the same stable service ↔ charge resolver as the single check.
    """
    if not source_ids:
        return {}
    from modules.billing.services.service_charge_resolver import resolve_service_charges

    charges = resolve_service_charges(db, hospital_id, source_type, list(source_ids))
    return {sid: _state_from_charge(charges.get(sid)) for sid in source_ids}


def _parse_admission_policy_from_settings(settings: dict | None) -> AdmissionFinancialPolicy:
    stored = (settings or {}).get("admission_financial_policy") or (settings or {}).get("admission_advance_policy") or {}
    return AdmissionFinancialPolicy(
        financial_gate_enabled=stored.get("financial_gate_enabled", stored.get("enabled", True)),
        advance_mode=stored.get("advance_mode", "mandatory"),
        minimum_advance_amount=float(stored.get("minimum_advance_amount", stored.get("fixed_minimum_amount", 0.0)) or 0.0),
        include_admission_fee=stored.get("include_admission_fee", True),
        include_first_day_bed_tariff=stored.get("include_first_day_bed_tariff", True),
        emergency_bypass_allowed=stored.get("emergency_bypass_allowed", True),
    )


def bulk_evaluate_bed_allocation_clearance(
    db: Session,
    hospital_id: UUID,
    admission_ids: list[UUID],
    ward_overrides: dict[UUID, UUID | None] | None = None,
    bed_overrides: dict[UUID, UUID | None] | None = None,
) -> dict[UUID, BedAllocationClearanceState]:
    """Bulk evaluate authoritative financial readiness for IPD bed allocation.

    Executes in a constant number of bounded queries (~4-5 queries total) regardless
    of how many admissions N are evaluated:
      1. Bulk load admissions with joined patient, ward, room, and bed relationships.
      2. Bulk load hospital facility settings for admission policy.
      3. Bulk load existing open IPD FinancialAccounts for these admissions.
      4. Single combined set-based aggregation query computing episode + lifetime
         charges, payments, and deposits per patient and per admission account.
      5. Bulk load active FinancialClearanceExceptions for all admissions.
    """
    if not admission_ids:
        return {}

    from sqlalchemy import case, func, or_
    from modules.beds.entities.bed import Bed, Ward, WardType
    from modules.billing.entities.billing_entities import (
        BillingCharge,
        BillingChargeStatus,
        BillingDeposit,
        BillingPayment,
        DepositStatus,
        FinancialAccount,
        FinancialAccountType,
        FinancialAccountStatus,
    )
    from modules.inpatient.entities.admission import Admission
    from modules.tenancy.entities.hospital import Hospital

    ward_overrides = ward_overrides or {}
    bed_overrides = bed_overrides or {}

    # Query 1: Bulk load admissions with relationships
    admissions = (
        db.query(Admission)
        .options(
            joinedload(Admission.patient),
            joinedload(Admission.ward),
            joinedload(Admission.bed),
        )
        .filter(Admission.id.in_(admission_ids), Admission.hospital_id == hospital_id)
        .all()
    )
    admission_map = {a.id: a for a in admissions}

    # Query 2: Policy & default ward if needed
    hosp = db.query(Hospital).filter(Hospital.id == hospital_id).first()
    policy = _parse_admission_policy_from_settings(hosp.facility_settings if hosp else {})

    default_ward = None
    if policy.financial_gate_enabled and policy.advance_mode == "mandatory":
        default_ward = (
            db.query(Ward)
            .filter(Ward.hospital_id == hospital_id, Ward.is_active == True)
            .order_by((Ward.ward_type == WardType.general).desc(), Ward.admission_fee.asc())
            .first()
        )

    # Resolve target ward per admission
    extra_bed_ids = {
        bed_overrides[aid]
        for aid in admission_ids
        if bed_overrides.get(aid) and not ward_overrides.get(aid)
    } - {None}
    extra_beds = {}
    if extra_bed_ids:
        extra_beds = {
            b.id: b
            for b in db.query(Bed).filter(Bed.id.in_(extra_bed_ids), Bed.hospital_id == hospital_id).all()
        }

    extra_ward_ids = set()
    for aid in admission_ids:
        w_override = ward_overrides.get(aid)
        if w_override:
            extra_ward_ids.add(w_override)
        elif bed_overrides.get(aid) and bed_overrides[aid] in extra_beds:
            b_obj = extra_beds[bed_overrides[aid]]
            if b_obj.ward_id:
                extra_ward_ids.add(b_obj.ward_id)

    extra_wards = {}
    if extra_ward_ids:
        extra_wards = {
            w.id: w
            for w in db.query(Ward).filter(Ward.id.in_(extra_ward_ids), Ward.hospital_id == hospital_id).all()
        }

    # Query 3: Bulk load open IPD accounts for these admissions (read path - no write locks!)
    existing_accounts = (
        db.query(FinancialAccount)
        .filter(
            FinancialAccount.hospital_id == hospital_id,
            FinancialAccount.admission_id.in_(admission_ids),
            FinancialAccount.account_type == FinancialAccountType.ipd,
            FinancialAccount.status == FinancialAccountStatus.open,
        )
        .all()
    )
    acc_by_admission: dict[UUID, FinancialAccount] = {
        acc.admission_id: acc for acc in existing_accounts if acc.admission_id
    }

    patient_ids = list({a.patient_id for a in admissions if a.patient_id})
    all_account_ids = [acc.id for acc in existing_accounts]

    # Query 4: Set-based aggregation for charges, payments, and deposits
    # 4a. Charges aggregation: group by patient_id
    charge_aggs = {}
    if patient_ids:
        c_rows = (
            db.query(
                BillingCharge.patient_id,
                func.coalesce(func.sum(BillingCharge.net_amount), 0.0).label("lifetime_charges"),
                func.coalesce(func.sum(BillingCharge.amount_paid), 0.0).label("lifetime_paid"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                BillingCharge.account_id.in_(all_account_ids) if all_account_ids else False,
                                BillingCharge.amount_paid,
                            ),
                            else_=0.0,
                        )
                    ),
                    0.0,
                ).label("episode_paid"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                BillingCharge.account_id.in_(all_account_ids) if all_account_ids else False,
                                case(
                                    (BillingCharge.net_amount > BillingCharge.amount_paid, BillingCharge.net_amount - BillingCharge.amount_paid),
                                    else_=0.0,
                                ),
                            ),
                            else_=0.0,
                        )
                    ),
                    0.0,
                ).label("episode_outstanding"),
                func.coalesce(
                    func.sum(
                        case(
                            (BillingCharge.net_amount > BillingCharge.amount_paid, BillingCharge.net_amount - BillingCharge.amount_paid),
                            else_=0.0,
                        )
                    ),
                    0.0,
                ).label("lifetime_outstanding"),
            )
            .filter(
                BillingCharge.hospital_id == hospital_id,
                BillingCharge.patient_id.in_(patient_ids),
                BillingCharge.status != BillingChargeStatus.cancelled,
            )
            .group_by(BillingCharge.patient_id)
            .all()
        )
        charge_aggs = {r[0]: r for r in c_rows}

    # Per-account charges aggregation (if multiple admissions belong to same patient)
    acc_charge_aggs = {}
    if all_account_ids:
        ac_rows = (
            db.query(
                BillingCharge.account_id,
                func.coalesce(func.sum(BillingCharge.amount_paid), 0.0),
                func.coalesce(
                    func.sum(
                        case(
                            (BillingCharge.net_amount > BillingCharge.amount_paid, BillingCharge.net_amount - BillingCharge.amount_paid),
                            else_=0.0,
                        )
                    ),
                    0.0,
                ),
            )
            .filter(
                BillingCharge.hospital_id == hospital_id,
                BillingCharge.account_id.in_(all_account_ids),
                BillingCharge.status != BillingChargeStatus.cancelled,
            )
            .group_by(BillingCharge.account_id)
            .all()
        )
        acc_charge_aggs = {r[0]: (float(r[1]), float(r[2])) for r in ac_rows}

    # 4b. Deposits aggregation per admission and per account
    dep_aggs: dict[UUID, float] = {}
    if admission_ids:
        d_rows = (
            db.query(
                BillingDeposit.admission_id,
                func.coalesce(func.sum(BillingDeposit.available_amount), 0.0),
            )
            .filter(
                BillingDeposit.hospital_id == hospital_id,
                BillingDeposit.admission_id.in_(admission_ids),
                BillingDeposit.status.in_([DepositStatus.available, DepositStatus.partially_allocated]),
            )
            .group_by(BillingDeposit.admission_id)
            .all()
        )
        dep_aggs = {r[0]: float(r[1]) for r in d_rows if r[0]}

    # Query 5: Active exceptions for all target admissions
    exceptions = (
        db.query(FinancialClearanceException)
        .filter(
            FinancialClearanceException.hospital_id == hospital_id,
            FinancialClearanceException.admission_id.in_(admission_ids),
            FinancialClearanceException.service_type.in_(["admission_bed", "ipd_bed_allocation", "bed_allocation", "admission"]),
            FinancialClearanceException.is_consumed == False,
        )
        .order_by(FinancialClearanceException.created_at.desc())
        .all()
    )
    exc_by_admission: dict[UUID, FinancialClearanceException] = {}
    for exc in exceptions:
        if exc.admission_id and exc.admission_id not in exc_by_admission:
            exc_by_admission[exc.admission_id] = exc

    # Construct clearance state for each admission
    results: dict[UUID, BedAllocationClearanceState] = {}
    for aid in admission_ids:
        adm = admission_map.get(aid)
        if not adm:
            continue

        resolved_ward = None
        w_id = ward_overrides.get(aid)
        if w_id and w_id in extra_wards:
            resolved_ward = extra_wards[w_id]
        elif bed_overrides.get(aid) and bed_overrides[aid] in extra_beds:
            b_obj = extra_beds[bed_overrides[aid]]
            if b_obj.ward_id and b_obj.ward_id in extra_wards:
                resolved_ward = extra_wards[b_obj.ward_id]
        if not resolved_ward and adm.ward:
            resolved_ward = adm.ward

        is_prov = False
        if not resolved_ward and policy.financial_gate_enabled and policy.advance_mode == "mandatory":
            resolved_ward = default_ward
            is_prov = True

        admission_fee = float(getattr(resolved_ward, "admission_fee", 0.0) or 0.0) if resolved_ward else 0.0
        bed_charge_per_day = float(getattr(resolved_ward, "bed_charge_per_day", 0.0) or 0.0) if resolved_ward else 0.0

        if not policy.financial_gate_enabled or policy.advance_mode in ("waived", "optional"):
            required_advance = 0.0
        else:
            fee = admission_fee if policy.include_admission_fee else 0.0
            tariff = bed_charge_per_day if policy.include_first_day_bed_tariff else 0.0
            base_advance = round(fee + tariff, 2)
            required_advance = max(base_advance, policy.minimum_advance_amount) if policy.minimum_advance_amount > 0 else base_advance

        acc = acc_by_admission.get(aid)
        ipd_acc_id = acc.id if acc else None

        if ipd_acc_id and ipd_acc_id in acc_charge_aggs:
            paid = round(acc_charge_aggs[ipd_acc_id][0], 2)
            current_ipd_outstanding = round(acc_charge_aggs[ipd_acc_id][1], 2)
        else:
            paid = 0.0
            current_ipd_outstanding = 0.0

        deposit = round(dep_aggs.get(aid, 0.0), 2)

        c_row = charge_aggs.get(adm.patient_id)
        lifetime_outstanding = round(float(c_row[5] if c_row else 0.0), 2)
        historical_balance = max(0.0, round(lifetime_outstanding - current_ipd_outstanding, 2))

        total_cov = round(paid + deposit, 2)
        shortfall = max(0.0, round(required_advance - total_cov, 2))

        active_exc = exc_by_admission.get(aid)
        if active_exc and active_exc.status == FinancialExceptionStatus.approved.value:
            status_str = "emergency_override" if active_exc.is_emergency else "exception_approved"
            reason_str = (
                f"Emergency clinical override active: {active_exc.reason}"
                if active_exc.is_emergency
                else f"Approved financial exception ({active_exc.reason_category}): {active_exc.reason}"
            )
            results[aid] = BedAllocationClearanceState(
                is_cleared=True,
                status=status_str,
                required_advance=required_advance,
                admission_fee=admission_fee,
                bed_charge_per_day=bed_charge_per_day,
                paid_or_allocated_amount=paid,
                available_deposit=deposit,
                shortfall=shortfall,
                ipd_account_id=ipd_acc_id,
                current_ipd_outstanding=current_ipd_outstanding,
                historical_patient_balance=historical_balance,
                active_exception_id=active_exc.id,
                active_exception_status=active_exc.status,
                reason=reason_str,
            )
        elif active_exc and active_exc.status == FinancialExceptionStatus.pending.value:
            results[aid] = BedAllocationClearanceState(
                is_cleared=False,
                status="exception_pending",
                required_advance=required_advance,
                admission_fee=admission_fee,
                bed_charge_per_day=bed_charge_per_day,
                paid_or_allocated_amount=paid,
                available_deposit=deposit,
                shortfall=shortfall,
                ipd_account_id=ipd_acc_id,
                current_ipd_outstanding=current_ipd_outstanding,
                historical_patient_balance=historical_balance,
                active_exception_id=active_exc.id,
                active_exception_status=active_exc.status,
                reason=f"Financial exception request pending review ({active_exc.reason_category}). Shortfall: ₹{shortfall:.2f}",
            )
        elif shortfall <= 0.0:
            results[aid] = BedAllocationClearanceState(
                is_cleared=True,
                status="financially_cleared",
                required_advance=required_advance,
                admission_fee=admission_fee,
                bed_charge_per_day=bed_charge_per_day,
                paid_or_allocated_amount=paid,
                available_deposit=deposit,
                shortfall=0.0,
                ipd_account_id=ipd_acc_id,
                current_ipd_outstanding=current_ipd_outstanding,
                historical_patient_balance=historical_balance,
                active_exception_id=None,
                active_exception_status=None,
                reason="Financially cleared for standard ward admission (pending bed allocation)." if is_prov else "Financially cleared for bed allocation.",
            )
        else:
            results[aid] = BedAllocationClearanceState(
                is_cleared=False,
                status="payment_required",
                required_advance=required_advance,
                admission_fee=admission_fee,
                bed_charge_per_day=bed_charge_per_day,
                paid_or_allocated_amount=paid,
                available_deposit=deposit,
                shortfall=shortfall,
                ipd_account_id=ipd_acc_id,
                current_ipd_outstanding=current_ipd_outstanding,
                historical_patient_balance=historical_balance,
                active_exception_id=None,
                active_exception_status=None,
                reason=(
                    f"Payment or deposit of ₹{shortfall:.2f} required (estimated for standard ward). Final amount adjusts upon specific bed allocation."
                    if is_prov
                    else f"Payment or deposit of ₹{shortfall:.2f} required for admission advance."
                ),
            )

    return results


def evaluate_bed_allocation_clearance(
    db: Session,
    hospital_id: UUID,
    admission_id: UUID,
    ward_id: UUID | None = None,
    bed_id: UUID | None = None,
) -> BedAllocationClearanceState:
    """Evaluate authoritative financial readiness for IPD bed allocation.

    Uses bulk evaluation architecture behind the scenes to execute in a constant,
    bounded number of set-based queries (~4 queries) without N+1 or write-locking.
    """
    res = bulk_evaluate_bed_allocation_clearance(
        db,
        hospital_id,
        [admission_id],
        ward_overrides={admission_id: ward_id} if ward_id else None,
        bed_overrides={admission_id: bed_id} if bed_id else None,
    )
    if admission_id not in res:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Admission not found for financial clearance evaluation.",
        )
    return res[admission_id]


def enforce_bed_allocation_clearance(
    db: Session,
    hospital_id: UUID,
    admission: Any,
    *,
    ward_id: UUID | None,
    bed_id: UUID | None,
    actor: dict[str, Any] | None,
    is_emergency_override: bool = False,
    emergency_override_reason: str | None = None,
) -> BedAllocationClearanceState:
    """Gate a first bed allocation for ``admission`` (402 when not financially cleared).

    A cleared state consumes the admission's approved exception, if any. When not
    cleared, an explicit emergency override is authorized through the canonical helper
    (permission + reason + ``emergency_bypass_allowed`` policy) and persisted as a
    consumed admission-scoped override with audit + sync. The caller commits.
    """
    from dataclasses import replace

    from modules.billing.services.financial_exception_service import (
        ENTITY_TYPE,
        consume_exception,
        exception_audit_details,
        record_emergency_override,
    )
    from shared.audit.service import write_audit_log

    fin_state = evaluate_bed_allocation_clearance(db, hospital_id, admission.id, ward_id=ward_id, bed_id=bed_id)
    if fin_state.is_cleared:
        if fin_state.active_exception_id:
            exc = (
                db.query(FinancialClearanceException)
                .filter(
                    FinancialClearanceException.id == fin_state.active_exception_id,
                    FinancialClearanceException.hospital_id == hospital_id,
                )
                .first()
            )
            if exc is not None:
                consume_exception(exc)
                db.flush()
                write_audit_log(
                    db,
                    hospital_id=hospital_id,
                    actor=actor or {"sub": "system", "name": "System", "role": "system"},
                    action="consume",
                    entity_type=ENTITY_TYPE,
                    entity_id=exc.id,
                    summary=f"Financial exception consumed for bed allocation (admission {admission.id})",
                    details=exception_audit_details(exc, consumed_for_bed_id=str(bed_id) if bed_id else None),
                )
        return fin_state

    if not is_emergency_override:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "message": f"Payment or deposit required before bed allocation. Shortfall: ₹{fin_state.shortfall:.2f}.",
                "status": fin_state.status,
                "required_advance": fin_state.required_advance,
                "admission_fee": fin_state.admission_fee,
                "bed_charge_per_day": fin_state.bed_charge_per_day,
                "paid_or_allocated_amount": fin_state.paid_or_allocated_amount,
                "available_deposit": fin_state.available_deposit,
                "shortfall": fin_state.shortfall,
                "reason": fin_state.reason,
                "ipd_account_id": str(fin_state.ipd_account_id) if fin_state.ipd_account_id else None,
            },
        )

    from modules.billing.services.financial_authorization import (
        OverrideScope,
        authorize_emergency_override,
    )

    scope = OverrideScope.admission(admission.id, patient_id=admission.patient_id, source_id=bed_id)
    override_actor = authorize_emergency_override(
        db, hospital_id, actor, scope=scope, reason=emergency_override_reason
    )
    em_exc = record_emergency_override(
        db,
        hospital_id,
        override_actor,
        scope,
        emergency_override_reason or "",
        required_amount=fin_state.required_advance,
        amount_covered=round(fin_state.paid_or_allocated_amount + fin_state.available_deposit, 2),
        consumed=True,
        financial_account_id=fin_state.ipd_account_id,
    )
    return replace(
        fin_state,
        is_cleared=True,
        status="emergency_override",
        active_exception_id=em_exc.id,
        active_exception_status=em_exc.status,
        reason=f"Emergency clinical override active: {em_exc.reason}",
    )


def _linked_source_ids(
    db: Session, hospital_id: UUID, source_type: BillingSourceType, source_id: UUID
) -> list[UUID]:
    """The source plus its lab/radiology order ↔ prescription-request counterpart."""
    ids: list[UUID] = [source_id]
    rows: list[Any] = []
    if source_type == BillingSourceType.laboratory:
        from modules.laboratory.entities.lab_entities import LabOrder

        rows = (
            db.query(LabOrder.id, LabOrder.prescription_request_id)
            .filter(
                LabOrder.hospital_id == hospital_id,
                (LabOrder.id == source_id) | (LabOrder.prescription_request_id == source_id),
            )
            .all()
        )
    elif source_type == BillingSourceType.radiology:
        from modules.radiology.entities.radiology_entities import RadiologyOrder

        rows = (
            db.query(RadiologyOrder.id, RadiologyOrder.prescription_request_id)
            .filter(
                RadiologyOrder.hospital_id == hospital_id,
                (RadiologyOrder.id == source_id) | (RadiologyOrder.prescription_request_id == source_id),
            )
            .all()
        )
    for order_id, request_id in rows:
        for candidate in (order_id, request_id):
            if candidate and candidate not in ids:
                ids.append(candidate)
    return ids


def _service_scope_ids(
    db: Session, hospital_id: UUID, source_type: BillingSourceType, source_id: UUID
) -> list[UUID]:
    """Ids whose exception authorizes every step of this service: the source itself
    plus, for an order created from a prescription request, that request (the canonical
    billable service). Sibling orders of the same request are never included."""
    from modules.billing.services.service_charge_resolver import service_charge_key

    key = service_charge_key(db, hospital_id, source_type, source_id)
    ids = [source_id]
    if key.canonical_source_id not in ids:
        ids.append(key.canonical_source_id)
    return ids


def _find_order_scoped_exception(
    db: Session,
    hospital_id: UUID,
    source_type: BillingSourceType,
    source_id: UUID,
    patient_id: UUID | None,
) -> FinancialClearanceException | None:
    """An approved exception / emergency override already used by an earlier step of
    the same service (status ``consumed``) keeps authorizing its later steps."""
    q = db.query(FinancialClearanceException).filter(
        FinancialClearanceException.hospital_id == hospital_id,
        FinancialClearanceException.service_type == source_type.value,
        FinancialClearanceException.is_consumed == True,
        FinancialClearanceException.status == FinancialExceptionStatus.consumed.value,
        FinancialClearanceException.source_id.in_(
            _service_scope_ids(db, hospital_id, source_type, source_id)
        ),
    )
    if patient_id:
        q = q.filter(FinancialClearanceException.patient_id == patient_id)
    return q.order_by(FinancialClearanceException.created_at.desc()).first()


def _exception_cleared_state(state: ServiceFinancialState, exc: FinancialClearanceException) -> ServiceFinancialState:
    if exc.is_emergency:
        reason = f"Emergency clinical override active: {exc.reason}"
    else:
        reason = f"Approved financial exception ({exc.reason_category}): {exc.reason}"
    return ServiceFinancialState(
        charge_exists=state.charge_exists,
        charge_id=state.charge_id,
        status=state.status,
        net_amount=state.net_amount,
        amount_paid=state.amount_paid,
        outstanding_amount=state.outstanding_amount,
        is_cleared=True,
        reason=reason,
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
    exception_scope: str = "step",
) -> ServiceFinancialState:
    """
    Assert that the service charge is financially cleared.
    Raises HTTPException 402 PAYMENT_REQUIRED (or 400 if cancelled) if blocked.
    If an approved FinancialClearanceException scoped to this exact source exists, it is
    consumed and the service proceeds.
    If is_emergency_override is True, the actor is authorized through
    ``financial_authorization.authorize_emergency_override`` (403 when not permitted,
    400 without reason) and a consumed emergency override record + audit are staged.

    ``exception_scope="order"`` (Lab / Radiology): an exception or override already
    consumed by an earlier step of the same service (the order, or the prescription
    request it fulfils) keeps authorizing every later step, so staff never re-override
    each step of one order. ``"step"`` keeps per-step consumption (OT, pharmacy, ...).
    """
    state = check_service_financial_clearance(db, hospital_id, source_type, source_id)
    if state.is_cleared:
        return state

    if state.status == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot {action_description}: billing charge is cancelled.",
        )

    # Automatically apply eligible deposits to cover the service charge if available
    if state.charge_id and (state.outstanding_amount or 0) > 0:
        from modules.billing.entities.billing_entities import BillingCharge
        from modules.billing.services.billing_service import draw_down_account_deposits_for_charge

        charge = db.query(BillingCharge).filter(
            BillingCharge.id == state.charge_id,
            BillingCharge.hospital_id == hospital_id,
        ).first()
        if charge and charge.status != BillingChargeStatus.cancelled:
            allocated = draw_down_account_deposits_for_charge(
                db,
                hospital_id=hospital_id,
                charge=charge,
                created_by_name=str((actor or {}).get("name") or (actor or {}).get("sub") or "Auto Deposit Settlement"),
            )
            if allocated > 0:
                db.flush()
                state = check_service_financial_clearance(db, hospital_id, source_type, source_id)
                if state.is_cleared:
                    return state

    if exception_scope == "order":
        prior = _find_order_scoped_exception(db, hospital_id, source_type, source_id, patient_id)
        if prior is not None:
            return _exception_cleared_state(state, prior)

    # 1. Approved exception scoped to this source (or its linked order / request).
    #    Rows with a NULL source_id never match: they would unlock every source of the type.
    exc_q = (
        db.query(FinancialClearanceException)
        .filter(
            FinancialClearanceException.hospital_id == hospital_id,
            FinancialClearanceException.status == FinancialExceptionStatus.approved.value,
            FinancialClearanceException.is_consumed == False,
            FinancialClearanceException.service_type == source_type.value,
            FinancialClearanceException.source_id.in_(
                _linked_source_ids(db, hospital_id, source_type, source_id)
            ),
        )
    )
    if patient_id:
        exc_q = exc_q.filter(FinancialClearanceException.patient_id == patient_id)
    if admission_id:
        exc_q = exc_q.filter(
            (FinancialClearanceException.admission_id == admission_id)
            | (FinancialClearanceException.admission_id.is_(None))
        )

    approved_exc = exc_q.order_by(FinancialClearanceException.created_at.desc()).first()
    if approved_exc:
        from modules.billing.services.financial_exception_service import (
            ENTITY_TYPE,
            consume_exception,
            exception_audit_details,
        )
        from shared.audit.service import write_audit_log

        consume_exception(approved_exc)
        db.flush()
        write_audit_log(
            db,
            hospital_id=hospital_id,
            actor=actor or {"sub": "system", "name": "System", "role": "system"},
            action="consume",
            entity_type=ENTITY_TYPE,
            entity_id=approved_exc.id,
            summary=f"Financial exception consumed to {action_description} ({source_type.value} {source_id})",
            details=exception_audit_details(approved_exc, consumed_for_source_id=str(source_id)),
        )

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

    # 2. Authorized, recorded emergency override
    if is_emergency_override:
        from modules.billing.services.financial_authorization import (
            OverrideScope,
            authorize_emergency_override,
        )
        from modules.billing.services.financial_exception_service import record_emergency_override

        charge = db.query(BillingCharge).filter(BillingCharge.id == state.charge_id).first() if state.charge_id else None
        scope = OverrideScope.service(
            source_type.value,
            source_id,
            patient_id=patient_id or (charge.patient_id if charge else None),
            admission_id=admission_id,
        )
        override_actor = authorize_emergency_override(
            db, hospital_id, actor, scope=scope, reason=emergency_reason
        )
        em_exc = record_emergency_override(
            db,
            hospital_id,
            override_actor,
            scope,
            emergency_reason or "",
            required_amount=state.net_amount,
            amount_covered=state.amount_paid,
            consumed=True,
            financial_account_id=charge.account_id if charge else None,
        )

        return ServiceFinancialState(
            charge_exists=state.charge_exists,
            charge_id=state.charge_id,
            status=state.status,
            net_amount=state.net_amount,
            amount_paid=state.amount_paid,
            outstanding_amount=state.outstanding_amount,
            is_cleared=True,
            reason=f"Emergency clinical override active: {em_exc.reason}",
        )

    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={
            "code": "FINANCIAL_CLEARANCE_REQUIRED",
            "message": f"Payment required to {action_description}.",
            "service_type": source_type.value,
            "source_id": str(source_id),
            "charge_id": str(state.charge_id) if state.charge_id else None,
            "status": state.status,
            "net_amount": state.net_amount,
            "amount_paid": state.amount_paid,
            "outstanding_amount": state.outstanding_amount,
            "reason": state.reason,
        },
    )


def assert_order_financially_cleared(
    db: Session,
    hospital_id: UUID,
    source_type: BillingSourceType,
    order: Any,
    actor: dict[str, Any] | None,
    *,
    action_description: str,
    is_emergency_override: bool = False,
    emergency_reason: str | None = None,
) -> ServiceFinancialState:
    """Canonical gate for executing a step of a Lab / Radiology order.

    Uses the order's resolved charge (request charge for prescription-originated
    orders), 402 with the structured clearance body when not cleared, and order-scoped
    exceptions: an exception / emergency override used by any earlier step of the same
    order (or of the prescription request it fulfils) authorizes every later step.
    """
    return assert_service_financially_cleared(
        db,
        hospital_id,
        source_type,
        order.id,
        action_description=action_description,
        is_emergency_override=is_emergency_override,
        actor=actor,
        emergency_reason=emergency_reason,
        admission_id=getattr(order, "admission_id", None),
        patient_id=getattr(order, "patient_id", None),
        exception_scope="order",
    )
