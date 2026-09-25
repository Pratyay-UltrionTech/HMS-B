# HMS Master Cache Invalidation and Live Queues Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Invalidate only affected cached master data across connected clients and keep active search, directory, queue, and board views aligned with canonical state.

**Architecture:** Use the existing tenant-scoped SSE stream and shared invalidation mapping to clear relevant `clientCache` keys in the receiving browser, then let the active screen refetch its canonical API resource. Keep TTL caching for unrelated data.

**Tech Stack:** FastAPI, SQLAlchemy, pytest, React, TypeScript, Vite, current `clientCache` and sync utilities.

**Spec:** `docs/superpowers/specs/2026-09-25-hms-cross-module-consistency-design.md`

## Global Constraints

- Do not disable caching globally or clear unrelated keys.
- Include hospital/tenant identity in every cache key and event scope.
- Master events carry invalidations only.
- Keep queues and search results on canonical backend API results.

---

### Task 1: Audit master event emitters and cache key construction

**Files:**
- Inspect: `HMS-B/modules/masters/actions/masters_actions.py`
- Inspect: `HMS-B/modules/masters/api/masters_api.py`
- Inspect: `HMS-frontend/src/lib/clientCache.ts`
- Inspect: `HMS-frontend/src/app/MastersPage.tsx`
- Inspect: `HMS-frontend/src/app/AdminPage.tsx`
- Inspect: `HMS-frontend/src/app/OTPage.tsx`
- Inspect: `HMS-frontend/src/app/LaboratoryPage.tsx`, `RadiologyPage.tsx`, `DoctorsPage.tsx`, `AppointmentPage.tsx`

- [ ] **Step 1: List cached master keys and their API source**

Find every `clientCache.getOrFetch`, `set`, and invalidation call. Record the exact key format, TTL, hospital scope, and consuming page.

- [ ] **Step 2: List master mutations and event names**

Enumerate department, wing, ward, room, staff/doctor, consultation pricing, lab, radiology, insurance, and operational catalogue create/update/archive paths.

- [ ] **Step 3: Identify key scope fixes**

If any master key lacks hospital identity, add it consistently to reads and local invalidations before adding cross-client behavior; test that equal catalogue names in different hospitals never share entries.

### Task 2: Implement reusable cross-client targeted invalidation

**Files:**
- Modify: `HMS-frontend/src/lib/clientCache.ts`
- Modify: `HMS-frontend/src/lib/sync/eventInvalidationPolicy.ts`
- Modify: `HMS-frontend/src/lib/sync/syncService.ts`
- Create: `HMS-frontend/src/lib/sync/cacheInvalidation.test.ts`
- Modify as needed: `HMS-frontend/src/main.tsx` or authenticated app shell where a single global listener is mounted

- [ ] **Step 1: Write targeted invalidation tests**

Cache department, pricing, lab, radiology, and ward entries for two hospital keys. Dispatch a matching event and assert only relevant entries for the event hospital invalidate.

- [ ] **Step 2: Add a typed prefix invalidation helper if needed**

Expose a reusable `invalidatePrefixes(prefixes: readonly string[])` on `ClientCache`; keep `invalidate(keyOrPrefix)` backward compatible.

- [ ] **Step 3: Connect one authenticated event listener**

Mount one listener at an app-level authenticated boundary or equivalent existing provider. Resolve hospital scope from authenticated context and invalidate mapped prefixes; do not add per-page custom `if(event===...)` logic.

- [ ] **Step 4: Ensure active pages refetch after invalidation**

Pages that cache masters while open must subscribe to relevant master domains and rerun their loaders; a cache clear alone does not update already-rendered local state.

- [ ] **Step 5: Run cache tests and build**

Run `pnpm test -- --run` and `pnpm build`.

### Task 3: Close active queue and directory gaps

**Files:**
- Inspect/modify discovered subscribers in: `HMS-frontend/src/app/RegistrationPage.tsx`, `AppointmentPage.tsx`, `DoctorsPage.tsx`, `NursesPage.tsx`, `LaboratoryPage.tsx`, `RadiologyPage.tsx`, `PharmacyPage.tsx`, `BillingPage.tsx`, `BedsPage.tsx`, `EmergencyPage.tsx`, `CriticalCarePage.tsx`, `OTPage.tsx`, and `DMSPage.tsx`
- Test: `HMS-frontend/src/lib/sync/liveQueueSync.test.ts`

- [ ] **Step 1: Build queue dependency map**

For each active list, name its canonical API loader, event dependencies, current coalescing strategy, and user-entered filters that should survive reload.

- [ ] **Step 2: Add missing event-to-loader tests**

Test patient rename updates active search/directory results; lab cancellation leaves active queues; discharge removes/reclassifies inpatient census; bed transfer updates bed board; events preserve active filters and selection where safe.

- [ ] **Step 3: Add missing targeted subscriptions**

For each gap, schedule only the relevant canonical list loader. Do not reset filters, search text, pagination, or unrelated module state.

- [ ] **Step 4: Verify reconnect and tab recovery**

Ensure important active lists reconcile once after visibility/focus or SSE reconnect; do not add timers unless a documented safety need exists.

### Task 4: Backend event coverage and isolation verification

**Files:**
- Modify only gaps proven by matrix in master/queue mutation action files under `HMS-B/modules/`
- Test: `HMS-B/modules/masters/tests/test_masters.py`
- Test: `HMS-B/modules/sync/tests/test_sync_api.py`

- [ ] **Step 1: Add tests for master mutation invalidation**

Assert commit emits the hospital-scoped domain event and rollback emits none for representative master changes.

- [ ] **Step 2: Fill only missing event paths**

Stage events using existing audit service in the mutation transaction. Keep details non-sensitive and avoid changing established event names without a compatibility alias.

- [ ] **Step 3: Test tenant isolation**

Assert a client authenticated for one hospital cannot receive or invalidate another hospital's events or cached keys.

### Task 5: Verify master and queue scenarios

- [ ] **Step 1: Run focused backend tests**

Run `.\.venv\Scripts\pytest.exe modules/masters/tests modules/sync/tests -q` from `HMS-B`.

- [ ] **Step 2: Run frontend tests/build**

Run `pnpm test -- --run` and `pnpm build` from `HMS-frontend`.

- [ ] **Step 3: Complete manual scenarios E and applicable queue cases**

Verify a master price changed in one session is used by another session before a future transaction, and check patient search, active cancellation, discharge census, and bed board behavior.

- [ ] **Step 4: Update the matrix**

Record event, cache prefix, active loader, tenant scope, and automated/manual evidence.

**Acceptance:** Cross-client invalidation is targeted, active views refetch canonically, and unrelated cached masters retain TTL benefits.

