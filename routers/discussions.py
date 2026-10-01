from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from sqlalchemy import desc, func
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
import uuid
from datetime import datetime

import models
from database import get_db
from services import auth

router = APIRouter(tags=["Course Discussions"])


# ==========================================
# Pydantic Schemas
# ==========================================

class DiscussionCreatePayload(BaseModel):
    title: str = Field(..., min_length=3, max_length=255)
    content: str = Field(..., min_length=5)
    category: str = Field("question", pattern="^(question|idea|discussion|resource)$")


class ReplyCreatePayload(BaseModel):
    content: str = Field(..., min_length=2)


def _serialize_user(user: Optional[models.User]) -> Dict[str, Any]:
    if not user:
        return {"id": "", "name": "Anonymous", "role": "student", "avatar": None}
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or user.username or "Student"
    return {
        "id": str(user.id),
        "name": full_name,
        "username": user.username,
        "role": str(user.role.value if hasattr(user.role, "value") else user.role),
        "avatar": user.profile_picture_url,
    }


def _serialize_discussion(d: models.CourseDiscussion, current_user_id: Optional[uuid.UUID] = None, user_upvoted: bool = False) -> Dict[str, Any]:
    return {
        "id": str(d.id),
        "course_id": str(d.course_id),
        "title": d.title,
        "content": d.content,
        "category": d.category or "question",
        "upvotes_count": d.upvotes_count or 0,
        "replies_count": d.replies_count or 0,
        "is_pinned": bool(d.is_pinned),
        "is_resolved": bool(d.is_resolved),
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
        "author": _serialize_user(d.author),
        "has_upvoted": user_upvoted,
        "is_owner": bool(current_user_id and d.user_id == current_user_id),
    }


def _serialize_reply(r: models.CourseDiscussionReply, current_user_id: Optional[uuid.UUID] = None, user_upvoted: bool = False) -> Dict[str, Any]:
    return {
        "id": str(r.id),
        "discussion_id": str(r.discussion_id),
        "content": r.content,
        "upvotes_count": r.upvotes_count or 0,
        "is_endorsed": bool(r.is_endorsed),
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        "author": _serialize_user(r.author),
        "has_upvoted": user_upvoted,
        "is_owner": bool(current_user_id and r.user_id == current_user_id),
    }


# ==========================================
# Endpoints
# ==========================================

