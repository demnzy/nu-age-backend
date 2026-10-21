"""
services/promo_service.py
──────────────────────────────────────────────────────────────────────────────
Comprehensive Promo Code, Badges, and Gamification Service for Nu-Age.
Handles:
- Atomic multi-reward execution (coins, AI credits, trial days, badges).
- Idempotent redemptions with limits and expiry safeguards.
- Ambassador attribution and referral kickbacks.
- Default badge seeding and primary badge equipping.
──────────────────────────────────────────────────────────────────────────────
"""

import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException, status

import models
from services import coin_service


# Standard preset badges for Nu-Age Gamification
PRESET_BADGES = [
    {
        "slug": "campus_ambassador",
        "name": "Campus Ambassador",
        "description": "Official campus representative for Nu Age.",
        "icon": "SCHOOL_ROUNDED",
        "color_hex": "#4CAF50",
        "gradient_start": "#2E7D32",
        "gradient_end": "#1B5E20",
        "is_automated": False,
    },
    {
        "slug": "streak_king",
        "name": "#1 Streak Master",
        "description": "Rank #1 on the platform-wide daily study streak leaderboard.",
        "icon": "LOCAL_FIRE_DEPARTMENT_ROUNDED",
        "color_hex": "#FF9800",
        "gradient_start": "#E65100",
        "gradient_end": "#FF8F00",
        "is_automated": True,
    },
    {
        "slug": "duo_kings",
        "name": "Duo Kings",
        "description": "Top-ranked study partnership holding the active #1 Duo Streak.",
        "icon": "WORKSPACE_PREMIUM_ROUNDED",
        "color_hex": "#E91E63",
        "gradient_start": "#880E4F",
        "gradient_end": "#C2185B",
        "is_automated": True,
    },
    {
        "slug": "century_club",
        "name": "100-Day Streak",
        "description": "Achieved an unbroken 100-day daily study streak.",
        "icon": "MILITARY_TECH_ROUNDED",
        "color_hex": "#9C27B0",
        "gradient_start": "#4A148C",
        "gradient_end": "#7B1FA2",
        "is_automated": True,
    },
    {
        "slug": "merit_scholar",
        "name": "Merit Scholar",
        "description": "Completed extensive curriculum exams and learning objectives.",
        "icon": "STAR_ROUNDED",
        "color_hex": "#2196F3",
        "gradient_start": "#0D47A1",
        "gradient_end": "#1976D2",
        "is_automated": False,
    },
    {
        "slug": "verified_tutor",
        "name": "Verified Tutor",
        "description": "Accredited peer educator and learning content creator.",
        "icon": "VERIFIED_ROUNDED",
        "color_hex": "#00BCD4",
        "gradient_start": "#006064",
        "gradient_end": "#00838F",
        "is_automated": False,
    },
    {
        "slug": "beta_tester",
        "name": "Beta Pioneer",
        "description": "Early adopter driving the future of Nu Age learning.",
        "icon": "SHIELD_ROUNDED",
        "color_hex": "#FF5722",
        "gradient_start": "#BF360C",
        "gradient_end": "#D84315",
        "is_automated": False,
    },
]


def seed_default_badges_if_empty(db: Session):
    """Ensures all standard gamification badges exist in the database."""
    for b_data in PRESET_BADGES:
        existing = db.query(models.MemberBadge).filter(models.MemberBadge.slug == b_data["slug"]).first()
        if not existing:
            badge = models.MemberBadge(
                slug=b_data["slug"],
                name=b_data["name"],
                description=b_data["description"],
                icon=b_data["icon"],
                color_hex=b_data["color_hex"],
                gradient_start=b_data.get("gradient_start"),
                gradient_end=b_data.get("gradient_end"),
                is_automated=b_data["is_automated"],
            )
            db.add(badge)
    db.commit()


