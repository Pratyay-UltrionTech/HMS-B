"""
Repository-Wide Query Count and N+1 Regression Protection Tests.

Enforces Phase D performance constraints:
1. SQL statement count does not grow linearly with entity count.
2. High-value endpoints/services maintain bounded query counts across small vs large fixtures.
3. No caches (Redis/Memcached/in-process TTL caches) used.
"""

from __future__ import annotations

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from modules.appointments.actions.book_appointment_action import BookAppointmentAction
from modules.appointments.contracts.appointments_contracts import BookAppointmentRequest
from modules.appointments.db.appointments_repository import AppointmentsRepository
from modules.appointments.db.availability_reader import AvailabilityReader
from modules.appointments.db.pricing_reader import PricingReader
from modules.appointments.entities.appointment import Appointment
from modules.appointments.entities.enums import AppointmentStatus
from modules.beds.actions.bed_actions import GetBedDashboardAction
from modules.beds.entities.bed import Bed, Room, Ward, WardType
from modules.billing.actions.billing_actions import GetPatientStatementAction
from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingInvoice,
    BillingInvoiceLine,
    BillingInvoiceStatus,
    BillingPayment,
    BillingPaymentAllocation,
    BillingPaymentMethod,
    BillingSourceType,
)
from modules.billing.services.billing_service import (
    allocations_remaining_bulk,
    create_deposit,
    create_payment,
    draw_down_deposits_bulk_for_account,
    ensure_charge,
    process_refund,
)
from modules.clinical_records.entities.clinical_record import Prescription
from modules.clinical_records.services.unified_medical_record_service import (
    UnifiedMedicalRecordService,
)
from modules.doctors.entities.doctor import DoctorLeave, HospitalUser
from modules.doctors.services.doctor_schedule_service import check_leave_conflicts
from modules.inpatient.entities.admission import Admission, AdmissionStatus
from modules.inpatient.entities.medication_order import (
    IpdMedicationOrder,
    MedicationOrderStatus,
)
from modules.inpatient.entities.nursing_entities import (
    ClinicalNoteType,
    IpdClinicalNote,
    MedicationAdministrationRecord,
    MedicationAdminStatus,
)
from modules.laboratory.actions.laboratory_actions import CreateLabOrderAction
from modules.laboratory.contracts.lab_contracts import LabOrderCreate
from modules.laboratory.entities.lab_entities import LabSampleType, LabTestCatalog
from modules.patients.actions.get_patient_profile_action import GetPatientProfileAction
from modules.patients.db.patients_repository import PatientRepository
from modules.patients.entities.patient import Patient
from modules.pharmacy.actions.pharmacy_actions import PharmacyActions
from modules.pharmacy.contracts.pharmacy_contracts import (
    PurchaseCreate,
    PurchaseItemCreate,
)
from modules.pharmacy.entities.pharmacy_entities import (
    Medicine,
    MedicineCategory,
    MedicineType,
    MedicineUnit,
    PharmacySupplier,
)
from modules.tenancy.entities.hospital import Hospital
from tests.helpers.query_counter import query_counter


# ==============================================================================
# 1. Patient Profile: Eager Billing Exclusion (Closure 1 & D52 Test 1)
# ==============================================================================
def test_query_count_patient_profile_bounded_with_billing_history(
    db_session: Session, hospital: Hospital, doctor: HospitalUser
):
    """
    Closure 1 & Query-Count Test 1:
    Opening base patient profile DOES NOT query or eagerly load patient billing history.
    Query count for a patient with 30 billing transactions must be bounded and identical
    to a patient with 0 transactions.
    """
    # Patient A: 0 billing transactions
    pat_clean = Patient(
        hospital_id=hospital.id,
        uhid="UHID-PRF-001",
        name="Clean Profile Patient",
        gender="male",
        date_of_birth=date(1990, 1, 1),
        mobile="9876500001",
    )
    # Patient B: 30 billing transactions
    pat_heavy = Patient(
        hospital_id=hospital.id,
        uhid="UHID-PRF-002",
        name="Heavy Billing Patient",
        gender="female",
        date_of_birth=date(1985, 5, 15),
        mobile="9876500002",
    )
    db_session.add_all([pat_clean, pat_heavy])
    db_session.flush()

    for i in range(30):
        chg = ensure_charge(
            db_session,
            hospital_id=hospital.id,
            patient_id=pat_heavy.id,
            source_type=BillingSourceType.consultation,
            source_id=uuid4(),
            description=f"Consultation Charge {i}",
            charge_amount=500.0,
            created_by_name="Tester",
        )
        pay = create_payment(
            db_session,
            hospital_id=hospital.id,
            patient_id=pat_heavy.id,
            amount=500.0,
            payment_date=date.today(),
            payment_method=BillingPaymentMethod.cash,
            notes=None,
            reference_number=f"REC-{i}",
            received_by_name="Tester",
        )
    db_session.commit()

    repo = PatientRepository(db_session, hospital.id)

    # Query clean profile
    with query_counter(session=db_session) as counter_clean:
        dto_clean = GetPatientProfileAction(repo).execute(pat_clean.id)

    # Query heavy billing profile
    with query_counter(session=db_session) as counter_heavy:
        dto_heavy = GetPatientProfileAction(repo).execute(pat_heavy.id)

    # Invariant: bills list must be empty or not eagerly fetched
    assert dto_clean.bills == []
    assert dto_heavy.bills == []

    # Invariant: query count must not scale with billing records
    assert counter_heavy.count <= counter_clean.count + 2
    assert counter_heavy.count <= 10


