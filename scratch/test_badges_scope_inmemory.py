import sys
import os
import uuid
from datetime import datetime, timezone, timedelta
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from database import Base
import models
import monetization_models
from services import promo_service, ambassador_service

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

@compiles(ARRAY, "sqlite")
def compile_array(element, compiler, **kw):
    return "TEXT"

@compiles(JSONB, "sqlite")
def compile_jsonb(element, compiler, **kw):
    return "JSON"

def run_tests():
    print("[TEST] Initializing in-memory SQLite database...")
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    print("[TEST] 1. Testing Campus Ambassador Account-First Lookup Invariant...")
    # Attempt to invite an unregistered email
    try:
        ambassador_service.invite_campus_ambassador(
            db=db,
            email="nonexistent.student@unilag.edu.ng",
            university="University of Lagos (UNILAG)",
            notes="Should fail",
        )
        assert False, "Expected HTTPException 400 for unregistered email"
    except HTTPException as ex:
        assert ex.status_code == 400
        assert "No registered Nu-Age account found with email" in ex.detail
        print(" -> Successfully rejected unregistered candidate email.")

    # Create a registered user
    user_unilag = models.User(
        id=uuid.uuid4(),
        email="adebayo@unilag.edu.ng",
        first_name="Chukwuemeka",
        last_name="Adebayo",
        username="adebayo_unilag",
        password="mockhash",
        role=models.Roles.STUDENT,
        gender=models.Gender.MALE,
        university="University of Lagos (UNILAG)",
    )
    user_ui = models.User(
        id=uuid.uuid4(),
        email="fatima@ui.edu.ng",
        first_name="Fatima",
        last_name="Bello",
        username="fatima_ui",
        password="mockhash",
        role=models.Roles.STUDENT,
        gender=models.Gender.FEMALE,
        university="University of Ibadan (UI)",
    )
    db.add_all([user_unilag, user_ui])
    db.commit()

    # Invite registered user
    amb = ambassador_service.invite_campus_ambassador(
        db=db,
        email="adebayo@unilag.edu.ng",
        university="University of Lagos (UNILAG)",
        notes="Faculty of Science lead",
    )
    assert amb.user_id == user_unilag.id
    assert amb.status == "invited"
    assert amb.user.first_name == "Chukwuemeka"
    print(" -> Successfully linked registered learner to ambassador invitation.")

    print("[TEST] 2. Testing Badges CRUD & Cascading Invariants...")
    # Create badge
    custom_badge = models.MemberBadge(
        id=uuid.uuid4(),
        slug="campus_vip",
        name="Campus VIP",
        description="Exclusive badge for campus orientation participants",
        icon="WORKSPACE_PREMIUM_ROUNDED",
        color_hex="#8B5CF6",
        is_automated=False,
    )
    db.add(custom_badge)
    db.commit()
    db.refresh(custom_badge)
    assert custom_badge.slug == "campus_vip"

    # Assign badge to user
    ub = models.UserBadge(
        id=uuid.uuid4(),
        user_id=user_unilag.id,
        badge_id=custom_badge.id,
        is_primary=True,
    )
    db.add(ub)
    db.commit()
    assert db.query(models.UserBadge).filter(models.UserBadge.badge_id == custom_badge.id).count() == 1

    # Update badge
    custom_badge.name = "Campus VIP Elite"
    custom_badge.color_hex = "#7C3AED"
    db.commit()
    db.refresh(custom_badge)
    assert custom_badge.name == "Campus VIP Elite"
    assert custom_badge.color_hex == "#7C3AED"
    print(" -> Successfully created and updated badge.")

    # Delete badge with cascade user badge cleanup
    db.query(models.UserBadge).filter(models.UserBadge.badge_id == custom_badge.id).delete(synchronize_session=False)
    db.delete(custom_badge)
    db.commit()
    assert db.query(models.MemberBadge).filter(models.MemberBadge.slug == "campus_vip").first() is None
    assert db.query(models.UserBadge).filter(models.UserBadge.badge_id == custom_badge.id).count() == 0
    print(" -> Successfully deleted badge and cascaded user awards.")

    print("[TEST] 3. Testing University-Scoped Promo Code Redemption & Pro Trial...")
    # Re-seed default badges
    promo_service.seed_default_badges_if_empty(db)

    # Create promo code scoped to UNILAG
    scoped_promo = models.PromoCode(
        id=uuid.uuid4(),
        code="UNILAG2026",
        title="UNILAG Orientation Drop",
        description="Exclusive welcome bundle for UNILAG students",
        reward_coins=250,
        reward_ai_credits=10,
        reward_trial_days=14,
        reward_badge_slug="beta_tester",
        scoped_university="University of Lagos (UNILAG)",
        is_active=True,
    )
    db.add(scoped_promo)
    db.commit()

    # User from UI tries to redeem
    try:
        promo_service.redeem_promo_code(db=db, user_id=user_ui.id, code_str="UNILAG2026")
        assert False, "Expected HTTP 400 for university mismatch"
    except HTTPException as ex:
        assert ex.status_code == 400
        assert "exclusively reserved for students of University of Lagos (UNILAG)" in ex.detail
        print(" -> Successfully blocked redemption from non-matching university.")

    # User from UNILAG redeems
    res = promo_service.redeem_promo_code(db=db, user_id=user_unilag.id, code_str="unilag2026")
    assert res["status"] == "success"
    rewards = res["rewards"]
    assert rewards["coins"] == 250
    assert rewards["ai_credits"] == 10
    assert rewards["trial"]["plan_id"] == "pro"
    assert rewards["trial"]["trial_days"] == 14
    assert rewards["badge"]["slug"] == "beta_tester"
    print(" -> Successfully redeemed promo code for matching university with full multi-reward payload.")

    # Verify Pro trial in database
    sub = db.query(models.UserSubscription).filter(models.UserSubscription.user_id == user_unilag.id).first()
    assert sub is not None
    assert sub.plan_id == "pro"
    assert sub.status == "active"
    assert sub.billing_cycle == "trial"
    assert sub.bonus_generations == 10
    print(" -> Verified Pro trial and bonus generations persistence.")

    # Verify idempotency
    try:
        promo_service.redeem_promo_code(db=db, user_id=user_unilag.id, code_str="UNILAG2026")
        assert False, "Expected HTTP 400 on duplicate redemption"
    except HTTPException as ex:
        assert ex.status_code == 400
        assert "already redeemed" in ex.detail
        print(" -> Verified duplicate redemption is rejected.")

    print("\n[SUCCESS] ALL IN-MEMORY INTEGRATION TESTS PASSED!")

if __name__ == "__main__":
    run_tests()