def _apply_pro_trial(db: Session, user_id: uuid.UUID, days: int) -> Dict[str, Any]:
    """
    Applies or extends a Pro Scholar trial without downgrading any existing plan.
    """
    now = datetime.now(timezone.utc)
    sub = db.query(models.UserSubscription).filter(models.UserSubscription.user_id == user_id).first()
    if not sub:
        sub = models.UserSubscription(user_id=user_id, plan_id="free")
        db.add(sub)
        db.flush()

    # Ensure plan_obj for pro exists
    plan_obj = db.query(models.StoreProduct).filter(models.StoreProduct.id == "pro").first()

    if sub.expires_at:
        exp = sub.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp > now and sub.plan_id == "pro":
            new_exp = exp + timedelta(days=days)
        else:
            new_exp = now + timedelta(days=days)
    else:
        new_exp = now + timedelta(days=days)

    if sub.plan_id not in ("pro", "unlimited"):
        sub.plan_id = "pro"
        sub.billing_cycle = "trial"

    sub.status = "active"
    sub.expires_at = new_exp
    sub.grace_period_until = new_exp + timedelta(days=3)
    db.flush()
    return {
        "plan_id": sub.plan_id,
        "trial_days": days,
        "expires_at": new_exp.isoformat(),
    }


def redeem_promo_code(db: Session, user_id: uuid.UUID, code_str: str) -> Dict[str, Any]:
    """
    Executes a multi-reward promo code redemption within a single atomic transaction.
    Guarantees:
    - Code existence, case-insensitivity, and active state.
    - Expiration and usage limits enforcement.
    - Idempotency (single redemption per user per code with IntegrityError handling).
    - Multi-reward dispatch (coins, AI generation credits, trial days, badge, pack unlocks).
    - Ambassador referral kickback (only if ambassador is active).
    """
    clean_code = (code_str or "").strip().upper()
    if not clean_code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Promo code cannot be empty.")

    try:
        promo = db.query(models.PromoCode).filter(models.PromoCode.code == clean_code).with_for_update().first()
    except Exception:
        promo = db.query(models.PromoCode).filter(models.PromoCode.code == clean_code).first()

    if not promo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid promo code. Please check and try again.")

    if not promo.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This promo code is currently disabled.")

    now = datetime.now(timezone.utc)
    if promo.expires_at:
        exp = promo.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp < now:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This promo code has expired.")

    if promo.max_uses is not None and (promo.uses_count or 0) >= promo.max_uses:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This promo code has reached its maximum redemptions limit.")

    # Idempotency check
    existing_redemption = db.query(models.PromoCodeRedemption).filter(
        models.PromoCodeRedemption.promo_code_id == promo.id,
        models.PromoCodeRedemption.user_id == user_id
    ).first()
    if existing_redemption:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You have already redeemed this promo code.")

    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    if promo.scoped_university:
        user_uni = (user.university or "").strip().lower()
        scoped_uni = promo.scoped_university.strip().lower()
        if not user_uni or user_uni != scoped_uni:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"This promo code is exclusively reserved for students of {promo.scoped_university}."
            )

    granted_summary: Dict[str, Any] = {}

    try:
        # 1. Nu-Coins Reward (Atomic flush, no early commit)
        if promo.reward_coins and promo.reward_coins > 0:
            _, new_coin_bal = coin_service.grant_coins(
                db,
                user_id=user_id,
                amount=promo.reward_coins,
                category="promo_code",
                description=f"Redeemed promo code {promo.code}",
                source_type="promo_code",
                reference_id=str(promo.id),
                commit=False,
            )
            granted_summary["coins"] = promo.reward_coins
            granted_summary["new_coins_balance"] = new_coin_bal

        # 2. AI Generation Credits Reward -> Mapped strictly to UserSubscription.bonus_generations
        if promo.reward_ai_credits and promo.reward_ai_credits > 0:
            sub = db.query(models.UserSubscription).filter(models.UserSubscription.user_id == user_id).first()
            if not sub:
                sub = models.UserSubscription(user_id=user_id, plan_id="free")
                db.add(sub)
                db.flush()
            sub.bonus_generations = (sub.bonus_generations or 0) + promo.reward_ai_credits
            db.flush()
            granted_summary["ai_credits"] = promo.reward_ai_credits
            granted_summary["bonus_generations"] = sub.bonus_generations

        # 3. Pro Trial Days
        if promo.reward_trial_days and promo.reward_trial_days > 0:
            trial_res = _apply_pro_trial(db, user_id=user_id, days=promo.reward_trial_days)
            granted_summary["trial"] = trial_res

        # 4. Exclusive Badge Award
        if promo.reward_badge_slug:
            badge = db.query(models.MemberBadge).filter(models.MemberBadge.slug == promo.reward_badge_slug).first()
            if badge:
                user_badge = db.query(models.UserBadge).filter(
                    models.UserBadge.user_id == user_id,
                    models.UserBadge.badge_id == badge.id
                ).first()
                if not user_badge:
                    user_badge = models.UserBadge(
                        user_id=user_id,
                        badge_id=badge.id,
                        is_primary=False
                    )
                    db.add(user_badge)
                granted_summary["badge"] = {
                    "slug": badge.slug,
                    "name": badge.name,
                    "icon": badge.icon,
                    "color_hex": badge.color_hex
                }

        # 6. Ambassador Referral Kickback (active ambassadors only)
        if promo.ambassador_id and promo.ambassador_id != user_id and promo.ambassador_coin_reward > 0:
            amb = db.query(models.CampusAmbassador).filter(models.CampusAmbassador.user_id == promo.ambassador_id).first()
            # If an ambassador profile exists, ensure it is active
            if not amb or amb.status == "active":
                coin_service.grant_coins(
                    db,
                    user_id=promo.ambassador_id,
                    amount=promo.ambassador_coin_reward,
                    category="referral_commission",
                    description=f"Referral commission from code {promo.code}",
                    source_type="ambassador_referral",
                    reference_id=str(promo.id),
                    commit=False,
                )
                granted_summary["ambassador_notified"] = True

        # Record redemption log & increment usage atomically
        redemption = models.PromoCodeRedemption(
            promo_code_id=promo.id,
            user_id=user_id,
            rewards_granted=granted_summary
        )
        db.add(redemption)
        promo.uses_count = (promo.uses_count or 0) + 1

        db.commit()

    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You have already redeemed this promo code.")
    except HTTPException:
        db.rollback()
        raise
    except Exception as ex:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to redeem promo code: {str(ex)}")

    return {
        "status": "success",
        "message": f"Successfully redeemed code {promo.code}!",
        "code": promo.code,
        "title": promo.title,
        "rewards": granted_summary,
    }


