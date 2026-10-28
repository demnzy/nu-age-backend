"""
Subscription Lifecycle & Privilege Service.
Handles:
1. Subscription status evaluation (active, grace_period, expired).
2. Material & generation quota checking with read-only preservation for expired plans.
3. Automated renewal reminder dispatching (in-app, push via OneSignal, email fallback).
4. Subscription renewals and monthly generation cycle reset.
"""
from datetime import datetime, timezone
import logging
from typing import Tuple, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session, joinedload

import models
from services.notifications import dispatch_notification

logger = logging.getLogger("subscription_service")


def get_effective_subscription(user_id: UUID, db: Session) -> Tuple[models.UserSubscription, models.StoreProduct, str, bool]:
    """
    Evaluates and returns:
    (sub, effective_plan, current_status, is_expired)
    
    If the subscription is past its grace period:
    - status = 'expired'
    - is_expired = True
    - effective_plan falls back to the base 'free' plan
    """
    sub = db.query(models.UserSubscription).options(
        joinedload(models.UserSubscription.plan),
        joinedload(models.UserSubscription.user)
    ).filter(
        models.UserSubscription.user_id == user_id
    ).first()

    if not sub:
        free_plan = db.query(models.StoreProduct).filter(models.StoreProduct.id == "free").first()
        sub = models.UserSubscription(user_id=user_id, plan_id="free")
        db.add(sub)
        db.commit()
        db.refresh(sub)
        return sub, free_plan, "active", False

    now = datetime.now(timezone.utc)
    current_status = getattr(sub, "status", "active") or "active"
    is_expired = False

    if sub.expires_at:
        exp = sub.expires_at if sub.expires_at.tzinfo else sub.expires_at.replace(tzinfo=timezone.utc)
        grace = (
            sub.grace_period_until
            if sub.grace_period_until and sub.grace_period_until.tzinfo
            else (sub.grace_period_until.replace(tzinfo=timezone.utc) if sub.grace_period_until else exp)
        )

        if now > exp:
            if now <= grace:
                current_status = "grace_period"
                is_expired = False
            else:
                current_status = "expired"
                is_expired = True
        else:
            current_status = "active"
            is_expired = False

        if sub.status != current_status:
            sub.status = current_status
            db.commit()

    # Determine effective plan
    effective_plan = sub.plan
    if is_expired and sub.plan_id != "free":
        free_plan = db.query(models.StoreProduct).filter(models.StoreProduct.id == "free").first()
        if free_plan:
            effective_plan = free_plan

    return sub, effective_plan, current_status, is_expired


def check_material_upload_allowed(user_id: UUID, db: Session) -> models.UserSubscription:
    """
    Ensures user can upload study materials.
    - If expired: Existing materials remain preserved (never deleted), but new uploads
      are blocked if the count exceeds the free plan limit.
    - If active: Standard plan limit applies.
    """
    sub, effective_plan, status, is_expired = get_effective_subscription(user_id, db)

    # Sync actual materials count
    actual_count = db.query(models.StudyMaterial).filter(models.StudyMaterial.user_id == user_id).count()
    if sub.materials_uploaded < actual_count:
        sub.materials_uploaded = actual_count
        db.commit()

    bonus_mats = getattr(sub, "bonus_materials", 0) or 0
    mat_limit = (effective_plan.materials_limit + bonus_mats) if (effective_plan and effective_plan.materials_limit is not None) else None

    if mat_limit is not None and sub.materials_uploaded >= mat_limit:
        if is_expired:
            raise HTTPException(
                status_code=403,
                detail="Your subscription has expired. Your existing uploaded materials are safely preserved in read-only mode, but you have reached the free upload limit. Please renew your plan to upload more materials."
            )
        else:
            raise HTTPException(
                status_code=403,
                detail="You have reached your material upload limit. Please upgrade your plan or top-up with a booster pack."
            )

    return sub


