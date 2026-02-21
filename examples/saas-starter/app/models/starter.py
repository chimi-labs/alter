"""SaaS starter models covering the full range of supported SQLModel features."""

import uuid
from datetime import datetime
from typing import Optional

from sqlmodel import Field, Relationship, SQLModel

from app.enums import InvoiceStatus, Role, SubscriptionStatus
from app.models.parents import TimestampedBase, UUIDBase, NameBase


class User(UUIDBase, TimestampedBase, NameBase, table=True):
    """Application user account."""

    __tablename__ = "users"

    email: str = Field(unique=True, index=True, max_length=255)
    is_active: bool = Field(default=True)

    memberships: list["Membership"] = Relationship(back_populates="user")
    posts: list["Post"] = Relationship(back_populates="author")
    audit_logs: list["AuditLog"] = Relationship(back_populates="user")


class Organization(SQLModel, NameBase, table=True):
    """An organization/team that owns resources."""

    __tablename__ = "organizations"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    slug: str = Field(unique=True, index=True, max_length=100)
    settings: Optional[dict] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    memberships: list["Membership"] = Relationship(back_populates="organization")
    subscription: Optional["Subscription"] = Relationship(back_populates="organization")
    posts: list["Post"] = Relationship(back_populates="organization")
    invoices: list["Invoice"] = Relationship(back_populates="organization")


class Membership(SQLModel, table=True):
    """Join table linking users to organizations with a role."""

    __tablename__ = "memberships"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id")
    organization_id: uuid.UUID = Field(foreign_key="organizations.id")
    role: Role = Field(default=Role.member)
    joined_at: datetime = Field(default_factory=datetime.utcnow)

    user: Optional[User] = Relationship(back_populates="memberships")
    organization: Optional[Organization] = Relationship(back_populates="memberships")


class Subscription(SQLModel, table=True):
    """Billing subscription for an organization. One-to-one via unique FK on organization_id."""

    __tablename__ = "subscriptions"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    organization_id: uuid.UUID = Field(foreign_key="organizations.id", unique=True)
    status: SubscriptionStatus = Field(default=SubscriptionStatus.trialing)
    plan: str = Field(default="free", max_length=50)
    trial_ends_at: Optional[datetime] = Field(default=None)
    current_period_end: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    organization: Optional[Organization] = Relationship(back_populates="subscription")
    invoices: list["Invoice"] = Relationship(back_populates="subscription")


class Invoice(SQLModel, table=True):
    """Invoice issued to an organization under a subscription."""

    __tablename__ = "invoices"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    organization_id: uuid.UUID = Field(foreign_key="organizations.id", index=True)
    subscription_id: uuid.UUID = Field(foreign_key="subscriptions.id")
    status: InvoiceStatus = Field(default=InvoiceStatus.draft)
    amount_cents: int = Field()
    currency: str = Field(default="usd", max_length=3)
    due_at: Optional[datetime] = Field(default=None)
    paid_at: Optional[datetime] = Field(default=None)
    line_items: Optional[dict] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    organization: Optional[Organization] = Relationship(back_populates="invoices")
    subscription: Optional[Subscription] = Relationship(back_populates="invoices")


class Post(UUIDBase, TimestampedBase, table=True):
    """A blog post or content item authored by a user within an organization."""

    __tablename__ = "posts"

    title: str = Field(max_length=500, index=True)
    body: Optional[str] = Field(default=None)
    is_published: bool = Field(default=False)
    author_id: uuid.UUID = Field(foreign_key="users.id")
    organization_id: uuid.UUID = Field(foreign_key="organizations.id")

    author: Optional[User] = Relationship(back_populates="posts")
    organization: Optional[Organization] = Relationship(back_populates="posts")


class AuditLog(SQLModel, table=True):
    """Immutable audit trail entry. user_id is nullable to support system-generated events."""

    __tablename__ = "audit_logs"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: Optional[uuid.UUID] = Field(default=None, foreign_key="users.id")
    action: str = Field(max_length=100, index=True)
    table_name: str = Field(max_length=100)
    record_id: Optional[uuid.UUID] = Field(default=None)
    payload: Optional[dict] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    user: Optional[User] = Relationship(back_populates="audit_logs")
