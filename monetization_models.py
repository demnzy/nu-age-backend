# monetization_models.py
#
# New tables for RevenueCat-backed monetization. Nothing here modifies your
# existing models — these are additive. Two independent gates:
#
#   1. Student AI Self-Study Hub generations
#      -> existing free quota (UserSubscription.generations_used vs
#         StudyPlan.generations_limit) PLUS a purchasable, non-expiring
#         credit balance (CreditBalance) for consumable top-ups.
#
#   2. Organisation AI course drafting
#      -> gated by a subscription entitlement + monthly quota on the org's
#         Plan, tracked the same way UserSubscription already tracks
#         student generations. No consumables involved.
#
# Both are driven by RevenueCat, reconciled via webhook, and both write to
# an append-only ledger (CreditLedgerEntry / OrgDraftLedgerEntry) so you
# always have an audit trail independent of the current balance/counter —
# useful for debugging disputes and for the HAMM award's "articulate your
# monetization strategy" ask, since you can literally show usage over time.

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column, Integer, String, DateTime, ForeignKey, Boolean, Float,
    UniqueConstraint, Enum as SQLEnum,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from database import Base


# ---------------------------------------------------------------------------
# 1. STUDENT CREDITS (consumable, for AI Self-Study Hub generations)
# ---------------------------------------------------------------------------

class CreditPack(Base):
    """
    A purchasable pack of generation credits. The primary key IS the
    RevenueCat product identifier (e.g. "credits_10"), so we never need a
    separate mapping table between our catalog and RevenueCat's.
    """
    __tablename__ = "credit_packs"

    id = Column(String, primary_key=True)          # RevenueCat product id
    label = Column(String, nullable=False)          # "10 Generations"
    credits = Column(Integer, nullable=False)        # 10
    price_display = Column(String, nullable=True)    # "$2.99" — UI only, not authoritative
    active = Column(Boolean, default=True, nullable=False)  # soft-hide retired packs
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CreditBalance(Base):
    """
    One row per user. This is the number your API reads/writes on every
    generation request. Never trust a client-supplied balance — this table
    is the only source of truth, mutated only by:
      - the RevenueCat webhook handler (grants, on verified purchase)
      - the generation-spend endpoint (debits, on verified use)
    """
    __tablename__ = "credit_balances"

    user_id = Column(UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), primary_key=True)
    balance = Column(Integer, default=0, nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", backref="credit_balance", uselist=False)


class CreditPurchase(Base):
    """
    One row per RevenueCat purchase event that granted credits. Existence
    of a row with a given rc_event_id is the idempotency check — RevenueCat
    webhooks can and will redeliver, and users can retry client-side confirm
    calls after a flaky connection.
    """
    __tablename__ = "credit_purchases"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)

    rc_event_id = Column(String, unique=True, nullable=False, index=True)       # webhook event.id — primary idempotency key
    rc_transaction_id = Column(String, nullable=True, index=True)                # store id, useful for support lookups
    product_id = Column(String, ForeignKey("credit_packs.id"), nullable=False)
    credits_granted = Column(Integer, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", backref="credit_purchases")
    pack = relationship("CreditPack")


class CreditLedgerEntry(Base):
    """
    Append-only log of every balance change (grants AND spends). The
    CreditBalance row is a cached total; this table is the audit trail.
    delta is positive for grants, negative for spends.
    """
    __tablename__ = "credit_ledger_entries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)

    delta = Column(Integer, nullable=False)
    reason = Column(String, nullable=False)   # "purchase", "generation_spend", "free_quota" (informational), "admin_adjustment"
    reference_id = Column(String, nullable=True)  # e.g. CreditPurchase.id or the generation/material id spent on

    balance_after = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", backref="credit_ledger_entries")


# ---------------------------------------------------------------------------
# 2. ORG AI COURSE DRAFTING (subscription entitlement, not consumable)
# ---------------------------------------------------------------------------

class DraftStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


# Extends your existing Plan model without modifying it: a satellite table
# keyed 1:1 on Plan.id. This keeps the diff on your existing models.py at
# zero — you only need to add the relationship line noted below.
class PlanAIDraftingConfig(Base):
    """
    AI course-drafting entitlement + monthly quota for a given Plan.
    Not every Plan needs a row here — absence of a row means the plan does
    not include AI drafting at all (treated as entitlement=False).
    """
    __tablename__ = "plan_ai_drafting_configs"

    plan_id = Column(UUID(as_uuid=True), ForeignKey("plans.id", ondelete="CASCADE"), primary_key=True)
    entitled = Column(Boolean, default=False, nullable=False)
    monthly_draft_limit = Column(Integer, nullable=True)  # None == unlimited, given entitled=True

    plan = relationship("Plan", backref="ai_drafting_config", uselist=False)


class OrganisationAIDraftingUsage(Base):
    """
    Mirrors UserSubscription's generations_used/cycle_start_date pattern,
    but keyed on Organisation instead of User. One row per org.

    Reset drafts_used_this_cycle to 0 when cycle_start_date rolls over —
    do this lazily on read (check if now() - cycle_start_date >= 1 month,
    reset + bump cycle_start_date) rather than needing a cron job.
    """
    __tablename__ = "organisation_ai_drafting_usage"

    org_id = Column(UUID(as_uuid=True), ForeignKey("Organisations.id", ondelete="CASCADE"), primary_key=True)
    drafts_used_this_cycle = Column(Integer, default=0, nullable=False)
    cycle_start_date = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    organisation = relationship("Organisation", backref="ai_drafting_usage", uselist=False)


class OrgDraftLedgerEntry(Base):
    """
    Append-only audit trail of every course-draft generation an org has
    used, independent of the rolling counter above.
    """
    __tablename__ = "org_draft_ledger_entries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    org_id = Column(UUID(as_uuid=True), ForeignKey("Organisations.id", ondelete="CASCADE"), nullable=False, index=True)
    requested_by_user_id = Column(UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True)

    topic = Column(String, nullable=False)
    status = Column(SQLEnum(DraftStatus), default=DraftStatus.PENDING, nullable=False)
    draft_job_id = Column(String, ForeignKey("course_draft_jobs.id", ondelete="SET NULL"), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    organisation = relationship("Organisation", backref="draft_ledger_entries")
    requested_by = relationship("User")


# ---------------------------------------------------------------------------
# 3. REVENUECAT WEBHOOK EVENT LOG (shared by both systems)
# ---------------------------------------------------------------------------

class RevenueCatWebhookEvent(Base):
    """
    Raw log of every webhook RevenueCat has sent us, keyed on RevenueCat's
    own event.id for global idempotency BEFORE we even look at whether it's
    a student credit purchase or an org subscription event. Insert this row
    first (in the same transaction as the balance/entitlement mutation);
    if the insert violates the unique constraint, the event was already
    processed — short-circuit and return 200 to RevenueCat without
    reprocessing.
    """
    __tablename__ = "revenuecat_webhook_events"

    event_id = Column(String, primary_key=True)   # RevenueCat's event.id
    event_type = Column(String, nullable=False)    # e.g. "NON_RENEWING_PURCHASE", "RENEWAL", "CANCELLATION"
    app_user_id = Column(String, nullable=False, index=True)  # == User.id as string, per your confirmed mapping
    raw_payload = Column(String, nullable=False)    # store as JSON text for later debugging/replay
    received_at = Column(DateTime(timezone=True), server_default=func.now())