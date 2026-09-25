# HMS Sync Contracts and Clinical Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Define stable sync invalidation contracts and reconcile patient, admission, bed, nursing, and doctor views without losing active clinical edits.

**Architecture:** Keep backend audit/session commit hooks and the current SSE broker. Enumerate canonical resources and event domains, then wire focused frontend consumers to existing loaders and coalesced refresh handles. Enforce admission/bed lifecycle consistency in existing transactions.

**Tech Stack:** FastAPI, SQLAlchemy, pytest, React, TypeScript, Vite, `useSyncEvent`, `useCoalescedRefresh`, tab/reconnect utilities.

**Spec:** `docs/superpowers/specs/2026-09-25-hms-cross-module-consistency-design.md`

## Global Constraints

- Events are invalidations only; canonical API responses supply refreshed state.
- Keep tenant scoping, legacy event compatibility, existing bed uniqueness, and transaction boundaries.
- Preserve dirty clinical fields and finalized records.
- Avoid touching unrelated pre-existing diffs; record the baseline before editing any dirty file.

---

### Task 1: Record baseline and event-consumer matrix

**Files:**
- Create: `HMS-B/docs/superpowers/plans/hms-sync-event-consumer-matrix.md`
- Inspect: `HMS-B/shared/audit/service.py`, `HMS-B/shared/sync/broker.py`, `HMS-B/modules/sync/api/sync_api.py`
- Inspect: `HMS-frontend/src/lib/sync/syncService.ts`, `HMS-frontend/src/lib/sync/useSyncEvent.ts`, `HMS-frontend/src/lib/sync/refreshScheduler.ts`

- [ ] **Step 1: Record current changes before touching files**

Run `git -C HMS-B status --short` and `git -C HMS-frontend status --short`; save the baseline in the matrix and do not stage unrelated files.

- [ ] **Step 2: Trace the event path**

Record the session staging function, commit/rollback hooks, broker publication, SSE tenant filter, frontend event contract, reconnect handler, and tab reconciliation behavior using exact function names and event fields.

- [ ] **Step 3: Inventory high-risk domains**

For patient, admission, bed, vital/IPD vital, medication order, lab, radiology, pharmacy, billing, and master domains, list concrete mutation action/API files, emitted event names, active consumers, canonical fetch function, and tests. Search mutations in `HMS-B/modules` and subscribers in `HMS-frontend/src/app`.

- [ ] **Step 4: Check matrix completeness**

Run `rg -n "write_audit_log|record_audit|useSyncEvent|useCoalescedRefresh" HMS-B/modules HMS-frontend/src/app`; account for each key lifecycle mutation and screen or document the gap.

**Acceptance:** Every listed domain has an identified canonical owner and emitter/subscriber/test status; the matrix distinguishes unknowns from verified paths.

### Task 2: Add focused frontend sync utility tests and contract policy

**Files:**
- Modify: `HMS-frontend/package.json`
- Modify: `HMS-frontend/src/lib/sync/syncService.ts`
- Modify: `HMS-frontend/src/lib/sync/useSyncEvent.ts`
- Modify: `HMS-frontend/src/lib/sync/refreshScheduler.ts`
- Create: `HMS-frontend/src/lib/sync/eventInvalidationPolicy.ts`
- Create: `HMS-frontend/src/lib/sync/eventInvalidationPolicy.test.ts`
- Create/modify: `HMS-frontend/vite.config.ts` only if required for the test runner

- [ ] **Step 1: Establish a minimal frontend test command**

Add Vitest and the minimal DOM/testing dependencies only if component behavior tests require them. Add `"test": "vitest run"` and `"test:watch": "vitest"` scripts; keep `pnpm build` unchanged. Add test config using the existing Vite config.

- [ ] **Step 2: Write policy tests first**

Test that patient, admission/bed, vitals, laboratory, radiology, pharmacy, billing, and master events map only to their documented resource/cache prefixes; test legacy aliases and hospital-qualified keys remain isolated.

- [ ] **Step 3: Implement a small shared mapping**

Export a typed `cachePrefixesForSyncEvent(event: SyncEvent): readonly string[]` from `eventInvalidationPolicy.ts`. Map domain/type aliases explicitly; unknown events return an empty list. Do not add entity payload mirrors or page-specific refresh code to this utility.

- [ ] **Step 4: Test scheduler and event behavior**

Use fake timers to prove matching event bursts coalesce, in-flight refreshes produce one trailing refresh, and unsubscribe/unmount cancels pending work. Preserve observable errors without clearing last-known UI data.

- [ ] **Step 5: Run frontend tests and build**

Run `pnpm test -- --run` and `pnpm build` from `HMS-frontend`; record exact output.

**Acceptance:** Shared policy is typed, narrow, and tested; no screen yet clears unrelated cache keys.

### Task 3: Enforce canonical admission and bed lifecycle invariants

