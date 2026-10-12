"""Database-backed login rate-limiting entity for multi-worker protection."""

from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from infrastructure.postgres.base import Base


class LoginAttempt(Base):
    """Tracks failed login attempts across multiple workers for distributed rate limiting."""

    __tablename__ = "login_attempts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    key_hash: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False
    )
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    success: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

