from sqlalchemy.orm import DeclarativeBase, MappedColumn, mapped_column
from sqlalchemy import Integer
import datetime
from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped


class Base(DeclarativeBase):
    """Shared declarative base for all SQLAlchemy models."""
    pass
