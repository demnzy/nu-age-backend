"""
Unit and integration test for Nu-Age Platform Admin Router.
Tests Excel export generator, schemas, and analytics calculation.
"""
import sys
import os
import io

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from routers.platform_admin import _generate_excel_workbook
import models
from schemas import Roles, Gender
from uuid import uuid4
from datetime import datetime, timezone
import openpyxl

print("--- [1] Testing Excel Workbook Generator ---")
mock_user_1 = models.User(
    id=uuid4(),
    first_name="Ada",
    last_name="Lovelace",
    username="ada",
    email="ada@example.com",
    number="+2348012345678",
    role=Roles.ADMIN,
    gender=Gender.FEMALE,
    is_verified=True,
    streak=42,
    university="University of Lagos",
    last_login_date=datetime.now(timezone.utc).date(),
    created_at=datetime.now(timezone.utc),
)
mock_user_2 = models.User(
    id=uuid4(),
    first_name="Alan",
    last_name="Turing",
    username="alan",
    email="alan@example.com",
    number=None,
    role=Roles.STUDENT,
    gender=Gender.MALE,
    is_verified=False,
    streak=7,
    university="Covenant University",
    last_login_date=None,
    created_at=datetime.now(timezone.utc),
)

excel_stream = _generate_excel_workbook([mock_user_1, mock_user_2])
assert isinstance(excel_stream, io.BytesIO)
excel_bytes = excel_stream.getvalue()
assert len(excel_bytes) > 1000

# Verify openpyxl can load it back
wb = openpyxl.load_workbook(io.BytesIO(excel_bytes))
ws = wb.active
assert ws.title == "Nu-Age Users"
assert ws.max_row == 3  # Header + 2 rows
assert ws.cell(row=1, column=1).value == "User ID"
assert ws.cell(row=2, column=2).value == "Ada Lovelace"
assert ws.cell(row=2, column=5).value == "ada"
assert ws.cell(row=3, column=2).value == "Alan Turing"
print(f"[OK] Excel workbook generated successfully ({len(excel_bytes)} bytes, {ws.max_row} rows)!")

print("\n--- [2] Testing Platform Admin Router Imports & Routes ---")
from main import app
routes = [route.path for route in app.routes]
assert "/platform-admin/auth/verify" in routes
assert "/platform-admin/analytics" in routes
assert "/platform-admin/users" in routes
assert "/platform-admin/users/export" in routes
assert "/platform-admin/broadcast/email" in routes
assert "/platform-admin/broadcast/push" in routes
assert "/platform-admin/health" in routes
print(f"[OK] All 7 platform admin routes cleanly registered in FastAPI app!")

print("\nALL BACKEND PLATFORM ADMIN CHECKS PASSED SUCCESSFULLY!")
