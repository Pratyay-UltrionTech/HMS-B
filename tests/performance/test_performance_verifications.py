"""
Comprehensive Performance and Atomicity Regression Verification Tests.

Covers Phase 13 requirements:
1. Lab worklist & dashboard bounded query scaling (FLAW / N+1 regression)
2. Radiology worklist bounded query scaling (released_only=True and False)
3. DMS patient file bounded scaling and heavy blob deferral
4. GRN transaction atomicity and single commit
5. Pharmacy sale transaction atomicity and bulk inventory behavior
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from modules.clinical_records.entities.clinical_record import MedicalRecord, PatientDocument, Prescription
from modules.dms.actions.dms_actions import GetDmsPatientFileAction
from modules.dms.contracts.dms_contracts import PatientDocumentCategory
from modules.dms.services.dms_service import build_documents
from modules.doctors.entities.doctor import HospitalUser, StaffRole
from modules.inpatient.entities.admission import Admission, AdmissionStatus, IpdFormSubmission
from modules.inventory.entities.inventory_entities import (
    CentralInventoryStock,
    ConsumableBatch,
    ConsumableCategory,
    ConsumableItem,
    InventoryStockTransaction,
)
from modules.laboratory.actions.laboratory_actions import (
    GetLabDashboardAction,
    ListLabOrdersAction,
    ListLabPrescriptionRequestsAction,
)
from modules.laboratory.entities.lab_entities import (
    LabOrder,
    LabOrderStatus,
    LabPrescriptionRequest,
    LabPrescriptionRequestStatus,
    LabSampleType,
    LabTestCatalog,
)
from modules.masters.entities.organization_entities import Supplier
from modules.patients.entities.patient import Patient
from modules.pharmacy.actions.pharmacy_actions import PharmacyActions
from modules.pharmacy.contracts.pharmacy_contracts import SaleCreate, SaleItemCreate
from modules.pharmacy.entities.pharmacy_entities import (
    Medicine,
    MedicineBatch,
    MedicineInventory,
    PharmacySale,
    PharmacySaleItem,
    PharmacySaleType,
    StockTransaction,
)
from modules.procurement.actions.procurement_actions import ProcurementActions
from modules.procurement.contracts.procurement_contracts import GrnCreate, GrnItemCreate
from modules.procurement.entities.procurement_entities import (
    GoodsReceivedNote,
    GoodsReceivedNoteItem,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
)
from modules.radiology.actions.radiology_actions import ListRadPrescriptionRequestsAction
from modules.radiology.entities.radiology_entities import (
    RadPrescriptionRequest,
    RadPrescriptionRequestStatus,
    RadiologyOrder,
    RadiologyOrderStatus,
)
from modules.tenancy.entities.hospital import Hospital
from tests.performance.test_query_counts import query_counter


# ==============================================================
# 1. Laboratory Dashboard & Worklist Bounded Query Scaling
# ==============================================================================
def test_lab_worklist_and_dashboard_scaling_bounded(db_session: Session, hospital: Hospital):
    """
    Verify that ListLabPrescriptionRequestsAction scales with O(1) database queries,
    preloading financial clearance and exceptions in bulk rather than per row.
    """
    action = ListLabPrescriptionRequestsAction(db_session, hospital.id)
    dashboard_action = GetLabDashboardAction(db_session, hospital.id)

    role = StaffRole(hospital_id=hospital.id, name=f"Doctor {uuid4().hex[:4]}")
    db_session.add(role)
    db_session.flush()

    doc_user = HospitalUser(
        id=uuid4(),
        hospital_id=hospital.id,
        role_id=role.id,
        name="Dr. Lab",
        email=f"drlab_{uuid4().hex[:4]}@hosp.com",
        phone="9876543210",
        password_hash="x",
    )
    db_session.add(doc_user)
    db_session.flush()

    # Populate 10 prescription requests
    for i in range(10):
        pat = Patient(
            hospital_id=hospital.id,
            uhid=f"UHID-LAB-{i}-{uuid4().hex[:4]}",
            name=f"Lab Patient {i}",
            first_name="Lab",
            last_name=f"Patient {i}",
            mobile=f"981000{i:04d}",
        )
        db_session.add(pat)
        db_session.flush()

        rx = Prescription(
            id=uuid4(),
            hospital_id=hospital.id,
            patient_id=pat.id,
            doctor_id=doc_user.id,
            symptoms="Fever",
            diagnosis="Fever",
            medicines="Paracetamol",
            dosage="500mg",
        )
        db_session.add(rx)
        db_session.flush()

        req = LabPrescriptionRequest(
            hospital_id=hospital.id,
            prescription_id=rx.id,
            patient_id=pat.id,
            doctor_id=doc_user.id,
            status=LabPrescriptionRequestStatus.pending,
            clinical_notes=f"Test notes {i}",
            prescribed_test_ids=[],
            prescribed_panel_ids=[],
        )
        db_session.add(req)

    db_session.commit()

    with query_counter(session=db_session) as counter_list:
        results = action.execute()

    assert len(results) == 10
    # Before the fix, N=10 triggered 55 queries. With bulk clearance & db=None, bounded at <= 8 queries.
    assert counter_list.count <= 8, f"Lab worklist executed {counter_list.count} queries, expected <= 8"

    with query_counter(session=db_session) as counter_dash:
        dash = dashboard_action.execute()

    assert dash.pending_doctor_requests >= 0
    assert counter_dash.count <= 15, f"Lab dashboard executed {counter_dash.count} queries, expected <= 15"


# ==============================================================================
# 2. Radiology Worklist Bounded Query Scaling
# ==============================================================================
def test_radiology_worklist_scaling_bounded(db_session: Session, hospital: Hospital):
    """
    Verify that ListRadPrescriptionRequestsAction uses bulk financial resolution
    for both released_only=False and released_only=True paths.
    """
    action = ListRadPrescriptionRequestsAction(db_session, hospital.id)

    role = StaffRole(hospital_id=hospital.id, name=f"Rad Doctor {uuid4().hex[:4]}")
    db_session.add(role)
    db_session.flush()

    doc_user = HospitalUser(
        id=uuid4(),
        hospital_id=hospital.id,
        role_id=role.id,
        name="Dr. Rad",
        email=f"drrad_{uuid4().hex[:4]}@hosp.com",
        phone="9876543211",
        password_hash="x",
    )
    db_session.add(doc_user)
    db_session.flush()

    for i in range(10):
        pat = Patient(
            hospital_id=hospital.id,
            uhid=f"UHID-RAD-{i}-{uuid4().hex[:4]}",
            name=f"Rad Patient {i}",
            first_name="Rad",
            last_name=f"Patient {i}",
            mobile=f"982000{i:04d}",
        )
        db_session.add(pat)
        db_session.flush()

        rx = Prescription(
            id=uuid4(),
            hospital_id=hospital.id,
            patient_id=pat.id,
            doctor_id=doc_user.id,
            symptoms="Cough",
            diagnosis="Cough",
            medicines="Cough Syrup",
            dosage="10ml",
        )
        db_session.add(rx)
        db_session.flush()

        req = RadPrescriptionRequest(
            hospital_id=hospital.id,
            prescription_id=rx.id,
            patient_id=pat.id,
            doctor_id=doc_user.id,
            status=RadPrescriptionRequestStatus.pending,
            clinical_notes=f"Rad notes {i}",
            prescribed_scan_ids=[],
        )
        db_session.add(req)

    db_session.commit()

    with query_counter(session=db_session) as counter_unreleased:
        res_unreleased = action.execute(released_only=False)

    with query_counter(session=db_session) as counter_released:
        res_released = action.execute(released_only=True)

    assert len(res_unreleased) == 10
    assert counter_unreleased.count <= 7, f"Radiology unreleased queries {counter_unreleased.count} > 7"
    assert counter_released.count <= 7, f"Radiology released queries {counter_released.count} > 7"


# ==============================================================================
# 3. DMS Patient File Bounded Scaling & Blob Deferral
# ==============================================================================
def test_dms_patient_file_scaling_and_blob_deferral(db_session: Session, hospital: Hospital):
    """
    Verify that collation of documents in build_documents and build_patient_file
    operates with bounded query count and does NOT lazy-load large binary file_data
    or html_snapshot blobs to compute file presence.
    """
    pat = Patient(
        hospital_id=hospital.id,
        uhid=f"UHID-DMS-{uuid4().hex[:4]}",
        name="DMS Patient",
        first_name="DMS",
        last_name="Patient",
        mobile="9830001234",
    )
    db_session.add(pat)
    db_session.flush()

    for i in range(15):
        doc = PatientDocument(
            hospital_id=hospital.id,
            patient_id=pat.id,
            category=PatientDocumentCategory.other,
            title=f"Document {i}",
            file_name=f"doc_{i}.pdf",
            file_data="A" * 10000,  # 10KB blob
            is_deleted=False,
        )
        db_session.add(doc)

        rec = MedicalRecord(
            hospital_id=hospital.id,
            patient_id=pat.id,
            doctor_id=uuid4(),
            report_type="Lab Report",
            title=f"Lab Record {i}",
            file_name=f"lab_{i}.pdf",
            file_data="B" * 10000,
        )
        db_session.add(rec)

        sub = IpdFormSubmission(
            hospital_id=hospital.id,
            patient_id=pat.id,
            form_id=f"form_{i}",
            form_title=f"Consent Form {i}",
            form_data={},
            html_snapshot="<html/>",
        )
        db_session.add(sub)

    db_session.commit()

    with query_counter(session=db_session) as counter_docs:
        docs = build_documents(db_session, patient_id=pat.id, hospital_id=hospital.id)

    assert len(docs) >= 30
    # Before the fix, accessing default file_data in getattr caused 45+ extra queries.
    # With presence column properties, build_documents executes bounded constant queries (<= 6).
    assert counter_docs.count <= 6, f"build_documents executed {counter_docs.count} queries, expected <= 6"

    # Verify that all documents correctly report has_file=True without loading file_data
    assert all(d.has_file is True for d in docs)

    with query_counter(session=db_session) as counter_file:
        p_file = GetDmsPatientFileAction(db_session, hospital.id).execute(pat.id)

    assert len(p_file.documents) >= 30
    assert counter_file.count <= 30, f"GetDmsPatientFileAction queries {counter_file.count} > 30"


# ==============================================================================
# 4. GRN Transaction Atomicity and Rollback Verification
# ==============================================================================
def test_grn_atomicity_and_single_commit(db_session: Session, hospital: Hospital):
    """
    Verify:
    1. A multi-item GRN commits in a single transaction.
    2. Any failure halfway through a multi-line GRN rolls back completely.
    """
    supplier = Supplier(
        hospital_id=hospital.id,
        name=f"GRN Supplier {uuid4().hex[:4]}",
        contact_person="Contact",
        email="sup@grn.com",
        phone="9988771122",
    )
    db_session.add(supplier)
    db_session.flush()

    items = []
    for i in range(10):
        item = ConsumableItem(
            hospital_id=hospital.id,
            item_code=f"ITM-ATOM-{i+1:03d}",
            item_name=f"Atom Item {i+1}",
            category=ConsumableCategory.surgical_disposable,
            unit_of_issue="box",
            reorder_level=10.0,
        )
        db_session.add(item)
        items.append(item)
    db_session.flush()

    po = PurchaseOrder(
        hospital_id=hospital.id,
        po_number=f"PO-ATOM-{uuid4().hex[:4]}",
        supplier_id=supplier.id,
        status=PurchaseOrderStatus.sent,
        terms="30 days",
    )
    db_session.add(po)
    db_session.flush()

    po_items = []
    for i in range(10):
        poi = PurchaseOrderItem(
            hospital_id=hospital.id,
            purchase_order_id=po.id,
            consumable_item_id=items[i].id,
            quantity_ordered=50.0,
            quantity_received=0.0,
            negotiated_rate=10.0,
        )
        db_session.add(poi)
        po_items.append(poi)
    db_session.commit()

    user = {"id": str(uuid4()), "role": "hospital_admin", "name": "Proc Admin"}
    actions = ProcurementActions(db_session, hospital.id, user)

    # 1. Failure halfway through: item 7 (index 6) attempts over-receipt
    fail_items = []
    for i in range(10):
        qty = 100.0 if i == 6 else 10.0
        fail_items.append(
            GrnItemCreate(
                purchase_order_item_id=po_items[i].id,
                accepted_quantity=qty,
                rejected_quantity=0.0,
                batch_number=f"BATCH-ATOM-{i+1}",
                expiry_date=date(2027, 1, 1),
            )
        )

    payload_fail = GrnCreate(
        purchase_order_id=po.id,
        received_date=date.today(),
        supplier_invoice_number="INV-ATOM-01",
        received_by_staff_id=str(uuid4()),
        items=fail_items,
    )

    with pytest.raises(HTTPException):
        actions.record_grn(payload_fail)

    # Assert complete rollback: zero partial persistence
    grn_count = db_session.query(GoodsReceivedNote).filter(GoodsReceivedNote.hospital_id == hospital.id).count()
    grn_items_count = db_session.query(GoodsReceivedNoteItem).filter(GoodsReceivedNoteItem.hospital_id == hospital.id).count()
    stock_tx_count = db_session.query(InventoryStockTransaction).filter(InventoryStockTransaction.hospital_id == hospital.id).count()
    batches_count = db_session.query(ConsumableBatch).filter(ConsumableBatch.hospital_id == hospital.id).count()

    assert grn_count == 0, f"Expected 0 GRNs after failed receipt, got {grn_count}"
    assert grn_items_count == 0, f"Expected 0 GRN items after rollback, got {grn_items_count}"
    assert stock_tx_count == 0, f"Expected 0 stock transactions after rollback, got {stock_tx_count}"
    assert batches_count == 0, f"Expected 0 batches after rollback, got {batches_count}"

    # 2. Success: all 10 lines valid
    success_items = []
    for i in range(10):
        success_items.append(
            GrnItemCreate(
                purchase_order_item_id=po_items[i].id,
                accepted_quantity=10.0,
                rejected_quantity=0.0,
                batch_number=f"BATCH-OK-{i+1}",
                expiry_date=date(2027, 1, 1),
            )
        )

    payload_ok = GrnCreate(
        purchase_order_id=po.id,
        received_date=date.today(),
        supplier_invoice_number="INV-ATOM-02",
        received_by_staff_id=str(uuid4()),
        items=success_items,
    )

    res = actions.record_grn(payload_ok)
    assert res.grn_number is not None
    assert len(res.items) == 10

    # Verify inventory received
    assert db_session.query(GoodsReceivedNote).filter(GoodsReceivedNote.hospital_id == hospital.id).count() == 1
    assert db_session.query(GoodsReceivedNoteItem).filter(GoodsReceivedNoteItem.hospital_id == hospital.id).count() == 10


# ==============================================================================
# 5. Pharmacy Sale Atomicity and Bulk Stock Verification
# ==============================================================================
def test_pharmacy_sale_atomicity_and_bulk_stock(db_session: Session, hospital: Hospital):
    """
    Verify:
    1. Multi-line sale failure halfway through rolls back completely.
    2. Successful sale decrements stock and refreshes inventory in bulk.
    """
    meds = []
    batches = []
    for i in range(5):
        med = Medicine(
            hospital_id=hospital.id,
            medicine_name=f"Med Atom {i+1}",
            generic_name="Generic Med",
            brand_name=f"Brand {i+1}",
            strength="500mg",
            manufacturer="PharmaCorp",
            unit="tablet",
            selling_price=10.0,
            purchase_price=5.0,
            gst_percent=12.0,
            reorder_level=10,
        )
        db_session.add(med)
        db_session.flush()
        meds.append(med)

        batch = MedicineBatch(
            hospital_id=hospital.id,
            medicine_id=med.id,
            batch_number=f"B-ATOM-{i+1}",
            expiry_date=date(2027, 12, 31),
            initial_quantity=100.0,
            available_quantity=50.0,
            purchase_price=5.0,
            selling_price=10.0,
            is_active=True,
        )
        db_session.add(batch)
        db_session.flush()
        batches.append(batch)

        inv = MedicineInventory(
            hospital_id=hospital.id,
            medicine_id=med.id,
            current_stock=50.0,
            available_stock=50.0,
        )
        db_session.add(inv)

    db_session.commit()

    user = {"id": str(uuid4()), "role": "pharmacist", "name": "Pharmacist"}
    actions = PharmacyActions(db_session, hospital.id, user)

    # 1. Failure halfway through (item 3 requests 999 quantity)
    fail_items = []
    for i in range(5):
        qty = 999.0 if i == 2 else 5.0
        fail_items.append(
            SaleItemCreate(
                medicine_id=meds[i].id,
                batch_id=batches[i].id,
                quantity=qty,
            )
        )

    payload_fail = SaleCreate(
        customer_name="Walkin Fail",
        customer_phone="9999999999",
        sale_type=PharmacySaleType.walk_in,
        payment_method="cash",
        amount_paid=1000.0,
        items=fail_items,
    )

    with pytest.raises(HTTPException):
        actions.create_sale(payload_fail)

    assert db_session.query(PharmacySale).filter(PharmacySale.hospital_id == hospital.id).count() == 0
    assert db_session.query(PharmacySaleItem).filter(PharmacySaleItem.hospital_id == hospital.id).count() == 0
    assert db_session.query(StockTransaction).filter(StockTransaction.hospital_id == hospital.id).count() == 0

    # 2. Success: all 5 items valid
    success_items = []
    for i in range(5):
        success_items.append(
            SaleItemCreate(
                medicine_id=meds[i].id,
                batch_id=batches[i].id,
                quantity=5.0,
            )
        )

    payload_ok = SaleCreate(
        customer_name="Walkin OK",
        customer_phone="9999999999",
        sale_type=PharmacySaleType.walk_in,
        payment_method="cash",
        amount_paid=500.0,
        items=success_items,
    )

    sale_res = actions.create_sale(payload_ok)
    assert sale_res.invoice_number is not None
    assert len(sale_res.items) == 5

    # Verify inventory was decremented
    for i in range(5):
        b = db_session.query(MedicineBatch).filter(MedicineBatch.id == batches[i].id).first()
        inv = db_session.query(MedicineInventory).filter(MedicineInventory.medicine_id == meds[i].id).first()
        assert b.available_quantity == 45.0
        assert inv.available_stock == 45.0
