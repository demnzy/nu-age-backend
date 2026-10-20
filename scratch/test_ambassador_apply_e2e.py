import re
import unittest
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, User, CampusAmbassadorApplication, CampusAmbassador, MemberBadge, UserBadge, Enrollment
from services import ambassador_service
from routers.promo import AmbassadorApplicationRequest

class TestAmbassadorE2EValidation(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        # Only create tables relevant to ambassador applications, users, and enrollments
        User.__table__.create(self.engine)
        Enrollment.__table__.create(self.engine)
        CampusAmbassadorApplication.__table__.create(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

    def tearDown(self):
        self.db.close()

    def test_cors_regex_matching(self):
        # The regex configured in Nu-age/main.py
        cors_regex = re.compile(
            r"^(https?://(localhost|127\.0\.0\.1|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+)(:\d+)?|https://(.*\.)?nu-age\.(name|com)\.ng|https://.*\.vercel\.app)$"
        )
        test_origins = [
            "https://nu-age.name.ng",
            "https://www.nu-age.name.ng",
            "https://nu-age.com.ng",
            "https://www.nu-age.com.ng",
            "https://learn.nu-age.name.ng",
            "https://learn.nu-age.com.ng",
            "https://nu-web.vercel.app",
            "http://localhost:3000",
            "http://localhost:5500",
            "http://10.0.1.5:39616",
            "http://192.168.1.50:8080",
        ]
        for origin in test_origins:
            self.assertTrue(bool(cors_regex.match(origin)), f"Origin failed regex match: {origin}")

    def test_form_submission_and_db_persistence(self):
        # Payload exactly as produced by nu-web/ambassadors.html
        raw_form_payload = {
            "full_name": "Oluwaseun Adeleke",
            "email": "adeleke@gmail.com",
            "university": "Federal University of Technology, Minna",
            "department": "Mechatronics Engineering",
            "level": "400L",
            "phone": "08012345678",
            "notes": "Eager to lead offline study sessions.",
            "is_enrolled": True,
            "applied_at": "2026-10-20T03:00:00.000Z"
        }

        # 1. Pydantic validation
        req = AmbassadorApplicationRequest(**raw_form_payload)
        self.assertEqual(req.full_name, "Oluwaseun Adeleke")
        self.assertEqual(req.university, "Federal University of Technology, Minna")

        # 2. Database persistence via ambassador_service
        app_record = ambassador_service.submit_ambassador_application(self.db, req.model_dump())
        self.assertIsNotNone(app_record.id)
        self.assertEqual(app_record.status, "pending")
        self.assertEqual(app_record.email, "adeleke@gmail.com")
        self.assertEqual(app_record.university, "Federal University of Technology, Minna")

        # 3. Verify it is queryable in admin listing
        admin_list = ambassador_service.list_ambassador_applications_admin(self.db)
        self.assertEqual(len(admin_list), 1)
        self.assertEqual(admin_list[0]["email"], "adeleke@gmail.com")
        self.assertFalse(admin_list[0]["is_on_platform"])

        # 4. Create user on platform to test platform presence link
        user = User(
            email="adeleke@gmail.com",
            username="adeleke",
            first_name="Oluwaseun",
            last_name="Adeleke",
            gender="MALE",
            password="hashedpassword",
            role="STUDENT"
        )
        self.db.add(user)
        self.db.commit()

        admin_list_with_user = ambassador_service.list_ambassador_applications_admin(self.db)
        self.assertTrue(admin_list_with_user[0]["is_on_platform"])
        self.assertEqual(admin_list_with_user[0]["username"], "adeleke")

    def test_other_university_submission(self):
        # Test submission when user selects 'Other' and types custom university name
        custom_payload = {
            "full_name": "Amina Bello",
            "email": "amina@example.com",
            "university": "Kwara State Polytechnic",
            "department": "Computer Science",
            "level": "HND 1",
            "phone": None,
            "notes": None,
            "is_enrolled": True,
            "applied_at": "2026-10-20T03:05:00.000Z"
        }
        req = AmbassadorApplicationRequest(**custom_payload)
        app_record = ambassador_service.submit_ambassador_application(self.db, req.model_dump())
        self.assertEqual(app_record.university, "Kwara State Polytechnic")

if __name__ == "__main__":
    unittest.main()
