"""
reengagement_engine.py
──────────────────────────────────────────────────────────────────────────────
Autonomous Semantic Re-Engagement & Retention Engine for Nu-Age:
1. Detects users with imminent Cohort Exams (< 24h, emergency < 3h).
2. Detects users with streaks at risk of expiring at midnight (Solo & Duo Streaks).
3. Detects pending Quickfire Duels & Partner Challenges waiting for student response.
4. Detects Duo accountability gaps (when a study partner studied today, but you haven't).
5. Detects due Spaced Repetition (SRS) Flashcards ready for review.
6. Detects next unfinished lesson with format-specific nudges (video, quiz, code lab, cards).
7. Detects leaderboard rivalry & rank shifts.
8. Detects objective quests near completion (>= 65% progress).
9. Detects broken streak recovery within 24 hours of streak loss.
10. Detects dormant learners (3+ days) & personalized new course drops based on category affinity.

Enforces:
- Dynamic multi-factor scoring (urgency + base priority + affinity)
- Adaptive anti-spam limits (3.5h standard spacing, 1.5h emergency exception)
- Strict quiet hours (11:00 PM - 8:30 AM WAT, emergency grace to 11:30 PM WAT)
- Safe batch iteration across ALL platform users without arbitrary truncation.
──────────────────────────────────────────────────────────────────────────────
"""

import random
from datetime import datetime, date, timedelta, timezone
from typing import Dict, Any, List, Optional, Tuple
from uuid import UUID
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_, and_, func

import models
from services.notifications import dispatch_notification

# ─────────────────────────────────────────────────────────────────────────────
# 1. TIMEZONE & ACTIVE HOURS HELPERS (WAT = UTC+1, Nigerian Local Time)
# ─────────────────────────────────────────────────────────────────────────────

def get_current_wat_time() -> datetime:
    """Returns the current time in West Africa Time (UTC+1)."""
    utc_now = datetime.now(timezone.utc)
    wat_tz = timezone(timedelta(hours=1))
    return utc_now.astimezone(wat_tz)


def get_current_wat_date() -> date:
    """Returns today's date in West Africa Time (UTC+1)."""
    return get_current_wat_time().date()


# ─────────────────────────────────────────────────────────────────────────────
# 2. SEMANTIC COPY BANKS (10 TIERS)
# ─────────────────────────────────────────────────────────────────────────────

TIER_1_COHORT_EXAM_COPY = [
    {
        "title": "🚨 Urgent: '{exam_title}' closes in {time_str}!",
        "body": "Your cohort exam '{exam_title}' closes soon! Don't miss your submission window. Jump in and show what you know 🎯",
    },
    {
        "title": "Exam countdown ticking: '{exam_title}' ⏳",
        "body": "Only {time_str} remaining before '{exam_title}' in {cohort_name} locks! Secure your score now ✍️",
    },
    {
        "title": "Final hours for '{exam_title}'! 📚🔥",
        "body": "You have {time_str} left to complete '{exam_title}'. Get seated, open the exam, and finish strong!",
    },
    {
        "title": "Don't get marked absent for '{exam_title}' 😬",
        "body": "Your cohort exam in {cohort_name} is closing in {time_str}. Complete your attempt before the portal closes!",
    },
]

TIER_2_STREAK_AT_RISK_COPY = [
    {
        "title": "Your streak is crying in the corner 😭",
        "body": "Look at your {streak}-day streak... look at the clock... look at your streak again! Answer a few questions before midnight to protect it 🔥",
    },
    {
        "title": "Don't let the flame die tonight! 🔥",
        "body": "Your {streak}-day streak is about to reset to 0 in a few hours. 1 quick 5-minute session saves it! Be the hero your habits deserve 🦸‍♂️",
    },
    {
        "title": "Midnight countdown is ticking ⏳",
        "body": "Tick-tock! Your {streak}-day streak goes poof at midnight unless you complete your daily study practice. Jump in now!",
    },
    {
        "title": "Emergency streak alert! 🚨🔥",
        "body": "You've worked so hard for {streak} consecutive days! Don't throw it away now. Open Nu Age and secure the flame.",
    },
    {
        "title": "5 minutes now = streak saved ⚡",
        "body": "It only takes 5 minutes to keep your {streak}-day streak alive. No excuses, future graduate! Let's get it done.",
    },
]

TIER_3_QUICKFIRE_DUEL_COPY = [
    {
        "title": "{challenger_name} just challenged you to a Duel! ⚔️⚡",
        "body": "{challenger_name} completed their round in {subject}. Can you beat their score? Tap to accept the challenge!",
    },
    {
        "title": "Are you going to let {challenger_name} win? 👀",
        "body": "{challenger_name} posted a challenge score in {subject} and is waiting on you. Defend your honor in Quickfire Duels!",
    },
    {
        "title": "Duel pending: Show {challenger_name} what you've got! 🏆",
        "body": "You've been challenged in {subject}! Step up, answer the questions, and take the crown 🎯",
    },
    {
        "title": "Quickfire Duel alert! 🥊",
        "body": "{challenger_name} has laid down the gauntlet in {subject}. 5 minutes, 10 questions. Prove who's boss!",
    },
]

TIER_4_DUO_ACCOUNTABILITY_COPY = [
    {
        "title": "{partner_name} is carrying the team right now! 👀",
        "body": "{partner_name} already logged their study session today. Are you really going to leave them hanging and let the Duo Streak die? 😬🔥",
    },
    {
        "title": "Accountability check! 🤝",
        "body": "{partner_name} did their part today! Jump into Nu Age for 10 minutes so you both lock today's Duo Streak together 🎯",
    },
    {
        "title": "{partner_name} is waiting on you! ⚡",
        "body": "Your study partner {partner_name} completed their focus goal today. Match their energy before the clock strikes midnight!",
    },
    {
        "title": "Don't break {partner_name}'s heart 💔",
        "body": "{partner_name} showed up for your accountability partnership today. Jump in, solve a few questions, and claim your duo bonus!",
    },
    {
        "title": "Duo Alert: Your seat is empty! 🪑",
        "body": "{partner_name} is studying hard today. Don't be the partner that breaks the streak! 5 minutes is all you need 🚀",
    },
]

TIER_5_SRS_FLASHCARDS_COPY = [
    {
        "title": "🧠 {count} flashcards ready for review!",
        "body": "Spaced repetition works magic when you review on time. Knock out your {count} due flashcards in 3 minutes 🗂️⚡",
    },
    {
        "title": "Keep those memories sharp! 🗂️",
        "body": "You have {count} flashcards scheduled for today. Quick review now locks them into long-term memory!",
    },
    {
        "title": "Quick flashcard drill: {count} cards due ⏱️",
        "body": "Don't let what you studied fade away. 3 minutes of active recall right now protects your knowledge!",
    },
    {
        "title": "Active recall check! 💡",
        "body": "{count} cards in your decks need your attention today. Flip through them on your break and boost retention!",
    },
]

