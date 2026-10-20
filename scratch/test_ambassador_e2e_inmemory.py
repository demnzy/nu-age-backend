import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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

import models
from services import ambassador_service


class TestAmbassadorE2EInMemory(unittest.TestCase):
    def setUp(self):
        # In-memory SQLite isolated database
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

        # Create required tables for isolated test
        self.target_tables = [
            models.User.__table__,
            models.Course.__table__,
            models.Enrollment.__table__,
            models.MemberBadge.__table__,
            models.UserBadge.__table__,
            models.PromoCode.__table__,
            models.CampusAmbassador.__table__,
            models.CampusAmbassadorApplication.__table__,
        ]
        models.Base.metadata.create_all(self.engine, tables=self.target_tables)

        # Prepopulate MemberBadge for campus_ambassador
        badge = models.MemberBadge(
            slug="campus_ambassador",
            name="Campus Ambassador",
            description="Verified campus growth leader",
            icon="SCHOOL_ROUNDED",
        )
        self.db.add(badge)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        models.Base.metadata.drop_all(self.engine, tables=self.target_tables)

    def test_submit_and_list_applications_with_user_join(self):
        # 1. Create a platform user with course enrollments
        user_id = uuid.uuid4()
        user = models.User(
            id=user_id,
            first_name="Chidi",
            last_name="Okafor",
            email="chidi@futminna.edu.ng",
            password="hashed",
            username="chidi_fut",
            gender="MALE",
            role="STUDENT",
            university="Federal University of Technology, Minna (FUTMinna)",
            coins_balance=450,
            streak=7,
            is_verified=True,
        )
        self.db.add(user)

        # Add course and enrollment
        course = models.Course(
            id=uuid.uuid4(),
            admin_id=user_id,
            name="EEE 311 Electric Circuits",
            description="Circuit theory",
        )
        self.db.add(course)

        enrollment = models.Enrollment(
            student_id=user_id,
            course_id=course.id,
            progress=45.0,
        )
        self.db.add(enrollment)
        self.db.commit()

        # 2. Submit an application for this registered user
        app1 = ambassador_service.submit_ambassador_application(self.db, {
            "full_name": "Chidi Okafor",
            "email": "chidi@futminna.edu.ng",
            "university": "FUTMinna",
            "department": "Electrical Engineering",
            "level": "300L",
            "phone": "08012345678",
            "notes": "Leading the campus tech club and eager to share Nu-Age with coursemates.",
        })
        self.assertIsNotNone(app1.id)
        self.assertEqual(app1.status, "pending")

        # 3. Submit an application for an unregistered user
        app2 = ambassador_service.submit_ambassador_application(self.db, {
            "full_name": "Fatima Bello",
            "email": "fatima@unilag.edu.ng",
            "university": "UNILAG",
            "department": "Mass Communication",
            "level": "200L",
            "phone": "08098765432",
            "notes": "Passionate about educational tech and student study circles.",
        })
        self.assertIsNotNone(app2.id)

        # 4. List applications and verify joined platform user statistics
        apps = ambassador_service.list_ambassador_applications_admin(self.db)
        self.assertEqual(len(apps), 2)

        # Inspect Chidi's record (registered user)
        chidi_rec = next(a for a in apps if a["email"] == "chidi@futminna.edu.ng")
        self.assertTrue(chidi_rec["is_on_platform"])
        self.assertEqual(chidi_rec["user_id"], str(user_id))
        self.assertEqual(chidi_rec["username"], "chidi_fut")
        self.assertEqual(chidi_rec["user_name"], "Chidi Okafor")
        self.assertEqual(chidi_rec["courses_enrolled_count"], 1)
        self.assertEqual(chidi_rec["coins_balance"], 450)
        self.assertEqual(chidi_rec["streak"], 7)
        self.assertTrue(chidi_rec["is_verified"])

        # Inspect Fatima's record (unregistered user)
        fatima_rec = next(a for a in apps if a["email"] == "fatima@unilag.edu.ng")
        self.assertFalse(fatima_rec["is_on_platform"])
        self.assertIsNone(fatima_rec["user_id"])
        self.assertIsNone(fatima_rec["username"])
        self.assertEqual(fatima_rec["courses_enrolled_count"], 0)

    def test_process_invitation_lifecycle(self):
        # 1. Invite an unregistered candidate
        amb = ambassador_service.invite_campus_ambassador(
            db=self.db,
            email="new_lead@oauife.edu.ng",
            university="Obafemi Awolowo University (OAU)",
            notes="Web application referral",
        )
        self.assertIsNotNone(amb.invite_token)
        self.assertIsNone(amb.user_id)
        self.assertEqual(amb.status, "invited")

        # 2. Process invite before signing up -> should return 'needs_signup'
        res1 = ambassador_service.process_ambassador_invitation(self.db, amb.invite_token)
        self.assertEqual(res1["status"], "needs_signup")
        self.assertEqual(res1["university"], "Obafemi Awolowo University (OAU)")
        self.assertEqual(res1["email"], "new_lead@oauife.edu.ng")

        # 3. Now user registers an account on the platform
        u_id = uuid.uuid4()
        user = models.User(
            id=u_id,
            first_name="Kehinde",
            last_name="Adeyemi",
            email="new_lead@oauife.edu.ng",
            password="hashed",
            username="k_adeyemi",
            gender="FEMALE",
            role="STUDENT",
            university="Obafemi Awolowo University (OAU)",
        )
        self.db.add(user)
        self.db.commit()

        # 4. Process invite again -> should succeed, activate, and award badge
        res2 = ambassador_service.process_ambassador_invitation(self.db, amb.invite_token)
        self.assertEqual(res2["status"], "success")
        self.assertIn("Kehinde Adeyemi", res2["name"])

        # Verify DB ambassador is active
        refreshed_amb = self.db.query(models.CampusAmbassador).filter_by(id=amb.id).first()
        self.assertEqual(refreshed_amb.status, "active")
        self.assertEqual(refreshed_amb.user_id, u_id)

        # Verify badge awarded to user
        user_badge = self.db.query(models.UserBadge).filter_by(user_id=u_id).first()
        self.assertIsNotNone(user_badge)

        # 5. Process invite a third time -> should return 'already_active'
        res3 = ambassador_service.process_ambassador_invitation(self.db, amb.invite_token)
        self.assertEqual(res3["status"], "already_active")

    def test_review_application_approve_and_reject(self):
        # Create an application
        app = ambassador_service.submit_ambassador_application(self.db, {
            "full_name": "Tariq Ibrahim",
            "email": "tariq@abu.edu.ng",
            "university": "Ahmadu Bello University (ABU)",
            "department": "Computer Science",
            "level": "400L",
        })

        admin_id = uuid.uuid4()
        # Approve application
        res_app = ambassador_service.review_ambassador_application(
            db=self.db,
            application_id=app.id,
            action="approve",
            admin_user_id=admin_id,
        )
        self.assertEqual(res_app["status"], "success")
        self.assertEqual(res_app["application_status"], "approved")

        # Verify an ambassador invite record was automatically created
        created_amb = self.db.query(models.CampusAmbassador).filter_by(email="tariq@abu.edu.ng").first()
        self.assertIsNotNone(created_amb)
        self.assertEqual(created_amb.status, "invited")
        self.assertEqual(created_amb.university, "Ahmadu Bello University (ABU)")

        # Create another application and reject it
        app2 = ambassador_service.submit_ambassador_application(self.db, {
            "full_name": "Spam User",
            "email": "spam@example.com",
            "university": "Fake Uni",
            "department": "None",
            "level": "100L",
        })
        res_rej = ambassador_service.review_ambassador_application(
            db=self.db,
            application_id=app2.id,
            action="reject",
            admin_user_id=admin_id,
        )
        self.assertEqual(res_rej["status"], "success")
        self.assertEqual(res_rej["application_status"], "rejected")

        refreshed_app2 = self.db.query(models.CampusAmbassadorApplication).filter_by(id=app2.id).first()
        self.assertEqual(refreshed_app2.status, "rejected")


if __name__ == "__main__":
    unittest.main()
