# HMS Cross-Module Consistency Design

## Purpose

Make active HMS modules converge on canonical backend state after relevant committed mutations, while retaining immutable transaction-time/clinical history and protecting in-progress edits from stale writes and background refreshes.

This is an incremental consistency project, not an architecture rewrite. The existing SQLAlchemy transaction hooks, audit-driven sync broker, SSE API, frontend `useSyncEvent`, coalesced refresh scheduler, tab/reconnect hooks, API clients, and `clientCache` remain the foundation.

## Current Context and Constraints

- Backend sync events are staged in the SQLAlchemy session and published after commit; rollback clears pending events.
- Frontend has SSE subscription and coalesced refresh utilities, and some pages already consume them. Coverage is uneven; `PharmacyPage`, `BillingPage`, `RegistrationPage`, `EmergencyPage`, `CriticalCarePage`, `InpatientChart`, `AllIpdForms`, and DMS are among named audit targets.
- `clientCache` is in-memory and currently offers exact/prefix invalidation. Per-browser invalidation does not reach other clients.
- Backend schema changes are made through project SQL migration scripts; no active Alembic migration tree was identified. A schema change must include a compatible project migration script and deployment notes.
- Invoice creation (`modules/billing/services/invoice_service.py:create_invoice_from_charges`) re-queries submitted charge IDs from the database, validates hospital and patient ownership and non-cancelled status, and snapshots current canonical charge amounts. It does not independently select every currently unbilled charge; the frontend supplies the selection. Existing discharge validation detects charges omitted from the latest final invoice. The implementation must preserve these safeguards and determine whether additional server-side unbilled-charge validation is required by actual invoice/finalization semantics before changing it.
- Both `HMS-B` and `HMS-frontend` have extensive pre-existing working-tree modifications. Implementation must inspect and preserve them; no unrelated formatting or cleanup is in scope.

## Goals

1. Define canonical owner, event domains, and active consumers for high-risk shared entities.
2. Close stale-state gaps with domain-scoped invalidation and targeted canonical API refreshes.
3. Keep admission lifecycle and bed occupancy consistent transactionally.
4. Protect drafts, active clinical editing, and finalized historical records.
5. Detect and reject stale writes for selected high-risk editable resources using existing persistence/migration conventions.
6. Preserve hospital/tenant scoping, performance, existing lifecycle workflows, and event compatibility.
7. Add focused backend and frontend regression tests for critical cross-module scenarios.

## Non-Goals

- Replacing SSE, introducing another broker, Redux, or a broad state-management rewrite.
- Refreshing all HMS data after every event.
- Converting historical snapshots into live references.
- Removing duplicated fields before their role is classified.
- Aggressive polling or global cache disabling.
- A risky global identifier rename or broad department-rule change.

## Consistency Contract

The mutation and reconciliation path is:

```text
canonical backend mutation
  -> same database transaction stages audit/sync invalidation
  -> successful commit publishes event
  -> tenant-scoped active consumers invalidate relevant cache/resource state
  -> coalesced targeted API refetch reconciles UI
```

An event communicates that a domain may be stale; the event payload is not an alternative state store. Reconciliation failures leave usable current UI state where possible, are observable in development without PHI, and can retry on reconnect/tab visibility or the existing safety mechanism. Refreshes must not replace dirty clinician-authored inputs.

Data is classified explicitly:

- **Live reference:** canonical patient demographics, active admission/location, pending work status, current balance, active orders, active master data. Refetch or derive from the authoritative resource.
- **Historical snapshot:** issued invoices/receipts, completed pharmacy transactions, administered medication records, completed bed-stay prices, finalized/signed forms and reports, released results. Preserve original values; corrections use existing amendment/history workflows.
- **Draft/mixed:** refresh only system-owned header/prefill fields when safe; retain clinician-entered values and mark/reconcile conflicts rather than silently replacing dirty form data.

## Canonical Ownership Decisions

- Patient identity and current demographics: patient record.
- Current IPD episode/lifecycle: canonical admission. Any compatibility `patients.status` representation is derived or updated within the same transaction and guarded by tests/invariants; do not create an endless asynchronous status mirroring loop.
- Current physical location and occupancy: admission bed reference plus transactional bed/stay records and existing bed constraints.
- Orders/results: domain order and result/report entities; consumers refetch these entities.
- Billing balance and invoiceable charges: canonical billing account/charge/payment entities. Issued invoice lines remain snapshots.
- Master catalogues/pricing/staff: corresponding canonical master entities with hospital-scoped invalidation.
- Pharmacy request display fields are classified individually before changes: current contact data resolves from patient; transaction-time display snapshots remain if audit/history semantics require them; compatibility-only fields are not removed until consumers and persistence constraints are known.

## Event and Cache Policy

Create a small reusable invalidation policy that maps stable event domains to cache/resource prefixes (for example patient, admission/bed, billing, laboratory, radiology, pharmacy, and master domains). Keep legacy event names working; add aliases or compatible emissions where a logical domain currently has ambiguous names. Do not put full clinical/business records in events.

Emit invalidations for create/update/cancel/complete/transfer/admit/discharge/restore/reopen/void/amend where the mutation is supported. Events stay hospital-scoped and are emitted only after successful commit. Frontend consumers subscribe only to dependencies they display. Multiple related events share the existing debounce/coalescing behavior. Master-data invalidation removes only matching cached keys for the active hospital/session; unrelated cache entries and TTL benefits remain.

## Workstreams and Sequence

