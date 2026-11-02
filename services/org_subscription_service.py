"""
services/org_subscription_service.py
──────────────────────────────────────────────────────────────────────────────
Authoritative service for Organization Plans, Quotas, Limits & Expiration.
Handles:
1. Subscription status evaluation (active, grace_period, expired).
2. Fallback to Free tier on expiration with 100% read-only data preservation.
3. Member limit enforcement (active members + pending invites).
4. Course creation limit enforcement.
5. AI Course Drafting permission checks.
"""
from datetime import datetime, timezone, timedelta
import logging
from typing import Tuple, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func

import models

logger = logging.getLogger("org_subscription_service")

# Predefined UUID for the base Free plan
DEFAULT_FREE_PLAN_ID = UUID("e8b15d94-8a43-4f11-9238-a5c2d6e7f8b9")
UNLIMITED_PLAN_ID = UUID("d4e5f6a7-b8c9-4d0e-1f2a-3b4c5d6e7f8a")
GRACE_PERIOD_DAYS = 3


def get_free_plan(db: Session) -> models.Plan:
    """Retrieves the default Free Plan record."""
    plan = db.query(models.Plan).filter(models.Plan.id == DEFAULT_FREE_PLAN_ID).first()
    if not plan:
        plan = db.query(models.Plan).filter(models.Plan.name.ilike("free")).first()
    return plan


def get_effective_org_plan(
    org: models.Organisation,
    db: Session,
) -> Tuple[models.Plan, str, bool, Optional[int]]:
    """
    Evaluates and returns:
    (effective_plan, status, is_expired, days_remaining)

    Statuses:
    - "active": Plan is valid and unexpired (or lifetime/unlimited).
    - "grace_period": Plan expired within GRACE_PERIOD_DAYS; full access retained with warning.
    - "expired": Plan expired past grace period; effective_plan falls back to Free tier.
                 Existing courses and members are strictly preserved in read-only mode.
    """
    if not org:
        free_p = get_free_plan(db)
        return free_p, "active", False, None

    plan = org.plan or get_free_plan(db)
    if not plan:
        free_p = get_free_plan(db)
        return free_p, "active", False, None

    # Flagship / Unlimited Tier or Plans without expiration dates are perpetual
    if plan.name.lower() == "unlimited" or str(plan.id) == str(UNLIMITED_PLAN_ID) or org.plan_expires_at is None:
        return plan, "active", False, None

    now = datetime.now(timezone.utc)
    exp = org.plan_expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)

    grace_end = exp + timedelta(days=GRACE_PERIOD_DAYS)

    if now <= exp:
        days_rem = max(0, (exp - now).days)
        return plan, "active", False, days_rem
    elif now <= grace_end:
        days_rem = max(0, (grace_end - now).days)
        return plan, "grace_period", False, days_rem
    else:
        # Expired: Fallback to Free plan
        free_plan = get_free_plan(db) or plan
        return free_plan, "expired", True, 0


def check_org_member_limit_allowed(
    org_id: UUID,
    db: Session,
    incoming_count: int = 1,
) -> bool:
    """
    Ensures the organization has capacity to invite or accept new members.
    Accounts for both currently joined members and active pending invitations.
    """
    org = db.query(models.Organisation).filter(models.Organisation.id == org_id).first()
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organisation not found",
        )

    effective_plan, sub_status, is_expired, _ = get_effective_org_plan(org, db)

    # Unlimited tier has no cap
    if effective_plan.max_members is None:
        return True

    now = datetime.now(timezone.utc)

    # 1. Active members
    active_members_count = db.query(models.OrganisationMember).filter(
        models.OrganisationMember.organisation_id == org_id
    ).count()

    # 2. Pending active invitations
    pending_invites_count = db.query(models.Invitations).filter(
        models.Invitations.organisation_id == org_id,
        models.Invitations.uses_left > 0,
        models.Invitations.expires_at > now,
    ).count()

    total_roster = active_members_count + pending_invites_count
    max_allowed = effective_plan.max_members

    if total_roster + incoming_count > max_allowed:
        plan_label = "Free Plan" if is_expired else effective_plan.name
        detail_msg = (
            f"Your organization '{org.name}' has reached its member quota "
            f"({total_roster}/{max_allowed} on {plan_label}). "
            f"Please upgrade your plan to invite more members."
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "member_limit_exceeded",
                "current": total_roster,
                "limit": max_allowed,
                "plan_name": plan_label,
                "is_expired": is_expired,
                "message": detail_msg,
            },
        )

    return True


def check_org_course_limit_allowed(
    org_id: UUID,
    db: Session,
) -> bool:
    """
    Ensures the organization has capacity to create and publish a new course.
    """
    org = db.query(models.Organisation).filter(models.Organisation.id == org_id).first()
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organisation not found",
        )

    effective_plan, sub_status, is_expired, _ = get_effective_org_plan(org, db)

    # Unlimited tier has no course cap
    if effective_plan.max_courses is None:
        return True

    current_courses = db.query(models.Course).filter(
        models.Course.org_id == org_id
    ).count()

    max_allowed = effective_plan.max_courses

    if current_courses >= max_allowed:
        plan_label = "Free Plan" if is_expired else effective_plan.name
        detail_msg = (
            f"Your organization '{org.name}' has reached its course creation quota "
            f"({current_courses}/{max_allowed} on {plan_label}). "
            f"Please upgrade your plan to publish more courses."
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "course_limit_exceeded",
                "current": current_courses,
                "limit": max_allowed,
                "plan_name": plan_label,
                "is_expired": is_expired,
                "message": detail_msg,
            },
        )

    return True


def check_org_ai_draft_allowed(
    org: Optional[models.Organisation],
    db: Session,
) -> bool:
    """
    Verifies that the organization plan permits automated AI Course Drafting.
    Returns True if allowed, or raises HTTPException(403) with a clean reason.
    """
    if not org:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="plan_required",
        )

    effective_plan, sub_status, is_expired, _ = get_effective_org_plan(org, db)

    if is_expired:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "plan_expired",
                "message": "Your organization subscription has expired. Please renew your plan to use AI Course Drafting.",
            },
        )

    features = effective_plan.features or []
    has_ai_perk = any(
        "ai" in str(f).lower() and ("draft" in str(f).lower() or "generator" in str(f).lower())
        for f in features
    )

    if not has_ai_perk:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="plan_required",
        )

    return True

