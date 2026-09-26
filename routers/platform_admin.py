import os
import sys
import io
import csv
import time
from uuid import UUID
from datetime import datetime, timezone, timedelta
from typing import List, Optional
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from fastapi import APIRouter, Depends, HTTPException, status, Query, BackgroundTasks
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy import or_, and_, func, desc
from sqlalchemy.orm import Session

from database import get_db, Settings
import models
from schemas import Roles
from services import auth, utils
from services.notifications import send_push_notification
import resend


router = APIRouter(prefix="/platform-admin", tags=["Platform Super Admin"])


# ─────────────────────────────────────────────────────────────────────────────
# PYDANTIC SCHEMAS
# ─────────────────────────────────────────────────────────────────────────────

class AdminLoginRequest(BaseModel):
    username: str  # Can be username or email
    password: str


class AdminVerifyResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    user_id: UUID
    username: str
    email: str
    role: str
    name: str


class PlatformAnalyticsResponse(BaseModel):
    total_users: int
    total_students: int
    total_teachers: int
    total_admins: int
    verified_users: int
    unverified_users: int
    active_today: int
    active_this_week: int
    total_organisations: int
    total_courses: int
    total_enrollments: int
    total_device_tokens: int


class AdminUserItem(BaseModel):
    id: UUID
    name: str
    first_name: str
    last_name: str
    username: str
    email: str
    number: Optional[str] = None
    role: str
    gender: str
    university: Optional[str] = None
    streak: int = 0
    is_verified: bool = False
    last_login_date: Optional[str] = None
    created_at: Optional[str] = None
    enrolled_courses_count: int = 0
    created_courses_count: int = 0
    organisations_count: int = 0


class AdminUsersPageResponse(BaseModel):
    items: List[AdminUserItem]
    total: int
    page: int
    limit: int
    total_pages: int


class AdminUserUpdateRequest(BaseModel):
    role: Optional[str] = None
    is_verified: Optional[bool] = None
    streak: Optional[int] = None
    university: Optional[str] = None


class BulkEmailRequest(BaseModel):
    audience: str = "all"  # "all", "students", "teachers", "admins", "unverified"
    subject: str
    body_html: str
    sender_name: Optional[str] = "Tobi from Nu Age"


class BulkPushRequest(BaseModel):
    audience: str = "all"  # "all", "students", "teachers", "admins", "unverified"
    title: str
    body: str
    action_route: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# SUPER ADMIN DEPENDENCY GUARD
# ─────────────────────────────────────────────────────────────────────────────

def get_configured_platform_admins() -> List[str]:
    """
    Retrieves the list of designated super admin usernames and emails
    using the codebase Settings() env search pattern in database.py
    with os.getenv fallback.
    """
    try:
        raw = getattr(Settings(), "PLATFORM_SUPER_ADMINS", "")
    except Exception:
        raw = ""
    if not raw:
        raw = os.getenv("PLATFORM_SUPER_ADMINS", "")
    return [s.strip().lower() for s in str(raw).split(",") if s.strip()]


def get_current_super_admin(
    current_user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> models.User:
    """
    Enforces that the caller is authenticated and possesses Platform Super Admin
    privileges (either role == Admin, or listed in PLATFORM_SUPER_ADMINS).
    """
    role_str = str(getattr(current_user, "role", "")).upper()
    is_admin_role = (getattr(current_user, "role", None) == Roles.ADMIN or "ADMIN" in role_str)

    configured_admins = get_configured_platform_admins()
    is_designated = (
        current_user.username.lower() in configured_admins
        or (current_user.email and current_user.email.lower() in configured_admins)
    )

    if not (is_admin_role or is_designated):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: Platform Super Administrator privileges required.",
        )

    return current_user


