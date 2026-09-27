from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session, joinedload
from uuid import UUID
from typing import Optional, List
from datetime import datetime, timezone
import models
import auth
from database import get_db

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.get("", status_code=status.HTTP_200_OK)
def get_user_notifications(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    category: Optional[str] = Query(None),
    user = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """Fetches paginated in-app notifications for the authenticated user."""
    query = (
        db.query(models.UserNotification)
        .filter(models.UserNotification.user_id == user.id)
        .options(joinedload(models.UserNotification.sender))
    )

    if category and category.lower() != "all":
        query = query.filter(models.UserNotification.category == category.lower())

    total = query.count()
    items = query.order_by(models.UserNotification.created_at.desc()).offset(offset).limit(limit).all()

    unread_count = (
        db.query(models.UserNotification)
        .filter(models.UserNotification.user_id == user.id, models.UserNotification.is_read == False)
        .count()
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

        results.append({
            "id": str(item.id),
            "title": item.title,
            "body": item.body,
            "category": item.category or "general",
            "action_route": item.action_route,
            "data_payload": item.data_payload or {},
            "is_read": bool(item.is_read),
            "created_at": item.created_at.isoformat() if item.created_at else datetime.now(timezone.utc).isoformat(),
            "sender": sender_data
        })

    return {
        "unread_count": unread_count,
        "total": total,
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
