# HMS Backend Migration Progress

## Project
HMS-B → UltrionTech-Backend-Template

## Migration Objective

Preserve existing HMS functionality and behavior while incrementally restructuring
the backend toward the target UltrionTech architecture.

---

## Current Status

Phase: COMPLETE HMS BACKEND MIGRATION — 100% TARGET-NATIVE  
Status: COMPLETED & FULLY VERIFIED (207/207 Total Tests Passing, 0 Failing)  
Current Slice: All 18 Target Modules fully implemented across `hms_migration/modules/` with 281 / 281 (100.0%) legacy business endpoints migrated; target Base metadata expanded to 60+ independent tables; controlled cutover flags active in `app/config.py` and `app/main.py` for all domains; `hms_migration/app/router.py` completely composes all 18 target routers for standalone operability.  
Overall Progress: 281 / 281 (100.0%) Business Endpoints Migrated | 18 / 18 Target Modules Live | Zero Legacy Runtime Dependencies in Migration Production Code | 207 Automated Tests Passing.  
Last Updated: 2026-09-07  


---

## Rules / Constraints

- Preserve existing behavior.
- Do not guess. Explicitly document `UNKNOWN / REQUIRES VERIFICATION`.
- Follow UltrionTech template architecture.
- Treat template READMEs as engineering specifications.
- Keep changes incremental.
- Verify behavior after changes.
- Respect the 500-line structural guideline.
- No broad refactoring before establishing test baselines.
- Maintain existing API paths, methods, parameters, and response contracts for HMS-frontend.
- Do not apply generic JSON envelope wrappers (`{ "data": ..., "meta": ... }`) to HTML/streaming endpoints.

---

## Repository Baseline

### HMS-B
- **Location**: `e:\Ultrion Tech\HMS-B`
- **Git Branch**: `chandan/hms-backend` (Up to date with `origin/chandan/hms-backend`)
- **Runtime**: Python 3.11.9 (Local virtualenv `.venv`), Python 3.12 target in Azure CI/CD
- **Framework**: FastAPI 0.115.6, Uvicorn 0.34.0, Pydantic 2.12.5, Pydantic-Settings 2.12.0
- **Database**: PostgreSQL on Azure Database for PostgreSQL Flexible Server, SQLAlchemy 2.0.51, psycopg2-binary 2.9.10 (Synchronous engine)
- **Observability**: Azure Monitor OpenTelemetry 1.8.9 (FastAPI, SQLAlchemy, HTTPX)
- **Deployment**: GitHub Actions (`.github/workflows/main_hms-ultrion-dev.yml`) to Azure Web App (`HMS-Ultrion-Dev`)
- **Scale**:
  - Total Python source files under `app/`: 61 files (28,035 lines total)
  - Registered HTTP Routes: 288 endpoints across 19 router prefixes
  - SQLAlchemy ORM Tables: 53 mapped tables in monolithic `app/models.py`
  - Enums: 32 domain Enums in `app/models.py`
  - Automated Tests: 0 test files exist in HMS-B (pytest not in requirements)

### UltrionTech-Backend-Template
- **Location**: `e:\Ultrion Tech\UltrionTech-Backend-Template`
- **Git Branch**: `main`
- **Specifications**: 56 Markdown specifications across root, `.cursor/`, `docs/`, `app/`, `config/`, `modules/`, `platform/`, `shared/`, `infrastructure/`, `integrations/`, `scripts/`, `tests/`
- **Code Placeholders**: Size 0-byte Python template stubs (`app/main.py`, `app/router.py`, `app/lifespan.py`, `config/*.py`, `scripts/*.py`)
- **Target Architecture**:
  - Modular vertical domain slices under `modules/<name>/` with 8 sub-layers: `api/`, `actions/`, `db/`, `entities/`, `contracts/`, `validators/`, `permissions/`, `tests/`
  - Asynchronous I/O (`sqlalchemy[asyncio]`, `asyncpg`, `motor`)
  - Platform capabilities (`tenancy/`, `permissions/`, `pagination/`, `filtering/`, `feature_flags/`, `module_registry/`)
  - Shared generic utilities (`auth/`, `security/`, `logging/`, `tracing/`, `exceptions/`, `audit/`, `storage/`, `middleware/`, `responses/`)
  - Standalone operational CLI scripts (`scripts/migrate.py`, `scripts/seed_data.py`, etc.)

---

## Migration Phases

| Phase | Description | Status | Notes |
|---|---|---|---|
| Phase 0: Repository Audit | Complete architectural and compliance audit | **COMPLETED** | Comprehensive audit report delivered; baseline verified |
| Phase 0: Candidate Selection | Identification and scoring of safest first migration slice | **COMPLETED** | `modules/vitals/` selected as primary candidate |
| Phase 1: Baseline Test Harness | Establishing pytest fixtures and API integration tests | **COMPLETED** | 32 tests passing (100%), 0 production changes, SQLite isolated harness |
| Phase 2: First Slice Migration | Execute vertical migration of selected slice (`vitals`) | **COMPLETED** | Migrated to `hms_migration/modules/vitals/` across 8 sub-layers; 66/66 tests passing |
| Phase 3: Slice Verification | Full regression and contract verification of Slice 1 | **COMPLETED** | 100% route/OpenAPI compatibility; coexistence conflicts identified; Strategy B recommended |
| Phase 4: Foundational Core | Config, infrastructure database session, shared layer, and controlled cutover | **COMPLETED** | `config/`, `infrastructure/postgres/`, `shared/`, `modules/vitals` decoupled; 78/78 tests passing |
| Phase 5: Subsequent Module Slices | Incremental vertical migration of Analytics (`modules/analytics/`) | **COMPLETED** | Decoupled Base, AuditLog, VitalReading; 94/94 tests passing; PostgreSQL verified |
| Phase 6: Core Patient Domain | Migration of Core Patient registration, directory, profile & Vitals decoupling | **COMPLETED** | `modules/patients` migrated; Vitals decoupled from `app.models.Patient`; 113/113 tests passing |
| Phase 7: Appointments Domain Migration | 20 endpoints across 8 template layers, lifecycle service, missed appointments background worker, cross-module decoupling (Vitals & Patients) | **COMPLETED** | 20 endpoints migrated; 137/137 tests passing; Vitals & Patients decoupled from legacy appointments; controlled cutover (`USE_MIGRATED_APPOINTMENTS`) |
| Phase 8: Clinical & Bed Management Workflows | Doctors / Clinical Records / Bed Management / Inpatient Admission Migration | **COMPLETED** | 48 endpoints migrated across 4 target modules; 18 target entities; 154/154 tests passing; controlled cutovers (`USE_MIGRATED_DOCTORS`, `USE_MIGRATED_BEDS`, `USE_MIGRATED_INPATIENT`) |
| Phase 9: Clinical Diagnostics & Facilities | Billing (21), Laboratory (24), DMS (9), Radiology (16), OT (14), Equipment (23), Pharmacy (27) | **COMPLETED** | 134 endpoints migrated; 24 new target entities; 0 legacy util dependencies; full cutover flags |
| Phase 10: MIS Management Reporting | MIS Analytics & Executive Reports (7 endpoints) | **COMPLETED** | 7 endpoints migrated; 6 dedicated integration tests passing; controlled cutover (`USE_MIGRATED_MIS`) |
| Phase 11: Enterprise Masters | Hospital organizational masters, wards, rooms, wings, departments, suppliers, shift types (40 endpoints) | **COMPLETED** | 40 endpoints migrated; 6 dedicated integration tests passing; controlled cutover (`USE_MIGRATED_MASTERS`) |
| Phase 12: Admin & Tenancy Domain | System administration, roles, staff users, shift rosters, audit logs, and hospital tenancy (21 endpoints) | **COMPLETED** | 21 endpoints migrated (15 Admin + 6 Tenancy); 8 dedicated integration tests passing; controlled cutovers (`USE_MIGRATED_ADMIN`, `USE_MIGRATED_HOSPITALS`); 281/281 total endpoints complete (100.0%) |

---

## Backend Inventory

### 500-Line Structural Guideline Breakdown

#### A. Very Large Files (> 1,000 lines) — 9 files (14,636 lines total)
- `app/models.py` (2,240 lines, 98 classes): Monolithic ORM mapping 53 tables and 32 Enums.
- `app/routers/doctors.py` (2,078 lines, 47 functions): Doctor appointments, prescriptions, HTML PDF export, leaves, medical records.
- `app/routers/hospitals.py` (1,774 lines, 24 functions): Hospital CRUD + 7-role dashboard aggregations (Doctor, Nurse, Reception, Lab, Radiology, OT, Billing).
- `app/routers/pharmacy.py` (1,576 lines, 35 functions): POS sales, medicine catalog, purchases, stock adjustments, returns.
- `app/main.py` (1,329 lines, 28 functions): Application boot, 23 ad-hoc SQL DDL migration functions, background loop, router mounts.
- `app/routers/laboratory.py` (1,322 lines, 37 functions): Lab tests, panels, orders, sample collection, results, HTML report streaming.
- `app/routers/appointment.py` (1,285 lines, 39 functions): OPD scheduling, doctor availability, queue tokens, check-in, completion blockers.
- `app/routers/dms.py` (1,064 lines, 25 functions): Patient documents, unified timeline, dossier streaming, base64 file handling.
- `app/routers/equipment.py` (1,038 lines, 30 functions): Equipment items, assignments, maintenance, service logs, requests.