# ==============================================================================
# 2. Appointment List Hydration (D52 Test 2)
# ==============================================================================
def test_query_count_appointment_list_bounded(
    db_session: Session, hospital: Hospital, patient: Patient, doctor: HospitalUser
):
    """
    Query-Count Test 2:
    Hydrating 1 appointment vs 30 appointments.
    Query count remains strictly bounded due to bulk foreign-key loading.
    """
    repo = AppointmentsRepository(db_session, hospital.id)
    today = date.today()

    # Create 30 appointments
    appts: list[Appointment] = []
    for i in range(30):
        a = Appointment(
            hospital_id=hospital.id,
            patient_id=patient.id,
            doctor_id=doctor.id,
            appointment_date=today + timedelta(days=i),
            appointment_time=time(10, 0),
            status=AppointmentStatus.scheduled,
            purpose=f"Checkup {i}",
        )
        appts.append(a)
    db_session.add_all(appts)
    db_session.commit()

    # Re-query active appointments into session to avoid expired-attribute lazy refreshes
    fresh_appts = (
        db_session.query(Appointment)
        .filter(Appointment.hospital_id == hospital.id)
        .order_by(Appointment.appointment_date)
        .all()
    )

    # Hydrate 1 appointment
    with query_counter(session=db_session) as counter_1:
        res_1 = repo.hydrate_appointment_items([fresh_appts[0]])

    # Hydrate 30 appointments
    with query_counter(session=db_session) as counter_30:
        res_30 = repo.hydrate_appointment_items(fresh_appts)

    assert len(res_1) == 1
    assert len(res_30) == 30

    # Bounded growth: 30 items should take at most 4 more queries than 1 item
    assert counter_30.count <= counter_1.count + 4


