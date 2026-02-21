"""Parent/mixin base models for inheritance testing."""

import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel

from app.enums import UpdateSource


class UUIDBase(SQLModel):
    """Provides a UUID primary key. Not a table itself."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)


class TimestampedBase(SQLModel):
    """Provides created_at / updated_at timestamps and update source. Not a table itself."""

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime | None = Field(default=None)
    update_source: UpdateSource = Field(default=UpdateSource.manual)


class NameBase(SQLModel):
    """Provides a name field. Not a table itself."""

    name: str = Field(max_length=100)