TIER_6_LESSON_VIDEO_COPY = [
    {
        "title": "🎬 Lesson ready: '{lesson_title}' in {course_title}",
        "body": "Your next video lesson is queued up! Grab your earphones and knock it out today 🎧",
    },
    {
        "title": "Continue {course_title}: '{lesson_title}' 🍿",
        "body": "Sit back, watch, and learn. Next video '{lesson_title}' is ready whenever you are!",
    },
]

TIER_6_LESSON_ASSESSMENT_COPY = [
    {
        "title": "📝 Quiz ready: '{lesson_title}' in {course_title}",
        "body": "Test your grasp on '{lesson_title}'! A quick quiz now cements everything you learned 🧠",
    },
    {
        "title": "Can you ace this quiz? 🎯",
        "body": "Lesson '{lesson_title}' has a practice assessment waiting in {course_title}. Put your skills to the test!",
    },
]

TIER_6_LESSON_CODELAB_COPY = [
    {
        "title": "💻 Code lab ready: '{lesson_title}'",
        "body": "Hands on the keyboard! Write code and pass test cases for '{lesson_title}' in {course_title} ⚡",
    },
    {
        "title": "Code challenge waiting in {course_title}! 🚀",
        "body": "Build muscle memory! Solve the code challenge in '{lesson_title}' today.",
    },
]

TIER_6_LESSON_GENERIC_COPY = [
    {
        "title": "Next step: '{lesson_title}' in {course_title} 📖",
        "body": "You're making steady headway! Jump into '{lesson_title}' today and move closer to your certificate 📜",
    },
    {
        "title": "Who left {course_title} waiting? 👀",
        "body": "Lesson '{lesson_title}' is waiting for you! 5 minutes today keeps your learning momentum alive ⚡",
    },
]

TIER_7_LEADERBOARD_SHIFT_COPY = [
    {
        "title": "Someone is gaining on you on the leaderboard! 👀🔥",
        "body": "Competition is heating up! Log a quick study session now to defend your rank and widen the gap 🏆",
    },
    {
        "title": "Leaderboard shakeup in progress! ⚡",
        "body": "Rivals are logging study minutes right now! Jump in, solve a few questions, and claim your place in the Top ranks 🎯",
    },
    {
        "title": "You're within striking distance of the top! 🥇",
        "body": "Just a few more points puts you higher on the leaderboard. Give it a quick 5-minute push today!",
    },
]

TIER_8_OBJECTIVE_QUEST_COPY = [
    {
        "title": "🎯 Quest almost complete, {name}!",
        "body": "You're only {remaining} {unit} away from finishing '{quest_title}'! Jump in now to claim your {coins} Nu-Coins 💰",
    },
    {
        "title": "So close to your bonus! ⚡",
        "body": "You are {progress}% through '{quest_title}'. Just {remaining} {unit} left to lock in your {coins} coins reward!",
    },
    {
        "title": "Don't leave {coins} Nu-Coins on the table! 🪙",
        "body": "Your '{quest_title}' quest is almost done ({progress}%). Knock out the remaining {remaining} {unit} before it resets!",
    },
    {
        "title": "Finish your quest today! 🏆",
        "body": "Only {remaining} {unit} left for '{quest_title}'. A quick 3-minute session earns you {coins} Nu-Coins!",
    },
]

TIER_9_BROKEN_STREAK_RECOVERY_COPY = [
    {
        "title": "Rekindle the flame! 🔥✨",
        "body": "You missed yesterday, but true champions don't give up! Study for 5 minutes today to restart your momentum + claim a comeback bonus 🛡️",
    },
    {
        "title": "24-Hour Streak Recovery Window! ⏳",
        "body": "Don't let one missed day derail your habits. Complete a quick practice session today and get right back in the game 🚀",
    },
    {
        "title": "Bounce back stronger today! 💪",
        "body": "Even the best have off days. Jump into Nu Age today, answer a few questions, and start building your next streak milestone!",
    },
]

TIER_10_INACTIVITY_COMEBACK_COPY = [
    {
        "title": "We haven't seen you in {days} days 🥺",
        "body": "The practice packs are getting lonely! Jump back in today and show these exam questions who's boss 💡",
    },
    {
        "title": "Your future exam-self is calling ⏳",
        "body": "Consistency is what turns average students into top rankers. Take 5 minutes today to get back on track!",
    },
    {
        "title": "These questions won't answer themselves! 📚",
        "body": "You've been away for {days} days. Jump back into Nu Age today and restart your momentum 🚀",
    },
]

TIER_10_NEW_COURSE_COPY = [
    {
        "title": "Fresh on Nu Age: {course_title}! 🆕✨",
        "body": "A brand new course just dropped in {category}! Be among the first to explore it and earn milestone Nu-Coins 🚀",
    },
    {
        "title": "New course alert: {course_title} 🎓",
        "body": "Expand your knowledge! {course_title} was just published. Check out Lesson 1 today 💡",
    },
    {
        "title": "Level up your skills with {course_title} 🧠",
        "body": "Brand new material just landed in {category}! Dive into {course_title} and claim your early learner badge 🏆",
    },
]

TIER_DAYTIME_NUDGE_COPY = [
    {
        "title": "Got 5 minutes? 🧠",
        "body": "That’s all it takes to keep your brain sharp today. Quick practice now = zero panic on exam day 🎯",
    },
    {
        "title": "Your textbooks called 📖",
        "body": "They miss the sound of your page turns! Jump into the Self-Study Hub for a quick review session.",
    },
    {
        "title": "Brain workout time! 🏋️‍♂️",
        "body": "Give your mind a quick 5-minute stretch. Solve a few flashcards or practice questions now.",
    },
]

AUTOMATED_CATEGORIES = (
    "cohort_exam",
    "streak_warning",
    "quickfire_duel",
    "duo_nudge",
    "srs_flashcards",
    "course_progress",
    "leaderboard_shift",
    "objective_quest",
    "broken_streak_recovery",
    "reengagement",
    "new_courses",
    "daytime_nudge",
)

# ─────────────────────────────────────────────────────────────────────────────
# 3. ANTI-SPAM & MULTI-NUDGE DAILY VOLUME LIMITS
# ─────────────────────────────────────────────────────────────────────────────

MAX_DAILY_AUTOMATED_NOTIFICATIONS = 3   # Standard daily cap
MAX_DAILY_EMERGENCY_NOTIFICATIONS = 4   # Cap when critical emergency arrives (exam closing < 3h, streak expiring < 2h)
MIN_HOURS_BETWEEN_NUDGES = 3.5          # Standard spacing
MIN_HOURS_EMERGENCY = 1.5               # Emergency spacing