#### B. Large Files (500 – 1,000 lines) — 9 files (6,998 lines total)
- `app/routers/masters.py` (948 lines, 47 functions): 10 metadata entities (wings, departments, shifts, holidays, wards, rooms, etc.).
- `app/routers/admin.py` (905 lines, 31 functions): Staff roles, permissions, user management, shift rosters, audit log queries.
- `app/routers/billing.py` (898 lines, 29 functions): Ledger charges, payments, invoice/receipt numbering, HTML print/PDF export.
- `app/routers/ot.py` (816 lines, 25 functions): Surgery scheduling, surgical checklist notes, OT calendar, file streaming.
- `app/routers/radiology.py` (737 lines, 23 functions): Scan catalog, orders, scheduling, reporting, image streaming.
- `app/routers/beds.py` (704 lines, 21 functions): Bed occupancy, room allocation, bed transfers, discharge clearance checks.
- `app/routers/mis.py` (684 lines, 16 functions): Executive reports (doctor, patient, bed, daily summary).
- `app/routers/registration.py` (673 lines, 16 functions): Patient registration, UHID assignment, direct admission.
- `app/schemas_pharmacy.py` (533 lines, 43 classes): Pydantic contracts for pharmacy inventory, sales, purchases.

#### C. Medium Files (200 – 499 lines) — 15 files (4,151 lines total)
- `app/utils/invoices.py` (472 lines, 13 functions): Numbering sequences, HTML templates for invoices/receipts.
- `app/schemas_masters.py` (376 lines, 30 classes): Pydantic contracts for 10 master entities.
- `app/utils/billing.py` (364 lines, 14 functions): Ledger math, charge allocation, net calculations.
- `app/routers/ipd.py` (351 lines, 14 functions): Inpatient forms submissions, consent forms, discharge summary view.
- `app/routers/vitals.py` (348 lines, 10 functions): Daily vitals recording, queue tokens, today vitals view.
- `app/schemas_doctors.py` (300 lines, 22 classes): Contracts for doctor prescriptions, leaves, records.
- `app/schemas_laboratory.py` (275 lines, 20 classes): Contracts for lab tests, panels, orders, results.
- `app/utils/appointment_lifecycle.py` (272 lines, 10 functions): Auto-cancel loop logic, completion blockers.
- `app/schemas_registration.py` (253 lines, 11 classes): Contracts for patient registration and UHID.
- `app/schemas_admin.py` (238 lines, 17 classes): Contracts for roles, permissions, users, rosters.
- `app/schemas_equipment.py` (229 lines, 18 classes): Contracts for equipment items, maintenance, requests.
- `app/utils/pharmacy_stock.py` (222 lines, 6 functions): FIFO batch stock deduction and restore logic.
- `app/schemas_billing.py` (210 lines, 14 classes): Contracts for charges, payments, invoices, receipts.
- `app/utils/lab_panels.py` (206 lines, 3 functions): Panel test member lookup and pricing math.
- `app/utils/lab_prescription_requests.py` (201 lines, 8 functions): Doctor prescription lab fulfillment helpers.

#### D. Small Files (50 – 199 lines) — 18 files (2,002 lines total)
- `app/utils/doctor_leave.py` (188 lines): Leave conflict checks against doctor schedules.
- `app/telemetry.py` (186 lines): Azure Application Insights OpenTelemetry setup.
- `app/utils/medical_record_sync.py` (168 lines): Sync finalized lab/rad orders into doctor clinical records.
- `app/schemas_dms.py` (163 lines): Contracts for document metadata and timeline events.
- `app/schemas.py` (159 lines): Auth tokens and login request/response contracts.
- `app/schemas_appointment.py` (159 lines): Contracts for appointment booking, availability, queue.
- `app/routers/analytics.py` (146 lines, 4 functions): Platform-level super-admin growth and usage analytics.
- `app/schemas_ot.py` (141 lines): Contracts for surgeries and OT calendar.
- `app/schemas_beds.py` (128 lines): Contracts for bed allocation and occupancy.
- `app/schemas_radiology.py` (125 lines): Contracts for radiology scans and orders.
- `app/routers/auth.py` (124 lines, 1 function): Login endpoint for super admin, hospital admin, staff.
- `app/utils/admissions.py` (106 lines): Inpatient admission discharge helpers.
- `app/utils/catalogue_templates.py` (99 lines): Standard catalog seed templates for lab & radiology.
- `app/utils/auth.py` (90 lines): JWT token encode/decode, role dependencies.
- `app/schemas_mis.py` (73 lines): Contracts for MIS reports.
- `app/schemas_vitals.py` (55 lines, 5 classes): Contracts for vital signs recording.
- `app/utils/encounter_ids.py` (53 lines): Clinical encounter ID formatters.
- `app/config.py` (50 lines): BaseSettings loading from `.env`.

#### E. Very Small Files (< 50 lines) — 12 files (284 lines total)
- `app/schemas_ipd.py` (48 lines): Inpatient form contracts.
- `app/schemas_analytics.py` (37 lines): Analytics response contracts.
- `app/utils/audit.py` (34 lines): Audit log append function.
- `app/utils/phone.py` (34 lines): Indian phone regex validation.
- `scripts/init_db.py` (22 lines): Database creation script.
- `app/database.py` (21 lines): SQLAlchemy engine & session factory.
- `run.py` (20 lines): Uvicorn runner.
- `app/utils/password.py` (18 lines): Bcrypt password hashing.
- `app/utils/hospital_id.py` (17 lines): Hospital ID sanitizer.
- `app/__init__.py`, `app/routers/__init__.py`, `app/utils/__init__.py` (1 line each).

---

## Feature / Module Inventory

| Module | Feature Area | Primary Source Files | Size (Lines) | Complexity | Coupling Level | Overall Risk | Template Target Location | First-Migration Candidate? |
|---|---|---|---|---|---|---|---|---|
| **Vitals** | Patient vital signs, today's bookings queue | `routers/vitals.py`, `schemas_vitals.py`, `VitalReading` in `models.py` | 443 | Low-Medium | Low-Medium (Appt status) | **LOW** | `modules/vitals/` | **YES (Top Candidate)** |
| **Analytics** | Platform super-admin statistics | `routers/analytics.py`, `schemas_analytics.py` | 183 | Low | Low (Read-only on 4 tables)| **VERY LOW**| `modules/analytics/` | **YES (Conservative Alternative)** |
| **Masters (Holidays)** | Hospital holiday schedule | `routers/masters.py` (lines 301-343), `schemas_masters.py`, `Holiday` in `models.py` | 79 | Very Low | Very Low (Isolated table) | **VERY LOW**| `modules/masters/holidays/` | **YES (Minimalist Sub-Slice)** |
| **Auth** | Login & JWT issuance | `routers/auth.py`, `schemas.py`, `utils/auth.py`, `utils/password.py` | 391 | Medium | Medium (All roles, audit) | **MEDIUM** | `shared/auth/` + `modules/auth/` | Secondary (Affects all auth) |
| **IPD Forms** | Inpatient form submissions | `routers/ipd.py`, `schemas_ipd.py`, `IpdFormSubmission` in `models.py` | 444 | Medium | Medium (Admissions, Patient) | **MEDIUM** | `modules/ipd/` | Secondary |
| **Beds** | Bed occupancy & transfers | `routers/beds.py`, `schemas_beds.py`, `utils/admissions.py` | 938 | Medium-High | High (Billing discharge block) | **HIGH** | `modules/beds/` | No (Defer) |
| **Equipment** | Medical device lifecycle | `routers/equipment.py`, `schemas_equipment.py` | 1,267 | Medium | Low-Medium (Staff requests) | **MEDIUM** | `modules/equipment/` | No (Too large for Slice 1) |
| **DMS** | Document storage & timeline | `routers/dms.py`, `schemas_dms.py` | 1,227 | Medium-High | High (Base64 data URLs) | **HIGH** | `modules/dms/` | No (Defer until storage decided) |
| **Registration**| Patient registration & UHID | `routers/registration.py`, `schemas_registration.py` | 926 | Medium-High | High (Root of all patients) | **HIGH** | `modules/registration/` | No (Defer) |
| **Radiology** | Scans, scheduling, reports | `routers/radiology.py`, `schemas_radiology.py` | 862 | High | High (Medical record sync, HTML) | **HIGH** | `modules/radiology/` | No (Defer) |
| **OT** | Surgeries & OT checklist | `routers/ot.py`, `schemas_ot.py` | 957 | High | High (Doctor, Patient, Rooms) | **HIGH** | `modules/ot/` | No (Defer) |
| **MIS** | Executive operational reports | `routers/mis.py`, `schemas_mis.py` | 757 | Medium | High (Cross-module queries) | **MEDIUM** | `modules/mis/` | No (Defer) |
| **Masters (All)**| 10 hospital metadata entities | `routers/masters.py`, `schemas_masters.py` | 1,324 | Medium | Medium (FK root for many) | **HIGH** | `modules/masters/` | No (Too large for Slice 1) |
| **Laboratory** | Tests, orders, results | `routers/laboratory.py`, `schemas_laboratory.py`, utils (3) | 2,004 | Very High | Very High (Billing, sync, HTML) | **CRITICAL** | `modules/laboratory/` | No (Defer) |
| **Doctors** | Prescriptions, leaves, records | `routers/doctors.py`, `schemas_doctors.py`, utils (2) | 2,566 | Very High | Very High (Lab/Rad orders, PDF) | **CRITICAL** | `modules/doctors/` | No (Defer) |
| **Billing** | Financial ledger, invoices | `routers/billing.py`, `schemas_billing.py`, utils (2) | 1,944 | Very High | Critical (Core financial hub) | **CRITICAL** | `modules/billing/` | No (Defer) |
| **Appointments**| OPD scheduling, queue, cancel| `routers/appointment.py`, `schemas_appointment.py`, utils | 1,716 | Very High | Critical (Billing, Lab blockers)| **CRITICAL** | `modules/appointments/` | No (Defer) |
| **Pharmacy** | POS, inventory, FIFO batches | `routers/pharmacy.py`, `schemas_pharmacy.py`, utils | 2,331 | Very High | Critical (Financial & stock) | **CRITICAL** | `modules/pharmacy/` | No (Defer) |
| **Hospitals** | Tenancy CRUD & 7 Dashboards | `routers/hospitals.py` | 1,774 | Very High | High (Aggregates 7 modules) | **HIGH** | `modules/hospitals/` | No (Defer) |