# ─────────────────────────────────────────────────────────────────────────────
# 1. AUTHENTICATION & LOCK-SCREEN VERIFICATION
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/auth/verify", response_model=AdminVerifyResponse)
def verify_super_admin_credentials(
    payload: AdminLoginRequest,
    db: Session = Depends(get_db),
):
    """
    Verifies super-admin credentials directly from the admin lock screen.
    Returns an elevated access token and admin profile.
    """
    ident = payload.username.strip()
    user = (
        db.query(models.User).filter(models.User.email == ident).first()
        or db.query(models.User).filter(models.User.username == ident).first()
    )

    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User account not found.")

    if not utils.verify_password(payload.password, user.password):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Incorrect password.")

    role_str = str(getattr(user, "role", "")).upper()
    is_admin_role = (getattr(user, "role", None) == Roles.ADMIN or "ADMIN" in role_str)
    configured_admins = get_configured_platform_admins()
    is_designated = (
        user.username.lower() in configured_admins
        or (user.email and user.email.lower() in configured_admins)
    )

    if not (is_admin_role or is_designated):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is not designated as a Nu-Age Platform Administrator.",
        )

    # Issue access token with is_super_admin payload
    token = auth.create_access_token({"email": user.email, "is_super_admin": True})

    full_name = f"{user.first_name} {user.last_name}".strip()
    return {
        "access_token": token["access_token"],
        "token_type": "Bearer",
        "user_id": user.id,
        "username": user.username,
        "email": user.email,
        "role": str(user.role.value if hasattr(user.role, "value") else user.role),
        "name": full_name or user.username,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. PLATFORM ANALYTICS & TELEMETRY
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/analytics", response_model=PlatformAnalyticsResponse)
def get_platform_analytics(
    current_admin: models.User = Depends(get_current_super_admin),
    db: Session = Depends(get_db),
):
    """
    Computes global platform KPIs: total users, roles breakdown, verification status,
    active users, total organisations, courses, and enrollments.
    """
    now = datetime.now(timezone.utc)
    today = now.date()
    seven_days_ago = today - timedelta(days=7)

    total_users = db.query(func.count(models.User.id)).scalar() or 0
    total_students = db.query(func.count(models.User.id)).filter(models.User.role == Roles.STUDENT).scalar() or 0
    total_teachers = db.query(func.count(models.User.id)).filter(models.User.role == Roles.TEACHER).scalar() or 0
    total_admins = db.query(func.count(models.User.id)).filter(models.User.role == Roles.ADMIN).scalar() or 0

    verified_users = db.query(func.count(models.User.id)).filter(models.User.is_verified == True).scalar() or 0
    unverified_users = total_users - verified_users

    active_today = db.query(func.count(models.User.id)).filter(models.User.last_login_date == today).scalar() or 0
    active_this_week = db.query(func.count(models.User.id)).filter(models.User.last_login_date >= seven_days_ago).scalar() or 0

    total_orgs = db.query(func.count(models.Organisation.id)).scalar() or 0
    total_courses = db.query(func.count(models.Course.id)).scalar() or 0

    # Total enrollments
    enrollment_table = models.Base.metadata.tables.get("enrollments")
    total_enrollments = 0
    if enrollment_table is not None:
        total_enrollments = db.query(func.count()).select_from(enrollment_table).scalar() or 0

    total_device_tokens = db.query(func.count(models.DeviceToken.id)).scalar() or 0

    return {
        "total_users": total_users,
        "total_students": total_students,
        "total_teachers": total_teachers,
        "total_admins": total_admins,
        "verified_users": verified_users,
        "unverified_users": unverified_users,
        "active_today": active_today,
        "active_this_week": active_this_week,
        "total_organisations": total_orgs,
        "total_courses": total_courses,
        "total_enrollments": total_enrollments,
        "total_device_tokens": total_device_tokens,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 3. USER DIRECTORY & MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/users", response_model=AdminUsersPageResponse)
def list_platform_users(
    q: Optional[str] = Query(None, description="Search query across name, username, email, university"),
    role: Optional[str] = Query(None, description="Filter by role: Student, Teacher, Admin"),
    is_verified: Optional[bool] = Query(None, description="Filter by verification state"),
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
    sort_by: str = Query("created_desc", description="created_desc, created_asc, streak_desc, name_asc"),
    current_admin: models.User = Depends(get_current_super_admin),
    db: Session = Depends(get_db),
):
    """
    Returns a paginated list of users with search and filter controls.
    """
    query = db.query(models.User)

    if isinstance(q, str) and q.strip():
        term = f"%{q.strip().lower()}%"
        query = query.filter(
            or_(
                func.lower(models.User.first_name).like(term),
                func.lower(models.User.last_name).like(term),
                func.lower(models.User.username).like(term),
                func.lower(models.User.email).like(term),
                func.lower(models.User.university).like(term),
            )
        )

    if isinstance(role, str) and role.strip() and role.lower() != "all":
        r_clean = role.strip().capitalize()
        for r_enum in Roles:
            if r_enum.value.lower() == r_clean.lower():
                query = query.filter(models.User.role == r_enum)
                break

    if is_verified is not None:
        query = query.filter(models.User.is_verified == is_verified)

    if sort_by == "created_asc":
        query = query.order_by(models.User.created_at.asc())
    elif sort_by == "streak_desc":
        query = query.order_by(models.User.streak.desc())
    elif sort_by == "name_asc":
        query = query.order_by(models.User.first_name.asc(), models.User.last_name.asc())
    else:
        query = query.order_by(models.User.created_at.desc())

    total = query.count()
    total_pages = max(1, (total + limit - 1) // limit)
    offset = (page - 1) * limit
    users = query.offset(offset).limit(limit).all()

    items = []
    for u in users:
        role_val = str(u.role.value if hasattr(u.role, "value") else u.role)
        gender_val = str(u.gender.value if hasattr(u.gender, "value") else u.gender)
        full_name = f"{u.first_name} {u.last_name}".strip()

        items.append(
            AdminUserItem(
                id=u.id,
                name=full_name or u.username,
                first_name=u.first_name,
                last_name=u.last_name,
                username=u.username,
                email=u.email,
                number=u.number,
                role=role_val,
                gender=gender_val,
                university=u.university,
                streak=u.streak or 0,
                is_verified=bool(u.is_verified),
                last_login_date=u.last_login_date.strftime("%Y-%m-%d") if u.last_login_date else None,
                created_at=u.created_at.strftime("%Y-%m-%d %H:%M") if u.created_at else None,
                enrolled_courses_count=len(u.courses) if hasattr(u, "courses") and u.courses else 0,
                created_courses_count=len(u.created_courses) if hasattr(u, "created_courses") and u.created_courses else 0,
                organisations_count=len(u.organisations) if hasattr(u, "organisations") and u.organisations else 0,
            )
        )

    return {
        "items": items,
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
    }


@router.get("/users/{user_id}", response_model=AdminUserItem)
def get_user_details(
    user_id: UUID,
    current_admin: models.User = Depends(get_current_super_admin),
    db: Session = Depends(get_db),
):
    """
    Returns complete details of a single user by ID.
    """
    u = db.query(models.User).filter(models.User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    role_val = str(u.role.value if hasattr(u.role, "value") else u.role)
    gender_val = str(u.gender.value if hasattr(u.gender, "value") else u.gender)
    full_name = f"{u.first_name} {u.last_name}".strip()

    return AdminUserItem(
        id=u.id,
        name=full_name or u.username,
        first_name=u.first_name,
        last_name=u.last_name,
        username=u.username,
        email=u.email,
        number=u.number,
        role=role_val,
        gender=gender_val,
        university=u.university,
        streak=u.streak or 0,
        is_verified=bool(u.is_verified),
        last_login_date=u.last_login_date.strftime("%Y-%m-%d") if u.last_login_date else None,
        created_at=u.created_at.strftime("%Y-%m-%d %H:%M") if u.created_at else None,
        enrolled_courses_count=len(u.courses) if hasattr(u, "courses") and u.courses else 0,
        created_courses_count=len(u.created_courses) if hasattr(u, "created_courses") and u.created_courses else 0,
        organisations_count=len(u.organisations) if hasattr(u, "organisations") and u.organisations else 0,
    )


@router.patch("/users/{user_id}")
def update_user_attributes(
    user_id: UUID,
    payload: AdminUserUpdateRequest,
    current_admin: models.User = Depends(get_current_super_admin),
    db: Session = Depends(get_db),
):
    """
    Modifies a user's role, verification status, streak, or university.
    """
    u = db.query(models.User).filter(models.User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    if payload.role is not None:
        r_clean = payload.role.strip().capitalize()
        for r_enum in Roles:
            if r_enum.value.lower() == r_clean.lower():
                u.role = r_enum
                break

    if payload.is_verified is not None:
        u.is_verified = payload.is_verified

    if payload.streak is not None:
        u.streak = max(0, payload.streak)

    if payload.university is not None:
        u.university = payload.university.strip()

    db.commit()
    db.refresh(u)
    return {"message": "User updated successfully.", "user_id": str(u.id)}


@router.delete("/users/{user_id}")
def delete_user_account(
    user_id: UUID,
    current_admin: models.User = Depends(get_current_super_admin),
    db: Session = Depends(get_db),
):
    """
    Permanently deletes a user account with cascading cleanup of foreign-key dependencies
    (refresh tokens, device tokens, memberships, submissions, connections).
    """
    if str(current_admin.id) == str(user_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own super administrator account while logged in.",
        )

    u = db.query(models.User).filter(models.User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    try:
        # 1. Clean up refresh tokens (foreign key with no default cascade)
        db.query(models.RefreshToken).filter(models.RefreshToken.user_id == user_id).delete()

        # 2. Clean up device tokens
        db.query(models.DeviceToken).filter(models.DeviceToken.user_id == user_id).delete()

        # 3. Clean up OTP records if present
        if u.email:
            db.query(models.SignupOTP).filter(models.SignupOTP.email == u.email).delete()
            db.query(models.PasswordResetOTP).filter(models.PasswordResetOTP.email == u.email).delete()

        # 4. Delete the user (SQLAlchemy / Postgres cascades handle remaining child relations)
        db.delete(u)
        db.commit()
        return {"message": f"User account '{u.username}' ({u.email}) permanently deleted.", "user_id": str(user_id)}
    except Exception as ex:
        db.rollback()
        print(f"[platform_admin] Error deleting user {user_id}: {ex}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete user account: {ex}",
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4. EXCEL (.XLSX) & CSV DATA EXPORT
# ─────────────────────────────────────────────────────────────────────────────

def _generate_excel_workbook(users: List[models.User]) -> io.BytesIO:
    """
    Generates a beautifully styled Excel workbook containing all user information.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Nu-Age Users"

    # Brand Colors
    header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")  # Slate 800
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    data_font = Font(name="Calibri", size=10, color="0F172A")
    border_side = Side(border_style="thin", color="CBD5E1")
    cell_border = Border(left=border_side, right=border_side, top=border_side, bottom=border_side)

    headers = [
        "User ID",
        "Full Name",
        "First Name",
        "Last Name",
        "Username",
        "Email",
        "Phone Number",
        "Role",
        "Gender",
        "Verified",
        "Streak (Days)",
        "University",
        "Last Login Date",
        "Created At (UTC)",
    ]
    ws.append(headers)

    # Style Header Row
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = cell_border
    ws.row_dimensions[1].height = 28

    # Populate Data
    row_idx = 2
    for u in users:
        role_str = str(u.role.value if hasattr(u.role, "value") else u.role)
        gender_str = str(u.gender.value if hasattr(u.gender, "value") else u.gender)
        full_name = f"{u.first_name} {u.last_name}".strip()

        row_data = [
            str(u.id),
            full_name or u.username,
            u.first_name,
            u.last_name,
            u.username,
            u.email,
            u.number or "",
            role_str,
            gender_str,
            "Yes" if u.is_verified else "No",
            u.streak or 0,
            u.university or "",
            u.last_login_date.strftime("%Y-%m-%d") if u.last_login_date else "",
            u.created_at.strftime("%Y-%m-%d %H:%M:%S") if u.created_at else "",
        ]
        ws.append(row_data)

        # Style data cells
        for col_idx in range(1, len(row_data) + 1):
            c = ws.cell(row=row_idx, column=col_idx)
            c.font = data_font
            c.border = cell_border
            if col_idx in (1, 8, 9, 10, 11, 13, 14):
                c.alignment = Alignment(horizontal="center", vertical="center")
            else:
                c.alignment = Alignment(horizontal="left", vertical="center")

        ws.row_dimensions[row_idx].height = 20
        row_idx += 1

    # Auto-adjust column widths
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            val_str = str(cell.value or "")
            if len(val_str) > max_len:
                max_len = len(val_str)
        ws.column_dimensions[col_letter].width = max(max_len + 4, 12)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output


@router.get("/users/export")
def export_platform_users(
    format: str = Query("xlsx", pattern="^(xlsx|csv)$"),
    role: Optional[str] = Query(None, description="Optional role filter"),
    current_admin: models.User = Depends(get_current_super_admin),
    db: Session = Depends(get_db),
):
    """
    Exports all current platform users to a formatted Excel workbook (.xlsx) or CSV file.
    """
    query = db.query(models.User).order_by(models.User.created_at.desc())
    if isinstance(role, str) and role.strip() and role.lower() != "all":
        r_clean = role.strip().capitalize()
        for r_enum in Roles:
            if r_enum.value.lower() == r_clean.lower():
                query = query.filter(models.User.role == r_enum)
                break

    users = query.all()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if format.lower() == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "User ID", "Full Name", "First Name", "Last Name", "Username", "Email",
            "Phone Number", "Role", "Gender", "Verified", "Streak", "University",
            "Last Login Date", "Created At"
        ])
        for u in users:
            role_str = str(u.role.value if hasattr(u.role, "value") else u.role)
            gender_str = str(u.gender.value if hasattr(u.gender, "value") else u.gender)
            writer.writerow([
                str(u.id), f"{u.first_name} {u.last_name}".strip(), u.first_name, u.last_name,
                u.username, u.email, u.number or "", role_str, gender_str,
                "Yes" if u.is_verified else "No", u.streak or 0, u.university or "",
                u.last_login_date.strftime("%Y-%m-%d") if u.last_login_date else "",
                u.created_at.strftime("%Y-%m-%d %H:%M:%S") if u.created_at else "",
            ])
        output.seek(0)
        return StreamingResponse(
            io.BytesIO(output.getvalue().encode("utf-8")),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=nu_age_users_{timestamp}.csv"},
        )

    # Default: Excel (.xlsx)
    excel_stream = _generate_excel_workbook(users)
    return StreamingResponse(
        excel_stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=nu_age_users_{timestamp}.xlsx"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# 5. BULK COMMUNICATIONS (EMAIL & PUSH NOTIFICATIONS)
# ─────────────────────────────────────────────────────────────────────────────

def _filter_users_by_audience(db: Session, audience: str) -> List[models.User]:
    aud = (audience or "all").lower().strip()
    query = db.query(models.User)
    if aud == "students":
        query = query.filter(models.User.role == Roles.STUDENT)
    elif aud == "teachers":
        query = query.filter(models.User.role == Roles.TEACHER)
    elif aud == "admins":
        query = query.filter(models.User.role == Roles.ADMIN)
    elif aud == "unverified":
        query = query.filter(models.User.is_verified == False)
    return query.all()


def _dispatch_bulk_email_task(emails: List[str], subject: str, html_body: str, sender_name: str):
    """
    Background batch dispatcher for bulk emails using Resend with rate limit pacing.
    """
    resend.api_key = Settings().RESEND_API_KEY
    sender_email = f"{sender_name} <support@nu-age.name.ng>"

    batch_size = 50
    for i in range(0, len(emails), batch_size):
        chunk = emails[i : i + batch_size]
        for email in chunk:
            try:
                params: resend.Emails.SendParams = {
                    "from": sender_email,
                    "to": [email],
                    "subject": subject,
                    "html": html_body,
                }
                resend.Emails.send(params)
            except Exception as e:
                print(f"[platform_admin] Failed email to {email}: {e}")
        # Pace batches to respect provider rate limits
        time.sleep(0.5)


@router.post("/broadcast/email")
def broadcast_bulk_email(
    payload: BulkEmailRequest,
    background_tasks: BackgroundTasks,
    current_admin: models.User = Depends(get_current_super_admin),
    db: Session = Depends(get_db),
):
    """
    Sends a bulk email to all users matching the audience filter via Resend in the background.
    """
    if not payload.subject.strip():
        raise HTTPException(status_code=400, detail="Subject cannot be empty.")
    if not payload.body_html.strip():
        raise HTTPException(status_code=400, detail="Email body cannot be empty.")

    target_users = _filter_users_by_audience(db, payload.audience)
    emails = [u.email for u in target_users if u.email and "@" in u.email]

    if not emails:
        return {"message": "No users found matching audience criteria.", "recipient_count": 0}

    background_tasks.add_task(
        _dispatch_bulk_email_task,
        emails,
        payload.subject.strip(),
        payload.body_html.strip(),
        payload.sender_name or "Tobi from Nu Age",
    )

    return {
        "message": f"Bulk email queued successfully for {len(emails)} recipients in audience '{payload.audience}'.",
        "recipient_count": len(emails),
        "audience": payload.audience,
    }


@router.post("/broadcast/push")
def broadcast_bulk_push_notification(
    payload: BulkPushRequest,
    current_admin: models.User = Depends(get_current_super_admin),
    db: Session = Depends(get_db),
):
    """
    Dispatches a push notification via Firebase FCM to all devices belonging to users in the audience.
    """
    if not payload.title.strip() or not payload.body.strip():
        raise HTTPException(status_code=400, detail="Notification title and body are required.")

    target_users = _filter_users_by_audience(db, payload.audience)
    user_ids = [u.id for u in target_users]

    if not user_ids:
        return {"message": "No users found matching audience criteria.", "device_count": 0}

    # Query all active device tokens for these users
    tokens = db.query(models.DeviceToken).filter(models.DeviceToken.user_id.in_(user_ids)).all()
    token_strings = [t.token for t in tokens if t.token]

    if not token_strings:
        return {"message": "No registered device tokens found for target audience.", "device_count": 0}

    sent_count = 0
    failure_count = 0
    dead_tokens = []

    try:
        import firebase_admin
        from firebase_admin import messaging

        data_payload = {"route": payload.action_route} if payload.action_route else {}

        # Chunk into 500 tokens (Firebase multicast limit)
        chunk_size = 500
        for i in range(0, len(token_strings), chunk_size):
            chunk = token_strings[i : i + chunk_size]
            msg = messaging.MulticastMessage(
                notification=messaging.Notification(
                    title=payload.title.strip(),
                    body=payload.body.strip(),
                ),
                data=data_payload,
                tokens=chunk,
            )
            response = messaging.send_each_for_multicast(msg)
            sent_count += response.success_count
            failure_count += response.failure_count

            # Collect dead tokens for cleanup
            if response.failure_count > 0:
                for idx, resp in enumerate(response.responses):
                    if not resp.success:
                        if getattr(resp.exception, "code", "") == "messaging/registration-token-not-registered":
                            dead_tokens.append(chunk[idx])

        # Purge dead tokens from database
        if dead_tokens:
            db.query(models.DeviceToken).filter(models.DeviceToken.token.in_(dead_tokens)).delete(synchronize_session=False)
            db.commit()

    except Exception as ex:
        print(f"[platform_admin] Push notification dispatch error: {ex}")
        raise HTTPException(status_code=500, detail=f"Failed sending push notification: {ex}")

    return {
        "message": f"Push broadcast delivered to {sent_count} device(s) ({failure_count} failures, {len(dead_tokens)} dead tokens pruned).",
        "devices_targeted": len(token_strings),
        "sent_count": sent_count,
        "failure_count": failure_count,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 6. SYSTEM HEALTH & DIAGNOSTICS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/health")
def platform_system_health(
    current_admin: models.User = Depends(get_current_super_admin),
    db: Session = Depends(get_db),
):
    """
    Checks database connection responsiveness, server timestamp, and active pool health.
    """
    t0 = time.time()
    try:
        from sqlalchemy import text
        db.execute(text("SELECT 1;"))
        db_latency_ms = round((time.time() - t0) * 1000, 2)
        db_status = "healthy"
    except Exception as ex:
        db_latency_ms = round((time.time() - t0) * 1000, 2)
        db_status = f"unhealthy: {ex}"

    return {
        "status": "online",
        "server_time_utc": datetime.now(timezone.utc).isoformat(),
        "database_status": db_status,
        "database_latency_ms": db_latency_ms,
        "super_admin": current_admin.username,
    }