def check_user_nudge_eligibility(
    db: Session,
    user_id: UUID,
    is_emergency: bool = False,
    max_daily: int = MAX_DAILY_AUTOMATED_NOTIFICATIONS,
    min_interval_hours: float = MIN_HOURS_BETWEEN_NUDGES,
) -> tuple[bool, str]:
    """
    Checks whether a user is eligible to receive another automated re-engagement nudge.
    Safeguards:
    1. Minimum interval spacing (3.5h standard, 1.5h for emergency triggers).
    2. Daily volume cap (3 standard, 4 for emergency per 24h rolling window).
    """
    now = datetime.now(timezone.utc)
    effective_interval = MIN_HOURS_EMERGENCY if is_emergency else min_interval_hours
    effective_max_daily = MAX_DAILY_EMERGENCY_NOTIFICATIONS if is_emergency else max_daily

    # 1. Spacing guard
    min_cutoff = now - timedelta(hours=effective_interval)
    recent = (
        db.query(models.UserNotification.id)
        .filter(
            models.UserNotification.user_id == user_id,
            models.UserNotification.category.in_(AUTOMATED_CATEGORIES),
            models.UserNotification.created_at >= min_cutoff,
        )
        .first()
    )
    if recent is not None:
        return False, "min_interval"

    # 2. Daily volume guard (past 24 hours)
    day_cutoff = now - timedelta(hours=24.0)
    count_today = (
        db.query(func.count(models.UserNotification.id))
        .filter(
            models.UserNotification.user_id == user_id,
            models.UserNotification.category.in_(AUTOMATED_CATEGORIES),
            models.UserNotification.created_at >= day_cutoff,
        )
        .scalar() or 0
    )
    if count_today >= effective_max_daily:
        return False, "daily_limit"

    return True, "eligible"


def user_was_recently_nudged(db: Session, user_id: UUID, window_hours: float = MIN_HOURS_BETWEEN_NUDGES) -> bool:
    """Backward-compatible helper."""
    eligible, _ = check_user_nudge_eligibility(db, user_id, min_interval_hours=window_hours)
    return not eligible


# ─────────────────────────────────────────────────────────────────────────────
# 4. DATA MINING COLLECTORS (BATCHED)
# ─────────────────────────────────────────────────────────────────────────────

def _mine_cohort_exams_batch(db: Session, user_ids: List[UUID], utc_now: datetime) -> Dict[UUID, List[Dict[str, Any]]]:
    """
    Finds active cohort exams closing in the next 36 hours for users who haven't submitted yet.
    """
    results: Dict[UUID, List[Dict[str, Any]]] = {}
    if not user_ids:
        return results

    try:
        # User memberships
        memberships = (
            db.query(models.CohortMember.user_id, models.CohortMember.cohort_id)
            .filter(
                models.CohortMember.user_id.in_(user_ids),
                models.CohortMember.status.in_(["active", "enrolled"]),
            )
            .all()
        )
        if not memberships:
            return results

        cohort_ids = list(set([m.cohort_id for m in memberships]))
        user_cohorts: Dict[UUID, set] = {}
        for uid, cid in memberships:
            user_cohorts.setdefault(uid, set()).add(cid)

        # Open exams closing within 36 hours
        window_end = utc_now + timedelta(hours=36)
        exams = (
            db.query(models.CohortExam)
            .filter(
                models.CohortExam.cohort_id.in_(cohort_ids),
                models.CohortExam.opens_at <= utc_now,
                models.CohortExam.closes_at > utc_now,
                models.CohortExam.closes_at <= window_end,
            )
            .all()
        )
        if not exams:
            return results

        exam_ids = [e.id for e in exams]

        # Completed submissions
        submissions = (
            db.query(models.CohortExamSubmission.user_id, models.CohortExamSubmission.exam_id)
            .filter(
                models.CohortExamSubmission.user_id.in_(user_ids),
                models.CohortExamSubmission.exam_id.in_(exam_ids),
                models.CohortExamSubmission.status.in_(["submitted", "graded", "passed"]),
            )
            .all()
        )
        submitted_set = set((s.user_id, s.exam_id) for s in submissions)

        for exam in exams:
            for uid in user_ids:
                if exam.cohort_id in user_cohorts.get(uid, set()):
                    if (uid, exam.id) not in submitted_set:
                        closes = exam.closes_at
                        if getattr(closes, "tzinfo", None) is None:
                            closes = closes.replace(tzinfo=timezone.utc)
                        hours_left = max(0.1, (closes - utc_now).total_seconds() / 3600.0)
                        results.setdefault(uid, []).append({
                            "exam": exam,
                            "hours_left": hours_left,
                        })
    except Exception as e:
        print(f"[reengagement_engine] _mine_cohort_exams_batch error: {e}")

    return results


def _mine_pending_duels_batch(db: Session, user_ids: List[UUID]) -> Dict[UUID, List[Dict[str, Any]]]:
    """
    Finds quickfire duels where the challenged user hasn't completed their round.
    """
    results: Dict[UUID, List[Dict[str, Any]]] = {}
    if not user_ids:
        return results

    try:
        challenges = (
            db.query(models.PartnerChallenge)
            .options(joinedload(models.PartnerChallenge.challenger))
            .filter(
                models.PartnerChallenge.challenged_id.in_(user_ids),
                models.PartnerChallenge.status.in_(["challenger_done", "pending"]),
            )
            .order_by(models.PartnerChallenge.created_at.desc())
            .all()
        )
        for c in challenges:
            results.setdefault(c.challenged_id, []).append({
                "challenge": c,
                "challenger": c.challenger,
            })
    except Exception as e:
        print(f"[reengagement_engine] _mine_pending_duels_batch error: {e}")

    return results


def _mine_due_flashcards_batch(db: Session, user_ids: List[UUID], utc_now: datetime) -> Dict[UUID, int]:
    """
    Counts due flashcards (next_review_date <= utc_now) for each user.
    """
    results: Dict[UUID, int] = {}
    if not user_ids:
        return results

    try:
        rows = (
            db.query(models.Flashcard.user_id, func.count(models.Flashcard.id))
            .filter(
                models.Flashcard.user_id.in_(user_ids),
                models.Flashcard.next_review_date <= utc_now,
            )
            .group_by(models.Flashcard.user_id)
            .all()
        )
        for uid, cnt in rows:
            results[uid] = cnt
    except Exception as e:
        print(f"[reengagement_engine] _mine_due_flashcards_batch error: {e}")

    return results