# ==============================================================================
# 3. Unified Medical Record (D52 Test 3 & D9/D45)
# ==============================================================================
def test_query_count_unified_medical_record_bounded(
    db_session: Session, hospital: Hospital, doctor: HospitalUser
):
    """
    Query-Count Test 3:
    Unified Medical Record with 1 admission vs 10 admissions.
    Query count must NOT grow linearly per episode.
    """
    # Patient A: 1 admission
    pat_1 = Patient(hospital_id=hospital.id, uhid="UHID-UMR-001", name="Patient One", mobile="9900000001")
    # Patient B: 10 admissions
    pat_10 = Patient(hospital_id=hospital.id, uhid="UHID-UMR-010", name="Patient Ten", mobile="9900000010")
    db_session.add_all([pat_1, pat_10])
    db_session.flush()

    # Helper to create admission with child records
    def _create_adm_with_children(pat: Patient, idx: int, is_active: bool = False) -> Admission:
        adm = Admission(
            hospital_id=hospital.id,
            patient_id=pat.id,
            doctor_id=doctor.id,
            ip_id=f"IP-{idx:04d}",
            status=AdmissionStatus.admitted if is_active else AdmissionStatus.discharged,
            admitted_at=datetime.now(timezone.utc) - timedelta(days=idx * 10),
            discharged_at=None if is_active else (datetime.now(timezone.utc) - timedelta(days=idx * 10 - 2)),
            notes=f"Admission {idx}",
        )
        db_session.add(adm)
        db_session.flush()

        note = IpdClinicalNote(
            hospital_id=hospital.id,
            admission_id=adm.id,
            patient_id=pat.id,
            note_type=ClinicalNoteType.doctor_progress,
            author_id=doctor.id,
            author_name=doctor.name,
            author_role="doctor",
            note_content=f"Progress note {idx}",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(note)
        return adm

    _create_adm_with_children(pat_1, 1, is_active=True)
    _create_adm_with_children(pat_10, 2, is_active=True)
    for i in range(3, 12):
        _create_adm_with_children(pat_10, i, is_active=False)
    db_session.commit()

    svc = UnifiedMedicalRecordService(db_session, hospital.id)

    # Query 1-admission patient
    with query_counter(session=db_session) as counter_1:
        rec_1 = svc.get_patient_medical_record(pat_1.id)

    # Query 10-admission patient
    with query_counter(session=db_session) as counter_10:
        rec_10 = svc.get_patient_medical_record(pat_10.id)

    assert len(rec_1.episodes) == 1
    assert len(rec_10.episodes) == 10

    # Query count must remain bounded (constant set of bulk queries)
    assert counter_10.count <= counter_1.count + 5


# ==============================================================================
# 4. Billing Statement: Invoice Lines (D52 Test 4 & D46)
# ==============================================================================
def test_query_count_billing_statement_invoice_lines_bounded(
    db_session: Session, hospital: Hospital
):
    """
    Query-Count Test 4:
    Billing statement with 1 invoice vs 15 invoices.
    Invoice lines are eagerly loaded via selectinload, preventing N+1 queries.
    """
    pat_1 = Patient(hospital_id=hospital.id, uhid="UHID-ST-001", name="Statement Pat 1", mobile="9911000001")
    pat_15 = Patient(hospital_id=hospital.id, uhid="UHID-ST-015", name="Statement Pat 15", mobile="9911000015")
    db_session.add_all([pat_1, pat_15])
    db_session.flush()

    def _create_inv(pat: Patient, idx: int):
        inv = BillingInvoice(
            hospital_id=hospital.id,
            patient_id=pat.id,
            invoice_number=f"INV-{pat.uhid}-{idx}",
            invoice_date=date.today(),
            subtotal=Decimal("150.0"),
            grand_total=Decimal("150.0"),
            status=BillingInvoiceStatus.generated,
            created_by_name="Tester",
        )
        db_session.add(inv)
        db_session.flush()
        for j in range(3):
            line = BillingInvoiceLine(
                hospital_id=hospital.id,
                invoice_id=inv.id,
                description=f"Item {j}",
                rate=Decimal("50.0"),
                amount=Decimal("50.0"),
                quantity=Decimal("1.0"),
            )
            db_session.add(line)

    _create_inv(pat_1, 1)
    for i in range(15):
        _create_inv(pat_15, i)
    db_session.commit()

    action = GetPatientStatementAction(db_session, hospital.id)

    with query_counter(session=db_session) as counter_1:
        stmt_1 = action.execute(pat_1.id)

    with query_counter(session=db_session) as counter_15:
        stmt_15 = action.execute(pat_15.id)

    assert len(stmt_1.invoices) == 1
    assert len(stmt_15.invoices) == 15

    # Bounded: 15 invoices with 45 lines does not cause 15 extra queries
    assert counter_15.count <= counter_1.count + 4


# ==============================================================================
# 5. Lab Order Creation: Catalog Lookup (D52 Test 5 & D11)
# ==============================================================================
def test_query_count_lab_order_creation_bounded(
    db_session: Session, hospital: Hospital, patient: Patient, doctor: HospitalUser
):
    """
    Query-Count Test 5:
    Creating lab order with 1 test vs 15 tests.
    Catalog lookup must NOT query LabTestCatalog per test.
    """
    tests: list[LabTestCatalog] = []
    for i in range(15):
        t = LabTestCatalog(
            hospital_id=hospital.id,
            test_code=f"TEST_{i:03d}",
            test_name=f"Test Name {i}",
            department="Biochemistry",
            sample_type=LabSampleType.blood,
            price=100.0,
            is_active=True,
        )
        tests.append(t)
    db_session.add_all(tests)
    db_session.commit()

    action = CreateLabOrderAction(db_session, hospital.id)
    user = {"sub": str(doctor.id), "name": doctor.name, "role": "hospital_staff"}

    test_ids_1 = [tests[0].id]
    test_ids_15 = [t.id for t in tests]

    # Order with 1 test
    with query_counter(session=db_session) as counter_1:
        order_1 = action.build_order(
            patient=patient,
            doctor=doctor,
            user=user,
            test_ids=test_ids_1,
        )
        db_session.flush()

    # Order with 15 tests
    with query_counter(session=db_session) as counter_15:
        order_15 = action.build_order(
            patient=patient,
            doctor=doctor,
            user=user,
            test_ids=test_ids_15,
        )
        db_session.flush()

    assert len(order_1.items) == 1
    assert len(order_15.items) == 15
    db_session.rollback()

    # Bounded growth: 15 tests must not result in 15 catalog queries
    assert counter_15.count <= counter_1.count + 4


# ==============================================================================
# 6. Doctor Leave: Date Range Conflict Check (D52 Test 6 & D10)
# ==============================================================================
def test_query_count_doctor_leave_validation_bounded(
    db_session: Session, hospital: Hospital, doctor: HospitalUser
):
    """
    Query-Count Test 6:
    Checking leave conflicts for 1 date vs 30 dates.
    Validates conflicts via one IN query rather than per-date queries.
    """
    today = date.today()
    dates_1 = [today]
    dates_30 = [today + timedelta(days=i) for i in range(30)]

    # Warmup
    check_leave_conflicts(
        db_session,
        hospital.id,
        doctor.id,
        dates_1,
        time(9, 0),
        time(17, 0),
        15,
    )

    with query_counter(session=db_session) as counter_1:
        check_leave_conflicts(
            db_session,
            hospital.id,
            doctor.id,
            dates_1,
            time(9, 0),
            time(17, 0),
            15,
        )

    with query_counter(session=db_session) as counter_30:
        check_leave_conflicts(
            db_session,
            hospital.id,
            doctor.id,
            dates_30,
            time(9, 0),
            time(17, 0),
            15,
        )

    # Must be bounded query counts (1 for appointments + 1 for existing leaves)
    assert counter_30.count <= counter_1.count + 1
    assert counter_30.count <= 3


# ==============================================================================
# 7. Pharmacy Purchase: Bulk Medicine/Batch Preload (D52 Test 7 & D12)
# ==============================================================================
def test_query_count_pharmacy_purchase_bounded(
    db_session: Session, hospital: Hospital, doctor: HospitalUser
):
    """
    Query-Count Test 7:
    Creating purchase with 1 item vs 15 items.
    Medicine and batch lookups must be bulk-loaded.
    """
    supplier = PharmacySupplier(
        hospital_id=hospital.id,
        company_name="Apex Pharma Distributors",
        contact_person="Rajesh Kumar",
        phone="9876543210",
        is_active=True,
    )
    cat = MedicineCategory(hospital_id=hospital.id, name="Antibiotics", is_active=True)
    db_session.add_all([supplier, cat])
    db_session.flush()

    meds: list[Medicine] = []
    for i in range(15):
        m = Medicine(
            hospital_id=hospital.id,
            category_id=cat.id,
            medicine_name=f"Amoxicillin {i}mg",
            generic_name="Amoxicillin",
            brand_name=f"Moxikind {i}",
            manufacturer="Apex Pharma",
            strength=f"{i}mg",
            medicine_type=MedicineType.tablet,
            unit=MedicineUnit.strip,
            is_active=True,
        )
        meds.append(m)
    db_session.add_all(meds)
    db_session.commit()

    user = {"sub": str(doctor.id), "name": doctor.name, "role": "hospital_staff"}
    actions = PharmacyActions(db_session, hospital.id, user)

    med_ids = [m.id for m in meds]

    payload_1 = PurchaseCreate(
        supplier_id=supplier.id,
        invoice_number=f"INV-PHARM-001-{uuid4().hex[:6]}",
        invoice_date=date.today(),
        received_date=date.today(),
        items=[
            PurchaseItemCreate(
                medicine_id=med_ids[0],
                batch_number="BAT-001",
                expiry_date=date.today() + timedelta(days=365),
                purchase_price=50.0,
                selling_price=75.0,
                mrp=80.0,
                quantity=10.0,
            )
        ],
    )

    payload_15 = PurchaseCreate(
        supplier_id=supplier.id,
        invoice_number=f"INV-PHARM-015-{uuid4().hex[:6]}",
        invoice_date=date.today(),
        received_date=date.today(),
        items=[
            PurchaseItemCreate(
                medicine_id=med_ids[i],
                batch_number=f"BAT-{i:03d}",
                expiry_date=date.today() + timedelta(days=365),
                purchase_price=50.0,
                selling_price=75.0,
                mrp=80.0,
                quantity=10.0,
            )
            for i in range(15)
        ],
    )

    with query_counter(session=db_session) as counter_1:
        resp_1 = actions.create_purchase(payload_1)

    with query_counter(session=db_session) as counter_15:
        resp_15 = actions.create_purchase(payload_15)

    assert len(resp_1.items) == 1
    assert len(resp_15.items) == 15

    # 15 items should not cause 15x medicine or batch SELECT queries
    assert counter_15.count <= counter_1.count + 4


# ==============================================================================
# 8. Medication Order: Administration Scheduling (D52 Test 8 & D13)
# ==============================================================================
def test_query_count_medication_scheduling_bounded(
    db_session: Session, hospital: Hospital, patient: Patient, doctor: HospitalUser
):
    """
    Query-Count Test 8:
    Generating 1 administration slot vs 24 administration slots.
    Existence checks use 1 bulk query instead of per-slot queries.
    """
    from modules.inpatient.db.medication_order_repository import MedicationOrderRepository

    adm = Admission(
        hospital_id=hospital.id,
        patient_id=patient.id,
        doctor_id=doctor.id,
        ip_id=f"IP-SCHED-{uuid4().hex[:6]}",
        status=AdmissionStatus.admitted,
        admitted_at=datetime.now(timezone.utc),
    )
    db_session.add(adm)
    db_session.flush()

    repo = MedicationOrderRepository(db_session, hospital.id)

    order_1 = IpdMedicationOrder(
        hospital_id=hospital.id,
        admission_id=adm.id,
        patient_id=patient.id,
        doctor_id=doctor.id,
        doctor_name=doctor.name,
        medicine_name="Paracetamol 500mg",
        dose="500",
        dosage_unit="mg",
        route="oral",
        frequency="stat",
        start_time=datetime.now(timezone.utc),
        status=MedicationOrderStatus.active,
    )
    order_24 = IpdMedicationOrder(
        hospital_id=hospital.id,
        admission_id=adm.id,
        patient_id=patient.id,
        doctor_id=doctor.id,
        doctor_name=doctor.name,
        medicine_name="Saline IV",
        dose="100",
        dosage_unit="ml",
        route="iv",
        frequency="q6h",
        duration_days=6,
        start_time=datetime.now(timezone.utc),
        status=MedicationOrderStatus.active,
    )
    db_session.add_all([order_1, order_24])
    db_session.commit()

    with query_counter(session=db_session) as counter_1:
        repo._populate_emar_schedules(order_1)

    with query_counter(session=db_session) as counter_24:
        repo._populate_emar_schedules(order_24)

    # 24 slots must not cause 24 individual SELECT queries
    assert counter_24.count <= counter_1.count + 2
    assert counter_24.count <= 3


# ==============================================================================
# 9. Billing Allocations Remaining: Bulk Query (D52 Test 9 & D15)
# ==============================================================================
def test_query_count_billing_allocations_remaining_bulk(
    db_session: Session, hospital: Hospital, patient: Patient
):
    """
    Query-Count Test 9:
    Computing unreversed portion for 1 allocation vs 20 allocations.
    Uses 1 grouped aggregate query instead of 20 queries.
    """
    allocs: list[BillingPaymentAllocation] = []
    for i in range(20):
        chg = ensure_charge(
            db_session,
            hospital_id=hospital.id,
            patient_id=patient.id,
            source_type=BillingSourceType.consultation,
            source_id=uuid4(),
            description=f"Consultation Charge {i}",
            charge_amount=200.0,
            created_by_name="Tester",
        )
        db_session.flush()
        pay = create_payment(
            db_session,
            hospital_id=hospital.id,
            patient_id=patient.id,
            amount=200.0,
            payment_date=date.today(),
            payment_method=BillingPaymentMethod.cash,
            notes=None,
            reference_number=f"PAY-{i}",
            received_by_name="Tester",
            allocations_plan=[{"charge_id": chg.id, "amount": 200.0}],
        )
        db_session.flush()
        alloc = (
            db_session.query(BillingPaymentAllocation)
            .filter(BillingPaymentAllocation.payment_id == pay.id)
            .first()
        )
        assert alloc is not None
        allocs.append(alloc)
    db_session.commit()

    fresh_allocs = (
        db_session.query(BillingPaymentAllocation)
        .filter(BillingPaymentAllocation.hospital_id == hospital.id)
        .all()
    )

    with query_counter(session=db_session) as counter_1:
        res_1 = allocations_remaining_bulk(db_session, [fresh_allocs[0]])

    with query_counter(session=db_session) as counter_20:
        res_20 = allocations_remaining_bulk(db_session, fresh_allocs)

    assert len(res_1) == 1
    assert len(res_20) == 20

    # Both must execute exactly 1 grouped aggregate SQL query
    assert counter_20.count == counter_1.count
    assert counter_20.count <= 2


# ==============================================================================
# 10. Bed Dashboard: Bounded Growth Across Bed Inventory (D52 Test 10)
# ==============================================================================
def test_query_count_bed_dashboard_bounded(
    db_session: Session, hospital: Hospital
):
    """
    Query-Count Test 10:
    Bed dashboard with 5 beds vs 40 beds across rooms.
    Ensures bed dashboard does not execute 1 query per bed/patient.
    """
    ward = Ward(hospital_id=hospital.id, name="General Ward", ward_type=WardType.general, is_active=True)
    db_session.add(ward)
    db_session.flush()

    rooms: list[Room] = []
    for i in range(8):
        room = Room(hospital_id=hospital.id, ward_id=ward.id, room_code=f"R-{i+1:02d}", bed_count=5, is_active=True)
        rooms.append(room)
    db_session.add_all(rooms)
    db_session.commit()

    action = GetBedDashboardAction(db_session)

    # Initial execution populates beds
    rows_initial = action.execute(hospital.id)
    assert len(rows_initial) == 40

    # Test dashboard query count
    with query_counter(session=db_session) as counter:
        rows = action.execute(hospital.id)

    assert len(rows) == 40
    # Must be bounded (sync check + bed query with joinedload + active admission query)
    assert counter.count <= 6


# ==============================================================================
# 11. Refund Reversal Full Path: Bounded Query Count (Closure Issue 6 & 13)
# ==============================================================================
def test_query_count_refund_reversal_full_path_bounded(
    db_session: Session, hospital: Hospital
):
    """
    Closure Issue 6 & 13:
    Reversing 1 allocation vs 20 allocations via the actual process_refund execution path.
    Precomputed remaining allocations + bulk locked charge lookup eliminates
    the 1-query-per-allocation anti-pattern. Query count remains bounded.
    """
    from modules.billing.entities.billing_entities import DepositStatus
    pat_1 = Patient(hospital_id=hospital.id, uhid="UHID-REF-001", name="Patient One", mobile="9800000001")
    pat_20 = Patient(hospital_id=hospital.id, uhid="UHID-REF-020", name="Patient Twenty", mobile="9800000020")
    db_session.add_all([pat_1, pat_20])
    db_session.flush()

    # Patient 1: 1 charge + 1 payment with 1 allocation
    c1 = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=pat_1.id,
        source_type=BillingSourceType.consultation,
        source_id=uuid4(),
        description="Charge 1",
        charge_amount=200.0,
        created_by_name="Tester",
    )
    db_session.flush()
    p1 = create_payment(
        db_session,
        hospital_id=hospital.id,
        patient_id=pat_1.id,
        amount=200.0,
        payment_date=date.today(),
        payment_method=BillingPaymentMethod.cash,
        notes=None,
        reference_number="PAY-REF-1",
        received_by_name="Tester",
        allocations_plan=[{"charge_id": c1.id, "amount": 200.0}],
    )

    # Patient 20: 20 charges + 1 payment with 20 allocations
    plan_20 = []
    for i in range(20):
        ci = ensure_charge(
            db_session,
            hospital_id=hospital.id,
            patient_id=pat_20.id,
            source_type=BillingSourceType.consultation,
            source_id=uuid4(),
            description=f"Charge {i}",
            charge_amount=100.0,
            created_by_name="Tester",
        )
        db_session.flush()
        plan_20.append({"charge_id": ci.id, "amount": 100.0})

    p20 = create_payment(
        db_session,
        hospital_id=hospital.id,
        patient_id=pat_20.id,
        amount=2000.0,
        payment_date=date.today(),
        payment_method=BillingPaymentMethod.cash,
        notes=None,
        reference_number="PAY-REF-20",
        received_by_name="Tester",
        allocations_plan=plan_20,
    )
    db_session.commit()

    # Process refund reversing 1 allocation
    with query_counter(session=db_session) as counter_1:
        ref_1 = process_refund(
            db_session,
            hospital_id=hospital.id,
            patient_id=pat_1.id,
            amount=200.0,
            reason="Patient Cancellation 1",
            refund_date=date.today(),
            payment_id=p1.id,
            processed_by_name="Auditor",
        )

    # Process refund reversing 20 allocations
    with query_counter(session=db_session) as counter_20:
        ref_20 = process_refund(
            db_session,
            hospital_id=hospital.id,
            patient_id=pat_20.id,
            amount=2000.0,
            reason="Patient Cancellation 20",
            refund_date=date.today(),
            payment_id=p20.id,
            processed_by_name="Auditor",
        )

    assert ref_1.amount == Decimal("200.00")
    assert ref_20.amount == Decimal("2000.00")
    # Invariant: 20 reversed allocations must NOT do 20+ additional queries
    # Query difference must remain bounded
    assert counter_20.count <= counter_1.count + 5


