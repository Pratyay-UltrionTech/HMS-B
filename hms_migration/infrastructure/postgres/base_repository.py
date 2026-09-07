"""
Base repository interface for PostgreSQL database access.

Conforms to UltrionTech-Backend-Template infrastructure/postgres/ specification.
"""

from typing import Any, Generic, TypeVar
from uuid import UUID

from sqlalchemy.orm import Session

T = TypeVar("T")


class BaseRepository(Generic[T]):
    """Abstract base repository providing common persistence primitives."""

    def __init__(self, session: Session, hospital_id: UUID) -> None:
        self.session = session
        self.hospital_id = hospital_id

    def add(self, entity: T) -> None:
        """Stage a domain entity in the current session."""
        self.session.add(entity)

    def delete(self, entity: T) -> None:
        """Mark an entity for deletion in the current session."""
        self.session.delete(entity)

    def flush(self) -> None:
        """Flush changes to the active database transaction."""
        self.session.flush()

    def commit(self) -> None:
        """Commit the active database transaction."""
        self.session.commit()

    def refresh(self, entity: Any) -> None:
        """Refresh instance attributes from the database."""
        self.session.refresh(entity)