def check_subscription_renewals(db: Session) -> dict:
    """
    Background evaluation task:
    1. Scans non-free subscriptions with expiration dates.
    2. Sends 3-day advance renewal reminders.
    3. Sends grace-period warnings.
    4. Sends suspension notices upon expiration.
    5. Returns audit summary dict.
    """
    now = datetime.now(timezone.utc)
    subs = db.query(models.UserSubscription).options(
        joinedload(models.UserSubscription.plan),
        joinedload(models.UserSubscription.user)
    ).filter(
        models.UserSubscription.plan_id != "free",
        models.UserSubscription.expires_at.isnot(None)
    ).all()

    summary = {
        "scanned": len(subs),
        "reminders_3day_sent": 0,
        "grace_warnings_sent": 0,
        "suspensions_sent": 0,
    }

    for sub in subs:
        exp = sub.expires_at if sub.expires_at.tzinfo else sub.expires_at.replace(tzinfo=timezone.utc)
        grace = (
            sub.grace_period_until
            if sub.grace_period_until and sub.grace_period_until.tzinfo
            else (sub.grace_period_until.replace(tzinfo=timezone.utc) if sub.grace_period_until else exp)
        )

        plan_name = (sub.plan.label if sub.plan and hasattr(sub.plan, "label") else (getattr(sub.plan, "name", None) or "Pro"))
        user = sub.user

        # 1. 3-day advance notice
        if exp > now:
            days_left = (exp - now).total_seconds() / 86400.0
            if days_left <= 3.0 and sub.last_reminder_sent != "3_day_advance":
                dispatch_notification(
                    db=db,
                    recipient_user_ids=[sub.user_id],
                    title="⏰ Subscription Renewal Notice",
                    body=f"Your {plan_name} plan renews in {max(1, int(round(days_left)))} day(s). Renew now to maintain uninterrupted access.",
                    category="billing",
                    action_route="/store",
                    collapse_id=f"sub_renew_{sub.user_id}",
                )
                if user and user.email:
                    try:
                        from services.email_service import send_email
                        send_email(
                            to=user.email,
                            subject=f"⏰ Your Nu Age {plan_name} Subscription Renews Soon",
                            html=f"<p>Hi {user.first_name},</p><p>Your <strong>{plan_name}</strong> plan is set to expire on {exp.strftime('%b %d, %Y')}.<br>Log into Nu Age to renew your plan and keep your study limits active.</p>"
                        )
                    except Exception as email_err:
                        logger.warning(f"Failed to send email renewal reminder to {user.email}: {email_err}")

                sub.last_reminder_sent = "3_day_advance"
                sub.status = "active"
                db.commit()
                summary["reminders_3day_sent"] += 1

        # 2. Grace period (expired, but within grace window)
        elif now > exp and now <= grace:
            if sub.last_reminder_sent != "grace_period":
                sub.status = "grace_period"
                dispatch_notification(
                    db=db,
                    recipient_user_ids=[sub.user_id],
                    title="⚠️ Subscription Grace Period Active",
                    body=f"Your {plan_name} subscription expired on {exp.strftime('%b %d')}. You have a 3-day grace period to renew before privileges are suspended.",
                    category="billing",
                    action_route="/store",
                    collapse_id=f"sub_grace_{sub.user_id}",
                )
                if user and user.email:
                    try:
                        from services.email_service import send_email
                        send_email(
                            to=user.email,
                            subject=f"⚠️ Action Required: Nu Age {plan_name} Grace Period Active",
                            html=f"<p>Hi {user.first_name},</p><p>Your {plan_name} subscription has expired. You are currently in your 3-day grace period until {grace.strftime('%b %d, %Y')}. Please renew now to prevent service interruption.</p>"
                        )
                    except Exception as email_err:
                        logger.warning(f"Failed to send grace period email: {email_err}")

                sub.last_reminder_sent = "grace_period"
                db.commit()
                summary["grace_warnings_sent"] += 1

        # 3. Hard expiration (past grace period)
        elif now > grace:
            if sub.last_reminder_sent != "expired" or sub.status != "expired":
                sub.status = "expired"
                if sub.last_reminder_sent != "expired":
                    dispatch_notification(
                        db=db,
                        recipient_user_ids=[sub.user_id],
                        title="🔒 Subscription Suspended",
                        body=f"Your {plan_name} subscription has expired. Your uploaded study materials are safely saved in read-only mode. Renew anytime to restore full upload & AI privileges.",
                        category="billing",
                        action_route="/store",
                        collapse_id=f"sub_expired_{sub.user_id}",
                    )
                    sub.last_reminder_sent = "expired"
                db.commit()
                summary["suspensions_sent"] += 1

    return summary