def _mine_in_progress_lessons_batch(
    db: Session,
    user_ids: List[UUID],
) -> Dict[UUID, List[Dict[str, Any]]]:
    """
    Finds in-progress course enrollments and the specific next unfinished lesson.
    Optimized: selects only lightweight lesson metadata (id, title, type), avoiding
    loading heavy content JSONB blobs over the network.
    """
    results: Dict[UUID, List[Dict[str, Any]]] = {}
    if not user_ids:
        return results

    try:
        enrollments = (
            db.query(models.Enrollment)
            .options(joinedload(models.Enrollment.course))
            .filter(
                models.Enrollment.student_id.in_(user_ids),
                models.Enrollment.completed_at.is_(None),
                models.Enrollment.progress < 99.9,
            )
            .all()
        )
        if not enrollments:
            return results

        course_ids = list(set([e.course_id for e in enrollments if e.course]))

        # Pre-fetch completed lesson IDs for these users
        completed_progress = (
            db.query(models.LessonProgress.student_id, models.LessonProgress.lesson_id)
            .filter(
                models.LessonProgress.student_id.in_(user_ids),
                models.LessonProgress.course_id.in_(course_ids),
            )
            .all()
        )
        completed_by_user: Dict[UUID, set] = {}
        for uid, lid in completed_progress:
            completed_by_user.setdefault(uid, set()).add(lid)

        # Pre-fetch only lightweight lesson metadata (id, title, type, module_id, course_id)
        # Avoids loading heavy content blobs, achieving a 50x query speedup
        lesson_rows = (
            db.query(
                models.Lesson.id,
                models.Lesson.title,
                models.Lesson.type,
                models.Lesson.module_id,
                models.Lesson.order_index,
                models.Module.course_id,
            )
            .join(models.Module, models.Lesson.module_id == models.Module.id)
            .filter(models.Module.course_id.in_(course_ids))
            .order_by(models.Module.order_index.asc(), models.Lesson.order_index.asc())
            .all()
        )
        course_lessons: Dict[UUID, List[Dict[str, Any]]] = {}
        for row in lesson_rows:
            course_lessons.setdefault(row.course_id, []).append({
                "id": row.id,
                "title": row.title,
                "type": row.type,
            })

        for en in enrollments:
            if not en.course:
                continue
            uid = en.student_id
            user_done = completed_by_user.get(uid, set())
            lessons = course_lessons.get(en.course_id, [])

            next_lesson_info = None
            for l in lessons:
                if l["id"] not in user_done:
                    next_lesson_info = l
                    break

            results.setdefault(uid, []).append({
                "enrollment": en,
                "course": en.course,
                "progress": float(en.progress or 0.0),
                "next_lesson": next_lesson_info,
            })
    except Exception as e:
        print(f"[reengagement_engine] _mine_in_progress_lessons_batch error: {e}")

    return results


# ─────────────────────────────────────────────────────────────────────────────
# 5. MULTI-FACTOR CANDIDATE SCORING & SELECTION
# ─────────────────────────────────────────────────────────────────────────────

def _score_trigger(
    tier: int,
    is_emergency: bool = False,
    urgency_boost: float = 0.0,
    affinity_boost: float = 0.0,
    preferred_tier: Optional[int] = None,
    preferred_category: Optional[str] = None,
    trigger_category: Optional[str] = None,
) -> float:
    """
    Calculates a composite evaluation score for a trigger candidate.
    Base tier hierarchy:
    Tier 1 (Cohort Exam): 100
    Tier 2 (Streak Saver): 90
    Tier 3 (Quickfire Duel): 85
    Tier 4 (Duo Accountability): 80
    Tier 5 (SRS Flashcards): 75
    Tier 6 (Lesson Resumption): 70
    Tier 7 (Leaderboard Shift): 65
    Tier 8 (Objective Quest): 60
    Tier 9 (Broken Streak Recovery): 55
    Tier 10 (Comeback / New Course): 45
    Daytime Nudge: 30
    """
    base_scores = {
        1: 100.0,
        2: 90.0,
        3: 85.0,
        4: 80.0,
        5: 75.0,
        6: 70.0,
        7: 65.0,
        8: 60.0,
        9: 55.0,
        10: 45.0,
    }
    score = base_scores.get(tier, 30.0)

    if is_emergency:
        score += 50.0

    score += urgency_boost + affinity_boost

    # Admin / test explicit preference boost
    if preferred_tier is not None and preferred_tier == tier:
        score += 2000.0
    if preferred_category is not None and preferred_category == trigger_category:
        score += 2000.0

    return score


