from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.models import IpdFormSubmissionStatus


class IpdFormSubmissionCreate(BaseModel):
    patient_id: UUID
    admission_id: UUID | None = None
    form_id: str = Field(min_length=1, max_length=128)
    form_title: str = Field(min_length=1, max_length=255)
    form_data: dict[str, Any] = Field(default_factory=dict)
    html_snapshot: str | None = None
    status: IpdFormSubmissionStatus = IpdFormSubmissionStatus.draft


class IpdFormSubmissionUpdate(BaseModel):
    admission_id: UUID | None = None
    form_data: dict[str, Any] | None = None
    html_snapshot: str | None = None
    status: IpdFormSubmissionStatus | None = None
    form_title: str | None = Field(default=None, min_length=1, max_length=255)


class IpdFormSubmissionResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    patient_id: UUID
    admission_id: UUID | None
    form_id: str
    form_title: str
    form_data: dict[str, Any]
    status: IpdFormSubmissionStatus
    filled_by_id: UUID | None
    filled_by_name: str
    filled_by_role: str
    medical_record_id: UUID | None
    patient_document_id: UUID | None
    has_html_snapshot: bool = False
    created_at: datetime
    updated_at: datetime
    patient_name: str | None = None
    patient_uhid: str | None = None

    model_config = {"from_attributes": True}
