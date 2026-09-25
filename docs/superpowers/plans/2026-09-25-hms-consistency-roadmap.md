# HMS Cross-Module Consistency Implementation Roadmap

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make active HMS modules reconcile canonical backend state safely while preserving drafts, transaction-time history, tenant isolation, and current transactional workflows.

**Architecture:** Extend existing SQLAlchemy after-commit sync events, SSE, frontend subscriptions/coalescing/tab recovery, and targeted API loaders. Work is divided into five independently testable implementation plans; do not implement a second event system or global refresh layer.

**Tech Stack:** Python, SQLAlchemy, FastAPI, pytest; React, TypeScript, Vite, existing sync hooks and API clients; SQL scripts in `HMS-B/scripts` only if schema changes are justified.

**Spec:** `docs/superpowers/specs/2026-09-25-hms-cross-module-consistency-design.md`

## Global Constraints

- Events invalidate canonical resources; they do not carry authoritative entity copies.
- Publish sync events only after successful commit and preserve rollback behavior.
- Keep all queries, events, subscriptions, and cache keys hospital scoped.
- Never rewrite finalized invoices, receipts, pharmacy sales, released results, administered records, or signed/finalized forms during reconciliation.
- Never replace dirty clinician-authored form data during background refresh.
- Preserve current event names and workflows unless a compatibility-safe alias is required.
- Preserve pre-existing uncommitted edits in both repositories; inspect diffs before editing shared files.
- Do not add polling where event, reconnect, focus, or visibility reconciliation suffices.

---

## Implementation Plans

Execute in order and finish each plan's verification before starting the next. Each plan contains exact module targets, expected focused tests, and its own completion criteria.

1. [Sync contracts and clinical reconciliation](2026-09-25-hms-sync-contracts-clinical.md)
2. [Billing consistency](2026-09-25-hms-billing-consistency.md)
3. [Pharmacy and diagnostics](2026-09-25-hms-pharmacy-diagnostics-sync.md)
4. [Master invalidation and live queues](2026-09-25-hms-master-cache-live-queues.md)
5. [Concurrency and regression verification](2026-09-25-hms-concurrency-regression.md)

Each plan must update an event/consumer matrix with concrete emitter, subscriber, canonical API loader, cache keys invalidated, and relevant tests. Record any item that cannot be completed with the reason and exact affected workflow.