---

## Migration Candidates

| Candidate | Total Lines | Complexity | Coupling Level | DB Risk | API Risk | Testability | Template Fit | Overall Risk | Recommendation |
|---|---|---|---|---|---|---|---|---|---|
| **Candidate A: `modules/vitals/`** | 443 | Low-Medium | Low-Medium | Low | Low | Very High | **EXCELLENT** | **LOW** | **RECOMMENDED (Primary)** |
| **Candidate B: `modules/analytics/`** | 183 | Low | Low (Read-only) | Zero | Very Low | Very High | Good | **VERY LOW** | **RECOMMENDED (Alternative)** |
| **Candidate C: `masters/holidays`** | 79 | Very Low | Very Low | Very Low | Very Low | Very High | Moderate (Sub-slice) | **VERY LOW** | Feasible Minimalist Slice |
| **Candidate D: `shared/security/`** | 18 | Very Low | Very Low | Zero | Zero | High | Partial (No module) | **VERY LOW** | Supporting Slice Only |
| **Candidate E: `modules/ipd/`** | 444 | Medium | Medium | Medium | Medium (HTML) | Medium | Good | **MEDIUM** | Defer to Phase 5 |

---

## Selected Migration Slice

### Primary Recommendation: `modules/vitals/`

#### Why this slice was selected
1. **Structural Fit (< 500 lines)**: Router (348 lines) + Schemas (55 lines) + Entity (40 lines) = 443 lines total. It respects the 500-line guideline naturally without artificial code truncation.
2. **Complete Vertical Representation**: It represents a genuine clinical capability (vital signs measurement for hospital outpatients) and maps cleanly to all 8 layers of the target template:
   - `modules/vitals/api/vitals_api.py` (Route endpoints)
   - `modules/vitals/actions/record_vitals_action.py`, `get_today_vitals_action.py`, `update_vital_action.py`, `delete_vital_action.py` (Business logic)
   - `modules/vitals/db/vitals_repository.py` (Data access)
   - `modules/vitals/entities/vital_reading.py` (ORM entity)
   - `modules/vitals/contracts/vitals_contracts.py` (Pydantic schemas)
   - `modules/vitals/validators/vitals_validator.py` (Clinical range validation)
   - `modules/vitals/permissions/vitals_permissions.py` (Staff permission policies)
   - `modules/vitals/tests/test_vitals.py` (Unit and API integration tests)
3. **Low Architectural Blast Radius**: Only 1 dedicated database table (`vital_readings`). No foreign keys point *into* `vital_readings` from other tables.
4. **Preserves Observable Frontend Behavior**:
   - `GET /api/vitals/today`
   - `GET /api/vitals`
   - `POST /api/vitals`
   - `PUT /api/vitals/{vital_id}`
   - `DELETE /api/vitals/{vital_id}`
   All 5 endpoints preserve exact request/response schemas, status codes, and query parameters.
5. **Manages One Controlled Cross-Module Interaction**: When vitals are created, it marks `AppointmentStatus.scheduled` → `waiting` (checked-in) and assigns a queue token. This provides the exact right level of complexity to validate how cross-module actions should be orchestrated in the UltrionTech architecture without touching financial or inventory data.

#### Conservative Alternative: `modules/analytics/`
If the project owner prefers an ultra-conservative first slice with zero database writes, `modules/analytics/` (146 lines, 1 endpoint, read-only aggregation) can be executed first as a zero-risk proof of concept.

### Migration Boundary for `modules/vitals/`

#### In Scope
- Create `modules/vitals/` directory tree matching template.
- Move schemas from `app/schemas_vitals.py` to `modules/vitals/contracts/vitals_contracts.py`.
- Move `VitalReading` model from `app/models.py` to `modules/vitals/entities/vital_reading.py` (maintaining declarative base registration).
- Extract data access from `app/routers/vitals.py` into `modules/vitals/db/vitals_repository.py`.
- Extract business logic (recording vitals, queue token generation, appointment check-in transition) into `modules/vitals/actions/`.
- Create thin API handlers in `modules/vitals/api/vitals_api.py`.
- Create `modules/vitals/tests/test_vitals.py`.
- Mount router via `app/router.py` (or route inclusion bridge).

#### Out of Scope
- Rewriting global database engine from sync to async (remains on synchronous session during Slice 1).
- Modifying physical PostgreSQL schema or table names (`vital_readings` stays unchanged).
- Altering HMS-frontend API client (`api.ts`).
- Moving or altering unrelated router files (`billing.py`, `pharmacy.py`, etc.).

---

## Migration History

### Slice 0: Architectural Audit & Baseline Specification
- **Date**: 2026-09-06
- **Feature**: Full Repository Audit & Migration Readiness Assessment
- **Files changed**:
  - `MIGRATION_PROGRESS.md` (Created)
- **Architecture change**: None (Audit & Planning only)
- **Tests**: Importability check (`app.main`), route registration accounting (288 routes confirmed)
- **Verification**: Verified exit code 0 on Python 3.11 import; verified zero syntax errors across 61 files
- **Result**: Baseline established; Slice 1 candidate identified
- **Issues**: Zero existing automated tests in HMS-B; 18 files violate 500-line guideline; 23 DDL migrations run on boot

### Phase 1: Vitals Behavioral & API Contract Baseline Suite
- **Date**: 2026-09-07
- **Feature**: Establishing Automated Behavioral Baseline for Existing Unmodified Vitals Implementation
- **Files changed**:
  - `requirements.txt` (Added test dependencies: pytest==9.1.1, httpx==0.28.1)
  - `pytest.ini` (Created test configuration)
  - `tests/__init__.py` (Created tests package)
  - `tests/conftest.py` (Created isolated SQLite test harness, DB session fixtures, and auth fixtures)
  - `tests/integration/__init__.py` (Created integration package)
  - `tests/integration/test_vitals_baseline.py` (Created 32 comprehensive baseline tests)
  - `MIGRATION_PROGRESS.md` (Updated with Phase 1 results)
- **Existing Production Code**:
  - `app/routers/vitals.py`: **UNCHANGED**
  - `app/schemas_vitals.py`: **UNCHANGED**
  - `app/models.py`: **UNCHANGED**
  - `scripts/`: **UNCHANGED**
  - `git diff app/`: **0 lines modified (Clean)**
- **Architecture change**: None (`hms_migration/` NOT created during Phase 1; reference code frozen)
- **Database Isolation Guarantee**:
  - `app.database.engine.connect` defensively monkeypatched to raise `RuntimeError` immediately if accessed.
  - Tests run on isolated in-memory SQLite (`sqlite:///:memory:`) using `StaticPool`.
  - `@compiles(JSONB, "sqlite")` hook enables SQLite compatibility with PostgreSQL JSONB types.
  - Application lifespan bypassed in tests to prevent executing startup DDL migrations or background loops against Azure PostgreSQL.
- **Tests**: 32 tests implemented, 32 passed, 0 failed, 0 errors (execution time: 0.99s)
- **Coverage**:
  - Authentication (HTTPBearer 403 on missing auth, 401 on invalid/expired token, 403 on super_admin, 401 on missing hospital_uuid)
  - Cross-hospital multi-tenant isolation across all 5 endpoints
  - `GET /api/vitals/today`: Empty result, chronological ordering, cancelled exclusion, automatic reversion of `waiting` without vitals to `scheduled`
  - `GET /api/vitals`: Query parameter validation (400 if missing both), filtering by `appointment_id` or `patient_id`
  - `POST /api/vitals`: Batch creation, appointment validation (404 not found, 400 on completed/cancelled/no-show/transferred), whitespace validation, appointment status transition `scheduled` -> `waiting`, `checked_in_at` assignment, sequential `queue_token` allocation, `AuditLog` row generation
  - `PUT /api/vitals/{id}`: Not found (404), completed appointment guard (400), whitespace validation (400), update persistence, `AuditLog` generation
  - `DELETE /api/vitals/{id}`: Not found (404), completed appointment guard (400), row deletion, `AuditLog` generation
- **Result**: Complete, trustworthy behavioral contract established and verified. Phase 1 complete.

