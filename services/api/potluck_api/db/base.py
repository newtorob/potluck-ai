from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base for all SQLAlchemy models.

    Import this in every model file and in alembic/env.py so Alembic can
    discover all mapped tables via Base.metadata.
    """

    pass
