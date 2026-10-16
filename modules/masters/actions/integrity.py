"""Dependency checks shared by master deletion and room inventory changes."""
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from infrastructure.postgres.base import Base
from modules.beds.entities.bed import Bed, Room, Ward
from modules.critical_care.entities.critical_care_entities import CodeBlueIncident


# Legacy clinical snapshots deliberately store UUIDs without database FKs.
# Keep their dependency semantics explicit beside the declared-FK enumeration.
LOGICAL_REFERENCES = {
    'beds': (CodeBlueIncident.__table__.c.bed_id,),
    'wards': (CodeBlueIncident.__table__.c.ward_id,),
}


def referencing_columns(table_name: str):
    seen = set()
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if any(fk.column.table.name == table_name and fk.column.name == 'id' for fk in column.foreign_keys):
                seen.add((table.name, column.name))
                yield table, column
    for column in LOGICAL_REFERENCES.get(table_name, ()):
        if (column.table.name, column.name) not in seen:
            yield column.table, column


def assert_unreferenced(db: Session, item, *, ignore_tables=(), action: str = 'delete') -> None:
    for table, column in referencing_columns(item.__tablename__):
        if table.name in ignore_tables:
            continue
        if db.execute(select(column).where(column == item.id).limit(1)).first():
            raise HTTPException(409, f'Cannot {action} {item.__tablename__}: referenced by {table.name}. Deactivate it instead.')


def protected_bed_ids(db: Session, ids: list[UUID]) -> set[UUID]:
    protected = set()
    if not ids:
        return protected
    for table, column in referencing_columns('beds'):
        protected.update(db.execute(select(column).where(column.in_(ids)).distinct()).scalars())
    return protected


def reconcile_room_beds(db: Session, room: Room, *, previous_ward_id: UUID | None = None, original_is_active: bool = True) -> None:
    """Reconcile inventory inside the caller's transaction; preserve clinical history."""
    beds = db.query(Bed).filter(Bed.room_id == room.id, Bed.hospital_id == room.hospital_id).order_by(Bed.bed_code).populate_existing().with_for_update().all()
    protected = protected_bed_ids(db, [bed.id for bed in beds])

    if original_is_active and not room.is_active:
        from modules.inpatient.entities.admission import Admission, OPEN_ADMISSION_STATUSES
        bed_ids = [bed.id for bed in beds]
        open_bed_ids = set()
        if bed_ids:
            open_admissions = db.query(Admission.bed_id).filter(
                Admission.hospital_id == room.hospital_id,
                Admission.bed_id.in_(bed_ids),
                Admission.status.in_(OPEN_ADMISSION_STATUSES),
            ).all()
            open_bed_ids = {r[0] for r in open_admissions}
        occupied_beds = [b for b in beds if b.is_occupied or b.id in open_bed_ids]
        if occupied_beds:
            count = len(occupied_beds)
            raise HTTPException(409, f"Cannot deactivate Room {room.room_code} while {count} bed{'s are' if count > 1 else ' is'} occupied.")

    if room.is_active:
        ward = db.query(Ward).filter(Ward.id == room.ward_id, Ward.hospital_id == room.hospital_id).first()
        if ward and not ward.is_active:
            raise HTTPException(400, "Cannot activate Room under an inactive Ward.")

    if previous_ward_id is not None and previous_ward_id != room.ward_id:
        if any(bed.is_occupied or bed.id in protected for bed in beds):
            raise HTTPException(409, 'Cannot move room while beds are occupied or referenced by clinical history.')
        assert_unreferenced(db, room, ignore_tables=('beds',), action='move')
    removable = [bed for bed in reversed(beds) if not bed.is_occupied and bed.id not in protected]
    excess = max(0, len(beds) - room.bed_count)
    if excess > len(removable):
        raise HTTPException(409, 'Cannot reduce room capacity while occupied or referenced beds exceed requested capacity.')
    removed = removable[:excess]
    for bed in removed:
        db.delete(bed)
    retained = [bed for bed in beds if bed not in removed]
    codes = {bed.bed_code for bed in retained}
    number = 1
    while len(retained) < room.bed_count:
        while f'Bed-{number}' in codes:
            number += 1
        code = f'Bed-{number}'
        codes.add(code)
        bed = Bed(hospital_id=room.hospital_id, room_id=room.id, ward_id=room.ward_id, bed_code=code, is_active=room.is_active, is_occupied=False)
        db.add(bed)
        retained.append(bed)
    for bed in retained:
        bed.ward_id = room.ward_id
        bed.is_active = room.is_active


def delete_room_inventory(db: Session, room: Room) -> None:
    beds = db.query(Bed).filter(Bed.room_id == room.id).populate_existing().with_for_update().all()
    protected = protected_bed_ids(db, [bed.id for bed in beds])
    if any(bed.is_occupied or bed.id in protected for bed in beds):
        raise HTTPException(409, 'Cannot delete room while beds are occupied or referenced. Deactivate it instead.')
    assert_unreferenced(db, room, ignore_tables=('beds',))
    for bed in beds:
        db.delete(bed)
    db.flush()
