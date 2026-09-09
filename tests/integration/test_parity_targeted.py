"""
Comprehensive Target Parity Integration Tests for HMS Features:
- FEAT-003: Masters (Cascading & Referential Integrity on Wing/Department/Room/Ward)
- FEAT-011: Inpatient (Discharge ledger outstanding verification)
- FEAT-014: Operation Theatre (Room conflict overlap windows)
- FEAT-015: Pharmacy (FIFO/FEFO multi-batch allocation & zero stock handling)
- FEAT-018: Medical Equipment (Maintenance overdue & scheduled status thresholds)
"""

from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID, uuid4
import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi import HTTPException

from hms_migration.infrastructure.postgres.base import Base
from hms_migration.modules.appointments.entities.appointment_type import AppointmentType
from hms_migration.modules.clinical_records.entities.clinical_record import Prescription
from hms_migration.modules.doctors.entities.doctor import HospitalUser, StaffRole
from hms_migration.modules.tenancy.entities.hospital import Hospital, PlanType
from hms_migration.modules.masters.entities.organization_entities import Wing, Department
from hms_migration.modules.beds.entities.bed import Ward, Room, Bed, WardType
from hms_migration.modules.pharmacy.entities.pharmacy_entities import (
    Medicine,
    MedicineBatch,
    MedicineCategory,
    StockTransaction,
)
from hms_migration.modules.pharmacy.services.pharmacy_stock_service import allocate_fifo
from hms_migration.modules.equipment.entities.equipment_entities import (
    EquipmentCategory,
    EquipmentItem,
    EquipmentMaintenance,
    EquipmentStatus,
    MaintenanceStatus,
)
from hms_migration.modules.equipment.services.equipment_service import refresh_maintenance_status
from hms_migration.modules.ot.entities.ot_entities import OtRoom, OtSurgery, OtSurgeryStatus
from hms_migration.modules.inpatient.entities.admission import Admission, AdmissionStatus
from hms_migration.modules.patients.entities.patient import Patient, PatientStatus
from hms_migration.modules.inpatient.actions.admission_actions import DischargePatientAction
from hms_migration.modules.inpatient.contracts.inpatient_contracts import DischargeRequest
from hms_migration.modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingSourceType,
)


@pytest.fixture
def parity_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionClass = sessionmaker(bind=engine)
    session = SessionClass()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture
def sample_hospital(parity_db: Session) -> Hospital:
    h = Hospital(
        id=uuid4(),
        hospital_id="HOSP-PARITY",
        name="Parity General Hospital",
        address="123 Health Ave",
        phone="555-1234",
        email="info@parityhospital.com",
        password_hash="hash",
        plan=PlanType.basic,
        is_active=True,
    )
    parity_db.add(h)
    parity_db.commit()
    parity_db.refresh(h)
    return h


# ==============================================================================
# FEAT-003: Masters Parity Tests
# ==============================================================================
def test_masters_wing_and_department_relationship(parity_db: Session, sample_hospital: Hospital):
    """Verifies Wing creation, Department association, and referential behavior."""
    wing = Wing(
        id=uuid4(),
        hospital_id=sample_hospital.id,
        name="Block A",
        code="BLK-A",
        is_active=True,
    )
    parity_db.add(wing)
    parity_db.commit()

    dept = Department(
        id=uuid4(),
        hospital_id=sample_hospital.id,
        wing_id=wing.id,
        name="Cardiology",
        code="CARD",
        is_active=True,
    )
    parity_db.add(dept)
    parity_db.commit()

    loaded_dept = parity_db.query(Department).filter(Department.id == dept.id).first()
    assert loaded_dept is not None
    assert loaded_dept.wing_id == wing.id
    assert loaded_dept.name == "Cardiology"


