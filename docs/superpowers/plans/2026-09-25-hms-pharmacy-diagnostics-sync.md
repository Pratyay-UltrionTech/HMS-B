# HMS Pharmacy and Diagnostics Synchronization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reconcile Pharmacy, Nursing, Doctor, Lab, Radiology, Billing, and patient record views after relevant prescription, medication-order, diagnostic, fulfilment, or cancellation changes.

**Architecture:** Retain domain-owned order/result models and after-commit invalidations. Active consumers subscribe to relevant domains and refetch canonical queue/detail resources through coalesced loaders. Historical dispensing, administration, and released results remain immutable under existing amendment semantics.

**Tech Stack:** Python, SQLAlchemy, pytest, React, TypeScript, Vite, HMS SSE hooks and API clients.

**Spec:** `docs/superpowers/specs/2026-09-25-hms-cross-module-consistency-design.md`

## Global Constraints

- Preserve completed POS sales, administered medication records, and released result history.
- Cancel downstream work according to its current lifecycle state.
- Do not use event payloads as source-of-truth entities.
- Do not erase dirty clinical/pharmacy input on refresh.

---

### Task 1: Map prescription, medication, lab, and radiology lifecycles

**Files:**
- Inspect: `HMS-B/modules/pharmacy/actions/pharmacy_actions.py`
- Inspect: `HMS-B/modules/pharmacy/api/pharmacy_api.py`
- Inspect: `HMS-B/modules/inpatient/actions/medication_order_actions.py`
- Inspect: `HMS-B/modules/inpatient/actions/emar_actions.py`
- Inspect: `HMS-B/modules/laboratory/actions/laboratory_actions.py`
- Inspect: `HMS-B/modules/radiology/actions/radiology_actions.py`
- Inspect: matching entities under `HMS-B/modules/pharmacy/entities/`, `HMS-B/modules/inpatient/entities/`, `HMS-B/modules/laboratory/entities/`, and `HMS-B/modules/radiology/entities/`

- [ ] **Step 1: Enumerate state transitions**

For each entity, record create/amend/cancel/collect/process/verify/release/dispense/complete transitions and the emitted event name/action.

- [ ] **Step 2: Classify duplicate pharmacy request fields**

Inspect `PharmacyRxRequest` fields and all consumers; label patient name/phone/doctor name as live, display, compatibility, snapshot, or accidental, with evidence and safe action.

- [ ] **Step 3: Update the event matrix**

Record queue/detail consumers and canonical API loaders, including Billing, Nursing/eMAR, Doctors/Inpatient Chart, DMS/clinical records, and patient views.

### Task 2: Subscribe Pharmacy and connected nursing/doctor consumers

**Files:**
- Modify: `HMS-frontend/src/app/PharmacyPage.tsx`
- Modify if loader definitions require it: `HMS-frontend/src/app/PharmacyPage.sections.ts`
- Modify as needed: `HMS-frontend/src/app/PrescriptionWorkspace.tsx`
- Modify as needed: `HMS-frontend/src/app/NursesPage.tsx`
- Modify as needed: `HMS-frontend/src/app/inpatient/InpatientChart.tsx`
- Reuse: `HMS-frontend/src/lib/sync/useSyncEvent.ts`, `refreshScheduler.ts`, `eventInvalidationPolicy.ts`
- Test: `HMS-frontend/src/lib/sync/pharmacySync.test.ts`

- [ ] **Step 1: Write failing queue refresh tests**

Verify prescription create/cancel/amend and medication order/fulfilment events cause one canonical Pharmacy queue refresh; patient event refreshes only patient display-dependent data.

- [ ] **Step 2: Wire Pharmacy subscriptions**

Add event domains identified by backend emitters; use the current queue loaders and coalesced refresh. Refetch order/request rows rather than constructing queue items from SSE payloads.

- [ ] **Step 3: Wire nursing and doctor downstream state**

Ensure medication discontinuation invalidates eMAR due schedules and active doctor chart order sections. Keep dirty note/form state intact.

- [ ] **Step 4: Verify completed sale history**

Test external inventory/fulfilment changes do not mutate completed transaction display snapshots.

### Task 3: Complete lab and radiology downstream invalidation

**Files:**
- Modify only where inventory identifies gaps: `HMS-frontend/src/app/LaboratoryPage.tsx`
- Modify only where inventory identifies gaps: `HMS-frontend/src/app/RadiologyPage.tsx`
- Modify only where inventory identifies gaps: `HMS-frontend/src/app/LabOrderDetailPage.tsx`
- Modify only where inventory identifies gaps: `HMS-frontend/src/app/RadOrderDetailPage.tsx`
- Modify: `HMS-frontend/src/app/inpatient/InpatientChart.tsx`
- Test: `HMS-frontend/src/lib/sync/diagnosticsSync.test.ts`

- [ ] **Step 1: Add tests for create, cancellation, completion, and amendment**

Assert relevant Doctor/Inpatient chart subresources, nursing where consumed, and active Billing status refresh from canonical APIs. Preserve prior released result/report versions.

- [ ] **Step 2: Add only missing subscriptions**

Use domain events already emitted by backend and refresh only the affected queue/detail/clinical section; preserve existing working Laboratory/Radiology sync.

- [ ] **Step 3: Verify state-aware cancellation on backend**

Add tests proving pending work may cancel, collected/performed work follows supported cancellation/exception state, and completed results remain queryable. Update backend actions only where a tested lifecycle gap exists.

- [ ] **Step 4: Run focused tests**

Run pytest for `modules/pharmacy/tests`, `modules/laboratory/tests`, and `modules/radiology/tests`; run `pnpm test -- --run` and `pnpm build`.

### Task 4: Verify scenarios and event coverage

- [ ] **Step 1: Complete manual scenarios B, D, and relevant parts of H**

Verify prescription appears in active Pharmacy queue, completed lab result appears in Doctor/Inpatient view, and demographic changes do not rewrite released result history.

- [ ] **Step 2: Audit cancellation and event actions**

Use the matrix to verify cancel/complete/amend paths emit suitable invalidations in addition to creation paths.

- [ ] **Step 3: Record exact evidence and gaps**

List test commands/results and any unsupported event path with affected entity and reason.

**Acceptance:** Pharmacy and diagnostics consumers reconcile after relevant lifecycle changes with immutable completed transaction/result history.

