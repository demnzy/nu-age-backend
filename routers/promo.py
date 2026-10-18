"""
routers/promo.py
──────────────────────────────────────────────────────────────────────────────
Learner-Facing Promo Codes, Badges, and Ambassador Acceptance Router.
──────────────────────────────────────────────────────────────────────────────
"""

import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional, List, Dict, Any

import models
from database import get_db
from services import auth, promo_service, ambassador_service

router = APIRouter(prefix="/promo", tags=["Promo Codes & Badges"])


class PromoRedeemRequest(BaseModel):
    code: str


class AmbassadorAcceptRequest(BaseModel):
    token: str


@router.post("/redeem", status_code=status.HTTP_200_OK)
def redeem_promo_code_endpoint(
    payload: PromoRedeemRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Redeems a promo code for the authenticated user and atomically delivers all rewards.
    """
    result = promo_service.redeem_promo_code(db, current_user.id, payload.code)
    return result


@router.get("/my-badges", status_code=status.HTTP_200_OK)
def get_my_badges_endpoint(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Retrieves all gamification badges unlocked by the authenticated user.
    """
    badges = promo_service.get_user_badges(db, current_user.id)
    return {
        "status": "success",
        "badges": badges,
    }


@router.put("/my-badges/{badge_id}/equip", status_code=status.HTTP_200_OK)
def equip_badge_endpoint(
    badge_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Equips an unlocked badge as the user's primary display badge.
    """
    result = promo_service.equip_primary_badge(db, current_user.id, badge_id)
    return result


@router.post("/ambassador/accept", status_code=status.HTTP_200_OK)
def accept_ambassador_invitation_endpoint(
    payload: AmbassadorAcceptRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Accepts a campus ambassador invitation token and binds it to the current user's profile.
    """
    result = ambassador_service.accept_ambassador_invitation(db, payload.token, current_user.id)
    return result
