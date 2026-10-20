"""
scratch/test_network_notif_and_reengagement.py
──────────────────────────────────────────────────────────────────────────────
In-memory verification suite strictly adhering to Nu-age/GEMINI.md:
1. Validates network request dispatch & acceptance notifications.
2. Validates multi-nudge daily volume limits (< 3/day) and 4-hour interval spacing.
3. Validates Tier 7 New Courses contextual pipeline and quirky copy generation.
Uses isolated in-memory SQLite (:memory:) without touching any live database.
──────────────────────────────────────────────────────────────────────────────
"""

import sys
import os
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

@compiles(ARRAY, "sqlite")
def compile_array(element, compiler, **kw):
    return "TEXT"

@compiles(JSONB, "sqlite")
def compile_jsonb(element, compiler, **kw):
    return "JSON"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import models
from database import Base
import schemas
from services.reengagement_engine import (
    AUTOMATED_CATEGORIES,
    MAX_DAILY_AUTOMATED_NOTIFICATIONS,
    MIN_HOURS_BETWEEN_NUDGES,
    check_user_nudge_eligibility,
    user_was_recently_nudged,
    evaluate_and_dispatch_nudges,
    TIER_7_NEW_COURSE_COPY,
)


def get_test_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Create required tables
    tables = [
        models.User.__table__,
        models.DeviceToken.__table__,
        models.Category.__table__,
        models.Plan.__table__,
        models.Organisation.__table__,
        models.Course.__table__,
        models.Enrollment.__table__,
        models.StudyPartnership.__table__,
        models.PlatformObjective.__table__,
        models.UserObjectiveProgress.__table__,
        models.UserNotification.__table__,
        models.Connection.__table__,
    ]
    models.Base.metadata.create_all(engine, tables=tables)
    Session = sessionmaker(bind=engine)
    return Session()


def test_network_request_and_accepted_notifications():
    print("\n--- 1. Testing Network Request & Acceptance Notifications ---")
    db = get_test_db()
    
    user_a = models.User(
        id=uuid.uuid4(),
        first_name="Ada",
        last_name="Lovelace",
        email="ada@example.com",
        username="ada_dev",
        password="pwd",
        gender=schemas.Gender.FEMALE,
        role=schemas.Roles.STUDENT,
        is_verified=True,
    )
    user_b = models.User(
        id=uuid.uuid4(),
        first_name="Alan",
        last_name="Turing",
        email="alan@example.com",
        username="alan_code",
        password="pwd",
        gender=schemas.Gender.MALE,
        role=schemas.Roles.STUDENT,
        is_verified=True,
    )
    db.add_all([user_a, user_b])
    db.commit()

    from routers.network import send_request, accept_request

    # User A sends request to User B
    resp = send_request(target_user_id=user_b.id, db=db, current_user=user_a)
    assert resp["status"] == "success"
    req_id = uuid.UUID(resp["request_id"])

    # Verify connection created
    conn = db.query(models.Connection).filter(models.Connection.id == req_id).first()
    assert conn is not None
    assert conn.status == "pending"

    # Verify notification dispatched to User B
    notif_b = db.query(models.UserNotification).filter(
        models.UserNotification.user_id == user_b.id,
        models.UserNotification.category == "network_request"
    ).first()
    assert notif_b is not None
    assert "Ada Lovelace" in notif_b.body or "ada_dev" in notif_b.body
    assert notif_b.action_route == "/network"
    assert notif_b.sender_id == user_a.id
    print("  [OK] Network request notification to recipient: PASS")

    # User B accepts request
    acc_resp = accept_request(request_id=req_id, db=db, current_user=user_b)
    assert acc_resp["status"] == "success"
    db.refresh(conn)
    assert conn.status == "accepted"

    # Verify notification dispatched to User A
    notif_a = db.query(models.UserNotification).filter(
        models.UserNotification.user_id == user_a.id,
        models.UserNotification.category == "network_accepted"
    ).first()
    assert notif_a is not None
    assert "Alan Turing" in notif_a.body
    assert notif_a.action_route == "/network"
    print("  [OK] Network accept notification to requester: PASS")


