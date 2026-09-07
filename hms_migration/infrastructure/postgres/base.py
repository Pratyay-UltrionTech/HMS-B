"""
Declarative base for target architecture SQLAlchemy entities.

Conforms to UltrionTech-Backend-Template infrastructure/postgres/ specification.
Provides an independent DeclarativeBase and MetaData collection, completely
isolated from legacy app.models.Base.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Target architecture declarative base class."""
    pass
