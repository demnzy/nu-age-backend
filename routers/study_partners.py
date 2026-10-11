from fastapi import APIRouter, Depends, HTTPException, status, Query, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_, not_, func, desc
from datetime import datetime, date, timezone, timedelta
from typing import List, Dict, Any, Optional
import uuid
import json

import models
import schemas
from database import get_db
from services import auth
from services.notifications import dispatch_notification

router = APIRouter(prefix="/study/partnerships", tags=["Study Partnerships"])


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _get_partner_user(partnership: models.StudyPartnership, current_user_id: uuid.UUID) -> models.User:
    return partnership.user_b if partnership.user_a_id == current_user_id else partnership.user_a


def _format_user_summary(user: models.User, badge: Optional[str] = None) -> Dict[str, Any]:
    if not user:
        return {}
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
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. ACTIVE PARTNERSHIP & DASHBOARD COCKPIT
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/active")
def get_active_partnership(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Fetches the authenticated user's active study partnership, partner details,
    Duo streak, today's dual check-in progress, and active challenges.
    """
    today = date.today()
    partnership = db.query(models.StudyPartnership).filter(
        models.StudyPartnership.status == "active",
        or_(
            models.StudyPartnership.user_a_id == current_user.id,
            models.StudyPartnership.user_b_id == current_user.id
        )
    ).first()

    if not partnership:
        return {"has_partnership": False, "partnership": None}

    is_user_a = (partnership.user_a_id == current_user.id)
    partner = partnership.user_b if is_user_a else partnership.user_a

    # Reset daily minutes if date has rolled over
    user_studied_today = (partnership.user_a_last_study_date == today) if is_user_a else (partnership.user_b_last_study_date == today)
    partner_studied_today = (partnership.user_b_last_study_date == today) if is_user_a else (partnership.user_a_last_study_date == today)

    user_today_mins = partnership.user_a_today_minutes if (is_user_a and user_studied_today) else 0
    partner_today_mins = partnership.user_b_today_minutes if (not is_user_a and partner_studied_today) else 0

    user_today_qs = partnership.user_a_today_questions if (is_user_a and user_studied_today) else 0
    partner_today_qs = partnership.user_b_today_questions if (not is_user_a and partner_studied_today) else 0

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
        challenge_list.append({
            "id": str(ch.id),
            "title": ch.title,
            "subject": ch.subject,
            "challenge_type": ch.challenge_type,
            "total_questions": ch.total_questions,
            "status": ch.status,
            "is_challenger": is_my_challenge,
            "needs_my_response": (not is_my_challenge and ch.status == "challenger_done"),
            "challenger_score": ch.challenger_score,
            "challenged_score": ch.challenged_score,
            "winner_id": str(ch.winner_id) if ch.winner_id else None,
            "created_at": ch.created_at.isoformat() if ch.created_at else None,
        })

    return {
        "has_partnership": True,
        "partnership": {
            "id": str(partnership.id),
            "status": partnership.status,
            "goal_type": partnership.goal_type,
            "target_exam": partnership.target_exam,
            "target_subjects": partnership.target_subjects or [],
            "daily_target_minutes": partnership.daily_target_minutes,
            "daily_target_questions": partnership.daily_target_questions,
            "duo_streak": partnership.duo_streak,
            "best_duo_streak": partnership.best_duo_streak,
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
    target_exam: str = Query("JAMB UTME"),
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
def log_partnership_activity(
    partnership_id: uuid.UUID,
    minutes_spent: int = Query(..., ge=1),
    questions_completed: int = Query(..., ge=1),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Called upon completing a quiz, flashcard deck, or CBT simulator exam.
    Records daily activity and re-evaluates the Duo Streak.
    """
    today = date.today()
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

    is_user_a = (partnership.user_a_id == current_user.id)

    if is_user_a:
        if partnership.user_a_last_study_date != today:
            partnership.user_a_today_minutes = 0
            partnership.user_a_today_questions = 0
            partnership.user_a_last_study_date = today
        partnership.user_a_today_minutes += minutes_spent
        partnership.user_a_today_questions += questions_completed
    else:
        if partnership.user_b_last_study_date != today:
            partnership.user_b_today_minutes = 0
            partnership.user_b_today_questions = 0
            partnership.user_b_last_study_date = today
        partnership.user_b_today_minutes += minutes_spent
        partnership.user_b_today_questions += questions_completed

    # Check Duo Streak Condition (Both users completed study today)
    both_studied_today = (
        partnership.user_a_last_study_date == today and
        partnership.user_b_last_study_date == today and
        partnership.user_a_today_minutes >= min(10, partnership.daily_target_minutes) and
        partnership.user_b_today_minutes >= min(10, partnership.daily_target_minutes)
    )

    streak_incremented = False
    if both_studied_today and partnership.last_duo_streak_date != today:
        partnership.duo_streak += 1
        partnership.best_duo_streak = max(partnership.best_duo_streak, partnership.duo_streak)
        partnership.last_duo_streak_date = today
        streak_incremented = True

    db.commit()

    return {
        "status": "success",
        "duo_streak": partnership.duo_streak,
        "streak_incremented": streak_incremented,
        "both_completed_today": both_studied_today
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

    challenge = models.PartnerChallenge(
        partnership_id=partnership.id,
        challenger_id=current_user.id,
        challenged_id=partner.id,
        challenge_type=payload.get("challenge_type", "cbt_past_questions"),
        title=title,
        subject=payload.get("subject", "General"),
        question_snapshot=questions,
        total_questions=len(questions),
        duration_seconds=payload.get("duration_seconds", 600),
        status="challenger_done",
        challenger_score=c_score,
        challenger_time_seconds=c_time,
        challenger_breakdown=c_breakdown,
        challenger_completed_at=datetime.now(timezone.utc),
    )

    db.add(challenge)
    db.commit()

    # Notify partner of duel
    try:
        dispatch_notification(
            db=db,
            recipient_user_ids=[partner.id],
            title="Partner Challenge Received! ⚔️",
            body=f"{current_user.first_name} challenged you to a {len(questions)}-question duel in {challenge.subject}!",
            category="partner_challenge",
            action_route="/network?tab=partners",
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
    partner = ch.challenged if is_challenger else ch.challenger

    return {
        "id": str(ch.id),
        "title": ch.title,
        "subject": ch.subject,
        "challenge_type": ch.challenge_type,
        "status": ch.status,
        "total_questions": ch.total_questions,
        "duration_seconds": ch.duration_seconds,
        "question_snapshot": ch.question_snapshot,
        "is_challenger": is_challenger,
        "challenger": _format_user_summary(ch.challenger),
        "challenged": _format_user_summary(ch.challenged),
        "challenger_score": ch.challenger_score,
        "challenger_time_seconds": ch.challenger_time_seconds,
        "challenged_score": ch.challenged_score,
        "challenged_time_seconds": ch.challenged_time_seconds,
        "winner_id": str(ch.winner_id) if ch.winner_id else None,
        "coins_awarded": ch.coins_awarded,
    }


@router.post("/challenges/{challenge_id}/submit")
def submit_partner_challenge(
    challenge_id: uuid.UUID,
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Submits the challenged partner's score, resolves the winner, and awards victory coins.
    """
    ch = db.query(models.PartnerChallenge).filter(
        models.PartnerChallenge.id == challenge_id,
        models.PartnerChallenge.challenged_id == current_user.id,
        models.PartnerChallenge.status == "challenger_done"
    ).first()

    if not ch:
        raise HTTPException(status_code=404, detail="Challenge not found or already completed.")

    challenged_score = int(payload.get("score", 0))
    challenged_time = int(payload.get("time_seconds", 0))

    ch.challenged_score = challenged_score
    ch.challenged_time_seconds = challenged_time
    ch.challenged_breakdown = payload.get("breakdown", {})
    ch.challenged_completed_at = datetime.now(timezone.utc)
    ch.status = "completed"

    # Evaluate Winner (Score first, then speed tie-breaker)
    c_score = ch.challenger_score or 0
    c_time = ch.challenger_time_seconds or 999999

    if challenged_score > c_score:
        ch.winner_id = current_user.id
        ch.coins_awarded = 50
    elif c_score > challenged_score:
        ch.winner_id = ch.challenger_id
        ch.coins_awarded = 50
    else:
        # Tie breaker on time
        if challenged_time < c_time:
            ch.winner_id = current_user.id
            ch.coins_awarded = 50
        elif c_time < challenged_time:
            ch.winner_id = ch.challenger_id
            ch.coins_awarded = 50
        else:
            ch.winner_id = None
            ch.coins_awarded = 25  # Draw bonus

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
            action_route="/network?tab=partners",
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
    }