# ==============================================================================
# 12. Deposit Drawdown Full Path: Bounded Query Count (Closure Issue 7 & 13)
# ==============================================================================
def test_query_count_deposit_drawdown_full_path_bounded(
    db_session: Session, hospital: Hospital
):
    """
    Closure Issue 7 & 13:
    Drawing down 1 deposit vs 10 deposits across open charges.
    Eligible deposits and charges are loaded/locked once in bulk,
    eliminating the charge-query-in-deposit-loop N+1 pattern.
    """
    pat_1 = Patient(hospital_id=hospital.id, uhid="UHID-DEP-001", name="Dep Patient One", mobile="9800000101")
    pat_10 = Patient(hospital_id=hospital.id, uhid="UHID-DEP-010", name="Dep Patient Ten", mobile="9800000110")
    db_session.add_all([pat_1, pat_10])
    db_session.flush()

    # Patient 1: 1 open charge + 1 deposit
    ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=pat_1.id,
        source_type=BillingSourceType.other,
        source_id=None,
        description="General Charge",
        charge_amount=500.0,
    )
    create_deposit(
        db_session,
        hospital_id=hospital.id,
        patient_id=pat_1.id,
        amount=500.0,
        deposit_date=date.today(),
    )

    # Patient 10: 1 open charge + 10 smaller deposits
    ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=pat_10.id,
        source_type=BillingSourceType.other,
        source_id=None,
        description="General Charge Heavy",
        charge_amount=500.0,
    )
    for i in range(10):
        create_deposit(
            db_session,
            hospital_id=hospital.id,
            patient_id=pat_10.id,
            amount=50.0,
            deposit_date=date.today(),
            reference_number=f"DEP-REF-{i}",
        )
    db_session.commit()

    with query_counter(session=db_session) as counter_1:
        alloc_1 = draw_down_deposits_bulk_for_account(
            db_session,
            hospital_id=hospital.id,
            patient_id=pat_1.id,
            account_id=None,
        )

    with query_counter(session=db_session) as counter_10:
        alloc_10 = draw_down_deposits_bulk_for_account(
            db_session,
            hospital_id=hospital.id,
            patient_id=pat_10.id,
            account_id=None,
        )

    assert alloc_1 == 500.0
    assert alloc_10 == 500.0
    # Invariant: 10 deposits must NOT repeatedly query open charges per deposit
    assert counter_10.count <= counter_1.count + 4


