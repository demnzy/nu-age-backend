"""
scratch/test_gamification_e2e.py
──────────────────────────────────────────────────────────────────────────────
In-memory SQLite unit test suite for:
- Promo code multi-reward execution (coins, AI credits, badges).
- Idempotency & usage limits enforcement.
- Ambassador attribution & coin kickbacks.
- Objectives claim with badge reward.
- Campus Ambassador invitation and token acceptance.
Zero live database corruption (uses sqlite:///:memory:).
──────────────────────────────────────────────────────────────────────────────
"""

import unittest
import uuid
from datetime import datetime, timezone, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from models import Base
from services import promo_service, ambassador_service, coin_service


class TestGamificationE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Create isolated in-memory SQLite engine
        cls.engine = create_engine("sqlite:///:memory:")
        target_tables = [
            models.User.__table__,
            models.PromoCode.__table__,
            models.PromoCodeRedemption.__table__,
            models.MemberBadge.__table__,
            models.UserBadge.__table__,
            models.CampusAmbassador.__table__,
            models.PlatformObjective.__table__,
            models.UserObjectiveProgress.__table__,
            models.CoinTransaction.__table__,
        ]
        try:
            from monetization_models import CreditBalance, CreditLedgerEntry
            target_tables.extend([CreditBalance.__table__, CreditLedgerEntry.__table__])
        except Exception:
            pass

        Base.metadata.create_all(bind=cls.engine, tables=target_tables)
        cls.SessionLocal = sessionmaker(bind=cls.engine)

    def setUp(self):
        self.db = self.SessionLocal()
        promo_service.seed_default_badges_if_empty(self.db)

        # Create test users
        self.student = models.User(
            id=uuid.uuid4(),
            first_name="Amara",
            last_name="Okonkwo",
            gender=models.Gender.FEMALE,
            email=f"amara_{uuid.uuid4().hex[:6]}@example.com",
            password="hashed_pwd",
            username=f"amara_{uuid.uuid4().hex[:6]}",
            role=models.Roles.STUDENT,
            coins_balance=100,
            streak=12,
        )
        self.ambassador_user = models.User(
            id=uuid.uuid4(),
            first_name="Dami",
            last_name="Adeyemi",
            gender=models.Gender.MALE,
            email=f"dami_{uuid.uuid4().hex[:6]}@example.com",
            password="hashed_pwd",
            username=f"dami_{uuid.uuid4().hex[:6]}",
            role=models.Roles.STUDENT,
            coins_balance=50,
            streak=5,
        )
        self.db.add_all([self.student, self.ambassador_user])
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_01_promo_multi_reward_and_idempotency(self):
        # 1. Create a promo code with coins, AI credits, and a badge
        promo = models.PromoCode(
            code="WELCOMEVIP",
            title="Welcome VIP Pack",
            reward_coins=250,
            reward_ai_credits=50,
            reward_badge_slug="beta_tester",
            is_active=True,
            max_uses=10,
        )
        self.db.add(promo)
        self.db.commit()

        # 2. Redeem promo code for student
        res = promo_service.redeem_promo_code(self.db, self.student.id, "welcomevip")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["rewards"]["coins"], 250)
        self.assertEqual(res["rewards"]["badge"]["slug"], "beta_tester")

        # 3. Verify user's coin balance increased
        self.student = self.db.query(models.User).filter(models.User.id == self.student.id).first()
        self.assertEqual(self.student.coins_balance, 350)

        # 4. Verify badge was unlocked
        badges = promo_service.get_user_badges(self.db, self.student.id)
        slugs = [b["slug"] for b in badges]
        self.assertIn("beta_tester", slugs)

        # 5. Verify idempotency: duplicate redemption raises error
        with self.assertRaises(Exception):
            promo_service.redeem_promo_code(self.db, self.student.id, "WELCOMEVIP")

    def test_02_ambassador_referral_attribution(self):
        # Create an ambassador promo code
        amb_code = models.PromoCode(
            code="UNILAGDAMI",
            title="Dami's UNILAG Discount",
            reward_coins=100,
            ambassador_id=self.ambassador_user.id,
            ambassador_coin_reward=40,
            is_active=True,
        )
        self.db.add(amb_code)
        self.db.commit()

        initial_amb_coins = self.ambassador_user.coins_balance or 0

        # Student redeems ambassador code
        res = promo_service.redeem_promo_code(self.db, self.student.id, "UNILAGDAMI")
        self.assertEqual(res["status"], "success")

        # Verify ambassador received commission
        self.ambassador_user = self.db.query(models.User).filter(models.User.id == self.ambassador_user.id).first()
        self.assertEqual(self.ambassador_user.coins_balance, initial_amb_coins + 40)

    def test_03_promo_code_max_uses(self):
        # Code with max_uses = 1
        limited_code = models.PromoCode(
            code="FIRSTCOME1",
            title="Exclusive Drop",
            reward_coins=50,
            max_uses=1,
            is_active=True,
        )
        self.db.add(limited_code)
        self.db.commit()

        # 1st redemption succeeds
        res = promo_service.redeem_promo_code(self.db, self.student.id, "FIRSTCOME1")
        self.assertEqual(res["status"], "success")

        # 2nd user tries to redeem
        other_student = models.User(
            id=uuid.uuid4(),
            first_name="Tunde",
            last_name="Bakare",
            gender=models.Gender.MALE,
            email=f"tunde_{uuid.uuid4().hex[:6]}@example.com",
            password="pwd",
            username=f"tunde_{uuid.uuid4().hex[:6]}",
            role=models.Roles.STUDENT,
        )
        self.db.add(other_student)
        self.db.commit()

        with self.assertRaises(Exception):
            promo_service.redeem_promo_code(self.db, other_student.id, "FIRSTCOME1")

    def test_04_objective_with_badge_reward(self):
        # Create objective with badge_reward_slug
        obj = models.PlatformObjective(
            code="merit_mastery",
            title="Merit Mastery",
            description="Achieve master curriculum review",
            category="courses",
            target_value=1,
            coin_reward=80,
            badge_reward_slug="merit_scholar",
            is_active=True,
        )
        self.db.add(obj)
        self.db.commit()

        # Add progress and complete it
        prog = models.UserObjectiveProgress(
            user_id=self.student.id,
            objective_code="merit_mastery",
            current_value=1,
            target_value=1,
            is_completed=True,
            is_claimed=False,
        )
        self.db.add(prog)
        self.db.commit()

        # Claim objective
        ok, coins, msg = coin_service.claim_objective_reward(self.db, self.student.id, "merit_mastery")
        self.assertTrue(ok)
        self.assertEqual(coins, 80)
        self.assertIn("Merit Scholar", msg)

        # Verify badge unlocked
        badges = promo_service.get_user_badges(self.db, self.student.id)
        slugs = [b["slug"] for b in badges]
        self.assertIn("merit_scholar", slugs)

    def test_05_ambassador_invitation_and_acceptance(self):
        # Invite ambassador
        invitee_email = f"lead_{uuid.uuid4().hex[:6]}@unilag.edu.ng"
        amb = ambassador_service.invite_campus_ambassador(
            self.db,
            email=invitee_email,
            university="University of Lagos",
            notes="Faculty president",
        )
        self.assertEqual(amb.status, "invited")
        self.assertTrue(bool(amb.invite_token))

        # Accept invitation
        res = ambassador_service.accept_ambassador_invitation(self.db, amb.invite_token, self.student.id)
        self.assertEqual(res["status"], "success")

        # Verify ambassador status is active and badge awarded
        refreshed_amb = self.db.query(models.CampusAmbassador).filter(models.CampusAmbassador.id == amb.id).first()
        self.assertEqual(refreshed_amb.status, "active")
        self.assertEqual(refreshed_amb.user_id, self.student.id)

        # Verify primary badge is campus_ambassador
        resolved = promo_service.resolve_user_primary_badge(self.db, self.student.id)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["slug"], "campus_ambassador")


if __name__ == "__main__":
    unittest.main()
