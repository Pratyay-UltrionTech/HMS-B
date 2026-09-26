"""Pydantic contracts for the Unified Patient Medical Record / Clinical Timeline layer."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ClinicalRecordTimelineItem(BaseModel):
    id: UUID
    source_type: str  # "note", "nursing", "care_plan", "handover", "vital", "medication", "emar", "lab", "radiology", "procedure", "form", "discharge_summary"
    title: str
    occurred_at: datetime
    author_name: str | None = None
    author_role: str | None = None
    status: str | None = None
    summary: str | None = None
    admission_id: UUID | None = None
    appointment_id: UUID | None = None
    is_finalized: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(from_attributes=True)


class EpisodeSummary(BaseModel):
    episode_id: UUID
    encounter_type: str  # "ipd" or "opd"
    identifier: str  # e.g. "IP-xxxx" or "OP-xxxx"
    start_date: datetime | str
    end_date: datetime | str | None = None
    status: str
    attending_doctor_name: str | None = None
    department_name: str | None = None
    diagnosis: str | None = None
    room_or_bed: str | None = None

    overview: dict[str, Any] = Field(default_factory=dict)
    clinical_notes: list[dict[str, Any]] = Field(default_factory=list)
    nursing_notes: list[dict[str, Any]] = Field(default_factory=list)
    care_plans: list[dict[str, Any]] = Field(default_factory=list)
    shift_handovers: list[dict[str, Any]] = Field(default_factory=list)
    vitals: list[dict[str, Any]] = Field(default_factory=list)
    medications: list[dict[str, Any]] = Field(default_factory=list)
    emar: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    procedures: list[dict[str, Any]] = Field(default_factory=list)
    forms_and_documents: list[dict[str, Any]] = Field(default_factory=list)
    discharge_summary: dict[str, Any] | None = None

    model_config = ConfigDict(from_attributes=True)


class UnifiedPatientMedicalRecordResponse(BaseModel):
    patient_id: UUID
    uhid: str
    name: str
    gender: str | None = None
    date_of_birth: date | None = None
    blood_group: str | None = None
    phone: str | None = None
    episodes: list[EpisodeSummary] = Field(default_factory=list)
    standalone_records: list[ClinicalRecordTimelineItem] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)
