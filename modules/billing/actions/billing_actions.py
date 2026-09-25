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
from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

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
    PatientFinancialSummary,
    PatientLedgerResponse,
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
)
from modules.billing.services.invoice_service import (
    create_invoice_from_charges,
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
) -> list[BillingPaymentResponse]:
    """
    Batch version of _payment_response_dto for list/dashboard responses.

    The per-payment version queries BillingReceipt once per payment (N+1 —
    up to 200 extra round trips on the payments list, 5 more on the
    dashboard). This fetches all matching receipts in one query and maps
    them back, producing identical output to calling _payment_response_dto
    in a loop.
    """
    if not payments:
        return []
    payment_ids = [p.id for p in payments]
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

        # Check if already on a non-cancelled invoice
        from modules.billing.services.invoice_service import charges_already_invoiced
        if charges_already_invoiced(self.db, self.repo.hospital_id, [charge.id]):
            # If attempting to alter financial numbers on an invoiced charge, reject
            if (
                payload.discount_amount is not None
                or payload.discount_percent is not None
                or payload.tax_amount is not None
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot modify financial amounts or discounts on an invoiced charge. Issue an adjustment or credit note.",
                )

        if payload.description is not None:
            charge.description = payload.description.strip()[:512]
        if payload.notes is not None:
            charge.notes = payload.notes

        old_disc = float(charge.discount_amount or 0)
        disc_amt = payload.discount_amount if payload.discount_amount is not None else old_disc
        disc_pct = payload.discount_percent if payload.discount_percent is not None else charge.discount_percent
        disc, net = compute_net(float(charge.charge_amount or 0), disc_amt, disc_pct)
        charge.discount_amount = disc
        charge.discount_percent = disc_pct
        if payload.discount_reason is not None:
            charge.discount_reason = payload.discount_reason.strip() or None
        charge.net_amount = net

        if payload.status is not None:
            charge.status = payload.status
        else:
            paid = float(charge.amount_paid or 0)
            if net <= 0 or paid >= net:
                charge.status = BillingChargeStatus.paid
                charge.amount_paid = net
            elif paid > 0:
                charge.status = BillingChargeStatus.partially_paid
            else:
                charge.status = BillingChargeStatus.pending

        self.db.commit()
        self.db.refresh(charge)
        audit_summary = f"Updated charge {charge.id} (Net: ₹{charge.net_amount:.2f})"
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
        )
        self.db.commit()
        return BillingChargeResponse.model_validate(charge_to_dict(charge))



class CancelChargeAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, charge_id: UUID, user: dict[str, Any]) -> BillingChargeResponse:
        charge = self.repo.get_charge_by_id(charge_id)
        if not charge:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Charge not found")
        
        paid_amount = round(float(charge.amount_paid or 0.0), 2)
        charge.status = BillingChargeStatus.cancelled
        charge.amount_paid = 0.0
        self.db.flush()

        # If money was paid towards this charge, convert into an available advance deposit
        if paid_amount > 0:
            create_deposit(
                self.db,
                hospital_id=self.repo.hospital_id,
                patient_id=charge.patient_id,
                account_id=charge.account_id,
                amount=paid_amount,
                deposit_date=date.today(),
                deposit_type="cancellation_credit",
                payment_method=BillingPaymentMethod.other,
                notes=f"Credit from cancelled charge {charge.description[:40]}",
                received_by_name=_actor(user),
            )

        self.db.commit()
        self.db.refresh(charge)
        write_audit_log(
            self.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="cancel",
            entity_type="billing_charge",
            entity_id=charge.id,
            summary=f"Cancelled charge {charge.id} ({charge.description[:32]}). Credited paid amount ₹{paid_amount:.2f} as available patient deposit.",
        )
        self.db.commit()
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
        try:
            receipt = issue_receipt_for_payment(
                self.db,
                pay,
                linked_invoice_id=payload.linked_invoice_id,
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
                    linked_invoice_id=payload.linked_invoice_id,
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
        patient = self.repo.get_patient(inv.patient_id)
        out = patient_outstanding_for_invoice(self.db, self.repo.hospital_id, inv.patient_id)
        return invoice_html(inv, hospital, patient, outstanding=out, auto_print=auto_print)


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
        patient = self.repo.get_patient(r.patient_id)
        return receipt_html(r, hospital, patient, auto_print=auto_print)


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
            )
            self.db.commit()
            self.db.refresh(refund)
        except ValueError as e:
            self.db.rollback()
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

        write_audit_log(
            self.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="refund",
            entity_type="billing_refund",
            entity_id=refund.id,
            summary=f"Refund voucher {refund.refund_number} (₹{refund.amount:.2f}) issued for {patient.name}: {refund.reason[:40]}",
        )
        self.db.commit()
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

    def execute(self, patient_id: UUID) -> PatientLedgerResponse:
        patient = _get_patient_or_404(self.repo, patient_id)
        # Fetch charges/payments once and share them across totals, the
        # response's own charges/payments fields, and build_ledger_entries —
        # this endpoint previously fetched each of those independently
        # (1 query here in the caller + 2 inside patient_ledger_totals + 2
        # inside build_ledger_entries = 5 queries over the same two tables).
        # all_charges intentionally has NO status filter (unlike the old
        # standalone query below) because build_ledger_entries needs
        # cancelled charges too, to render them in the timeline; it filters
        # them out itself. non_cancelled_charges recreates the old filtered
        # set for totals and the response's `charges` field.
        all_charges = (
            self.db.query(BillingCharge)
            .filter(
                BillingCharge.hospital_id == self.repo.hospital_id,
                BillingCharge.patient_id == patient_id,
            )
            .order_by(BillingCharge.created_at.desc())
            .all()
        )
        non_cancelled_charges = [c for c in all_charges if c.status != BillingChargeStatus.cancelled]
        payments = (
            self.db.query(BillingPayment)
            .filter(
                BillingPayment.hospital_id == self.repo.hospital_id,
                BillingPayment.patient_id == patient_id,
            )
            .order_by(BillingPayment.created_at.desc())
            .all()
        )
        totals = patient_ledger_totals(
            self.db, self.repo.hospital_id, patient_id, charges=non_cancelled_charges, payments=payments
        )
        charges = non_cancelled_charges
        invoices = (
            self.db.query(BillingInvoice)
            .options(joinedload(BillingInvoice.lines))
            .filter(
                BillingInvoice.hospital_id == self.repo.hospital_id,
                BillingInvoice.patient_id == patient_id,
                BillingInvoice.status != BillingInvoiceStatus.cancelled,
            )
            .order_by(BillingInvoice.created_at.desc())
            .all()
        )
        receipts = (
            self.db.query(BillingReceipt)
            .filter(
                BillingReceipt.hospital_id == self.repo.hospital_id,
                BillingReceipt.patient_id == patient_id,
                BillingReceipt.status != BillingReceiptStatus.cancelled,
            )
            .order_by(BillingReceipt.created_at.desc())
            .all()
        )
        entries = build_ledger_entries(
            self.db, self.repo.hospital_id, patient_id, charges=all_charges, payments=payments
        )
        return PatientLedgerResponse(
            patient_id=patient_id,
            patient_name=patient.name,
            patient_uhid=patient.uhid,
            total_charges=totals["total_charges"],
            total_paid=totals["total_paid"],
            outstanding=totals["outstanding"],
            charge_count=totals["charge_count"],
            payment_count=totals["payment_count"],
            charges=[BillingChargeResponse.model_validate(charge_to_dict(c, patient)) for c in charges],
            payments=_payment_response_dtos(self.db, payments, patient),
            invoices=[BillingInvoiceResponse.model_validate(invoice_to_dict(inv, patient)) for inv in invoices],
            receipts=[BillingReceiptResponse.model_validate(receipt_to_dict(r, patient)) for r in receipts],
            entries=[LedgerEntry.model_validate(e) for e in entries],
        )


class GetPatientSummaryAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, patient_id: UUID) -> PatientFinancialSummary:
        _get_patient_or_404(self.repo, patient_id)
        # Same sharing as GetPatientLedgerAction above: one charges fetch and
        # one payments fetch instead of 2 queries inside patient_ledger_totals
        # plus 2 more inside build_ledger_entries.
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
        totals = patient_ledger_totals(
            self.db, self.repo.hospital_id, patient_id, charges=non_cancelled_charges, payments=payments
        )
        entries = build_ledger_entries(
            self.db, self.repo.hospital_id, patient_id, charges=all_charges, payments=payments
        )[:10]
        return PatientFinancialSummary(
            patient_id=patient_id,
            total_charges=totals["total_charges"],
            total_paid=totals["total_paid"],
            outstanding=totals["outstanding"],
            recent_entries=[LedgerEntry.model_validate(e) for e in entries],
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

        # All 5 of these scan billing_charges for this hospital_id — collapsed
        # into one conditional-aggregation query (was 5 separate round trips).
        # outstanding_by_cat below stays a separate GROUP BY query: combining a
        # scalar-aggregate query with a grouped one needs a window function or
        # UNION, which isn't worth the added complexity for one extra round
        # trip that's already a single efficient query on its own (see the
        # revised optimization plan, "be careful with query consolidation").
        is_today = (BillingCharge.created_at >= today_start) & (BillingCharge.created_at <= today_end)
        not_cancelled = BillingCharge.status != BillingChargeStatus.cancelled
        charge_row = (
            self.db.query(
                func.coalesce(func.sum(case((is_today & not_cancelled, BillingCharge.net_amount), else_=0.0)), 0.0),
                func.coalesce(func.sum(case((not_cancelled, BillingCharge.net_amount), else_=0.0)), 0.0),
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
                ),
                func.coalesce(
                    func.sum(
                        case((is_today & not_cancelled & (BillingCharge.source_type == BillingSourceType.ot), BillingCharge.net_amount), else_=0.0)
                    ),
                    0.0,
                ),
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
                ),
            )
            .filter(BillingCharge.hospital_id == hid)
            .one()
        )
        todays_charges, total_charges, pending_count, todays_ot, todays_ipd = charge_row

        # todays_collections + total_paid both scan billing_payments — 1 query.
        payment_row = (
            self.db.query(
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
                ),
                func.coalesce(func.sum(BillingPayment.amount), 0.0),
            )
            .filter(BillingPayment.hospital_id == hid)
            .one()
        )
        todays_collections, total_paid = payment_row
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

        # today_inv_count + total_inv both scan billing_invoices — 1 query.
        invoice_row = (
            self.db.query(
                func.coalesce(
                    func.sum(case((BillingInvoice.invoice_date == today, 1), else_=0)), 0
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (BillingInvoice.status != BillingInvoiceStatus.cancelled, BillingInvoice.grand_total),
                            else_=0.0,
                        )
                    ),
                    0.0,
                ),
            )
            .filter(BillingInvoice.hospital_id == hid)
            .one()
        )
        today_inv_count, total_inv = invoice_row

        today_rcpt_count = (
            self.db.query(func.count(BillingReceipt.id))
            .filter(BillingReceipt.hospital_id == hid, BillingReceipt.payment_date == today)
            .scalar()
            or 0
        )

        recent_charges = (
            self.db.query(BillingCharge)
            .options(joinedload(BillingCharge.patient))
            .filter(BillingCharge.hospital_id == hid, BillingCharge.status != BillingChargeStatus.cancelled)
            .order_by(BillingCharge.created_at.desc())
            .limit(5)
            .all()
        )
        recent_payments = (
            self.db.query(BillingPayment)
            .options(joinedload(BillingPayment.patient))
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
