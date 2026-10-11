"""
routers/objectives.py
──────────────────────────────────────────────────────────────────────────────
Endpoints for Platform Objectives, Nu-Coin Milestones, and Wallet Ledger.
──────────────────────────────────────────────────────────────────────────────
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Dict, Any
import models
from database import get_db
from services import auth
from services.coin_service import (
    get_user_coins,
    claim_objective_reward,
    grant_coins,
    spend_coins,
)

router = APIRouter(prefix="/objectives", tags=["Objectives & Nu-Coins"])


@router.get("", status_code=status.HTTP_200_OK)
def get_platform_objectives(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Returns all active platform preliminary objectives with current user's progress.
    """
    objectives = db.query(models.PlatformObjective).filter(
        models.PlatformObjective.is_active == True
    ).order_by(models.PlatformObjective.coin_reward.asc()).all()

    # Fetch user's progress records
    user_prog_rows = db.query(models.UserObjectiveProgress).filter(
        models.UserObjectiveProgress.user_id == current_user.id
    ).all()
    prog_map = {p.objective_code: p for p in user_prog_rows}

    results = []
    for obj in objectives:
        prog = prog_map.get(obj.code)
        c_val = prog.current_value if prog else 0
        is_comp = prog.is_completed if prog else False
        is_clm = prog.is_claimed if prog else False

        # Live sync if target met
        if c_val >= obj.target_value and not is_comp:
            is_comp = True

        pct = min(1.0, round(c_val / max(obj.target_value, 1), 2))

        results.append({
            "id": str(obj.id),
            "code": obj.code,
            "title": obj.title,
            "description": obj.description,
            "category": obj.category,
            "target_value": obj.target_value,
            "target_unit": obj.target_unit,
            "current_value": min(c_val, obj.target_value),
            "coin_reward": obj.coin_reward,
            "icon": obj.icon,
            "progress_ratio": pct,
            "is_completed": is_comp,
            "is_claimed": is_clm,
        })

    balance = get_user_coins(db, current_user.id)

    return {
        "status": "success",
        "coins_balance": balance,
        "objectives": results,
    }


@router.post("/{objective_code}/claim", status_code=status.HTTP_200_OK)
def claim_objective(
    objective_code: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Claims the Nu-Coin reward for a completed objective.
    """
    success, coins, msg = claim_objective_reward(db, current_user.id, objective_code)
    if not success:
        raise HTTPException(status_code=400, detail=msg)

    new_balance = get_user_coins(db, current_user.id)
    return {
        "status": "success",
        "message": msg,
        "coins_awarded": coins,
        "coins_balance": new_balance,
    }


@router.get("/wallet/ledger", status_code=status.HTTP_200_OK)
def get_coin_wallet_ledger(
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Returns the user's total Nu-Coin balance and history of transactions.
    """
    balance = get_user_coins(db, current_user.id)
    txs = db.query(models.CoinTransaction).filter(
        models.CoinTransaction.user_id == current_user.id
    ).order_by(models.CoinTransaction.created_at.desc()).limit(limit).all()

    tx_list = [
        {
            "id": str(t.id),
            "amount": t.amount,
            "category": t.category,
            "source_type": t.source_type,
            "reference_id": t.reference_id,
            "description": t.description,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in txs
    ]

    return {
        "status": "success",
        "coins_balance": balance,
        "transactions": tx_list,
    }