The umbrella work is divided into independently reviewable vertical slices. Each slice begins with a precise inventory of existing endpoints, emitters, subscribers, forms, tests, and current dirty-file state. A slice must preserve existing behavior and add regression tests before claiming completion.

### Slice 1: Contracts and Clinical/Operational Reconciliation

- Produce an event/consumer matrix for patient, admission, bed, vitals, care team, medication orders, lab, radiology, billing, and master domains.
- Audit lifecycle emitters and fill missing commit-after-event coverage without renaming event contracts incompatibly.
- Integrate named stale consumers, prioritizing `InpatientChart`, Nursing/eMAR, Registration, Emergency, Critical Care, IPD forms, and DMS. Use targeted resource loaders, not chart remounts.
- Preserve dirty clinical forms: background events may update clean, system-owned display/header state; dirty clinician-owned state remains untouched and is not submitted over newer canonical state without an explicit conflict path.
- Verify transfer still closes the old stay, releases the old bed, updates admission location, occupies the destination, opens the new stay, and reconciles open dependent views.
- Verify discharge remains transactional and active queues remove/reclassify discharged patients while historical encounter records remain.
- Resolve contradictory patient/admission status by canonical derivation where possible; otherwise transactionally maintain and test a documented compatibility invariant.

### Slice 2: Billing and Financial Consumers

- Add relevant billing invalidation to active Billing and downstream account views; reconcile charges/payments from canonical account APIs.
- Keep pharmacy/lab/radiology/bed/procedure charge mutations visible to billing after commit.
- Preserve backend invoice input validation, charge snapshot semantics, and discharge stale-invoice gate. Inspect the complete callers/contracts to establish whether charge selection is intentionally partial or must cover every current unbilled charge before modifying finalization behavior.
- No completed invoice, receipt, or pharmacy transaction is rewritten by a refresh.

### Slice 3: Pharmacy and Diagnostics

- Add Pharmacy subscriptions and targeted queue/account refreshes for prescription create/amend/cancel, medication-order changes, patient updates, fulfilment, relevant inventory, and dispensing.
- Ensure pharmacy billing/nursing/discharge side effects emit or consume existing suitable invalidations.
- Extend lab and radiology downstream consumer coverage for creation, collection/processing, verification/completion, cancellation, and explicit amendment.
- Apply state-aware cancellation: pending downstream work may cancel; collected/performed/completed work retains history and follows domain-appropriate states.

### Slice 4: Cross-Client Master Invalidation and Live Queues

- Audit master mutation endpoints and events for departments, wings, wards, doctors/staff, consultation pricing, lab/radiology catalogues, and other cached operational masters.
- Reuse the shared cache invalidation policy in active clients and retain hospital-scoped keys.
- Cover search, patient directory, appointment/doctor/nursing/IPD/lab/radiology/pharmacy/billing/bed/emergency/ICU/OT/DMS live queues discovered in inventory.
- Keep invalidation targeted and refetch only resources used by the active view.

### Slice 5: Selected Stale-Write Protection

- Inventory edit endpoints and existing concurrency patterns first. Select high-risk mutable records where lost updates are plausible (at minimum evaluate draft IPD forms/notes, admission lifecycle edits, and active orders).
- Prefer an existing version/timestamp/ETag mechanism if already present. If persistence changes are needed, add a safe project-native SQL migration, API contract, and controlled conflict response.
- A stale client must receive a conflict and current-version/reload guidance rather than silently overwriting a newer record. Finalized records remain immutable under existing domain rules.

## Failure, Reconnect, and Performance Behavior

- Coalesce related events and guard in-flight refreshes using current scheduler utilities.
- Use existing tab-visible, focus, and SSE reconnect reconciliation so missed event history cannot leave important active state stale indefinitely.
- Avoid a new periodic polling layer. Preserve any existing justified safety refresh.
- Failed refetches do not crash the page or clear valid displayed data. Unauthorized sessions follow existing auth handling; removed entities are reconciled from canonical list/detail responses.
- Add development-level sync diagnostics only where useful, without PHI or noisy production logs.
- Keep every query, cache key, subscription, and event scoped by hospital/tenant.

## Testing and Acceptance

Backend tests cover lifecycle/status consistency, active-admission and active-bed uniqueness, transfer stay closure/opening, discharge bed release, state-aware cancellation, invoice canonical validation, event-after-commit/rollback behavior, tenant isolation, and stale-write conflicts for protected entities.

Frontend tests cover event-to-targeted-refetch behavior for patient changes, bed transfer, lab completion, pharmacy queue, billing charge visibility, master cache invalidation across sessions, and preservation of dirty form values during sync. Existing test tooling should be used; no parallel sync framework is introduced.

Acceptance also includes the requested manual scenarios A-I where supported by the local app/test environment, frontend tests and build/typecheck, relevant backend tests, configured lint, SSE reconnect recovery, no refresh loops/storms, and tenant isolation. Reports must distinguish automated results from manual checks and environment limitations.

## Migration and Compatibility

No schema change is assumed by this design. If concurrency protection needs a version column or another schema change, use a reviewed migration script in `HMS-B/scripts` following existing idempotency/deployment conventions; do not add Alembic unless the project migration strategy has changed. Keep event names compatible, retain historical columns unless classified and safely deprecated, and preserve all existing workflows named in the user request.

## Deliverables

1. Event/consumer/canonical-owner matrix maintained with implementation evidence.
2. Incremental code and focused tests for each accepted slice.
3. Migration scripts only where justified.
4. Final implementation report covering architecture, changed files, event coverage, canonical/history decisions, invoice behavior, concurrency scope, draft behavior, tests/results, migrations, and remaining risks.

