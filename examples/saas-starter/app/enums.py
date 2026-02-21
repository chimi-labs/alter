"""Shared enums for the SaaS starter models."""

from enum import Enum


class Role(str, Enum):
    """Membership role within an organization."""

    admin = "admin"
    member = "member"
    viewer = "viewer"


class SubscriptionStatus(str, Enum):
    """Subscription billing status."""

    trialing = "trialing"
    active = "active"
    past_due = "past_due"
    canceled = "canceled"


class InvoiceStatus(str, Enum):
    """Invoice payment status."""

    draft = "draft"
    open = "open"
    paid = "paid"
    void = "void"
    uncollectible = "uncollectible"


class UpdateSource(str, Enum):
    """Tracks what triggered the last update to a timestamped record."""

    manual = "manual"
    api = "api"
    system = "system"
    import_ = "import"
