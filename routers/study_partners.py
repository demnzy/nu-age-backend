from fastapi import APIRouter, Depends, HTTPException, status, Query, BackgroundTasks, Request
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_, not_, func, desc
from datetime import datetime, date, timezone, timedelta
from typing import List, Dict, Any, Optional
import uuid
import json

import models
import schemas
from database import get_db
from services import auth, coin_service, promo_service
from services.notifications import dispatch_notification

router = APIRouter(prefix="/study/partnerships", tags=["Study Partnerships"])


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _get_partner_user(partnership: models.StudyPartnership, current_user_id: uuid.UUID) -> models.User:
    return partnership.user_b if partnership.user_a_id == current_user_id else partnership.user_a


def _format_user_summary(user: models.User, badge: Optional[str] = None, db: Optional[Session] = None) -> Dict[str, Any]:
    if not user:
        return {}
    p_badge = None
    if db:
        p_badge = promo_service.resolve_user_primary_badge(db, user.id)
    return {
        "id": str(user.id),
        "first_name": user.first_name,
        "last_name": user.last_name,
        "username": user.username,
        "email": user.email,
        "university": getattr(user, "university", None),
        "profile_picture_url": getattr(user, "profile_picture_url", None),
        "streak": getattr(user, "streak", 0) or 0,
        "badge": badge,
        "primary_badge": p_badge,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. ACTIVE PARTNERSHIP & DASHBOARD COCKPIT
# ─────────────────────────────────────────────────────────────────────────────

def _record_partner_study_activity(
    db: Session,
    partnership: models.StudyPartnership,
    user_id: uuid.UUID,
    minutes: int,
    questions: int
) -> tuple[bool, list]:
    today = date.today()
    yesterday = today - timedelta(days=1)
    is_user_a = (partnership.user_a_id == user_id)

    if is_user_a:
        if partnership.user_a_last_study_date != today:
            partnership.user_a_today_minutes = 0
            partnership.user_a_today_questions = 0
            partnership.user_a_last_study_date = today
        partnership.user_a_today_minutes += minutes
        partnership.user_a_today_questions += questions
    else:
        if partnership.user_b_last_study_date != today:
            partnership.user_b_today_minutes = 0
            partnership.user_b_today_questions = 0
            partnership.user_b_last_study_date = today
        partnership.user_b_today_minutes += minutes
        partnership.user_b_today_questions += questions

    # Check streak decay (reset if a day was skipped)
    if partnership.last_duo_streak_date and partnership.last_duo_streak_date < yesterday:
        partnership.duo_streak = 0

    streak_target = min(10, partnership.daily_target_minutes or 10)
    both_studied_today = (
        partnership.user_a_last_study_date == today and
        partnership.user_b_last_study_date == today and
        partnership.user_a_today_minutes >= streak_target and
        partnership.user_b_today_minutes >= streak_target
    )

    streak_incremented = False
    milestones_awarded = []
    if both_studied_today and partnership.last_duo_streak_date != today:
        if partnership.last_duo_streak_date == yesterday:
            partnership.duo_streak += 1
        else:
            partnership.duo_streak = 1
        partnership.best_duo_streak = max(partnership.best_duo_streak or 0, partnership.duo_streak)
        partnership.last_duo_streak_date = today
        streak_incremented = True

        milestones_awarded = coin_service.check_and_award_duo_streak_milestones(db, partnership, partnership.duo_streak)
        coin_service.track_objective_progress(db, user_id, "streak_5_day", partnership.duo_streak, set_absolute=True)
        partner_user = _get_partner_user(partnership, user_id)
        if partner_user:
            coin_service.track_objective_progress(db, partner_user.id, "streak_5_day", partnership.duo_streak, set_absolute=True)

    coin_service.track_objective_progress(db, user_id, "study_time_60m", minutes)
    coin_service.track_objective_progress(db, user_id, "practice_100q", questions)

    return streak_incremented, milestones_awarded


def _format_active_partnership_payload(partnership, current_user, db: Session, today: date) -> dict:
    is_user_a = (partnership.user_a_id == current_user.id)
    partner = partnership.user_b if is_user_a else partnership.user_a

    # Reset daily minutes if date has rolled over
    user_studied_today = (partnership.user_a_last_study_date == today) if is_user_a else (partnership.user_b_last_study_date == today)
    partner_studied_today = (partnership.user_b_last_study_date == today) if is_user_a else (partnership.user_a_last_study_date == today)

    user_today_mins = (partnership.user_a_today_minutes if is_user_a else partnership.user_b_today_minutes) if user_studied_today else 0
    partner_today_mins = (partnership.user_b_today_minutes if is_user_a else partnership.user_a_today_minutes) if partner_studied_today else 0

    user_today_qs = (partnership.user_a_today_questions if is_user_a else partnership.user_b_today_questions) if user_studied_today else 0
    partner_today_qs = (partnership.user_b_today_questions if is_user_a else partnership.user_a_today_questions) if partner_studied_today else 0

    # Streak decay check (if missed yesterday and haven't secured today)
    yesterday = today - timedelta(days=1)
    if partnership.last_duo_streak_date and partnership.last_duo_streak_date < yesterday:
        if partnership.duo_streak != 0:
            partnership.duo_streak = 0
            db.commit()

    streak_target = min(10, partnership.daily_target_minutes or 10)
    user_streak_qualified = (user_today_mins >= streak_target)
    partner_streak_qualified = (partner_today_mins >= streak_target)
    duo_streak_locked_today = (partnership.last_duo_streak_date == today)

    # Streak at risk check (e.g. current user studied but partner hasn't, or vice-versa)
    can_nudge = True
    if partnership.last_nudge_at:
        elapsed = datetime.now(timezone.utc) - partnership.last_nudge_at
        if elapsed.total_seconds() < 10800:  # 3 hour cooldown
            can_nudge = False

    # Fetch pending challenges
    challenges = db.query(models.PartnerChallenge).filter(
        models.PartnerChallenge.partnership_id == partnership.id,
        models.PartnerChallenge.status.in_(["challenger_done", "completed"])
    ).order_by(models.PartnerChallenge.created_at.desc()).limit(5).all()

    challenge_list = []
    for ch in challenges:
        is_my_challenge = (ch.challenger_id == current_user.id)
        my_score = ch.challenger_score if is_my_challenge else ch.challenged_score
        partner_score = ch.challenged_score if is_my_challenge else ch.challenger_score
        my_time = ch.challenger_time_seconds if is_my_challenge else ch.challenged_time_seconds
        partner_time = ch.challenged_time_seconds if is_my_challenge else ch.challenger_time_seconds
        challenge_list.append({
            "id": str(ch.id),
            "title": ch.title,
            "subject": ch.subject,
            "pack_id": str(ch.pack_id) if ch.pack_id else None,
            "challenge_type": ch.challenge_type,
            "total_questions": ch.total_questions,
            "duration_seconds": ch.duration_seconds,
            "status": ch.status,
            "challenger_id": str(ch.challenger_id),
            "challenged_id": str(ch.challenged_id),
            "is_challenger": is_my_challenge,
            "needs_my_response": (not is_my_challenge and ch.status == "challenger_done"),
            "challenger_name": ch.challenger.first_name if ch.challenger else "Partner",
            "challenged_name": ch.challenged.first_name if ch.challenged else "Partner",
            "my_score": my_score,
            "partner_score": partner_score,
            "my_time_seconds": my_time,
            "partner_time_seconds": partner_time,
            "challenger_score": ch.challenger_score,
            "challenger_time_seconds": ch.challenger_time_seconds,
            "challenged_score": ch.challenged_score,
            "challenged_time_seconds": ch.challenged_time_seconds,
            "winner_id": str(ch.winner_id) if ch.winner_id else None,
            "coins_awarded": ch.coins_awarded,
            "wager_amount": getattr(ch, "wager_amount", 0) or 0,
            "question_snapshot": ch.question_snapshot or [],
            "created_at": ch.created_at.isoformat() if ch.created_at else None,
        })

    return {
        "id": str(partnership.id),
        "current_user_id": str(current_user.id),
        "status": partnership.status,
        "goal_type": partnership.goal_type,
        "target_exam": partnership.target_exam,
        "target_subjects": partnership.target_subjects or [],
        "daily_target_minutes": partnership.daily_target_minutes,
        "daily_target_questions": partnership.daily_target_questions,
        "duo_streak": partnership.duo_streak,
        "best_duo_streak": partnership.best_duo_streak,
        "streak_threshold_minutes": streak_target,
        "user_streak_qualified": user_streak_qualified,
        "partner_streak_qualified": partner_streak_qualified,
        "duo_streak_locked_today": duo_streak_locked_today,
        "user_studied_today": user_studied_today,
        "partner_studied_today": partner_studied_today,
        "user_today_minutes": user_today_mins,
        "partner_today_minutes": partner_today_mins,
        "user_today_questions": user_today_qs,
        "partner_today_questions": partner_today_qs,
        "can_nudge": can_nudge,
        "partner": _format_user_summary(partner),
        "recent_challenges": challenge_list,
    }


@router.get("/active")
def get_active_partnership(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Fetches the authenticated user's active study partnerships, partner details,
    Duo streak, today's dual check-in progress, and active challenges.
    Supports learners with multiple simultaneous active duo partnerships.
    """
    today = date.today()
    partnerships = db.query(models.StudyPartnership).filter(
        models.StudyPartnership.status == "active",
        or_(
            models.StudyPartnership.user_a_id == current_user.id,
            models.StudyPartnership.user_b_id == current_user.id
        )
    ).order_by(models.StudyPartnership.duo_streak.desc(), models.StudyPartnership.updated_at.desc()).all()

    if not partnerships:
        return {"has_partnership": False, "partnership": None, "partnerships": []}

    formatted_list = [
        _format_active_partnership_payload(p, current_user, db, today)
        for p in partnerships
    ]

    return {
        "has_partnership": True,
        "partnership": formatted_list[0],
        "partnerships": formatted_list,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. MATCHMAKING & PARTNER RECOMMENDATIONS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/recommendations")
def get_partner_recommendations(
    limit: int = 10,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Smart Study Partner Matchmaker:
    Scores candidate peers based on:
      1. Course Choice Similarity (enrolled courses overlap - calculated silently)
      2. University Match (same campus)
      3. Active Study Streak (proven study habit)
      4. Shared Organisations
    Excludes existing partners and pending partner requests.
    """
    # 1. Collect IDs to exclude (current user, existing active/pending partnerships)
    excluded_ids = {current_user.id}
    existing_partnerships = db.query(models.StudyPartnership).filter(
        models.StudyPartnership.status.in_(["pending", "active"]),
        or_(
            models.StudyPartnership.user_a_id == current_user.id,
            models.StudyPartnership.user_b_id == current_user.id
        )
    ).all()
    for p in existing_partnerships:
        excluded_ids.add(p.user_a_id)
        excluded_ids.add(p.user_b_id)

    # 2. Get current user's enrolled course IDs
    my_course_ids = set()
    try:
        enrollments = db.query(models.Enrollment.course_id).filter(
            models.Enrollment.user_id == current_user.id
        ).all()
        my_course_ids = {e[0] for e in enrollments if e[0]}
    except Exception:
        pass

    # 3. Get current user's organisation IDs
    my_org_ids = set()
    try:
        org_memberships = db.query(models.OrganisationMember.organisation_id).filter(
            models.OrganisationMember.user_id == current_user.id
        ).all()
        my_org_ids = {o[0] for o in org_memberships if o[0]}
    except Exception:
        pass

    # 4. Fetch candidate users (active learners not in excluded list)
    candidates = db.query(models.User).filter(
        models.User.id.notin_(excluded_ids)
    ).limit(80).all()

    scored_candidates = []
    my_uni = (current_user.university or "").strip().lower()

    my_dept = (getattr(current_user, "department", None) or "").strip().lower()

    for cand in candidates:
        score = 0
        cand_badges = []

        # Metric A: Course Choice Similarity (Calculated silently - never exposed to users)
        try:
            cand_courses = db.query(models.Enrollment.course_id).filter(
                models.Enrollment.user_id == cand.id
            ).all()
            cand_course_ids = {c[0] for c in cand_courses if c[0]}
            shared_courses_count = len(my_course_ids.intersection(cand_course_ids))
            if shared_courses_count > 0:
                score += shared_courses_count * 25
                cand_badges.append("Academic Match")
        except Exception:
            pass

        # Metric B: University Match
        cand_uni = (getattr(cand, "university", None) or "").strip().lower()
        if my_uni and cand_uni and my_uni == cand_uni:
            score += 30
            cand_badges.append("Same University")

        # Metric C: Department Match (if populated now or in future)
        cand_dept = (getattr(cand, "department", None) or "").strip().lower()
        if my_dept and cand_dept and my_dept == cand_dept:
            score += 25
            cand_badges.append("Same Department")

        # Metric D: Organisation Match
        try:
            if my_org_ids:
                cand_orgs = db.query(models.OrganisationMember.organisation_id).filter(
                    models.OrganisationMember.user_id == cand.id
                ).all()
                cand_org_ids = {o[0] for o in cand_orgs if o[0]}
                if my_org_ids.intersection(cand_org_ids):
                    score += 15
                    cand_badges.append("Campus Org")
        except Exception:
            pass

        # Metric E: Active Streak & Study Habit
        c_streak = getattr(cand, "streak", 0) or 0
        if c_streak > 0:
            score += min(c_streak * 2, 20)
            if c_streak >= 3:
                cand_badges.append("Active Streak")

        # Primary Badge Assignment (Contextual badge without exposing sensitive choices)
        primary_badge = "High Match" if score >= 45 else (cand_badges[0] if cand_badges else "Study Peer")

        scored_candidates.append({
            "candidate": cand,
            "score": score,
            "badge": primary_badge,
        })

    # Sort descending by compatibility score
    scored_candidates.sort(key=lambda x: x["score"], reverse=True)
    top_candidates = scored_candidates[:limit]

    return [_format_user_summary(item["candidate"], badge=item["badge"]) for item in top_candidates]


# ─────────────────────────────────────────────────────────────────────────────
# 3. PARTNERSHIP INVITATIONS (INBOX & SENT)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/requests/incoming")
def get_incoming_partnership_requests(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    requests = db.query(models.StudyPartnership).filter(
        models.StudyPartnership.user_b_id == current_user.id,
        models.StudyPartnership.status == "pending"
    ).all()

    return [{
        "id": str(req.id),
        "target_exam": req.target_exam,
        "target_subjects": req.target_subjects or [],
        "user": _format_user_summary(req.user_a),
        "partner": _format_user_summary(req.user_a),
        "created_at": req.created_at.isoformat() if req.created_at else None,
    } for req in requests]


@router.get("/requests/sent")
def get_sent_partnership_requests(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    requests = db.query(models.StudyPartnership).filter(
        models.StudyPartnership.user_a_id == current_user.id,
        models.StudyPartnership.status == "pending"
    ).all()

    return [{
        "id": str(req.id),
        "target_exam": req.target_exam,
        "target_subjects": req.target_subjects or [],
        "user": _format_user_summary(req.user_b),
        "partner": _format_user_summary(req.user_b),
        "created_at": req.created_at.isoformat() if req.created_at else None,
    } for req in requests]


@router.post("/invite/{target_user_id}")
def invite_study_partner(
    target_user_id: uuid.UUID,
    target_exam: str = Query("General CBT"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """Sends a Study Partnership invitation to a peer."""
    if target_user_id == current_user.id:
        raise HTTPException(status_code=400, detail="You cannot partner with yourself.")

    target_user = db.query(models.User).filter(models.User.id == target_user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="Target user not found.")

    # Check for existing partnership
    existing = db.query(models.StudyPartnership).filter(
        models.StudyPartnership.status.in_(["pending", "active"]),
        or_(
            and_(models.StudyPartnership.user_a_id == current_user.id, models.StudyPartnership.user_b_id == target_user_id),
            and_(models.StudyPartnership.user_a_id == target_user_id, models.StudyPartnership.user_b_id == current_user.id)
        )
    ).first()

    if existing:
        raise HTTPException(status_code=400, detail="A partnership or pending invitation already exists with this user.")

    new_partnership = models.StudyPartnership(
        user_a_id=current_user.id,
        user_b_id=target_user_id,
        status="pending",
        goal_type="cbt_exam",
        target_exam=target_exam,
        target_subjects=["Physics", "Chemistry", "Mathematics", "English"],
        daily_target_minutes=30,
        daily_target_questions=15,
    )
    db.add(new_partnership)
    db.commit()

    # Notify recipient via OneSignal
    try:
        dispatch_notification(
            db=db,
            recipient_user_ids=[target_user_id],
            title="Study Partner Invitation! 🤝",
            body=f"{current_user.first_name} invited you to become an Accountability Study Partner!",
            category="study_partner_invite",
            action_route="/network?tab=partners",
            sender_id=current_user.id,
            collapse_id=f"partner_invite_{target_user_id}",
        )
    except Exception as e:
        print(f"[Partnership] Notification error: {e}")

    return {"status": "success", "message": "Study partnership invitation sent.", "partnership_id": str(new_partnership.id)}


@router.post("/{partnership_id}/accept")
def accept_partnership(
    partnership_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    partnership = db.query(models.StudyPartnership).filter(
        models.StudyPartnership.id == partnership_id,
        models.StudyPartnership.user_b_id == current_user.id,
        models.StudyPartnership.status == "pending"
    ).first()

    if not partnership:
        raise HTTPException(status_code=404, detail="Pending partnership request not found.")

    partnership.status = "active"
    partnership.duo_streak = 0
    db.commit()

    # Notify requester
    try:
        dispatch_notification(
            db=db,
            recipient_user_ids=[partnership.user_a_id],
            title="Study Partnership Formed! 🔥",
            body=f"{current_user.first_name} accepted your study partnership. Start your Duo Streak today!",
            category="study_partner_accepted",
            action_route="/self-study",
            sender_id=current_user.id,
            collapse_id=f"partner_accepted_{partnership.id}",
        )
    except Exception as e:
        print(f"[Partnership] Notification error: {e}")

    return {"status": "success", "message": "Study partnership accepted!"}


@router.post("/{partnership_id}/decline")
def decline_partnership(
    partnership_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    partnership = db.query(models.StudyPartnership).filter(
        models.StudyPartnership.id == partnership_id,
        models.StudyPartnership.user_b_id == current_user.id,
        models.StudyPartnership.status == "pending"
    ).first()

    if not partnership:
        raise HTTPException(status_code=404, detail="Pending partnership request not found.")

    partnership.status = "dissolved"
    db.commit()
    return {"status": "success", "message": "Partnership invitation declined."}


@router.delete("/{partnership_id}")
def dissolve_partnership(
    partnership_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    partnership = db.query(models.StudyPartnership).filter(
        models.StudyPartnership.id == partnership_id,
        or_(
            models.StudyPartnership.user_a_id == current_user.id,
            models.StudyPartnership.user_b_id == current_user.id
        )
    ).first()

    if not partnership:
        raise HTTPException(status_code=404, detail="Partnership not found.")

    partnership.status = "dissolved"
    db.commit()
    return {"status": "success", "message": "Study partnership dissolved."}


# ─────────────────────────────────────────────────────────────────────────────
# 4. PARTNER NUDGES (ONESIGNAL PUSH DISPATCH)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/{partnership_id}/nudge")
def nudge_partner(
    partnership_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Sends an instant high-priority accountability nudge to partner via OneSignal.
    Rate limited to 1 nudge every 3 hours with grouped collapse keys.
    """
    partnership = db.query(models.StudyPartnership).filter(
        models.StudyPartnership.id == partnership_id,
        models.StudyPartnership.status == "active",
        or_(
            models.StudyPartnership.user_a_id == current_user.id,
            models.StudyPartnership.user_b_id == current_user.id
        )
    ).first()

    if not partnership:
        raise HTTPException(status_code=404, detail="Active partnership not found.")

    # 3-Hour cooldown check
    if partnership.last_nudge_at:
        elapsed = datetime.now(timezone.utc) - partnership.last_nudge_at
        if elapsed.total_seconds() < 10800:
            remaining_mins = max(1, int((10800 - elapsed.total_seconds()) / 60))
            raise HTTPException(status_code=429, detail=f"Partner already nudged recently. Try again in {remaining_mins} minutes.")

    partner = _get_partner_user(partnership, current_user.id)

    # Update nudge cooldown timestamp
    partnership.last_nudge_at = datetime.now(timezone.utc)
    partnership.last_nudge_by_id = current_user.id
    db.commit()

    # OneSignal push dispatch
    streak_val = partnership.duo_streak
    streak_text = f"Keep your {streak_val}-day Duo Streak alive! 🔥" if streak_val > 0 else "Start your Duo Streak today! 🚀"
    
    try:
        dispatch_notification(
            db=db,
            recipient_user_ids=[partner.id],
            title=f"{current_user.first_name} nudged you! ⏰",
            body=f"{streak_text} Log into Nu-age and complete your daily questions.",
            category="study_partner_nudge",
            action_route="/self-study",
            sender_id=current_user.id,
            collapse_id=f"partner_nudge_{partnership.id}",
        )
    except Exception as e:
        print(f"[Partnership Nudge] Push error: {e}")

    return {"status": "success", "message": f"{partner.first_name} has been nudged!"}


# ─────────────────────────────────────────────────────────────────────────────
# 5. LOG DAILY ACTIVITY & DUO STREAK RECONCILIATION
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/{partnership_id}/log-activity")
async def log_partnership_activity(
    partnership_id: uuid.UUID,
    request: Request,
    minutes_spent: int = Query(0, ge=0),
    questions_completed: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Called upon completing a quiz, flashcard deck, or CBT simulator exam.
    Records daily activity and re-evaluates the Duo Streak.
    Accepts data from URL query params or JSON request body.
    """
    partnership = db.query(models.StudyPartnership).filter(
        models.StudyPartnership.id == partnership_id,
        models.StudyPartnership.status == "active",
        or_(
            models.StudyPartnership.user_a_id == current_user.id,
            models.StudyPartnership.user_b_id == current_user.id
        )
    ).first()

    if not partnership:
        raise HTTPException(status_code=404, detail="Active partnership not found.")

    # Resilient payload parsing: extract from body if present, fallback to query params
    body_data = {}
    try:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            body_data = await request.json()
    except Exception:
        body_data = {}

    if not isinstance(body_data, dict):
        body_data = {}

    final_minutes = int(body_data.get("minutes_spent", body_data.get("minutes_studied", body_data.get("minutes", minutes_spent))) or 0)
    final_questions = int(body_data.get("questions_completed", body_data.get("questions_solved", body_data.get("questions", questions_completed))) or 0)

    streak_incremented, milestones_awarded = _record_partner_study_activity(
        db=db,
        partnership=partnership,
        user_id=current_user.id,
        minutes=final_minutes,
        questions=final_questions
    )

    db.commit()

    today = date.today()
    streak_target = min(10, partnership.daily_target_minutes or 10)
    both_studied_today = (
        partnership.user_a_last_study_date == today and
        partnership.user_b_last_study_date == today and
        partnership.user_a_today_minutes >= streak_target and
        partnership.user_b_today_minutes >= streak_target
    )

    return {
        "status": "success",
        "duo_streak": partnership.duo_streak,
        "streak_incremented": streak_incremented,
        "both_completed_today": both_studied_today,
        "milestones_awarded": milestones_awarded,
        "coins_balance": coin_service.get_user_coins(db, current_user.id),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 6. HEAD-TO-HEAD PARTNER DUELS & CHALLENGES
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/{partnership_id}/challenges/create")
def create_partner_challenge(
    partnership_id: uuid.UUID,
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Creates a head-to-head CBT / Quiz challenge with a seeded question snapshot.
    The challenger submits their performance, and the challenged partner takes the exact same paper.
    """
    partnership = db.query(models.StudyPartnership).filter(
        models.StudyPartnership.id == partnership_id,
        models.StudyPartnership.status == "active",
        or_(
            models.StudyPartnership.user_a_id == current_user.id,
            models.StudyPartnership.user_b_id == current_user.id
        )
    ).first()

    if not partnership:
        raise HTTPException(status_code=404, detail="Active partnership not found.")

    partner = _get_partner_user(partnership, current_user.id)

    title = payload.get("title") or f"{payload.get('subject', 'CBT')} Partner Duel"
    questions = payload.get("question_snapshot") or payload.get("questions") or []
    if not questions:
        raise HTTPException(status_code=400, detail="Challenge must contain at least 1 question snapshot.")

    c_score = payload.get("challenger_score", payload.get("score", 0))
    c_time = payload.get("challenger_time_seconds", payload.get("time_seconds", 0))
    c_breakdown = payload.get("challenger_breakdown", payload.get("breakdown", {}))
    p_id = payload.get("pack_id")
    pack_uuid = None
    if p_id:
        try:
            pack_uuid = uuid.UUID(str(p_id))
        except Exception:
            pack_uuid = None

    wager_amount = int(payload.get("wager_amount") or 0)
    if wager_amount < 0 or wager_amount not in (0, 10, 25, 50, 100):
        raise HTTPException(status_code=400, detail="Invalid wager amount. Allowed stakes: 0, 10, 25, 50, 100 Nu-Coins.")

    is_escrowed = False
    if wager_amount > 0:
        c_coins = coin_service.get_user_coins(db, current_user.id)
        if c_coins < wager_amount:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient Nu-Coins for stake. You have {c_coins} coins, but {wager_amount} coins are required."
            )
        ok, _, msg = coin_service.spend_coins(
            db,
            user_id=current_user.id,
            amount=wager_amount,
            category="duel_wager_escrow",
            description=f"Escrowed {wager_amount} Nu-Coins for Quickfire Duel against {partner.first_name}",
            source_type="challenge_escrow",
            reference_id=f"duel_wager_{current_user.id}",
        )
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
        is_escrowed = True

    challenge = models.PartnerChallenge(
        partnership_id=partnership.id,
        challenger_id=current_user.id,
        challenged_id=partner.id,
        pack_id=pack_uuid,
        challenge_type=payload.get("challenge_type", "quickfire_duel"),
        title=title,
        subject=payload.get("subject", "General"),
        question_snapshot=questions,
        total_questions=len(questions),
        duration_seconds=payload.get("duration_seconds", 180),
        status="challenger_done",
        challenger_score=c_score,
        challenger_time_seconds=c_time,
        challenger_breakdown=c_breakdown,
        challenger_completed_at=datetime.now(timezone.utc),
        wager_amount=wager_amount,
        is_escrowed=is_escrowed,
    )

    db.add(challenge)

    # Record study activity for challenger in this partnership
    c_mins = max(3, int(c_time or 0) // 60 or 5)
    _record_partner_study_activity(db, partnership, current_user.id, minutes=c_mins, questions=len(questions))

    db.commit()

    # Notify partner of duel
    try:
        wager_text = f" ⚔️ Stake: {wager_amount} Nu-Coins each!" if wager_amount > 0 else ""
        dispatch_notification(
            db=db,
            recipient_user_ids=[partner.id],
            title="Partner Challenge Received! ⚔️",
            body=f"{current_user.first_name} challenged you to a {len(questions)}-question Quickfire Duel in {challenge.subject}!{wager_text}",
            category="partner_challenge",
            action_route=f"/network?tab=partners&duel={challenge.id}",
            sender_id=current_user.id,
            collapse_id=f"partner_challenge_{challenge.id}",
        )
    except Exception as e:
        print(f"[Partner Challenge] Push error: {e}")

    return {
        "status": "success",
        "challenge_id": str(challenge.id),
        "message": f"Challenge created! Sent to {partner.first_name}."
    }


def _format_challenge_history_item(ch: models.PartnerChallenge, current_user: models.User) -> dict:
    is_my_challenge = (ch.challenger_id == current_user.id)
    partner = ch.challenged if is_my_challenge else ch.challenger
    my_score = ch.challenger_score if is_my_challenge else ch.challenged_score
    partner_score = ch.challenged_score if is_my_challenge else ch.challenger_score
    my_time = ch.challenger_time_seconds if is_my_challenge else ch.challenged_time_seconds
    partner_time = ch.challenged_time_seconds if is_my_challenge else ch.challenger_time_seconds

    outcome = "tie"
    if ch.status == "challenger_done":
        outcome = "pending" if is_my_challenge else "needs_action"
    elif ch.winner_id:
        outcome = "won" if ch.winner_id == current_user.id else "lost"

    return {
        "id": str(ch.id),
        "title": ch.title,
        "subject": ch.subject,
        "pack_id": str(ch.pack_id) if ch.pack_id else None,
        "challenge_type": ch.challenge_type,
        "total_questions": ch.total_questions,
        "duration_seconds": ch.duration_seconds,
        "status": ch.status,
        "is_challenger": is_my_challenge,
        "challenger_id": str(ch.challenger_id),
        "challenged_id": str(ch.challenged_id),
        "partner_name": partner.first_name if partner else "Partner",
        "challenger_name": ch.challenger.first_name if ch.challenger else "Challenger",
        "challenged_name": ch.challenged.first_name if ch.challenged else "Partner",
        "my_score": my_score,
        "partner_score": partner_score,
        "challenger_score": ch.challenger_score,
        "challenged_score": ch.challenged_score,
        "my_time_seconds": my_time,
        "partner_time_seconds": partner_time,
        "challenger_time_seconds": ch.challenger_time_seconds,
        "challenged_time_seconds": ch.challenged_time_seconds,
        "outcome": outcome,
        "winner_id": str(ch.winner_id) if ch.winner_id else None,
        "coins_awarded": ch.coins_awarded or 0,
        "wager_amount": getattr(ch, "wager_amount", 0) or 0,
        "completed_at": (ch.challenged_completed_at or ch.created_at).isoformat() if (ch.challenged_completed_at or ch.created_at) else None,
    }


@router.get("/challenges/history")
def get_all_challenges_history(
    limit: int = Query(20, ge=1, le=50),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Returns the complete chronological history of 15-Q Duels for the user across any partnership,
    including active pending duels, completed duels, score breakdowns, and coins awarded.
    """
    challenges = db.query(models.PartnerChallenge).filter(
        or_(
            models.PartnerChallenge.challenger_id == current_user.id,
            models.PartnerChallenge.challenged_id == current_user.id
        ),
        models.PartnerChallenge.status.in_(["completed", "expired", "challenger_done"])
    ).order_by(models.PartnerChallenge.created_at.desc()).limit(limit).all()

    return {"challenges": [_format_challenge_history_item(ch, current_user) for ch in challenges]}


@router.get("/challenges/{challenge_id}")
def get_partner_challenge(
    challenge_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    ch = db.query(models.PartnerChallenge).filter(
        models.PartnerChallenge.id == challenge_id,
        or_(
            models.PartnerChallenge.challenger_id == current_user.id,
            models.PartnerChallenge.challenged_id == current_user.id
        )
    ).first()

    if not ch:
        raise HTTPException(status_code=404, detail="Challenge not found.")

    is_challenger = (ch.challenger_id == current_user.id)
    my_score = ch.challenger_score if is_challenger else ch.challenged_score
    partner_score = ch.challenged_score if is_challenger else ch.challenger_score
    my_time = ch.challenger_time_seconds if is_challenger else ch.challenged_time_seconds
    partner_time = ch.challenged_time_seconds if is_challenger else ch.challenger_time_seconds

    return {
        "id": str(ch.id),
        "title": ch.title,
        "subject": ch.subject,
        "pack_id": str(ch.pack_id) if ch.pack_id else None,
        "challenge_type": ch.challenge_type,
        "status": ch.status,
        "total_questions": ch.total_questions,
        "duration_seconds": ch.duration_seconds,
        "question_snapshot": ch.question_snapshot,
        "challenger_id": str(ch.challenger_id),
        "challenged_id": str(ch.challenged_id),
        "challenger_name": ch.challenger.first_name if ch.challenger else "Challenger",
        "challenged_name": ch.challenged.first_name if ch.challenged else "Partner",
        "is_challenger": is_challenger,
        "needs_my_response": (not is_challenger and ch.status == "challenger_done"),
        "my_score": my_score,
        "partner_score": partner_score,
        "my_time_seconds": my_time,
        "partner_time_seconds": partner_time,
        "challenger": _format_user_summary(ch.challenger),
        "challenged": _format_user_summary(ch.challenged),
        "challenger_score": ch.challenger_score,
        "challenger_time_seconds": ch.challenger_time_seconds,
        "challenger_breakdown": ch.challenger_breakdown,
        "challenged_score": ch.challenged_score,
        "challenged_time_seconds": ch.challenged_time_seconds,
        "challenged_breakdown": ch.challenged_breakdown,
        "winner_id": str(ch.winner_id) if ch.winner_id else None,
        "coins_awarded": ch.coins_awarded or 0,
        "wager_amount": getattr(ch, "wager_amount", 0) or 0,
    }


@router.get("/{partnership_id}/challenges/history")
def get_challenge_history(
    partnership_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=50),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Returns the complete chronological history of 15-Q Duels for the duo,
    with full score breakdown, winner badges, and coins awarded.
    """
    partnership = db.query(models.StudyPartnership).filter(
        models.StudyPartnership.id == partnership_id,
        or_(
            models.StudyPartnership.user_a_id == current_user.id,
            models.StudyPartnership.user_b_id == current_user.id
        )
    ).first()

    if not partnership:
        raise HTTPException(status_code=404, detail="Partnership not found.")

    challenges = db.query(models.PartnerChallenge).filter(
        models.PartnerChallenge.partnership_id == partnership.id,
        models.PartnerChallenge.status.in_(["completed", "expired", "challenger_done"])
    ).order_by(models.PartnerChallenge.created_at.desc()).limit(limit).all()

    return {"challenges": [_format_challenge_history_item(ch, current_user) for ch in challenges]}


@router.post("/challenges/{challenge_id}/submit")
def submit_partner_challenge(
    challenge_id: uuid.UUID,
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Submits the challenged partner's score, resolves the winner, and awards victory coins to both.
    """
    ch = db.query(models.PartnerChallenge).filter(
        models.PartnerChallenge.id == challenge_id,
        models.PartnerChallenge.challenged_id == current_user.id,
        models.PartnerChallenge.status == "challenger_done"
    ).first()

    if not ch:
        raise HTTPException(status_code=404, detail="Challenge not found or already completed.")

    challenged_score = int(payload.get("challenged_score", payload.get("score", 0)))
    challenged_time = int(payload.get("challenged_time_seconds", payload.get("time_seconds", 0)))

    ch.challenged_score = challenged_score
    ch.challenged_time_seconds = challenged_time
    ch.challenged_breakdown = payload.get("challenged_breakdown", payload.get("breakdown", {}))
    ch.challenged_completed_at = datetime.now(timezone.utc)
    ch.status = "completed"

    # If this was a wagered duel, match stake from challenged partner
    wager = getattr(ch, "wager_amount", 0) or 0
    if wager > 0:
        d_coins = coin_service.get_user_coins(db, current_user.id)
        if d_coins < wager:
            raise HTTPException(
                status_code=400,
                detail=f"Matching stake of {wager} Nu-Coins required to complete this wagered duel. You have {d_coins} coins."
            )
        ok, _, msg = coin_service.spend_coins(
            db,
            user_id=current_user.id,
            amount=wager,
            category="duel_wager_match",
            description=f"Matched {wager} Nu-Coins stake for duel against {ch.challenger.first_name}",
            source_type="challenge_match",
            reference_id=str(ch.id),
        )
        if not ok:
            raise HTTPException(status_code=400, detail=msg)

    # Evaluate Winner (Score first, then speed tie-breaker)
    c_score = ch.challenger_score or 0
    c_time = ch.challenger_time_seconds or 999999

    if challenged_score > c_score:
        winner_id = current_user.id
    elif c_score > challenged_score:
        winner_id = ch.challenger_id
    else:
        # Tie breaker on time
        if challenged_time < c_time:
            winner_id = current_user.id
        elif c_time < challenged_time:
            winner_id = ch.challenger_id
        else:
            winner_id = None

    ch.winner_id = winner_id

    # Settle Wagered Pot vs Casual Free Duel
    if wager > 0:
        total_pot = wager * 2
        platform_burn = int(total_pot * 0.10)
        winner_payout = total_pot - platform_burn

        if winner_id:
            ch.coins_awarded = winner_payout
            winner_user = current_user if winner_id == current_user.id else ch.challenger
            loser_user = ch.challenger if winner_id == current_user.id else current_user
            coin_service.grant_coins(
                db=db,
                user_id=winner_id,
                amount=winner_payout,
                category="duel_wager_win",
                description=f"Won {wager}-coin wagered duel against {loser_user.first_name}! (Pot: {winner_payout} coins)",
                source_type="challenge",
                reference_id=str(ch.id),
            )
        else:
            # Exact tie: refund both players 100% of their stake
            ch.coins_awarded = wager
            coin_service.grant_coins(
                db=db,
                user_id=current_user.id,
                amount=wager,
                category="duel_wager_refund",
                description=f"Tied duel with {ch.challenger.first_name}: refunded {wager} Nu-Coins stake",
                source_type="challenge",
                reference_id=str(ch.id),
            )
            coin_service.grant_coins(
                db=db,
                user_id=ch.challenger_id,
                amount=wager,
                category="duel_wager_refund",
                description=f"Tied duel with {current_user.first_name}: refunded {wager} Nu-Coins stake",
                source_type="challenge",
                reference_id=str(ch.id),
            )
    else:
        # Casual duel: Exactly 0 coins awarded to either player
        ch.coins_awarded = 0

    if winner_id:
        coin_service.record_metric_event(db, winner_id, "win_duel", 1)

    # Record study activity for challenged partner on this partnership
    p = ch.partnership or db.query(models.StudyPartnership).filter(models.StudyPartnership.id == ch.partnership_id).first()
    if p:
        mins = max(3, challenged_time // 60 or 5)
        _record_partner_study_activity(db, p, current_user.id, minutes=mins, questions=ch.total_questions or 15)

    db.commit()

    # Notify challenger that challenge is completed
    try:
        winner_name = current_user.first_name if ch.winner_id == current_user.id else ch.challenger.first_name
        winner_text = f"Winner: {winner_name}!" if ch.winner_id else "It's a draw!"
        dispatch_notification(
            db=db,
            recipient_user_ids=[ch.challenger_id],
            title="Duel Results Ready! 🏆",
            body=f"{current_user.first_name} finished the duel! {ch.challenger_score} vs {challenged_score}. {winner_text}",
            category="partner_challenge_completed",
            action_route=f"/network?tab=partners&duel={ch.id}",
            sender_id=current_user.id,
            collapse_id=f"duel_completed_{ch.id}",
        )
    except Exception as e:
        print(f"[Challenge Submit] Notification error: {e}")

    return {
        "status": "success",
        "message": "Challenge completed!",
        "winner_id": str(ch.winner_id) if ch.winner_id else None,
        "challenger_score": ch.challenger_score,
        "challenged_score": challenged_score,
        "coins_awarded": ch.coins_awarded,
        "coins_balance": coin_service.get_user_coins(db, current_user.id),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 7. STUDY PACK SHARING BETWEEN DUO PARTNERS
def _resolve_study_pack_or_material(db: Session, item_id: uuid.UUID, user_id: uuid.UUID) -> Optional[models.StudyPack]:
    """
    Resolves either a marketplace StudyPack or a personal vault StudyMaterial.
    If a StudyMaterial is shared, automatically synthesizes a StudyPack snapshot
    containing its flashcards and quiz questions for seamless duo sharing.
    """
    # 1. Direct StudyPack match
    pack = db.query(models.StudyPack).filter_by(id=item_id).first()
    if pack:
        return pack

    # 2. Check if item_id matches a PartnershipSharedPack
    share = db.query(models.PartnershipSharedPack).filter_by(id=item_id).first()
    if share and share.pack:
        return share.pack

    # 3. Check if item_id matches a personal StudyMaterial in the vault
    mat = db.query(models.StudyMaterial).filter_by(id=item_id).first()
    if not mat:
        # Check if item_id is an existing source_material_id
        return db.query(models.StudyPack).filter_by(source_material_id=item_id).first()

    # Find existing pack created from this material
    existing = db.query(models.StudyPack).filter_by(source_material_id=mat.id).first()
    if existing:
        return existing

    # Synthesize a StudyPack snapshot from the StudyMaterial
    fc_list = [{"front": fc.front, "back": fc.back} for fc in (mat.flashcards or [])]
    q_list = []
    for q in (mat.questions or []):
        opts = q.options if isinstance(q.options, list) else []
        ans_idx = getattr(q, "answer_index", 0) or 0
        correct_ans = opts[ans_idx] if (0 <= ans_idx < len(opts)) else (opts[0] if opts else "Option A")
        q_text = getattr(q, "question_text", None) or getattr(q, "question", "")
        q_list.append({
            "question": q_text,
            "options": opts,
            "answer_index": ans_idx,
            "correct_answer": correct_ans,
            "explanation": getattr(q, "explanation", "") or "",
        })

    pack_data = {
        "title": mat.title,
        "source_type": mat.source_type,
        "flashcards": fc_list,
        "questions": q_list,
    }

    new_pack = models.StudyPack(
        creator_id=mat.user_id,
        title=mat.title,
        description=f"Shared study material: {mat.title}",
        category="Duo Study",
        theme_gradient="green_emerald",
        price_coins=0,
        status="partner_only",
        is_official=False,
        pack_data=pack_data,
        source_material_id=mat.id,
    )
    db.add(new_pack)
    db.flush()
    return new_pack


@router.post("/{partnership_id}/share-pack")
def share_study_pack(
    partnership_id: uuid.UUID,
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Shares an acquired Study Pack with the accountability study partner.
    """
    partnership = db.query(models.StudyPartnership).filter(
        models.StudyPartnership.id == partnership_id,
        models.StudyPartnership.status == "active",
        or_(
            models.StudyPartnership.user_a_id == current_user.id,
            models.StudyPartnership.user_b_id == current_user.id
        )
    ).first()

    if not partnership:
        raise HTTPException(status_code=404, detail="Active study partnership not found.")

    pack_id_str = payload.get("pack_id")
    if not pack_id_str:
        raise HTTPException(status_code=400, detail="Missing pack_id in payload.")

    try:
        pack_id = uuid.UUID(str(pack_id_str))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid pack_id format.")

    pack = _resolve_study_pack_or_material(db, pack_id, current_user.id)
    if not pack:
        raise HTTPException(status_code=404, detail="Study Pack not found.")

    partner = _get_partner_user(partnership, current_user.id)

    # Check if already shared
    existing = db.query(models.PartnershipSharedPack).filter_by(
        partnership_id=partnership.id,
        pack_id=pack.id
    ).first()

    if existing:
        if existing.status == "accepted":
            return {"status": "success", "message": f"{partner.first_name} has already accepted this pack."}
        existing.status = "pending"
        existing.sender_id = current_user.id
        existing.recipient_id = partner.id
        existing.created_at = datetime.now(timezone.utc)
    else:
        shared_record = models.PartnershipSharedPack(
            partnership_id=partnership.id,
            pack_id=pack.id,
            sender_id=current_user.id,
            recipient_id=partner.id,
            status="pending"
        )
        db.add(shared_record)

    # Track share objective for sender
    coin_service.record_metric_event(db, current_user.id, "share_pack", 1)
    db.commit()

    # Dispatch rich push notification
    try:
        dispatch_notification(
            db=db,
            recipient_user_ids=[partner.id],
            title="Shared Study Pack! 📚",
            body=f"{current_user.first_name} shared '{pack.title}'! This pack is now unlocked for 15-Q Duels. Preview flashcards and start a duel.",
            category="study_pack_shared",
            action_route="/self-study?tab=duo",
            sender_id=current_user.id,
            data_payload={"pack_id": str(pack.id)},
            collapse_id=f"pack_shared_{pack.id}_{partner.id}",
        )
    except Exception as ex:
        print(f"[Share Pack] Push error: {ex}")

    return {
        "status": "success",
        "message": f"Successfully shared '{pack.title}' with {partner.first_name}!",
        "pack_id": str(pack.id)
    }


# ─────────────────────────────────────────────────────────────────────────────
# 7.1. SHARED STUDY PACKS FLASHCARD-ONLY PREVIEW
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{partnership_id}/shared-packs/{pack_id}/preview")
def preview_shared_study_pack(
    partnership_id: uuid.UUID,
    pack_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Returns a flashcard-only preview for a shared study pack.
    Quiz questions are strictly withheld during preview to preserve competition integrity!
    """
    partnership = db.query(models.StudyPartnership).filter(
        models.StudyPartnership.id == partnership_id,
        or_(
            models.StudyPartnership.user_a_id == current_user.id,
            models.StudyPartnership.user_b_id == current_user.id
        )
    ).first()

    if not partnership:
        raise HTTPException(status_code=404, detail="Partnership not found.")

    pack = _resolve_study_pack_or_material(db, pack_id, current_user.id)
    if not pack:
        raise HTTPException(status_code=404, detail="Study Pack not found.")

    p_data = pack.pack_data or {}
    flashcards = list(p_data.get("flashcards", []))

    return {
        "pack_id": str(pack.id),
        "title": pack.title,
        "description": pack.description,
        "category": pack.category,
        "total_flashcards": len(flashcards),
        "flashcards": flashcards,
    }


@router.get("/{partnership_id}/shared-packs")
def get_shared_packs(
    partnership_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Lists all study packs shared between the two partners.
    """
    partnership = db.query(models.StudyPartnership).filter(
        models.StudyPartnership.id == partnership_id,
        or_(
            models.StudyPartnership.user_a_id == current_user.id,
            models.StudyPartnership.user_b_id == current_user.id
        )
    ).first()

    if not partnership:
        raise HTTPException(status_code=404, detail="Partnership not found.")

    shares = db.query(models.PartnershipSharedPack).filter_by(
        partnership_id=partnership.id
    ).order_by(models.PartnershipSharedPack.created_at.desc()).all()

    results = []
    for s in shares:
        p = s.pack
        if not p:
            continue
        p_data = p.pack_data or {}
        q_count = len(p_data.get("questions", []))
        fc_count = len(p_data.get("flashcards", []))

        results.append({
            "id": str(s.id),
            "pack_id": str(p.id),
            "title": p.title,
            "category": p.category,
            "theme_gradient": p.theme_gradient,
            "questions_count": q_count,
            "flashcards_count": fc_count,
            "flashcards": list(p_data.get("flashcards", [])),
            "sender_id": str(s.sender_id),
            "sender_name": s.sender.first_name if s.sender else "Partner",
            "is_sent_by_me": (s.sender_id == current_user.id),
            "status": s.status,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "accepted_at": s.accepted_at.isoformat() if s.accepted_at else None,
        })

    return {"status": "success", "shared_packs": results}


@router.post("/{partnership_id}/shared-packs/{shared_id}/accept")
def accept_shared_pack(
    partnership_id: uuid.UUID,
    shared_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Accepts a shared study pack, grants +20 coins to the sender, and unlocks it.
    """
    share = db.query(models.PartnershipSharedPack).filter_by(
        id=shared_id,
        partnership_id=partnership_id,
        recipient_id=current_user.id
    ).first()

    if not share:
        raise HTTPException(status_code=404, detail="Shared pack request not found.")

    share.status = "accepted"
    share.accepted_at = datetime.now(timezone.utc)

    # Clone pack into recipient's study vault as StudyMaterial if not already present
    pack = share.pack
    if pack:
        existing_mat = db.query(models.StudyMaterial).filter_by(
            user_id=current_user.id,
            title=pack.title
        ).first()
        if not existing_mat:
            new_mat = models.StudyMaterial(
                user_id=current_user.id,
                title=pack.title,
                source_type="pack_import",
                content=f"Imported from duo partner: {pack.title}",
            )
            db.add(new_mat)
            db.flush()
            p_data = pack.pack_data or {}
            for fc in p_data.get("flashcards", []):
                db.add(models.Flashcard(
                    user_id=current_user.id,
                    material_id=new_mat.id,
                    front=fc.get("front", ""),
                    back=fc.get("back", ""),
                ))
            for q in p_data.get("questions", []):
                db.add(models.Question(
                    user_id=current_user.id,
                    material_id=new_mat.id,
                    question_text=q.get("question") or q.get("question_text", ""),
                    options=q.get("options", []),
                    answer_index=q.get("answer_index", 0),
                    explanation=q.get("explanation", ""),
                ))
            db.flush()

    db.commit()

    # Notify sender
    try:
        dispatch_notification(
            db=db,
            recipient_user_ids=[share.sender_id],
            title="Study Pack Accepted! 🌟",
            body=f"{current_user.first_name} accepted your study pack! You earned +20 Nu-Coins.",
            category="study_pack_accepted",
            action_route="/self-study?tab=duo",
            sender_id=current_user.id,
            collapse_id=f"pack_accepted_{share.id}",
        )
    except Exception as ex:
        print(f"[Accept Pack] Push notice: {ex}")

    return {"status": "success", "message": "Study pack accepted and added to your collection!"}


# ─────────────────────────────────────────────────────────────────────────────
# 8. SEEDED 15-QUESTION QUICKFIRE DUEL GENERATOR FROM SHARED PACK
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/{partnership_id}/challenges/quickfire-from-pack")
def generate_quickfire_from_pack(
    partnership_id: uuid.UUID,
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Extracts exactly 15 questions from the chosen Study Pack to seed a fair,
    asynchronous Quickfire Duel.
    """
    partnership = db.query(models.StudyPartnership).filter(
        models.StudyPartnership.id == partnership_id,
        models.StudyPartnership.status == "active",
        or_(
            models.StudyPartnership.user_a_id == current_user.id,
            models.StudyPartnership.user_b_id == current_user.id
        )
    ).first()

    if not partnership:
        raise HTTPException(status_code=404, detail="Active partnership not found.")

    pack_id_str = payload.get("pack_id")
    if not pack_id_str:
        raise HTTPException(status_code=400, detail="Missing pack_id.")

    try:
        pack_id = uuid.UUID(str(pack_id_str))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid pack_id.")

    pack = _resolve_study_pack_or_material(db, pack_id, current_user.id)
    if not pack:
        raise HTTPException(status_code=404, detail="Study Pack not found.")

    p_data = pack.pack_data or {}
    raw_questions = list(p_data.get("questions", []))
    flashcards = list(p_data.get("flashcards", []))

    # Compile exactly 15 questions
    import random
    quiz_pool = []
    for q in raw_questions:
        q_text = q.get("question") or q.get("question_text") or q.get("text", "")
        options = q.get("options") or ["True", "False"]
        correct = q.get("correct_answer") or q.get("answer") or options[0]
        expl = q.get("explanation") or ""
        if q_text:
            quiz_pool.append({
                "id": str(q.get("id") or uuid.uuid4()),
                "question": q_text,
                "options": options,
                "correct_answer": correct,
                "explanation": expl,
            })

    # If questions pool has fewer than 15, synthesize from flashcards
    if len(quiz_pool) < 15 and flashcards:
        for fc in flashcards:
            term = fc.get("term") or fc.get("front") or ""
            definition = fc.get("definition") or fc.get("back") or ""
            if term and definition:
                distractors = [f.get("definition") or f.get("back") for f in flashcards if f != fc and (f.get("definition") or f.get("back"))][:3]
                while len(distractors) < 3:
                    distractors.append("None of the above")
                all_opts = [definition] + distractors
                random.shuffle(all_opts)
                quiz_pool.append({
                    "id": str(uuid.uuid4()),
                    "question": f"What is the definition of '{term}'?",
                    "options": all_opts,
                    "correct_answer": definition,
                    "explanation": f"'{term}' means: {definition}",
                })
            if len(quiz_pool) >= 15:
                break

    if not quiz_pool:
        # Fallback question set
        for i in range(15):
            quiz_pool.append({
                "id": str(uuid.uuid4()),
                "question": f"Question {i+1}: What is the primary concept in {pack.title}?",
                "options": ["Fundamental Principles", "Practical Application", "Core Methodology", "Analysis & Synthesis"],
                "correct_answer": "Fundamental Principles",
                "explanation": "Core review topic.",
            })

    # Pick up to 15 questions
    seeded_15 = quiz_pool[:15]
    if len(seeded_15) < 15:
        # Loop to fill exactly 15
        while len(seeded_15) < 15:
            dup = dict(seeded_15[len(seeded_15) % len(quiz_pool)])
            dup["id"] = str(uuid.uuid4())
            seeded_15.append(dup)

    partner = _get_partner_user(partnership, current_user.id)

    return {
        "status": "success",
        "pack_id": str(pack.id),
        "pack_title": pack.title,
        "subject": pack.category,
        "total_questions": 15,
        "duration_seconds": 180,
        "questions": seeded_15,
        "partner": _format_user_summary(partner),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 9. REAL-TIME STUDY LEADERBOARD
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/leaderboard")
def get_study_leaderboard(
    filter_type: str = Query("today", alias="filter"),
    limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Real-time study rankings:
    - 'today': Ranked by active study minutes logged today in Self-Study Hub.
    - 'weekly': Ranked by active study streak and total questions.
    - 'duo_streaks': Top study partnerships ranked by active Duo Streak count.
    """
    today = date.today()

    try:
        from services.promo_service import evaluate_rank_badges
        evaluate_rank_badges(db)
    except Exception:
        pass

    if filter_type == "duo_streaks":
        duos = db.query(models.StudyPartnership).filter(
            models.StudyPartnership.status == "active"
        ).order_by(
            models.StudyPartnership.duo_streak.desc(),
            models.StudyPartnership.best_duo_streak.desc(),
            models.StudyPartnership.created_at.desc()
        ).limit(limit).all()

        rankings = []
        yesterday = today - timedelta(days=1)
        for idx, d in enumerate(duos, start=1):
            effective_streak = d.duo_streak or 0
            if d.last_duo_streak_date and d.last_duo_streak_date < yesterday:
                effective_streak = 0
            clean_target = d.target_exam
            if clean_target and any(term in str(clean_target).upper() for term in ["JAMB", "UTME", "GENERAL"]):
                clean_target = None

            rankings.append({
                "rank": idx,
                "partnership_id": str(d.id),
                "duo_streak": effective_streak,
                "best_duo_streak": d.best_duo_streak or 0,
                "target_exam": clean_target,
                "partner_a": _format_user_summary(d.user_a, db=db),
                "partner_b": _format_user_summary(d.user_b, db=db),
                "is_my_duo": (d.user_a_id == current_user.id or d.user_b_id == current_user.id),
            })
        return {"status": "success", "filter": "duo_streaks", "leaderboard": rankings}

    if filter_type == "today":
        # Rank by actual minutes studied today
        active_partnerships = db.query(models.StudyPartnership).filter(
            models.StudyPartnership.status == "active",
            or_(
                models.StudyPartnership.user_a_last_study_date == today,
                models.StudyPartnership.user_b_last_study_date == today,
            )
        ).all()
        user_mins_map = {}
        for p in active_partnerships:
            if p.user_a_last_study_date == today and p.user_a_today_minutes > 0:
                user_mins_map[p.user_a_id] = user_mins_map.get(p.user_a_id, 0) + p.user_a_today_minutes
            if p.user_b_last_study_date == today and p.user_b_today_minutes > 0:
                user_mins_map[p.user_b_id] = user_mins_map.get(p.user_b_id, 0) + p.user_b_today_minutes

        if user_mins_map:
            sorted_uids = sorted(user_mins_map.keys(), key=lambda uid: user_mins_map[uid], reverse=True)[:limit]
            users_by_id = {u.id: u for u in db.query(models.User).filter(models.User.id.in_(sorted_uids)).all()}
            rankings = []
            for idx, uid in enumerate(sorted_uids, start=1):
                u = users_by_id.get(uid)
                if not u:
                    continue
                rankings.append({
                    "rank": idx,
                    "user_id": str(u.id),
                    "username": u.username,
                    "first_name": u.first_name,
                    "last_name": u.last_name,
                    "university": getattr(u, "university", None),
                    "profile_picture_url": getattr(u, "profile_picture_url", None),
                    "study_minutes": user_mins_map[uid],
                    "streak": getattr(u, "streak", 0) or 0,
                    "coins_balance": getattr(u, "coins_balance", 0) or 0,
                    "primary_badge": promo_service.resolve_user_primary_badge(db, u.id),
                    "is_me": (u.id == current_user.id),
                })
            return {"status": "success", "filter": "today", "leaderboard": rankings}
        else:
            return {"status": "success", "filter": "today", "leaderboard": []}

    # Solo study streak rankings (filter_type == "weekly" or default)
    users_q = db.query(models.User).order_by(models.User.streak.desc(), models.User.coins_balance.desc()).limit(limit).all()
    rankings = []
    for idx, u in enumerate(users_q, start=1):
        rankings.append({
            "rank": idx,
            "user_id": str(u.id),
            "username": u.username,
            "first_name": u.first_name,
            "last_name": u.last_name,
            "university": getattr(u, "university", None),
            "profile_picture_url": getattr(u, "profile_picture_url", None),
            "study_minutes": 0,
            "streak": getattr(u, "streak", 0) or 0,
            "coins_balance": getattr(u, "coins_balance", 0) or 0,
            "primary_badge": promo_service.resolve_user_primary_badge(db, u.id),
            "is_me": (u.id == current_user.id),
        })

    return {"status": "success", "filter": filter_type, "leaderboard": rankings}