### Phase 2: First Slice Migration (`hms_migration/modules/vitals/`)
- **Date**: 2026-09-07
- **Feature**: Vertical Slice Migration of Vitals Module into `hms_migration/modules/vitals/`
- **Files created** (24 files, all under 500 lines):
  - `hms_migration/__init__.py`
  - `hms_migration/modules/__init__.py`
  - `hms_migration/modules/vitals/__init__.py`
  - `hms_migration/modules/vitals/README.md`
  - `hms_migration/modules/vitals/contracts/__init__.py`, `vitals_contracts.py` (Pydantic contracts: `VitalItemCreate`, `VitalBatchCreate`, `VitalItemUpdate`, `VitalReadingResponse`, `VitalsTodayItem`)
  - `hms_migration/modules/vitals/entities/__init__.py`, `vital_reading.py` (Transitional domain entity bridge to `app.models.VitalReading`)
  - `hms_migration/modules/vitals/validators/__init__.py`, `vitals_validator.py` (Pure validation: query params, string stripping, visit mutation state guards)
  - `hms_migration/modules/vitals/permissions/__init__.py`, `vitals_permissions.py` (Module authorization policy: staff/admin context, hospital UUID extraction, super_admin 403 restriction)
  - `hms_migration/modules/vitals/db/__init__.py`, `vitals_repository.py` (Data access repository: query construction, eager loading, tenant filtering, CRUD persistence)
  - `hms_migration/modules/vitals/actions/__init__.py`, `get_today_vitals_action.py`, `list_vitals_action.py`, `create_vitals_action.py`, `update_vital_action.py`, `delete_vital_action.py` (Business workflows, state transition `scheduled` -> `waiting`, queue token allocation, audit logging)
  - `hms_migration/modules/vitals/api/__init__.py`, `vitals_api.py` (Thin HTTP routing handlers)
  - `hms_migration/modules/vitals/tests/__init__.py`, `conftest.py`, `test_vitals.py` (Module unit tests and router integration tests)
- **Files modified** (1 file):
  - `pytest.ini` (`testpaths = tests hms_migration`)
- **Existing Production Code**:
  - `app/routers/vitals.py`: **UNCHANGED**
  - `app/schemas_vitals.py`: **UNCHANGED**
  - `app/models.py`: **UNCHANGED**
  - `scripts/`: **UNCHANGED**
  - `git diff app/`: **0 lines modified (Clean)**
  - `git diff scripts/`: **0 lines modified (Clean)**
- **Architecture change**:
  - Implemented the complete 8-layer vertical architecture required by `UltrionTech-Backend-Template`.
  - Avoided duplicate SQLAlchemy table/mapper collisions by referencing the canonical `VitalReading` model from `app.models` inside `entities/vital_reading.py` as an explicit transitional bridge.
  - Kept repository layer strictly dedicated to data access; business decisions (auto-reversion of orphaned waiting visits, status check-in transition, audit side-effects) reside in actions.
- **Tests**:
  - Legacy baseline suite (`test_vitals_baseline.py`): 32/32 passed (100%)
  - Migrated module suite (`test_vitals.py`): 34/34 passed (100%)
  - Combined suite: 66/66 passed in 1.71s
- **Tested Equivalence vs. Operational Boundaries**:
  - *Tested Equivalence Verified*: Identical API routes, methods, parameters, status codes (200, 201, 204, 400, 401, 403, 404), validation details, multi-tenancy isolation, appointment lifecycle transitions (`scheduled` → `waiting`), sequential queue tokens, and audit log generation under the isolated SQLite test harness.
- **Result**: Vertical slice migration of Vitals completed with zero production regressions.

### Phase 3: Vitals Router Integration Strategy & Verification
- **Date**: 2026-09-07
- **Feature**: Router Integration Strategy, Route Compatibility Audit & Coexistence Analysis
- **Production Code Status**:
  - `app/main.py`: **UNCHANGED** (Production routing preserved)
  - `app/routers/vitals.py`: **UNCHANGED** (Legacy implementation preserved)
  - `app/schemas_vitals.py`: **UNCHANGED**
  - `app/models.py`: **UNCHANGED**
  - `git diff app/`: **0 lines modified (Clean)**
  - `git diff scripts/`: **0 lines modified (Clean)**
- **Audit Findings**:
  - *Current Registration*: Registered in `app/main.py` line 16 (`from app.routers import ..., vitals`) and mounted at line 1318 via `app.include_router(vitals.router, prefix="/api")`. Effective path prefix `/api/vitals`, tags `["vitals"]`.
  - *Route Equivalence*: 100% path, method, status code, query parameter, request body, and response model parity across all 5 endpoints (`GET /api/vitals/today`, `GET /api/vitals`, `POST /api/vitals`, `PUT /api/vitals/{id}`, `DELETE /api/vitals/{id}`).
  - *OpenAPI Compatibility*: Exact match across paths, tags, operation IDs (`list_today_bookings_api_vitals_today_get`, `list_vitals_api_vitals_get`, `create_vitals_api_vitals_post`, `update_vital_api_vitals__vital_id__put`, `delete_vital_api_vitals__vital_id__delete`), security (`HTTPBearer`), and schemas. Only difference is enhanced, non-breaking docstrings in migrated handlers.
  - *Coexistence Limitation*: Both routers CANNOT safely coexist under `/api`. Starlette route matching is first-match-wins (second router is 100% shadowed), and FastAPI generates 5 `Duplicate Operation ID` warnings with OpenAPI schema overwrites.
  - *Parallel Path Infeasibility*: Mounting under `/api/v2/vitals` or `/api/migration/vitals` is rejected because `HMS-frontend` (`src/lib/api.ts`) hardcodes `/api/vitals`.
  - *Cross-Module Dependency Discovery*: `app/routers/doctors.py` imports `_reading_response` and `_revert_checked_in_without_vitals` directly from `app.routers.vitals`; `app/schemas_doctors.py` imports `VitalReadingResponse` from `app.schemas_vitals`. Therefore, `app/routers/vitals.py` and `app/schemas_vitals.py` must NOT be deleted or emptied during cutover.
  - *Startup / Lifespan Safety*: Verified empirically that importing and mounting `hms_migration.modules.vitals.api.vitals_api` incurs zero database connections, zero queries, and zero startup side-effects. None of the 23 DDL migrations in `app.main:lifespan` touch `vital_readings`.
- **Selected Integration Strategy**:
  - **Strategy B (Configuration-Based Router Selection via `Settings.use_migrated_vitals`)**: Evaluated as the safest integration mechanism. Allows controlled toggling between legacy and migrated router via Azure Web App Application Settings / environment variable `USE_MIGRATED_VITALS=true` without code redeployment, providing an instant (<30s) zero-downtime rollback mechanism.
  - **Strategy A (Direct Replacement in `main.py`)**: Maintained as clean secondary alternative if single-commit git-revert rollout is preferred.
  - **Strategies C (Dynamic Feature Flag Platform) and D (Parallel Routes)**: Formally rejected (no feature flag infrastructure exists in HMS-B; frontend client cannot consume alternate paths).
- **Tests**:
  - Full suite verified: 71 passed, 0 failed, 0 errors, 0 skipped (32 legacy baseline + 39 migrated module tests).
- **Result**: Phase 3 strategy verified with zero production regressions and no premature cutover.

### Phase 4: Foundational Architecture & Controlled Vitals Cutover
- **Date**: 2026-09-07
- **Feature**: Establishing Foundational Target Architecture (`shared/`, `config/`, `infrastructure/`, `app/`), Decoupling Cross-Domain Dependencies, and Executing Controlled Vitals Runtime Cutover
- **Foundational Architecture Introduced**:
  - `hms_migration/config/settings.py` (BaseSettings supporting all existing environment variables + `USE_MIGRATED_VITALS` toggle + `asyncpg_database_url`)
  - `hms_migration/shared/exceptions/` (`base.py`, `handlers.py`, `__init__.py` — base domain exceptions: `AppError`, `NotFoundError`, `ValidationError`, `ConflictError`, `UnauthorizedError`, `ForbiddenError`; global exception handlers mapping domain errors to exact OG HTTP status codes and `{"detail": ...}` payload)
  - `hms_migration/shared/auth/` (`jwt.py`, `security.py`, `dependencies.py`, `__init__.py` — token creation/decoding, password hashing, and user/hospital context dependencies matching OG claims)
  - `hms_migration/shared/audit/` (`service.py`, `__init__.py` — `AuditService` and `write_audit_log` supporting both synchronous and asynchronous sessions with exact OG `AuditLog` fields)
  - `hms_migration/infrastructure/postgres/` (`engine.py`, `session.py`, `base_repository.py`, `__init__.py` — `create_async_engine` using `asyncpg`, `get_async_session` dependency, `BaseRepository[T]`, and explicitly marked transitional synchronous session bridge `get_transitional_sync_session`)
  - `hms_migration/app/` (`router.py`, `lifespan.py`, `main.py` — root router composing module routers, async engine disposal lifespan, and `create_app` factory)