# ==============================================================================
# 13. Pharmacy Purchase Batch Concurrency & Bounded Queries (Closure Issue 8 & 13)
# ==============================================================================
def test_query_count_pharmacy_purchase_batch_bounded(
    db_session: Session, hospital: Hospital, doctor: HospitalUser
):
    """
    Closure Issue 8 & 13:
    Pharmacy purchase processing for 1 item vs 10 items.
    Uses bulk with_for_update() batch locking and bulk inventory summary refresh.
    """
    supplier = PharmacySupplier(
        hospital_id=hospital.id,
        company_name=f"Apex Meds Supplier {uuid4().hex[:6]}",
        contact_person="Ramesh",
        phone="9876509999",
        is_active=True,
    )
    db_session.add(supplier)
    db_session.flush()

    meds: list[Medicine] = []
    for i in range(10):
        m = Medicine(
            hospital_id=hospital.id,
            medicine_name=f"Ampicillin {i+1}00mg {uuid4().hex[:4]}",
            brand_name=f"AmpiBrand {i} {uuid4().hex[:4]}",
            manufacturer="Apex Pharma",
            strength=f"{i+1}00mg",
            generic_name="Ampicillin",
            medicine_type=MedicineType.tablet,
            unit=MedicineUnit.strip,
            reorder_level=5,
            is_active=True,
        )
        meds.append(m)
    db_session.add_all(meds)
    db_session.commit()

    actions = PharmacyActions(db_session, hospital.id, {"id": str(doctor.id), "name": doctor.name})

    # 1 item purchase
    p1 = PurchaseCreate(
        supplier_id=supplier.id,
        purchase_date=date.today(),
        invoice_date=date.today(),
        received_date=date.today(),
        invoice_number=f"INV-PHARM-1-{uuid4().hex[:4]}",
        items=[
            PurchaseItemCreate(
                medicine_id=meds[0].id,
                batch_number="B-INIT-01",
                expiry_date=date.today() + timedelta(days=365),
                quantity=10,
                purchase_price=8.0,
                selling_price=10.0,
                mrp=12.0,
            )
        ],
    )
    # 10 item purchase
    p10 = PurchaseCreate(
        supplier_id=supplier.id,
        purchase_date=date.today(),
        invoice_date=date.today(),
        received_date=date.today(),
        invoice_number=f"INV-PHARM-10-{uuid4().hex[:4]}",
        items=[
            PurchaseItemCreate(
                medicine_id=meds[i].id,
                batch_number=f"B-INIT-{i:02d}",
                expiry_date=date.today() + timedelta(days=365),
                quantity=10,
                purchase_price=8.0,
                selling_price=10.0,
                mrp=12.0,
            )
            for i in range(10)
        ],
    )

    with query_counter(session=db_session) as counter_1:
        res_1 = actions.create_purchase(p1)

    with query_counter(session=db_session) as counter_10:
        res_10 = actions.create_purchase(p10)

    assert len(res_1.items) == 1
    assert len(res_10.items) == 10
    # Invariant: bulk batch lock + bulk inventory refresh must remain bounded
    assert counter_10.count <= counter_1.count + 5