**Files:**
- Inspect/modify: `HMS-B/modules/inpatient/actions/admission_actions.py`
- Inspect/modify: `HMS-B/modules/inpatient/actions/admission_lifecycle_actions.py`
- Inspect/modify: `HMS-B/modules/inpatient/actions/registration_admission_actions.py`
- Inspect/modify: `HMS-B/modules/beds/actions/bed_actions.py`
- Inspect/modify: `HMS-B/modules/inpatient/entities/admission.py`
- Inspect/modify: `HMS-B/modules/patients/entities/patient.py`
- Test: `HMS-B/modules/inpatient/tests/test_canonical_lifecycle.py`
- Test: `HMS-B/modules/inpatient/tests/test_duplicate_admission_guard.py`
- Test: `HMS-B/modules/beds/tests/test_beds.py`

- [ ] **Step 1: Add invariant tests**

Cover request/admit/transfer/discharge/cancel transitions and assert patient compatibility status cannot contradict active admission lifecycle. Assert no patient has multiple active admissions and no bed has multiple active occupants.

- [ ] **Step 2: Run focused tests to establish failures or current guarantees**

Run `.\.venv\Scripts\pytest.exe modules/inpatient/tests/test_canonical_lifecycle.py modules/inpatient/tests/test_duplicate_admission_guard.py modules/beds/tests/test_beds.py -q` from `HMS-B`.

- [ ] **Step 3: Implement the smallest transactional invariant fix**

Derive patient status from the active admission where safe. If compatibility requires a stored status, update it in the existing admission transaction and centralize the invariant helper rather than adding an asynchronous mirror.

- [ ] **Step 4: Verify transfer and discharge stay segments**

Assert old stay closes before the new stay opens, old bed frees, new bed occupies, admission location updates, and discharge closes the stay/releases the bed. Preserve current uniqueness constraints.

- [ ] **Step 5: Verify after-commit events**

Add a rollback assertion that no event publishes after rollback and a commit assertion for lifecycle invalidations using existing sync tests or fixtures.

**Acceptance:** Critical lifecycle operations are transactional, tested, tenant scoped, and emit only post-commit invalidations.

### Task 4: Reconcile active clinical and operational screens

**Files:**
- Modify as inventory confirms: `HMS-frontend/src/app/inpatient/InpatientChart.tsx`
- Modify as inventory confirms: `HMS-frontend/src/app/inpatient/useInpatientChart.ts`
- Modify: `HMS-frontend/src/app/NursesPage.tsx`
- Modify: `HMS-frontend/src/app/DoctorsPage.tsx`
- Modify: `HMS-frontend/src/app/RegistrationPage.tsx`
- Modify: `HMS-frontend/src/app/EmergencyPage.tsx`
- Modify: `HMS-frontend/src/app/CriticalCarePage.tsx`
- Modify: `HMS-frontend/src/app/AllIpdForms.tsx` (resolve actual path from `rg --files` before editing)
- Modify: `HMS-frontend/src/app/DMSPage.tsx`
- Create: focused tests adjacent to sync hooks/loaders under `HMS-frontend/src/lib/sync/` and `HMS-frontend/src/app/inpatient/`

- [ ] **Step 1: Identify each loader and dirty-state boundary**

For each page, record existing loaders and which state is system-owned, derived, or user-edited. Do not implement a full-page remount as a refresh mechanism.

- [ ] **Step 2: Add patient/admission/bed/vitals/order subscriptions**

Subscribe only to domains each view consumes; route callbacks through coalesced targeted loaders. Include lab/radiology/care-team/discharge domains for inpatient chart only where data is displayed.

- [ ] **Step 3: Protect dirty forms**

Refresh read-only subresources and clean system-owned headers. When a form/note is dirty, retain editor state and surface/record that canonical data changed; do not reset form state or submit stale state silently.

- [ ] **Step 4: Add synchronization behavior tests**

Test nurse transfer updates doctor location, nurse vital updates chart data without remount, and an external patient/admission event leaves dirty form fields unchanged.

- [ ] **Step 5: Verify navigation/reconnect behavior**

Use existing tab/reconnect reconciliation hooks for missed SSE periods; assert coalescing prevents duplicate fetches when focus and visibility fire together.

**Acceptance:** Live clinical views reconcile by targeted fetch; dirty inputs and finalized records remain intact.

### Task 5: Clinical slice verification

**Files:**
- Update: `HMS-B/docs/superpowers/plans/hms-sync-event-consumer-matrix.md`

- [ ] **Step 1: Run backend clinical tests**

Run `.\.venv\Scripts\pytest.exe modules/inpatient/tests modules/beds/tests modules/patients/tests modules/sync/tests -q` from `HMS-B`.

- [ ] **Step 2: Run frontend tests/build**

Run `pnpm test -- --run` and `pnpm build` from `HMS-frontend`.

- [ ] **Step 3: Complete manual scenarios A, C, H, and I**

Record whether bed transfer, vitals, patient demographic amendments, and dirty IPD forms reconcile in the running app. Mark each as manual, automated, or blocked by environment.

- [ ] **Step 4: Update matrix with evidence**

Record exact test names, emitter/subscriber/data loader, and any remaining gaps. Do not claim scenarios not executed.

**Acceptance:** Clinical slice report is evidence-backed and no unrelated working-tree change is staged.

