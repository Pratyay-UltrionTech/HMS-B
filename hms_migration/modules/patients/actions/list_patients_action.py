"""
Action to list and search patients within the hospital tenant.

Conforms to UltrionTech-Backend-Template modules/patients/actions/ specification.
Handles:
- Filtering by tenant, status, and multi-field search term
- Bulk resolution of last appointment dates via profile reader
- Directory response serialization
"""

from hms_migration.modules.patients.contracts.patients_contracts import (
    PatientDirectoryItem,
    PatientStatus,
)
from hms_migration.modules.patients.db.patients_repository import PatientRepository


class ListPatientsAction:
    """Action orchestrating patient search and directory listing."""

    def __init__(self, repo: PatientRepository) -> None:
        self.repo = repo

    def execute(
        self,
        search: str | None = None,
        status_filter: PatientStatus | None = None,
    ) -> list[PatientDirectoryItem]:
        """Execute patient search with bulk last visit resolution."""
        rows = self.repo.list_patients(search=search, status_filter=status_filter)
        visits = self.repo.profile_reader.get_bulk_last_visits([p.id for p in rows])

        return [
            PatientDirectoryItem(
                id=p.id,
                uhid=p.uhid,
                name=p.name,
                first_name=p.first_name or "",
                last_name=p.last_name or "",
                mobile=p.mobile,
                email=p.email,
                gender=p.gender,
                age=p.age,
                date_of_birth=p.date_of_birth,
                blood_group=p.blood_group,
                status=p.status,
                last_visit=visits.get(p.id),
                created_at=p.created_at,
            )
            for p in rows
        ]
