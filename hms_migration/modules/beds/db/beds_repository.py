"""
Database repository for Beds, Wards, and Rooms.

Conforms to UltrionTech-Backend-Template modules/beds/db/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from hms_migration.modules.beds.entities.bed import Bed, Room, Ward
from hms_migration.modules.inpatient.entities.admission import Admission, AdmissionStatus


class BedsRepository:
    """Repository handling data access and synchronization for beds, wards, and rooms."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def ensure_beds_for_room(self, hospital_id: UUID, room: Room) -> None:
        """Create bed rows if fewer than room.bed_count exist."""
        existing = (
            self.db.query(func.count(Bed.id))
            .filter(Bed.room_id == room.id, Bed.hospital_id == hospital_id)
            .scalar()
            or 0
        )
        if existing >= room.bed_count:
            return
        for i in range(existing + 1, room.bed_count + 1):
            self.db.add(
                Bed(
                    hospital_id=hospital_id,
                    ward_id=room.ward_id,
                    room_id=room.id,
                    bed_code=f"Bed-{i}",
                    is_occupied=False,
                    is_active=True,
                )
            )
        self.db.flush()

    def sync_all_beds(self, hospital_id: UUID) -> None:
        """Ensure all active rooms have their allocated beds instantiated."""
        rooms = (
            self.db.query(Room)
            .filter(Room.hospital_id == hospital_id, Room.is_active.is_(True))
            .all()
        )
        for room in rooms:
            self.ensure_beds_for_room(hospital_id, room)
        self.db.flush()

    def get_dashboard_rows(
        self,
        hospital_id: UUID,
        ward_id: UUID | None = None,
        status_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch beds and overlay active admissions for the bed dashboard."""
        self.sync_all_beds(hospital_id)
        self.db.commit()

        q = (
            self.db.query(Bed)
            .options(joinedload(Bed.ward), joinedload(Bed.room))
            .filter(Bed.hospital_id == hospital_id, Bed.is_active.is_(True))
        )
        if ward_id:
            q = q.filter(Bed.ward_id == ward_id)
        if status_filter == "available":
            q = q.filter(Bed.is_occupied.is_(False))
        elif status_filter == "occupied":
            q = q.filter(Bed.is_occupied.is_(True))

        beds = q.order_by(Bed.ward_id.asc(), Bed.room_id.asc(), Bed.bed_code.asc()).all()

        active = (
            self.db.query(Admission)
            .options(joinedload(Admission.patient), joinedload(Admission.doctor))
            .filter(
                Admission.hospital_id == hospital_id,
                Admission.status.in_([AdmissionStatus.admitted, AdmissionStatus.discharge_requested]),
            )
            .all()
        )
        by_bed = {a.bed_id: a for a in active}

        rows = []
        for b in beds:
            a = by_bed.get(b.id)
            occupied = bool(b.is_occupied or a)
            rows.append(
                {
                    "bed_id": b.id,
                    "ward_id": b.ward_id,
                    "room_id": b.room_id,
                    "ward_name": b.ward.name if b.ward else None,
                    "room_code": b.room.room_code if b.room else None,
                    "bed_code": b.bed_code,
                    "status": "Occupied" if occupied else "Available",
                    "is_occupied": occupied,
                    "patient_id": a.patient_id if a else None,
                    "patient_name": a.patient.name if a and a.patient else None,
                    "patient_uhid": a.patient.uhid if a and a.patient else None,
                    "admission_id": a.id if a else None,
                    "doctor_name": a.doctor.name if a and a.doctor else None,
                }
            )
        return rows

    def get_occupancy_report(self, hospital_id: UUID) -> dict[str, Any]:
        """Calculate hospital-wide and per-ward bed occupancy statistics."""
        self.sync_all_beds(hospital_id)
        self.db.commit()

        total = (
            self.db.query(func.count(Bed.id))
            .filter(Bed.hospital_id == hospital_id, Bed.is_active.is_(True))
            .scalar()
            or 0
        )
        occupied = (
            self.db.query(func.count(Bed.id))
            .filter(
                Bed.hospital_id == hospital_id,
                Bed.is_active.is_(True),
                Bed.is_occupied.is_(True),
            )
            .scalar()
            or 0
        )
        available = int(total) - int(occupied)
        pct = round((occupied / total) * 100, 1) if total else 0.0

        wards = (
            self.db.query(Ward)
            .filter(Ward.hospital_id == hospital_id, Ward.is_active.is_(True))
            .all()
        )
        by_ward = []
        if wards:
            ward_ids = [w.id for w in wards]
            total_rows = (
                self.db.query(Bed.ward_id, func.count(Bed.id))
                .filter(
                    Bed.hospital_id == hospital_id,
                    Bed.ward_id.in_(ward_ids),
                    Bed.is_active.is_(True),
                )
                .group_by(Bed.ward_id)
                .all()
            )
            occ_rows = (
                self.db.query(Bed.ward_id, func.count(Bed.id))
                .filter(
                    Bed.hospital_id == hospital_id,
                    Bed.ward_id.in_(ward_ids),
                    Bed.is_active.is_(True),
                    Bed.is_occupied.is_(True),
                )
                .group_by(Bed.ward_id)
                .all()
            )
            totals_map = {wid: int(cnt) for wid, cnt in total_rows}
            occ_map = {wid: int(cnt) for wid, cnt in occ_rows}
            for w in wards:
                wt = totals_map.get(w.id, 0)
                wo = occ_map.get(w.id, 0)
                by_ward.append(
                    {
                        "ward_id": str(w.id),
                        "ward_name": w.name,
                        "total": wt,
                        "occupied": wo,
                        "available": wt - wo,
                        "occupancy_percent": round((wo / wt) * 100, 1) if wt else 0.0,
                    }
                )

        return {
            "total_beds": int(total),
            "occupied_beds": int(occupied),
            "available_beds": available,
            "occupancy_percent": pct,
            "by_ward": by_ward,
        }

    def list_wards(self, hospital_id: UUID) -> list[Ward]:
        """List active wards ordered by name."""
        return (
            self.db.query(Ward)
            .filter(Ward.hospital_id == hospital_id, Ward.is_active.is_(True))
            .order_by(Ward.name.asc())
            .all()
        )

    def list_rooms(self, hospital_id: UUID, ward_id: UUID | None = None) -> list[Room]:
        """List active rooms ordered by code, optionally filtered by ward."""
        q = self.db.query(Room).filter(Room.hospital_id == hospital_id, Room.is_active.is_(True))
        if ward_id:
            q = q.filter(Room.ward_id == ward_id)
        return q.order_by(Room.room_code.asc()).all()

    def list_bed_options(
        self,
        hospital_id: UUID,
        ward_id: UUID | None = None,
        room_id: UUID | None = None,
        available_only: bool = True,
    ) -> list[Bed]:
        """Sync and fetch beds matching criteria."""
        rooms_q = self.db.query(Room).filter(Room.hospital_id == hospital_id, Room.is_active.is_(True))
        if ward_id:
            rooms_q = rooms_q.filter(Room.ward_id == ward_id)
        if room_id:
            rooms_q = rooms_q.filter(Room.id == room_id)
        for room in rooms_q.all():
            self.ensure_beds_for_room(hospital_id, room)
        self.db.commit()

        q = (
            self.db.query(Bed)
            .options(joinedload(Bed.ward), joinedload(Bed.room))
            .filter(Bed.hospital_id == hospital_id, Bed.is_active.is_(True))
        )
        if ward_id:
            q = q.filter(Bed.ward_id == ward_id)
        if room_id:
            q = q.filter(Bed.room_id == room_id)
        if available_only:
            q = q.filter(Bed.is_occupied.is_(False))
        return q.order_by(Bed.bed_code.asc()).all()

    def get_bed_by_id(self, hospital_id: UUID, bed_id: UUID) -> Bed | None:
        """Fetch single active bed with ward and room eager-loaded."""
        return (
            self.db.query(Bed)
            .options(joinedload(Bed.ward), joinedload(Bed.room))
            .filter(Bed.id == bed_id, Bed.hospital_id == hospital_id, Bed.is_active.is_(True))
            .first()
        )