DAILY_FREE_AI_CHAT_LIMIT = 15


def get_chat_quota_status(user_id: UUID, db: Session) -> dict:
    """
    Evaluates current daily AI chat quota for this user.
    - Paid Pro/Unlimited users enjoy unlimited AI chat without daily locks.
    - Free tier users get DAILY_FREE_AI_CHAT_LIMIT (15 queries) per day, resetting at 00:00 UTC.
    """
    sub, effective_plan, status, is_expired = get_effective_subscription(user_id, db)
    if effective_plan and effective_plan.id != "free" and not is_expired:
        return {
            "plan": effective_plan.id,
            "plan_name": effective_plan.name if hasattr(effective_plan, "name") and effective_plan.name else "Pro Scholar",
            "is_unlimited": True,
            "limit": -1,
            "used": 0,
            "remaining": -1,
            "locked": False,
            "reset_at": "00:00 UTC",
        }

    today = datetime.now(timezone.utc).date()
    usage = db.query(models.AIChatDailyUsage).filter(
        models.AIChatDailyUsage.user_id == user_id,
        models.AIChatDailyUsage.usage_date == today,
    ).first()

    used = int(usage.query_count) if usage else 0
    limit = DAILY_FREE_AI_CHAT_LIMIT
    remaining = max(0, limit - used)
    locked = bool(used >= limit)

    return {
        "plan": "free",
        "plan_name": "Free Tier",
        "is_unlimited": False,
        "limit": limit,
        "used": used,
        "remaining": remaining,
        "locked": locked,
        "reset_at": "00:00 UTC",
    }


def assert_chat_quota_available(user_id: UUID, db: Session) -> dict:
    """
    Verifies the user has remaining chat quota before initiating an AI completion.
    Raises HTTPException 403 if daily free limit has been reached.
    """
    quota = get_chat_quota_status(user_id, db)
    if quota.get("locked"):
        limit = quota.get("limit", DAILY_FREE_AI_CHAT_LIMIT)
        raise HTTPException(
            status_code=403,
            detail=f"Free daily AI tutor limit reached ({limit}/{limit}). Daily quota resets at 00:00 UTC. Upgrade to Pro Scholar for unlimited access.",
        )
    return quota


def record_chat_query_usage(user_id: UUID, db: Session) -> dict:
    """
    Records one successful AI chat query. Only increments on free tier.
    Returns the newly updated quota metadata.
    """
    quota = get_chat_quota_status(user_id, db)
    if quota.get("is_unlimited"):
        return quota

    today = datetime.now(timezone.utc).date()
    usage = db.query(models.AIChatDailyUsage).filter(
        models.AIChatDailyUsage.user_id == user_id,
        models.AIChatDailyUsage.usage_date == today,
    ).first()

    if not usage:
        usage = models.AIChatDailyUsage(
            user_id=user_id,
            usage_date=today,
            query_count=1,
        )
        db.add(usage)
    else:
        usage.query_count = int(usage.query_count or 0) + 1

    db.commit()
    db.refresh(usage)

    used = int(usage.query_count)
    limit = DAILY_FREE_AI_CHAT_LIMIT
    remaining = max(0, limit - used)
    locked = bool(used >= limit)

    return {
        "plan": "free",
        "plan_name": "Free Tier",
        "is_unlimited": False,
        "limit": limit,
        "used": used,
        "remaining": remaining,
        "locked": locked,
        "reset_at": "00:00 UTC",
    }