def get_user_badges(db: Session, user_id: uuid.UUID) -> List[Dict[str, Any]]:
    """Returns all badges earned by the user, noting the primary equipped badge."""
    seed_default_badges_if_empty(db)

    # Dynamic auto-eval: Check 100-day streak
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user and getattr(user, "streak", 0) and user.streak >= 100:
        c_badge = db.query(models.MemberBadge).filter(models.MemberBadge.slug == "century_club").first()
        if c_badge:
            existing = db.query(models.UserBadge).filter(
                models.UserBadge.user_id == user_id,
                models.UserBadge.badge_id == c_badge.id
            ).first()
            if not existing:
                db.add(models.UserBadge(user_id=user_id, badge_id=c_badge.id, is_primary=False))
                db.commit()

    user_badges = db.query(models.UserBadge).filter(models.UserBadge.user_id == user_id).all()
    results = []
    for ub in user_badges:
        b = ub.badge
        if not b:
            continue
        results.append({
            "id": str(b.id),
            "slug": b.slug,
            "name": b.name,
            "description": b.description,
            "icon": b.icon,
            "color_hex": b.color_hex,
            "gradient_start": b.gradient_start,
            "gradient_end": b.gradient_end,
            "is_primary": ub.is_primary,
            "awarded_at": ub.awarded_at.isoformat() if ub.awarded_at else None,
        })
    return results


def equip_primary_badge(db: Session, user_id: uuid.UUID, badge_id: uuid.UUID) -> Dict[str, Any]:
    """Sets a specific badge as the user's primary equipped badge."""
    target_ub = db.query(models.UserBadge).filter(
        models.UserBadge.user_id == user_id,
        models.UserBadge.badge_id == badge_id
    ).first()
    if not target_ub:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="You have not unlocked this badge.")

    # Un-equip existing primary badges
    db.query(models.UserBadge).filter(
        models.UserBadge.user_id == user_id,
        models.UserBadge.is_primary == True
    ).update({"is_primary": False})

    target_ub.is_primary = True
    db.commit()
    db.refresh(target_ub)

    b = target_ub.badge
    return {
        "status": "success",
        "message": f"Equipped '{b.name}' as your primary badge!",
        "badge": {
            "id": str(b.id),
            "slug": b.slug,
            "name": b.name,
            "icon": b.icon,
            "color_hex": b.color_hex,
            "gradient_start": b.gradient_start,
            "gradient_end": b.gradient_end,
            "is_primary": True,
        }
    }


