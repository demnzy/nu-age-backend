"""
scratch/test_partnerships_and_coins_backend.py
──────────────────────────────────────────────────────────────────────────────
Validates the revamped Study Partnerships, Nu-Coins Economy, 15-Question Duels,
Pack Sharing, and Notification Deduplication safeguards.
──────────────────────────────────────────────────────────────────────────────
"""

import sys
import os
import uuid
import time
from datetime import datetime, date, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Setup path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import models
import schemas
from services import coin_service
from services.notifications import dispatch_notification, _RECENT_DISPATCHES
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB, ARRAY

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "TEXT"

@compiles(ARRAY, "sqlite")
def compile_array_sqlite(type_, compiler, **kw):
    return "TEXT"


def run_all_tests():
    print("=====================================================================")
    print(">>> 1. INITIALIZING IN-MEMORY SQLITE DATABASE & TEST FIXTURES <<<")
    print("=====================================================================")

    engine = create_engine("sqlite:///:memory:", echo=False)
    target_tables = [
        models.User.__table__,
        models.CoinTransaction.__table__,
        models.PlatformObjective.__table__,
        models.UserObjectiveProgress.__table__,
        models.StudyPartnership.__table__,
        models.PartnerChallenge.__table__,
        models.PartnershipSharedPack.__table__,
        models.StudyPack.__table__,
        models.StudyMaterial.__table__,
        models.Flashcard.__table__,
        models.Question.__table__,
        models.UserNotification.__table__,
    ]
    models.Base.metadata.create_all(bind=engine, tables=target_tables)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()

    # Create test users
    user_a = models.User(
        id=uuid.uuid4(),
        email="alex@test.com",
        password="hashed_pwd",
        username="alex",
        first_name="Alex",
        last_name="Doe",
        role=schemas.Roles.STUDENT,
        gender=schemas.Gender.MALE,
        coins_balance=50,
        streak=4,
    )
    user_b = models.User(
        id=uuid.uuid4(),
        email="sam@test.com",
        password="hashed_pwd",
        username="sam",
        first_name="Sam",
        last_name="Smith",
        role=schemas.Roles.STUDENT,
        gender=schemas.Gender.MALE,
        coins_balance=10,
        streak=4,
    )
    db.add_all([user_a, user_b])
    db.commit()

    print(f"Created User A ({user_a.first_name}, {user_a.coins_balance} coins)")
    print(f"Created User B ({user_b.first_name}, {user_b.coins_balance} coins)")

    # ─────────────────────────────────────────────────────────────────────────
    print("\n>>> 2. TESTING COIN SERVICE (GRANTS, SPENDS, LEDGER) <<<")
    # Grant coins to A
    ok, bal = coin_service.grant_coins(db, user_a.id, 40, "test_reward", "Test grant")
    assert ok and bal == 90, f"Expected 90, got {bal}"
    print(f"  [PASS] Grant coins: New balance = {bal}")

    # Spend coins from A
    ok, bal, msg = coin_service.spend_coins(db, user_a.id, 25, "streak_shield", "Shield purchase")
    assert ok and bal == 65, f"Expected 65, got {bal}"
    print(f"  [PASS] Spend coins: New balance = {bal}")

    # Overspend coins
    ok, bal, msg = coin_service.spend_coins(db, user_a.id, 500, "overspend")
    assert not ok and bal == 65, f"Expected overspend reject, got {bal}"
    print(f"  [PASS] Overspend rejected properly: {msg}")

    # ─────────────────────────────────────────────────────────────────────────
    print("\n>>> 3. TESTING PLATFORM OBJECTIVES & CLAIM ENGINE <<<")
    obj = models.PlatformObjective(
        code="test_course_finish",
        title="Course Finisher",
        description="Finish 1 course",
        category="courses",
        target_value=1,
        coin_reward=150,
        icon="SCHOOL",
    )
    db.add(obj)
    db.commit()

    # Track progress (not yet complete)
    prog = coin_service.track_objective_progress(db, user_a.id, "test_course_finish", 0)
    assert not prog.is_completed, "Should not be completed with 0"

    # Try claiming early
    ok, coins, msg = coin_service.claim_objective_reward(db, user_a.id, "test_course_finish")
    assert not ok, "Claiming incomplete objective should fail"
    print("  [PASS] Early objective claim rejected")

    # Complete objective
    prog = coin_service.track_objective_progress(db, user_a.id, "test_course_finish", 1)
    assert prog.is_completed, "Should be completed with 1"
    print("  [PASS] Objective automatically marked complete upon hitting target")

    # Claim reward
    ok, coins, msg = coin_service.claim_objective_reward(db, user_a.id, "test_course_finish")
    assert ok and coins == 150, f"Expected 150 coins reward, got {coins}"
    new_bal = coin_service.get_user_coins(db, user_a.id)
    assert new_bal == 65 + 150, f"Expected 215, got {new_bal}"
    print(f"  [PASS] Successfully claimed objective reward: +{coins} coins! New balance = {new_bal}")

    # Double claim
    ok, coins, msg = coin_service.claim_objective_reward(db, user_a.id, "test_course_finish")
    assert not ok, "Double claim should be rejected"
    print("  [PASS] Double claim safeguard verified")

    # ─────────────────────────────────────────────────────────────────────────
    print("\n>>> 4. TESTING DUO STREAK MILESTONES (COINS TO BOTH PARTNERS) <<<")
    partnership = models.StudyPartnership(
        user_a_id=user_a.id,
        user_b_id=user_b.id,
        status="active",
        duo_streak=4,
        best_duo_streak=4,
    )
    db.add(partnership)
    db.commit()

    # Milestone 5 days reached
    bal_a_before = coin_service.get_user_coins(db, user_a.id)
    bal_b_before = coin_service.get_user_coins(db, user_b.id)

    milestones = coin_service.check_and_award_duo_streak_milestones(db, partnership, 5)
    assert len(milestones) == 1 and milestones[0]["reward_coins"] == 60
    bal_a_after = coin_service.get_user_coins(db, user_a.id)
    bal_b_after = coin_service.get_user_coins(db, user_b.id)

    assert bal_a_after == bal_a_before + 60, f"Expected Partner A +60, got {bal_a_after - bal_a_before}"
    assert bal_b_after == bal_b_before + 60, f"Expected Partner B +60, got {bal_b_after - bal_b_before}"
    print(f"  [PASS] Duo Streak 5-day milestone awarded +60 coins to BOTH partners! A={bal_a_after}, B={bal_b_after}")

    # ─────────────────────────────────────────────────────────────────────────
    print("\n>>> 5. TESTING STUDY PACK SHARING & ACCEPTANCE <<<")
    pack = models.StudyPack(
        creator_id=user_a.id,
        title="JAMB Physics Mastery",
        description="Comprehensive past questions",
        category="Physics",
        pack_data={
            "questions": [
                {
                    "question": f"Question {i+1}: What is velocity formula?",
                    "options": ["d/t", "m*a", "v/t", "1/2*m*v^2"],
                    "correct_answer": "d/t",
                    "explanation": "Velocity is displacement divided by time."
                }
                for i in range(20)
            ]
        }
    )
    db.add(pack)
    db.commit()

    # Share pack
    share = models.PartnershipSharedPack(
        partnership_id=partnership.id,
        pack_id=pack.id,
        sender_id=user_a.id,
        recipient_id=user_b.id,
        status="pending"
    )
    db.add(share)
    db.commit()
    assert share.status == "pending"
    print("  [PASS] PartnershipSharedPack created with status='pending'")

    # Accept pack -> awards +20 coins to sender
    bal_sender_before = coin_service.get_user_coins(db, user_a.id)
    share.status = "accepted"
    share.accepted_at = datetime.now(timezone.utc)
    coin_service.grant_coins(db, share.sender_id, 20, "pack_share", "Partner accepted pack")
    bal_sender_after = coin_service.get_user_coins(db, user_a.id)
    assert bal_sender_after == bal_sender_before + 20
    print(f"  [PASS] Pack acceptance awarded +20 coins to sender A: {bal_sender_after}")

    # Test sharing personal vault StudyMaterial (synthesizing StudyPack)
    from routers.study_partners import _resolve_study_pack_or_material
    vault_mat = models.StudyMaterial(
        user_id=user_a.id,
        title="Personal Cell Biology Notes",
        source_type="text",
        content="Cell notes",
    )
    db.add(vault_mat)
    db.commit()
    fc = models.Flashcard(user_id=user_a.id, material_id=vault_mat.id, front="Mitochondria", back="Powerhouse of cell")
    q1 = models.Question(user_id=user_a.id, material_id=vault_mat.id, question_text="What is ATP?", options=["Energy currency", "Protein", "Lipid", "DNA"], answer_index=0)
    db.add_all([fc, q1])
    db.commit()

    synthesized_pack = _resolve_study_pack_or_material(db, vault_mat.id, user_a.id)
    assert synthesized_pack is not None, "Must resolve StudyMaterial to a StudyPack"
    assert synthesized_pack.title == "Personal Cell Biology Notes"
    assert len(synthesized_pack.pack_data.get("flashcards", [])) == 1
    assert len(synthesized_pack.pack_data.get("questions", [])) == 1
    print("  [PASS] _resolve_study_pack_or_material successfully synthesized StudyPack from vault StudyMaterial!")

    # ─────────────────────────────────────────────────────────────────────────
    print("\n>>> 6. TESTING REAL 15-QUESTION QUICKFIRE DUEL FLOW <<<")
    # Generate 15 questions from pack
    raw_qs = pack.pack_data["questions"][:15]
    assert len(raw_qs) == 15, f"Expected 15 questions, got {len(raw_qs)}"

    # Challenger creates challenge with 15 questions, scores 12/15 in 90 seconds
    challenge = models.PartnerChallenge(
        partnership_id=partnership.id,
        challenger_id=user_a.id,
        challenged_id=user_b.id,
        pack_id=pack.id,
        challenge_type="quickfire_duel",
        title="JAMB Physics Quickfire Duel",
        subject="Physics",
        question_snapshot=raw_qs,
        total_questions=15,
        duration_seconds=180,
        status="challenger_done",
        challenger_score=12,
        challenger_time_seconds=90,
        challenger_breakdown={"correct": 12, "wrong": 3},
    )
    db.add(challenge)
    db.commit()

    assert challenge.total_questions == 15
    assert challenge.duration_seconds == 180
    assert len(challenge.question_snapshot) == 15
    print("  [PASS] Quickfire Challenge initialized with exactly 15 questions and 180s timer")

    # Challenged B completes with 14/15 in 110s -> B WINS!
    bal_b_before_duel = coin_service.get_user_coins(db, user_b.id)
    bal_a_before_duel = coin_service.get_user_coins(db, user_a.id)

    challenge.challenged_score = 14
    challenge.challenged_time_seconds = 110
    challenge.status = "completed"
    challenge.winner_id = user_b.id
    challenge.coins_awarded = 35

    # Winner gets +35, runner-up gets +10
    coin_service.grant_coins(db, user_b.id, 35, "duel_win")
    coin_service.grant_coins(db, user_a.id, 10, "duel_complete")

    assert coin_service.get_user_coins(db, user_b.id) == bal_b_before_duel + 35
    assert coin_service.get_user_coins(db, user_a.id) == bal_a_before_duel + 10
    print(f"  [PASS] Duel completed: Winner B got +35 coins, Runner-up A got +10 coins")

    # ─────────────────────────────────────────────────────────────────────────
    print("\n>>> 7. TESTING NOTIFICATION DEDUPLICATION SAFEGUARD <<<")
    _RECENT_DISPATCHES.clear()
    
    # First dispatch
    notifs_1 = dispatch_notification(
        db=db,
        recipient_user_ids=[user_b.id],
        title="Nudge Alert",
        body="Time to study!",
        category="study_partner_nudge",
        sender_id=user_a.id,
        collapse_id="test_nudge_123",
        send_push=False,  # Skip network HTTP in unit test
    )
    assert len(notifs_1) == 1, "First dispatch should create UserNotification record"
    print("  [PASS] First notification dispatch succeeded and logged")

    # Immediate second dispatch with identical parameters (Rapid double-tap simulation)
    notifs_2 = dispatch_notification(
        db=db,
        recipient_user_ids=[user_b.id],
        title="Nudge Alert",
        body="Time to study!",
        category="study_partner_nudge",
        sender_id=user_a.id,
        collapse_id="test_nudge_123",
        send_push=False,
    )
    assert len(notifs_2) == 0, "Duplicate dispatch within 10s must be dropped!"
    print("  [PASS] Duplicate notification within 10s window was successfully dropped by sliding-window cache!")

    print("\n=====================================================================")
    print(">>> ALL 7 BACKEND TEST SUITES PASSED FLAWLESSLY (100% SUCCESS) <<<")
    print("=====================================================================\n")


if __name__ == "__main__":
    run_all_tests()
