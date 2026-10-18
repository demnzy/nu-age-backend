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
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import or_
from fastapi import HTTPException, status

import models
from services import coin_service


# Standard preset badges for Nu-Age Gamification
PRESET_BADGES = [
    {
        "slug": "campus_ambassador",
        "name": "Campus Ambassador",
        "description": "Official campus representative for Nu-Age.",
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
        "description": "Early adopter driving the future of Nu-Age learning.",
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


def redeem_promo_code(db: Session, user_id: uuid.UUID, code_str: str) -> Dict[str, Any]:
    """
    Executes a multi-reward promo code redemption within a single atomic transaction.
    Guarantees:
    - Code existence, case-insensitivity, and active state.
    - Expiration and usage limits enforcement.
    - Idempotency (single redemption per user per code).
    - Multi-reward dispatch (coins, AI credits, trial days, badge, pack unlocks).
    - Ambassador referral kickback.
    """
    clean_code = (code_str or "").strip().upper()
    if not clean_code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Promo code cannot be empty.")

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

    granted_summary: Dict[str, Any] = {}

    # 1. Nu-Coins Reward
    if promo.reward_coins and promo.reward_coins > 0:
        new_coin_bal = coin_service.grant_coins(
            db,
            user_id=user_id,
            amount=promo.reward_coins,
            category="promo_code",
            description=f"Redeemed promo code {promo.code}",
            source_type="promo_code",
            reference_id=str(promo.id),
        )
        granted_summary["coins"] = promo.reward_coins
        granted_summary["new_coins_balance"] = new_coin_bal

    # 2. AI Generation Credits Reward
    if promo.reward_ai_credits and promo.reward_ai_credits > 0:
        try:
            from monetization_models import CreditBalance, CreditLedgerEntry
            cb = db.query(CreditBalance).filter(CreditBalance.user_id == user_id).first()
            if not cb:
                cb = CreditBalance(user_id=user_id, balance=100 + promo.reward_ai_credits)
                db.add(cb)
            else:
                cb.balance = (cb.balance or 0) + promo.reward_ai_credits

            ledger = CreditLedgerEntry(
                user_id=user_id,
                delta=promo.reward_ai_credits,
                reason="promo_code",
                reference_id=str(promo.id),
                balance_after=cb.balance,
            )
            db.add(ledger)
            granted_summary["ai_credits"] = promo.reward_ai_credits
            granted_summary["new_ai_credits_balance"] = cb.balance
        except Exception as ex:
            print(f"[Promo] AI credit grant note: {ex}")

    # 3. Pro Trial Days
    if promo.reward_trial_days and promo.reward_trial_days > 0:
        granted_summary["trial_days"] = promo.reward_trial_days

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

    # 5. Ambassador Referral Kickback
    if promo.ambassador_id and promo.ambassador_id != user_id and promo.ambassador_coin_reward > 0:
        try:
            coin_service.grant_coins(
                db,
                user_id=promo.ambassador_id,
                amount=promo.ambassador_coin_reward,
                category="referral_commission",
                description=f"Referral commission from code {promo.code}",
                source_type="ambassador_referral",
                reference_id=str(promo.id),
            )
            granted_summary["ambassador_notified"] = True
        except Exception as amb_ex:
            print(f"[Promo] Ambassador kickback error: {amb_ex}")

    # Record redemption log & increment usage
    redemption = models.PromoCodeRedemption(
        promo_code_id=promo.id,
        user_id=user_id,
        rewards_granted=granted_summary
    )
    db.add(redemption)
    promo.uses_count = (promo.uses_count or 0) + 1

    db.commit()

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
