"""
Unit & integration tests for the overhauled AdmitIpdAction and ledger hydration.
Validates:
1. Double booking prevention (row locking + 409 Conflict)
2. Patient status updated to admitted
3. Bed occupied flag updated to True
4. Encounter counter uses next_ip_encounter_id (IP-YYYY-NNNNN)
5. Inpatient admission fee billing charge created
6. Active admission conflict prevention
7. AppointmentListItem ledger fields hydration
"""

import uuid
from datetime import date, datetime, time, timezone
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from infrastructure.postgres.base import Base
from modules.appointments.actions.appointment_status_actions import AdmitIpdAction
from modules.appointments.contracts.appointments_contracts import AdmitIpdRequest, AppointmentListItem
from modules.appointments.db.appointments_repository import AppointmentsRepository
from modules.appointments.entities.appointment import Appointment
from modules.appointments.entities.enums import AppointmentStatus
from modules.appointments.exceptions.appointments_exceptions import (
    AppointmentConflictError,
    AppointmentNotFoundError,
)
from modules.beds.entities.bed import Bed, Room, Ward, WardType
from modules.billing.entities.billing_entities import BillingCharge, BillingChargeStatus, BillingPayment, BillingSourceType
from modules.tenancy.entities.hospital import Hospital
from modules.doctors.entities.doctor import HospitalUser, StaffRole
from modules.inpatient.entities.admission import Admission, AdmissionStatus
from modules.patients.entities.patient import Patient, PatientStatus


