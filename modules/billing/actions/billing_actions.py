"""Business actions for Billing domain.

Conforms to UltrionTech-Backend-Template modules/billing/actions/ specification.
Implements charge workflows, payments, invoice snapshots, receipts, and patient ledgers.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from io import BytesIO
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import case, func, literal, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload

from modules.billing.contracts.billing_contracts import (
    BillingChargeCreate,
    BillingChargeResponse,
    BillingChargeUpdate,
    BillingDashboardResponse,
    BillingDepositAllocate,
    BillingDepositCreate,
    BillingDepositResponse,
    BillingInvoiceCreate,
    BillingInvoiceResponse,
    BillingPaymentCreate,
    BillingPaymentResponse,
    BillingReceiptCreate,
    BillingReceiptResponse,
    BillingRefundCreate,
    BillingRefundResponse,
    FinancialAccountCreate,
    FinancialAccountResponse,
    LedgerEntry,
    PatientBillingStatementResponse,
    PatientDuesResponse,
    PatientFinancialSummary,
    PatientLedgerResponse,
    StatementDeposit,
    StatementFinancialSummary,
    StatementHospitalInfo,
    StatementItemizedCharge,
    StatementPatientInfo,
    StatementPayment,
    StatementRefund,
    StatementScopeInfo,
)
from modules.billing.db.billing_repository import BillingRepository
from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingDeposit,
    BillingInvoice,
    BillingInvoiceLine,
    BillingInvoiceStatus,
    BillingPayment,
    BillingPaymentAllocation,
    BillingPaymentMethod,
    BillingReceipt,
    BillingReceiptStatus,
    BillingRefund,
    BillingSourceType,
    DepositStatus,
    FinancialAccount,
    FinancialAccountStatus,
    FinancialAccountType,
    RefundStatus,
)
from modules.billing.services.billing_service import (
    account_to_dict,
    build_ledger_entries,
    charge_to_dict,
    close_financial_account,
    compute_net,
    create_deposit,
    create_payment,
    deposit_to_dict,
    draw_down_deposit_for_charges,
    ensure_charge,
    get_or_create_financial_account,
    patient_ledger_totals,
    payment_to_dict,
    process_refund,
    refund_to_dict,
    recompute_charge_financials,
)
from modules.billing.services.invoice_service import (
    _resolve_encounter_details,
    create_invoice_from_charges,
    deposit_receipt_html,
    invoice_html,
    invoice_to_dict,
    issue_receipt,
    issue_receipt_for_payment,
    patient_outstanding_for_invoice,
    receipt_html,
    receipt_to_dict,
    refund_html,
    refresh_invoice_paid_status,
)
from modules.billing.services.charge_reversal_service import (
    cancel_charge,
    is_charge_service_performed,
    reconcile_charge_after_reprice,
)
from modules.patients.entities.patient import Patient
from modules.tenancy.entities.hospital import Hospital
from shared.audit.service import write_audit_log


def _actor(user: dict[str, Any]) -> str:
    return str(user.get("name") or user.get("sub") or "Staff")


def _get_patient_or_404(repo: BillingRepository, patient_id: UUID) -> Patient:
    patient = repo.get_patient(patient_id)
    if not patient:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
    return patient


def _apply_global_settlement_discount(
    db: Session, hospital_id: UUID, patient_id: UUID, payload: BillingPaymentCreate
) -> None:
    """Fold a top-level settlement discount into ``payload.allocations``.

    A global concession accepted on the contract must never be silently
    dropped. Distribution is proportional to each target's amount due:

    - Explicit allocations: only items without their own discount receive a
      share (per-item entries always win over the global value).
    - No allocations (FIFO tender): a plan is built across all open,
      non-invoiced charges in FIFO order, with tender covering the
      discounted remainder. ``payload.amount`` must equal that remainder.

    Invoiced targets are rejected (same convention as UpdateChargeAction:
    invoiced charges need an adjustment/credit note, not a silent edit).
    Raises ValueError (mapped to 400) on any inconsistency.
    """
    from modules.billing.services.invoice_service import charges_already_invoiced

    disc_amt = round(float(payload.discount_amount or 0), 2)
    disc_pct = float(payload.discount_percent or 0)
    if disc_amt <= 0 and disc_pct <= 0:
        return
    reason = (payload.discount_reason or "").strip()

    if payload.allocations:
        target_ids = [a.charge_id for a in payload.allocations]
        charges = (
            db.query(BillingCharge)
            .filter(
                BillingCharge.id.in_(target_ids),
                BillingCharge.hospital_id == hospital_id,
                BillingCharge.patient_id == patient_id,
                BillingCharge.status.in_(
                    [BillingChargeStatus.pending, BillingChargeStatus.partially_paid]
                ),
            )
            .order_by(BillingCharge.created_at.asc())
            .all()
        )
        by_id = {c.id: c for c in charges}
        # Only items without an explicit discount receive a global share.
        receivable = [
            (a, by_id[a.charge_id])
            for a in payload.allocations
            if a.charge_id in by_id
            and (a.discount_amount or 0) <= 0
            and (a.discount_percent or 0) <= 0
        ]
        if not receivable:
            return
        invoiced = charges_already_invoiced(
            db, hospital_id, [c.id for _, c in receivable]
        )
        if invoiced:
            raise ValueError(
                "Discount cannot be applied at settlement: one or more selected "
                "charges are on an active invoice. Issue an adjustment or credit note."
            )
        dues = {
            c.id: round(float(c.net_amount) - float(c.amount_paid or 0), 2)
            for _, c in receivable
        }
        _split_discount(receivable, dues, disc_amt, disc_pct, reason)
    else:
        charges = (
            db.query(BillingCharge)
            .filter(
                BillingCharge.hospital_id == hospital_id,
                BillingCharge.patient_id == patient_id,
                BillingCharge.status.in_(
                    [BillingChargeStatus.pending, BillingChargeStatus.partially_paid]
                ),
            )
            .order_by(BillingCharge.created_at.asc())
            .all()
        )
        invoiced = set(
            charges_already_invoiced(db, hospital_id, [c.id for c in charges])
        )
        discountable = [c for c in charges if c.id not in invoiced]
        if not discountable:
            raise ValueError(
                "Discount cannot be applied at settlement: all open charges are "
                "on an active invoice. Issue an adjustment or credit note."
            )
        dues = {
            c.id: round(float(c.net_amount) - float(c.amount_paid or 0), 2)
            for c in discountable
        }
        dues = {cid: d for cid, d in dues.items() if d > 0}
        if not dues:
            raise ValueError("There is no outstanding balance to discount.")
        from modules.billing.contracts.billing_contracts import ChargeAllocationItem

        plan = [
            ChargeAllocationItem(charge_id=c.id, amount=dues[c.id])
            for c in discountable
            if c.id in dues
        ]
        receivable = [
            (a, next(c for c in discountable if c.id == a.charge_id)) for a in plan
        ]
        shares = _split_discount(receivable, dues, disc_amt, disc_pct, reason)
        expected_tender = round(
            sum(dues[c.id] - shares[c.id] for _, c in receivable), 2
        )
        if abs(float(payload.amount) - expected_tender) > 0.01:
            raise ValueError(
                f"Payment amount ₹{float(payload.amount):.2f} must equal the discounted "
                f"total ₹{expected_tender:.2f} when a global discount is applied "
                "without explicit allocations."
            )
        payload.allocations = plan


def _split_discount(
    receivable, dues: dict, disc_amt: float, disc_pct: float, reason: str
) -> dict:
    """Assign proportional amount shares (plus pass-through percent) in place.

    Returns {charge_id: share} so callers can reconcile tender without
    stashing private attributes on the pydantic plan items.
    """
    from uuid import UUID as _UUID

    total_due = round(sum(dues.values()), 2)
    if total_due <= 0:
        raise ValueError("There is no outstanding balance to discount.")
    total = min(disc_amt, total_due)
    shares: dict[_UUID, float] = {}
    assigned = 0.0
    for idx, (item, charge) in enumerate(receivable):
        due = dues[charge.id]
        if idx == len(receivable) - 1:
            share = round(total - assigned, 2)
        else:
            share = round(total * due / total_due, 2) if total_due else 0.0
            assigned = round(assigned + share, 2)
        share = max(0.0, min(share, due))
        item.discount_amount = share if share > 0 or disc_amt > 0 else None
        if disc_pct > 0:
            item.discount_percent = disc_pct
        if reason:
            item.discount_reason = reason
        shares[charge.id] = share
    return shares


def _resync_billing_counters(db: Session, hospital_id: UUID) -> None:
    """Resync all billing document counters past legacy MAX+1 rows.

    Called before a single retry after a receipt/invoice numbering
    UniqueViolation. The per-call resync in the number generators normally
    prevents this; this is the backstop for a counter that fell behind
    between generation and flush under concurrency.
    """
    from shared.database.sequences import ensure_counter_at_least

    specs = [
        (BillingInvoice, BillingInvoice.invoice_number, "invoice", "INV"),
        (BillingReceipt, BillingReceipt.receipt_number, "receipt", "RCPT"),
        (BillingDeposit, BillingDeposit.deposit_number, "deposit", "DEP"),
        (BillingRefund, BillingRefund.refund_number, "refund", "REF"),
    ]
    for entity, column, counter_base, prefix in specs:
        years: set[int] = set()
        max_by_year: dict[int, int] = {}
        for (num,) in db.query(column).filter(
            entity.hospital_id == hospital_id,
        ).all():
            try:
                parts = str(num).split("-")
                y, seq = int(parts[1]), int(parts[-1])
            except (ValueError, IndexError):
                continue
            if not str(num).startswith(f"{prefix}-"):
                continue
            years.add(y)
            max_by_year[y] = max(max_by_year.get(y, 0), seq)
        for y in years:
            ensure_counter_at_least(db, hospital_id, f"{counter_base}_{y}", max_by_year[y])
    db.flush()


def _payment_response_dto(
    db: Session,
    pay: BillingPayment,
    patient: Patient | None = None,
    *,
    receipt_id: UUID | None = None,
    receipt_number: str | None = None,
) -> BillingPaymentResponse:
    d = payment_to_dict(pay, patient)
    if receipt_id and receipt_number:
        d["receipt_id"] = receipt_id
        d["receipt_number"] = receipt_number
    else:
        rcpt = (
            db.query(BillingReceipt)
            .filter(
                BillingReceipt.hospital_id == pay.hospital_id,
                BillingReceipt.payment_id == pay.id,
                BillingReceipt.status != BillingReceiptStatus.cancelled,
            )
            .first()
        )
        if rcpt:
            d["receipt_id"] = rcpt.id
            d["receipt_number"] = rcpt.receipt_number
    return BillingPaymentResponse.model_validate(d)


def _payment_response_dtos(
    db: Session,
    payments: list[BillingPayment],
    patient: Patient | None = None,
    receipts: list[BillingReceipt] | None = None,
) -> list[BillingPaymentResponse]:
    """
    Batch version of _payment_response_dto for list/dashboard responses.

    The per-payment version queries BillingReceipt once per payment (N+1 —
    up to 200 extra round trips on the payments list, 5 more on the
    dashboard). This fetches all matching receipts in one query and maps
    them back, producing identical output to calling _payment_response_dto
    in a loop. If receipts are already fetched by the caller (e.g. in patient
    ledger), pass them directly to eliminate redundant round trips.
    """
    if not payments:
        return []
    payment_ids = [p.id for p in payments]
    if receipts is None:
        receipts = (
            db.query(BillingReceipt)
            .filter(
                BillingReceipt.hospital_id == payments[0].hospital_id,
                BillingReceipt.payment_id.in_(payment_ids),
                BillingReceipt.status != BillingReceiptStatus.cancelled,
            )
            .all()
        )
    # Same tie-break as the per-payment query's .first(): first match wins if
    # more than one non-cancelled receipt somehow exists for a payment.
    receipt_by_payment_id: dict[UUID, BillingReceipt] = {}
    for r in receipts:
        if r.payment_id is not None:
            receipt_by_payment_id.setdefault(r.payment_id, r)

    out: list[BillingPaymentResponse] = []
    for p in payments:
        rcpt = receipt_by_payment_id.get(p.id)
        d = payment_to_dict(p, patient)
        if rcpt:
            d["receipt_id"] = rcpt.id
            d["receipt_number"] = rcpt.receipt_number
        out.append(BillingPaymentResponse.model_validate(d))
    return out


# ── Charge Actions ──────────────────────────────────────────────────────────

class ListChargesAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(
        self,
        *,
        patient_id: UUID | None = None,
        status: BillingChargeStatus | None = None,
        source_type: BillingSourceType | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        search: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[BillingChargeResponse]:
        charges = self.repo.list_charges(
            patient_id=patient_id,
            status=status,
            source_type=source_type,
            from_date=from_date,
            to_date=to_date,
            search=search,
            limit=limit,
            offset=offset,
        )
        return [BillingChargeResponse.model_validate(charge_to_dict(c)) for c in charges]


class CreateChargeAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, payload: BillingChargeCreate, user: dict[str, Any]) -> BillingChargeResponse:
        patient = _get_patient_or_404(self.repo, payload.patient_id)
        row = ensure_charge(
            self.db,
            hospital_id=self.repo.hospital_id,
            patient_id=payload.patient_id,
            source_type=payload.source_type,
            source_id=payload.source_id,
            description=payload.description,
            charge_amount=payload.charge_amount,
            discount_amount=payload.discount_amount,
            discount_percent=payload.discount_percent,
            discount_reason=payload.discount_reason,
            quantity=payload.quantity,
            unit_price=payload.unit_price,
            tax_amount=payload.tax_amount,
            gst_rate=payload.gst_rate,
            hsn_sac_code=payload.hsn_sac_code,
            notes=payload.notes,
            created_by_name=_actor(user),
        )
        self.db.commit()
        self.db.refresh(row)
        audit_summary = f"Charge ₹{row.net_amount:.2f} created for {patient.name} ({row.description[:40]})"
        if row.discount_amount > 0:
            audit_summary += f" [Discount: ₹{row.discount_amount:.2f}, Reason: {row.discount_reason}]"
        write_audit_log(
            self.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="create",
            entity_type="billing_charge",
            entity_id=row.id,
            summary=audit_summary,
        )
        self.db.commit()
        return BillingChargeResponse.model_validate(charge_to_dict(row, patient))


class UpdateChargeAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, charge_id: UUID, payload: BillingChargeUpdate, user: dict[str, Any]) -> BillingChargeResponse:
        charge = self.repo.get_charge_by_id(charge_id)
        if not charge:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Charge not found")
        if charge.status == BillingChargeStatus.cancelled:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot edit a cancelled charge")
        if payload.status is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Charge status cannot be edited: paid / partially paid / pending are derived from "
                    "recorded payments and deposit allocations. Record a payment to settle it, or use "
                    "the cancel action to cancel it."
                ),
            )

        amount_fields_changed = (
            payload.discount_amount is not None
            or payload.discount_percent is not None
            or payload.tax_amount is not None
            or payload.quantity is not None
            or payload.unit_price is not None
        )
        from modules.billing.services.invoice_service import charges_already_invoiced
        if amount_fields_changed and charges_already_invoiced(self.db, self.repo.hospital_id, [charge.id]):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot modify financial amounts or discounts on an invoiced charge. Issue an adjustment or credit note.",
            )

        if payload.description is not None:
            charge.description = payload.description.strip()[:512]
        if payload.notes is not None:
            charge.notes = payload.notes

        old_net = float(charge.net_amount or 0)
        old_disc = float(charge.discount_amount or 0)
        if payload.quantity is not None or payload.unit_price is not None:
            qty = float(payload.quantity if payload.quantity is not None else charge.quantity or 1)
            unit = float(payload.unit_price if payload.unit_price is not None else charge.unit_price or 0)
            charge.quantity = qty
            charge.unit_price = round(unit, 2)
            charge.charge_amount = round(qty * unit, 2)
        if payload.tax_amount is not None:
            charge.tax_amount = round(float(payload.tax_amount), 2)
        disc_amt = payload.discount_amount if payload.discount_amount is not None else old_disc
        disc_pct = payload.discount_percent if payload.discount_percent is not None else charge.discount_percent
        disc, net = compute_net(
            float(charge.charge_amount or 0), disc_amt, disc_pct, float(charge.tax_amount or 0)
        )
        charge.discount_amount = disc
        charge.discount_percent = disc_pct
        if payload.discount_reason is not None:
            charge.discount_reason = payload.discount_reason.strip() or None
        charge.net_amount = net

        released = reconcile_charge_after_reprice(
            self.db, charge, reason=f"Charge re-priced from ₹{old_net:.2f} to ₹{net:.2f}", actor=user
        )

        audit_summary = f"Updated charge {charge.id} (Net: ₹{float(charge.net_amount):.2f})"
        if disc > 0 and disc != old_disc:
            audit_summary += f" [Discount updated: ₹{disc:.2f}, Reason: {charge.discount_reason}]"
        write_audit_log(
            self.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="update",
            entity_type="billing_charge",
            entity_id=charge.id,
            summary=audit_summary,
            details={
                "patient_id": str(charge.patient_id),
                "account_id": str(charge.account_id) if charge.account_id else None,
                "old_net_amount": old_net,
                "net_amount": float(charge.net_amount),
                "status": charge.status.value,
                "released_to_credit": released.amount_to_credit if released else 0.0,
                "credit_deposit_id": str(released.credit_deposit_id) if released and released.credit_deposit_id else None,
            },
        )
        self.db.commit()
        self.db.refresh(charge)
        return BillingChargeResponse.model_validate(charge_to_dict(charge))



class CancelChargeAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, charge_id: UUID, user: dict[str, Any]) -> BillingChargeResponse:
        charge = self.repo.get_charge_by_id(charge_id)
        if not charge:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Charge not found")

        cancel_charge(
            self.db,
            charge,
            reason="Cancelled at billing desk",
            actor=user,
            performed=is_charge_service_performed(self.db, charge),
        )
        self.db.commit()
        self.db.refresh(charge)
        return BillingChargeResponse.model_validate(charge_to_dict(charge))


# ── Payment Actions ─────────────────────────────────────────────────────────

class ListPaymentsAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(
        self,
        *,
        patient_id: UUID | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        payment_method: BillingPaymentMethod | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[BillingPaymentResponse]:
        payments = self.repo.list_payments(
            patient_id=patient_id,
            from_date=from_date,
            to_date=to_date,
            payment_method=payment_method,
            limit=limit,
            offset=offset,
        )
        return _payment_response_dtos(self.db, payments)


class CreatePaymentAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, payload: BillingPaymentCreate, user: dict[str, Any]) -> BillingPaymentResponse:
        patient = _get_patient_or_404(self.repo, payload.patient_id)
        pdate = payload.payment_date or date.today()

        # Fold a top-level settlement concession into per-charge allocations
        # (or build the plan for FIFO tenders). Never silently drop it.
        try:
            _apply_global_settlement_discount(
                self.db, self.repo.hospital_id, payload.patient_id, payload
            )
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

        # Convert Pydantic allocation models if provided
        allocations_plan = None
        if payload.allocations:
            allocations_plan = [
                {
                    "charge_id": item.charge_id,
                    "amount": item.amount,
                    "discount_amount": item.discount_amount,
                    "discount_percent": item.discount_percent,
                    "discount_reason": item.discount_reason or payload.discount_reason,
                }
                for item in payload.allocations
            ]

        if payload.idempotency_key:
            existing = (
                self.db.query(BillingPayment)
                .filter(
                    BillingPayment.hospital_id == self.repo.hospital_id,
                    BillingPayment.idempotency_key == payload.idempotency_key,
                )
                .first()
            )
            if existing:
                return _payment_response_dto(self.db, existing, patient)

        # Explicit charge settlements always retain an itemized bill. Reuse the
        # one invoice that already contains every selected charge; otherwise
        # snapshot precisely these selected charges before applying payment.
        selected_charge_ids = list(dict.fromkeys(
            item["charge_id"] for item in (allocations_plan or []) if item.get("charge_id")
        ))
        created_invoice_for_payment = False
        if selected_charge_ids:
            existing_lines = (
                self.db.query(BillingInvoiceLine.charge_id, BillingInvoiceLine.invoice_id)
                .join(BillingInvoice, BillingInvoice.id == BillingInvoiceLine.invoice_id)
                .filter(
                    BillingInvoice.hospital_id == self.repo.hospital_id,
                    BillingInvoice.patient_id == payload.patient_id,
                    BillingInvoiceLine.charge_id.in_(selected_charge_ids),
                    BillingInvoice.status != BillingInvoiceStatus.cancelled,
                ).all()
            )
            by_charge = {charge_id: invoice_id for charge_id, invoice_id in existing_lines}
            invoice_ids = set(by_charge.values())
            if len(invoice_ids) > 1 or (invoice_ids and len(by_charge) != len(selected_charge_ids)):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Selected charges belong to different bills. Collect payment against one invoice at a time.",
                )
            if invoice_ids:
                effective_invoice_id = next(iter(invoice_ids))
                if payload.linked_invoice_id and payload.linked_invoice_id != effective_invoice_id:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Selected charges do not belong to the linked invoice")
            else:
                if payload.linked_invoice_id:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Selected charges are not on the linked invoice.",
                    )
                try:
                    # Apply per-line settlement concessions before taking the
                    # immutable invoice snapshot, using the same calculation
                    # as the allocator. The allocator fields are cleared after
                    # snapshotting below to prevent a second application.
                    charge_map = {c.id: c for c in self.db.query(BillingCharge).filter(
                        BillingCharge.id.in_(selected_charge_ids),
                        BillingCharge.hospital_id == self.repo.hospital_id,
                        BillingCharge.patient_id == payload.patient_id,
                    ).all()}
                    for allocation in allocations_plan or []:
                        charge = charge_map.get(allocation["charge_id"])
                        if not charge:
                            continue
                        if allocation.get("discount_amount") is not None:
                            charge.discount_amount = round(float(allocation["discount_amount"]), 2)
                        if allocation.get("discount_percent") is not None:
                            charge.discount_percent = round(float(allocation["discount_percent"]), 2)
                        if allocation.get("discount_reason"):
                            charge.discount_reason = allocation["discount_reason"]
                        if allocation.get("discount_amount") is not None or allocation.get("discount_percent") is not None:
                            disc, net = compute_net(
                                float(charge.charge_amount or 0), float(charge.discount_amount or 0),
                                charge.discount_percent, float(charge.tax_amount or 0),
                            )
                            charge.discount_amount, charge.net_amount = disc, net
                            recompute_charge_financials(self.db, charge)
                    inv = create_invoice_from_charges(
                        self.db,
                        hospital_id=self.repo.hospital_id,
                        patient_id=payload.patient_id,
                        charge_ids=selected_charge_ids,
                        account_id=payload.account_id,
                        created_by_name=_actor(user),
                    )
                    effective_invoice_id = inv.id
                    created_invoice_for_payment = True
                except ValueError as e:
                    self.db.rollback()
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        if created_invoice_for_payment and allocations_plan:
            # Settlement concessions were applied to charge snapshots before
            # this invoice was created. Do not ask the allocator to apply them
            # a second time after the charge has become invoiced.
            for allocation in allocations_plan:
                allocation["discount_amount"] = None
                allocation["discount_percent"] = None
                allocation["discount_reason"] = None
        else:
            effective_invoice_id = payload.linked_invoice_id

        try:
            pay = create_payment(
                self.db,
                hospital_id=self.repo.hospital_id,
                patient_id=payload.patient_id,
                amount=payload.amount,
                payment_date=pdate,
                payment_method=payload.payment_method,
                account_id=payload.account_id,
                reference_number=payload.reference_number,
                idempotency_key=payload.idempotency_key,
                notes=payload.notes,
                received_by_name=_actor(user),
                allocate=True,
                allocations_plan=allocations_plan,
            )
        except ValueError as e:
            # Settlement-guard rejection (e.g. discount on an invoiced charge).
            self.db.rollback()
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        if not effective_invoice_id and pay.allocations:
            allocated_charge_ids = [a.charge_id for a in pay.allocations if a.charge_id]
            if allocated_charge_ids:
                inv_lines = (
                    self.db.query(BillingInvoiceLine.invoice_id)
                    .join(BillingInvoice, BillingInvoice.id == BillingInvoiceLine.invoice_id)
                    .filter(
                        BillingInvoiceLine.charge_id.in_(allocated_charge_ids),
                        BillingInvoice.status != BillingInvoiceStatus.cancelled,
                    )
                    .distinct()
                    .all()
                )
                if len(inv_lines) == 1 and inv_lines[0][0]:
                    effective_invoice_id = inv_lines[0][0]

        try:
            receipt = issue_receipt_for_payment(
                self.db,
                pay,
                linked_invoice_id=effective_invoice_id,
                reference_number=payload.reference_number,
            )
            refresh_invoice_paid_status(self.db, self.repo.hospital_id, payload.patient_id)
            self.db.commit()
        except IntegrityError as e:
            # Receipt/invoice numbering collision (stale sequence counter behind
            # legacy MAX+1 rows, or concurrent writers). Roll back the whole
            # payment attempt so a client retry cannot mint a duplicate payment,
            # resync the counters past existing rows, and retry exactly once.
            self.db.rollback()
            if "uq_billing_receipt_number" not in str(e.orig or e) and \
               "uq_billing_invoice_number" not in str(e.orig or e):
                raise
            _resync_billing_counters(self.db, self.repo.hospital_id)
            pay = create_payment(
                self.db,
                hospital_id=self.repo.hospital_id,
                patient_id=payload.patient_id,
                amount=payload.amount,
                payment_date=pdate,
                payment_method=payload.payment_method,
                account_id=payload.account_id,
                reference_number=payload.reference_number,
                notes=payload.notes,
                received_by_name=_actor(user),
                allocate=True,
                allocations_plan=allocations_plan,
            )
            try:
                receipt = issue_receipt_for_payment(
                    self.db,
                    pay,
                    linked_invoice_id=effective_invoice_id,
                    reference_number=payload.reference_number,
                )
                refresh_invoice_paid_status(self.db, self.repo.hospital_id, payload.patient_id)
                self.db.commit()
            except IntegrityError:
                self.db.rollback()
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Receipt numbering conflict — please retry recording the payment",
                )
        self.db.refresh(pay)
        self.db.refresh(receipt)
        write_audit_log(
            self.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="create",
            entity_type="billing_payment",
            entity_id=pay.id,
            summary=f"Payment ₹{pay.amount:.2f} received for {patient.name} ({receipt.receipt_number})",
        )
        self.db.commit()
        return _payment_response_dto(
            self.db, pay, patient, receipt_id=receipt.id, receipt_number=receipt.receipt_number
        )


# ── Invoice Actions ─────────────────────────────────────────────────────────

class ListInvoicesAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(
        self,
        *,
        patient_id: UUID | None = None,
        status: BillingInvoiceStatus | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        search: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[BillingInvoiceResponse]:
        invoices = self.repo.list_invoices(
            patient_id=patient_id,
            status=status,
            from_date=from_date,
            to_date=to_date,
            search=search,
            limit=limit,
            offset=offset,
        )
        # include_lines=False: the Invoices tab table never reads line items
        # (only the "view invoice" detail modal does, which fetches its own
        # full detail via GET /invoices/{id} — see BillingPage.tsx's
        # setViewInvoice). Dropping lines here trims this list response and
        # lets the repository skip eager-loading them (see list_invoices()
        # in billing_repository.py).
        return [
            BillingInvoiceResponse.model_validate(invoice_to_dict(inv, include_lines=False))
            for inv in invoices
        ]


class GetInvoiceAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, invoice_id: UUID) -> BillingInvoiceResponse:
        inv = self.repo.get_invoice_by_id(invoice_id)
        if not inv:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
        return BillingInvoiceResponse.model_validate(invoice_to_dict(inv))


class CreateInvoiceAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, payload: BillingInvoiceCreate, user: dict[str, Any]) -> BillingInvoiceResponse:
        patient = _get_patient_or_404(self.repo, payload.patient_id)
        try:
            inv = create_invoice_from_charges(
                self.db,
                hospital_id=self.repo.hospital_id,
                patient_id=payload.patient_id,
                charge_ids=payload.charge_ids,
                account_id=payload.account_id,
                invoice_date=payload.invoice_date,
                tax_amount=payload.tax_amount,
                cgst_amount=payload.cgst_amount,
                sgst_amount=payload.sgst_amount,
                igst_amount=payload.igst_amount,
                notes=payload.notes,
                created_by_name=_actor(user),
            )
            try:
                self.db.commit()
            except IntegrityError as e:
                # Invoice numbering collision (stale counter) — resync + retry once.
                self.db.rollback()
                if "uq_billing_invoice_number" not in str(e.orig or e):
                    raise
                _resync_billing_counters(self.db, self.repo.hospital_id)
                inv = create_invoice_from_charges(
                    self.db,
                    hospital_id=self.repo.hospital_id,
                    patient_id=payload.patient_id,
                    charge_ids=payload.charge_ids,
                    account_id=payload.account_id,
                    invoice_date=payload.invoice_date,
                    tax_amount=payload.tax_amount,
                    cgst_amount=payload.cgst_amount,
                    sgst_amount=payload.sgst_amount,
                    igst_amount=payload.igst_amount,
                    notes=payload.notes,
                    created_by_name=_actor(user),
                )
                try:
                    self.db.commit()
                except IntegrityError:
                    self.db.rollback()
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Invoice numbering conflict — please retry generating the invoice",
                    )
            self.db.refresh(inv)
        except ValueError as e:
            self.db.rollback()
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

        write_audit_log(
            self.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="create",
            entity_type="billing_invoice",
            entity_id=inv.id,
            summary=f"Invoice {inv.invoice_number} (₹{inv.grand_total:.2f}) created for {patient.name}",
        )
        self.db.commit()
        return BillingInvoiceResponse.model_validate(invoice_to_dict(inv, patient))


class CancelInvoiceAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, invoice_id: UUID, user: dict[str, Any]) -> BillingInvoiceResponse:
        inv = self.repo.get_invoice_by_id(invoice_id)
        if not inv:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
        inv.status = BillingInvoiceStatus.cancelled
        self.db.commit()
        self.db.refresh(inv)
        write_audit_log(
            self.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="cancel",
            entity_type="billing_invoice",
            entity_id=inv.id,
            summary=f"Cancelled invoice {inv.invoice_number}",
        )
        self.db.commit()
        return BillingInvoiceResponse.model_validate(invoice_to_dict(inv))


class PrintInvoiceAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, invoice_id: UUID, auto_print: bool = True) -> str:
        inv = self.repo.get_invoice_by_id(invoice_id)
        if not inv:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
        hospital = self.db.query(Hospital).filter(Hospital.id == self.repo.hospital_id).first()
        patient = inv.patient or self.repo.get_patient(inv.patient_id)
        encounter = _resolve_encounter_details(self.db, getattr(inv, "account_id", None))
        charge_ids = [line.charge_id for line in inv.lines if line.charge_id]
        charges = self.db.query(BillingCharge).filter(BillingCharge.id.in_(charge_ids)).all() if charge_ids else []
        amount_paid = min(float(inv.grand_total or 0), round(sum(float(c.amount_paid or 0) for c in charges), 2))
        by_payment: dict[UUID, float] = {}
        if charge_ids:
            for payment_id, allocated in (
                self.db.query(BillingPaymentAllocation.payment_id, func.sum(BillingPaymentAllocation.allocated_amount))
                .filter(
                    BillingPaymentAllocation.charge_id.in_(charge_ids),
                    BillingPaymentAllocation.payment_id.isnot(None),
                ).group_by(BillingPaymentAllocation.payment_id).all()
            ):
                if payment_id and float(allocated or 0) > 0:
                    by_payment[payment_id] = float(allocated)
        receipt_query = self.db.query(BillingReceipt).filter(
            BillingReceipt.hospital_id == self.repo.hospital_id,
            BillingReceipt.patient_id == inv.patient_id,
            BillingReceipt.status != BillingReceiptStatus.cancelled,
        )
        receipt_filters = [BillingReceipt.linked_invoice_id == inv.id]
        if by_payment:
            receipt_filters.append(BillingReceipt.payment_id.in_(by_payment))
        receipts = receipt_query.filter(or_(*receipt_filters)).all()
        payment_rows = [
            {
                "amount": float(r.amount or 0),
                "method": r.payment_method.value.replace("_", " ").title() if r.payment_method else "Payment",
                "date": r.payment_date.strftime("%d/%m/%Y") if r.payment_date else "",
                "receipt": r.receipt_number,
            }
            for r in receipts
        ]
        return invoice_html(
            inv, hospital, patient, encounter=encounter, auto_print=auto_print,
            settlement={"amount_paid": amount_paid, "payments": payment_rows},
        )


# ── Receipt Actions ─────────────────────────────────────────────────────────

class ListReceiptsAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(
        self,
        *,
        patient_id: UUID | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[BillingReceiptResponse]:
        receipts = self.repo.list_receipts(
            patient_id=patient_id, from_date=from_date, to_date=to_date, limit=limit, offset=offset
        )
        return [BillingReceiptResponse.model_validate(receipt_to_dict(r)) for r in receipts]


class GetReceiptAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, receipt_id: UUID) -> BillingReceiptResponse:
        r = self.repo.get_receipt_by_id(receipt_id)
        if not r:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Receipt not found")
        return BillingReceiptResponse.model_validate(receipt_to_dict(r))


class CreateReceiptAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, payload: BillingReceiptCreate, user: dict[str, Any]) -> BillingReceiptResponse:
        patient = _get_patient_or_404(self.repo, payload.patient_id)
        pdate = payload.payment_date or date.today()
        pay: BillingPayment | None = None
        if payload.create_payment:
            pay = create_payment(
                self.db,
                hospital_id=self.repo.hospital_id,
                patient_id=payload.patient_id,
                amount=payload.amount,
                payment_date=pdate,
                payment_method=payload.payment_method,
                notes=payload.notes,
                received_by_name=_actor(user),
                allocate=True,
            )
        try:
            rcpt = issue_receipt(
                self.db,
                hospital_id=self.repo.hospital_id,
                patient_id=payload.patient_id,
                amount=payload.amount,
                payment_date=pdate,
                payment_method=payload.payment_method,
                collected_by_name=_actor(user),
                reference_number=payload.reference_number,
                notes=payload.notes,
                payment_id=pay.id if pay else None,
                linked_invoice_id=payload.linked_invoice_id,
            )
            refresh_invoice_paid_status(self.db, self.repo.hospital_id, payload.patient_id)
            self.db.commit()
            self.db.refresh(rcpt)
        except ValueError as e:
            self.db.rollback()
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

        write_audit_log(
            self.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="create",
            entity_type="billing_receipt",
            entity_id=rcpt.id,
            summary=f"Receipt {rcpt.receipt_number} issued for {patient.name}",
        )
        self.db.commit()
        return BillingReceiptResponse.model_validate(receipt_to_dict(rcpt, patient))


class CancelReceiptAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, receipt_id: UUID, user: dict[str, Any]) -> BillingReceiptResponse:
        r = self.repo.get_receipt_by_id(receipt_id)
        if not r:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Receipt not found")
        r.status = BillingReceiptStatus.cancelled
        self.db.commit()
        self.db.refresh(r)
        write_audit_log(
            self.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="cancel",
            entity_type="billing_receipt",
            entity_id=r.id,
            summary=f"Cancelled receipt {r.receipt_number}",
        )
        self.db.commit()
        return BillingReceiptResponse.model_validate(receipt_to_dict(r))


class PrintReceiptAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, receipt_id: UUID, auto_print: bool = True) -> str:
        r = self.repo.get_receipt_by_id(receipt_id)
        if not r:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Receipt not found")
        hospital = self.db.query(Hospital).filter(Hospital.id == self.repo.hospital_id).first()
        patient = r.patient or self.repo.get_patient(r.patient_id)
        linked_inv = None
        if r.linked_invoice_id:
            linked_inv = self.db.query(BillingInvoice).filter(BillingInvoice.id == r.linked_invoice_id).first()
        paid_against = []
        if r.payment_id:
            allocs = self.db.query(BillingPaymentAllocation).filter(
                BillingPaymentAllocation.payment_id == r.payment_id
            ).all()
            reversed_amount = {}
            for allocation in allocs:
                if allocation.reversal_of_id:
                    reversed_amount[allocation.reversal_of_id] = reversed_amount.get(allocation.reversal_of_id, 0) + float(allocation.allocated_amount or 0)
            totals = {}
            for allocation in allocs:
                if allocation.reversal_of_id is None and float(allocation.allocated_amount or 0) > 0:
                    remaining = round(float(allocation.allocated_amount) + reversed_amount.get(allocation.id, 0), 2)
                    if remaining > 0:
                        totals[allocation.charge_id] = totals.get(allocation.charge_id, 0) + remaining
            charges = self.db.query(BillingCharge).filter(BillingCharge.id.in_(totals)).all() if totals else []
            charge_objs = {c.id: c for c in charges}
            paid_against = [
                {
                    "description": charge_objs[cid].description if cid in charge_objs else "Charge",
                    "amount": amount,
                    "source_type": charge_objs[cid].source_type.value if cid in charge_objs and charge_objs[cid].source_type else "",
                }
                for cid, amount in totals.items()
            ]
        return receipt_html(
            r, hospital, patient, linked_invoice=linked_inv, auto_print=auto_print,
            paid_against=paid_against,
        )


# ── Financial Account Actions ───────────────────────────────────────────────

class ListFinancialAccountsAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(
        self,
        *,
        patient_id: UUID | None = None,
        status: FinancialAccountStatus | None = None,
        account_type: FinancialAccountType | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[FinancialAccountResponse]:
        accounts = self.repo.list_accounts(
            patient_id=patient_id, status=status, account_type=account_type, limit=limit, offset=offset
        )
        return [FinancialAccountResponse.model_validate(account_to_dict(acc)) for acc in accounts]


class CreateFinancialAccountAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, payload: FinancialAccountCreate, user: dict[str, Any]) -> FinancialAccountResponse:
        patient = _get_patient_or_404(self.repo, payload.patient_id)
        acc = get_or_create_financial_account(
            self.db,
            hospital_id=self.repo.hospital_id,
            patient_id=payload.patient_id,
            account_type=payload.account_type,
            admission_id=payload.admission_id,
            appointment_id=payload.appointment_id,
            notes=payload.notes,
            created_by_name=_actor(user),
        )
        self.db.commit()
        self.db.refresh(acc)
        write_audit_log(
            self.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="create",
            entity_type="financial_account",
            entity_id=acc.id,
            summary=f"Financial account {acc.account_number} ({acc.account_type.value}) created for {patient.name}",
        )
        self.db.commit()
        return FinancialAccountResponse.model_validate(account_to_dict(acc, patient))


class CloseFinancialAccountAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, account_id: UUID, user: dict[str, Any]) -> FinancialAccountResponse:
        try:
            acc = close_financial_account(self.db, hospital_id=self.repo.hospital_id, account_id=account_id)
            self.db.commit()
            self.db.refresh(acc)
        except ValueError as e:
            self.db.rollback()
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        write_audit_log(
            self.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="close",
            entity_type="financial_account",
            entity_id=acc.id,
            summary=f"Closed financial account {acc.account_number}",
        )
        self.db.commit()
        return FinancialAccountResponse.model_validate(account_to_dict(acc))


# ── Deposit Actions ─────────────────────────────────────────────────────────

class ListDepositsAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(
        self,
        *,
        patient_id: UUID | None = None,
        account_id: UUID | None = None,
        admission_id: UUID | None = None,
        status: DepositStatus | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[BillingDepositResponse]:
        deposits = self.repo.list_deposits(
            patient_id=patient_id,
            account_id=account_id,
            admission_id=admission_id,
            status=status,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            offset=offset,
        )
        return [BillingDepositResponse.model_validate(deposit_to_dict(d)) for d in deposits]


class CreateDepositAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, payload: BillingDepositCreate, user: dict[str, Any]) -> BillingDepositResponse:
        patient = _get_patient_or_404(self.repo, payload.patient_id)
        dep_date = payload.deposit_date or date.today()

        if payload.idempotency_key:
            existing = (
                self.db.query(BillingDeposit)
                .filter(
                    BillingDeposit.hospital_id == self.repo.hospital_id,
                    BillingDeposit.idempotency_key == payload.idempotency_key,
                )
                .first()
            )
            if existing:
                return BillingDepositResponse.model_validate(deposit_to_dict(existing, patient))

        try:
            deposit = create_deposit(
                self.db,
                hospital_id=self.repo.hospital_id,
                patient_id=payload.patient_id,
                amount=payload.amount,
                deposit_date=dep_date,
                deposit_type=payload.deposit_type,
                payment_method=payload.payment_method,
                account_id=payload.account_id,
                admission_id=payload.admission_id,
                reference_number=payload.reference_number,
                idempotency_key=payload.idempotency_key,
                notes=payload.notes,
                received_by_name=_actor(user),
            )
        except ValueError as e:
            self.db.rollback()
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        
        # Issue corresponding money receipt for the advance deposit
        rcpt = issue_receipt(
            self.db,
            hospital_id=self.repo.hospital_id,
            patient_id=payload.patient_id,
            amount=payload.amount,
            payment_date=dep_date,
            payment_method=payload.payment_method,
            collected_by_name=_actor(user),
            reference_number=payload.reference_number,
            notes=f"Advance Deposit: {deposit.deposit_number}",
        )
        rcpt.deposit_id = deposit.id
        self.db.commit()
        self.db.refresh(deposit)

        write_audit_log(
            self.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="create",
            entity_type="billing_deposit",
            entity_id=deposit.id,
            summary=f"Advance deposit {deposit.deposit_number} (₹{deposit.original_amount:.2f}) received for {patient.name}",
        )
        if deposit.admission_id:
            write_audit_log(
                self.db,
                hospital_id=self.repo.hospital_id,
                actor=user,
                action="financial_update",
                entity_type="admission",
                entity_id=deposit.admission_id,
                summary=f"Advance deposit {deposit.deposit_number} (₹{deposit.original_amount:.2f}) linked to admission",
                details={"deposit_id": str(deposit.id), "amount": float(deposit.original_amount)},
            )
        self.db.commit()
        return BillingDepositResponse.model_validate(deposit_to_dict(deposit, patient))


class AllocateDepositAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(
        self, deposit_id: UUID, payload: BillingDepositAllocate, user: dict[str, Any]
    ) -> BillingDepositResponse:
        alloc_list = [{"charge_id": item.charge_id, "amount": item.amount} for item in payload.allocations]
        try:
            allocated = draw_down_deposit_for_charges(
                self.db,
                hospital_id=self.repo.hospital_id,
                deposit_id=deposit_id,
                allocations_plan=alloc_list,
                created_by_name=_actor(user),
            )
            deposit = self.repo.get_deposit_by_id(deposit_id)
            if not deposit:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deposit not found")
            refresh_invoice_paid_status(self.db, self.repo.hospital_id, deposit.patient_id)
            self.db.commit()
            self.db.refresh(deposit)
        except ValueError as e:
            self.db.rollback()
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

        write_audit_log(
            self.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="allocate",
            entity_type="billing_deposit",
            entity_id=deposit_id,
            summary=f"Allocated ₹{allocated:.2f} from deposit {deposit.deposit_number} to {len(payload.allocations)} charges",
        )
        self.db.commit()
        return BillingDepositResponse.model_validate(deposit_to_dict(deposit))


class PrintDepositAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, deposit_id: UUID, auto_print: bool = True) -> str:
        dep = self.repo.get_deposit_by_id(deposit_id)
        if not dep:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deposit not found")
        hospital = self.db.query(Hospital).filter(Hospital.id == self.repo.hospital_id).first()
        patient = self.repo.get_patient(dep.patient_id)
        encounter = _resolve_encounter_details(self.db, getattr(dep, "account_id", None))
        return deposit_receipt_html(dep, hospital, patient, encounter=encounter, auto_print=auto_print)


# ── Refund Actions ──────────────────────────────────────────────────────────

class ListRefundsAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(
        self,
        *,
        patient_id: UUID | None = None,
        status: RefundStatus | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[BillingRefundResponse]:
        refunds = self.repo.list_refunds(
            patient_id=patient_id, status=status, from_date=from_date, to_date=to_date, limit=limit, offset=offset
        )
        return [BillingRefundResponse.model_validate(refund_to_dict(ref)) for ref in refunds]


class CreateRefundAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, payload: BillingRefundCreate, user: dict[str, Any]) -> BillingRefundResponse:
        patient = _get_patient_or_404(self.repo, payload.patient_id)
        ref_date = payload.refund_date or date.today()
        try:
            refund = process_refund(
                self.db,
                hospital_id=self.repo.hospital_id,
                patient_id=payload.patient_id,
                amount=payload.amount,
                reason=payload.reason,
                refund_date=ref_date,
                refund_method=payload.refund_method,
                payment_id=payload.payment_id,
                deposit_id=payload.deposit_id,
                account_id=payload.account_id,
                approved_by_name=_actor(user),
                processed_by_name=_actor(user),
                charge_ids=payload.charge_ids,
            )
        except ValueError as e:
            self.db.rollback()
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

        reversed_charge_ids = sorted({
            str(row[0])
            for row in self.db.query(BillingPaymentAllocation.charge_id)
            .filter(BillingPaymentAllocation.refund_id == refund.id)
            .all()
        })
        admission_id = None
        if refund.account_id:
            acc = self.db.query(FinancialAccount).filter(FinancialAccount.id == refund.account_id).first()
            admission_id = acc.admission_id if acc else None
        write_audit_log(
            self.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="refund",
            entity_type="billing_refund",
            entity_id=refund.id,
            summary=f"Refund voucher {refund.refund_number} (₹{float(refund.amount):.2f}) issued for {patient.name}: {refund.reason[:40]}",
            details={
                "patient_id": str(refund.patient_id),
                "account_id": str(refund.account_id) if refund.account_id else None,
                "admission_id": str(admission_id) if admission_id else None,
                "payment_id": str(refund.payment_id) if refund.payment_id else None,
                "deposit_id": str(refund.deposit_id) if refund.deposit_id else None,
                "amount": float(refund.amount),
                "reversed_charge_ids": reversed_charge_ids,
            },
        )
        self.db.commit()
        self.db.refresh(refund)
        return BillingRefundResponse.model_validate(refund_to_dict(refund, patient))


class PrintRefundAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, refund_id: UUID, auto_print: bool = True) -> str:
        ref = self.repo.get_refund_by_id(refund_id)
        if not ref:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Refund voucher not found")
        hospital = self.db.query(Hospital).filter(Hospital.id == self.repo.hospital_id).first()
        patient = self.repo.get_patient(ref.patient_id)
        return refund_html(ref, hospital, patient, auto_print=auto_print)


# ── Ledger & Dashboard Actions ──────────────────────────────────────────────

class GetPatientLedgerAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(
        self,
        patient_id: UUID,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
        source_type: BillingSourceType | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> PatientLedgerResponse:
        patient = _get_patient_or_404(self.repo, patient_id)

        # Base charges query with eager account loading
        q_chg = (
            self.db.query(BillingCharge)
            .options(joinedload(BillingCharge.account))
            .filter(
                BillingCharge.hospital_id == self.repo.hospital_id,
                BillingCharge.patient_id == patient_id,
            )
        )
        if date_from:
            q_chg = q_chg.filter(BillingCharge.created_at >= datetime.combine(date_from, time.min))
        if date_to:
            q_chg = q_chg.filter(BillingCharge.created_at <= datetime.combine(date_to, time.max))
        if source_type:
            q_chg = q_chg.filter(BillingCharge.source_type == source_type)
        all_charges = q_chg.order_by(BillingCharge.created_at.desc()).all()

        non_cancelled_charges = [c for c in all_charges if c.status != BillingChargeStatus.cancelled]

        # Base payments query
        q_pay = (
            self.db.query(BillingPayment)
            .filter(
                BillingPayment.hospital_id == self.repo.hospital_id,
                BillingPayment.patient_id == patient_id,
            )
        )
        if date_from:
            q_pay = q_pay.filter(BillingPayment.payment_date >= date_from)
        if date_to:
            q_pay = q_pay.filter(BillingPayment.payment_date <= date_to)
        payments = q_pay.order_by(BillingPayment.created_at.desc()).all()

        # Deposits query
        q_dep = (
            self.db.query(BillingDeposit)
            .filter(
                BillingDeposit.hospital_id == self.repo.hospital_id,
                BillingDeposit.patient_id == patient_id,
            )
        )
        if date_from:
            q_dep = q_dep.filter(BillingDeposit.deposit_date >= date_from)
        if date_to:
            q_dep = q_dep.filter(BillingDeposit.deposit_date <= date_to)
        deposits = q_dep.order_by(BillingDeposit.created_at.desc()).all()

        # Refunds query
        q_ref = (
            self.db.query(BillingRefund)
            .filter(
                BillingRefund.hospital_id == self.repo.hospital_id,
                BillingRefund.patient_id == patient_id,
            )
        )
        if date_from:
            q_ref = q_ref.filter(BillingRefund.refund_date >= date_from)
        if date_to:
            q_ref = q_ref.filter(BillingRefund.refund_date <= date_to)
        refunds = q_ref.order_by(BillingRefund.created_at.desc()).all()

        active_deposits = [
            d for d in deposits
            if d.status in (DepositStatus.available, DepositStatus.partially_allocated)
        ]
        totals = patient_ledger_totals(
            self.db,
            self.repo.hospital_id,
            patient_id,
            charges=non_cancelled_charges,
            payments=payments,
            deposits=active_deposits,
        )
        charges = non_cancelled_charges

        # Invoices with lines eager loaded (no N+1)
        q_inv = (
            self.db.query(BillingInvoice)
            .options(selectinload(BillingInvoice.lines))
            .filter(
                BillingInvoice.hospital_id == self.repo.hospital_id,
                BillingInvoice.patient_id == patient_id,
                BillingInvoice.status != BillingInvoiceStatus.cancelled,
            )
        )
        if date_from:
            q_inv = q_inv.filter(BillingInvoice.invoice_date >= date_from)
        if date_to:
            q_inv = q_inv.filter(BillingInvoice.invoice_date <= date_to)
        invoices = q_inv.order_by(BillingInvoice.created_at.desc()).all()

        # Receipts query
        q_rcp = (
            self.db.query(BillingReceipt)
            .filter(
                BillingReceipt.hospital_id == self.repo.hospital_id,
                BillingReceipt.patient_id == patient_id,
                BillingReceipt.status != BillingReceiptStatus.cancelled,
            )
        )
        if date_from:
            q_rcp = q_rcp.filter(BillingReceipt.payment_date >= date_from)
        if date_to:
            q_rcp = q_rcp.filter(BillingReceipt.payment_date <= date_to)
        receipts = q_rcp.order_by(BillingReceipt.created_at.desc()).all()

        entries = build_ledger_entries(
            self.db,
            self.repo.hospital_id,
            patient_id,
            charges=all_charges,
            payments=payments,
            deposits=deposits,
            refunds=refunds,
        )

        # Pagination if requested
        if limit is not None and limit > 0:
            paginated_entries = entries[offset : offset + limit]
            paginated_charges = charges[offset : offset + limit]
        else:
            paginated_entries = entries
            paginated_charges = charges

        return PatientLedgerResponse(
            patient_id=patient_id,
            patient_name=patient.name,
            patient_uhid=patient.uhid,
            total_charges=totals["total_charges"],
            total_paid=totals["total_paid"],
            total_deposits_available=totals.get("total_deposits_available", 0.0),
            outstanding=totals["outstanding"],
            net_patient_balance=totals.get("net_patient_balance", 0.0),
            charge_count=totals["charge_count"],
            payment_count=totals["payment_count"],
            charges=[BillingChargeResponse.model_validate(charge_to_dict(c, patient)) for c in paginated_charges],
            payments=_payment_response_dtos(self.db, payments, patient, receipts=receipts),
            deposits=[BillingDepositResponse.model_validate(deposit_to_dict(d, patient)) for d in deposits],
            refunds=[BillingRefundResponse.model_validate(refund_to_dict(r, patient)) for r in refunds],
            invoices=[BillingInvoiceResponse.model_validate(invoice_to_dict(inv, patient)) for inv in invoices],
            receipts=[BillingReceiptResponse.model_validate(receipt_to_dict(r, patient)) for r in receipts],
            entries=[LedgerEntry.model_validate(e) for e in paginated_entries],
        )


class GetPatientStatementAction:
    """Produces canonical, itemized consolidated billing statement for insurance and patient journey."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(
        self,
        patient_id: UUID,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
        source_type: BillingSourceType | None = None,
        account_id: UUID | None = None,
    ) -> PatientBillingStatementResponse:
        from modules.tenancy.entities.hospital import Hospital

        patient = _get_patient_or_404(self.repo, patient_id)
        hospital = self.db.query(Hospital).filter(Hospital.id == self.repo.hospital_id).first()
        if not hospital:
            raise HTTPException(status_code=404, detail="Hospital not found")

        # Hospital metadata (strictly authoritative from actual Hospital entity fields: name, address, phone, email, gstin)
        real_gstin = hospital.gstin or None
        hosp_info = StatementHospitalInfo(
            hospital_id=hospital.id,
            name=hospital.name,
            hospital_code=hospital.hospital_id or f"HOSP-{str(hospital.id)[:6].upper()}",
            address=hospital.address or None,
            phone=hospital.phone or None,
            email=hospital.email or None,
            gstin=real_gstin,
            tax_identifier=real_gstin,
        )

        # Patient metadata (UHID canonical identifier; MRN omitted to prevent fabrication)
        pat_info = StatementPatientInfo(
            patient_id=patient.id,
            name=patient.name,
            uhid=patient.uhid,
            gender=patient.gender,
            age=patient.age,
            date_of_birth=patient.date_of_birth,
            mobile=patient.mobile,
            emergency_contact=patient.emergency_contact,
            address=patient.address,
        )

        # Charges query (chronological, with account eager loading)
        q_chg = (
            self.db.query(BillingCharge)
            .options(joinedload(BillingCharge.account))
            .filter(
                BillingCharge.hospital_id == self.repo.hospital_id,
                BillingCharge.patient_id == patient_id,
            )
        )
        if date_from:
            q_chg = q_chg.filter(BillingCharge.created_at >= datetime.combine(date_from, time.min))
        if date_to:
            q_chg = q_chg.filter(BillingCharge.created_at <= datetime.combine(date_to, time.max))
        if source_type:
            q_chg = q_chg.filter(BillingCharge.source_type == source_type)
        if account_id:
            q_chg = q_chg.filter(BillingCharge.account_id == account_id)
        all_charges = q_chg.order_by(BillingCharge.created_at.asc()).all()

        non_cancelled_charges = [c for c in all_charges if c.status != BillingChargeStatus.cancelled]
        is_scoped = bool(date_from or date_to or source_type or account_id)

        scoped_charge_ids = [c.id for c in non_cancelled_charges]
        pay_applied_map: dict[UUID, float] = {}
        dep_applied_map: dict[UUID, float] = {}
        refund_applied_map: dict[UUID, float] = {}

        if is_scoped:
            # Scoped statement: determine tender and settlements through explicit allocations
            # Service charges define the scope. Payments, deposits, refunds, and invoices linked
            # to these scoped charges are included even if settlement occurred outside the date filter.
            scoped_allocs: list[BillingPaymentAllocation] = []
            if scoped_charge_ids:
                scoped_allocs = (
                    self.db.query(BillingPaymentAllocation)
                    .filter(
                        BillingPaymentAllocation.hospital_id == self.repo.hospital_id,
                        BillingPaymentAllocation.charge_id.in_(scoped_charge_ids),
                    )
                    .all()
                )

            for a in scoped_allocs:
                if a.payment_id:
                    pay_applied_map[a.payment_id] = round(
                        pay_applied_map.get(a.payment_id, 0.0) + float(a.allocated_amount or 0.0), 2
                    )
                if a.deposit_id:
                    dep_applied_map[a.deposit_id] = round(
                        dep_applied_map.get(a.deposit_id, 0.0) + float(a.allocated_amount or 0.0), 2
                    )
                if a.refund_id and float(a.allocated_amount or 0.0) < 0:
                    refund_applied_map[a.refund_id] = round(
                        refund_applied_map.get(a.refund_id, 0.0) + abs(float(a.allocated_amount or 0.0)), 2
                    )

            # Payments: only those that applied money toward scoped charges
            scoped_payment_ids = [pid for pid, amt in pay_applied_map.items() if amt > 0.009]
            if scoped_payment_ids:
                scoped_payments = (
                    self.db.query(BillingPayment)
                    .options(joinedload(BillingPayment.account))
                    .filter(
                        BillingPayment.hospital_id == self.repo.hospital_id,
                        BillingPayment.id.in_(scoped_payment_ids),
                    )
                    .order_by(BillingPayment.payment_date.asc(), BillingPayment.created_at.asc())
                    .all()
                )
            else:
                scoped_payments = []
            total_paid = round(sum(pay_applied_map.values()), 2)

            # Receipts: only those linked to scoped payments
            if scoped_payment_ids:
                scoped_receipts = (
                    self.db.query(BillingReceipt)
                    .filter(
                        BillingReceipt.hospital_id == self.repo.hospital_id,
                        BillingReceipt.payment_id.in_(scoped_payment_ids),
                        BillingReceipt.status != BillingReceiptStatus.cancelled,
                    )
                    .order_by(BillingReceipt.payment_date.asc(), BillingReceipt.created_at.asc())
                    .all()
                )
            else:
                scoped_receipts = []
            rcpt_by_pay = {r.payment_id: r for r in scoped_receipts if r.payment_id}

            # Deposits: only those drawn down toward scoped charges
            scoped_deposit_ids = [did for did, amt in dep_applied_map.items() if amt > 0.009]
            if scoped_deposit_ids:
                scoped_deposits = (
                    self.db.query(BillingDeposit)
                    .filter(
                        BillingDeposit.hospital_id == self.repo.hospital_id,
                        BillingDeposit.id.in_(scoped_deposit_ids),
                    )
                    .order_by(BillingDeposit.deposit_date.asc(), BillingDeposit.created_at.asc())
                    .all()
                )
            else:
                scoped_deposits = []
            deposits_applied = round(sum(dep_applied_map.values()), 2)
            deposits_received = deposits_applied
            deposits_available = 0.0

            # Refunds: only those connected to reversals of allocations on scoped charges
            scoped_refund_ids = list(refund_applied_map.keys())
            if scoped_refund_ids:
                scoped_refunds = (
                    self.db.query(BillingRefund)
                    .filter(
                        BillingRefund.hospital_id == self.repo.hospital_id,
                        BillingRefund.id.in_(scoped_refund_ids),
                    )
                    .order_by(BillingRefund.refund_date.asc(), BillingRefund.created_at.asc())
                    .all()
                )
            else:
                scoped_refunds = []
            total_refunds = round(sum(refund_applied_map.values()), 2)

            # Invoices: only invoices itemizing scoped charges
            scoped_inv_lines = (
                self.db.query(BillingInvoiceLine.invoice_id)
                .filter(BillingInvoiceLine.charge_id.in_(scoped_charge_ids))
                .all()
            ) if scoped_charge_ids else []
            scoped_inv_ids = {row[0] for row in scoped_inv_lines}
            if scoped_inv_ids:
                scoped_invoices = (
                    self.db.query(BillingInvoice)
                    .options(selectinload(BillingInvoice.lines))
                    .filter(
                        BillingInvoice.hospital_id == self.repo.hospital_id,
                        BillingInvoice.id.in_(scoped_inv_ids),
                        BillingInvoice.status != BillingInvoiceStatus.cancelled,
                    )
                    .order_by(BillingInvoice.invoice_date.asc(), BillingInvoice.created_at.asc())
                    .all()
                )
            else:
                scoped_invoices = []

            gross_charges = round(sum(float(c.charge_amount or 0.0) for c in non_cancelled_charges), 2)
            discount_total = round(sum(float(c.discount_amount or 0.0) for c in non_cancelled_charges), 2)
            tax_total = round(sum(float(c.tax_amount or 0.0) for c in non_cancelled_charges), 2)
            net_charges = round(sum(float(c.net_amount or 0.0) for c in non_cancelled_charges), 2)
            outstanding = round(
                sum(max(0.0, float(c.net_amount or 0.0) - float(c.amount_paid or 0.0)) for c in non_cancelled_charges), 2
            )
            net_patient_balance = outstanding
        else:
            # Entire journey: canonical reconciliation with patient_ledger_totals
            payments = (
                self.db.query(BillingPayment)
                .options(joinedload(BillingPayment.account))
                .filter(
                    BillingPayment.hospital_id == self.repo.hospital_id,
                    BillingPayment.patient_id == patient_id,
                )
                .order_by(BillingPayment.payment_date.asc(), BillingPayment.created_at.asc())
                .all()
            )
            scoped_receipts = (
                self.db.query(BillingReceipt)
                .filter(
                    BillingReceipt.hospital_id == self.repo.hospital_id,
                    BillingReceipt.patient_id == patient_id,
                    BillingReceipt.status != BillingReceiptStatus.cancelled,
                )
                .order_by(BillingReceipt.payment_date.asc(), BillingReceipt.created_at.asc())
                .all()
            )
            rcpt_by_pay = {r.payment_id: r for r in scoped_receipts if r.payment_id}

            scoped_invoices = (
                self.db.query(BillingInvoice)
                .options(selectinload(BillingInvoice.lines))
                .filter(
                    BillingInvoice.hospital_id == self.repo.hospital_id,
                    BillingInvoice.patient_id == patient_id,
                    BillingInvoice.status != BillingInvoiceStatus.cancelled,
                )
                .order_by(BillingInvoice.invoice_date.asc(), BillingInvoice.created_at.asc())
                .all()
            )

            deposits = (
                self.db.query(BillingDeposit)
                .filter(
                    BillingDeposit.hospital_id == self.repo.hospital_id,
                    BillingDeposit.patient_id == patient_id,
                )
                .order_by(BillingDeposit.deposit_date.asc(), BillingDeposit.created_at.asc())
                .all()
            )

            refunds = (
                self.db.query(BillingRefund)
                .filter(
                    BillingRefund.hospital_id == self.repo.hospital_id,
                    BillingRefund.patient_id == patient_id,
                )
                .order_by(BillingRefund.refund_date.asc(), BillingRefund.created_at.asc())
                .all()
            )

            active_deposits = [
                d for d in deposits
                if d.status in (DepositStatus.available, DepositStatus.partially_allocated)
            ]
            totals = patient_ledger_totals(
                self.db,
                self.repo.hospital_id,
                patient_id,
                charges=non_cancelled_charges,
                payments=payments,
                deposits=active_deposits,
                account_id=account_id,
            )

            scoped_payments = payments
            scoped_deposits = deposits
            scoped_refunds = refunds

            gross_charges = round(sum(float(c.charge_amount or 0.0) for c in non_cancelled_charges), 2)
            discount_total = round(sum(float(c.discount_amount or 0.0) for c in non_cancelled_charges), 2)
            tax_total = round(sum(float(c.tax_amount or 0.0) for c in non_cancelled_charges), 2)
            net_charges = totals["total_charges"]
            total_paid = totals["total_paid"]
            deposits_received = round(sum(float(d.original_amount or 0.0) for d in deposits), 2)
            deposits_applied = round(sum(float(d.original_amount or 0.0) - float(d.available_amount or 0.0) for d in deposits), 2)
            deposits_available = totals["total_deposits_available"]
            total_refunds = round(sum(float(r.amount or 0.0) for r in refunds), 2)
            outstanding = totals["outstanding"]
            net_patient_balance = totals["net_patient_balance"]

        # Scope
        scope_str = "date_range" if (date_from or date_to) else ("episode" if account_id else "entire_journey")
        scope_info = StatementScopeInfo(
            scope=scope_str,
            date_from=date_from,
            date_to=date_to,
            source_filter=source_type.value if source_type else None,
            total_episodes=len(set(c.account_id for c in all_charges if c.account_id)) or 1,
        )

        # Build itemized charges list
        itemized_charges: list[StatementItemizedCharge] = []
        for c in all_charges:
            out_item = max(0.0, float(c.net_amount or 0.0) - float(c.amount_paid or 0.0))
            st_val = c.status.value if hasattr(c.status, "value") else str(c.status)
            src_val = c.source_type.value if hasattr(c.source_type, "value") else str(c.source_type)
            acct_no = c.account.account_number if getattr(c, "account", None) else None
            itemized_charges.append(
                StatementItemizedCharge(
                    id=c.id,
                    charge_date=c.created_at,
                    source_type=src_val,
                    source_id=c.source_id,
                    description=c.description,
                    quantity=float(c.quantity or 1.0),
                    unit_price=float(c.unit_price or 0.0),
                    gross_amount=float(c.charge_amount or 0.0),
                    discount_amount=float(c.discount_amount or 0.0),
                    tax_amount=float(c.tax_amount or 0.0),
                    net_amount=float(c.net_amount or 0.0),
                    amount_paid=float(c.amount_paid or 0.0),
                    outstanding=round(out_item, 2),
                    status=st_val,
                    reference_number=str(c.source_id)[:8] if c.source_id else None,
                    account_id=c.account_id,
                    account_number=acct_no,
                )
            )

        # Build payments list with linked receipts
        stmt_payments: list[StatementPayment] = []
        for p in scoped_payments:
            rcpt = rcpt_by_pay.get(p.id)
            meth_val = p.payment_method.value if hasattr(p.payment_method, "value") else str(p.payment_method)
            acct_no = p.account.account_number if getattr(p, "account", None) else None
            applied_amt = pay_applied_map.get(p.id, float(p.amount)) if is_scoped else float(p.amount)
            stmt_payments.append(
                StatementPayment(
                    id=p.id,
                    payment_date=p.payment_date,
                    amount=applied_amt,
                    payment_method=meth_val,
                    reference_number=p.reference_number,
                    receipt_number=rcpt.receipt_number if rcpt else None,
                    receipt_id=rcpt.id if rcpt else None,
                    received_by_name=p.received_by_name or "",
                    account_number=acct_no,
                )
            )

        # Build deposits list
        stmt_deposits: list[StatementDeposit] = []
        for d in scoped_deposits:
            meth_val = d.payment_method.value if hasattr(d.payment_method, "value") else str(d.payment_method)
            st_val = d.status.value if hasattr(d.status, "value") else str(d.status)
            used = dep_applied_map.get(d.id, round(float(d.original_amount) - float(d.available_amount), 2)) if is_scoped else round(float(d.original_amount) - float(d.available_amount), 2)
            orig = float(d.original_amount)
            avail = 0.0 if is_scoped else float(d.available_amount)
            stmt_deposits.append(
                StatementDeposit(
                    id=d.id,
                    deposit_number=d.deposit_number,
                    deposit_date=d.deposit_date,
                    deposit_type=d.deposit_type,
                    payment_method=meth_val,
                    original_amount=orig,
                    available_amount=avail,
                    used_amount=used,
                    status=st_val,
                    reference_number=d.reference_number,
                )
            )

        # Build refunds list
        stmt_refunds: list[StatementRefund] = []
        for r in scoped_refunds:
            meth_val = r.refund_method.value if hasattr(r.refund_method, "value") else str(r.refund_method)
            st_val = r.status.value if hasattr(r.status, "value") else str(r.status)
            amt = refund_applied_map.get(r.id, float(r.amount)) if is_scoped else float(r.amount)
            stmt_refunds.append(
                StatementRefund(
                    id=r.id,
                    refund_number=r.refund_number,
                    refund_date=r.refund_date,
                    refund_method=meth_val,
                    amount=amt,
                    reason=r.reason,
                    status=st_val,
                    reference_number=r.reference_number if hasattr(r, "reference_number") else None,
                )
            )

        summary = StatementFinancialSummary(
            gross_charges=gross_charges,
            discount_total=discount_total,
            tax_total=tax_total,
            net_charges=net_charges,
            total_paid=total_paid,
            deposits_received=deposits_received,
            deposits_applied=deposits_applied,
            deposits_available=deposits_available,
            total_refunds=total_refunds,
            outstanding=outstanding,
            net_patient_balance=net_patient_balance,
        )

        stmt_id = f"STMT-{patient.uhid}-{date.today().strftime('%Y%m%d')}"

        return PatientBillingStatementResponse(
            statement_id=stmt_id,
            generated_at=datetime.now(timezone.utc),
            hospital=hosp_info,
            patient=pat_info,
            scope=scope_info,
            charges=itemized_charges,
            payments=stmt_payments,
            receipts=[BillingReceiptResponse.model_validate(receipt_to_dict(r, patient)) for r in scoped_receipts],
            invoices=[BillingInvoiceResponse.model_validate(invoice_to_dict(inv, patient)) for inv in scoped_invoices],
            deposits=stmt_deposits,
            refunds=stmt_refunds,
            summary=summary,
        )


