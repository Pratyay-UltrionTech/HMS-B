# HMS Concurrency Protection and Regression Verification Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Protect selected high-risk editable records from silent stale overwrites and complete the cross-module regression evidence for sync, history, reconnect, and tenant isolation.

**Architecture:** First reuse any existing row version, updated timestamp, ETag, or conditional update mechanism. If none exists, add the smallest version-based API contract and project-native migration; conflict responses retain server state and require an explicit reload/merge decision. Final records continue to follow existing immutability rules.

**Tech Stack:** SQLAlchemy, FastAPI, pytest, React, TypeScript, Vite, existing migration scripts and API error handling.

**Spec:** `docs/superpowers/specs/2026-09-25-hms-cross-module-consistency-design.md`

## Global Constraints

- Do not add schema changes before confirming the active migration strategy and checking current model/schema state.
- Protect only selected high-risk mutable entities; avoid a global version column rollout.
- A stale write returns a controlled conflict and never silently wins.
- Preserve pre-existing working tree content and finalized historical records.

---

### Task 1: Select concurrency-protected resources from actual write paths

**Files:**
- Inspect: `HMS-B/modules/inpatient/entities/`, `actions/clinical_notes_actions.py`, `actions/ipd_actions.py`, `actions/medication_order_actions.py`
- Inspect: relevant patient, admission, billing, and order entities/actions
- Inspect migration examples: `HMS-B/scripts/migrate_ipd_canonical_lifecycle.py`, `migrate_ipd_final_closure.py`, `migrate_group6_operational_state.py`
- Inspect API error contracts in `HMS-B/modules/inpatient/contracts/` and related APIs

- [ ] **Step 1: Inventory lost-update exposure**

For each candidate, trace read version, client edit payload, write query, finalization behavior, and current tests. Score impact and choose the smallest set with realistic concurrent edits.

- [ ] **Step 2: Check existing schema and migration conventions**

Confirm whether candidates already have `version`, `updated_at`, conditional updates, or database constraints. Record script transaction/idempotency and deployment patterns.

- [ ] **Step 3: Document selected entities and conflict API shape**

Define expected version input and conflict response fields using current contract conventions. Exclude entities where transaction isolation or immutability already prevents the risk.

### Task 2: Add stale-write protection and migration if necessary

**Files:**
- Modify only selected entity/action/contract/API/test files established by Task 1
- Create migration under `HMS-B/scripts/migrate_<selected_entity>_version.py` only if a new column is necessary
- Add focused tests beside the selected module tests

- [ ] **Step 1: Write concurrent stale-save test**

Load the same record/version twice; save user A; attempt save user B with the old version; assert controlled conflict, unchanged user A data, and latest version returned or retrievable.

- [ ] **Step 2: Add version field only when no existing mechanism fits**

Use integer monotonic version or existing project-supported conditional token. Include a safe migration script matching neighboring scripts, with idempotent schema checks and deployment instructions.

- [ ] **Step 3: Enforce version in the mutation transaction**

Use an atomic conditional update or row lock; increment version on success; return a domain conflict on zero matched rows. Emit sync invalidation only after the successful transaction commits.

- [ ] **Step 4: Preserve draft and finalized-state rules**

Allow versioned edits only in permitted lifecycle states. Do not make finalized/signed entities editable through this path; amendments remain explicit and historical.

- [ ] **Step 5: Run focused backend tests**

Run the selected module test file(s) and migration validation using the project’s established script/test method.

### Task 3: Frontend conflict behavior

**Files:**
- Modify only selected editor components/hooks found in Task 1
- Modify shared API error parsing only if current clients cannot represent a conflict
- Test: `HMS-frontend/src/lib/sync/concurrencyConflict.test.ts`

- [ ] **Step 1: Test conflict response handling**

Assert a stale-save response retains the user's draft, does not retry/overwrite automatically, and presents a clear reload/compare action.

- [ ] **Step 2: Send current version on edits**

Pass the loaded entity version/token through the existing save call without using event payloads as concurrency authority.

- [ ] **Step 3: Preserve editor state during external sync**

When invalidation arrives while dirty, update conflict indicator/canonical version metadata but retain authored field values until user resolves the conflict.

- [ ] **Step 4: Run frontend test and build**

Run the focused Vitest file, then `pnpm test -- --run` and `pnpm build`.

### Task 4: Complete regression and manual verification

**Files:**
- Update: `HMS-B/docs/superpowers/plans/hms-sync-event-consumer-matrix.md`
- Update final report alongside the matrix, including migration notes and remaining risks

- [ ] **Step 1: Run all relevant backend tests**

Run `.\.venv\Scripts\pytest.exe -q` from `HMS-B`; if the complete suite cannot pass due environment or pre-existing failure, list the exact failing tests and command output.

- [ ] **Step 2: Run frontend tests and production build**

Run `pnpm test -- --run` and `pnpm build` from `HMS-frontend`; run configured lint only if a lint script exists.

- [ ] **Step 3: Complete manual scenarios A-I**

Record each scenario as pass, fail, or not run with environment reason. Verify SSE reconnect, no refresh loops/storms, tenant isolation, and absence of new console errors.

- [ ] **Step 4: Audit final diffs and historical behavior**

Review all touched diffs against the pre-existing status baseline. Confirm finalized invoice, receipt, lab/radiology result, IPD form, medication administration, discharge, and pharmacy sale data are not rewritten by sync.

- [ ] **Step 5: Produce the implementation report**

Report architecture, files, emitter/subscriber/data refreshed matrix, canonical ownership, snapshots, invoice behavior, concurrency entities, draft rules, tests/commands/results, migrations, and residual risks.

**Acceptance:** Stale writes are controlled for selected resources; tests and manual evidence are accurately reported; no claim exceeds executed verification.