def resolve_user_primary_badge(db: Session, user_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """Resolves the user's active badge for chat and profile serialization."""
    if not user_id:
        return None

    # 1. Equipped primary badge
    ub = db.query(models.UserBadge).filter(
        models.UserBadge.user_id == user_id,
        models.UserBadge.is_primary == True
    ).first()
    if ub and ub.badge:
        return {
            "id": str(ub.badge.id),
            "slug": ub.badge.slug,
            "name": ub.badge.name,
            "icon": ub.badge.icon,
            "color_hex": ub.badge.color_hex,
            "gradient_start": ub.badge.gradient_start,
            "gradient_end": ub.badge.gradient_end,
        }

    # 2. Fallback to any earned badge (e.g. campus_ambassador or first available)
    first_ub = db.query(models.UserBadge).filter(models.UserBadge.user_id == user_id).first()
    if first_ub and first_ub.badge:
        return {
            "id": str(first_ub.badge.id),
            "slug": first_ub.badge.slug,
            "name": first_ub.badge.name,
            "icon": first_ub.badge.icon,
            "color_hex": first_ub.badge.color_hex,
            "gradient_start": first_ub.badge.gradient_start,
            "gradient_end": first_ub.badge.gradient_end,
        }

    return None


_last_rank_eval = 0.0


def evaluate_rank_badges(db: Session, force: bool = False):
    """
    Evaluates platform leaderboards and dynamically awards/migrates the automated rank badges:
    - streak_king: Rank #1 on individual daily streak.
    - duo_kings: Rank #1 partnership on duo streak.
    Throttled to once every 10 minutes unless force=True.
    """
    global _last_rank_eval
    import time
    now_ts = time.time()
    if not force and (now_ts - _last_rank_eval) < 600:
        return
    _last_rank_eval = now_ts

    seed_default_badges_if_empty(db)

    try:
        # 1. Streak King
        sk_badge = db.query(models.MemberBadge).filter(models.MemberBadge.slug == "streak_king").first()
        if sk_badge:
            top_user = db.query(models.User).filter(models.User.streak > 0).order_by(models.User.streak.desc()).first()
            if top_user:
                db.query(models.UserBadge).filter(
                    models.UserBadge.badge_id == sk_badge.id,
                    models.UserBadge.user_id != top_user.id
                ).delete(synchronize_session=False)

                has_badge = db.query(models.UserBadge).filter(
                    models.UserBadge.badge_id == sk_badge.id,
                    models.UserBadge.user_id == top_user.id
                ).first()
                if not has_badge:
                    db.add(models.UserBadge(user_id=top_user.id, badge_id=sk_badge.id, is_primary=False))

        # 2. Duo Kings
        dk_badge = db.query(models.MemberBadge).filter(models.MemberBadge.slug == "duo_kings").first()
        if dk_badge:
            top_duo = db.query(models.StudyPartnership).filter(
                models.StudyPartnership.status == "active",
                models.StudyPartnership.duo_streak > 0
            ).order_by(models.StudyPartnership.duo_streak.desc()).first()
            if top_duo:
                target_ids = {top_duo.user1_id, top_duo.user2_id}
                db.query(models.UserBadge).filter(
                    models.UserBadge.badge_id == dk_badge.id,
                    ~models.UserBadge.user_id.in_(target_ids)
                ).delete(synchronize_session=False)

                for uid in target_ids:
                    if uid:
                        has_b = db.query(models.UserBadge).filter(
                            models.UserBadge.badge_id == dk_badge.id,
                            models.UserBadge.user_id == uid
                        ).first()
                        if not has_b:
                            db.add(models.UserBadge(user_id=uid, badge_id=dk_badge.id, is_primary=False))

        db.commit()
    except Exception as ex:
        db.rollback()
        print(f"[promo_service] evaluate_rank_badges note: {ex}")

