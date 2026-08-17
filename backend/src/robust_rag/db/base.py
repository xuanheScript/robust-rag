"""Declarative SQLAlchemy base."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class shared by all persisted models."""
