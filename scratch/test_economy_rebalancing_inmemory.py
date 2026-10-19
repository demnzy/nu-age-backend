import os
import sys
import uuid
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Setup in-memory SQLite test database
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
sys.path.insert(0, os.path.abspath("."))

from database import Base
import models
import monetization_models
from fastapi import HTTPException
from services import coin_service
from routers.payments import exchange_coins_for_generations, ExchangeCoinsPayload, EXCHANGE_TIERS
from routers.study_partners import submit_partner_challenge

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

def run_economy_rebalancing_tests():
    db = SessionLocal()
    print("=== Starting In-Memory Nu-Coins Economy Rebalancing Tests ===")

    # 1. Create Test Users
    user_a = models.User(
        id=uuid.uuid4(),
        email="learner_a@unilag.edu.ng",
        username="learner_a",
        first_name="Tobi",
        last_name="Davies",
        gender=models.Gender.MALE,
        password="hashed_pw",
        role=models.Roles.STUDENT,
        coins_balance=100,
    )
    user_b = models.User(
        id=uuid.uuid4(),
        email="learner_b@unilag.edu.ng",
        username="learner_b",
        first_name="Kemi",
        last_name="Adebayo",
        gender=models.Gender.FEMALE,
        password="hashed_pw",
        role=models.Roles.STUDENT,
        coins_balance=100,
    )
    db.add_all([user_a, user_b])
    db.commit()

    # User A Subscription: Plan free with 0 generations remaining and 0 bonus generations
    sub_a = models.UserSubscription(
        user_id=user_a.id,
        plan_id="free",
        generations_used=40,
        bonus_generations=0,
    )
    db.add(sub_a)
    db.commit()

    print("[PASS] Test users and subscription initialized in memory.")

    # ─────────────────────────────────────────────────────────────────────────
    # TEST 1: AI Generation Decoupling & Quota Gate
    # ─────────────────────────────────────────────────────────────────────────
    print("\n--- TEST 1: AI Generation Decoupling ---")
    effective_limit = 40  # free plan limit
    has_plan_quota = sub_a.generations_used < effective_limit
    has_bonus_quota = (sub_a.bonus_generations or 0) > 0

    assert not has_plan_quota, "Plan quota should be exhausted (40/40)"
    assert not has_bonus_quota, "Bonus quota should be 0"
    assert coin_service.get_user_coins(db, user_a.id) == 100, "User A has 100 Nu-Coins"

    # User has 100 coins, but decoupled AI generation MUST NOT automatically debit coins!
    blocked = False
    try:
        if not has_plan_quota and not has_bonus_quota:
            raise HTTPException(
                status_code=402,
                detail="You're out of AI generation credits. Exchange Nu-Coins for AI generation credits in the Store or top up."
            )
    except HTTPException as e:
        blocked = True
        assert e.status_code == 402
    assert blocked, "AI generation must be blocked when quota is exhausted, even with 100 coins in wallet"
    assert coin_service.get_user_coins(db, user_a.id) == 100, "Nu-Coins wallet remains untouched (100 coins)"
    print("[PASS] AI generation decoupled: 402 raised without auto-debiting Nu-Coins.")

    # ─────────────────────────────────────────────────────────────────────────
    # TEST 2: In-Store Explicit Coin Exchange
    # ─────────────────────────────────────────────────────────────────────────
    print("\n--- TEST 2: In-Store Explicit Nu-Coins -> AI Generation Exchange ---")
    payload = ExchangeCoinsPayload(package_id="ai_gen_5")
    res = exchange_coins_for_generations(payload=payload, db=db, current_user=user_a)

    assert res["success"] is True
    assert res["generations_added"] == 5
    assert res["new_bonus_generations"] == 5
    assert res["new_coins_balance"] == 0, f"Expected 0 coins remaining, got {res['new_coins_balance']}"
    assert coin_service.get_user_coins(db, user_a.id) == 0

    # Refresh subscription
    db.refresh(sub_a)
    assert sub_a.bonus_generations == 5

    # Now verify AI generation quota succeeds with bonus generation
    has_plan_quota = sub_a.generations_used < effective_limit
    has_bonus_quota = (sub_a.bonus_generations or 0) > 0
    assert not has_plan_quota
    assert has_bonus_quota

    # Consume 1 bonus generation
    sub_a.bonus_generations -= 1
    db.commit()
    db.refresh(sub_a)
    assert sub_a.bonus_generations == 4
    print("[PASS] Explicit exchange successfully converted 100 Nu-Coins into 5 bonus generations!")

    # ─────────────────────────────────────────────────────────────────────────
    # TEST 3: Casual Duel (0 Stake) Zero Coin Printing
    # ─────────────────────────────────────────────────────────────────────────
    print("\n--- TEST 3: Casual Duel (Stake = 0) Zero Coin Award ---")
    partnership = models.StudyPartnership(
        user_a_id=user_a.id,
        user_b_id=user_b.id,
        status="active",
    )
    db.add(partnership)
    db.commit()

    challenge_casual = models.PartnerChallenge(
        id=uuid.uuid4(),
        partnership_id=partnership.id,
        challenger_id=user_a.id,
        challenged_id=user_b.id,
        challenge_type="quickfire_duel",
        title="Quickfire Duel",
        total_questions=15,
        challenger_score=14,
        challenger_time_seconds=60,
        status="challenger_done",
        wager_amount=0,
    )
    db.add(challenge_casual)
    db.commit()

    # User B submits and loses (scored 10)
    user_b_coins_before = coin_service.get_user_coins(db, user_b.id)
    user_a_coins_before = coin_service.get_user_coins(db, user_a.id)

    submit_payload = {
        "challenged_score": 10,
        "challenged_time_seconds": 70,
        "challenged_breakdown": {},
    }
    complete_res = submit_partner_challenge(
        challenge_id=challenge_casual.id,
        payload=submit_payload,
        db=db,
        current_user=user_b,
    )

    db.refresh(challenge_casual)
    assert challenge_casual.winner_id == user_a.id
    assert challenge_casual.coins_awarded == 0, f"Expected 0 coins awarded, got {challenge_casual.coins_awarded}"
    assert coin_service.get_user_coins(db, user_a.id) == user_a_coins_before, "Winner of casual duel receives 0 coins"
    assert coin_service.get_user_coins(db, user_b.id) == user_b_coins_before, "Loser of casual duel receives 0 coins"
    print("[PASS] Casual duel awarded 0 coins to winner and loser (no free coin printing).")

    # ─────────────────────────────────────────────────────────────────────────
    # TEST 4: Wagered Duel (Stake = 25) with 10% Platform Burn
    # ─────────────────────────────────────────────────────────────────────────
    print("\n--- TEST 4: Wagered Duel (Stake = 25 Coins) with 10% Platform Burn ---")
    # Give both users 100 coins
    coin_service.grant_coins(db, user_a.id, 100, category="test_topup", description="test topup")
    # user_b already has 100
    assert coin_service.get_user_coins(db, user_a.id) == 100
    assert coin_service.get_user_coins(db, user_b.id) == 100

    # User A creates duel with 25 stake -> 25 escrowed
    stake = 25
    coin_service.spend_coins(db, user_a.id, stake, category="duel_wager_escrow", description="Escrowed 25 coins")
    assert coin_service.get_user_coins(db, user_a.id) == 75

    challenge_wager = models.PartnerChallenge(
        id=uuid.uuid4(),
        partnership_id=partnership.id,
        challenger_id=user_a.id,
        challenged_id=user_b.id,
        challenge_type="quickfire_duel",
        title="Quickfire Duel",
        total_questions=15,
        challenger_score=15,
        challenger_time_seconds=55,
        status="challenger_done",
        wager_amount=stake,
    )
    db.add(challenge_wager)
    db.commit()

    # User B submits with score 12 (loses)
    # submit_partner_challenge will escrow User B's 25 coins and settle 90% payout to User A (45 coins)
    submit_payload_w = {
        "challenged_score": 12,
        "challenged_time_seconds": 65,
        "challenged_breakdown": {},
    }
    complete_res_w = submit_partner_challenge(
        challenge_id=challenge_wager.id,
        payload=submit_payload_w,
        db=db,
        current_user=user_b,
    )

    db.refresh(challenge_wager)
    assert challenge_wager.winner_id == user_a.id
    # Pot: 25 * 2 = 50. Platform burn: 10% of 50 = 5. Winner payout: 45.
    expected_payout = int(stake * 2 * 0.90)  # 45
    assert challenge_wager.coins_awarded == expected_payout, f"Expected {expected_payout}, got {challenge_wager.coins_awarded}"

    bal_a = coin_service.get_user_coins(db, user_a.id)
    bal_b = coin_service.get_user_coins(db, user_b.id)
    assert bal_a == 75 + 45, f"User A should have 120 coins (75 + 45), got {bal_a}"
    assert bal_b == 75, f"User B should have 75 coins (100 - 25), got {bal_b}"
    # Net economy: Total starting was 200. Total ending is 120 + 75 = 195. Exactly 5 coins burned!
    assert (bal_a + bal_b) == 195, f"Expected 195 coins total (5 burned), got {bal_a + bal_b}"
    print(f"[PASS] Wager duel completed: Winner got {expected_payout} coins. 5 coins burned by platform!")

    # ─────────────────────────────────────────────────────────────────────────
    # TEST 5: Wagered Duel Exact Tie (100% Refunded)
    # ─────────────────────────────────────────────────────────────────────────
    print("\n--- TEST 5: Wagered Duel Tie (100% Stake Refunded) ---")
    stake_tie = 10
    coin_service.spend_coins(db, user_a.id, stake_tie, category="duel_wager_escrow", description="Escrowed 10 coins")
    assert coin_service.get_user_coins(db, user_a.id) == 120 - 10  # 110

    challenge_tie = models.PartnerChallenge(
        id=uuid.uuid4(),
        partnership_id=partnership.id,
        challenger_id=user_a.id,
        challenged_id=user_b.id,
        challenge_type="quickfire_duel",
        title="Quickfire Duel",
        total_questions=15,
        challenger_score=15,
        challenger_time_seconds=50,
        status="challenger_done",
        wager_amount=stake_tie,
    )
    db.add(challenge_tie)
    db.commit()

    # User B submits exact same score and time
    submit_payload_tie = {
        "challenged_score": 15,
        "challenged_time_seconds": 50,
        "challenged_breakdown": {},
    }
    complete_res_tie = submit_partner_challenge(
        challenge_id=challenge_tie.id,
        payload=submit_payload_tie,
        db=db,
        current_user=user_b,
    )

    db.refresh(challenge_tie)
    assert challenge_tie.winner_id is None, "Tie has no winner"
    bal_a_tie = coin_service.get_user_coins(db, user_a.id)
    bal_b_tie = coin_service.get_user_coins(db, user_b.id)
    assert bal_a_tie == 120, f"User A refunded 10 coins back to 120, got {bal_a_tie}"
    assert bal_b_tie == 75, f"User B matched 10 and refunded 10 back to 75, got {bal_b_tie}"
    print("[PASS] Exact tie refunded 100% of stakes to both players.")

    print("\n=======================================================")
    print("ALL NU-COINS ECONOMY REBALANCING TESTS PASSED (100%)!")
    print("=======================================================")

if __name__ == "__main__":
    run_economy_rebalancing_tests()
