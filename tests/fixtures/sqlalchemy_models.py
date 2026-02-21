"""SQLAlchemy test fixture models.

This file is used as an AST-parsing target by ``tests/test_parser_sqlalchemy.py``.
It is NOT imported at test time — the parser reads it as source text.

Covers:
- SQLAlchemy 2.0 style: Mapped[T] + mapped_column()
- SQLAlchemy 1.x style: Column(Type, ...)
- Both styles in the same file
- Enum subclasses
- ForeignKey relations
- Optional (nullable) columns
- Primary keys, unique, index, default
- __tablename__
"""

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """SQLAlchemy declarative base."""

    pass


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TeamRole(str, Enum):
    """Role within a team."""

    owner = "owner"
    admin = "admin"
    member = "member"


class ArticleStatus(str, Enum):
    """Publication status of an article."""

    draft = "draft"
    published = "published"
    archived = "archived"


# ---------------------------------------------------------------------------
# 2.0-style models (Mapped[T] + mapped_column)
# ---------------------------------------------------------------------------


class Team(Base):
    """A team that groups members together."""

    __tablename__ = "teams"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column()
    slug: Mapped[str] = mapped_column(unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column()

    members: Mapped[list["Member"]] = relationship(back_populates="team")


class Member(Base):
    """Membership linking a user to a team with a role."""

    __tablename__ = "members"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id"))
    email: Mapped[str] = mapped_column(unique=True)
    role: Mapped[str] = mapped_column(default="member")
    joined_at: Mapped[datetime] = mapped_column()
    bio: Mapped[Optional[str]] = mapped_column(default=None)

    team: Mapped[Optional["Team"]] = relationship(back_populates="members")


# ---------------------------------------------------------------------------
# 1.x-style models (Column)
# ---------------------------------------------------------------------------


class Article(Base):
    """An article authored by a team member (1.x style)."""

    __tablename__ = "articles"

    id = Column(Integer, primary_key=True)
    title = Column(String(500), nullable=False, index=True)
    body = Column(String, nullable=True)
    is_draft = Column(Boolean, nullable=False)
    author_id = Column(Integer, ForeignKey("members.id"))
    created_at = Column(DateTime, nullable=False)


class Tag(Base):
    """A tag for categorising articles (1.x style, no FK)."""

    __tablename__ = "tags"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True)
    slug = Column(String(100), nullable=False, index=True)