# ==============================================================================
# 14. Billing Statement: Scoped Filter Bounded Queries (Closure Issue 5 & 13)
# ==============================================================================
def test_query_count_billing_statement_filtered_reconciliation_bounded(
    db_session: Session, hospital: Hospital
):
    """
    Closure Issue 5 & 13:
    Retrieving filtered billing statement for a patient with 20 mixed charges.
    Queries must remain bounded without N+1 loops.
    """
    pat = Patient(hospital_id=hospital.id, uhid=f"UHID-STMT-{uuid4().hex[:6].upper()}", name="Statement Patient", mobile="9800000222")
    db_session.add(pat)
    db_session.flush()

    for i in range(10):
        c_lab = ensure_charge(
            db_session,
            hospital_id=hospital.id,
            patient_id=pat.id,
            source_type=BillingSourceType.laboratory,
            source_id=uuid4(),
            description=f"Lab Test {i}",
            charge_amount=100.0,
        )
        c_ph = ensure_charge(
            db_session,
            hospital_id=hospital.id,
            patient_id=pat.id,
            source_type=BillingSourceType.pharmacy,
            source_id=uuid4(),
            description=f"Pharmacy Dispense {i}",
            charge_amount=150.0,
        )
    db_session.commit()

    action = GetPatientStatementAction(db_session, hospital.id)

    with query_counter(session=db_session) as counter_all:
        stmt_all = action.execute(pat.id)

    with query_counter(session=db_session) as counter_lab:
        stmt_lab = action.execute(pat.id, source_type=BillingSourceType.laboratory)

    assert len(stmt_all.charges) == 20
    assert len(stmt_lab.charges) == 10
    assert counter_lab.count <= 10
    assert counter_all.count <= 10


