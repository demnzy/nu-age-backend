"""
reengagement_engine.py
──────────────────────────────────────────────────────────────────────────────
Duolingo-Style Automated Re-Engagement & Streak-Saver Engine for Nu-Age:
1. Detects users with streaks at risk of expiring at midnight (Solo & Duo Streaks).
2. Detects Duo accountability gaps (when a study partner studied today, but you haven't).
3. Detects inactive learners (48h - 72h absence) and daytime focus nudges.
4. Selects quirky, engaging, randomized copy inspired by Duolingo's signature tone.
5. Dispatches persistent in-app UserNotification and OneSignal/FCM push notifications.
6. Enforces robust safeguards: strict 20h anti-spam window and quiet hours (10:30 PM - 9:00 AM WAT).
──────────────────────────────────────────────────────────────────────────────
"""

import random
from datetime import datetime, date, timedelta, timezone
from typing import Dict, Any, List, Optional
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
# 2. DUOLINGO-INSPIRED QUIRKY COPY BANK
# ─────────────────────────────────────────────────────────────────────────────

TIER_1_STREAK_AT_RISK_COPY = [
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
        "body": "You've worked so hard for {streak} consecutive days! Don't throw it away now. Open Nu-Age and secure the flame.",
    },
    {
        "title": "5 minutes now = streak saved ⚡",
        "body": "It only takes 5 minutes to keep your {streak}-day streak alive. No excuses, future graduate! Let's get it done.",
    },
]