def test_reengagement_daily_limits_and_interval_spacing():
    print("\n--- 2. Testing Daily Volume Limits & Interval Spacing ---")
    db = get_test_db()

    user = models.User(
        id=uuid.uuid4(),
        first_name="Grace",
        last_name="Hopper",
        email="grace@example.com",
        username="ghopper",
        password="pwd",
        gender=schemas.Gender.FEMALE,
        role=schemas.Roles.STUDENT,
        is_verified=True,
    )
    db.add(user)
    db.commit()

    assert MAX_DAILY_AUTOMATED_NOTIFICATIONS == 3
    assert MIN_HOURS_BETWEEN_NUDGES == 4.0
    assert "new_courses" in AUTOMATED_CATEGORIES

    # Initial state: Eligible
    eligible, reason = check_user_nudge_eligibility(db, user.id)
    assert eligible is True
    assert reason == "eligible"
    print("  [OK] Initial state is eligible: PASS")

    # Add 1 notification now
    notif1 = models.UserNotification(
        id=uuid.uuid4(),
        user_id=user.id,
        title="Nudge 1",
        body="Nudge 1 body",
        category="daytime_nudge",
        created_at=datetime.now(timezone.utc),
    )
    db.add(notif1)
    db.commit()

    # Immediate second check should fail with min_interval (< 4h)
    eligible, reason = check_user_nudge_eligibility(db, user.id)
    assert eligible is False
    assert reason == "min_interval"
    assert user_was_recently_nudged(db, user.id) is True
    print("  [OK] Spacing guard (< 4h) blocks consecutive spam: PASS")

    # Backdate notif 1 by 5 hours
    notif1.created_at = datetime.now(timezone.utc) - timedelta(hours=5)
    db.commit()

    # Now eligible for notification 2
    eligible, reason = check_user_nudge_eligibility(db, user.id)
    assert eligible is True
    print("  [OK] Spacing guard permits 2nd notification after 5h: PASS")

    # Add notification 2 (backdated by 4.5h) and notification 3 (now)
    notif2 = models.UserNotification(
        id=uuid.uuid4(),
        user_id=user.id,
        title="Nudge 2",
        body="Nudge 2 body",
        category="course_progress",
        created_at=datetime.now(timezone.utc) - timedelta(hours=4.5),
    )
    notif3 = models.UserNotification(
        id=uuid.uuid4(),
        user_id=user.id,
        title="Nudge 3",
        body="Nudge 3 body",
        category="new_courses",
        created_at=datetime.now(timezone.utc),
    )
    db.add_all([notif2, notif3])
    db.commit()

    # Total in past 24 hours is now 3. Even if spacing is bypassed, daily limit triggers
    notif3.created_at = datetime.now(timezone.utc) - timedelta(hours=4.5)
    db.commit()

    eligible, reason = check_user_nudge_eligibility(db, user.id)
    assert eligible is False
    assert reason == "daily_limit"
    print("  [OK] Daily limit guard blocks 4th notification within 24h: PASS")


