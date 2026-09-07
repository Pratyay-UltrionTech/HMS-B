"""
Actions for Bed and Ward Management.

Conforms to UltrionTech-Backend-Template modules/beds/actions/ specification.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from hms_migration.modules.beds.contracts.bed_contracts import (
    BedDashboardRow,
    BedOption,
    OccupancyReport,
    RoomOption,
    WardRoomOption,
    WardsRoomsCatalog,
)
from hms_migration.modules.beds.db.beds_repository import BedsRepository


class GetBedDashboardAction:
    def __init__(self, db: Session) -> None:
        self.repo = BedsRepository(db)

    def execute(
        self,
        hospital_id: UUID,
        ward_id: UUID | None = None,
        status_filter: str | None = None,
    ) -> list[BedDashboardRow]:
        raw_rows = self.repo.get_dashboard_rows(hospital_id, ward_id, status_filter)
        return [BedDashboardRow(**r) for r in raw_rows]


class GetOccupancyReportAction:
    def __init__(self, db: Session) -> None:
        self.repo = BedsRepository(db)

    def execute(self, hospital_id: UUID) -> OccupancyReport:
        data = self.repo.get_occupancy_report(hospital_id)
        return OccupancyReport(**data)


class ListWardsAction:
    def __init__(self, db: Session) -> None:
        self.repo = BedsRepository(db)

    def execute(self, hospital_id: UUID) -> list[WardRoomOption]:
        wards = self.repo.list_wards(hospital_id)
        return [
            WardRoomOption(
                id=w.id,
                name=w.name,
                ward_type=w.ward_type.value if w.ward_type else None,
                admission_fee=float(w.admission_fee or 0),
                bed_charge_per_day=float(w.bed_charge_per_day or 0),
            )
            for w in wards
        ]


class ListRoomsAction:
    def __init__(self, db: Session) -> None:
        self.repo = BedsRepository(db)

    def execute(self, hospital_id: UUID, ward_id: UUID | None = None) -> list[RoomOption]:
        rooms = self.repo.list_rooms(hospital_id, ward_id)
        return [
            RoomOption(
                id=r.id,
                ward_id=r.ward_id,
                room_code=r.room_code,
                name=r.name,
                bed_count=r.bed_count,
            )
            for r in rooms
        ]


class ListBedOptionsAction:
    def __init__(self, db: Session) -> None:
        self.repo = BedsRepository(db)

    def execute(
        self,
        hospital_id: UUID,
        ward_id: UUID | None = None,
        room_id: UUID | None = None,
        available_only: bool = True,
    ) -> list[BedOption]:
        beds = self.repo.list_bed_options(hospital_id, ward_id, room_id, available_only)
        return [
            BedOption(
                id=b.id,
                bed_code=b.bed_code,
                room_id=b.room_id,
                room_code=b.room.room_code if b.room else None,
                ward_id=b.ward_id,
                ward_name=b.ward.name if b.ward else None,
                is_occupied=b.is_occupied,
            )
            for b in beds
        ]


class GetWardsRoomsCatalogAction:
    def __init__(self, db: Session) -> None:
        self.repo = BedsRepository(db)

    def execute(self, hospital_id: UUID) -> WardsRoomsCatalog:
        wards = self.repo.list_wards(hospital_id)
        rooms = self.repo.list_rooms(hospital_id)
        return WardsRoomsCatalog(
            wards=[{"id": str(w.id), "name": w.name, "ward_type": w.ward_type.value} for w in wards],
            rooms=[
                {
                    "id": str(r.id),
                    "ward_id": str(r.ward_id),
                    "room_code": r.room_code,
                    "name": r.name,
                    "bed_count": r.bed_count,
                }
                for r in rooms
            ],
        )