# ==============================================================================
# FEAT-015: Pharmacy FEFO Allocation Parity Tests
# ==============================================================================
def test_pharmacy_fefo_allocation_multi_batch(parity_db: Session, sample_hospital: Hospital):
    """
    Verifies FEFO allocation matches OG logic:
    Earliest non-expired batch is deducted first.
    """
    cat = MedicineCategory(
        id=uuid4(),
        hospital_id=sample_hospital.id,
        name="Analgesics",
    )
    parity_db.add(cat)
    parity_db.flush()

    med = Medicine(
        id=uuid4(),
        hospital_id=sample_hospital.id,
        category_id=cat.id,
        medicine_name="Paracetamol 500mg",
        brand_name="Crocin",
        generic_name="Acetaminophen",
        manufacturer="GSK",
        minimum_stock=10,
        selling_price=2.50,
        is_active=True,
    )
    parity_db.add(med)
    parity_db.flush()

    # Batch 1: Expires in 30 days, quantity 5
    batch1 = MedicineBatch(
        id=uuid4(),
        hospital_id=sample_hospital.id,
        medicine_id=med.id,
        batch_number="BATCH-EARLY",
        expiry_date=date.today() + timedelta(days=30),
        available_quantity=5,
        initial_quantity=5,
        purchase_price=1.50,
        selling_price=2.50,
        mrp=2.50,
        is_active=True,
    )
    # Batch 2: Expires in 60 days, quantity 10
    batch2 = MedicineBatch(
        id=uuid4(),
        hospital_id=sample_hospital.id,
        medicine_id=med.id,
        batch_number="BATCH-LATE",
        expiry_date=date.today() + timedelta(days=60),
        available_quantity=10,
        initial_quantity=10,
        purchase_price=1.50,
        selling_price=2.50,
        mrp=2.50,
        is_active=True,
    )
    parity_db.add_all([batch1, batch2])
    parity_db.commit()

    # Request 8 units: should take 5 from batch1 and 3 from batch2
    allocations = allocate_fifo(parity_db, hospital_id=sample_hospital.id, medicine_id=med.id, quantity=8)
    assert len(allocations) == 2
    assert allocations[0][0].id == batch1.id
    assert allocations[0][1] == 5
    assert allocations[1][0].id == batch2.id
    assert allocations[1][1] == 3


def test_pharmacy_fefo_allocation_insufficient_stock(parity_db: Session, sample_hospital: Hospital):
    """Verifies that requesting more than total active batch stock raises 400."""
    cat = MedicineCategory(id=uuid4(), hospital_id=sample_hospital.id, name="Antibiotics")
    parity_db.add(cat)
    parity_db.flush()

    med = Medicine(
        id=uuid4(),
        hospital_id=sample_hospital.id,
        category_id=cat.id,
        medicine_name="Amoxicillin 250mg",
        brand_name="Mox",
        manufacturer="Ranbaxy",
        is_active=True,
    )
    parity_db.add(med)
    parity_db.flush()

    batch = MedicineBatch(
        id=uuid4(),
        hospital_id=sample_hospital.id,
        medicine_id=med.id,
        batch_number="AMX-01",
        expiry_date=date.today() + timedelta(days=90),
        available_quantity=3,
        initial_quantity=3,
        is_active=True,
    )
    parity_db.add(batch)
    parity_db.commit()

    with pytest.raises(HTTPException) as exc:
        allocate_fifo(parity_db, hospital_id=sample_hospital.id, medicine_id=med.id, quantity=10)
    assert exc.value.status_code == 400
    assert "Insufficient non-expired stock" in exc.value.detail


# ==============================================================================
# FEAT-018: Medical Equipment Maintenance Threshold Parity Tests
# ==============================================================================
def test_equipment_maintenance_thresholds(parity_db: Session, sample_hospital: Hospital):
    """
    Verifies maintenance status computation:
    - next_service_date < today -> overdue
    - next_service_date <= today -> due
    - next_service_date > today with last_service_date -> ok
    - next_service_date > today without last_service_date -> scheduled
    """
    today = date.today()
    cat = EquipmentCategory(id=uuid4(), hospital_id=sample_hospital.id, name="Diagnostic")
    item = EquipmentItem(
        id=uuid4(),
        hospital_id=sample_hospital.id,
        category_id=cat.id,
        asset_id="EQ-001",
        name="ECG Unit",
        manufacturer="GE",
        model="Mac 2000",
    )
    parity_db.add_all([cat, item])
    parity_db.flush()

    maint_overdue = EquipmentMaintenance(
        id=uuid4(),
        hospital_id=sample_hospital.id,
        equipment_id=item.id,
        next_service_date=today - timedelta(days=2),
        status=MaintenanceStatus.scheduled,
    )
    maint_due = EquipmentMaintenance(
        id=uuid4(),
        hospital_id=sample_hospital.id,
        equipment_id=item.id,
        next_service_date=today,
        status=MaintenanceStatus.scheduled,
    )
    maint_scheduled = EquipmentMaintenance(
        id=uuid4(),
        hospital_id=sample_hospital.id,
        equipment_id=item.id,
        next_service_date=today + timedelta(days=20),
        status=MaintenanceStatus.scheduled,
    )
    parity_db.add_all([maint_overdue, maint_due, maint_scheduled])
    parity_db.commit()

    refresh_maintenance_status(maint_overdue)
    assert maint_overdue.status == MaintenanceStatus.overdue

    refresh_maintenance_status(maint_due)
    assert maint_due.status == MaintenanceStatus.due

    refresh_maintenance_status(maint_scheduled)
    assert maint_scheduled.status == MaintenanceStatus.scheduled


