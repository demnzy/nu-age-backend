"""
services/coin_service.py
──────────────────────────────────────────────────────────────────────────────
Centralized Nu-Coins economy engine & Platform Objectives tracker.
- Manages user coin balances with an immutable ledger (CoinTransaction).
- Evaluates and awards Duo Streak milestones to both accountability partners.
- Tracks and claims platform preliminary objectives (courses, streaks, duels, focus time).
──────────────────────────────────────────────────────────────────────────────
"""

import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple, List, Dict, Any
from sqlalchemy.orm import Session
import models


# Duo Streak milestone rewards in Nu-Coins (awarded to BOTH partners upon reaching milestone)
DUO_STREAK_MILESTONES = {
    3: 30,
    5: 60,
    7: 100,
    14: 250,
    30: 600,
    60: 1500,
    100: 3000,
}


def get_user_coins(db: Session, user_id: uuid.UUID) -> int:
    """Returns the user's current verified Nu-Coin balance."""
    u = db.query(models.User).filter(models.User.id == user_id).first()
    return getattr(u, "coins_balance", 0) or 0


def grant_coins(
    db: Session,
    user_id: uuid.UUID,
    amount: int,
    category: str,
    description: str = "",
    source_type: Optional[str] = None,
    reference_id: Optional[str] = None,
) -> Tuple[bool, int]:
    """
    Grants Nu-Coins to a user, appends an immutable transaction record, and updates User.coins_balance.
    Returns (success, new_balance).
    """
    if amount <= 0:
        return False, get_user_coins(db, user_id)

    u = db.query(models.User).filter(models.User.id == user_id).first()
    if not u:
        return False, 0

    curr = getattr(u, "coins_balance", 0) or 0
    new_balance = curr + amount
    u.coins_balance = new_balance

    tx = models.CoinTransaction(
        user_id=user_id,
        amount=amount,
        category=category,
        source_type=source_type,
        reference_id=str(reference_id) if reference_id else None,
        description=description or f"Earned +{amount} Nu-Coins ({category})",
    )
    db.add(tx)
    db.commit()
    return True, new_balance


def spend_coins(
    db: Session,
    user_id: uuid.UUID,
    amount: int,
    category: str,
    description: str = "",
    source_type: Optional[str] = None,
    reference_id: Optional[str] = None,
) -> Tuple[bool, int, str]:
    """
    Spends Nu-Coins from user balance.
    Returns (success, new_balance, message).
    """
    if amount <= 0:
        return False, get_user_coins(db, user_id), "Invalid spend amount."

    u = db.query(models.User).filter(models.User.id == user_id).first()
    if not u:
        return False, 0, "User not found."

    curr = getattr(u, "coins_balance", 0) or 0
    if curr < amount:
        return False, curr, f"Insufficient Nu-Coins balance ({curr} available, {amount} required)."

    new_balance = curr - amount
    u.coins_balance = new_balance

    tx = models.CoinTransaction(
        user_id=user_id,
        amount=-amount,
        category=category,
        source_type=source_type,
        reference_id=str(reference_id) if reference_id else None,
        description=description or f"Spent {amount} Nu-Coins ({category})",
    )
    db.add(tx)
    db.commit()
    return True, new_balance, "Success"


def check_and_award_duo_streak_milestones(
    db: Session,
    partnership: models.StudyPartnership,
    new_streak: int,
) -> List[Dict[str, Any]]:
    """
    Checks if a newly reached Duo Streak hit a reward milestone.
    If so, grants coins to BOTH partners and logs the reward.
    Returns list of awarded milestones.
    """
    awarded = []
    if new_streak in DUO_STREAK_MILESTONES:
        reward_coins = DUO_STREAK_MILESTONES[new_streak]
        desc = f"Reached {new_streak}-day Duo Streak with study partner!"

        # Grant to Partner A
        grant_coins(
            db=db,
            user_id=partnership.user_a_id,
            amount=reward_coins,
            category="streak_milestone",
            description=desc,
            source_type="partnership",
            reference_id=str(partnership.id),
        )

        # Grant to Partner B
        grant_coins(
            db=db,
            user_id=partnership.user_b_id,
            amount=reward_coins,
            category="streak_milestone",
            description=desc,
            source_type="partnership",
            reference_id=str(partnership.id),
        )

        awarded.append({
            "streak": new_streak,
            "reward_coins": reward_coins,
            "description": desc,
        })
    return awarded


def track_objective_progress(
    db: Session,
    user_id: uuid.UUID,
    objective_code: str,
    progress_delta: int = 1,
    set_absolute: bool = False,
) -> Optional[models.UserObjectiveProgress]:
    """
    Updates a user's progress toward a platform objective.
    Automatically marks as completed if target_value is reached.
    """
    obj = db.query(models.PlatformObjective).filter(
        models.PlatformObjective.code == objective_code,
        models.PlatformObjective.is_active == True
    ).first()

    if not obj:
        return None

    prog = db.query(models.UserObjectiveProgress).filter(
        models.UserObjectiveProgress.user_id == user_id,
        models.UserObjectiveProgress.objective_code == objective_code
    ).first()

    if not prog:
        prog = models.UserObjectiveProgress(
            user_id=user_id,
            objective_code=objective_code,
            current_value=0,
            target_value=obj.target_value,
            is_completed=False,
            is_claimed=False,
        )
        db.add(prog)

    if set_absolute:
        prog.current_value = progress_delta
    else:
        prog.current_value = (prog.current_value or 0) + progress_delta

    if prog.current_value >= prog.target_value and not prog.is_completed:
        prog.is_completed = True
        prog.completed_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(prog)
    return prog


def claim_objective_reward(
    db: Session,
    user_id: uuid.UUID,
    objective_code: str,
) -> Tuple[bool, int, str]:
    """
    Claims the Nu-Coin reward for a completed objective.
    Returns (success, coins_awarded, message).
    """
    obj = db.query(models.PlatformObjective).filter(
        models.PlatformObjective.code == objective_code
    ).first()
    if not obj:
        return False, 0, "Objective not found."

    prog = db.query(models.UserObjectiveProgress).filter(
        models.UserObjectiveProgress.user_id == user_id,
        models.UserObjectiveProgress.objective_code == objective_code
    ).first()

    if not prog or not prog.is_completed:
        return False, 0, "Objective is not yet completed."

    if prog.is_claimed:
        return False, 0, "Reward for this objective has already been claimed."

    prog.is_claimed = True
    prog.claimed_at = datetime.now(timezone.utc)

    grant_coins(
        db=db,
        user_id=user_id,
        amount=obj.coin_reward,
        category="objective",
        description=f"Claimed reward for objective: {obj.title}",
        source_type="objective",
        reference_id=str(obj.id),
    )

    db.commit()
    return True, obj.coin_reward, f"Successfully claimed +{obj.coin_reward} Nu-Coins!"
