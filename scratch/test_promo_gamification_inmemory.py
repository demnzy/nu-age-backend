import os
import sys
import uuid
from datetime import datetime, timezone, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Setup in-memory SQLite test database
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
sys.path.insert(0, os.path.abspath("."))

from database import Base
import models
import monetization_models
from fastapi import HTTPException
from services.promo_service import (
    redeem_promo_code,
    evaluate_rank_badges,
    _apply_pro_trial,
    seed_default_badges_if_empty,
)
from services.ambassador_service import (
    invite_campus_ambassador,
    accept_ambassador_invitation,
    list_campus_ambassadors,
    toggle_ambassador_status,
)
from services.coin_service import grant_coins, get_user_coins

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

@compiles(ARRAY, "sqlite")
def compile_array(element, compiler, **kw):
    return "TEXT"

@compiles(JSONB, "sqlite")
def compile_jsonb(element, compiler, **kw):
    return "JSON"

engine = create_engine("sqlite:///:memory:", echo=False)
Base.metadata.create_all(bind=engine)
SessionLocal = sessionmaker(bind=engine)

def run_tests():
    db = SessionLocal()
    print("=== Starting In-Memory Promo & Gamification Tests ===")

    # 1. Create Test Users
    ambassador_user = models.User(
        id=uuid.uuid4(),
        email="ambassador@unilag.edu.ng",
        username="unilag_rep",
        first_name="Ada",
        last_name="Okonkwo",
        gender=models.Gender.FEMALE,
        password="hashed_pw",
        role=models.Roles.STUDENT,
    )
    student_user = models.User(
        id=uuid.uuid4(),
        email="student@unilag.edu.ng",
        username="freshman_ada",
        first_name="David",
        last_name="Ade",
        gender=models.Gender.MALE,
        password="hashed_pw",
        role=models.Roles.STUDENT,
    )
    db.add_all([ambassador_user, student_user])
    db.commit()

    # 2. Seed Default Badges
    seed_default_badges_if_empty(db)
    print("[PASS] Default badges seeded")

    # 3. Test Ambassador Invite with Assigned Promo Code
    # First create promo code
    promo = models.PromoCode(
        id=uuid.uuid4(),
        code="UNILAG2026",
        title="Unilag Welcome Drop",
        description="Orientation pack with bonus coins & AI generations",
        reward_coins=50,
        reward_ai_credits=10,  # Must map to bonus_generations!
        reward_trial_days=7,
        reward_badge_slug="campus_ambassador",
        ambassador_coin_reward=25,
        max_uses=100,
        is_active=True,
    )
    db.add(promo)
    db.commit()

    # Invite ambassador assigning this promo code
    amb_record = invite_campus_ambassador(
        db=db,
        email="ambassador@unilag.edu.ng",
        university="University of Lagos",
        notes="Faculty Lead",
        assigned_promo_code_id=promo.id,
    )
    assert amb_record is not None, "Ambassador invitation record should be created"
    assert amb_record.assigned_promo_code_id == promo.id, "Assigned promo code ID must match"
    print("[PASS] Ambassador invitation created with assigned promo code ID")

    # Accept invitation
    accept_res = accept_ambassador_invitation(
        db=db,
        token=amb_record.invite_token,
        user_id=ambassador_user.id,
    )
    assert accept_res["status"] == "success", "Acceptance should return status=success"
    db.refresh(amb_record)
    assert amb_record.status == "active", "Ambassador status should be active upon accept"
    db.refresh(promo)
    assert promo.ambassador_id == ambassador_user.id, "Promo code should be linked to ambassador user"
    print("[PASS] Ambassador invitation accepted and promo code linked to ambassador_user")

    # 4. Test Promo Code Redemption by Student
    redemption_res = redeem_promo_code(
        db=db,
        user_id=student_user.id,
        code_str="UNILAG2026",
    )
    assert redemption_res["status"] == "success", f"Redemption failed: {redemption_res}"
    print(f"[PASS] Promo code redeemed successfully! Rewards: {redemption_res['rewards']}")

    # Verify Coins (Initial default wallet is 100 coins + 50 promo reward = 150)
    student_coins = get_user_coins(db, student_user.id)
    assert student_coins == 150, f"Expected 150 coins for student (100 init + 50), got {student_coins}"
    print("[PASS] Student awarded 50 Nu-Coins (total 150)")

    # Verify Ambassador Referral Kickback (100 init + 25 referral kickback = 125)
    amb_coins = get_user_coins(db, ambassador_user.id)
    assert amb_coins == 125, f"Expected 125 referral coins for ambassador (100 init + 25), got {amb_coins}"
    print("[PASS] Ambassador awarded 25 Nu-Coins referral bonus (total 125)")

    # Verify AI Credits mapped strictly to bonus_generations (NOT mixed with coin wallet)
    sub = db.query(models.UserSubscription).filter_by(user_id=student_user.id).first()
    assert sub is not None, "Subscription record should be created"
    assert sub.bonus_generations == 10, f"Expected 10 bonus generations, got {sub.bonus_generations}"
    assert sub.billing_cycle == "trial", f"Expected trial billing cycle, got {sub.billing_cycle}"
    assert sub.plan_id == "pro", f"Expected pro plan for trial, got {sub.plan_id}"
    print("[PASS] AI credits verified to map strictly to bonus_generations (10) and pro trial activated")

    # Verify Badge Awarded
    awarded_badge = db.query(models.UserBadge).join(models.MemberBadge).filter(
        models.UserBadge.user_id == student_user.id,
        models.MemberBadge.slug == "campus_ambassador",
    ).first()
    assert awarded_badge is not None, "Badge should be awarded to student"
    print("[PASS] Honor badge awarded to student")

    # 5. Verify Prevention of Double Redemption
    try:
        double_res = redeem_promo_code(
            db=db,
            user_id=student_user.id,
            code_str="UNILAG2026",
        )
        assert False, f"Expected HTTPException for double redeem, got {double_res}"
    except HTTPException as e:
        assert e.status_code == 400
        print(f"[PASS] Re-redemption correctly blocked: {e.detail}")

    # 6. Verify Ambassador Referral Earnings and Listing
    amb_list = list_campus_ambassadors(db)
    assert len(amb_list) == 1, f"Expected 1 ambassador, got {len(amb_list)}"
    amb_info = amb_list[0]
    assert amb_info["students_referred_count"] == 1, f"Expected 1 referral, got {amb_info['students_referred_count']}"
    assert amb_info["total_coins_earned"] == 25, f"Expected 25 coins earned, got {amb_info['total_coins_earned']}"
    assert amb_info["status"] == "active", f"Expected active status, got {amb_info['status']}"
    print(f"[PASS] Ambassador list metrics verified: {amb_info['students_referred_count']} referral(s), {amb_info['total_coins_earned']} coins earned")

    # 7. Test Ambassador Suspension and Non-Awarding of Kickback
    toggle_ambassador_status(db, amb_record.id, "suspended")
    db.refresh(amb_record)
    assert amb_record.status == "suspended", "Ambassador should be suspended"
    
    # Create another student and redeem another code for this ambassador
    student2 = models.User(
        id=uuid.uuid4(),
        first_name="Chioma",
        last_name="Eze",
        email="student2@unilag.edu.ng",
        username="student2",
        gender=models.Gender.FEMALE,
        password="hashed_pw",
        role=models.Roles.STUDENT,
    )
    db.add(student2)
    db.commit()

    promo2 = models.PromoCode(
        id=uuid.uuid4(),
        code="UNILAG_SUSPENDED",
        title="Suspended Drop",
        ambassador_id=ambassador_user.id,
        reward_coins=20,
        ambassador_coin_reward=15,
        is_active=True,
    )
    db.add(promo2)
    db.commit()

    redeem_promo_code(db=db, user_id=student2.id, code_str="UNILAG_SUSPENDED")
    amb_coins_after = get_user_coins(db, ambassador_user.id)
    assert amb_coins_after == 125, f"Suspended ambassador should NOT receive kickback! Still 125, got {amb_coins_after}"
    print("[PASS] Suspended ambassador correctly excluded from referral kickback")

    # 8. Test Rank Badge Evaluation (streak_king)
    # Set student2 to top streak
    student2.streak = 45
    db.commit()

    evaluate_rank_badges(db, force=True)
    print("[PASS] Rank badge evaluated with force=True")
    king_badge = db.query(models.UserBadge).join(models.MemberBadge).filter(
        models.UserBadge.user_id == student2.id,
        models.MemberBadge.slug == "streak_king",
    ).first()
    assert king_badge is not None, "Student2 should hold streak_king badge"
    print("[PASS] streak_king badge awarded to top streak holder")

    db.close()
    print("=== All In-Memory Tests Passed Successfully! ===")

if __name__ == "__main__":
    run_tests()