- **Vitals Module Integration & Cross-Domain Decoupling**:
  - `hms_migration/modules/vitals/exceptions/` (`vitals_exceptions.py` — `VitalAppointmentNotFoundError`, `VitalReadingNotFoundError`, `VitalMutationNotAllowedError`, `VitalValidationError`)
  - Decoupled `fastapi.HTTPException` completely from `vitals_validator.py`, `create_vitals_action.py`, `update_vital_action.py`, `delete_vital_action.py` (zero HTTPException leakage)
  - Decoupled cross-domain Appointment data access into `hms_migration/modules/vitals/db/appointment_reader.py` (`AppointmentReader`), isolating appointment lookups, status transitions, and queue-token calculation
  - `VitalsRepository` refactored to inherit `BaseRepository[VitalReading]` with pure vital readings data access
  - Replaced legacy dependencies in Vitals: `app.utils.auth` replaced with `hms_migration.shared.auth`, `app.utils.audit` replaced with `hms_migration.shared.audit`, and `app.database.get_db` replaced with `hms_migration.infrastructure.postgres.session.get_transitional_sync_session`
- **Controlled Vitals Runtime Cutover in `app/main.py`**:
  - Updated `app/config.py` and `app/main.py` with `USE_MIGRATED_VITALS` toggle (default: `False`)
  - When `USE_MIGRATED_VITALS=false`: mounts legacy `app.routers.vitals.router` (production baseline preserved)
  - When `USE_MIGRATED_VITALS=true`: mounts migrated `hms_migration.modules.vitals.api.vitals_api.router`
  - Exactly ONE router mounted at `/api/vitals` per process; zero route shadowing; zero duplicate operation IDs; 288 total routes preserved
  - Registered shared exception handlers on `app.main:app`
- **Live PostgreSQL Verification** (Target: Azure PostgreSQL `HMSstage2`):
  - Connection & Async Engine: **PASS** (PostgreSQL 17.11 on x86_64-pc-linux-gnu via asyncpg)
  - Async Session Execution: **PASS** (63 public tables discovered)
  - Schema & Model Metadata: **PASS** (All 10 `vital_readings` physical columns verified against model)
  - Read Queries: **PASS** (Live samples of vitals and appointments read asynchronously)
  - Writes & Rollback: **NOT EXECUTED** (Staging database protected; safe isolated test database unavailable)
- **Tests**:
  - Full suite verified: **78 passed, 0 failed, 0 errors** in 3.33s (32 baseline + 39 migrated module + 7 runtime cutover integration tests)
- **Result**: Phase 4 foundational core, cross-domain decoupling, and runtime cutover verified with zero regressions.

---

## Verification Log

| Date | Slice | Verification Check | Expected Result | Actual Result | Status | Notes |
|---|---|---|---|---|---|---|
| 2026-09-06 | Slice 0 | Python AST Inventory | Parse all 61 files | 61 files parsed, 28,035 lines | **PASS** | 18 files exceed 500 lines |
| 2026-09-06 | Slice 0 | `app.main` Import Check | Exit code 0 | Exit code 0, telemetry bypassed | **PASS** | No syntax/import regressions |
| 2026-09-06 | Slice 0 | Route Registry Scan | Complete endpoint count | Exactly 288 routes registered | **PASS** | 19 tags + system routes |
| 2026-09-06 | Slice 0 | Test Discovery Scan | Discover test suites | 0 tests outside of `.venv` | **CONFIRMED** | Test harness must be created |
| 2026-09-07 | Phase 1 | Azure DB Isolation Check | Block Azure connection attempt | `app.database.engine.connect()` blocked, raises RuntimeError | **PASS** | Test runner isolated in SQLite |
| 2026-09-07 | Phase 1 | Vitals Auth & Isolation | 403 on unauth, 401 on bad token, tenant isolation | 7 auth/isolation tests passing | **PASS** | Multi-tenant isolation verified |
| 2026-09-07 | Phase 1 | GET /api/vitals/today | Chronological ordering, cancel exclusion, revert waiting | 5 tests passing | **PASS** | Auto-reversion behavior locked |
| 2026-09-07 | Phase 1 | GET /api/vitals | Query param validation, appointment & patient filter | 4 tests passing | **PASS** | Filter contract locked |
| 2026-09-07 | Phase 1 | POST /api/vitals | Batch create, status transition, queue token, audit log | 9 tests passing | **PASS** | Full side-effects contract locked |
| 2026-09-07 | Phase 1 | PUT /api/vitals/{id} | Record update, completed appt guard, validation, audit | 4 tests passing | **PASS** | Update contract locked |
| 2026-09-07 | Phase 1 | DELETE /api/vitals/{id} | Record deletion, completed guard, audit log | 3 tests passing | **PASS** | Delete contract locked |
| 2026-09-07 | Phase 1 | Full Suite Execution | 32 automated baseline tests | 32 passed, 0 failed, 0 errors in 0.99s | **PASS** | Complete behavioral baseline |
| 2026-09-07 | Phase 2 | Migrated Module Tests | Scoped unit & integration tests | 34 passed, 0 failed, 0 errors in 1.03s | **PASS** | Tests validators, actions, and router |
| 2026-09-07 | Phase 2 | Legacy Baseline Regression | 32 Phase 1 baseline tests | 32 passed, 0 failed, 0 errors in 1.02s | **PASS** | Zero regressions on legacy endpoints |
| 2026-09-07 | Phase 2 | Combined Workspace Suite | All tests (`pytest -v`) | 66 passed, 0 failed, 0 errors in 1.71s | **PASS** | 100% test pass rate across all suites |
| 2026-09-07 | Phase 2 | Production Code Invariance | Check `git diff app/ scripts/` | 0 lines modified in `app/` and `scripts/` | **PASS** | Legacy reference code 100% preserved |
| 2026-09-07 | Phase 2 | 500-Line Structural Check | All files < 500 lines | Max file: 473 lines (`test_vitals.py`) | **PASS** | Strict compliance with template rule |
| 2026-09-07 | Phase 2 Hardening | Ledger Audit | Check for duplicate ledgers | Root duplicate `E:\Ultrion Tech\MIGRATION_PROGRESS.md` deleted; single canonical ledger verified | **PASS** | `HMS-B/MIGRATION_PROGRESS.md` canonical |
| 2026-09-07 | Phase 2 Hardening | Test Suite Hardening | Add endpoint terminal status & whitespace rejection tests | 39 migrated tests passing; 71 total tests in suite passing in 2.00s | **PASS** | High-fidelity behavioral protection |
| 2026-09-07 | Phase 3 | OpenAPI Schema AST Diff | Exact match on paths, opIds, schemas, security | Exact 100% match across 5 endpoints & 7 schemas | **PASS** | Only docstring descriptions differ |
| 2026-09-07 | Phase 3 | Router Coexistence Conflict | Test simultaneous mounting under /api | Duplicate OpID warnings & route shadowing confirmed | **CONFIRMED** | Simultaneous mount rejected |
| 2026-09-07 | Phase 3 | Router Startup Side Effects | Import and mount migrated router without lifespan | Zero DB calls, zero Azure network calls | **PASS** | Completely passive on import/mount |
| 2026-09-07 | Phase 3 | Cross-Module Coupling Scan | Scan app/ for imports of app.routers.vitals | `doctors.py` imports 2 internal vitals functions | **IDENTIFIED** | Legacy file must be preserved |
| 2026-09-07 | Phase 3 | Frontend Path Binding Scan | Scan HMS-frontend for /vitals endpoints | `src/lib/api.ts` hardcoded to `/api/vitals/...` | **CONFIRMED** | Strategy D (parallel path) rejected |
| 2026-09-07 | Phase 3 | Config Selection Simulation | Test toggled router instantiation | Both branches instantiate cleanly (5 routes each) | **PASS** | Strategy B verified feasible |
| 2026-09-07 | Phase 4 | Shared Exceptions Hierarchy | AppError, NotFoundError, ValidationError, ConflictError | Domain exceptions mapped cleanly to HTTP status codes & detail payload | **PASS** | Zero HTTPException in actions/validators |
| 2026-09-07 | Phase 4 | Shared Auth Boundary | JWT decode, password hash, hospital context | Token and context dependencies mirror OG claims & status codes | **PASS** | Decoupled from app.utils.auth |
| 2026-09-07 | Phase 4 | Shared Audit Service | AuditService & write_audit_log | Exact AuditLog fields preserved across sync and async sessions | **PASS** | Decoupled from app.utils.audit |
| 2026-09-07 | Phase 4 | Cross-Domain Decoupling | AppointmentReader extraction | Appointment queries separated from VitalsRepository | **PASS** | VitalsRepository now 100% vitals data access |
| 2026-09-07 | Phase 4 | Controlled Cutover Switch | app/main.py router conditional mount | USE_MIGRATED_VITALS toggles legacy vs migrated with zero route collisions | **PASS** | 284 unique operation IDs (0 duplicates) |
| 2026-09-07 | Phase 4 | Real App Runtime Integration | Full HTTP lifecycle through app.main | 7 integration tests passing; auth -> action -> DB -> audit verified | **PASS** | Real application execution confirmed |
| 2026-09-07 | Phase 4 | Live PostgreSQL Connection | AsyncEngine & asyncpg connection | Connected to Azure PG 17.11 (HMSstage2) | **PASS** | Asynchronous driver verified |
| 2026-09-07 | Phase 4 | Live PostgreSQL Metadata | Inspect vital_readings & appointments | All 10 physical vital_readings columns match model | **PASS** | Schema metadata verified against PG |
| 2026-09-07 | Phase 4 | Live PostgreSQL Reads | Async read queries on vitals & appointments | Read 5 vitals rows, 5 appointment rows asynchronously | **PASS** | Read queries operational on PG |
| 2026-09-07 | Phase 4 | Live PostgreSQL Writes | Staging write & rollback safety | Safe isolated test DB unavailable; staging DB protected | **NOT EXECUTED** | Explicitly reported per safety requirement |
| 2026-09-07 | Phase 4 | Regression Safety | Health, CORS, and full suite | 78 passed, 0 failed across baseline, module, and cutover suites | **PASS** | Zero regressions on existing endpoints |


