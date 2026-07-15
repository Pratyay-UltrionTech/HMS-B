from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.models import PatientDocumentCategory


class DmsPatientItem(BaseModel):
    id: UUID
    uhid: str
    name: str
    mobile: str
    email: str | None = None
    gender: str | None = None
    age: int | None = None
    care_status: str  # OPD | IPD | Inactive
    status: str
    last_visit: date | None = None
    created_at: datetime


class DmsTimelineEvent(BaseModel):
    id: str
    event_type: str
    title: str
    detail: str | None = None
    occurred_at: datetime
    source_module: str
    entity_id: str | None = None
    view_path: str | None = None


class DmsDocumentCreate(BaseModel):
    category: PatientDocumentCategory = PatientDocumentCategory.other
    title: str = Field(min_length=1, max_length=255)
    notes: str | None = None
    file_name: str | None = None
    file_data: str | None = None


class DmsDocumentResponse(BaseModel):
    id: UUID
    patient_id: UUID
    category: PatientDocumentCategory
    title: str
    notes: str | None
    file_name: str | None
    has_file: bool = False
    uploaded_by_name: str
    uploaded_by_role: str
    created_at: datetime
    source: str = "dms"  # dms | medical_record
    doctor_name: str | None = None
    report_type: str | None = None

    model_config = {"from_attributes": True}


class DmsPrescriptionItem(BaseModel):
    id: UUID
    doctor_id: UUID
    doctor_name: str | None
    diagnosis: str
    medicines: str
    dosage: str
    advice: str | None
    created_at: datetime
    pdf_path: str


class DmsLabItem(BaseModel):
    id: UUID
    order_no: str
    status: str
    test_names: str | None
    ordered_at: datetime
    completed_at: datetime | None
    doctor_name: str | None
    report_path: str


class DmsRadiologyItem(BaseModel):
    id: UUID
    order_no: str
    scan_name: str
    scan_code: str
    status: str
    ordered_at: datetime
    has_report_file: bool
    has_image_file: bool
    doctor_name: str | None
    report_view_path: str
    report_file_path: str | None
    image_file_path: str | None


class DmsOtItem(BaseModel):
    id: UUID
    surgery_no: str
    surgery_type: str
    surgeon_name: str | None
    scheduled_at: datetime
    status: str
    has_notes: bool
    has_ot_report: bool
    has_consent: bool
    summary_path: str
    report_file_path: str | None


class DmsAdmissionItem(BaseModel):
    id: UUID
    status: str
    ward_name: str | None
    room_name: str | None
    bed_code: str | None
    doctor_name: str | None
    notes: str | None
    discharge_notes: str | None
    admitted_at: datetime
    discharged_at: datetime | None


class DmsBillingItem(BaseModel):
    id: str
    doc_type: str
    title: str
    amount: float | None = None
    status: str
    created_at: datetime | None = None
    note: str | None = None


class DmsPatientFile(BaseModel):
    patient: DmsPatientItem
    timeline: list[DmsTimelineEvent] = []
    documents: list[DmsDocumentResponse] = []
    prescriptions: list[DmsPrescriptionItem] = []
    lab_reports: list[DmsLabItem] = []
    radiology_reports: list[DmsRadiologyItem] = []
    ot_records: list[DmsOtItem] = []
    admissions: list[DmsAdmissionItem] = []
    billing_documents: list[DmsBillingItem] = []