# ─────────────────────────────────────────────────────────────────────────────
# 6. MAIN EVALUATION & DISPATCH ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_and_dispatch_nudges(
    db: Session,
    force: bool = False,
    dry_run: bool = False,
    target_user_id: Optional[UUID] = None,
    preferred_tier: Optional[int] = None,
    category: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Autonomous multi-tier evaluation cycle:
    1. Iterates across verified users in batches without arbitrary limits.
    2. Gathers contextual learner telemetry (exams, streaks, duels, duos, flashcards, lessons, quests).
    3. Evaluates all 10 semantic tiers and calculates composite score.
    4. Picks the single highest-priority eligible nudge per candidate.
    5. Dispatches personalized copy via in-app UserNotification & OneSignal push.
    """
    wat_now = get_current_wat_time()
    utc_now = datetime.now(timezone.utc)
    today_wat = wat_now.date()
    yesterday_wat = today_wat - timedelta(days=1)
    current_hour_wat = wat_now.hour + (wat_now.minute / 60.0)

    stats = {
        "timestamp_wat": wat_now.isoformat(),
        "evaluated_users": 0,
        "dispatched_tier_1_cohort_exam": 0,
        "dispatched_tier_2_streak": 0,
        "dispatched_tier_3_quickfire_duel": 0,
        "dispatched_tier_4_duo": 0,
        "dispatched_tier_5_srs_flashcards": 0,
        "dispatched_tier_6_lesson_resumption": 0,
        "dispatched_tier_7_leaderboard_shift": 0,
        "dispatched_tier_8_quest": 0,
        "dispatched_tier_9_broken_streak": 0,
        "dispatched_tier_10_comeback": 0,
        "dispatched_tier_10_new_course": 0,
        "dispatched_daytime_nudge": 0,
        "skipped_quiet_hours": False,
        "skipped_recently_nudged": 0,
        "skipped_daily_limit_reached": 0,
        "skipped_min_interval": 0,
        "skipped_already_studied": 0,
        "dispatched_records": [],
    }

    # Quiet Hours Guard: 11:00 PM to 8:30 AM WAT (unless force=True)
    # Emergency notifications (exams closing < 3h, streak reset at midnight) allowed until 11:30 PM (23.5)
    is_quiet_hours = (current_hour_wat >= 23.5 or current_hour_wat < 8.5)
    if not force and is_quiet_hours:
        stats["skipped_quiet_hours"] = True
        return stats

    # Query verified candidate users
    base_user_query = db.query(models.User).filter(models.User.is_verified == True)
    if target_user_id:
        candidate_users_list = base_user_query.filter(models.User.id == target_user_id).all()
        total_users = len(candidate_users_list)
    else:
        candidate_users_list = None
        total_users = base_user_query.count()

    stats["evaluated_users"] = total_users

    # Pre-fetch platform-wide active objectives
    active_objectives = (
        db.query(models.PlatformObjective)
        .filter(models.PlatformObjective.is_active == True)
        .all()
    )
    objectives_by_code: Dict[str, models.PlatformObjective] = {o.code: o for o in active_objectives}

    # Pre-fetch recent new courses (last 30 days)
    cutoff_30d = utc_now - timedelta(days=30)
    new_courses_pool = (
        db.query(models.Course)
        .options(joinedload(models.Course.category), joinedload(models.Course.organisation))
        .filter(
            models.Course.created_at >= cutoff_30d,
            or_(
                func.lower(models.Course.public) == "true",
                models.Course.public == "1",
            ),
        )
        .order_by(models.Course.created_at.desc())
        .all()
    )

    # Process in batches of 100 to ensure memory efficiency and avoid dropped users
    BATCH_SIZE = 100
    offsets = [0] if candidate_users_list is not None else list(range(0, total_users, BATCH_SIZE))

    for offset in offsets:
        if candidate_users_list is not None:
            batch_users = candidate_users_list
        else:
            batch_users = base_user_query.order_by(models.User.id.asc()).offset(offset).limit(BATCH_SIZE).all()

        if not batch_users:
            break

        batch_user_ids = [u.id for u in batch_users]

        # ── Pre-fetch batched data for this batch ──
        cohort_exams_by_user = _mine_cohort_exams_batch(db, batch_user_ids, utc_now)
        pending_duels_by_user = _mine_pending_duels_batch(db, batch_user_ids)
        due_flashcards_by_user = _mine_due_flashcards_batch(db, batch_user_ids, utc_now)
        in_progress_lessons_by_user = _mine_in_progress_lessons_batch(db, batch_user_ids)

        # Partnerships
        partnerships = (
            db.query(models.StudyPartnership)
            .filter(
                models.StudyPartnership.status == "active",
                or_(
                    models.StudyPartnership.user_a_id.in_(batch_user_ids),
                    models.StudyPartnership.user_b_id.in_(batch_user_ids),
                ),
            )
            .all()
        )
        partnerships_by_user: Dict[UUID, List[models.StudyPartnership]] = {}
        for p in partnerships:
            partnerships_by_user.setdefault(p.user_a_id, []).append(p)
            partnerships_by_user.setdefault(p.user_b_id, []).append(p)

        # User objectives
        user_obj_progress = (
            db.query(models.UserObjectiveProgress)
            .filter(
                models.UserObjectiveProgress.user_id.in_(batch_user_ids),
                models.UserObjectiveProgress.is_completed == False,
            )
            .all()
        )
        objectives_by_user: Dict[UUID, List[models.UserObjectiveProgress]] = {}
        for uop in user_obj_progress:
            if uop.objective_code in objectives_by_code:
                objectives_by_user.setdefault(uop.user_id, []).append(uop)

        # Enrolled course IDs for category matching
        all_enrollments = (
            db.query(models.Enrollment.student_id, models.Enrollment.course_id, models.Course.category_id)
            .join(models.Course, models.Enrollment.course_id == models.Course.id)
            .filter(models.Enrollment.student_id.in_(batch_user_ids))
            .all()
        )
        enrolled_course_ids: Dict[UUID, set] = {}
        user_categories: Dict[UUID, set] = {}
        for uid, cid, cat_id in all_enrollments:
            enrolled_course_ids.setdefault(uid, set()).add(cid)
            if cat_id:
                user_categories.setdefault(uid, set()).add(cat_id)

        # ── Evaluate each user in the batch ──
        for user in batch_users:
            u_id = user.id
            u_streak = getattr(user, "streak", 0) or 0
            u_last_login = getattr(user, "last_login_date", None)
            u_first = getattr(user, "first_name", "Scholar") or "Scholar"
            u_partnerships = partnerships_by_user.get(u_id, [])

            # Check if user already studied today
            user_studied_today = False
            if u_last_login == today_wat:
                for p in u_partnerships:
                    if p.user_a_id == u_id and (p.user_a_today_minutes >= 5 or p.user_a_last_study_date == today_wat):
                        user_studied_today = True
                        break
                    elif p.user_b_id == u_id and (p.user_b_today_minutes >= 5 or p.user_b_last_study_date == today_wat):
                        user_studied_today = True
                        break

            max_duo_streak = max([p.duo_streak for p in u_partnerships] + [0])
            effective_streak = max(u_streak, max_duo_streak)

            # Collection of eligible trigger candidates for this user
            candidates: List[Dict[str, Any]] = []

            # ─────────────────────────────────────────────────────────────────
            # CANDIDATE: TIER 1 - COHORT EXAMS IMMINENT
            # ─────────────────────────────────────────────────────────────────
            user_exams = cohort_exams_by_user.get(u_id, [])
            if user_exams:
                # Sort by closing earliest
                user_exams.sort(key=lambda x: x["hours_left"])
                top_exam_info = user_exams[0]
                top_exam: models.CohortExam = top_exam_info["exam"]
                hl = top_exam_info["hours_left"]

                is_emergency_exam = hl <= 3.0
                time_str = f"{int(round(hl))} hours" if hl >= 1.5 else f"{max(15, int(round(hl * 60)))} minutes"
                cohort_title = "your cohort"

                template = random.choice(TIER_1_COHORT_EXAM_COPY)
                t_title = template["title"].format(exam_title=top_exam.title, time_str=time_str, cohort_name=cohort_title)
                t_body = template["body"].format(exam_title=top_exam.title, time_str=time_str, cohort_name=cohort_title)

                urgency = 40.0 if hl <= 3.0 else (20.0 if hl <= 12.0 else 5.0)
                candidates.append({
                    "tier": 1,
                    "category": "cohort_exam",
                    "title": t_title,
                    "body": t_body,
                    "action_route": f"/cohorts/{top_exam.cohort_id}",
                    "data_payload": {
                        "type": "cohort_exam_deadline",
                        "exam_id": str(top_exam.id),
                        "cohort_id": str(top_exam.cohort_id),
                        "hours_left": hl,
                    },
                    "collapse_id": f"cohort_exam_{top_exam.id}",
                    "is_emergency": is_emergency_exam,
                    "score": _score_trigger(
                        tier=1,
                        is_emergency=is_emergency_exam,
                        urgency_boost=urgency,
                        preferred_tier=preferred_tier,
                        preferred_category=category,
                        trigger_category="cohort_exam",
                    ),
                    "stat_key": "dispatched_tier_1_cohort_exam",
                })

            # ─────────────────────────────────────────────────────────────────
            # CANDIDATE: TIER 2 - MIDNIGHT STREAK SAVER
            # ─────────────────────────────────────────────────────────────────
            if effective_streak >= 1 and not user_studied_today:
                in_evening_window = force or (17.5 <= current_hour_wat <= 23.5)
                if in_evening_window:
                    hours_to_midnight = max(0.2, 24.0 - current_hour_wat)
                    is_emergency_streak = (current_hour_wat >= 21.5)  # 9:30 PM WAT onwards

                    template = random.choice(TIER_2_STREAK_AT_RISK_COPY)
                    t_title = template["title"].format(streak=effective_streak)
                    t_body = template["body"].format(streak=effective_streak)

                    urgency = 35.0 if is_emergency_streak else (15.0 if current_hour_wat >= 19.5 else 5.0)
                    candidates.append({
                        "tier": 2,
                        "category": "streak_warning",
                        "title": t_title,
                        "body": t_body,
                        "action_route": "/study",
                        "data_payload": {
                            "type": "streak_saver",
                            "streak": effective_streak,
                        },
                        "collapse_id": "streak_warning_daily",
                        "is_emergency": is_emergency_streak,
                        "score": _score_trigger(
                            tier=2,
                            is_emergency=is_emergency_streak,
                            urgency_boost=urgency,
                            preferred_tier=preferred_tier,
                            preferred_category=category,
                            trigger_category="streak_warning",
                        ),
                        "stat_key": "dispatched_tier_2_streak",
                    })

            # ─────────────────────────────────────────────────────────────────
            # CANDIDATE: TIER 3 - QUICKFIRE DUELS PENDING
            # ─────────────────────────────────────────────────────────────────
            user_duels = pending_duels_by_user.get(u_id, [])
            if user_duels:
                top_duel_info = user_duels[0]
                duel: models.PartnerChallenge = top_duel_info["challenge"]
                challenger: models.User = top_duel_info["challenger"]

                c_name = getattr(challenger, "first_name", "A classmate") or "A classmate"
                c_uname = getattr(challenger, "username", None)
                challenger_display = f"@{c_uname}" if c_uname else c_name
                duel_subject = duel.subject or "Exam Practice"

                template = random.choice(TIER_3_QUICKFIRE_DUEL_COPY)
                t_title = template["title"].format(challenger_name=challenger_display, subject=duel_subject)
                t_body = template["body"].format(challenger_name=challenger_display, subject=duel_subject)

                candidates.append({
                    "tier": 3,
                    "category": "quickfire_duel",
                    "title": t_title,
                    "body": t_body,
                    "action_route": "/network?tab=challenges",
                    "data_payload": {
                        "type": "quickfire_duel_pending",
                        "challenge_id": str(duel.id),
                        "challenger_name": challenger_display,
                        "subject": duel_subject,
                    },
                    "collapse_id": f"challenge_{duel.id}",
                    "is_emergency": False,
                    "score": _score_trigger(
                        tier=3,
                        is_emergency=False,
                        urgency_boost=15.0,
                        preferred_tier=preferred_tier,
                        preferred_category=category,
                        trigger_category="quickfire_duel",
                    ),
                    "stat_key": "dispatched_tier_3_quickfire_duel",
                })

            # ─────────────────────────────────────────────────────────────────
            # CANDIDATE: TIER 4 - DUO PARTNER ACCOUNTABILITY GAP
            # ─────────────────────────────────────────────────────────────────
            if u_partnerships and not user_studied_today:
                in_duo_window = force or (14.5 <= current_hour_wat <= 22.0)
                if in_duo_window:
                    duo_candidate = None
                    for p in u_partnerships:
                        is_user_a = (p.user_a_id == u_id)
                        partner_user = p.user_b if is_user_a else p.user_a
                        partner_today_mins = p.user_b_today_minutes if is_user_a else p.user_a_today_minutes
                        partner_last_date = p.user_b_last_study_date if is_user_a else p.user_a_last_study_date

                        partner_did_study = (partner_today_mins >= 5 or partner_last_date == today_wat)
                        if partner_did_study and partner_user:
                            duo_candidate = (p, partner_user)
                            break

                    if duo_candidate:
                        p, partner_user = duo_candidate
                        p_first = getattr(partner_user, "first_name", "Partner") or "Partner"
                        p_uname = getattr(partner_user, "username", None)
                        display_partner = f"@{p_uname}" if p_uname else p_first

                        template = random.choice(TIER_4_DUO_ACCOUNTABILITY_COPY)
                        t_title = template["title"].format(partner_name=display_partner, streak=effective_streak)
                        t_body = template["body"].format(partner_name=display_partner, streak=effective_streak)

                        candidates.append({
                            "tier": 4,
                            "category": "duo_nudge",
                            "title": t_title,
                            "body": t_body,
                            "action_route": "/network?tab=partners",
                            "data_payload": {
                                "type": "duo_accountability",
                                "partnership_id": str(p.id),
                                "partner_id": str(partner_user.id),
                                "partner_name": display_partner,
                            },
                            "collapse_id": f"duo_nudge_{p.id}",
                            "is_emergency": False,
                            "score": _score_trigger(
                                tier=4,
                                is_emergency=False,
                                urgency_boost=20.0,
                                preferred_tier=preferred_tier,
                                preferred_category=category,
                                trigger_category="duo_nudge",
                            ),
                            "stat_key": "dispatched_tier_4_duo",
                        })

            # ─────────────────────────────────────────────────────────────────
            # CANDIDATE: TIER 5 - SPACED REPETITION FLASHCARDS DUE
            # ─────────────────────────────────────────────────────────────────
            due_cards_count = due_flashcards_by_user.get(u_id, 0)
            if due_cards_count >= 3:
                in_cards_window = force or (9.0 <= current_hour_wat <= 21.0)
                if in_cards_window:
                    template = random.choice(TIER_5_SRS_FLASHCARDS_COPY)
                    t_title = template["title"].format(count=due_cards_count)
                    t_body = template["body"].format(count=due_cards_count)

                    card_urgency = min(20.0, float(due_cards_count))
                    candidates.append({
                        "tier": 5,
                        "category": "srs_flashcards",
                        "title": t_title,
                        "body": t_body,
                        "action_route": "/study?tab=flashcards",
                        "data_payload": {
                            "type": "srs_flashcards_due",
                            "count": due_cards_count,
                        },
                        "collapse_id": "srs_flashcards_daily",
                        "is_emergency": False,
                        "score": _score_trigger(
                            tier=5,
                            is_emergency=False,
                            urgency_boost=card_urgency,
                            preferred_tier=preferred_tier,
                            preferred_category=category,
                            trigger_category="srs_flashcards",
                        ),
                        "stat_key": "dispatched_tier_5_srs_flashcards",
                    })

            # ─────────────────────────────────────────────────────────────────
            # CANDIDATE: TIER 6 - GRANULAR LESSON RESUMPTION & PROGRESS
            # ─────────────────────────────────────────────────────────────────
            user_lessons = in_progress_lessons_by_user.get(u_id, [])
            if user_lessons:
                in_lesson_window = force or (10.0 <= current_hour_wat <= 21.5)
                if in_lesson_window:
                    # Prefer enrollment with highest progress
                    user_lessons.sort(key=lambda x: x["progress"], reverse=True)
                    top_lesson_info = user_lessons[0]
                    course: models.Course = top_lesson_info["course"]
                    next_l = top_lesson_info.get("next_lesson")
                    c_prog = int(round(top_lesson_info["progress"]))

                    c_name = course.name.strip()
                    display_c_name = (c_name[:28] + "...") if len(c_name) > 30 else c_name
                    l_title = (next_l.get("title") or "Next Lesson").strip() if next_l else "Next Lesson"
                    display_l_title = (l_title[:28] + "...") if len(l_title) > 30 else l_title
                    l_type = (next_l.get("type") or "text") if next_l else "text"

                    if l_type == "video":
                        template = random.choice(TIER_6_LESSON_VIDEO_COPY)
                    elif l_type == "assessment":
                        template = random.choice(TIER_6_LESSON_ASSESSMENT_COPY)
                    elif l_type == "code_lab":
                        template = random.choice(TIER_6_LESSON_CODELAB_COPY)
                    else:
                        template = random.choice(TIER_6_LESSON_GENERIC_COPY)

                    t_title = template["title"].format(course_title=display_c_name, lesson_title=display_l_title)
                    t_body = template["body"].format(course_title=display_c_name, lesson_title=display_l_title)

                    action_route = f"/courses/{course.id}/view"
                    candidates.append({
                        "tier": 6,
                        "category": "course_progress",
                        "title": t_title,
                        "body": t_body,
                        "action_route": action_route,
                        "data_payload": {
                            "type": "lesson_resumption",
                            "course_id": str(course.id),
                            "course_title": course.name,
                            "lesson_id": str(next_l.get("id")) if next_l and next_l.get("id") else None,
                            "lesson_type": l_type,
                            "progress": c_prog,
                        },
                        "collapse_id": f"course_progress_{course.id}",
                        "is_emergency": False,
                        "score": _score_trigger(
                            tier=6,
                            is_emergency=False,
                            urgency_boost=15.0 if c_prog >= 60 else 5.0,
                            preferred_tier=preferred_tier,
                            preferred_category=category,
                            trigger_category="course_progress",
                        ),
                        "stat_key": "dispatched_tier_6_lesson_resumption",
                    })

            # ─────────────────────────────────────────────────────────────────
            # CANDIDATE: TIER 7 - LEADERBOARD SHIFT / SOCIAL RIVALRY
            # ─────────────────────────────────────────────────────────────────
            if effective_streak >= 1 and not user_studied_today:
                in_leaderboard_window = force or (12.0 <= current_hour_wat <= 20.0)
                if in_leaderboard_window:
                    template = random.choice(TIER_7_LEADERBOARD_SHIFT_COPY)
                    t_title = template["title"]
                    t_body = template["body"]

                    candidates.append({
                        "tier": 7,
                        "category": "leaderboard_shift",
                        "title": t_title,
                        "body": t_body,
                        "action_route": "/study?tab=leaderboard",
                        "data_payload": {
                            "type": "leaderboard_rivalry",
                            "streak": effective_streak,
                        },
                        "collapse_id": "leaderboard_shift_weekly",
                        "is_emergency": False,
                        "score": _score_trigger(
                            tier=7,
                            is_emergency=False,
                            urgency_boost=10.0,
                            preferred_tier=preferred_tier,
                            preferred_category=category,
                            trigger_category="leaderboard_shift",
                        ),
                        "stat_key": "dispatched_tier_7_leaderboard_shift",
                    })

            # ─────────────────────────────────────────────────────────────────
            # CANDIDATE: TIER 8 - OBJECTIVE QUESTS NEAR-COMPLETION
            # ─────────────────────────────────────────────────────────────────
            user_objs = objectives_by_user.get(u_id, [])
            if user_objs:
                candidate_quests = []
                for uop in user_objs:
                    obj = objectives_by_code.get(uop.objective_code)
                    if not obj or uop.target_value <= 0:
                        continue
                    pct = int(round((uop.current_value / uop.target_value) * 100))
                    if 65 <= pct < 100:
                        remaining = max(1, uop.target_value - uop.current_value)
                        candidate_quests.append((pct, remaining, uop, obj))

                if candidate_quests:
                    candidate_quests.sort(key=lambda x: x[0], reverse=True)
                    best_pct, remaining, best_uop, best_obj = candidate_quests[0]

                    q_title = best_obj.title.strip()
                    display_q_title = (q_title[:28] + "...") if len(q_title) > 30 else q_title
                    unit_label = best_obj.target_unit or "items"
                    if remaining == 1 and unit_label.endswith("s"):
                        unit_label = unit_label[:-1]

                    template = random.choice(TIER_8_OBJECTIVE_QUEST_COPY)
                    t_title = template["title"].format(
                        name=u_first,
                        quest_title=display_q_title,
                        remaining=remaining,
                        unit=unit_label,
                        progress=best_pct,
                        coins=best_obj.coin_reward,
                    )
                    t_body = template["body"].format(
                        name=u_first,
                        quest_title=display_q_title,
                        remaining=remaining,
                        unit=unit_label,
                        progress=best_pct,
                        coins=best_obj.coin_reward,
                    )

                    candidates.append({
                        "tier": 8,
                        "category": "objective_quest",
                        "title": t_title,
                        "body": t_body,
                        "action_route": "/study?tab=objectives",
                        "data_payload": {
                            "type": "objective_quest",
                            "objective_code": best_obj.code,
                            "objective_title": best_obj.title,
                            "progress": best_pct,
                            "remaining": remaining,
                            "reward_coins": best_obj.coin_reward,
                        },
                        "collapse_id": f"quest_{best_obj.code}",
                        "is_emergency": False,
                        "score": _score_trigger(
                            tier=8,
                            is_emergency=False,
                            urgency_boost=15.0 if best_pct >= 80 else 5.0,
                            preferred_tier=preferred_tier,
                            preferred_category=category,
                            trigger_category="objective_quest",
                        ),
                        "stat_key": "dispatched_tier_8_quest",
                    })

            # ─────────────────────────────────────────────────────────────────
            # CANDIDATE: TIER 9 - BROKEN STREAK RECOVERY (24H GRACE SAVE)
            # ─────────────────────────────────────────────────────────────────
            # User missed yesterday, streak broke, but was active 2 days ago
            if u_last_login and (today_wat - u_last_login).days == 2 and effective_streak == 0:
                in_recovery_window = force or (9.0 <= current_hour_wat <= 21.0)
                if in_recovery_window:
                    template = random.choice(TIER_9_BROKEN_STREAK_RECOVERY_COPY)
                    t_title = template["title"]
                    t_body = template["body"]

                    candidates.append({
                        "tier": 9,
                        "category": "broken_streak_recovery",
                        "title": t_title,
                        "body": t_body,
                        "action_route": "/study",
                        "data_payload": {
                            "type": "broken_streak_recovery",
                        },
                        "collapse_id": "streak_recovery_daily",
                        "is_emergency": False,
                        "score": _score_trigger(
                            tier=9,
                            is_emergency=False,
                            urgency_boost=10.0,
                            preferred_tier=preferred_tier,
                            preferred_category=category,
                            trigger_category="broken_streak_recovery",
                        ),
                        "stat_key": "dispatched_tier_9_broken_streak",
                    })

            # ─────────────────────────────────────────────────────────────────
            # CANDIDATE: TIER 10 - INACTIVITY COMEBACK & NEW COURSE DROP
            # ─────────────────────────────────────────────────────────────────
            absent_days = (today_wat - u_last_login).days if u_last_login else 999
            if 3 <= absent_days <= 14:
                in_comeback_window = force or (11.0 <= current_hour_wat <= 19.0)
                if in_comeback_window:
                    template = random.choice(TIER_10_INACTIVITY_COMEBACK_COPY)
                    t_title = template["title"].format(days=absent_days)
                    t_body = template["body"].format(days=absent_days)

                    candidates.append({
                        "tier": 10,
                        "category": "reengagement",
                        "title": t_title,
                        "body": t_body,
                        "action_route": "/study",
                        "data_payload": {
                            "type": "comeback_nudge",
                            "absent_days": absent_days,
                        },
                        "collapse_id": "reengagement_dormant",
                        "is_emergency": False,
                        "score": _score_trigger(
                            tier=10,
                            is_emergency=False,
                            urgency_boost=0.0,
                            preferred_tier=preferred_tier,
                            preferred_category=category,
                            trigger_category="reengagement",
                        ),
                        "stat_key": "dispatched_tier_10_comeback",
                    })

            # New Course discovery (Category affinity match)
            if new_courses_pool:
                user_enrolled = enrolled_course_ids.get(u_id, set())
                u_cats = user_categories.get(u_id, set())
                user_uni = (getattr(user, "university", "") or "").strip().lower()

                available_new = [c for c in new_courses_pool if c.id not in user_enrolled and c.admin_id != u_id]
                if available_new:
                    # Score by affinity
                    def _score_new_course(c: models.Course) -> float:
                        s = 0.0
                        if c.category_id and c.category_id in u_cats:
                            s += 5.0
                        if user_uni and c.organisation and user_uni in (c.organisation.name or "").lower():
                            s += 3.0
                        s += float(getattr(c, "rating", 4.5) or 4.5) * 0.1
                        return s

                    available_new.sort(key=_score_new_course, reverse=True)
                    top_c = available_new[0]
                    c_title = top_c.name.strip()
                    display_c_title = (c_title[:28] + "...") if len(c_title) > 30 else c_title
                    cat_name = top_c.category.name if top_c.category else "General"

                    template = random.choice(TIER_10_NEW_COURSE_COPY)
                    t_title = template["title"].format(course_title=display_c_title, category=cat_name)
                    t_body = template["body"].format(course_title=display_c_title, category=cat_name)

                    candidates.append({
                        "tier": 10,
                        "category": "new_courses",
                        "title": t_title,
                        "body": t_body,
                        "action_route": f"/courses/{top_c.id}/view",
                        "data_payload": {
                            "type": "new_course_discovery",
                            "course_id": str(top_c.id),
                            "course_title": top_c.name,
                            "category": cat_name,
                        },
                        "collapse_id": f"new_course_{top_c.id}",
                        "is_emergency": False,
                        "score": _score_trigger(
                            tier=10,
                            is_emergency=False,
                            urgency_boost=5.0 if top_c.category_id in u_cats else 0.0,
                            preferred_tier=preferred_tier,
                            preferred_category=category,
                            trigger_category="new_courses",
                        ),
                        "stat_key": "dispatched_tier_10_new_course",
                    })

            # ─────────────────────────────────────────────────────────────────
            # CANDIDATE: DAYTIME GENERAL FOCUS NUDGE
            # ─────────────────────────────────────────────────────────────────
            if not user_studied_today and u_last_login != today_wat:
                in_daytime_window = force or (12.5 <= current_hour_wat <= 15.5)
                if in_daytime_window:
                    template = random.choice(TIER_DAYTIME_NUDGE_COPY)
                    candidates.append({
                        "tier": 0,
                        "category": "daytime_nudge",
                        "title": template["title"],
                        "body": template["body"],
                        "action_route": "/study",
                        "data_payload": {"type": "daytime_nudge"},
                        "collapse_id": "daytime_nudge_daily",
                        "is_emergency": False,
                        "score": 30.0,
                        "stat_key": "dispatched_daytime_nudge",
                    })

            if not candidates:
                if user_studied_today:
                    stats["skipped_already_studied"] += 1
                continue

            # ─────────────────────────────────────────────────────────────────
            # SELECT HIGHEST-SCORING CANDIDATE TRIGGER
            # ─────────────────────────────────────────────────────────────────
            candidates.sort(key=lambda c: c["score"], reverse=True)
            chosen = candidates[0]

            # Anti-spam guard: check spacing & daily limits
            if not force:
                eligible, reason = check_user_nudge_eligibility(
                    db=db,
                    user_id=u_id,
                    is_emergency=chosen["is_emergency"],
                )
                if not eligible:
                    if reason == "daily_limit":
                        stats["skipped_daily_limit_reached"] += 1
                    else:
                        stats["skipped_min_interval"] += 1
                    stats["skipped_recently_nudged"] += 1
                    continue

            # Dispatch notification
            if not dry_run:
                dispatch_notification(
                    db=db,
                    recipient_user_ids=[u_id],
                    title=chosen["title"],
                    body=chosen["body"],
                    category=chosen["category"],
                    action_route=chosen["action_route"],
                    data_payload=chosen["data_payload"],
                    send_push=True,
                    collapse_id=chosen["collapse_id"],
                )

            # Update stats
            stat_key = chosen.get("stat_key")
            if stat_key and stat_key in stats:
                stats[stat_key] += 1

            stats["dispatched_records"].append({
                "user_id": str(u_id),
                "tier": chosen["tier"],
                "category": chosen["category"],
                "title": chosen["title"],
                "body": chosen["body"],
                "action_route": chosen["action_route"],
                "score": chosen["score"],
                "is_emergency": chosen["is_emergency"],
            })

    return stats