---

## Known Risks

1. **Async Conversion Regression Risk (CRITICAL)**:
   - *Risk*: Converting from synchronous `psycopg2` to async `asyncpg` touches all 288 endpoints. Naive conversion will block the event loop.
   - *Mitigation*: Keep Slice 1 on synchronous session provider; defer async conversion to a dedicated infrastructure phase.
2. **Circular Dependency on Model Decomposition (CRITICAL)**:
   - *Risk*: `app/models.py` has interconnected foreign keys across all 53 tables. Slicing entities into separate files can trigger Python circular import cycles.
   - *Mitigation*: Use string-based model relationships (`relationship("Patient")`) and shared `Base = DeclarativeBase()` registry.
3. **Startup DDL Migrations in `app/main.py` (HIGH)**:
   - *Risk*: `main.py` contains 23 runtime table alterations. Extracting them requires taking an exact schema snapshot from Azure PostgreSQL.
   - *Mitigation*: Baseline the staging schema with Alembic before removing in-code migrations.
4. **Frontend Response Envelope Breakage (HIGH)**:
   - *Risk*: Template specifies `{ "data": ..., "meta": ... }` envelope. Applying this to existing endpoints will break `HMS-frontend`.
   - *Mitigation*: Maintain raw Pydantic response models for existing endpoints.
5. **Streaming HTML Print Breakage (HIGH)**:
   - *Risk*: Prescription, lab report, and invoice PDF/print endpoints stream HTML. Wrapping them in JSON will destroy printable outputs.
   - *Mitigation*: Explicitly exempt all `StreamingResponse(media_type="text/html")` endpoints from response middleware.
6. **SQLite vs Azure PostgreSQL Behavioral Fidelity (MEDIUM)**:
   - *Risk*: Tests run on SQLite in-memory using a compiler hook `@compiles(JSONB, 'sqlite')` to render JSONB as TEXT. While basic JSON storage and queries work, complex PostgreSQL JSONB operators (`->>`, `@>`, etc.) or PostgreSQL-specific dialect features are not natively supported by SQLite.
   - *Mitigation*: Ensure baseline tests for Vitals test standard ORM queries and attributes; avoid assuming SQLite will catch PostgreSQL-specific syntax or constraint subtleties.
7. **Application Lifespan Staging Database Hazard (HIGH)**:
   - *Risk*: `app.main:lifespan` automatically executes 23 DDL migrations directly against `app.database.engine` and starts an infinite background auto-cancel loop. If tests trigger lifespan on `app.main:app`, they will attempt to run PostgreSQL DDL against whatever engine is bound.
   - *Mitigation*: Test harness creates isolated FastAPI app without lifespan, and monkeypatches `app.database.engine.connect` to guarantee fail-fast protection against any connection to Azure PostgreSQL.
8. **Cross-Module Coupling on Legacy Vitals Router (HIGH)**:
   - *Risk*: `app/routers/doctors.py` imports `_reading_response` and `_revert_checked_in_without_vitals` directly from `app.routers.vitals`, and `app/schemas_doctors.py` imports `VitalReadingResponse` from `app.schemas_vitals`. Deleting or emptying the legacy vitals files upon cutover will break the Doctors module with an `ImportError`.
   - *Mitigation*: Protect `app/routers/vitals.py` and `app/schemas_vitals.py` in place. Cutover only changes router registration in `app/main.py`.
9. **Simultaneous Router Mount Collision Hazard (HIGH)**:
   - *Risk*: Mounting both legacy and migrated routers under `/api` in `app/main.py` causes Starlette route shadowing (first router wins) and OpenAPI schema overwrites.
   - *Mitigation*: Never mount both routers concurrently under the same prefix. Use exclusive selection (Strategy B or Strategy A).


---

## Open Questions

1. **QUESTION**: Is it approved to keep Slice 1 (`vitals`) synchronous, delaying full asyncpg conversion until infrastructure phase?  
   **WHY IT MATTERS**: Converting to asyncpg immediately requires touching database adapters and session lifecycles across the entire app.  
   **WHAT EVIDENCE IS MISSING**: Confirmation from the project owner on whether immediate async is required or phased async is accepted.

2. **QUESTION**: Should the API response format remain un-enveloped (raw Pydantic) to maintain compatibility with `HMS-frontend`?  
   **WHY IT MATTERS**: `HMS-frontend/src/services/api.ts` expects raw arrays and objects. Enveloping in `{ "data": ... }` will break the client.  
   **WHAT EVIDENCE IS MISSING**: Confirmation on whether frontend updates will occur in parallel or if backend must maintain 100% contract compatibility.

3. **QUESTION**: Where should the role-based dashboard aggregations currently in `hospitals.py` live long-term?  
   **WHY IT MATTERS**: `hospitals.py` has 1,000+ lines calculating dashboards for doctors, nurses, reception, lab, radiology, OT, and billing.  
   **WHAT EVIDENCE IS MISSING**: Architecture decision on whether to decompose into module dashboard actions or maintain a dedicated reporting aggregation service.

---

## Decisions


