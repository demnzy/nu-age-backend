from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session, joinedload
from uuid import UUID
from typing import Optional, List
from datetime import datetime, timezone
import models
from services import auth
from database import get_db

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.get("", status_code=status.HTTP_200_OK)
def get_user_notifications(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    skip: Optional[int] = Query(None, ge=0),
    category: Optional[str] = Query(None),
    user = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """Fetches paginated in-app notifications for the authenticated user."""
    effective_offset = skip if skip is not None else offset
    query = (
        db.query(models.UserNotification)
        .filter(models.UserNotification.user_id == user.id)
        .options(joinedload(models.UserNotification.sender))
    )

    if category and category.lower() != "all":
        query = query.filter(models.UserNotification.category == category.lower())

    total = query.count()
    items = query.order_by(models.UserNotification.created_at.desc()).offset(effective_offset).limit(limit).all()

    unread_count = (
        db.query(models.UserNotification)
        .filter(models.UserNotification.user_id == user.id, models.UserNotification.is_read == False)
        .count()
    )

    # Fetch all claimed objective codes for this user to guarantee persistent claimed status
    claimed_codes = set(
        r[0] for r in db.query(models.UserObjectiveProgress.objective_code)
        .filter(models.UserObjectiveProgress.user_id == user.id, models.UserObjectiveProgress.is_claimed == True)
        .all()
    )

    results = []
    for item in items:
        sender_data = None
        if item.sender:
            sender_data = {
                "id": str(item.sender.id),
                "name": f"{item.sender.first_name} {item.sender.last_name}".strip(),
                "username": getattr(item.sender, "username", None) or item.sender.first_name,
                "email": item.sender.email
            }

        data_p = dict(item.data_payload or {})
        is_claimed = False
        body_text = item.body or ""
        if item.category == "objective_completed":
            c = data_p.get("code") or data_p.get("objective_code")
            if c in claimed_codes or data_p.get("is_claimed"):
                is_claimed = True
                data_p["is_claimed"] = True
                if "• Claimed" not in body_text:
                    body_text = f"{body_text} • Claimed"

        results.append({
            "id": str(item.id),
            "title": item.title,
            "body": body_text,
            "category": item.category or "general",
            "action_route": item.action_route,
            "data_payload": data_p,
            "is_read": bool(item.is_read),
            "is_claimed": is_claimed,
            "created_at": item.created_at.isoformat() if item.created_at else datetime.now(timezone.utc).isoformat(),
            "sender": sender_data
        })

    return {
        "unread_count": unread_count,
        "total": total,
        "items": results,
        "notifications": results
    }


@router.get("/unread-count", status_code=status.HTTP_200_OK)
def get_unread_count(
    user = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """Fast endpoint for syncing the in-app bottom bar and bell badges."""
    count = (
        db.query(models.UserNotification)
        .filter(models.UserNotification.user_id == user.id, models.UserNotification.is_read == False)
        .count()
    )
    return {"unread_count": count}


@router.put("/read-all", status_code=status.HTTP_200_OK)
def mark_all_notifications_as_read(
    user = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """Marks all notifications for the authenticated user as read."""
    updated = (
        db.query(models.UserNotification)
        .filter(models.UserNotification.user_id == user.id, models.UserNotification.is_read == False)
        .update({"is_read": True}, synchronize_session=False)
    )
    db.commit()
    return {"status": "ok", "updated_count": updated}


# ─────────────────────────────────────────────────────────────────────────────
# DUOLINGO-STYLE RE-ENGAGEMENT & STREAK-SAVER ENGINE ENDPOINTS
# (Must be declared before parameterized /{notification_id} routes)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/engine/run-evaluations", status_code=status.HTTP_200_OK)
def trigger_reengagement_evaluations(
    dry_run: bool = Query(False, description="Simulate without writing notifications or sending pushes"),
    force: bool = Query(False, description="Bypass quiet hours, minimum spacing intervals, and daily volume limits for testing"),
    user_id: Optional[UUID] = Query(None, description="Target specific user ID (optional)"),
    preferred_tier: Optional[int] = Query(None, description="Force evaluate a specific tier (1=streak, 2=duo, 3=daytime, 4=inactivity, 5=course_progress, 6=quest, 7=new_courses)"),
    category: Optional[str] = Query(None, description="Filter/target specific category, e.g. new_courses, course_progress, objective_quest"),
    current_user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """
    Evaluates users for Duolingo-style re-engagement, streak decay warnings,
    duo partner accountability, course in-progress nudges, and new courses discovery.
    If invoked by a learner, scopes evaluation safely to that user.
    Admins can trigger system-wide evaluations or target specific users.
    """
    from services.reengagement_engine import evaluate_and_dispatch_nudges

    # Learner scope restriction
    target_id = user_id
    user_role = str(getattr(current_user, "role", "")).lower()
    if "admin" not in user_role:
        target_id = current_user.id

    stats = evaluate_and_dispatch_nudges(
        db=db,
        force=force,
        dry_run=dry_run,
        target_user_id=target_id,
        preferred_tier=preferred_tier,
        category=category,
    )
    return {
        "status": "success",
        "dry_run": dry_run,
        "force": force,
        "stats": stats,
    }


@router.get("/engine/preview", status_code=status.HTTP_200_OK)
def preview_my_reengagement_nudge(
    preferred_tier: Optional[int] = Query(None, description="Force preview of specific tier (e.g. 7 for new_courses, 5 for course_progress)"),
    category: Optional[str] = Query(None, description="Filter preview to specific category (e.g. new_courses, course_progress)"),
    current_user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """
    Preview what quirky notification the current user would receive right now
    based on their current streak, duo partnerships, in-progress courses, and study activity.
    """
    from services.reengagement_engine import evaluate_and_dispatch_nudges
    stats = evaluate_and_dispatch_nudges(
        db=db,
        force=True,
        dry_run=True,
        target_user_id=current_user.id,
        preferred_tier=preferred_tier,
        category=category,
    )
    records = stats.get("dispatched_records", [])
    preview = records[0] if records else None
    return {
        "has_nudge": preview is not None,
        "nudge": preview,
        "context": {
            "streak": current_user.streak,
            "last_login_date": current_user.last_login_date.isoformat() if current_user.last_login_date else None,
        }
    }




@router.put("/{notification_id}/read", status_code=status.HTTP_200_OK)
def mark_notification_as_read(
    notification_id: UUID,
    user = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """Marks a single notification as read."""
    notif = (
        db.query(models.UserNotification)
        .filter(models.UserNotification.id == notification_id, models.UserNotification.user_id == user.id)
        .first()
    )
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")

    notif.is_read = True
    db.commit()
    return {"status": "ok", "id": str(notif.id)}
