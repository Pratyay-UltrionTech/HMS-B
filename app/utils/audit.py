from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import AuditLog


def write_audit(
    db: Session,
    *,
    hospital_id: UUID,
    actor: dict[str, Any],
    action: str,
    entity_type: str,
    summary: str,
    entity_id: str | UUID | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Append an audit log row. Caller is responsible for commit."""
    db.add(
        AuditLog(
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
    )
