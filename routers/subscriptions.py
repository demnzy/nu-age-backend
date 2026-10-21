from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload
from typing import Dict
import models
import schemas
from database import get_db
from services import auth

router = APIRouter(prefix="/subscription", tags=["Subscription"])

@router.get("/plans/config", response_model=Dict[str, schemas.PlanConfigItem])
def get_plans_config(db: Session = Depends(get_db)):
    """
    Returns the dynamic plan configuration from the database 
    formatted exactly as the frontend expects.
    """
    plans = db.query(models.StoreProduct).filter(
        models.StoreProduct.category == "plan",
        models.StoreProduct.is_active == True
    ).order_by(models.StoreProduct.display_order.asc()).all()
    
    if not plans:
        raise HTTPException(status_code=404, detail="No study plans configured in the database.")
        
    config_dict = {}
    for plan in plans:
        config_dict[plan.id] = {
            "label": plan.label,
            "materials_limit": plan.materials_limit,
            "generations_limit": plan.generations_limit
        }
        
    return config_dict


@router.get("/status", response_model=schemas.SubscriptionStatusResponse)
def get_subscription_status(
    db: Session = Depends(get_db), 
    current_user = Depends(auth.get_current_user)
):
    """
    Calculates the user's remaining limits based on their database plan.
    """
    # Fetch user subscription with the joined plan
    sub = db.query(models.UserSubscription).options(
        joinedload(models.UserSubscription.plan)
    ).filter(
        models.UserSubscription.user_id == current_user.id
    ).first()

    # Auto-create a free tier record if they don't have one
    if not sub:
        # Ensure the 'free' plan exists in the DB first
        free_plan = db.query(models.StoreProduct).filter(models.StoreProduct.id == "free").first()
        if not free_plan:
             raise HTTPException(status_code=500, detail="Database missing 'free' base plan.")
             
        sub = models.UserSubscription(user_id=current_user.id, plan_id="free")
        db.add(sub)
        db.commit()
        db.refresh(sub)

    # 1. Sync and preserve accurate materials upload count
    actual_mats = db.query(models.StudyMaterial).filter(models.StudyMaterial.user_id == current_user.id).count()
    if sub.materials_uploaded < actual_mats:
        sub.materials_uploaded = actual_mats
        db.commit()

    # 2. Evaluate Subscription Lifecycle & Expiration
    now = datetime.now(timezone.utc)
    current_status = getattr(sub, "status", "active") or "active"
    days_left = None
    is_expired = False

    if sub.expires_at:
        if now > sub.expires_at:
            if sub.grace_period_until and now <= sub.grace_period_until:
                current_status = "grace_period"
            else:
                current_status = "expired"
                is_expired = True
            days_left = 0
        else:
            diff = sub.expires_at - now
            days_left = max(0, diff.days)
            current_status = "active"

        if sub.status != current_status:
            sub.status = current_status
            db.commit()

    # Determine effective plan
    # If expired past grace period, effective plan falls back to free
    effective_plan = sub.plan
    effective_plan_id = sub.plan_id
    if is_expired and sub.plan_id != "free":
        free_plan = db.query(models.StoreProduct).filter(models.StoreProduct.id == "free").first()
        if free_plan:
            effective_plan = free_plan
            effective_plan_id = "free"

    # 3. Calculate remaining limits taking bonus_materials and bonus_generations into account
    bonus_mats = getattr(sub, "bonus_materials", 0) or 0
    bonus_gens = getattr(sub, "bonus_generations", 0) or 0

    effective_mat_limit = None
    mat_remaining = None
    if effective_plan.materials_limit is not None:
        effective_mat_limit = effective_plan.materials_limit + bonus_mats
        mat_remaining = max(0, effective_mat_limit - sub.materials_uploaded)

    effective_gen_limit = None
    gen_remaining = None
    if effective_plan.generations_limit is not None:
        effective_gen_limit = effective_plan.generations_limit + bonus_gens
        gen_remaining = max(0, effective_gen_limit - sub.generations_used)

    return {
        "plan_id": effective_plan_id,
        "label": effective_plan.label,
        "subscribed_plan_id": sub.plan_id,
        "status": current_status,
        "expires_at": sub.expires_at,
        "grace_period_until": sub.grace_period_until,
        "billing_cycle": getattr(sub, "billing_cycle", "one-time") or "one-time",
        "days_remaining": days_left,
        "can_renew": (sub.plan_id != "free"),

        "materials_used": sub.materials_uploaded,
        "materials_limit": effective_mat_limit if effective_mat_limit is not None else effective_plan.materials_limit,
        "materials_remaining": mat_remaining,
        "bonus_materials": bonus_mats,

        "generations_used": sub.generations_used,
        "generations_limit": effective_gen_limit if effective_gen_limit is not None else effective_plan.generations_limit,
        "generations_remaining": gen_remaining,
        "bonus_generations": bonus_gens,
    }


@router.post("/check-renewals")
def trigger_subscription_renewals_check(
    db: Session = Depends(get_db),
    current_user = Depends(auth.get_current_user)
):
    """
    Evaluates subscriptions nearing expiration or past grace period.
    Dispatches 3-day warnings, grace period alerts, or suspension notifications.
    """
    from services.subscription_service import check_subscription_renewals
    return check_subscription_renewals(db)