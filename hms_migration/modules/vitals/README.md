# Vitals Module

The Vitals module handles capturing, listing, updating, and removing outpatient vital sign readings (e.g., Blood Pressure, Pulse, Temperature), as well as presenting today's outpatient department (OPD) queue for vitals collection.

## Architecture

This module follows the `UltrionTech-Backend-Template` vertical slice architecture:

| Subdirectory | Purpose | Contents |
|---|---|---|
| `api/` | Thin HTTP endpoint routing | `vitals_api.py` |
| `actions/` | Use cases and business logic orchestration | `get_today_vitals_action.py`, `list_vitals_action.py`, `create_vitals_action.py`, `update_vital_action.py`, `delete_vital_action.py` |
| `db/` | Data access and queries | `vitals_repository.py` |
| `entities/` | Domain entity representation | `vital_reading.py` (transitional bridge to canonical ORM model) |
| `contracts/` | Request and response Pydantic schemas | `vitals_contracts.py` |
| `validators/` | Domain and input validation | `vitals_validator.py` |
| `permissions/` | Module authorization policies | `vitals_permissions.py` |
| `tests/` | Module-scoped unit and integration tests | `test_vitals.py` |

## Endpoints

- `GET /api/vitals/today`: Today's appointment bookings sorted chronologically, with recorded vitals and auto-reversion for waiting visits without vitals.
- `GET /api/vitals`: List recorded vitals filtered by `appointment_id` or `patient_id`.
- `POST /api/vitals`: Batch record vitals for an appointment, triggering `scheduled` → `waiting` check-in transition, sequential queue token assignment, and audit trail entry.
- `PUT /api/vitals/{vital_id}`: Update a recorded vital reading.
- `DELETE /api/vitals/{vital_id}`: Delete a recorded vital reading.
