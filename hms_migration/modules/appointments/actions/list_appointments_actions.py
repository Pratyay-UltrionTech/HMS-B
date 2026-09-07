"""
Actions for listing and querying appointments, schedules, and metadata.

Conforms to UltrionTech-Backend-Template modules/appointments/actions/ specification.
"""

from collections import defaultdict
from datetime import date
from typing import Any
from uuid import UUID

from hms_migration.modules.appointments.contracts.appointments_contracts import (
    AppointmentListItem,
    QueueGroup,
)
from hms_migration.modules.appointments.db.appointments_repository import AppointmentsRepository
from hms_migration.modules.appointments.entities.enums import AppointmentStatus


class ListAppointmentsActions:
    """Read actions for appointments and scheduling metadata."""

    def __init__(self, repo: AppointmentsRepository) -> None:
        self.repo = repo

    def get_today(
        self,
        doctor_id: UUID | None = None,
        status: AppointmentStatus | None = None,
    ) -> list[AppointmentListItem]:
        """Fetch today's appointments for clinic schedule."""
        appts = self.repo.list_today(doctor_id=doctor_id, status=status)
        return self.repo.hydrate_appointment_items(appts)

    def get_calendar(
        self,
        week_start: date,
        doctor_id: UUID | None = None,
    ) -> list[AppointmentListItem]:
        """Fetch appointments for calendar week view."""
        appts = self.repo.list_calendar(week_start=week_start, doctor_id=doctor_id)
        return self.repo.hydrate_appointment_items(appts)

    def get_queue(self, doctor_id: UUID | None = None) -> list[QueueGroup]:
        """Fetch waiting queue grouped by doctor."""
        appts = self.repo.list_queue(doctor_id=doctor_id)
        items = self.repo.hydrate_appointment_items(appts)

        grouped: dict[UUID, list[AppointmentListItem]] = defaultdict(list)
        doc_names: dict[UUID, str] = {}
        for item in items:
            grouped[item.doctor_id].append(item)
            if item.doctor_name:
                doc_names[item.doctor_id] = item.doctor_name

        groups: list[QueueGroup] = []
        for doc_id, p_items in grouped.items():
            groups.append(
                QueueGroup(
                    doctor_id=doc_id,
                    doctor_name=doc_names.get(doc_id, "Unknown Doctor"),
                    patients=p_items,
                )
            )
        return groups

    def get_history(
        self,
        patient_id: UUID | None = None,
        doctor_id: UUID | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        status: AppointmentStatus | None = None,
    ) -> list[AppointmentListItem]:
        """Fetch historical appointment records."""
        appts = self.repo.list_history(
            patient_id=patient_id,
            doctor_id=doctor_id,
            from_date=from_date,
            to_date=to_date,
            status=status,
        )
        return self.repo.hydrate_appointment_items(appts)

    def get_ipd_requests(self) -> list[AppointmentListItem]:
        """Fetch pending IPD bed transfer requests."""
        appts = self.repo.list_ipd_requests()
        return self.repo.hydrate_appointment_items(appts)

    def list_doctors(self) -> list[dict[str, Any]]:
        return self.repo.list_active_doctors()

    def list_nurses(self) -> list[dict[str, Any]]:
        return self.repo.list_active_nurses()

    def list_wings(self) -> list[dict[str, Any]]:
        return self.repo.list_wings()

    def list_departments(self) -> list[dict[str, Any]]:
        return self.repo.list_departments()

    def list_visit_types(self) -> list[dict[str, Any]]:
        return self.repo.list_visit_types()