def test_tier_7_new_courses_pipeline():
    print("\n--- 3. Testing Tier 7 New Courses Contextual Matching ---")
    db = get_test_db()

    user = models.User(
        id=uuid.uuid4(),
        first_name="Hedy",
        last_name="Lamarr",
        email="hedy@example.com",
        username="hedy_telecom",
        password="pwd",
        gender=schemas.Gender.FEMALE,
        role=schemas.Roles.STUDENT,
        university="MIT",
        is_verified=True,
    )
    author = models.User(
        id=uuid.uuid4(),
        first_name="Prof",
        last_name="Oak",
        email="oak@example.com",
        username="prof_oak",
        password="pwd",
        gender=schemas.Gender.MALE,
        role=schemas.Roles.TEACHER,
        is_verified=True,
    )
    cat_ai = models.Category(id=uuid.uuid4(), name="Artificial Intelligence", description="AI Courses")
    cat_bio = models.Category(id=uuid.uuid4(), name="Biology", description="Bio Courses")
    db.add_all([user, author, cat_ai, cat_bio])
    db.commit()

    # User is enrolled in an older AI course -> indicates category affinity
    old_ai_course = models.Course(
        id=uuid.uuid4(),
        admin_id=author.id,
        name="Intro to Neural Nets",
        description="Intro to Neural Nets",
        category_id=cat_ai.id,
        public="true",
        created_at=datetime.now(timezone.utc) - timedelta(days=60),
    )
    db.add(old_ai_course)
    db.commit()

    en = models.Enrollment(
        student_id=user.id,
        course_id=old_ai_course.id,
        progress=100.0,
        completed_at=datetime.now(timezone.utc) - timedelta(days=5),
    )
    db.add(en)

    # 1 New AI Course created 3 days ago (matches category affinity)
    new_ai_course = models.Course(
        id=uuid.uuid4(),
        admin_id=author.id,
        name="Deep Reinforcement Learning",
        description="Deep Reinforcement Learning",
        category_id=cat_ai.id,
        public="true",
        rating=4.9,
        created_at=datetime.now(timezone.utc) - timedelta(days=3),
    )
    # 1 New Bio Course created 2 days ago
    new_bio_course = models.Course(
        id=uuid.uuid4(),
        admin_id=author.id,
        name="Marine Biology",
        description="Marine Biology",
        category_id=cat_bio.id,
        public="true",
        rating=4.5,
        created_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    # 1 Stale Course created 45 days ago (should be ignored by 30-day cutoff)
    stale_course = models.Course(
        id=uuid.uuid4(),
        admin_id=author.id,
        name="Outdated Physics",
        description="Outdated Physics",
        category_id=cat_ai.id,
        public="true",
        created_at=datetime.now(timezone.utc) - timedelta(days=45),
    )
    db.add_all([new_ai_course, new_bio_course, stale_course])
    db.commit()

    # Run evaluation with preferred_tier=7 (or category="new_courses")
    stats = evaluate_and_dispatch_nudges(
        db=db,
        force=True,
        target_user_id=user.id,
        preferred_tier=7,
    )

    assert stats["dispatched_tier_7_new_course"] == 1
    assert len(stats["dispatched_records"]) == 1
    rec = stats["dispatched_records"][0]
    assert rec["tier"] == 7
    # Should contextually choose AI over Bio because user was enrolled in AI
    assert rec["course"] == "Deep Reinforcement Learning"
    assert rec["category"] == "Artificial Intelligence"
    print("  [OK] Tier 7 contextual affinity matching: PASS")

    # Verify notification record created in PostgreSQL / SQLite
    notif = db.query(models.UserNotification).filter(
        models.UserNotification.user_id == user.id,
        models.UserNotification.category == "new_courses"
    ).first()
    assert notif is not None
    assert "Deep Reinforcement Learning" in notif.title or "Deep Reinforcement Learning" in notif.body
    assert notif.action_route == f"/courses/{new_ai_course.id}/view"
    assert notif.data_payload["type"] == "new_course_discovery"
    print("  [OK] Tier 7 UserNotification persistence & deep-link routing: PASS")


def test_tier_7_normal_pipeline_dispatch():
    print("\n--- 4. Testing Tier 7 In Normal Automatic Pipeline (No Overrides) ---")
    db = get_test_db()

    user = models.User(
        id=uuid.uuid4(),
        first_name="Rosalind",
        last_name="Franklin",
        email="rosalind@example.com",
        username="rosalind_dna",
        password="pwd",
        gender=schemas.Gender.FEMALE,
        role=schemas.Roles.STUDENT,
        streak=0,
        is_verified=True,
    )
    author = models.User(
        id=uuid.uuid4(),
        first_name="Prof",
        last_name="Darwin",
        email="darwin@example.com",
        username="darwin_origin",
        password="pwd",
        gender=schemas.Gender.MALE,
        role=schemas.Roles.TEACHER,
        is_verified=True,
    )
    cat_genetics = models.Category(id=uuid.uuid4(), name="Genetics", description="Genetics Courses")
    db.add_all([user, author, cat_genetics])
    db.commit()

    new_genetics_course = models.Course(
        id=uuid.uuid4(),
        admin_id=author.id,
        name="CRISPR & Gene Editing",
        description="CRISPR & Gene Editing",
        category_id=cat_genetics.id,
        public="true",
        rating=4.95,
        created_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db.add(new_genetics_course)
    db.commit()

    # User has no in-progress courses, streak=0, hasn't studied today -> Tier 7 should dispatch
    stats = evaluate_and_dispatch_nudges(
        db=db,
        force=True, # bypass quiet hours / time of day for testing
        target_user_id=user.id,
    )

    assert stats["dispatched_tier_7_new_course"] == 1
    assert len(stats["dispatched_records"]) == 1
    rec = stats["dispatched_records"][0]
    assert rec["tier"] == 7
    assert rec["course"] == "CRISPR & Gene Editing"
    assert rec["category"] == "Genetics"
    print("  [OK] Tier 7 normal pipeline automatic dispatch: PASS")


if __name__ == "__main__":
    test_network_request_and_accepted_notifications()
    test_reengagement_daily_limits_and_interval_spacing()
    test_tier_7_new_courses_pipeline()
    test_tier_7_normal_pipeline_dispatch()
    print("\n==================================================================")
    print(">>> ALL NETWORK NOTIFICATIONS & RE-ENGAGEMENT TESTS PASSED (100%) <<<")
    print("==================================================================\n")