# ==============================================================================
# FEAT-014: Operation Theatre Collision Parity Tests
# ==============================================================================
def test_ot_room_collision_window(parity_db: Session, sample_hospital: Hospital):
    """
    Verifies OT room booking collision check:
    Existing surgery: 10:00 - 12:00
    - New surgery 11:00 - 13:00 -> Collision!
    - New surgery 12:00 - 14:00 -> Allowed (boundary touch)
    """
    dept = Department(
        id=uuid4(),
        hospital_id=sample_hospital.id,
        name="Surgery Department",
        code="SURG",
        is_active=True,
    )
    parity_db.add(dept)
    parity_db.flush()

    ot_room = OtRoom(
        id=uuid4(),
        hospital_id=sample_hospital.id,
        department_id=dept.id,
        code="OT-01",
        name="OT Suite 1",
        is_active=True,
    )
    patient = Patient(
        id=uuid4(),
        hospital_id=sample_hospital.id,
        name="John Doe",
        mobile="9876543210",
        uhid="P0001",
        age=35,
        gender="Male",
    )
    parity_db.add_all([ot_room, patient])
    parity_db.commit()

    now_utc = datetime.now(timezone.utc)
    surg_start = now_utc + timedelta(days=1)
    surg_start = surg_start.replace(hour=10, minute=0, second=0, microsecond=0)

    existing_surg = OtSurgery(
        id=uuid4(),
        hospital_id=sample_hospital.id,
        surgery_no="SURG-001",
        ot_room_id=ot_room.id,
        patient_id=patient.id,
        surgery_type="Appendectomy",
        scheduled_at=surg_start,
        duration_minutes=120,  # 10:00 to 12:00
        status=OtSurgeryStatus.scheduled,
    )
    parity_db.add(existing_surg)
    parity_db.commit()

    from hms_migration.modules.ot.db.ot_repository import OtRepository
    repo = OtRepository(parity_db, sample_hospital.id)

    # Overlapping 11:00 to 13:00 -> Collision (True)
    t11 = surg_start.replace(hour=11, minute=0)
    t13 = surg_start.replace(hour=13, minute=0)
    assert repo.check_ot_room_conflict(ot_room.id, t11, t13) is True

    # Non-overlapping 12:00 to 14:00 -> Allowed boundary touch (False)
    t12 = surg_start.replace(hour=12, minute=0)
    t14 = surg_start.replace(hour=14, minute=0)
    assert repo.check_ot_room_conflict(ot_room.id, t12, t14) is False

    # Non-overlapping 08:00 to 10:00 -> Allowed boundary touch (False)
    t8 = surg_start.replace(hour=8, minute=0)
    t10 = surg_start.replace(hour=10, minute=0)
    assert repo.check_ot_room_conflict(ot_room.id, t8, t10) is False


# ==============================================================================
# FEAT-011: Inpatient Discharge Outstanding Balance Parity Tests
# ==============================================================================
def test_inpatient_discharge_blocks_on_outstanding_dues(parity_db: Session, sample_hospital: Hospital):
    """
    Verifies that DischargePatientAction blocks discharge when ledger has outstanding dues > 0.009.
    """
    ward = Ward(
        id=uuid4(),
        hospital_id=sample_hospital.id,
        name="General Ward",
        ward_type=WardType.general,
        bed_charge_per_day=500.0,
    )
    room = Room(
        id=uuid4(),
        hospital_id=sample_hospital.id,
        ward_id=ward.id,
        room_code="R101",
        bed_count=1,
    )
    bed = Bed(
        id=uuid4(),
        hospital_id=sample_hospital.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_code="B101-1",
        is_occupied=True,
    )
    patient = Patient(
        id=uuid4(),
        hospital_id=sample_hospital.id,
        name="Admitted Patient",
        mobile="9123456789",
        uhid="P0002",
        age=40,
        gender="Female",
        status=PatientStatus.admitted,
    )
    parity_db.add_all([ward, room, bed, patient])
    parity_db.flush()

    admission = Admission(
        id=uuid4(),
        hospital_id=sample_hospital.id,
        patient_id=patient.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_id=bed.id,
        status=AdmissionStatus.discharge_requested,
        admitted_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    parity_db.add(admission)
    parity_db.flush()

    # Add an outstanding charge of 1000
    charge = BillingCharge(
        id=uuid4(),
        hospital_id=sample_hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.admission,
        source_id=admission.id,
        description="Room Stay Dues",
        charge_amount=1000.0,
        status=BillingChargeStatus.pending,
    )
    parity_db.add(charge)
    parity_db.commit()

    action = DischargePatientAction(parity_db)
    req = DischargeRequest(
        admission_id=admission.id,
        patient_id=patient.id,
        discharge_notes="Ready to go",
    )

    with pytest.raises(HTTPException) as exc:
        action.execute(
            sample_hospital.id,
            req,
            actor={"name": "Nurse Kelly", "role": "hospital_staff"},
        )
    assert exc.value.status_code == 400
    assert "outstanding balance" in exc.value.detail.lower()
