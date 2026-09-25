"""
Repository for Inpatient Vitals and Ward Intake/Output Observations.

Conforms to UltrionTech-Backend-Template modules/inpatient/db/ specification.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from modules.inpatient.contracts.nursing_contracts import IpdIntakeOutputSummary
from modules.inpatient.entities.nursing_entities import (
    IntakeOutputType,
    IpdIntakeOutput,
    IpdVitalSign,
)


class InpatientVitalsRepository:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    # ── Vitals Operations ───────────────────────────────────────────────────

    def add_vital(self, vital: IpdVitalSign) -> IpdVitalSign:
        self.db.add(vital)
        self.db.commit()
        self.db.refresh(vital)
        return vital

    def list_vitals(self, admission_id: UUID, limit: int = 100) -> list[IpdVitalSign]:
        return (
            self.db.query(IpdVitalSign)
            .filter(
                IpdVitalSign.hospital_id == self.hospital_id,
                IpdVitalSign.admission_id == admission_id,
            )
            .order_by(IpdVitalSign.recorded_at.desc())
            .limit(limit)
            .all()
        )

    def get_latest_vital(self, admission_id: UUID) -> IpdVitalSign | None:
        return (
            self.db.query(IpdVitalSign)
            .filter(
                IpdVitalSign.hospital_id == self.hospital_id,
                IpdVitalSign.admission_id == admission_id,
            )
            .order_by(IpdVitalSign.recorded_at.desc())
            .first()
        )

    # ── Intake & Output Operations ──────────────────────────────────────────

    def add_intake_output(self, record: IpdIntakeOutput) -> IpdIntakeOutput:
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    def list_intake_output(
        self, admission_id: UUID, limit: int = 200
    ) -> list[IpdIntakeOutput]:
        return (
            self.db.query(IpdIntakeOutput)
            .filter(
                IpdIntakeOutput.hospital_id == self.hospital_id,
                IpdIntakeOutput.admission_id == admission_id,
            )
            .order_by(IpdIntakeOutput.recorded_at.desc())
            .limit(limit)
            .all()
        )

    def get_intake_output_summary(
        self, admission_id: UUID, hours: int = 24
    ) -> IpdIntakeOutputSummary:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        records = (
            self.db.query(IpdIntakeOutput)
            .filter(
                IpdIntakeOutput.hospital_id == self.hospital_id,
                IpdIntakeOutput.admission_id == admission_id,
                IpdIntakeOutput.recorded_at >= since,
            )
            .all()
        )

        total_intake = 0.0
        total_output = 0.0
        intake_by_category: dict[str, float] = {}
        output_by_category: dict[str, float] = {}

        for r in records:
            vol = float(r.volume_ml or 0.0)
            cat = str(r.category or "other")
            if r.entry_type == IntakeOutputType.intake:
                total_intake += vol
                intake_by_category[cat] = round(intake_by_category.get(cat, 0.0) + vol, 2)
            else:
                total_output += vol
                output_by_category[cat] = round(output_by_category.get(cat, 0.0) + vol, 2)

        return IpdIntakeOutputSummary(
            total_intake_ml=round(total_intake, 2),
            total_output_ml=round(total_output, 2),
            net_balance_ml=round(total_intake - total_output, 2),
            intake_by_category=intake_by_category,
            output_by_category=output_by_category,
            period_hours=hours,
        )
