# HMS Billing Consistency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep active billing views current when canonical charges and payments change while preserving finalized invoice snapshots and server-side financial validation.

**Architecture:** Subscribe Billing to relevant invalidations, refetch the canonical account/charge/payment resources, and coalesce workflows that emit several related events. Preserve `create_invoice_from_charges` charge re-query and the discharge stale-invoice gate unless concrete caller analysis shows a required invariant gap.

**Tech Stack:** Python, SQLAlchemy, pytest, React, TypeScript, existing HMS sync hooks/API clients.

**Spec:** `docs/superpowers/specs/2026-09-25-hms-cross-module-consistency-design.md`

## Global Constraints

- Invoice generation re-queries each submitted charge ID; it does not currently auto-select all unbilled charges.
- Issued invoice lines and receipts remain historical snapshots.
- Never trust the currently visible UI as the sole financial validation authority.
- Keep event/query/cache scoping by hospital.

---

### Task 1: Trace billing mutation and invoice callers

**Files:**
- Inspect: `HMS-B/modules/billing/actions/billing_actions.py`
- Inspect: `HMS-B/modules/billing/api/billing_api.py`
- Inspect: `HMS-B/modules/billing/services/invoice_service.py`
- Inspect: `HMS-B/modules/inpatient/actions/admission_actions.py`
- Inspect: `HMS-frontend/src/app/BillingPage.tsx`
- Test: `HMS-B/modules/billing/tests/test_billing.py`
- Test: `HMS-B/modules/inpatient/tests/test_final_closure_verification.py`

- [ ] **Step 1: Trace invoice request payload to service call**

Record whether UI selection is allowed to be partial for each draft/provisional/final invoice path, whether service checks ownership/status, and how discharge detects uncovered charges.

- [ ] **Step 2: Trace charge mutation domains**

Map billing, pharmacy, lab, radiology, bed-stay, OT/procedure, admission, payment, and refund mutations to action/API function, audit event type, and billing loader.

- [ ] **Step 3: Add conclusions to matrix**

Update `HMS-B/docs/superpowers/plans/hms-sync-event-consumer-matrix.md` with verified behavior and any exact financial validation gap.

### Task 2: Reconcile active Billing screens

**Files:**
- Modify: `HMS-frontend/src/app/BillingPage.tsx`
- Modify only if needed: `HMS-frontend/src/app/BillingPage.sections.ts`
- Reuse: `HMS-frontend/src/lib/sync/useSyncEvent.ts`
- Reuse: `HMS-frontend/src/lib/sync/refreshScheduler.ts`
- Test: frontend sync tests under `HMS-frontend/src/lib/sync/`

- [ ] **Step 1: Add failing billing invalidation test**

Simulate committed billing, pharmacy, laboratory, radiology, bed, and procedure-charge events; assert one coalesced canonical account refresh and no read of event payload as account state.

- [ ] **Step 2: Subscribe to relevant domains**

Wire the existing Billing account/charge/payment loader through a coalesced scheduler. Keep unrelated billing admin/catalogue data out of the invalidation path.

- [ ] **Step 3: Preserve failure and dirty workflow state**

Leave last-known ledger visible on a failed silent refresh. Do not clear invoice composer selections or form input automatically; revalidate submitted IDs on commit and show the existing API conflict/error to the user.

- [ ] **Step 4: Run focused frontend tests**

Run the new billing test by its test file path, then run `pnpm test -- --run` and `pnpm build`.

### Task 3: Strengthen authoritative invoice boundary only if required

**Files:**
- Modify only if caller analysis proves a gap: `HMS-B/modules/billing/services/invoice_service.py`
- Modify caller contract only if needed: `HMS-B/modules/billing/contracts/billing_contracts.py`, `HMS-B/modules/billing/actions/billing_actions.py`, `HMS-B/modules/billing/api/billing_api.py`
- Test: `HMS-B/modules/billing/tests/test_billing.py`
- Test: `HMS-B/modules/inpatient/tests/test_final_closure_verification.py`

- [ ] **Step 1: Write tests for the established intended semantics**

Assert foreign-hospital, foreign-patient, cancelled, missing, or already-invoiced submitted charges cannot be invoiced. Assert final discharge cannot complete while current account charges remain outside the latest final invoice.

- [ ] **Step 2: Add all-unbilled validation only if finalization contract requires it**

If final invoice is intended to include all current unbilled charges, calculate that set server-side in the same transaction immediately before finalization and reject/recalculate stale submissions. Preserve legitimate partial/provisional invoice behavior.

- [ ] **Step 3: Preserve immutable line snapshots**

Keep invoice line values copied from backend charge rows at finalization; do not change existing generated invoices when source charge data changes later.

- [ ] **Step 4: Run backend financial tests**

Run `.\.venv\Scripts\pytest.exe modules/billing/tests/test_billing.py modules/inpatient/tests/test_final_closure_verification.py -q` from `HMS-B`.

### Task 4: Verify cross-module billing outcomes

- [ ] **Step 1: Run billing and source-module tests**

Run billing, pharmacy, lab, radiology, inpatient, and sync test directories with the local virtualenv pytest executable.

- [ ] **Step 2: Complete manual scenarios F and H**

Verify pharmacy charge appears in an open billing account after event; demographic edits do not rewrite invoices or receipts. Record exact environment/result.

- [ ] **Step 3: Update matrix and report**

Record event emitters, Billing subscriber, canonical resource loader, invoice path conclusion, and historical snapshot behavior.

**Acceptance:** Active billing balances reconcile from backend state; finalized transaction records remain unchanged; server validates submitted invoice selections and discharge readiness.