| Date | Decision | Reason | Impact |
|---|---|---|---|
| 2026-09-06 | Select `modules/vitals/` as Primary Slice 1 | Smallest self-contained clinical module (<500 lines) with complete 8-layer vertical applicability | Establishes reusable template pattern with lowest clinical and financial risk |
| 2026-09-06 | Defer Billing, Pharmacy, Appointments, Lab, Doctors | High cross-module coupling, financial calculations, and clinical synchronization risks | Prevents cascading regressions during early migration |
| 2026-09-06 | Maintain raw response format for Slice 1 | Avoid breaking `HMS-frontend` API contracts | Guarantees backwards compatibility for UI |
| 2026-09-06 | Establish `MIGRATION_PROGRESS.md` as Single Source of Truth | Persistent tracking across all agent and developer sessions | Eliminates loss of context and ensures continuous traceability |
| 2026-09-07 | Strict Isolation from Azure PostgreSQL | Monkeypatch `app.database.engine.connect` and execute tests against `sqlite:///:memory:` | Zero risk of touching production/staging database during tests |
| 2026-09-07 | Test App Construction without Lifespan | Mount `vitals.router` on dedicated test FastAPI app without running `app.main:lifespan` | Prevents executing 23 DDL migrations and infinite background loops during test runs |
| 2026-09-07 | Freeze Existing HMS-B Implementation | Keep `app/` and `scripts/` 100% unmodified during Phase 1 | Ensures baseline tests measure actual reference behavior |
| 2026-09-07 | Migrated Namespace Designation (`hms_migration/`) | Future migrated code will live in `hms_migration/` rather than root | Clean architectural separation between existing and migrated implementations |
| 2026-09-07 | Transitional Entity Bridge in `entities/vital_reading.py` | Avoid duplicate SQLAlchemy `Table("vital_readings")` definition error on shared `Base.metadata` | Prevents mapper collisions while `app/models.py` is preserved |
| 2026-09-07 | Thin API Handlers & Pure Repository Decoupling | Strict compliance with template coding standards (`.cursor/coding_standards.md`) | API handlers delegate to actions; repositories contain zero business decisions |
| 2026-09-07 | Eliminate Accidental Duplicate Ledger | Discovered untracked `MIGRATION_PROGRESS.md` in workspace root; deleted it | Enforces strict single-ledger rule at `HMS-B/MIGRATION_PROGRESS.md` |
| 2026-09-07 | Action Decoupling via Contracts Serializer | Avoid actions importing helper functions from sibling actions (`get_today_vitals_action`) | Clean domain separation where contracts layer manages DTO mapping |
| 2026-09-07 | Reject Simultaneous Router Mounting under `/api` | Starlette first-match-wins causes route shadowing; FastAPI generates Duplicate Operation ID warnings | Only one Vitals router will be mounted at `/api` at any time |
| 2026-09-07 | Reject Parallel Prefix Routing (Strategy D) | `HMS-frontend/src/lib/api.ts` hardcodes `/api/vitals`; frontend cannot call `/api/v2/vitals` | All vitals traffic must remain at `/api/vitals` |
| 2026-09-07 | Reject Dynamic Feature Flag Platform (Strategy C) | HMS-B lacks feature-flagging infrastructure; introducing one violates scoped migration principles | Avoid unnecessary infrastructure dependencies |
| 2026-09-07 | Recommend Configuration-Based Router Selection (Strategy B) | Environment-based toggling (`USE_MIGRATED_VITALS`) provides controlled router selection and safe rollback | Safest operational cutover path |
| 2026-09-07 | Preserve Legacy Files `app/routers/vitals.py` & `schemas_vitals.py` | `app/routers/doctors.py` and `schemas_doctors.py` import functions and types from legacy vitals | Prevents cascading broken imports into Doctors module |
| 2026-09-07 | Domain Exceptions Separation | Eliminate `HTTPException` from actions and validators in `modules/vitals` | Pure domain exceptions mapped at API boundary via global handlers |
| 2026-09-07 | Appointment Domain Boundary Isolation | Extract `AppointmentReader` to decouple `VitalsRepository` from `Appointment` table | Preserves transaction boundary and query logic while enforcing clean repository boundaries |
| 2026-09-07 | Async Database Foundation & Transitional Sync Session | Implement `create_async_engine` (asyncpg) with explicitly marked transitional `get_transitional_sync_session` | Establishes async foundation while safely bridging hybrid synchronous models |
| 2026-09-07 | Protected Staging Write Policy | Prohibit destructive write verification against shared staging Azure PostgreSQL | Prevents sequence advancement and staging state contamination |
| 2026-09-07 | Standalone Declarative Base | Create `hms_migration/infrastructure/postgres/base.py:Base` | Isolates target architecture entity metadata completely from legacy `app.models.Base` |
| 2026-09-07 | Audit Subsystem Decoupling | Define independent `AuditLog` entity in `shared/audit/entities/audit_log.py` | Eliminates `from app.models import AuditLog` runtime dependency |
| 2026-09-07 | Vitals Entity Decoupling | Define independent `VitalReading` entity on new `Base` | Eliminates `from app.models import VitalReading` runtime dependency |
| 2026-09-07 | Shared Auth Independence | Verified `hms_migration/shared/auth/` relies purely on `jose`, `bcrypt`, and central settings | Confirms zero legacy dependencies in authentication subsystem |
| 2026-09-07 | Pure SQLAlchemy Core for Analytics | Use `table()` and `column()` constructs in `AnalyticsRepository` | Completely eliminates `app.models` imports in new Analytics slice |
| 2026-09-07 | Controlled Analytics Cutover | Add `USE_MIGRATED_ANALYTICS` toggle in `app/main.py` and `config/settings.py` | Allows safe, zero-downtime cutover and immediate rollback for Analytics |
| 2026-09-07 | Patient Root Identity Prioritization | Prioritized Core Patient domain ahead of Appointments in pre-implementation audit | Validated Patient as clinical root prerequisite for Appointments and Vitals |
| 2026-09-07 | Core Patient Scope Demarcation | Migrated 4 registration and directory endpoints; preserved 5 inpatient bed/admission endpoints in legacy | Prevents premature scope inflation into inpatient/bed management domain |
| 2026-09-07 | Independent Patient Entity | Mapped on target `Base` with cross-dialect SQLite JSON and PostgreSQL JSONB support | Eliminates ORM relationship coupling to legacy models |
| 2026-09-07 | Vitals Decoupling from Legacy Patient | Redirected Vitals repository and action imports to migrated `Patient` entity | Eliminates foundational cross-module dependency from Vitals onto `app.models` |
| 2026-09-07 | Selective Inpatient Router Preservation | Under `USE_MIGRATED_PATIENTS=true`, legacy router mounts non-patient routes | Avoids route collision, eliminates duplicate operation IDs, preserves inpatient APIs |
| 2026-09-07 | Port Appointments Domain (20 Endpoints) | Migrated complete OPD appointments domain into `hms_migration/modules/appointments/` | Replaces 1,285-line legacy monolithic router with compliant 8-layer vertical architecture |
| 2026-09-07 | Cross-Module Decoupling (Vitals & Patients) | Replaced legacy `app.models.Appointment` and `app.utils.appointment_lifecycle` imports in Vitals and Patients with `hms_migration.modules.appointments` entities and services | Decouples Vitals and Patients modules completely from legacy appointment code |
| 2026-09-07 | Lifespan Missed Appointment Background Worker | Ported missed appointment auto-cancellation loop to `hms_migration/app/lifespan.py` using `get_transitional_sync_session_factory` | Eliminates reliance on legacy `app/main.py` lifespan background worker |
| 2026-09-07 | Controlled Appointments Cutover | Add `USE_MIGRATED_APPOINTMENTS` toggle in `app/main.py`, `app/config.py`, and `hms_migration/config/settings.py` | Enables safe runtime switching between legacy and migrated Appointments router with zero downtime |

---

## Known Unknowns

