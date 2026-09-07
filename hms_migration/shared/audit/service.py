"""
Shared audit logging service.

Conforms to UltrionTech-Backend-Template shared/audit/ specification.
Maintains exact behavioral and data fidelity with OG HMS-B AuditLog entries.
"""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from hms_migration.shared.audit.entities.audit_log import AuditLog


class AuditService:
    """Service providing audit trail recording for domain operations."""

    @staticmethod
    def record_audit(
        session: Session | AsyncSession,
        *,
        hospital_id: UUID,
        actor: dict[str, Any],
        action: str,
        entity_type: str,
        summary: str,
        entity_id: str | UUID | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditLog:
        """Create and stage an AuditLog record in the given session."""
        row = AuditLog(
            hospital_id=hospital_id,
            actor_email=str(actor.get("sub") or "unknown"),
            actor_name=str(actor.get("name") or actor.get("sub") or "Unknown"),
            actor_role=str(actor.get("role") or "unknown"),
            actor_role_label=actor.get("staff_role_name"),
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            summary=summary,
            details=details,
        )
        session.add(row)
        return row


def write_audit_log(
    session: Session | AsyncSession,
    *,
    hospital_id: UUID,
    actor: dict[str, Any],
    action: str,
    entity_type: str,
    summary: str,
    entity_id: str | UUID | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    """Convenience functional interface for recording audit records."""
    return AuditService.record_audit(
        session=session,
        hospital_id=hospital_id,
        actor=actor,
        action=action,
        entity_type=entity_type,
        summary=summary,
        entity_id=entity_id,
        details=details,
    )
