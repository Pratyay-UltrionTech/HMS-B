"""
Canonical Financial Clearance Service for Inpatient Discharge & Care Transitions.

Provides authoritative determination of whether an inpatient episode or service is
financially cleared for discharge, taking into account:
1. Posted charges and invoice status (un-invoiced charges, stale invoices).
2. Unallocated payment tenders and available deposits.
3. Automated payment-to-charge allocation and deposit drawdown.
4. Active, approved financial exceptions (separation-of-duties or emergency override).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingDeposit,
    BillingInvoice,
    BillingInvoiceStatus,
    BillingPayment,
    FinancialAccount,
)
from modules.billing.entities.financial_exception import (
    FinancialClearanceException,
    FinancialExceptionStatus,
)
from modules.billing.services.billing_service import (
    allocate_payment_to_charges,
    draw_down_deposits_bulk_for_account,
    payment_unallocated_amount,
)
from modules.billing.services.invoice_freshness_service import verify_invoice_freshness
from modules.inpatient.db.discharge_exception_repository import DischargeExceptionRepository
from modules.inpatient.entities.admission import Admission
from shared.exceptions.base import ValidationError


@dataclass(frozen=True)
class DischargeClearanceResult:
    can_discharge: bool
    status: str  # "cleared", "outstanding_due", "exception_approved", "emergency_override", "missing_invoice", "stale_invoice"
    outstanding_amount: float
    total_charges: float
    total_payments: float
    total_deposits_available: float
    unallocated_payments: float
    reason: str | None = None
    exception_id: UUID | None = None


class FinancialClearanceService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def evaluate_discharge_clearance(
        self,
        hospital_id: UUID,
        admission_id: UUID,
        patient_id: UUID | None = None,
    ) -> DischargeClearanceResult:
        """
        Pure evaluation of financial clearance without mutating payment allocations.
        Calculates ledger balances and checks for approved exceptions.
        """
        adm = (
            self.db.query(Admission)
            .filter(Admission.id == admission_id, Admission.hospital_id == hospital_id)
            .first()
        )
        if adm and not patient_id:
            patient_id = adm.patient_id

        acc = (
            self.db.query(FinancialAccount)
            .filter(
                FinancialAccount.hospital_id == hospital_id,
                FinancialAccount.admission_id == admission_id,
            )
            .first()
        )

        from modules.inpatient.services.inpatient_billing_service import InpatientBillingService
        billing_svc = InpatientBillingService(self.db)
        fin = billing_svc.get_ledger_totals(
            hospital_id, patient_id=patient_id, admission_id=admission_id
        )

        outstanding = float(fin.get("outstanding") or 0.0)
        total_charges = float(fin.get("total_charges") or 0.0)
        total_payments = float(fin.get("total_payments") or 0.0)
        deposits_avail = float(fin.get("total_deposits_available") or 0.0)

        # Calculate any unallocated payment tenders
        unallocated_total = 0.0
        if acc:
            payments = (
                self.db.query(BillingPayment)
                .filter(
                    BillingPayment.hospital_id == hospital_id,
                    BillingPayment.account_id == acc.id,
                )
                .all()
            )
            for p in payments:
                unallocated_total += float(payment_unallocated_amount(self.db, p))

        # Check for approved financial exceptions (Inpatient or Billing domain models)
        exc_repo = DischargeExceptionRepository(self.db, hospital_id)
        inpatient_exc = exc_repo.get_active_approved_exception(admission_id)

        billing_exc = (
            self.db.query(FinancialClearanceException)
            .filter(
                FinancialClearanceException.hospital_id == hospital_id,
                FinancialClearanceException.admission_id == admission_id,
                FinancialClearanceException.service_type == "discharge",
                FinancialClearanceException.status == FinancialExceptionStatus.approved,
            )
            .first()
        )

        active_exc_id = None
        is_emergency = False
        if inpatient_exc:
            active_exc_id = inpatient_exc.id
        elif billing_exc:
            active_exc_id = billing_exc.id
            is_emergency = billing_exc.is_emergency

        # Net balance after applying unallocated payments and available deposits
        net_outstanding = max(0.0, round(outstanding - unallocated_total - deposits_avail, 2))

        if active_exc_id:
            status_str = "emergency_override" if is_emergency else "exception_approved"
            return DischargeClearanceResult(
                can_discharge=True,
                status=status_str,
                outstanding_amount=net_outstanding,
                total_charges=total_charges,
                total_payments=total_payments,
                total_deposits_available=deposits_avail,
                unallocated_payments=unallocated_total,
                exception_id=active_exc_id,
            )

        if net_outstanding <= 0.009:
            return DischargeClearanceResult(
                can_discharge=True,
                status="cleared",
                outstanding_amount=0.0,
                total_charges=total_charges,
                total_payments=total_payments,
                total_deposits_available=deposits_avail,
                unallocated_payments=unallocated_total,
            )

        reason = (
            f"Cannot discharge — outstanding balance ₹{net_outstanding:,.2f} "
            f"(after ₹{deposits_avail:,.2f} deposit credit). "
            f"Clear all dues or obtain an approved financial discharge exception before discharging."
        )
        return DischargeClearanceResult(
            can_discharge=False,
            status="outstanding_due",
            outstanding_amount=net_outstanding,
            total_charges=total_charges,
            total_payments=total_payments,
            total_deposits_available=deposits_avail,
            unallocated_payments=unallocated_total,
            reason=reason,
        )

    def can_discharge_admission(
        self, hospital_id: UUID, admission_id: UUID, patient_id: UUID | None = None
    ) -> bool:
        """Convenience boolean check for discharge eligibility."""
        res = self.evaluate_discharge_clearance(hospital_id, admission_id, patient_id)
        return res.can_discharge

    def reconcile_and_clear_for_discharge(
        self,
        hospital_id: UUID,
        admission: Admission,
        actor: dict[str, Any],
    ) -> DischargeClearanceResult:
        """
        Executes automatic payment reconciliation, deposit drawdown, and enforces
        strict financial clearance gating prior to patient discharge.
        Raises ValidationError if discharge is blocked.
        """
        acc = (
            self.db.query(FinancialAccount)
            .filter(
                FinancialAccount.hospital_id == hospital_id,
                FinancialAccount.admission_id == admission.id,
            )
            .first()
        )

        if acc:
            # 1. Invoice freshness check
            latest_invoice = (
                self.db.query(BillingInvoice)
                .filter(
                    BillingInvoice.hospital_id == hospital_id,
                    BillingInvoice.account_id == acc.id,
                    BillingInvoice.status.in_([BillingInvoiceStatus.generated, BillingInvoiceStatus.paid]),
                )
                .order_by(BillingInvoice.created_at.desc())
                .first()
            )
            if not latest_invoice:
                raise ValidationError(
                    "Final billing required: A generated final invoice must exist for the admission account before discharge."
                )

            is_fresh, stale_reason = verify_invoice_freshness(self.db, hospital_id, latest_invoice)
            if not is_fresh:
                raise ValidationError(
                    f"Final bill out of date: {stale_reason} Please regenerate the final bill before discharge."
                )

            # 2. Reconcile unallocated payments
            acc_payments = (
                self.db.query(BillingPayment)
                .filter(
                    BillingPayment.hospital_id == hospital_id,
                    BillingPayment.account_id == acc.id,
                )
                .all()
            )
            for pay in acc_payments:
                unalloc_tender = payment_unallocated_amount(self.db, pay)
                if unalloc_tender > 0:
                    allocate_payment_to_charges(
                        self.db,
                        hospital_id,
                        admission.patient_id,
                        unalloc_tender,
                        payment_id=pay.id,
                        created_by_name=actor.get("name") or "Discharge Settlement",
                        account_id=acc.id,
                    )

            # 3. Draw down available deposits in bulk
            draw_down_deposits_bulk_for_account(
                self.db,
                hospital_id=hospital_id,
                patient_id=admission.patient_id,
                account_id=acc.id,
                created_by_name=actor.get("name") or "Discharge Settlement",
            )
            self.db.flush()

        # 4. Authoritative evaluation
        result = self.evaluate_discharge_clearance(
            hospital_id, admission.id, patient_id=admission.patient_id
        )
        if not result.can_discharge:
            raise ValidationError(result.reason or "Patient is not financially cleared for discharge.")

        return result
