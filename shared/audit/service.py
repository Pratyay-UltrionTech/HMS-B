"""
Shared audit logging service.

Conforms to UltrionTech-Backend-Template shared/audit/ specification.
Maintains exact behavioral and data fidelity with OG HMS-B AuditLog entries.
"""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from shared.audit.entities.audit_log import AuditLog


class AuditService:
    """Service providing audit trail recording for domain operations."""

    @staticmethod
    def build_row(
        *,
        hospital_id: UUID,
        actor: dict[str, Any],
        action: str,
        entity_type: str,
        summary: str,
        entity_id: str | UUID | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditLog:
        """Construct an AuditLog row without attaching it to any session."""
        return AuditLog(
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
        row = AuditService.build_row(
            hospital_id=hospital_id,
            actor=actor,
            action=action,
            entity_type=entity_type,
            summary=summary,
            entity_id=entity_id,
            details=details,
        )
        session.add(row)
        # Stage sync event on session so it only dispatches if commit succeeds
        try:
            if hasattr(session, "info"):
                session.info.setdefault("pending_sync_events", []).append(
                    (hospital_id, entity_type, action, str(entity_id) if entity_id is not None else None)
                )
        except Exception:
            pass
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
    """
    Convenience functional interface for recording audit records.

    Stages the AuditLog row on the caller's session, preserving the existing
    transactional contract: the row is committed together with the caller's
    business transaction via the caller's `session.commit()`.

    FLAW-029: For writes that must survive a caller rollback (forensic-critical
    actions such as document deletion), use `write_audit_log_autonomous` instead,
    which persists through an independent session with immediate commit.
    """
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


def write_audit_log_autonomous(
    *,
    hospital_id: UUID,
    actor: dict[str, Any],
    action: str,
    entity_type: str,
    summary: str,
    entity_id: str | UUID | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    """
    Persist an AuditLog row through an independent session with immediate commit.

    Unlike `AuditService.record_audit` (which stages the row on the caller's
    session and is therefore rolled back with the caller's transaction), this
    writer opens its own dedicated session so the audit record is durable even
    when the surrounding business transaction aborts.
    """
    from infrastructure.postgres.session import get_transitional_sync_session_factory
    from shared.sync.broker import sync_broker

    row = AuditService.build_row(
        hospital_id=hospital_id,
        actor=actor,
        action=action,
        entity_type=entity_type,
        summary=summary,
        entity_id=entity_id,
        details=details,
    )
    factory = get_transitional_sync_session_factory()
    with factory() as audit_session:
        audit_session.add(row)
        audit_session.commit()
    try:
        sync_broker.publish(hospital_id, entity_type, action, entity_id)
    except Exception:
        pass
    return row


# ---------------------------------------------------------------------------
# SQLAlchemy Session Transaction Event Hooks for Real-Time Sync
# ---------------------------------------------------------------------------
from sqlalchemy import event
from shared.sync.broker import sync_broker


@event.listens_for(Session, "after_commit")
def _dispatch_pending_sync_events(session: Session) -> None:
    events = session.info.pop("pending_sync_events", None) if hasattr(session, "info") else None
    if not events:
        return
    for h_id, ent_type, act, ent_id in events:
        try:
            sync_broker.publish(h_id, ent_type, act, ent_id)
        except Exception:
            pass


@event.listens_for(Session, "after_rollback")
def _clear_pending_sync_events(session: Session) -> None:
    if hasattr(session, "info"):
        session.info.pop("pending_sync_events", None)