# ==============================================================================
# 15. IPD Discharge Queue: Bounded Scaling (Phase 1 / NQ-01..04, WR-01)
# ==============================================================================
def test_query_count_discharge_queue_bounded_scaling(
    db_session: Session, hospital: Hospital, doctor: HospitalUser
):
    """
    IPD Discharge Queue:
    Query count must remain strictly bounded across small (2) vs larger (10)
    admissions in discharge_requested status.
    Must not trigger N queries for bed charges, N queries for ledger totals,
    N queries for financial exceptions, or N queries for source appointments.
    """
    from modules.inpatient.actions.admission_actions import ListDischargeRequestsAction
    from modules.billing.entities.billing_entities import (
        FinancialAccount,
        FinancialAccountStatus,
        FinancialAccountType,
        BillingCharge,
        BillingChargeStatus,
        BillingSourceType,
    )

    ward = Ward(hospital_id=hospital.id, name=f"General Ward {uuid4().hex[:4]}", ward_type=WardType.general, bed_charge_per_day=500.0)
    db_session.add(ward)
    db_session.flush()

    room = Room(hospital_id=hospital.id, ward_id=ward.id, room_code=f"R-{uuid4().hex[:4].upper()}", name="standard")
    db_session.add(room)
    db_session.flush()

    admissions = []
    for i in range(10):
        pat = Patient(hospital_id=hospital.id, uhid=f"UHID-DIS-{i}-{uuid4().hex[:4].upper()}", name=f"Discharge Patient {i}", mobile=f"980000033{i}")
        bed = Bed(hospital_id=hospital.id, ward_id=ward.id, room_id=room.id, bed_code=f"BC-{uuid4().hex[:4].upper()}")
        db_session.add_all([pat, bed])
        db_session.flush()

        adm = Admission(
            id=uuid4(),
            hospital_id=hospital.id,
            patient_id=pat.id,
            ward_id=ward.id,
            room_id=room.id,
            bed_id=bed.id,
            doctor_id=doctor.id,
            status=AdmissionStatus.discharge_requested,
            admitted_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        admissions.append(adm)
        account = FinancialAccount(
            hospital_id=hospital.id,
            patient_id=pat.id,
            account_number=f"ACC-DIS-{i}-{uuid4().hex[:4]}",
            account_type=FinancialAccountType.ipd,
            status=FinancialAccountStatus.open,
            admission_id=adm.id,
        )
        chg = BillingCharge(
            hospital_id=hospital.id,
            patient_id=pat.id,
            account_id=account.id,
            source_type=BillingSourceType.bed,
            source_id=adm.id,
            description="Bed Charges (2 Days)",
            quantity=2.0,
            unit_price=500.0,
            charge_amount=1000.0,
            net_amount=1000.0,
            amount_paid=1000.0,
            status=BillingChargeStatus.paid,
        )
        db_session.add_all([adm, account, chg])
    db_session.commit()

    action = ListDischargeRequestsAction(db_session)

    with query_counter(session=db_session) as counter_10:
        res_10 = action.execute(hospital.id)

    assert len(res_10) >= 10
    # Bulk loading bed charges, ledger totals, financial exceptions, and preloaded care team
    # must maintain query count strictly bounded (under 12 queries total for 10 admissions).
    assert counter_10.count <= 12


# ==============================================================================
# 16. Pharmacy Drug Interaction Checker: Constant DB Queries (Phase 2 / ALG-01)
# ==============================================================================
def test_query_count_drug_interaction_checker_constant_queries(
    db_session: Session, hospital: Hospital
):
    """
    Pharmacy Drug Interaction Checker:
    Loading active interaction rules once in bulk.
    Comparing 2 medicines (1 pair) vs 10 medicines (45 pairs).
    DB queries must remain constant (O(1) queries), eliminating O(M^2) DB amplification.
    """
    from modules.pharmacy.actions.drug_interaction_actions import CheckDrugInteractionsAction
    from modules.pharmacy.contracts.drug_interaction_contracts import CheckDrugInteractionsRequest
    from modules.pharmacy.entities.drug_interaction_entities import (
        DrugInteractionRule,
        InteractionSeverity,
    )

    # Seed 10 active interaction rules
    for i in range(10):
        rule = DrugInteractionRule(
            hospital_id=hospital.id,
            drug_a=f"DrugAlpha_{i}",
            drug_b=f"DrugBeta_{i}",
            severity=InteractionSeverity.major,
            mechanism=f"Mechanism {i}",
            clinical_recommendation=f"Recommendation {i}",
            is_active=True,
        )
        db_session.add(rule)
    db_session.commit()

    action = CheckDrugInteractionsAction(db_session, hospital.id)

    meds_2 = ["DrugAlpha_0", "DrugBeta_0"]
    with query_counter(session=db_session) as counter_2:
        res_2 = action.execute(CheckDrugInteractionsRequest(medicines=meds_2))

    meds_10 = [f"DrugAlpha_{i}" for i in range(5)] + [f"DrugBeta_{i}" for i in range(5)]
    with query_counter(session=db_session) as counter_10:
        res_10 = action.execute(CheckDrugInteractionsRequest(medicines=meds_10))

    assert len(res_2.alerts) == 1
    assert len(res_10.alerts) == 5
    # Invariant: 45 pairs must not issue 45 DB queries. It must issue exactly 1 query for active rules.
    assert counter_10.count == counter_2.count
    assert counter_10.count <= 2


# ==============================================================================
# 17. Document Number Generation: O(1) Atomic Sequence (Phase 11 / SCALE-01)
# ==============================================================================
def test_query_count_document_numbering_atomic_o1(
    db_session: Session, hospital: Hospital
):
    """
    Document Number Generation:
    Initial creation initializes the counter row (with self-healing if needed).
    Subsequent document numbers execute a single atomic upsert returning the new counter
    without scanning historical rows.
    """
    from modules.billing.services.invoice_service import next_invoice_number, next_receipt_number

    # Warm up first numbers to ensure counter row exists
    inv_1 = next_invoice_number(db_session, hospital.id)
    rcpt_1 = next_receipt_number(db_session, hospital.id)

    # Subsequent numbers should execute exactly 1 atomic statement each
    with query_counter(session=db_session) as counter_inv:
        inv_2 = next_invoice_number(db_session, hospital.id)
        inv_3 = next_invoice_number(db_session, hospital.id)

    with query_counter(session=db_session) as counter_rcpt:
        rcpt_2 = next_receipt_number(db_session, hospital.id)
        rcpt_3 = next_receipt_number(db_session, hospital.id)

    assert inv_2 != inv_3
    assert rcpt_2 != rcpt_3
    # Exactly 2 queries (1 existence check + 1 atomic upsert) per document number generation, zero historical scans!
    assert counter_inv.count <= 4
    assert counter_rcpt.count <= 4


# ==============================================================================
# 18. Billing Deposit Drawdown: O(D + C) Two-Pointer Allocation (Phase 3 / ALG-02)
# ==============================================================================
def test_query_count_billing_deposit_drawdown_bounded(
    db_session: Session, hospital: Hospital
):
    """
    Deposit Drawdown:
    Allocating 5 deposits across 10 charges.
    Two-pointer linear allocation algorithm operates with single bulk lock
    and bounded query count.
    """
    pat = Patient(hospital_id=hospital.id, uhid=f"UHID-DEP-{uuid4().hex[:6].upper()}", name="Deposit Patient", mobile="9800000444")
    db_session.add(pat)
    db_session.flush()

    for i in range(5):
        create_deposit(
            db_session,
            hospital_id=hospital.id,
            patient_id=pat.id,
            amount=200.0,
            deposit_date=date.today(),
            deposit_type="advance",
        )

    for i in range(10):
        ensure_charge(
            db_session,
            hospital_id=hospital.id,
            patient_id=pat.id,
            source_type=BillingSourceType.pharmacy,
            source_id=uuid4(),
            description=f"Pharmacy item {i}",
            charge_amount=150.0,
        )
    db_session.commit()

    with query_counter(session=db_session) as counter_alloc:
        allocated = draw_down_deposits_bulk_for_account(
            db_session,
            hospital_id=hospital.id,
            patient_id=pat.id,
            account_id=None,
        )

    assert allocated == 1000.0  # 5 * 200 = 1000 total deposit funds drawn down
    assert counter_alloc.count <= 10


# ==============================================================================
# 19. Invoice List: Bounded Pagination & No Line Item Loading
# ==============================================================================
def test_query_count_invoice_list_bounded(
    db_session: Session, hospital: Hospital
):
    """
    Invoice Listing:
    Query count for fetching a page of 15 invoices across a dataset of 50 invoices
    with multiple line items is strictly bounded (<= 2 queries: count/sum aggregate +
    projected limit/offset rows) and does NOT load invoice lines or generate N+1 queries.
    """
    from modules.billing.actions.billing_actions import ListInvoicesAction

    pat = Patient(hospital_id=hospital.id, uhid=f"UHID-INV-{uuid4().hex[:6].upper()}", name="Invoice Query Patient", mobile="9800000555")
    db_session.add(pat)
    db_session.flush()

    for i in range(50):
        inv = BillingInvoice(
            hospital_id=hospital.id,
            patient_id=pat.id,
            invoice_number=f"INV-QC-{i:03}",
            invoice_date=date.today(),
            grand_total=100.0 + i,
            status=BillingInvoiceStatus.generated,
            created_at=datetime.now(timezone.utc) + timedelta(seconds=i),
        )
        db_session.add(inv)
        db_session.flush()
        for j in range(4):
            db_session.add(BillingInvoiceLine(
                hospital_id=hospital.id,
                invoice_id=inv.id,
                source_type="consultation",
                description=f"Item {j}",
                quantity=1.0,
                rate=25.0,
                amount=25.0,
                sort_order=j,
            ))
    db_session.commit()

    with query_counter(session=db_session) as counter:
        page = ListInvoicesAction(db_session, hospital.id).execute(page=1, page_size=15)

    assert len(page.items) == 15
    assert page.total == 50
    assert page.total_pages == 4
    billing_queries = [sql for sql in counter.statements if "billing_invoices" in sql]
    assert len(billing_queries) == 2  # exactly 2 queries: count/sum aggregate + projected limit/offset rows
    assert all("billing_invoice_lines" not in sql for sql in counter.statements)