1. **UNKNOWN / REQUIRES VERIFICATION**: Staging database exact migration state. Whether all 23 migration functions in `main.py` have already run on the Azure database or if new columns are dynamically generated on restart.
2. **UNKNOWN / REQUIRES VERIFICATION**: Whether Azure Blob Storage credentials will be provisioned to replace Base64 data URLs in PostgreSQL `Text` columns.
3. **UNKNOWN / REQUIRES VERIFICATION**: Exact timeline for migrating `HMS-frontend` to match any potential envelope or versioning changes.
4. **UNKNOWN / REQUIRES VERIFICATION**: High-concurrency queue token race condition under parallel write load on Azure PostgreSQL (inherited from legacy code's `func.max(queue_token) + 1`).
5. **RESOLVED**: Cross-module coupling on legacy Appointments: Vitals (`appointment_reader.py`, `vitals_validator.py`, `create_vitals_action.py`) and Patients (`profile_reader.py`) now consume `hms_migration.modules.appointments` entities, enums, and lifecycle services.
6. **UNKNOWN / REQUIRES VERIFICATION**: When Inpatient/Bed management domain will be migrated to decommission the remaining 5 endpoints in `app/routers/registration.py` and IPD transfer integrations.

---

## Next Recommended Step

**Phase 8 — Doctors & Clinical Records / Inpatient (IPD) Domain Migration**:
1. Migrate `app/routers/doctors.py` and clinical documentation (prescriptions, consultation notes, diagnosis) to target architecture `modules/doctors/` and `modules/clinical/`.
2. Migrate remaining Inpatient Bed Management & Admission routes from `app/routers/registration.py` (`/beds`, `/wards-rooms`, `/admit`, `/discharge`) to `modules/inpatient/`.
3. Codify imperative startup DDL statements into versioned Alembic migrations.

---

## Change Log

- **2026-09-06**: Initial creation of `MIGRATION_PROGRESS.md` following repository-wide AST inventory, coupling analysis, 500-line guideline assessment, and candidate scoring (Phase 0).
- **2026-09-07**: Phase 1 completed. Established isolated pytest test harness with 32 integration tests covering all Vitals endpoints, authentication, authorization, tenant isolation, lifecycle side-effects, and audit logging. Verified zero changes to existing production code (`app/`).
- **2026-09-07**: Phase 2 completed. Migrated Vitals into `hms_migration/modules/vitals/` across 8 template layers (`api`, `actions`, `db`, `entities`, `contracts`, `validators`, `permissions`, `tests`). Established 34 new module tests; all 66 tests passing (32 baseline + 34 migrated). Legacy production files 100% untouched.
- **2026-09-07**: Phase 2 Independent Verification & Hardening completed. Discovered and removed root duplicate ledger; verified `HMS_Backend_Feature_Tracker.xlsx` is pre-existing; audited all 8 vertical slice layers; refactored action coupling by moving ORM-to-contract serialization to `contracts/`; added 5 endpoint validation tests (total 71 tests passing: 32 baseline + 39 migrated); verified zero changes to `app/` and `scripts/`. Legacy code remains pristine.
- **2026-09-07**: Phase 3 completed. Conducted comprehensive Vitals router integration strategy investigation and verification. Audited `app/main.py`, `app/routers/vitals.py`, `hms_migration/modules/vitals/api/vitals_api.py`, `app/routers/doctors.py`, and `HMS-frontend/src/lib/api.ts`. Verified 100% route, schema, parameter, and OpenAPI contract equivalence. Proved route conflict and shadowing when both routers coexist under `/api`. Discovered cross-module coupling in `doctors.py` requiring legacy vitals files to remain protected. Evaluated 5 integration strategies; selected Strategy B (configuration-based router selection) as primary recommendation and Strategy A (direct replacement) as fallback. Rejected Strategies C and D. Verified all 71 tests passing (32 baseline + 39 migrated). Production files `app/` and `scripts/` remain 100% untouched.
- **2026-09-07**: Phase 4 completed. Established foundational target architecture: `hms_migration/config/settings.py`, `hms_migration/shared/exceptions/` (domain hierarchy and global handlers), `hms_migration/shared/auth/` (JWT, security, dependencies), `hms_migration/shared/audit/` (`AuditService`, `write_audit_log`), `hms_migration/infrastructure/postgres/` (async engine, asyncpg, AsyncSession, BaseRepository, and transitional sync session bridge), and `hms_migration/app/` (root router, lifespan, create_app). Decoupled Vitals: eliminated all `HTTPException` leakage into `VitalAppointmentNotFoundError`, `VitalReadingNotFoundError`, `VitalMutationNotAllowedError`, `VitalValidationError`; extracted `AppointmentReader` to decouple cross-domain access from `VitalsRepository`; replaced legacy auth/audit/db imports with shared foundations. Implemented controlled runtime cutover in `app/main.py` via `USE_MIGRATED_VITALS` toggle. Verified live PostgreSQL 17.11 on Azure (connection, session, schema, and reads PASS; writes protected/not executed). Full test suite expanded to 78 tests passing (32 baseline + 39 migrated + 7 cutover integration tests). Zero production regressions.
- **2026-09-07**: Phase 5 completed. Executed Foundation Decoupling & Second Vertical Slice Migration (Analytics). Established standalone `hms_migration/infrastructure/postgres/base.py:Base`. Created independent domain entities `AuditLog` and `VitalReading`, completely eliminating `app.models.AuditLog` and `app.models.VitalReading` runtime dependencies from `hms_migration/`. Verified `shared/auth` is 100% independent. Migrated `modules/analytics/` across target template layers (`api`, `actions`, `db`, `contracts`, `tests`) using pure SQLAlchemy Core constructs with zero `app.models` imports. Implemented controlled cutover via `USE_MIGRATED_ANALYTICS` toggle in `app/main.py` and `config/settings.py`. Verified live Azure PostgreSQL (`HMSstage2`) read queries for Analytics, Vitals, and Audit (all PASS; writes protected/not executed). Test suite expanded to 94 passing tests (32 vitals baseline + 39 vitals module + 7 vitals cutover + 6 analytics baseline + 7 analytics module + 3 analytics cutover). Zero regressions.
- **2026-09-07**: Phase 5 Acceptance & Verification Audit completed. Conducted rigorous audit of code, tests, and database behavior:
  - Verified independent declarative Base (`hms_migration.infrastructure.postgres.base.Base`) with isolated `MetaData` registry.
  - Verified `AuditLog` domain entity is fully decoupled (zero `app.*` imports).
  - Verified `VitalReading` domain entity is decoupled from `app.models.VitalReading`, but accurately classified remaining transitional imports in Vitals (`Patient`, `Appointment`, `mark_in_progress`).
  - Verified `shared/auth` is 100% independent of legacy code.
  - Investigated and resolved plan-distribution and limit discrepancies: verified code uses `basic, premium, platinum` and `limit=8`, matching legacy behavior 100%.
  - Verified exact behavioral equivalence between legacy and migrated Analytics endpoints across 16 dimensions.
  - Executed full test regression: 94 passed, 0 failed, 12 warnings in 5.36s.
  - Executed live Azure PostgreSQL 17.11 read tests on `HMSstage2`: connection, decoupled `VitalReading`, decoupled `AuditLog`, and `AnalyticsRepository` multi-table reads verified matching legacy results identically. Writes/rollback safely recorded as NOT EXECUTED.
  - Verified controlled cutover for both Vitals (`USE_MIGRATED_VITALS`) and Analytics (`USE_MIGRATED_ANALYTICS`) in `app/main.py`.
  - Confirmed Phase 5 status: **COMPLETE**.
- **2026-09-07**: Phase 6 completed. Executed Core Patient Domain Migration:
  - Validated Patients as the true clinical root identity prerequisite over Appointments in pre-implementation audit.
  - Established 8 baseline behavioral tests (`tests/integration/test_patients_baseline.py`).
  - Created independent domain entity `Patient` in `hms_migration/modules/patients/entities/patient.py` on target `Base` with zero legacy relationships and SQLite/PG JSONB support.
  - Implemented target vertical slice architecture across `contracts/`, `validators/`, `db/` (with `PatientProfileReader` separating cross-domain queries), `actions/` (`RegisterPatientAction`, `ListPatientsAction`, `GetPatientProfileAction`, `UpdatePatientAction`), and `api/` (`patients_api.py`).
  - Decoupled `hms_migration/modules/vitals/` from `app.models.Patient`, redirecting imports to `hms_migration.modules.patients.entities.patient.Patient`.
  - Implemented controlled cutover via `USE_MIGRATED_PATIENTS` in `app/config.py`, `hms_migration/config/settings.py`, and `app/main.py`. Under cutover, legacy registration router continues serving 5 inpatient endpoints (`/beds`, `/wards-rooms`, `/admit`, `/discharge`, `/doctors`) with zero route collision or duplicate OpenAPI operation IDs.
  - Created 8 module tests (`hms_migration/modules/patients/tests/test_patients.py`) and 3 runtime cutover integration tests (`tests/integration/test_patients_cutover.py`).
  - Executed full test suite: 113 passed, 0 failed, 12 warnings in 6.37s.
  - Executed safe read-only verification on live staging Azure Database for PostgreSQL 17.11 (`HMSstage2`): connection, schema column verification, unique constraints, and migrated ORM read queries all PASS (110 live patients count verified; writes/rollbacks safely recorded as NOT EXECUTED).
  - Confirmed Phase 6 status: **COMPLETE**.
- **2026-09-07**: Phase 7 completed. Executed Appointments Domain Migration & Cross-Module Decoupling:
  - Established 8 baseline integration tests (`tests/integration/test_appointments_baseline.py`) against legacy appointment router.
  - Defined standalone entities `Appointment`, `AppointmentType`, and `AppointmentStatus` on target `Base` in `hms_migration/modules/appointments/entities/`.
  - Implemented target vertical slice architecture across 8 layers:
    - `contracts/`: Pydantic V2 schemas for booking, fee preview, rescheduling, queue, availability, leave blocks.
    - `permissions/`: role-based access for appointments actions.
    - `exceptions/`: domain hierarchy (`AppointmentNotFoundError`, `AppointmentValidationError`, `AppointmentConflictError`).
    - `validators/`: past slot, check-in state, reschedule constraints.
    - `services/`: appointment lifecycle helpers (`mark_in_progress`, completion blockers, auto-cancel missed appointments).
    - `db/`: `AppointmentsRepository`, `AvailabilityReader`, `PricingReader` under 500-line guidelines.
    - `actions/`: discrete actions for booking, fee calculation, availability checks, check-in, completion, cancellation, no-show, rescheduling, nurse assignment, IPD admission.
    - `api/`: all 20 business endpoints wired under `/appointments`.
  - Decoupled existing migrated modules (`vitals`, `patients`) from legacy Appointment models and lifecycle utilities.
  - Ported background auto-cancel worker into `hms_migration/app/lifespan.py`.
  - Implemented controlled runtime cutover via `USE_MIGRATED_APPOINTMENTS` in `app/main.py`, `app/config.py`, and `hms_migration/config/settings.py`.
  - Created 13 module tests (`hms_migration/modules/appointments/tests/test_appointments.py`) and 3 cutover integration tests (`tests/integration/test_appointments_cutover.py`).
  - Verified full test suite: **137 passed, 0 failed, 12 warnings in 11.99s**. Zero regressions.
  - Total business endpoints migrated increased to **30 / 281** (10.7%).
  - Confirmed Phase 7 status: **COMPLETE**.
- **2026-09-07**: Phase 8 completed. Executed Doctors, Clinical Records, Beds & Inpatient/IPD Domain Migration:
  - Audited legacy `app/routers/doctors.py`, `beds.py`, `ipd.py`, and `registration.py` along with `HMS-frontend` contract consumption.
  - Verified 48 total business endpoints across 4 target vertical modules:
    - `modules/doctors/`: 18 endpoints (directory, clinic profile, patient search, CRUD, appointments, calendar, schedule context, leaves, leave ranges, leave deletion).
    - `modules/clinical_records/`: 7 endpoints (prescriptions CRUD, HTML/PDF rendering, medical records creation/listing/file retrieval mounted under `/api/doctors/...` preserving exact public URL contracts).
    - `modules/beds/`: 9 endpoints (dashboard, occupancy, wards, rooms, options, doctor list, admit, allocate, transfer + `/api/registration/beds` & `/api/registration/wards-rooms`).
    - `modules/inpatient/`: 14 endpoints (discharge request, list requests, discharge execution, active admissions, registration patient admit & discharge, IPD form submissions CRUD & HTML view).
  - Target database entities defined on isolated target `Base` (`hms_migration.infrastructure.postgres.base.Base`):
    - `HospitalUser`, `StaffRole`, `ShiftType`, `DoctorLeave`, `Holiday` (Doctors)
    - `WardType`, `Ward`, `Room`, `Bed` (Beds)
    - `AdmissionStatus`, `Admission`, `IpdFormSubmissionStatus`, `IpdFormSubmission` (Inpatient)
    - `Prescription`, `MedicalRecord`, `PatientDocument`, `PatientDocumentCategory` (Clinical Records)
    - Target `Base.metadata` registry expanded to 18 tables with zero duplicate tables.
  - Decoupled `hms_migration/modules/appointments/actions/appointment_status_actions.py` from legacy `app.models` `Admission` and `Bed`.
  - Maintained `< 500 lines` structural rule across all Python source files via focused module action splitting (`doctor_appointment_actions.py`, `registration_admission_actions.py`).
  - Added 17 new automated tests (unit, tenant isolation, and cutover integration tests):
    - `hms_migration/modules/doctors/tests/test_doctors.py` (5 tests)
    - `hms_migration/modules/clinical_records/tests/test_clinical_records.py` (2 tests)
    - `hms_migration/modules/beds/tests/test_beds.py` (3 tests)
    - `hms_migration/modules/inpatient/tests/test_inpatient.py` (2 tests)
    - `tests/integration/test_phase8_cutover.py` (5 tests)
  - Full test suite expanded to **154 passed, 0 failed, 12 warnings in 16.56s**.
  - Controlled cutover flags introduced and verified: `USE_MIGRATED_DOCTORS`, `USE_MIGRATED_BEDS`, `USE_MIGRATED_INPATIENT`.
  - Cumulative business endpoints migrated increased from **30 / 281** (10.7%) to **78 / 281** (27.8%).
  - Confirmed Phase 8 status: **COMPLETE**.