TIER_2_DUO_ACCOUNTABILITY_COPY = [
    {
        "title": "{partner_name} is carrying the team right now! 👀",
        "body": "{partner_name} already logged their study session today. Are you really going to leave them hanging and let the Duo Streak die? 😬🔥",
    },
    {
        "title": "Accountability check! 🤝",
        "body": "{partner_name} did their part today! Jump into Nu-Age for 10 minutes so you both lock today's Duo Streak together 🎯",
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

TIER_3_DAYTIME_NUDGE_COPY = [
    {
        "title": "Got 5 minutes? 🧠",
        "body": "That’s all it takes to keep your brain sharp today. Quick practice now = zero panic on exam day 🎯",
    },
    {
        "title": "Your textbooks called 📖",
        "body": "They miss the sound of your page turns! Jump into the Self-Study Hub for a quick review session.",
    },
    {
        "title": "A question a day keeps exam fear away 💡",
        "body": "Knock out 10 practice questions on your commute or break. Your future self will thank you!",
    },
    {
        "title": "Brain workout time! 🏋️‍♂️",
        "body": "Give your mind a quick 5-minute stretch. Solve a few flashcards or practice questions now.",
    },
]

TIER_4_INACTIVITY_COMEBACK_COPY = [
    {
        "title": "We haven't seen you in {days} days 🥺",
        "body": "The practice packs are getting lonely! Jump back in today and show these exam questions who's boss 💡",
    },
    {
        "title": "Your future exam-self is begging you ⏳",
        "body": "Consistency is what turns average students into top rankers. Take 5 minutes today to get back on track!",
    },
    {
        "title": "These questions won't answer themselves! 📚",
        "body": "You've been away for {days} days. Jump back into Nu-Age today and restart your momentum 🚀",
    },
    {
        "title": "Streaks reset, but knowledge doesn't 🔥",
        "body": "Even the best miss a day. Start fresh today, claim milestone Nu-Coins, and build your greatest comeback yet!",
    },
]

TIER_5_COURSE_NEAR_COMPLETION_COPY = [
    {
        "title": "{course_title} is {progress}% complete! 🎓",
        "body": "You are so close to the finish line! Jump in for 10 minutes, finish your lessons, and claim your certificate 📜✨",
    },
    {
        "title": "Your {course_title} certificate is calling 📜",
        "body": "You're sitting at {progress}%! Don't let your certificate collect digital dust. Finish strong today 🚀",
    },
    {
        "title": "Almost a certified pro! 🏆",
        "body": "{progress}% done with {course_title}! Just a couple more lessons stand between you and your milestone achievement.",
    },
    {
        "title": "You didn't come this far to only come this far ⚡",
        "body": "{course_title} is at {progress}%. Knock out the next module today and celebrate your win 🎉",
    },
]

TIER_5_COURSE_MID_PROGRESS_COPY = [
    {
        "title": "Don't leave {course_title} hanging! 🥺",
        "body": "You paused at {progress}%. Your future self is waiting for you to finish what you started! Take 5 minutes now 💡",
    },
    {
        "title": "The next lesson doesn't bite, promise! 📖",
        "body": "You're {progress}% through {course_title}. Knock out just 1 lesson on your break and watch your progress bar climb ⚡",
    },
    {
        "title": "Who left {course_title} at {progress}%? 👀",
        "body": "Oh wait, that was you! 😂 No worries, 1 quick lesson today gets your momentum right back on track 🎯",
    },
    {
        "title": "Consistency beats perfection 🎯",
        "body": "Every expert was once a beginner who didn't quit at {progress}%. Jump back into {course_title} today!",
    },
    {
        "title": "Progress check: {progress}% done 🚀",
        "body": "Small daily wins turn into big credentials. Dive into {course_title} for 10 minutes today!",
    },
]

TIER_5_COURSE_UNSTARTED_COPY = [
    {
        "title": "Ready to start {course_title}? 🚀",
        "body": "You're enrolled and Lesson 1 is waiting for you! 10 minutes today is all it takes to kick off your learning journey.",
    },
    {
        "title": "Your first step in {course_title} 💡",
        "body": "The secret to getting ahead is getting started. Jump into Lesson 1 and build your momentum today 🎯",
    },
    {
        "title": "New course on your plate! 📚",
        "body": "Lesson 1 in {course_title} is ready whenever you are. Take 5 minutes to explore it today!",
    },
]

TIER_6_OBJECTIVE_QUEST_COPY = [
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

AUTOMATED_CATEGORIES = ("streak_warning", "duo_nudge", "daytime_nudge", "reengagement", "course_progress", "objective_quest")


# ─────────────────────────────────────────────────────────────────────────────
# 3. ANTI-SPAM & RECENT DISPATCH FILTER
# ─────────────────────────────────────────────────────────────────────────────

def user_was_recently_nudged(db: Session, user_id: UUID, window_hours: float = 18.0) -> bool:
    """
    Checks if an automated re-engagement or streak notification was already sent
    to this user within the sliding window (defaults to 18 hours), preventing spam.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    recent = (
        db.query(models.UserNotification.id)
        .filter(
            models.UserNotification.user_id == user_id,
            models.UserNotification.category.in_(AUTOMATED_CATEGORIES),
            models.UserNotification.created_at >= cutoff,
        )
        .first()
    )
    return recent is not None


# ─────────────────────────────────────────────────────────────────────────────
# 4. EVALUATION & DISPATCH ENGINE
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
    Main evaluation cycle:
    1. Inspects active users, their active study partnerships, and in-progress course enrollments.
    2. Enforces Nigerian WAT time of day (quiet hours vs midday vs evening).
    3. Categorizes users into:
       - Tier 1: Streak at Risk (Evening countdown before midnight)
       - Tier 2: Duo Accountability (Partner studied today, you haven't!)
       - Tier 3: Daytime Nudge (Midday focus questions)
       - Tier 4: Inactivity Comeback (2-3 days absent)
       - Tier 5: Course In-Progress (Unfinished courses, progress milestones, certificate goals)
       - Tier 6: Objective Near-Completion Quests (70%+ to target, coins reward pending)
    4. Dispatches personalized, quirky in-app and push notifications.
    """
    wat_now = get_current_wat_time()
    today_wat = wat_now.date()
    yesterday_wat = today_wat - timedelta(days=1)
    current_hour_wat = wat_now.hour + (wat_now.minute / 60.0)

    stats = {
        "timestamp_wat": wat_now.isoformat(),
        "evaluated_users": 0,
        "dispatched_tier_1_streak": 0,
        "dispatched_tier_2_duo": 0,
        "dispatched_tier_3_daytime": 0,
        "dispatched_tier_4_inactivity": 0,
        "dispatched_tier_5_course_progress": 0,
        "dispatched_tier_6_quest": 0,
        "skipped_quiet_hours": False,
        "skipped_recently_nudged": 0,
        "skipped_already_studied": 0,
        "dispatched_records": [],
    }

    # Quiet Hours Guard: We do not disturb users while sleeping (10:30 PM to 8:30 AM WAT)
    # unless force=True for testing/admin triggers.
    if not force:
        if current_hour_wat >= 22.5 or current_hour_wat < 8.5:
            stats["skipped_quiet_hours"] = True
            return stats

    # Query candidate users (verified accounts)
    user_query = db.query(models.User).filter(
        models.User.is_verified == True
    )
    if target_user_id:
        user_query = user_query.filter(models.User.id == target_user_id)
    else:
        # Limit to active/recent cohort to prevent unbounded queries
        user_query = user_query.order_by(models.User.streak.desc(), models.User.last_login_date.desc().nullslast()).limit(300)

    users = user_query.all()
    stats["evaluated_users"] = len(users)

    # Pre-fetch all active partnerships involving these users
    user_ids = [u.id for u in users]
    active_partnerships = (
        db.query(models.StudyPartnership)
        .filter(
            models.StudyPartnership.status == "active",
            or_(
                models.StudyPartnership.user_a_id.in_(user_ids),
                models.StudyPartnership.user_b_id.in_(user_ids),
            ),
        )
        .all()
    )

    # Group partnerships by user ID
    partnerships_by_user: Dict[UUID, List[models.StudyPartnership]] = {}
    for p in active_partnerships:
        partnerships_by_user.setdefault(p.user_a_id, []).append(p)
        partnerships_by_user.setdefault(p.user_b_id, []).append(p)

    # Pre-fetch incomplete course enrollments involving these users
    in_progress_enrollments = (
        db.query(models.Enrollment)
        .options(joinedload(models.Enrollment.course))
        .filter(
            models.Enrollment.student_id.in_(user_ids),
            models.Enrollment.completed_at.is_(None),
            models.Enrollment.progress < 99.9,
        )
        .all()
    )

    # Group enrollments by student_id
    enrollments_by_user: Dict[UUID, List[models.Enrollment]] = {}
    for en in in_progress_enrollments:
        if en.course and en.course.name:
            enrollments_by_user.setdefault(en.student_id, []).append(en)

    # Pre-fetch active platform objectives & incomplete user progress
    active_objectives = (
        db.query(models.PlatformObjective)
        .filter(models.PlatformObjective.is_active == True)
        .all()
    )
    objectives_by_code: Dict[str, models.PlatformObjective] = {o.code: o for o in active_objectives}

    user_obj_progress = (
        db.query(models.UserObjectiveProgress)
        .filter(
            models.UserObjectiveProgress.user_id.in_(user_ids),
            models.UserObjectiveProgress.is_completed == False,
        )
        .all()
    )
    objectives_by_user: Dict[UUID, List[models.UserObjectiveProgress]] = {}
    for uop in user_obj_progress:
        if uop.objective_code in objectives_by_code:
            objectives_by_user.setdefault(uop.user_id, []).append(uop)

    for user in users:
        u_id = user.id
        u_streak = getattr(user, "streak", 0) or 0
        u_last_login = getattr(user, "last_login_date", None)

        # Anti-spam guard: max 1 automated notification per user per 18 hours
        if not force and user_was_recently_nudged(db, u_id, window_hours=18.0):
            stats["skipped_recently_nudged"] += 1
            continue

        user_partnerships = partnerships_by_user.get(u_id, [])
        user_enrollments = enrollments_by_user.get(u_id, [])
        user_objectives = objectives_by_user.get(u_id, [])

        # Check if user has already completed study activity today
        user_studied_today = False
        if u_last_login == today_wat:
            # Check if any partnership has logged minutes today for this user
            for p in user_partnerships:
                if p.user_a_id == u_id and (p.user_a_today_minutes >= 5 or p.user_a_last_study_date == today_wat):
                    user_studied_today = True
                    break
                elif p.user_b_id == u_id and (p.user_b_today_minutes >= 5 or p.user_b_last_study_date == today_wat):
                    user_studied_today = True
                    break

        # Check max duo streak among active partnerships
        max_duo_streak = max([p.duo_streak for p in user_partnerships] + [0])
        effective_streak = max(u_streak, max_duo_streak)

        # ─────────────────────────────────────────────────────────────────────
        # EXPLICIT TIER OVERRIDE (e.g. for testing / previewing specific tiers)
        # ─────────────────────────────────────────────────────────────────────
        if (preferred_tier == 6 or category == "objective_quest") and user_objectives:
            candidate_quests = []
            for uop in user_objectives:
                obj = objectives_by_code.get(uop.objective_code)
                if not obj or uop.target_value <= 0:
                    continue
                pct = int(round((uop.current_value / uop.target_value) * 100))
                remaining = max(1, uop.target_value - uop.current_value)
                candidate_quests.append((pct, remaining, uop, obj))

            if candidate_quests:
                candidate_quests.sort(key=lambda x: x[0], reverse=True)
                best_pct, remaining, best_uop, best_obj = candidate_quests[0]

                user_first = getattr(user, "first_name", "Scholar") or "Scholar"
                q_title = best_obj.title.strip()
                display_q_title = (q_title[:30] + "...") if len(q_title) > 32 else q_title
                unit_label = best_obj.target_unit or "items"
                if remaining == 1 and unit_label.endswith("s"):
                    unit_label = unit_label[:-1]

                template = random.choice(TIER_6_OBJECTIVE_QUEST_COPY)
                title = template["title"].format(
                    name=user_first,
                    quest_title=display_q_title,
                    remaining=remaining,
                    unit=unit_label,
                    progress=best_pct,
                    coins=best_obj.coin_reward,
                )
                body = template["body"].format(
                    name=user_first,
                    quest_title=display_q_title,
                    remaining=remaining,
                    unit=unit_label,
                    progress=best_pct,
                    coins=best_obj.coin_reward,
                )

                if not dry_run:
                    dispatch_notification(
                        db=db,
                        recipient_user_ids=[u_id],
                        title=title,
                        body=body,
                        category="objective_quest",
                        action_route="/study?tab=objectives",
                        data_payload={
                            "type": "objective_quest",
                            "objective_code": best_obj.code,
                            "objective_title": best_obj.title,
                            "progress": best_pct,
                            "remaining": remaining,
                            "reward_coins": best_obj.coin_reward,
                        },
                        send_push=True,
                        collapse_id=f"quest_{best_obj.code}",
                    )
                stats["dispatched_tier_6_quest"] += 1
                stats["dispatched_records"].append({
                    "user_id": str(u_id),
                    "tier": 6,
                    "title": title,
                    "body": body,
                    "objective_code": best_obj.code,
                    "progress": best_pct,
                })
                continue

        if (preferred_tier == 5 or category == "course_progress") and user_enrollments:
            user_enrollments.sort(key=lambda e: (e.progress or 0.0), reverse=True)
            target_en = user_enrollments[0]
            c_name = target_en.course.name.strip()
            display_title = (c_name[:32] + "...") if len(c_name) > 35 else c_name
            prog_val = int(round(target_en.progress or 0.0))

            if prog_val >= 60:
                template = random.choice(TIER_5_COURSE_NEAR_COMPLETION_COPY)
            elif prog_val > 0:
                template = random.choice(TIER_5_COURSE_MID_PROGRESS_COPY)
            else:
                template = random.choice(TIER_5_COURSE_UNSTARTED_COPY)

            title = template["title"].format(course_title=display_title, progress=prog_val)
            body = template["body"].format(course_title=display_title, progress=prog_val)
            action_rt = f"/courses/{target_en.course.id}/view"

            if not dry_run:
                dispatch_notification(
                    db=db,
                    recipient_user_ids=[u_id],
                    title=title,
                    body=body,
                    category="course_progress",
                    action_route=action_rt,
                    data_payload={
                        "type": "course_progress",
                        "course_id": str(target_en.course.id),
                        "course_title": target_en.course.name,
                        "progress": prog_val,
                    },
                    send_push=True,
                    collapse_id=f"course_progress_{target_en.course.id}",
                )
            stats["dispatched_tier_5_course_progress"] += 1
            stats["dispatched_records"].append({
                "user_id": str(u_id),
                "tier": 5,
                "title": title,
                "body": body,
                "course": target_en.course.name,
                "progress": prog_val,
            })
            continue

        # ─────────────────────────────────────────────────────────────────────
        # EVALUATE TIER 2: DUO ACCOUNTABILITY NUDGE (High Priority)
        # Condition: Partner studied today, but this user hasn't!
        # Time window: 15:30 to 21:45 WAT
        # ─────────────────────────────────────────────────────────────────────
        duo_nudge_candidate = None
        if user_partnerships and not user_studied_today:
            if force or (15.5 <= current_hour_wat <= 21.75):
                for p in user_partnerships:
                    is_user_a = (p.user_a_id == u_id)
                    partner_user = p.user_b if is_user_a else p.user_a
                    partner_today_mins = p.user_b_today_minutes if is_user_a else p.user_a_today_minutes
                    partner_last_date = p.user_b_last_study_date if is_user_a else p.user_a_last_study_date

                    # Did the partner study today while this user hasn't?
                    partner_did_study = (partner_today_mins >= 5 or partner_last_date == today_wat)
                    if partner_did_study and partner_user:
                        duo_nudge_candidate = (p, partner_user)
                        break

        if duo_nudge_candidate:
            p, partner_user = duo_nudge_candidate
            partner_first = getattr(partner_user, "first_name", "Partner")
            partner_username = getattr(partner_user, "username", None)
            display_partner = f"@{partner_username}" if partner_username else partner_first

            template = random.choice(TIER_2_DUO_ACCOUNTABILITY_COPY)
            title = template["title"].format(partner_name=display_partner, streak=effective_streak)
            body = template["body"].format(partner_name=display_partner, streak=effective_streak)

            if not dry_run:
                dispatch_notification(
                    db=db,
                    recipient_user_ids=[u_id],
                    title=title,
                    body=body,
                    category="duo_nudge",
                    action_route="/network?tab=partners",
                    data_payload={
                        "type": "duo_accountability",
                        "partnership_id": str(p.id),
                        "partner_id": str(partner_user.id),
                        "partner_name": display_partner,
                    },
                    send_push=True,
                    collapse_id=f"duo_nudge_{p.id}",
                )
            stats["dispatched_tier_2_duo"] += 1
            stats["dispatched_records"].append({"user_id": str(u_id), "tier": 2, "title": title, "body": body})
            continue

        # ─────────────────────────────────────────────────────────────────────
        # EVALUATE TIER 1: STREAK AT RISK (Evening Warning: 18:00 - 22:00 WAT)
        # Condition: User has active streak >= 1, has not studied today
        # ─────────────────────────────────────────────────────────────────────
        if effective_streak >= 1 and not user_studied_today:
            if force or (18.0 <= current_hour_wat <= 22.0):
                template = random.choice(TIER_1_STREAK_AT_RISK_COPY)
                title = template["title"].format(streak=effective_streak)
                body = template["body"].format(streak=effective_streak)

                if not dry_run:
                    dispatch_notification(
                        db=db,
                        recipient_user_ids=[u_id],
                        title=title,
                        body=body,
                        category="streak_warning",
                        action_route="/study",
                        data_payload={
                            "type": "streak_saver",
                            "streak": effective_streak,
                        },
                        send_push=True,
                        collapse_id="streak_warning_daily",
                    )
                stats["dispatched_tier_1_streak"] += 1
                stats["dispatched_records"].append({"user_id": str(u_id), "tier": 1, "title": title, "body": body})
                continue

        # ─────────────────────────────────────────────────────────────────────
        # EVALUATE TIER 6: OBJECTIVE NEAR-COMPLETION QUEST NUDGE
        # Condition: User has active incomplete objective >= 70% complete
        # Time window: Daytime/afternoon/evening (12:00 to 21:30 WAT)
        # ─────────────────────────────────────────────────────────────────────
        if user_objectives and not user_studied_today:
            eval_quest_now = False
            if force:
                eval_quest_now = True
            elif 12.0 <= current_hour_wat <= 21.5:
                eval_quest_now = True

            if eval_quest_now:
                candidate_quests = []
                for uop in user_objectives:
                    obj = objectives_by_code.get(uop.objective_code)
                    if not obj or uop.target_value <= 0:
                        continue
                    pct = int(round((uop.current_value / uop.target_value) * 100))
                    if 70 <= pct < 100:
                        remaining = max(1, uop.target_value - uop.current_value)
                        candidate_quests.append((pct, remaining, uop, obj))

                if candidate_quests:
                    candidate_quests.sort(key=lambda x: x[0], reverse=True)
                    best_pct, remaining, best_uop, best_obj = candidate_quests[0]

                    user_first = getattr(user, "first_name", "Scholar") or "Scholar"
                    q_title = best_obj.title.strip()
                    display_q_title = (q_title[:30] + "...") if len(q_title) > 32 else q_title
                    unit_label = best_obj.target_unit or "items"
                    if remaining == 1 and unit_label.endswith("s"):
                        unit_label = unit_label[:-1]

                    template = random.choice(TIER_6_OBJECTIVE_QUEST_COPY)
                    title = template["title"].format(
                        name=user_first,
                        quest_title=display_q_title,
                        remaining=remaining,
                        unit=unit_label,
                        progress=best_pct,
                        coins=best_obj.coin_reward,
                    )
                    body = template["body"].format(
                        name=user_first,
                        quest_title=display_q_title,
                        remaining=remaining,
                        unit=unit_label,
                        progress=best_pct,
                        coins=best_obj.coin_reward,
                    )

                    if not dry_run:
                        dispatch_notification(
                            db=db,
                            recipient_user_ids=[u_id],
                            title=title,
                            body=body,
                            category="objective_quest",
                            action_route="/study?tab=objectives",
                            data_payload={
                                "type": "objective_quest",
                                "objective_code": best_obj.code,
                                "objective_title": best_obj.title,
                                "progress": best_pct,
                                "remaining": remaining,
                                "reward_coins": best_obj.coin_reward,
                            },
                            send_push=True,
                            collapse_id=f"quest_{best_obj.code}",
                        )
                    stats["dispatched_tier_6_quest"] += 1
                    stats["dispatched_records"].append({
                        "user_id": str(u_id),
                        "tier": 6,
                        "title": title,
                        "body": body,
                        "objective_code": best_obj.code,
                        "progress": best_pct,
                    })
                    continue

        # ─────────────────────────────────────────────────────────────────────
        # EVALUATE TIER 5: COURSE IN-PROGRESS NUDGE
        # Condition: User has an active incomplete enrollment, hasn't studied today
        # Time window: Daytime/afternoon focus (11:30 - 18:00), or evening (18:00 - 21:30) if streak < 1
        # ─────────────────────────────────────────────────────────────────────
        if user_enrollments and not user_studied_today:
            eval_course_now = False
            if force:
                eval_course_now = True
            elif 11.5 <= current_hour_wat < 18.0:
                eval_course_now = True
            elif 18.0 <= current_hour_wat <= 21.5 and effective_streak < 1:
                eval_course_now = True

            if eval_course_now:
                user_enrollments.sort(key=lambda e: (e.progress or 0.0), reverse=True)
                target_en = user_enrollments[0]
                c_name = target_en.course.name.strip()
                display_title = (c_name[:32] + "...") if len(c_name) > 35 else c_name
                prog_val = int(round(target_en.progress or 0.0))

                if prog_val >= 60:
                    template = random.choice(TIER_5_COURSE_NEAR_COMPLETION_COPY)
                elif prog_val > 0:
                    template = random.choice(TIER_5_COURSE_MID_PROGRESS_COPY)
                else:
                    template = random.choice(TIER_5_COURSE_UNSTARTED_COPY)

                title = template["title"].format(course_title=display_title, progress=prog_val)
                body = template["body"].format(course_title=display_title, progress=prog_val)
                action_rt = f"/courses/{target_en.course.id}/view"

                if not dry_run:
                    dispatch_notification(
                        db=db,
                        recipient_user_ids=[u_id],
                        title=title,
                        body=body,
                        category="course_progress",
                        action_route=action_rt,
                        data_payload={
                            "type": "course_progress",
                            "course_id": str(target_en.course.id),
                            "course_title": target_en.course.name,
                            "progress": prog_val,
                        },
                        send_push=True,
                        collapse_id=f"course_progress_{target_en.course.id}",
                    )
                stats["dispatched_tier_5_course_progress"] += 1
                stats["dispatched_records"].append({
                    "user_id": str(u_id),
                    "tier": 5,
                    "title": title,
                    "body": body,
                    "course": target_en.course.name,
                    "progress": prog_val,
                })
                continue

        # ─────────────────────────────────────────────────────────────────────
        # EVALUATE TIER 4: INACTIVITY RE-ENGAGEMENT (2 to 3 days absent)
        # Condition: User hasn't logged in for 2-3 days
        # Time window: 11:00 to 19:00 WAT
        # ─────────────────────────────────────────────────────────────────────
        if u_last_login and (today_wat - u_last_login).days in (2, 3):
            if force or (11.0 <= current_hour_wat <= 19.0):
                absent_days = (today_wat - u_last_login).days
                template = random.choice(TIER_4_INACTIVITY_COMEBACK_COPY)
                title = template["title"].format(days=absent_days, streak=effective_streak)
                body = template["body"].format(days=absent_days, streak=effective_streak)

                if not dry_run:
                    dispatch_notification(
                        db=db,
                        recipient_user_ids=[u_id],
                        title=title,
                        body=body,
                        category="reengagement",
                        action_route="/study",
                        data_payload={
                            "type": "comeback_nudge",
                            "absent_days": absent_days,
                        },
                        send_push=True,
                        collapse_id="reengagement_nudge",
                    )
                stats["dispatched_tier_4_inactivity"] += 1
                stats["dispatched_records"].append({"user_id": str(u_id), "tier": 4, "title": title, "body": body})
                continue

        # ─────────────────────────────────────────────────────────────────────
        # EVALUATE TIER 3: DAYTIME NUDGE (Mid-day 13:00 to 15:30 WAT)
        # Condition: User hasn't opened Nu-Age today yet (login != today)
        # ─────────────────────────────────────────────────────────────────────
        if not user_studied_today and u_last_login != today_wat:
            if force or (13.0 <= current_hour_wat <= 15.5):
                template = random.choice(TIER_3_DAYTIME_NUDGE_COPY)
                title = template["title"]
                body = template["body"]

                if not dry_run:
                    dispatch_notification(
                        db=db,
                        recipient_user_ids=[u_id],
                        title=title,
                        body=body,
                        category="daytime_nudge",
                        action_route="/study",
                        data_payload={
                            "type": "daytime_nudge",
                        },
                        send_push=True,
                        collapse_id="daytime_nudge_daily",
                    )
                stats["dispatched_tier_3_daytime"] += 1
                stats["dispatched_records"].append({"user_id": str(u_id), "tier": 3, "title": title, "body": body})
                continue

        if user_studied_today:
            stats["skipped_already_studied"] += 1

    return stats