def _esc_stmt(val: Any) -> str:
    import html as html_lib
    return html_lib.escape(str(val)) if val is not None else ""


def statement_html(statement: PatientBillingStatementResponse, auto_print: bool = True) -> str:
    h = statement.hospital
    p = statement.patient
    s = statement.summary
    scope = statement.scope

    charges_rows = []
    for c in statement.charges:
        dt_str = c.charge_date.strftime("%d/%m/%Y") if c.charge_date else "—"
        ref_text = f"<br/><span style='font-size:10px;color:#666;'>Ref: {_esc_stmt(c.reference_number)}</span>" if c.reference_number else ""
        disc_text = f"₹{c.discount_amount:,.2f}" if c.discount_amount > 0 else "—"
        tax_text = f"₹{c.tax_amount:,.2f}" if c.tax_amount > 0 else "—"
        charges_rows.append(
            f"<tr>"
            f"<td style='white-space:nowrap;font-family:monospace;'>{dt_str}</td>"
            f"<td style='text-transform:capitalize;'>{c.source_type.replace('_', ' ')}</td>"
            f"<td><strong>{_esc_stmt(c.description)}</strong>{ref_text}</td>"
            f"<td style='text-align:right;'>{c.quantity:g}</td>"
            f"<td style='text-align:right;'>₹{c.unit_price:,.2f}</td>"
            f"<td style='text-align:right;'>{disc_text}</td>"
            f"<td style='text-align:right;'>{tax_text}</td>"
            f"<td style='text-align:right;font-weight:bold;'>₹{c.net_amount:,.2f}</td>"
            f"<td style='text-align:center;text-transform:uppercase;font-size:10px;'>{c.status}</td>"
            f"</tr>"
        )
    charges_tbody = "".join(charges_rows) or "<tr><td colspan='9' style='text-align:center;padding:15px;'>No charges recorded</td></tr>"

    invoices_rows = []
    for inv in statement.invoices:
        dt_str = inv.invoice_date.strftime("%d/%m/%Y") if inv.invoice_date else "—"
        disc_text = f"₹{inv.discount_amount:,.2f}" if inv.discount_amount > 0 else "—"
        tax_text = f"₹{inv.tax_amount:,.2f}" if inv.tax_amount > 0 else "—"
        invoices_rows.append(
            f"<tr>"
            f"<td style='font-family:monospace;font-weight:bold;'>{inv.invoice_number}</td>"
            f"<td>{dt_str}</td>"
            f"<td style='text-align:right;'>₹{inv.subtotal:,.2f}</td>"
            f"<td style='text-align:right;'>{disc_text}</td>"
            f"<td style='text-align:right;'>{tax_text}</td>"
            f"<td style='text-align:right;font-weight:bold;'>₹{inv.grand_total:,.2f}</td>"
            f"<td style='text-align:center;text-transform:uppercase;font-size:10px;'>{inv.status}</td>"
            f"</tr>"
        )
    invoices_tbody = "".join(invoices_rows) or "<tr><td colspan='7' style='text-align:center;padding:15px;'>No invoices issued</td></tr>"

    payments_rows = []
    for pay in statement.payments:
        dt_str = pay.payment_date.strftime("%d/%m/%Y") if pay.payment_date else "—"
        payments_rows.append(
            f"<tr>"
            f"<td style='font-family:monospace;'>{dt_str}</td>"
            f"<td style='font-family:monospace;font-weight:bold;'>{pay.receipt_number or '—'}</td>"
            f"<td style='text-transform:capitalize;'>{pay.payment_method.replace('_', ' ')}</td>"
            f"<td>{pay.reference_number or '—'}</td>"
            f"<td>{_esc_stmt(pay.received_by_name or 'Cashier')}</td>"
            f"<td style='text-align:right;font-weight:bold;color:#166534;'>₹{pay.amount:,.2f}</td>"
            f"</tr>"
        )
    payments_tbody = "".join(payments_rows) or "<tr><td colspan='6' style='text-align:center;padding:15px;'>No payments recorded</td></tr>"

    deposits_section = ""
    if statement.deposits:
        dep_rows = []
        for d in statement.deposits:
            dt_str = d.deposit_date.strftime("%d/%m/%Y") if d.deposit_date else "—"
            dep_rows.append(
                f"<tr>"
                f"<td style='font-family:monospace;'>{dt_str}</td>"
                f"<td style='font-family:monospace;font-weight:bold;'>{d.deposit_number}</td>"
                f"<td style='text-transform:capitalize;'>{d.deposit_type} · {d.payment_method}</td>"
                f"<td style='text-align:right;'>₹{d.original_amount:,.2f}</td>"
                f"<td style='text-align:right;'>₹{d.used_amount:,.2f}</td>"
                f"<td style='text-align:right;font-weight:bold;color:#166534;'>₹{d.available_amount:,.2f}</td>"
                f"<td style='text-align:center;text-transform:uppercase;font-size:10px;'>{d.status}</td>"
                f"</tr>"
            )
        deposits_section = f"""
        <div style="margin-top:20px;">
          <h3 style="font-size:12px;text-transform:uppercase;margin:0 0 8px 0;letter-spacing:0.5px;">4. Advance Deposits &amp; Drawdowns</h3>
          <table class="data-table">
            <thead>
              <tr>
                <th>Date</th><th>Deposit No</th><th>Type / Method</th>
                <th style="text-align:right;">Original</th><th style="text-align:right;">Consumed</th>
                <th style="text-align:right;">Available</th><th>Status</th>
              </tr>
            </thead>
            <tbody>{"".join(dep_rows)}</tbody>
          </table>
        </div>
        """

    refunds_section = ""
    if statement.refunds:
        ref_rows = []
        for r in statement.refunds:
            dt_str = r.refund_date.strftime("%d/%m/%Y") if r.refund_date else "—"
            ref_rows.append(
                f"<tr>"
                f"<td style='font-family:monospace;'>{dt_str}</td>"
                f"<td style='font-family:monospace;font-weight:bold;'>{r.refund_number}</td>"
                f"<td style='text-transform:capitalize;'>{r.refund_method}</td>"
                f"<td>{_esc_stmt(r.reason)}</td>"
                f"<td style='text-align:center;text-transform:uppercase;font-size:10px;'>{r.status}</td>"
                f"<td style='text-align:right;font-weight:bold;color:#991b1b;'>₹{r.amount:,.2f}</td>"
                f"</tr>"
            )
        refunds_section = f"""
        <div style="margin-top:20px;">
          <h3 style="font-size:12px;text-transform:uppercase;margin:0 0 8px 0;letter-spacing:0.5px;">5. Refunds Processed</h3>
          <table class="data-table">
            <thead>
              <tr>
                <th>Date</th><th>Refund No</th><th>Method</th><th>Reason</th><th>Status</th>
                <th style="text-align:right;">Amount Refunded</th>
              </tr>
            </thead>
            <tbody>{"".join(ref_rows)}</tbody>
          </table>
        </div>
        """

    gen_time_str = statement.generated_at.strftime("%d/%m/%Y %I:%M %p") if statement.generated_at else "—"
    dob_str = p.date_of_birth.strftime("%d/%m/%Y") if p.date_of_birth else "—"
    scope_str = scope.scope.replace("_", " ").title()
    if scope.date_from:
        d_from = scope.date_from.strftime("%d/%m/%Y")
        d_to = scope.date_to.strftime("%d/%m/%Y") if scope.date_to else "Present"
        scope_str += f" ({d_from} to {d_to})"

    auto_script = "<script>window.onload = function() { window.print(); };</script>" if auto_print else ""

    meta_parts = []
    if h.phone:
        meta_parts.append(f"Tel: {_esc_stmt(h.phone)}")
    if h.email:
        meta_parts.append(f"Email: {_esc_stmt(h.email)}")
    gstin_val = h.gstin or h.tax_identifier
    if gstin_val:
        meta_parts.append(f"GSTIN: {_esc_stmt(gstin_val)}")
    hosp_meta_line = " | ".join(meta_parts)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Billing Statement - {p.uhid} - {statement.statement_id}</title>
  <style>
    @page {{ size: A4 portrait; margin: 12mm; }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      color: #111; margin: 0; padding: 12px; font-size: 11px; line-height: 1.4; background: #fff;
    }}
    .header-table {{ width: 100%; border-bottom: 2px solid #0f172a; padding-bottom: 10px; margin-bottom: 12px; }}
    .header-table td {{ vertical-align: top; }}
    .hosp-title {{ font-size: 18px; font-weight: 800; text-transform: uppercase; margin: 0; color: #0f172a; }}
    .badge {{
      display: inline-block; padding: 3px 8px; border: 1px solid #0f172a;
      font-weight: 700; font-size: 10px; text-transform: uppercase; border-radius: 3px;
    }}
    .patient-box {{
      width: 100%; border: 1px solid #cbd5e1; border-radius: 4px; padding: 10px;
      margin-bottom: 14px; background: #f8fafc;
    }}
    .patient-table {{ width: 100%; border-collapse: collapse; }}
    .patient-table td {{ padding: 3px 6px; font-size: 11px; }}
    .patient-table .lbl {{ color: #64748b; font-weight: 600; text-transform: uppercase; font-size: 9px; }}
    .data-table {{ width: 100%; border-collapse: collapse; margin-bottom: 14px; font-size: 10px; }}
    .data-table th {{
      background: #f1f5f9; border-top: 1px solid #cbd5e1; border-bottom: 1px solid #cbd5e1;
      padding: 5px 6px; text-align: left; font-weight: 700; text-transform: uppercase; font-size: 9px;
    }}
    .data-table td {{ padding: 5px 6px; border-bottom: 1px solid #e2e8f0; }}
    .summary-box {{
      width: 100%; border: 1px solid #cbd5e1; border-radius: 4px; padding: 12px; margin-top: 16px;
      page-break-inside: avoid;
    }}
    .summary-table {{ width: 100%; border-collapse: collapse; }}
    .summary-table td {{ padding: 4px 8px; font-size: 11px; }}
    .summary-total {{ font-weight: 800; font-size: 12px; border-top: 2px solid #0f172a; border-bottom: 2px solid #0f172a; }}
    .cert-box {{
      margin-top: 24px; padding-top: 12px; border-top: 1px solid #e2e8f0; font-size: 9px; color: #64748b;
      page-break-inside: avoid;
    }}
    @media print {{
      body {{ padding: 0; }}
      .no-print {{ display: none !important; }}
    }}
  </style>
  {auto_script}
</head>
<body>
  <div class="no-print" style="margin-bottom:12px;text-align:right;">
    <button onclick="window.print()" style="padding:6px 14px;background:#0284c7;color:#fff;border:none;border-radius:4px;font-weight:600;cursor:pointer;">Print Document</button>
  </div>

  <table class="header-table">
    <tr>
      <td style="width:60%;">
        <h1 class="hosp-title">{_esc_stmt(h.name)}</h1>
        <div style="font-size:10px;color:#475569;margin-top:2px;">{_esc_stmt(h.address or '')}</div>
        <div style="font-size:10px;color:#475569;">
          {hosp_meta_line}
        </div>
      </td>
      <td style="text-align:right;width:40%;">
        <span class="badge">Consolidated Billing Statement</span>
        <div style="font-family:monospace;font-weight:bold;font-size:11px;margin-top:4px;">Ref: {statement.statement_id}</div>
        <div style="font-size:10px;color:#64748b;">Generated: {gen_time_str}</div>
        <div style="font-size:10px;color:#64748b;">Scope: <strong>{scope_str}</strong></div>
      </td>
    </tr>
  </table>

  <div class="patient-box">
    <table class="patient-table">
      <tr>
        <td style="width:25%;"><span class="lbl">Patient Name</span><br/><strong>{_esc_stmt(p.name)}</strong></td>
        <td style="width:25%;"><span class="lbl">UHID</span><br/><strong style="font-family:monospace;">{_esc_stmt(p.uhid)}</strong></td>
        <td style="width:25%;"><span class="lbl">Age / Gender</span><br/>{f"{p.age} Yrs" if p.age else "—"} / {_esc_stmt(p.gender or "—")}</td>
        <td style="width:25%;"><span class="lbl">Contact Phone</span><br/>{_esc_stmt(p.mobile or "—")}</td>
      </tr>
      <tr>
        <td><span class="lbl">Date of Birth</span><br/>{dob_str}</td>
        <td><span class="lbl">Emergency Contact</span><br/>{_esc_stmt(p.emergency_contact or "—")}</td>
        <td colspan="2"><span class="lbl">Residential Address</span><br/>{_esc_stmt(p.address or "—")}</td>
      </tr>
    </table>
  </div>

  <h3 style="font-size:12px;text-transform:uppercase;margin:0 0 6px 0;letter-spacing:0.5px;">1. Itemized Hospital Services &amp; Charges ({len(statement.charges)})</h3>
  <table class="data-table">
    <thead>
      <tr>
        <th>Date</th><th>Department</th><th>Service Description</th>
        <th style="text-align:right;">Qty</th><th style="text-align:right;">Rate</th>
        <th style="text-align:right;">Discount</th><th style="text-align:right;">Tax</th>
        <th style="text-align:right;">Net Amount</th><th>Status</th>
      </tr>
    </thead>
    <tbody>{charges_tbody}</tbody>
  </table>

  <h3 style="font-size:12px;text-transform:uppercase;margin:16px 0 6px 0;letter-spacing:0.5px;">2. Invoices Issued ({len(statement.invoices)})</h3>
  <table class="data-table">
    <thead>
      <tr>
        <th>Invoice Number</th><th>Invoice Date</th>
        <th style="text-align:right;">Subtotal</th><th style="text-align:right;">Discount</th>
        <th style="text-align:right;">Tax</th><th style="text-align:right;">Grand Total</th>
        <th>Status</th>
      </tr>
    </thead>
    <tbody>{invoices_tbody}</tbody>
  </table>

  <h3 style="font-size:12px;text-transform:uppercase;margin:16px 0 6px 0;letter-spacing:0.5px;">3. Payments Received &amp; Receipts Issued ({len(statement.payments)})</h3>
  <table class="data-table">
    <thead>
      <tr>
        <th>Date</th><th>Receipt No</th><th>Payment Method</th><th>Reference No</th><th>Received By</th>
        <th style="text-align:right;">Amount Settled</th>
      </tr>
    </thead>
    <tbody>{payments_tbody}</tbody>
  </table>

  {deposits_section}
  {refunds_section}

  <div class="summary-box">
    <div style="font-size:11px;font-weight:700;text-transform:uppercase;margin-bottom:8px;border-bottom:1px solid #cbd5e1;padding-bottom:4px;">
      Financial Summary &amp; Ledger Reconciliation (Canonical Accounting)
    </div>
    <table class="summary-table">
      <tr>
        <td style="width:25%;color:#64748b;">Gross Billed Charges:</td>
        <td style="width:25%;font-family:monospace;text-align:right;">₹{s.gross_charges:,.2f}</td>
        <td style="width:25%;color:#64748b;">Total Payments Settled:</td>
        <td style="width:25%;font-family:monospace;font-weight:bold;text-align:right;color:#166534;">₹{s.total_paid:,.2f}</td>
      </tr>
      <tr>
        <td style="color:#64748b;">Less: Discounts:</td>
        <td style="font-family:monospace;text-align:right;">- ₹{s.discount_total:,.2f}</td>
        <td style="color:#64748b;">Advance Deposits Available:</td>
        <td style="font-family:monospace;text-align:right;">₹{s.deposits_available:,.2f}</td>
      </tr>
      <tr>
        <td style="color:#64748b;">Add: Applicable Taxes:</td>
        <td style="font-family:monospace;text-align:right;">+ ₹{s.tax_total:,.2f}</td>
        <td style="color:#64748b;">Total Refunds Processed:</td>
        <td style="font-family:monospace;text-align:right;">₹{s.total_refunds:,.2f}</td>
      </tr>
      <tr class="summary-total">
        <td>Net Canonical Charges:</td>
        <td style="font-family:monospace;text-align:right;">₹{s.net_charges:,.2f}</td>
        <td style="color:#991b1b;">Outstanding Balance:</td>
        <td style="font-family:monospace;text-align:right;color:#991b1b;">₹{s.outstanding:,.2f}</td>
      </tr>
      <tr>
        <td colspan="2"></td>
        <td style="font-weight:700;padding-top:6px;">Net Patient Balance:</td>
        <td style="font-family:monospace;font-weight:700;text-align:right;padding-top:6px;">₹{s.net_patient_balance:,.2f}</td>
      </tr>
    </table>
  </div>

  <div class="cert-box">
    <table style="width:100%;">
      <tr>
        <td style="width:65%;vertical-align:bottom;">
          <strong>Statement Certification</strong><br/>
          This is an official consolidated billing statement generated from the hospital financial ledger.
          Original invoice numbers, receipt numbers, and transaction records are preserved as canonical audit references
          for insurance reimbursement and clinical accounting.
        </td>
        <td style="width:35%;text-align:center;vertical-align:bottom;">
          <div style="border-top:1px dotted #000;padding-top:4px;margin-top:25px;">
            <strong>Authorized Signatory</strong><br/>
            Billing &amp; Financial Services
          </div>
        </td>
      </tr>
    </table>
  </div>
</body>
</html>"""


class PrintPatientStatementAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    def execute(
        self,
        patient_id: UUID,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
        source_type: BillingSourceType | None = None,
        account_id: UUID | None = None,
        auto_print: bool = True,
    ) -> str:
        statement = GetPatientStatementAction(self.db, self.hospital_id).execute(
            patient_id=patient_id,
            date_from=date_from,
            date_to=date_to,
            source_type=source_type,
            account_id=account_id,
        )
        return statement_html(statement, auto_print=auto_print)


class GetPatientSummaryAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, patient_id: UUID) -> PatientFinancialSummary:
        _get_patient_or_404(self.repo, patient_id)
        all_charges = (
            self.db.query(BillingCharge)
            .filter(
                BillingCharge.hospital_id == self.repo.hospital_id,
                BillingCharge.patient_id == patient_id,
            )
            .all()
        )
        non_cancelled_charges = [c for c in all_charges if c.status != BillingChargeStatus.cancelled]
        payments = (
            self.db.query(BillingPayment)
            .filter(
                BillingPayment.hospital_id == self.repo.hospital_id,
                BillingPayment.patient_id == patient_id,
            )
            .all()
        )
        deposits = (
            self.db.query(BillingDeposit)
            .filter(
                BillingDeposit.hospital_id == self.repo.hospital_id,
                BillingDeposit.patient_id == patient_id,
            )
            .all()
        )
        refunds = (
            self.db.query(BillingRefund)
            .filter(
                BillingRefund.hospital_id == self.repo.hospital_id,
                BillingRefund.patient_id == patient_id,
            )
            .all()
        )
        active_deposits = [
            d for d in deposits
            if d.status in (DepositStatus.available, DepositStatus.partially_allocated)
        ]
        totals = patient_ledger_totals(
            self.db,
            self.repo.hospital_id,
            patient_id,
            charges=non_cancelled_charges,
            payments=payments,
            deposits=active_deposits,
        )
        entries = build_ledger_entries(
            self.db,
            self.repo.hospital_id,
            patient_id,
            charges=all_charges,
            payments=payments,
            deposits=deposits,
            refunds=refunds,
        )[:10]
        return PatientFinancialSummary(
            patient_id=patient_id,
            total_charges=totals["total_charges"],
            total_paid=totals["total_paid"],
            outstanding=totals["outstanding"],
            recent_entries=[LedgerEntry.model_validate(e) for e in entries],
        )


class GetPatientDuesAction:
    """Lightweight dues query for the Record Payment modal.
    
    Returns only open (pending/partially_paid) charges, outstanding balance,
    available advance deposits, and active invoices containing those charges.
    Eliminates fetching historical receipts, payments, and timeline entries.
    """
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, patient_id: UUID) -> PatientDuesResponse:
        patient = _get_patient_or_404(self.repo, patient_id)
        open_charges = (
            self.db.query(BillingCharge)
            .filter(
                BillingCharge.hospital_id == self.repo.hospital_id,
                BillingCharge.patient_id == patient_id,
                BillingCharge.status.in_([BillingChargeStatus.pending, BillingChargeStatus.partially_paid]),
            )
            .order_by(BillingCharge.created_at.asc())
            .all()
        )
        outstanding = round(
            sum(max(0.0, float(c.net_amount or 0) - float(c.amount_paid or 0)) for c in open_charges),
            2,
        )
        total_deposits_avail = float(
            self.db.query(func.coalesce(func.sum(BillingDeposit.available_amount), 0.0))
            .filter(
                BillingDeposit.hospital_id == self.repo.hospital_id,
                BillingDeposit.patient_id == patient_id,
                BillingDeposit.status.in_([DepositStatus.available, DepositStatus.partially_allocated]),
            )
            .scalar()
            or 0.0
        )

        modal_invoices: list[BillingInvoice] = []
        open_charge_ids = [c.id for c in open_charges]
        if open_charge_ids:
            modal_invoices = (
                self.db.query(BillingInvoice)
                .options(selectinload(BillingInvoice.lines))
                .join(BillingInvoiceLine, BillingInvoiceLine.invoice_id == BillingInvoice.id)
                .filter(
                    BillingInvoice.hospital_id == self.repo.hospital_id,
                    BillingInvoice.patient_id == patient_id,
                    BillingInvoice.status != BillingInvoiceStatus.cancelled,
                    BillingInvoiceLine.charge_id.in_(open_charge_ids),
                )
                .distinct()
                .all()
            )

        return PatientDuesResponse(
            patient_id=patient_id,
            patient_name=patient.name,
            patient_uhid=patient.uhid,
            outstanding=outstanding,
            total_deposits_available=round(total_deposits_avail, 2),
            pending_charges=[BillingChargeResponse.model_validate(charge_to_dict(c, patient)) for c in open_charges],
            modal_invoices=[BillingInvoiceResponse.model_validate(invoice_to_dict(inv, patient)) for inv in modal_invoices],
        )


class GetBillingDashboardAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self) -> BillingDashboardResponse:
        hid = self.repo.hospital_id
        today_start = datetime.combine(date.today(), time.min).replace(tzinfo=timezone.utc)
        today_end = datetime.combine(date.today(), time.max).replace(tzinfo=timezone.utc)
        today = date.today()

        # Consolidate Charges, Payments, Invoices, and Receipts metrics into a single
        # set-based database round trip (collapses 4 separate round trips into 1).
        # This preserves strict tenant isolation (hid), identical financial definitions,
        # and exact date boundaries.
        is_today = (BillingCharge.created_at >= today_start) & (BillingCharge.created_at <= today_end)
        not_cancelled = BillingCharge.status != BillingChargeStatus.cancelled

        sq_charges = (
            select(
                literal(1).label("join_key"),
                func.coalesce(func.sum(case((is_today & not_cancelled, BillingCharge.net_amount), else_=0.0)), 0.0).label("todays_charges"),
                func.coalesce(func.sum(case((not_cancelled, BillingCharge.net_amount), else_=0.0)), 0.0).label("total_charges"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                BillingCharge.status.in_(
                                    [BillingChargeStatus.pending, BillingChargeStatus.partially_paid]
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("pending_count"),
                func.coalesce(
                    func.sum(
                        case((is_today & not_cancelled & (BillingCharge.source_type == BillingSourceType.ot), BillingCharge.net_amount), else_=0.0)
                    ),
                    0.0,
                ).label("todays_ot"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                is_today
                                & not_cancelled
                                & BillingCharge.source_type.in_([BillingSourceType.bed, BillingSourceType.admission]),
                                BillingCharge.net_amount,
                            ),
                            else_=0.0,
                        )
                    ),
                    0.0,
                ).label("todays_ipd"),
            )
            .where(BillingCharge.hospital_id == hid)
            .subquery()
        )

        sq_payments = (
            select(
                literal(1).label("join_key"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                (BillingPayment.created_at >= today_start) & (BillingPayment.created_at <= today_end),
                                BillingPayment.amount,
                            ),
                            else_=0.0,
                        )
                    ),
                    0.0,
                ).label("todays_collections"),
                func.coalesce(func.sum(BillingPayment.amount), 0.0).label("total_paid"),
            )
            .where(BillingPayment.hospital_id == hid)
            .subquery()
        )

        sq_invoices = (
            select(
                literal(1).label("join_key"),
                func.coalesce(
                    func.sum(case((BillingInvoice.invoice_date == today, 1), else_=0)), 0
                ).label("today_inv_count"),
                func.coalesce(
                    func.sum(
                        case(
                            (BillingInvoice.status != BillingInvoiceStatus.cancelled, BillingInvoice.grand_total),
                            else_=0.0,
                        )
                    ),
                    0.0,
                ).label("total_inv"),
            )
            .where(BillingInvoice.hospital_id == hid)
            .subquery()
        )

        sq_receipts = (
            select(
                literal(1).label("join_key"),
                func.coalesce(func.count(BillingReceipt.id), 0).label("today_rcpt_count"),
            )
            .where(BillingReceipt.hospital_id == hid, BillingReceipt.payment_date == today)
            .subquery()
        )

        comb_metrics_stmt = (
            select(
                sq_charges.c.todays_charges,
                sq_charges.c.total_charges,
                sq_charges.c.pending_count,
                sq_charges.c.todays_ot,
                sq_charges.c.todays_ipd,
                sq_payments.c.todays_collections,
                sq_payments.c.total_paid,
                sq_invoices.c.today_inv_count,
                sq_invoices.c.total_inv,
                sq_receipts.c.today_rcpt_count,
            )
            .select_from(
                sq_charges
                .join(sq_payments, sq_charges.c.join_key == sq_payments.c.join_key)
                .join(sq_invoices, sq_charges.c.join_key == sq_invoices.c.join_key)
                .join(sq_receipts, sq_charges.c.join_key == sq_receipts.c.join_key)
            )
        )

        metrics_row = self.db.execute(comb_metrics_stmt).one()
        (
            todays_charges,
            total_charges,
            pending_count,
            todays_ot,
            todays_ipd,
            todays_collections,
            total_paid,
            today_inv_count,
            total_inv,
            today_rcpt_count,
        ) = metrics_row
        outstanding_total = round(max(0.0, float(total_charges) - float(total_paid)), 2)

        # Outstanding by category
        cat_rows = (
            self.db.query(BillingCharge.source_type, func.coalesce(func.sum(BillingCharge.net_amount - BillingCharge.amount_paid), 0.0))
            .filter(
                BillingCharge.hospital_id == hid,
                BillingCharge.status.in_([BillingChargeStatus.pending, BillingChargeStatus.partially_paid]),
            )
            .group_by(BillingCharge.source_type)
            .all()
        )
        outstanding_by_cat: dict[str, float] = {
            (r[0].value if r[0] else "other"): round(float(r[1] or 0), 2)
            for r in cat_rows
        }

        recent_charges = (
            self.db.query(BillingCharge)
            .options(joinedload(BillingCharge.patient), joinedload(BillingCharge.account))
            .filter(BillingCharge.hospital_id == hid, BillingCharge.status != BillingChargeStatus.cancelled)
            .order_by(BillingCharge.created_at.desc())
            .limit(5)
            .all()
        )
        recent_payments = (
            self.db.query(BillingPayment)
            .options(
                joinedload(BillingPayment.patient),
                joinedload(BillingPayment.allocations).joinedload(BillingPaymentAllocation.charge),
            )
            .filter(BillingPayment.hospital_id == hid)
            .order_by(BillingPayment.created_at.desc())
            .limit(5)
            .all()
        )

        return BillingDashboardResponse(
            todays_charges=round(float(todays_charges), 2),
            todays_collections=round(float(todays_collections), 2),
            outstanding_total=outstanding_total,
            pending_charges_count=int(pending_count),
            todays_ot_revenue=round(float(todays_ot), 2),
            todays_ipd_revenue=round(float(todays_ipd), 2),
            outstanding_by_category=outstanding_by_cat,
            today_invoice_count=int(today_inv_count),
            today_receipt_count=int(today_rcpt_count),
            total_invoiced=round(float(total_inv), 2),
            total_collected=round(float(total_paid), 2),
            recent_charges=[BillingChargeResponse.model_validate(charge_to_dict(c)) for c in recent_charges],
            recent_payments=_payment_response_dtos(self.db, recent_payments),
        )
