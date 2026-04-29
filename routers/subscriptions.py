from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
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
    plans = db.query(models.StudyPlan).all()
    
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
    sub = db.query(models.UserSubscription).filter(
        models.UserSubscription.user_id == current_user.id
    ).first()

    # Auto-create a free tier record if they don't have one
    if not sub:
        # Ensure the 'free' plan exists in the DB first
        free_plan = db.query(models.StudyPlan).filter(models.StudyPlan.id == "free").first()
        if not free_plan:
             raise HTTPException(status_code=500, detail="Database missing 'free' base plan.")
             
        sub = models.UserSubscription(user_id=current_user.id, plan_id="free")
        db.add(sub)
        db.commit()
        db.refresh(sub)

    # Calculate remaining limits
    mat_remaining = None
    if sub.plan.materials_limit is not None:
        mat_remaining = max(0, sub.plan.materials_limit - sub.materials_uploaded)
        
    gen_remaining = None
    if sub.plan.generations_limit is not None:
        gen_remaining = max(0, sub.plan.generations_limit - sub.generations_used)

    return {
        "plan_id": sub.plan_id,
        "label": sub.plan.label,
        
        "materials_used": sub.materials_uploaded,
        "materials_limit": sub.plan.materials_limit,
        "materials_remaining": mat_remaining,
        
        "generations_used": sub.generations_used,
        "generations_limit": sub.plan.generations_limit,
        "generations_remaining": gen_remaining
    }