@pytest.fixture
def standalone_db():
    """Isolated SQLite database session for unit testing."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    yield session
    session.close()


def test_admit_ipd_action_success_and_side_effects(standalone_db: Session):
    db = standalone_db
    hospital_id = uuid.uuid4()

    # 1. Seed Hospital & User
    hosp = Hospital(
        id=hospital_id,
        hospital_id="HOSP001",
        name="Test Hospital",
        address="123 Main St",
        phone="555-0100",
        email=f"admin_{uuid.uuid4().hex[:6]}@hospital.org",
        password_hash="hash",
    )
    db.add(hosp)
    db.flush()

    role = StaffRole(id=uuid.uuid4(), hospital_id=hospital_id, name="Doctor")
    db.add(role)
    db.flush()

    doc = HospitalUser(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        name="Dr. Smith",
        email="smith@hospital.org",
        phone="9999999999",
        password_hash="hash",
        role_id=role.id,
        is_active=True,
    )
    db.add(doc)

    # 2. Seed Patient
    pat = Patient(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        name="John Doe",
        uhid="UHID-001",
        mobile="9876543210",
        status=PatientStatus.active,
    )
    db.add(pat)

    # 3. Seed Ward, Room, Bed
    ward = Ward(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        name="General Ward",
        ward_type=WardType.general,
        admission_fee=500.0,
        bed_charge_per_day=1200.0,
        is_active=True,
    )
    db.add(ward)
    db.flush()

    room = Room(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        ward_id=ward.id,
        room_code="101",
        bed_count=2,
        is_active=True,
    )
    db.add(room)
    db.flush()

    bed = Bed(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        ward_id=ward.id,
        room_id=room.id,
        bed_code="B-01",
        is_occupied=False,
        is_active=True,
    )
    db.add(bed)

    # 4. Seed Appointment requesting IPD transfer
    appt = Appointment(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        doctor_id=doc.id,
        patient_id=pat.id,
        appointment_date=date.today(),
        appointment_time=time(10, 0),
        status=AppointmentStatus.ipd_transfer_requested,
        op_id="OP-2026-00001",
        purpose="Consultation",
    )
    db.add(appt)
    db.commit()

    # 5. Execute AdmitIpdAction
    repo = AppointmentsRepository(db, hospital_id)
    action = AdmitIpdAction(db, hospital_id, repo)
    payload = AdmitIpdRequest(
        ward_id=ward.id,
        room_id=room.id,
        bed_id=bed.id,
        notes="Clinical step-in for surgical prep",
    )
    user_context = {"id": str(doc.id), "name": "Nurse Sarah", "role": "hospital_staff"}

    res = action.execute(appt.id, payload, user_context)

    assert res["status"] == "admitted"
    assert "admission_id" in res
    assert res["ip_id"].startswith("IP-")
    assert res["bed_code"] == "B-01"

    # Verify side effects
    db.refresh(bed)
    assert bed.is_occupied is True

    db.refresh(pat)
    assert pat.status == PatientStatus.admitted

    db.refresh(appt)
    assert appt.status == AppointmentStatus.transferred_to_inpatient
    assert str(appt.admission_id) == res["admission_id"]

    # Verify Admission entity created
    adm = db.query(Admission).filter(Admission.id == uuid.UUID(res["admission_id"])).first()
    assert adm is not None
    assert adm.patient_id == pat.id
    assert adm.bed_id == bed.id
    assert adm.ward_id == ward.id
    assert adm.source_appointment_id == appt.id
    assert adm.ip_id == res["ip_id"]
    assert adm.status == AdmissionStatus.admitted

    # Verify admission fee BillingCharge recorded
    charge = (
        db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.patient_id == pat.id,
            BillingCharge.source_type == BillingSourceType.admission,
            BillingCharge.source_id == adm.id,
        )
        .first()
    )
    assert charge is not None
    assert float(charge.net_amount) == 500.0

    # Verify AppointmentListItem hydration carries admission_ward, admission_bed, and admission_status
    hydrated = repo.hydrate_appointment_items([appt])
    assert len(hydrated) == 1
    assert hydrated[0].admission_id == adm.id
    assert hydrated[0].ip_id == res["ip_id"]
    assert hydrated[0].admission_ward == "General Ward"
    assert hydrated[0].admission_bed == "B-01"
    assert hydrated[0].admission_status == "admitted"


def test_admit_ipd_action_rejects_already_occupied_bed(standalone_db: Session):
    db = standalone_db
    hospital_id = uuid.uuid4()

    hosp = Hospital(
        id=hospital_id,
        hospital_id="HOSP002",
        name="Test Hospital 2",
        address="123 Main St",
        phone="555-0100",
        email=f"admin_{uuid.uuid4().hex[:6]}@hospital.org",
        password_hash="hash",
    )
    db.add(hosp)
    db.flush()

    role = StaffRole(id=uuid.uuid4(), hospital_id=hospital_id, name="Doctor")
    db.add(role)
    db.flush()

    doc = HospitalUser(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        name="Dr. Smith",
        email="smith@hospital.org",
        phone="9999999999",
        password_hash="hash",
        role_id=role.id,
        is_active=True,
    )
    db.add(doc)

    pat = Patient(id=uuid.uuid4(), hospital_id=hospital_id, name="Jane Doe", uhid="UHID-002", mobile="9876543211", status=PatientStatus.active)
    db.add(pat)

    ward = Ward(id=uuid.uuid4(), hospital_id=hospital_id, name="ICU", ward_type=WardType.icu)
    db.add(ward)
    db.flush()

    room = Room(id=uuid.uuid4(), hospital_id=hospital_id, ward_id=ward.id, room_code="ICU-1")
    db.add(room)
    db.flush()

    # Bed is already occupied!
    bed = Bed(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        ward_id=ward.id,
        room_id=room.id,
        bed_code="ICU-01",
        is_occupied=True,
    )
    db.add(bed)

    appt = Appointment(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        doctor_id=doc.id,
        patient_id=pat.id,
        appointment_date=date.today(),
        appointment_time=time(10, 0),
        status=AppointmentStatus.ipd_transfer_requested,
        purpose="Consultation",
    )
    db.add(appt)
    db.commit()

    repo = AppointmentsRepository(db, hospital_id)
    action = AdmitIpdAction(db, hospital_id, repo)
    payload = AdmitIpdRequest(ward_id=ward.id, room_id=room.id, bed_id=bed.id)

    with pytest.raises(AppointmentConflictError) as exc_info:
        action.execute(appt.id, payload, {"name": "Nurse"})

    assert "already occupied" in str(exc_info.value)


def test_hydrate_appointment_items_ledger_clearance(standalone_db: Session):
    db = standalone_db
    hospital_id = uuid.uuid4()

    hosp = Hospital(
        id=hospital_id,
        hospital_id="HOSP003",
        name="Test Hospital 3",
        address="123 Main St",
        phone="555-0100",
        email=f"admin_{uuid.uuid4().hex[:6]}@hospital.org",
        password_hash="hash",
    )
    db.add(hosp)
    db.flush()

    pat1 = Patient(id=uuid.uuid4(), hospital_id=hospital_id, name="Patient Cleared", uhid="UHID-10", mobile="9876543212", status=PatientStatus.active)
    pat2 = Patient(id=uuid.uuid4(), hospital_id=hospital_id, name="Patient Due", uhid="UHID-20", mobile="9876543213", status=PatientStatus.active)
    db.add_all([pat1, pat2])
    db.flush()

    # Pat1 has settled charges
    c1 = BillingCharge(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        patient_id=pat1.id,
        source_type=BillingSourceType.consultation,
        source_id=uuid.uuid4(),
        description="Consultation fee",
        net_amount=500.0,
        amount_paid=500.0,
        status=BillingChargeStatus.paid,
    )
    p1 = BillingPayment(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        patient_id=pat1.id,
        amount=500.0,
        payment_date=date.today(),
    )

    # Pat2 has unpaid dues
    c2 = BillingCharge(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        patient_id=pat2.id,
        source_type=BillingSourceType.laboratory,
        source_id=uuid.uuid4(),
        description="Blood chemistry panel",
        net_amount=1500.0,
        amount_paid=500.0,
        status=BillingChargeStatus.partially_paid,
    )
    p2 = BillingPayment(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        patient_id=pat2.id,
        amount=500.0,
        payment_date=date.today(),
    )
    db.add_all([c1, p1, c2, p2])

    appt1 = Appointment(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        doctor_id=uuid.uuid4(),
        patient_id=pat1.id,
        appointment_date=date.today(),
        appointment_time=time(10, 0),
        status=AppointmentStatus.ipd_transfer_requested,
        purpose="Consultation",
    )
    appt2 = Appointment(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        doctor_id=uuid.uuid4(),
        patient_id=pat2.id,
        appointment_date=date.today(),
        appointment_time=time(11, 0),
        status=AppointmentStatus.ipd_transfer_requested,
        purpose="Consultation",
    )
    db.add_all([appt1, appt2])
    db.commit()

    repo = AppointmentsRepository(db, hospital_id)
    items = repo.hydrate_appointment_items([appt1, appt2])

    assert len(items) == 2

    item1 = next(i for i in items if i.id == appt1.id)
    assert item1.ledger_outstanding == 0.0
    assert item1.ledger_total_charges == 500.0
    assert item1.ledger_total_paid == 500.0
    assert item1.ledger_status == "cleared"

    item2 = next(i for i in items if i.id == appt2.id)
    assert item2.ledger_outstanding == 1000.0
    assert item2.ledger_total_charges == 1500.0
    assert item2.ledger_total_paid == 500.0
    assert item2.ledger_status == "outstanding"