@router.get("/courses/{course_id}/discussions")
def list_course_discussions(
    course_id: uuid.UUID,
    category: Optional[str] = None,
    search: Optional[str] = None,
    sort_by: str = Query("latest", pattern="^(latest|top|unanswered)$"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """
    List discussion board posts for a specific course with category filtering and search.
    """
    query = db.query(models.CourseDiscussion).filter(models.CourseDiscussion.course_id == course_id)

    if category and category.lower() != "all":
        if category.lower() == "solved":
            query = query.filter(models.CourseDiscussion.is_resolved == True)
        else:
            query = query.filter(models.CourseDiscussion.category == category.lower())

    if search and search.strip():
        term = f"%{search.strip().lower()}%"
        query = query.filter(
            func.lower(models.CourseDiscussion.title).like(term)
            | func.lower(models.CourseDiscussion.content).like(term)
        )

    if sort_by == "top":
        query = query.order_by(models.CourseDiscussion.is_pinned.desc(), models.CourseDiscussion.upvotes_count.desc(), models.CourseDiscussion.created_at.desc())
    elif sort_by == "unanswered":
        query = query.filter(models.CourseDiscussion.replies_count == 0).order_by(models.CourseDiscussion.created_at.desc())
    else:
        query = query.order_by(models.CourseDiscussion.is_pinned.desc(), models.CourseDiscussion.created_at.desc())

    total = query.count()
    items = query.offset(offset).limit(limit).all()

    # Pre-fetch upvoted IDs by current user
    item_ids = [it.id for it in items]
    user_upvotes = set()
    if item_ids and current_user:
        upv_records = db.query(models.CourseDiscussionUpvote.discussion_id).filter(
            models.CourseDiscussionUpvote.user_id == current_user.id,
            models.CourseDiscussionUpvote.discussion_id.in_(item_ids)
        ).all()
        user_upvotes = {u[0] for u in upv_records}

    return {
        "total": total,
        "discussions": [_serialize_discussion(d, current_user.id, d.id in user_upvotes) for d in items]
    }


@router.post("/courses/{course_id}/discussions", status_code=status.HTTP_201_CREATED)
def create_course_discussion(
    course_id: uuid.UUID,
    payload: DiscussionCreatePayload,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """
    Create a new discussion topic or question on the course board.
    """
    course = db.query(models.Course).filter(models.Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    new_disc = models.CourseDiscussion(
        course_id=course_id,
        user_id=current_user.id,
        title=payload.title.strip(),
        content=payload.content.strip(),
        category=payload.category.lower(),
        upvotes_count=0,
        replies_count=0,
        is_pinned=False,
        is_resolved=False,
    )
    db.add(new_disc)
    db.commit()
    db.refresh(new_disc)

    # Dispatch notification to course instructor/admin
    try:
        from services.notifications import dispatch_notification
        targets = []
        if course.admin_id and course.admin_id != current_user.id:
            targets.append(course.admin_id)
        if course.teacher_id and course.teacher_id != current_user.id:
            targets.append(course.teacher_id)
        if targets:
            category_label = payload.category.capitalize()
            sender_name = current_user.full_name or "A learner"
            dispatch_notification(
                db=db,
                recipient_user_ids=targets,
                title=f"New {category_label} in {course.name}",
                body=f"{sender_name} posted: '{payload.title[:60]}'",
                category="discussion",
                action_route=f"/course/{course_id}?tab=discuss",
                sender_id=current_user.id,
                data_payload={"course_id": str(course_id), "discussion_id": str(new_disc.id)},
                send_push=True,
            )
    except Exception as ex:
        print(f"[Discussion Notification Error]: {ex}")

    return _serialize_discussion(new_disc, current_user.id, False)


@router.get("/discussions/{discussion_id}")
def get_discussion_details(
    discussion_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """
    Get a single discussion post along with all its replies.
    """
    disc = db.query(models.CourseDiscussion).filter(models.CourseDiscussion.id == discussion_id).first()
    if not disc:
        raise HTTPException(status_code=404, detail="Discussion not found")

    # Has current user upvoted post?
    user_upvoted_disc = bool(
        db.query(models.CourseDiscussionUpvote).filter(
            models.CourseDiscussionUpvote.user_id == current_user.id,
            models.CourseDiscussionUpvote.discussion_id == disc.id
        ).first()
    )

    # Replies and reply upvotes
    replies = disc.replies
    reply_ids = [r.id for r in replies]
    user_reply_upvotes = set()
    if reply_ids:
        upv_reply_records = db.query(models.CourseDiscussionUpvote.reply_id).filter(
            models.CourseDiscussionUpvote.user_id == current_user.id,
            models.CourseDiscussionUpvote.reply_id.in_(reply_ids)
        ).all()
        user_reply_upvotes = {u[0] for u in upv_reply_records}

    serialized_replies = [
        _serialize_reply(r, current_user.id, r.id in user_reply_upvotes)
        for r in replies
    ]

    res = _serialize_discussion(disc, current_user.id, user_upvoted_disc)
    res["replies"] = serialized_replies
    return res


@router.post("/discussions/{discussion_id}/replies", status_code=status.HTTP_201_CREATED)
def add_discussion_reply(
    discussion_id: uuid.UUID,
    payload: ReplyCreatePayload,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """
    Post a reply or answer to a course discussion thread.
    """
    disc = db.query(models.CourseDiscussion).filter(models.CourseDiscussion.id == discussion_id).first()
    if not disc:
        raise HTTPException(status_code=404, detail="Discussion not found")

    reply = models.CourseDiscussionReply(
        discussion_id=discussion_id,
        user_id=current_user.id,
        content=payload.content.strip(),
        upvotes_count=0,
        is_endorsed=False,
    )
    db.add(reply)
    disc.replies_count = (disc.replies_count or 0) + 1
    db.commit()
    db.refresh(reply)

    # Dispatch notifications for reply to thread author and previous repliers
    try:
        from services.notifications import dispatch_notification
        targets = set()
        if disc.user_id and disc.user_id != current_user.id:
            targets.add(disc.user_id)
        
        for existing_r in disc.replies:
            if existing_r.user_id and existing_r.user_id != current_user.id:
                targets.add(existing_r.user_id)

        if targets:
            category_label = (disc.category or "topic").capitalize()
            sender_name = current_user.full_name or "A classmate"
            snippet = payload.content.strip()
            if len(snippet) > 80:
                snippet = snippet[:77] + "..."
            dispatch_notification(
                db=db,
                recipient_user_ids=list(targets),
                title=f"New reply to {category_label}: '{disc.title[:45]}'",
                body=f"{sender_name}: {snippet}",
                category="discussion",
                action_route=f"/course/{disc.course_id}?tab=discuss&topic={disc.id}",
                sender_id=current_user.id,
                data_payload={
                    "course_id": str(disc.course_id),
                    "discussion_id": str(disc.id),
                    "reply_id": str(reply.id),
                },
                send_push=True,
            )
    except Exception as ex:
        print(f"[Reply Notification Error]: {ex}")

    return _serialize_reply(reply, current_user.id, False)


@router.post("/discussions/{discussion_id}/upvote")
def toggle_discussion_upvote(
    discussion_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """
    Toggle upvote on a discussion thread.
    """
    disc = db.query(models.CourseDiscussion).filter(models.CourseDiscussion.id == discussion_id).first()
    if not disc:
        raise HTTPException(status_code=404, detail="Discussion not found")

    existing = db.query(models.CourseDiscussionUpvote).filter(
        models.CourseDiscussionUpvote.user_id == current_user.id,
        models.CourseDiscussionUpvote.discussion_id == discussion_id,
    ).first()

    if existing:
        db.delete(existing)
        disc.upvotes_count = max(0, (disc.upvotes_count or 1) - 1)
        upvoted = False
    else:
        new_upvote = models.CourseDiscussionUpvote(
            user_id=current_user.id,
            discussion_id=discussion_id,
        )
        db.add(new_upvote)
        disc.upvotes_count = (disc.upvotes_count or 0) + 1
        upvoted = True

    db.commit()
    return {"upvoted": upvoted, "upvotes_count": disc.upvotes_count}


@router.post("/discussions/replies/{reply_id}/upvote")
def toggle_reply_upvote(
    reply_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """
    Toggle upvote on an answer / reply.
    """
    reply = db.query(models.CourseDiscussionReply).filter(models.CourseDiscussionReply.id == reply_id).first()
    if not reply:
        raise HTTPException(status_code=404, detail="Reply not found")

    existing = db.query(models.CourseDiscussionUpvote).filter(
        models.CourseDiscussionUpvote.user_id == current_user.id,
        models.CourseDiscussionUpvote.reply_id == reply_id,
    ).first()

    if existing:
        db.delete(existing)
        reply.upvotes_count = max(0, (reply.upvotes_count or 1) - 1)
        upvoted = False
    else:
        new_upvote = models.CourseDiscussionUpvote(
            user_id=current_user.id,
            reply_id=reply_id,
        )
        db.add(new_upvote)
        reply.upvotes_count = (reply.upvotes_count or 0) + 1
        upvoted = True

    db.commit()
    return {"upvoted": upvoted, "upvotes_count": reply.upvotes_count}


@router.patch("/discussions/{discussion_id}/resolve")
def toggle_discussion_resolved(
    discussion_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """
    Mark a discussion as resolved/answered (author or instructor).
    """
    disc = db.query(models.CourseDiscussion).filter(models.CourseDiscussion.id == discussion_id).first()
    if not disc:
        raise HTTPException(status_code=404, detail="Discussion not found")

    # Only the author who posted the question can mark it as solved
    if disc.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the author who posted this question can mark it as solved")

    disc.is_resolved = not bool(disc.is_resolved)
    db.commit()
    return {"is_resolved": disc.is_resolved}


@router.patch("/discussions/replies/{reply_id}/endorse")
def toggle_reply_endorsed(
    reply_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """
    Instructor/Admin endorses a helpful reply as a verified answer.
    """
    user_role = str(current_user.role.value if hasattr(current_user.role, "value") else current_user.role).lower()
    if user_role not in ("teacher", "admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="Only instructors can endorse replies")

    reply = db.query(models.CourseDiscussionReply).filter(models.CourseDiscussionReply.id == reply_id).first()
    if not reply:
        raise HTTPException(status_code=404, detail="Reply not found")

    reply.is_endorsed = not bool(reply.is_endorsed)
    db.commit()
    return {"is_endorsed": reply.is_endorsed}
