"""
Actions for Feature 8: Automated Clinical Deterioration Alerts (NEWS2/MEWS Engine).

Conforms to UltrionTech-Backend-Template modules/critical_care/actions/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from hms_migration.modules.critical_care.contracts.critical_care_contracts import (
    DeteriorationAlertCalculateRequest,
    DeteriorationAlertResponse,
)
from hms_migration.modules.critical_care.db.critical_care_repository import CriticalCareRepository
from hms_migration.modules.critical_care.entities.critical_care_entities import (
    AlertStatus,
    ClinicalDeteriorationAlert,
    DeteriorationRiskLevel,
)
from hms_migration.modules.patients.entities.patient import Patient
from hms_migration.shared.audit.service import write_audit_log


def compute_news2(
    respiratory_rate: float,
    spo2: float,
    on_supplemental_oxygen: bool,
    systolic_bp: float,
    heart_rate: float,
    avpu: str,
    temperature: float,
) -> tuple[int, DeteriorationRiskLevel, dict[str, int]]:
    """
    Standard National Early Warning Score 2 (NEWS2) algorithm.
    Returns (total_score, risk_level, score_breakdown).
    """
    scores: dict[str, int] = {}

    # 1. Respiration Rate
    if respiratory_rate <= 8:
        scores["respiratory_rate"] = 3
    elif 9 <= respiratory_rate <= 11:
        scores["respiratory_rate"] = 1
    elif 12 <= respiratory_rate <= 20:
        scores["respiratory_rate"] = 0
    elif 21 <= respiratory_rate <= 24:
        scores["respiratory_rate"] = 2
    else:  # >= 25
        scores["respiratory_rate"] = 3

    # 2. SpO2 Scale 1
    if spo2 <= 91:
        scores["spo2"] = 3
    elif 92 <= spo2 <= 93:
        scores["spo2"] = 2
    elif 94 <= spo2 <= 95:
        scores["spo2"] = 1
    else:  # >= 96
        scores["spo2"] = 0

    # 3. Supplemental Oxygen
    scores["supplemental_oxygen"] = 2 if on_supplemental_oxygen else 0

    # 4. Systolic BP
    if systolic_bp <= 90:
        scores["systolic_bp"] = 3
    elif 91 <= systolic_bp <= 100:
        scores["systolic_bp"] = 2
    elif 101 <= systolic_bp <= 110:
        scores["systolic_bp"] = 1
    elif 111 <= systolic_bp <= 219:
        scores["systolic_bp"] = 0
    else:  # >= 220
        scores["systolic_bp"] = 3

    # 5. Heart Rate
    if heart_rate <= 40:
        scores["heart_rate"] = 3
    elif 41 <= heart_rate <= 50:
        scores["heart_rate"] = 1
    elif 51 <= heart_rate <= 90:
        scores["heart_rate"] = 0
    elif 91 <= heart_rate <= 110:
        scores["heart_rate"] = 1
    elif 111 <= heart_rate <= 130:
        scores["heart_rate"] = 2
    else:  # >= 131
        scores["heart_rate"] = 3

    # 6. Consciousness (AVPU)
    if avpu.lower() in ("alert", "a"):
        scores["avpu"] = 0
    else:
        scores["avpu"] = 3

    # 7. Temperature
    if temperature <= 35.0:
        scores["temperature"] = 3
    elif 35.1 <= temperature <= 36.0:
        scores["temperature"] = 1
    elif 36.1 <= temperature <= 38.0:
        scores["temperature"] = 0
    elif 38.1 <= temperature <= 39.0:
        scores["temperature"] = 1
    else:  # >= 39.1
        scores["temperature"] = 2

    total = sum(scores.values())

    # Determine clinical risk band:
    # High: score >= 7 (Emergency critical response)
    # Medium: score 5-6 OR single score of 3 (Urgent ward response)
    # Low: score 1-4 (Routine monitoring)
    has_single_red_flag = any(v >= 3 for v in scores.values())

    if total >= 7:
        risk = DeteriorationRiskLevel.emergency
    elif total >= 5 or has_single_red_flag:
        risk = DeteriorationRiskLevel.high if total >= 5 else DeteriorationRiskLevel.medium
    elif total >= 1:
        risk = DeteriorationRiskLevel.low
    else:
        risk = DeteriorationRiskLevel.low

    return total, risk, scores


class CalculateDeteriorationScoreAction:
    """Action to compute NEWS2 score, evaluate risk thresholds, and persist alert if triggered."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = CriticalCareRepository(db, hospital_id)

    def execute(
        self,
        payload: DeteriorationAlertCalculateRequest,
        actor: dict[str, Any],
    ) -> DeteriorationAlertResponse:
        patient = (
            self.db.query(Patient)
            .filter(Patient.id == payload.patient_id, Patient.hospital_id == self.hospital_id)
            .first()
        )
        if not patient:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

        total_score, risk_level, breakdown = compute_news2(
            respiratory_rate=payload.respiratory_rate,
            spo2=payload.spo2,
            on_supplemental_oxygen=payload.on_supplemental_oxygen,
            systolic_bp=payload.systolic_bp,
            heart_rate=payload.heart_rate,
            avpu=payload.avpu,
            temperature=payload.temperature,
        )

        escalation_lvl = 1
        if risk_level == DeteriorationRiskLevel.emergency:
            escalation_lvl = 3
        elif risk_level in (DeteriorationRiskLevel.high, DeteriorationRiskLevel.medium):
            escalation_lvl = 2

        alert = ClinicalDeteriorationAlert(
            hospital_id=self.hospital_id,
            patient_id=payload.patient_id,
            admission_id=payload.admission_id,
            calculator_type="NEWS2",
            total_score=total_score,
            risk_level=risk_level,
            parameter_breakdown=breakdown,
            alert_status=AlertStatus.active if total_score >= 5 else AlertStatus.resolved,
            escalation_level=escalation_lvl,
            created_at=datetime.now(timezone.utc),
        )
        self.repo.add(alert)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="calculate_news2",
            entity_type="deterioration_alert",
            entity_id=alert.id,
            summary=f"Calculated NEWS2 score {total_score} ({risk_level.value}) for patient {patient.uhid}",
            details={"score": total_score, "risk": risk_level.value},
        )
        self.repo.commit()
        self.repo.refresh(alert)
        return DeteriorationAlertResponse.model_validate(alert)


class AcknowledgeDeteriorationAlertAction:
    """Acknowledge an active clinical deterioration alert."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = CriticalCareRepository(db, hospital_id)

    def execute(self, alert_id: UUID, notes: str | None, actor: dict[str, Any]) -> DeteriorationAlertResponse:
        alert = self.repo.get_alert_by_id(alert_id)
        if not alert:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")

        nurse_name = str(actor.get("name") or "Staff")
        nurse_id_raw = actor.get("user_id")
        nurse_id = UUID(str(nurse_id_raw)) if nurse_id_raw else None

        alert.alert_status = AlertStatus.acknowledged
        alert.acknowledged_by_id = nurse_id
        alert.acknowledged_by_name = nurse_name
        alert.acknowledged_at = datetime.now(timezone.utc)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="acknowledge_alert",
            entity_type="deterioration_alert",
            entity_id=alert.id,
            summary=f"Acknowledged deterioration alert {alert.id} by {nurse_name}",
        )
        self.repo.commit()
        self.repo.refresh(alert)
        return DeteriorationAlertResponse.model_validate(alert)
