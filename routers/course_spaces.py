"""
Course Spaces API Router for Nu-Age LMS.

Course Spaces are living, asynchronous course hubs ("Classrooms") supporting:
1. Multi-course curriculum hubs (linking 1 or more courses to a single space).
2. Space access: public open spaces or private spaces with shareable invite codes (e.g., 'SPACE-8X9L2K').
3. Automatic enrollment: Joining a space automatically enrolls the learner in all linked courses.
4. Asynchronous materials vault: Uploaded anytime by tutors via BunnyCDN, segmented by course & module,
   backed by real-time learner notifications and timestamps.
5. Topic-aligned practice drills / formative quizzes: Tied to course modules, taken in any order,
   with persistent attempt tracking, score history, and detailed explanations.
6. Scheduled classes & live meetings: Tutors schedule Google Meet / Zoom sessions with day, time,
   meeting URL, and automated student notifications.
"""

import io
import re
import uuid
import secrets
import string
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, desc, or_
from sqlalchemy.orm import Session, joinedload

import models
from database import get_db
from services import auth
from services.bunny_service import upload_bytes_to_bunny

router = APIRouter(prefix="/course-spaces", tags=["Course Spaces & Async Hubs"])


# =========================================================================
# SCHEMAS
# =========================================================================

class CreateCourseSpaceRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=255)
    description: Optional[str] = None
    is_open: bool = False
    banner_url: Optional[str] = None
    organisation_id: Optional[uuid.UUID] = None
    course_ids: Optional[List[uuid.UUID]] = []


class UpdateCourseSpaceRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=255)
    description: Optional[str] = None
    is_open: Optional[bool] = None
    banner_url: Optional[str] = None


class JoinSpaceRequest(BaseModel):
    code: Optional[str] = None
    space_id: Optional[uuid.UUID] = None


class AddCoursesToSpaceRequest(BaseModel):
    course_ids: List[uuid.UUID]


class CreateQuizQuestion(BaseModel):
    id: Optional[str] = None
    question_text: str
    scenario_text: Optional[str] = None
    options: List[str]
    correct_index: Optional[int] = None
    answer_index: Optional[int] = None
    explanation: Optional[str] = None
    points: float = 1.0
    topic: Optional[str] = None
    difficulty: Optional[str] = "standard"
    has_diagram: bool = False
    diagram_placeholder: Optional[str] = None
    image_url: Optional[str] = None


class CreateCourseSpaceQuizRequest(BaseModel):
    course_id: uuid.UUID
    module_id: Optional[uuid.UUID] = None
    title: str = Field(..., min_length=2, max_length=255)
    description: Optional[str] = None
    time_limit_minutes: int = 0
    passing_score_percentage: float = 70.0
    questions: List[CreateQuizQuestion]


class UpdateCourseSpaceQuizRequest(BaseModel):
    course_id: Optional[uuid.UUID] = None
    module_id: Optional[uuid.UUID] = None
    title: Optional[str] = Field(None, min_length=2, max_length=255)
    description: Optional[str] = None
    time_limit_minutes: Optional[int] = None
    passing_score_percentage: Optional[float] = None
    questions: Optional[List[CreateQuizQuestion]] = None


class UpdateSpaceMemberRoleRequest(BaseModel):
    role: str = Field(..., pattern="^(tutor|student|manager)$")


class AISpaceQuizParseTextRequest(BaseModel):
    raw_text: str = Field(..., min_length=10)
    subject: str = Field(default="General", max_length=120)
    exam_type: str = Field(default="Topic Drill CBT", max_length=100)
    exam_year: Optional[int] = None
    mode: str = Field(default="auto", description="'extract', 'generate', or 'auto'")
    question_count: int = Field(default=10, ge=1, le=50)
    difficulty: str = Field(default="standard")


class SubmitQuizAnswer(BaseModel):
    question_id: str
    chosen_index: int


class SubmitQuizAttemptRequest(BaseModel):
    answers: List[SubmitQuizAnswer]
    duration_seconds: int = 0


class CreateScheduledSessionRequest(BaseModel):
    title: str = Field(..., min_length=2, max_length=255)
    description: Optional[str] = None
    meeting_link: str
    start_time: datetime
    duration_minutes: int = 60
    course_id: Optional[uuid.UUID] = None


# =========================================================================
# HELPER FUNCTIONS
# =========================================================================

def _course_title(course: Optional[models.Course]) -> str:
    """Helper to extract course title (model uses 'name')."""
    if not course:
        return "Course"
    return getattr(course, "name", None) or getattr(course, "title", "Course")


def _generate_space_code(db: Session) -> str:
    """Generate a unique 6-8 character uppercase access code (e.g. SPACE-7H3K2P)."""
    alphabet = string.ascii_uppercase + string.digits
    for _ in range(20):
        random_suffix = "".join(secrets.choice(alphabet) for _ in range(6))
        code = f"SPACE-{random_suffix}"
        existing = db.query(models.CourseSpace).filter(models.CourseSpace.code == code).first()
        if not existing:
            return code
    return f"SPACE-{uuid.uuid4().hex[:8].upper()}"


def _get_user_space_role(db: Session, space_id: uuid.UUID, user_id: uuid.UUID) -> Optional[str]:
    """Return user's role in the space ('tutor', 'student', 'creator') or None."""
    member = db.query(models.CourseSpaceMember).filter(
        models.CourseSpaceMember.space_id == space_id,
        models.CourseSpaceMember.user_id == user_id
    ).first()
    return member.role if member else None


def _can_manage_space(db: Session, space: models.CourseSpace, user: models.User) -> bool:
    """Return True if user is space creator, space tutor/manager, org owner/admin/teacher, linked course creator, or platform admin."""
    if not space or not user:
        return False
    if space.created_by == user.id:
        return True
    role_val = str(getattr(user, "role", "")).upper()
    if "ADMIN" in role_val or getattr(user, "is_admin", False):
        return True
    space_role = _get_user_space_role(db, space.id, user.id)
    if space_role in ("tutor", "manager", "creator"):
        return True
    if space.organisation_id:
        org = db.query(models.Organisation).filter(models.Organisation.id == space.organisation_id).first()
        if org and org.owner_id == user.id:
            return True
        org_mem = db.query(models.OrganisationMember).filter(
            models.OrganisationMember.organisation_id == space.organisation_id,
            models.OrganisationMember.user_id == user.id
        ).first()
        if org_mem and str(org_mem.role or "").upper() in ("OWNER", "ADMIN", "TEACHER", "STAFF", "INSTRUCTOR"):
            return True
    linked_creator = (
        db.query(models.CourseSpaceCourse)
        .join(models.Course, models.Course.id == models.CourseSpaceCourse.course_id)
        .filter(
            models.CourseSpaceCourse.space_id == space.id,
            models.Course.admin_id == user.id
        )
        .first()
    )
    if linked_creator:
        return True
    return False


def _check_space_tutor_permission(db: Session, space: models.CourseSpace, user: models.User):
    """Enforce that the user has tutor/management permissions in the space."""
    if _can_manage_space(db, space, user):
        return True
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only tutors, course owners, or organisation admins can perform this action.")


def _dispatch_space_notifications(
    db: Session,
    space: models.CourseSpace,
    sender: models.User,
    title: str,
    body: str,
    action_route: str,
    payload: Dict[str, Any]
):
    """Deliver in-app notifications to all space members except the sender."""
    members = db.query(models.CourseSpaceMember).filter(
        models.CourseSpaceMember.space_id == space.id,
        models.CourseSpaceMember.user_id != sender.id
    ).all()

    for m in members:
        notif = models.UserNotification(
            user_id=m.user_id,
            sender_id=sender.id,
            title=title,
            body=body,
            category="course_spaces",
            action_route=action_route,
            data_payload=payload
        )
        db.add(notif)


def _format_space_summary(db: Session, sp: models.CourseSpace, user: models.User, user_mem: Optional[models.CourseSpaceMember] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Build a consistent summary dictionary for CourseSpace cards across My Spaces, Explore, and Org tabs."""
    if now is None:
        now = datetime.now(timezone.utc)
    if user_mem is None:
        user_mem = db.query(models.CourseSpaceMember).filter(
            models.CourseSpaceMember.space_id == sp.id,
            models.CourseSpaceMember.user_id == user.id
        ).first()

    c_count = len(sp.courses or [])
    m_count = db.query(func.count(models.CourseSpaceMaterial.id)).filter(models.CourseSpaceMaterial.space_id == sp.id).scalar() or 0
    q_count = db.query(func.count(models.CourseSpaceQuiz.id)).filter(models.CourseSpaceQuiz.space_id == sp.id).scalar() or 0
    s_count = db.query(func.count(models.CourseSpaceSession.id)).filter(
        models.CourseSpaceSession.space_id == sp.id,
        models.CourseSpaceSession.start_time >= now,
        models.CourseSpaceSession.is_cancelled == False
    ).scalar() or 0
    mem_count = db.query(func.count(models.CourseSpaceMember.id)).filter(models.CourseSpaceMember.space_id == sp.id).scalar() or 0

    creator_name = f"{sp.creator.first_name} {sp.creator.last_name}".strip() if sp.creator else "Instructor"
    creator_avatar = getattr(sp.creator, "profile_picture_url", None) if sp.creator else None
    can_manage = _can_manage_space(db, sp, user)
    linked_courses = []
    for sc in (sp.courses or []):
        c_obj = getattr(sc, "course", None)
        if c_obj:
            linked_courses.append({
                "id": str(c_obj.id),
                "title": _course_title(c_obj)
            })

    return {
        "id": str(sp.id),
        "name": sp.name,
        "description": sp.description,
        "code": sp.code,
        "is_open": sp.is_open,
        "banner_url": sp.banner_url,
        "organisation_id": str(sp.organisation_id) if sp.organisation_id else None,
        "role": user_mem.role if user_mem else ("tutor" if can_manage else None),
        "is_enrolled": bool(user_mem) or sp.created_by == user.id,
        "is_creator": sp.created_by == user.id,
        "can_manage": can_manage,
        "creator_name": creator_name,
        "creator_avatar": creator_avatar,
        "member_count": mem_count,
        "course_count": c_count,
        "courses": linked_courses,
        "material_count": m_count,
        "quiz_count": q_count,
        "upcoming_session_count": s_count,
        "joined_at": user_mem.joined_at.isoformat() if (user_mem and user_mem.joined_at) else None,
        "created_at": sp.created_at.isoformat() if sp.created_at else None
    }


# =========================================================================
# 1. COURSE SPACES: CRUD & ENROLLMENT
# =========================================================================

@router.post("", status_code=status.HTTP_201_CREATED)
def create_course_space(
    payload: CreateCourseSpaceRequest,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """Create a new Course Space (hub) for pre-existing or new courses."""
    if payload.organisation_id:
        org = db.query(models.Organisation).filter(models.Organisation.id == payload.organisation_id).first()
        if not org:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found.")
        is_org_owner = org.owner_id == user.id
        is_plat_admin = "ADMIN" in str(getattr(user, "role", "")).upper() or getattr(user, "is_admin", False)
        org_mem = db.query(models.OrganisationMember).filter(
            models.OrganisationMember.organisation_id == payload.organisation_id,
            models.OrganisationMember.user_id == user.id
        ).first()
        is_org_staff = org_mem and str(org_mem.role or "").upper() in ("OWNER", "ADMIN", "TEACHER", "STAFF", "INSTRUCTOR")
        if not (is_org_owner or is_plat_admin or is_org_staff):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only organisation admins or instructors can create an organisation Course Space.")

    code = _generate_space_code(db)
    space = models.CourseSpace(
        name=payload.name.strip(),
        description=payload.description.strip() if payload.description else None,
        code=code,
        is_open=payload.is_open,
        banner_url=payload.banner_url,
        organisation_id=payload.organisation_id,
        created_by=user.id
    )
    db.add(space)
    db.flush()

    # Creator is registered as tutor
    tutor_member = models.CourseSpaceMember(
        space_id=space.id,
        user_id=user.id,
        role="tutor"
    )
    db.add(tutor_member)

    # Attach initial courses if provided
    attached_courses = 0
    if payload.course_ids:
        seen_c_ids = set()
        for idx, c_id in enumerate(payload.course_ids):
            if c_id in seen_c_ids:
                continue
            seen_c_ids.add(c_id)
            course = db.query(models.Course).filter(models.Course.id == c_id).first()
            if course:
                link = models.CourseSpaceCourse(
                    space_id=space.id,
                    course_id=c_id,
                    order_index=idx
                )
                db.add(link)
                attached_courses += 1

    db.commit()
    db.refresh(space)

    return {
        "status": "success",
        "message": "Course Space created successfully.",
        "space": {
            "id": str(space.id),
            "name": space.name,
            "description": space.description,
            "code": space.code,
            "is_open": space.is_open,
            "banner_url": space.banner_url,
            "organisation_id": str(space.organisation_id) if space.organisation_id else None,
            "created_by": str(space.created_by),
            "course_count": attached_courses,
            "created_at": space.created_at.isoformat() if space.created_at else None
        }
    }


@router.get("/my-spaces")
def get_my_spaces(
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """List all spaces that the current user has joined or created."""
    memberships = (
        db.query(models.CourseSpaceMember)
        .filter(models.CourseSpaceMember.user_id == user.id)
        .options(
            joinedload(models.CourseSpaceMember.space)
            .joinedload(models.CourseSpace.creator),
            joinedload(models.CourseSpaceMember.space)
            .joinedload(models.CourseSpace.courses)
            .joinedload(models.CourseSpaceCourse.course)
        )
        .order_by(models.CourseSpaceMember.joined_at.desc())
        .all()
    )

    now = datetime.now(timezone.utc)
    results = []
    seen_ids = set()
    for m in memberships:
        sp = m.space
        if not sp or sp.id in seen_ids:
            continue
        seen_ids.add(sp.id)
        results.append(_format_space_summary(db, sp, user, user_mem=m, now=now))

    return {"status": "success", "spaces": results}


@router.get("/organisation/{org_id}")
def get_organisation_spaces(
    org_id: uuid.UUID,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """List all Course Spaces linked to a specific organisation."""
    spaces = (
        db.query(models.CourseSpace)
        .filter(models.CourseSpace.organisation_id == org_id)
        .options(
            joinedload(models.CourseSpace.creator),
            joinedload(models.CourseSpace.courses).joinedload(models.CourseSpaceCourse.course)
        )
        .order_by(models.CourseSpace.created_at.desc())
        .all()
    )

    now = datetime.now(timezone.utc)
    results = [_format_space_summary(db, sp, user, now=now) for sp in spaces]
    return {"status": "success", "spaces": results}


@router.get("/explore")
def explore_open_spaces(
    query: Optional[str] = None,
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """Explore public/open spaces that any learner can enroll into."""
    q = db.query(models.CourseSpace).filter(models.CourseSpace.is_open == True)
    if query:
        q = q.filter(or_(
            models.CourseSpace.name.ilike(f"%{query}%"),
            models.CourseSpace.description.ilike(f"%{query}%"),
            models.CourseSpace.code.ilike(f"%{query}%")
        ))

    spaces = (
        q.options(
            joinedload(models.CourseSpace.creator),
            joinedload(models.CourseSpace.courses).joinedload(models.CourseSpaceCourse.course)
        )
        .order_by(models.CourseSpace.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    now = datetime.now(timezone.utc)
    results = [_format_space_summary(db, sp, user, now=now) for sp in spaces]
    return {"status": "success", "spaces": results}


@router.post("/join")
def join_course_space(
    payload: JoinSpaceRequest,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """
    Join a course space via its invite code or via direct enrollment if the space is open.
    Automatically enrolls the student into all linked courses in the space!
    """
    space = None
    if payload.code:
        clean_code = payload.code.strip().upper()
        space = db.query(models.CourseSpace).filter(models.CourseSpace.code == clean_code).first()
        if not space:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid space code. Please check and try again.")
    elif payload.space_id:
        space = db.query(models.CourseSpace).filter(models.CourseSpace.id == payload.space_id).first()
        if not space:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course Space not found.")
        can_manage = _can_manage_space(db, space, user)
        if not space.is_open and not can_manage:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This Course Space is private. An invite code is required to join.")
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Either 'code' or 'space_id' must be provided.")

    # Check if already a member
    existing_member = db.query(models.CourseSpaceMember).filter(
        models.CourseSpaceMember.space_id == space.id,
        models.CourseSpaceMember.user_id == user.id
    ).first()

    if not existing_member:
        default_role = "tutor" if _can_manage_space(db, space, user) else "student"
        membership = models.CourseSpaceMember(
            space_id=space.id,
            user_id=user.id,
            role=default_role
        )
        db.add(membership)

    # Cascading auto-enrollment into all linked courses
    space_courses = db.query(models.CourseSpaceCourse).filter(models.CourseSpaceCourse.space_id == space.id).all()
    enrolled_courses_count = 0
    for sc in space_courses:
        existing_enr = db.query(models.Enrollment).filter(
            models.Enrollment.student_id == user.id,
            models.Enrollment.course_id == sc.course_id
        ).first()
        if not existing_enr:
            new_enr = models.Enrollment(
                student_id=user.id,
                course_id=sc.course_id,
                progress=0.0
            )
            db.add(new_enr)
            enrolled_courses_count += 1

    db.commit()

    return {
        "status": "success",
        "message": f"Successfully joined {space.name}!",
        "space_id": str(space.id),
        "name": space.name,
        "already_member": bool(existing_member),
        "courses_auto_enrolled": enrolled_courses_count
    }


@router.get("/{space_id}")
def get_space_details(
    space_id: uuid.UUID,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """Get full details of a Course Space including linked courses, modules, and user permissions."""
    space = (
        db.query(models.CourseSpace)
        .filter(models.CourseSpace.id == space_id)
        .options(
            joinedload(models.CourseSpace.creator),
            joinedload(models.CourseSpace.courses).joinedload(models.CourseSpaceCourse.course)
        )
        .first()
    )
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course Space not found.")

    role = _get_user_space_role(db, space.id, user.id)
    is_creator = space.created_by == user.id
    can_manage = _can_manage_space(db, space, user)
    is_member = role is not None or is_creator or can_manage

    # Check access permission: public spaces, members, or space managers can view
    if not space.is_open and not is_member:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You must join this Space to view its contents.")

    # Format linked courses with module list, module count & student progress
    courses_data = []
    for sc in sorted(space.courses or [], key=lambda x: getattr(x, "order_index", 0) or 0):
        c = sc.course
        if not c:
            continue
        modules = (
            db.query(models.Module)
            .filter(models.Module.course_id == c.id)
            .order_by(models.Module.order_index.asc())
            .all()
        )
        modules_list = [
            {
                "id": str(mod.id),
                "title": mod.title,
                "order": getattr(mod, "order_index", idx)
            }
            for idx, mod in enumerate(modules)
        ]
        enr = db.query(models.Enrollment).filter(
            models.Enrollment.student_id == user.id,
            models.Enrollment.course_id == c.id
        ).first()

        courses_data.append({
            "id": str(c.id),
            "title": _course_title(c),
            "description": c.description,
            "thumbnail_url": getattr(c, "image_url", None) or getattr(c, "course_banner", None),
            "module_count": len(modules_list),
            "modules": modules_list,
            "is_enrolled": enr is not None,
            "progress": enr.progress if enr else 0.0,
            "order_index": sc.order_index
        })

    now = datetime.now(timezone.utc)
    m_count = db.query(func.count(models.CourseSpaceMaterial.id)).filter(models.CourseSpaceMaterial.space_id == space.id).scalar() or 0
    q_count = db.query(func.count(models.CourseSpaceQuiz.id)).filter(models.CourseSpaceQuiz.space_id == space.id).scalar() or 0
    s_count = db.query(func.count(models.CourseSpaceSession.id)).filter(
        models.CourseSpaceSession.space_id == space.id,
        models.CourseSpaceSession.start_time >= now,
        models.CourseSpaceSession.is_cancelled == False
    ).scalar() or 0
    mem_count = db.query(func.count(models.CourseSpaceMember.id)).filter(models.CourseSpaceMember.space_id == space.id).scalar() or 0

    creator_name = f"{space.creator.first_name} {space.creator.last_name}".strip() if space.creator else "Instructor"
    creator_avatar = getattr(space.creator, "profile_picture_url", None) if space.creator else None

    return {
        "status": "success",
        "space": {
            "id": str(space.id),
            "name": space.name,
            "description": space.description,
            "code": space.code,
            "is_open": space.is_open,
            "banner_url": space.banner_url,
            "organisation_id": str(space.organisation_id) if space.organisation_id else None,
            "creator_id": str(space.created_by),
            "creator_name": creator_name,
            "creator_avatar": creator_avatar,
            "role": "creator" if is_creator else (role or ("tutor" if can_manage else "guest")),
            "is_member": is_member,
            "is_creator": is_creator,
            "can_manage": can_manage,
            "member_count": mem_count,
            "material_count": m_count,
            "quiz_count": q_count,
            "upcoming_session_count": s_count,
            "courses": courses_data,
            "created_at": space.created_at.isoformat() if space.created_at else None
        }
    }


@router.patch("/{space_id}")
def update_course_space(
    space_id: uuid.UUID,
    payload: UpdateCourseSpaceRequest,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """(Tutor/Creator/Admin only) Update Course Space settings (name, description, is_open, banner_url)."""
    space = db.query(models.CourseSpace).filter(models.CourseSpace.id == space_id).first()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course Space not found.")
    _check_space_tutor_permission(db, space, user)

    if payload.name is not None and payload.name.strip():
        space.name = payload.name.strip()
    if payload.description is not None:
        space.description = payload.description.strip() if payload.description.strip() else None
    if payload.is_open is not None:
        space.is_open = payload.is_open
    if payload.banner_url is not None:
        space.banner_url = payload.banner_url.strip() if payload.banner_url.strip() else None

    db.commit()
    db.refresh(space)
    return {
        "status": "success",
        "message": "Course Space updated successfully.",
        "space": {
            "id": str(space.id),
            "name": space.name,
            "description": space.description,
            "code": space.code,
            "is_open": space.is_open,
            "banner_url": space.banner_url
        }
    }


@router.delete("/{space_id}")
def delete_course_space(
    space_id: uuid.UUID,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """(Creator/Org Admin/Platform Admin) Delete a Course Space."""
    space = db.query(models.CourseSpace).filter(models.CourseSpace.id == space_id).first()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course Space not found.")
    _check_space_tutor_permission(db, space, user)

    db.delete(space)
    db.commit()
    return {"status": "success", "message": "Course Space deleted successfully."}


@router.post("/{space_id}/regenerate-code")
def regenerate_space_code(
    space_id: uuid.UUID,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """(Tutor/Creator/Admin only) Regenerate a new unique invite code for the Space."""
    space = db.query(models.CourseSpace).filter(models.CourseSpace.id == space_id).first()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course Space not found.")
    _check_space_tutor_permission(db, space, user)

    space.code = _generate_space_code(db)
    db.commit()
    return {"status": "success", "code": space.code, "message": "Invite code regenerated."}


@router.post("/{space_id}/leave")
def leave_course_space(
    space_id: uuid.UUID,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """Allow a student or non-creator member to leave a Course Space."""
    space = db.query(models.CourseSpace).filter(models.CourseSpace.id == space_id).first()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course Space not found.")
    if space.created_by == user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Space creators cannot leave their own space. You can delete the space instead.")

    mem = db.query(models.CourseSpaceMember).filter(
        models.CourseSpaceMember.space_id == space.id,
        models.CourseSpaceMember.user_id == user.id
    ).first()
    if not mem:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="You are not a member of this space.")

    db.delete(mem)
    db.commit()
    return {"status": "success", "message": f"You have left {space.name}."}


@router.get("/{space_id}/members")
def list_space_members(
    space_id: uuid.UUID,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """List all members and tutors in a Course Space."""
    space = db.query(models.CourseSpace).filter(models.CourseSpace.id == space_id).first()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course Space not found.")

    role = _get_user_space_role(db, space.id, user.id)
    can_manage = _can_manage_space(db, space, user)
    if not space.is_open and role is None and not can_manage:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Join this space to view members.")

    members = (
        db.query(models.CourseSpaceMember)
        .filter(models.CourseSpaceMember.space_id == space.id)
        .options(joinedload(models.CourseSpaceMember.user))
        .order_by(models.CourseSpaceMember.joined_at.asc())
        .all()
    )

    results = []
    for m in members:
        u = m.user
        if not u:
            continue
        full_name = f"{u.first_name or ''} {u.last_name or ''}".strip() or getattr(u, "username", "Learner")
        results.append({
            "id": str(m.id),
            "user_id": str(u.id),
            "name": full_name,
            "username": getattr(u, "username", None),
            "email": u.email if can_manage else None,
            "avatar_url": getattr(u, "profile_picture_url", None),
            "role": "creator" if space.created_by == u.id else m.role,
            "is_creator": space.created_by == u.id,
            "joined_at": m.joined_at.isoformat() if m.joined_at else None
        })

    return {"status": "success", "members": results, "can_manage": can_manage}


@router.patch("/{space_id}/members/{member_user_id}")
def update_space_member_role(
    space_id: uuid.UUID,
    member_user_id: uuid.UUID,
    payload: UpdateSpaceMemberRoleRequest,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """(Tutor/Creator/Admin only) Promote a member to co-tutor or demote to student."""
    space = db.query(models.CourseSpace).filter(models.CourseSpace.id == space_id).first()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course Space not found.")
    _check_space_tutor_permission(db, space, user)

    if member_user_id == space.created_by:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot modify the role of the Space creator.")

    mem = db.query(models.CourseSpaceMember).filter(
        models.CourseSpaceMember.space_id == space.id,
        models.CourseSpaceMember.user_id == member_user_id
    ).first()
    if not mem:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found in this space.")

    mem.role = payload.role
    db.commit()
    return {"status": "success", "message": f"Member role updated to {payload.role}.", "role": mem.role}


@router.delete("/{space_id}/members/{member_user_id}")
def remove_space_member(
    space_id: uuid.UUID,
    member_user_id: uuid.UUID,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """(Tutor/Creator/Admin only) Remove a member from the Course Space."""
    space = db.query(models.CourseSpace).filter(models.CourseSpace.id == space_id).first()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course Space not found.")
    _check_space_tutor_permission(db, space, user)

    if member_user_id == space.created_by:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot remove the Space creator.")

    mem = db.query(models.CourseSpaceMember).filter(
        models.CourseSpaceMember.space_id == space.id,
        models.CourseSpaceMember.user_id == member_user_id
    ).first()
    if not mem:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found in this space.")

    db.delete(mem)
    db.commit()
    return {"status": "success", "message": "Member removed from space."}


@router.post("/{space_id}/courses")
def add_courses_to_space(
    space_id: uuid.UUID,
    payload: AddCoursesToSpaceRequest,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """
    (Tutor/Creator/Admin only) Link existing courses to the Space and retroactively
    auto-enroll all current Space members into the newly added courses.
    """
    space = db.query(models.CourseSpace).filter(models.CourseSpace.id == space_id).first()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course Space not found.")
    _check_space_tutor_permission(db, space, user)

    added_count = 0
    added_course_Ids = []
    added_course_titles = []
    highest_order = (
        db.query(func.max(models.CourseSpaceCourse.order_index))
        .filter(models.CourseSpaceCourse.space_id == space.id)
        .scalar() or 0
    )

    for c_id in payload.course_ids:
        course = db.query(models.Course).filter(models.Course.id == c_id).first()
        if not course:
            continue
        existing = db.query(models.CourseSpaceCourse).filter(
            models.CourseSpaceCourse.space_id == space.id,
            models.CourseSpaceCourse.course_id == c_id
        ).first()
        if not existing:
            highest_order += 1
            sc = models.CourseSpaceCourse(
                space_id=space.id,
                course_id=c_id,
                order_index=highest_order
            )
            db.add(sc)
            added_count += 1
            added_course_Ids.append(c_id)
            added_course_titles.append(_course_title(course))

    # Retroactively enroll all existing space members into the newly linked courses
    if added_course_Ids:
        members = db.query(models.CourseSpaceMember).filter(models.CourseSpaceMember.space_id == space.id).all()
        for m in members:
            for c_id in added_course_Ids:
                existing_enr = db.query(models.Enrollment).filter(
                    models.Enrollment.student_id == m.user_id,
                    models.Enrollment.course_id == c_id
                ).first()
                if not existing_enr:
                    db.add(models.Enrollment(
                        student_id=m.user_id,
                        course_id=c_id,
                        progress=0.0
                    ))

        _dispatch_space_notifications(
            db=db,
            space=space,
            sender=user,
            title=f"New Course Added to {space.name}",
            body=f"{', '.join(added_course_titles[:2])} has been linked to {space.name} and added to your learning dashboard.",
            action_route=f"/spaces/{space.id}?tab=courses",
            payload={
                "space_id": str(space.id),
                "type": "course_linked"
            }
        )

    db.commit()
    return {"status": "success", "message": f"Added {added_count} course(s) to the space.", "added_count": added_count}


@router.delete("/{space_id}/courses/{course_id}")
def remove_course_from_space(
    space_id: uuid.UUID,
    course_id: uuid.UUID,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """(Tutor/Creator/Admin only) Unlink a course from the space."""
    space = db.query(models.CourseSpace).filter(models.CourseSpace.id == space_id).first()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course Space not found.")
    _check_space_tutor_permission(db, space, user)

    link = db.query(models.CourseSpaceCourse).filter(
        models.CourseSpaceCourse.space_id == space.id,
        models.CourseSpaceCourse.course_id == course_id
    ).first()
    if not link:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course is not linked to this Space.")

    db.delete(link)
    db.commit()
    return {"status": "success", "message": "Course removed from space."}


# =========================================================================
# 2. ASYNC MATERIALS VAULT: UPLOADS & DOWNLOADS
# =========================================================================

@router.post("/{space_id}/materials", status_code=status.HTTP_201_CREATED)
async def upload_space_material(
    space_id: uuid.UUID,
    file: UploadFile = File(...),
    course_id: uuid.UUID = Form(...),
    module_id: Optional[uuid.UUID] = Form(None),
    title: str = Form(...),
    description: Optional[str] = Form(None),
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """
    (Tutors/Creators only) Upload asynchronous learning materials & resources.
    Files are uploaded directly to BunnyCDN, tied to a specific course for segmentation,
    and trigger real-time notifications to all enrolled students in the Space.
    """
    space = db.query(models.CourseSpace).filter(models.CourseSpace.id == space_id).first()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course Space not found.")
    _check_space_tutor_permission(db, space, user)

    # Verify course belongs to space
    sc_link = db.query(models.CourseSpaceCourse).filter(
        models.CourseSpaceCourse.space_id == space.id,
        models.CourseSpaceCourse.course_id == course_id
    ).first()
    if not sc_link:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Selected course is not linked to this Space.")

    course = db.query(models.Course).filter(models.Course.id == course_id).first()
    course_title = _course_title(course)

    # Read bytes and validate file
    content = await file.read()
    file_size = len(content)
    if file_size == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot upload an empty file.")

    # 100MB limit for course materials
    if file_size > 100 * 1024 * 1024:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File size exceeds the 100MB limit.")

    original_filename = file.filename or "resource"
    clean_filename = re.sub(r"[^a-zA-Z0-9_.-]", "_", original_filename)
    safe_filename = f"{uuid.uuid4().hex[:8]}_{clean_filename}"

    # Push to BunnyCDN
    folder_path = f"spaces/{space_id}/materials"
    try:
        cdn_url = await upload_bytes_to_bunny(content, safe_filename, folder_path)
    except Exception as ex:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to store file on CDN: {str(ex)}")

    material = models.CourseSpaceMaterial(
        space_id=space.id,
        course_id=course_id,
        module_id=module_id,
        title=title.strip(),
        description=description.strip() if description else None,
        file_url=cdn_url,
        file_name=original_filename,
        file_size_bytes=file_size,
        mime_type=file.content_type or "application/octet-stream",
        uploaded_by=user.id
    )
    db.add(material)
    db.flush()

    # Dispatch notifications to learners in the space
    uploader_name = f"{user.first_name} {user.last_name}".strip()
    _dispatch_space_notifications(
        db=db,
        space=space,
        sender=user,
        title=f"New Resource in {space.name}",
        body=f"{uploader_name} uploaded '{title}' for {course_title}.",
        action_route=f"/spaces/{space_id}?tab=materials&course={course_id}",
        payload={
            "space_id": str(space_id),
            "course_id": str(course_id),
            "material_id": str(material.id),
            "type": "material_upload"
        }
    )

    db.commit()
    db.refresh(material)

    return {
        "status": "success",
        "message": "Resource material uploaded and students notified.",
        "material": {
            "id": str(material.id),
            "title": material.title,
            "description": material.description,
            "file_url": material.file_url,
            "file_name": material.file_name,
            "file_size_bytes": material.file_size_bytes,
            "mime_type": material.mime_type,
            "course_id": str(material.course_id),
            "module_id": str(material.module_id) if material.module_id else None,
            "created_at": material.created_at.isoformat() if material.created_at else None
        }
    }


@router.get("/{space_id}/materials")
def list_space_materials(
    space_id: uuid.UUID,
    course_id: Optional[uuid.UUID] = None,
    module_id: Optional[uuid.UUID] = None,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """
    List materials in the Space, segmented by course and optional module.
    Includes uploader details, relative timestamps, and file metadata.
    """
    space = db.query(models.CourseSpace).filter(models.CourseSpace.id == space_id).first()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course Space not found.")

    query = (
        db.query(models.CourseSpaceMaterial)
        .filter(models.CourseSpaceMaterial.space_id == space_id)
        .options(
            joinedload(models.CourseSpaceMaterial.uploader),
            joinedload(models.CourseSpaceMaterial.course),
            joinedload(models.CourseSpaceMaterial.module)
        )
    )

    if course_id:
        query = query.filter(models.CourseSpaceMaterial.course_id == course_id)
    if module_id:
        query = query.filter(models.CourseSpaceMaterial.module_id == module_id)

    materials = query.order_by(desc(models.CourseSpaceMaterial.created_at)).all()

    results = []
    for m in materials:
        uploader_name = f"{m.uploader.first_name} {m.uploader.last_name}" if m.uploader else "Tutor"
        uploader_avatar = getattr(m.uploader, "profile_picture_url", None) if m.uploader else None

        results.append({
            "id": str(m.id),
            "title": m.title,
            "description": m.description,
            "file_url": m.file_url,
            "file_name": m.file_name,
            "file_size_bytes": m.file_size_bytes,
            "mime_type": m.mime_type,
            "course_id": str(m.course_id),
            "course_title": _course_title(m.course),
            "module_id": str(m.module_id) if m.module_id else None,
            "module_title": m.module.title if m.module else None,
            "uploaded_by_name": uploader_name,
            "uploaded_by_avatar": uploader_avatar,
            "download_count": m.download_count,
            "created_at": m.created_at.isoformat() if m.created_at else None
        })

    return {"status": "success", "materials": results}


@router.delete("/{space_id}/materials/{material_id}")
def delete_space_material(
    space_id: uuid.UUID,
    material_id: uuid.UUID,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """(Tutors/Creators or uploader only) Delete a resource material."""
    space = db.query(models.CourseSpace).filter(models.CourseSpace.id == space_id).first()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course Space not found.")

    material = db.query(models.CourseSpaceMaterial).filter(
        models.CourseSpaceMaterial.id == material_id,
        models.CourseSpaceMaterial.space_id == space_id
    ).first()
    if not material:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Material not found.")

    # Check permission
    is_uploader = material.uploaded_by == user.id
    if not (is_uploader or _can_manage_space(db, space, user)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied to delete this material.")

    db.delete(material)
    db.commit()
    return {"status": "success", "message": "Material deleted successfully."}


# =========================================================================
# 3. TOPIC-ALIGNED QUIZZES, CBT AI INGESTION & FORMATIVE DRILLS
# =========================================================================

def _normalize_quiz_questions(questions: List[CreateQuizQuestion]) -> List[Dict[str, Any]]:
    if not questions:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Quiz must contain at least one question.")

    formatted_questions = []
    for idx, q in enumerate(questions):
        opts = [str(opt).strip() for opt in (q.options or []) if str(opt).strip()]
        if len(opts) < 2:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Question {idx+1} must have at least 2 non-empty options.")

        ans_idx = q.correct_index if q.correct_index is not None else (q.answer_index if q.answer_index is not None else 0)
        if ans_idx < 0 or ans_idx >= len(opts):
            ans_idx = 0

        formatted_questions.append({
            "id": q.id or f"q_{idx+1}_{uuid.uuid4().hex[:6]}",
            "question_text": (q.question_text or "").strip(),
            "scenario_text": q.scenario_text.strip() if q.scenario_text else None,
            "options": opts,
            "correct_index": ans_idx,
            "answer_index": ans_idx,
            "explanation": q.explanation.strip() if q.explanation else None,
            "points": float(q.points) if (q.points and q.points > 0) else 1.0,
            "topic": q.topic.strip() if q.topic else "General",
            "difficulty": q.difficulty or "standard",
            "has_diagram": bool(q.has_diagram or q.image_url),
            "diagram_placeholder": q.diagram_placeholder,
            "image_url": q.image_url.strip() if q.image_url else None
        })
    return formatted_questions


@router.post("/{space_id}/quizzes/ai-parse-text")
async def ai_parse_space_quiz_text(
    space_id: uuid.UUID,
    payload: AISpaceQuizParseTextRequest,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """
    (Tutor/Creator/Admin only) Use AI to either extract past questions or generate new CBT
    questions from raw text / syllabus / lecture notes for a Course Space drill.
    """
    space = db.query(models.CourseSpace).filter(models.CourseSpace.id == space_id).first()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course Space not found.")
    _check_space_tutor_permission(db, space, user)

    from services.cbt_parser_service import (
        parse_past_questions_with_ai,
        generate_cbt_questions_from_material_with_ai
    )

    mode = (payload.mode or "auto").lower().strip()
    raw_text = payload.raw_text.strip()

    if mode == "generate":
        parsed = await generate_cbt_questions_from_material_with_ai(
            raw_text=raw_text,
            subject=payload.subject,
            exam_type=payload.exam_type,
            question_count=payload.question_count,
            difficulty=payload.difficulty
        )
    elif mode == "extract":
        parsed = await parse_past_questions_with_ai(
            raw_text=raw_text,
            subject=payload.subject,
            exam_type=payload.exam_type,
            exam_year=payload.exam_year
        )
        if not parsed.questions:
            parsed = await generate_cbt_questions_from_material_with_ai(
                raw_text=raw_text,
                subject=payload.subject,
                exam_type=payload.exam_type,
                question_count=payload.question_count,
                difficulty=payload.difficulty
            )
    else:
        # Auto mode: check if input text has numbered questions with A/B/C/D options
        has_option_markers = bool(re.search(r"(?m)^\s*\(?[A-Da-d][\.\)\]\:\-]\s+.+", raw_text))
        if has_option_markers:
            parsed = await parse_past_questions_with_ai(
                raw_text=raw_text,
                subject=payload.subject,
                exam_type=payload.exam_type,
                exam_year=payload.exam_year
            )
            if not parsed.questions:
                parsed = await generate_cbt_questions_from_material_with_ai(
                    raw_text=raw_text,
                    subject=payload.subject,
                    exam_type=payload.exam_type,
                    question_count=payload.question_count,
                    difficulty=payload.difficulty
                )
        else:
            parsed = await generate_cbt_questions_from_material_with_ai(
                raw_text=raw_text,
                subject=payload.subject,
                exam_type=payload.exam_type,
                question_count=payload.question_count,
                difficulty=payload.difficulty
            )

    return {
        "status": "success",
        "success": True,
        "suggested_title": parsed.suggested_title,
        "subject": parsed.subject,
        "exam_type": parsed.exam_type,
        "exam_year": parsed.exam_year,
        "syllabus_topics": parsed.syllabus_topics,
        "questions": [q.model_dump() for q in parsed.questions],
        "questions_count": len(parsed.questions)
    }


@router.post("/{space_id}/quizzes/ai-parse-file")
async def ai_parse_space_quiz_file(
    space_id: uuid.UUID,
    file: UploadFile = File(...),
    subject: str = Form("General"),
    exam_type: str = Form("Topic Drill CBT"),
    exam_year: Optional[str] = Form(None),
    mode: str = Form("auto"),
    question_count: int = Form(10),
    difficulty: str = Form("standard"),
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """
    (Tutor/Creator/Admin only) Upload a document (PDF, DOCX, TXT, MD, or Image) to extract
    existing past questions or generate new CBT drill questions for the Course Space.
    """
    space = db.query(models.CourseSpace).filter(models.CourseSpace.id == space_id).first()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course Space not found.")
    _check_space_tutor_permission(db, space, user)

    from services.cbt_parser_service import (
        parse_past_questions_with_ai,
        parse_past_questions_from_image_with_ai,
        generate_cbt_questions_from_material_with_ai
    )

    filename = (file.filename or "").lower()
    file_bytes = await file.read()
    content_text = ""
    parsed_year = int(exam_year) if (exam_year and str(exam_year).strip().isdigit()) else None

    # 1. Handle image files (scanned past question photos or diagrams)
    is_img = any(filename.endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp"])
    if is_img:
        mime = "image/png" if filename.endswith(".png") else "image/jpeg"
        parsed = await parse_past_questions_from_image_with_ai(
            image_bytes=file_bytes,
            mime_type=mime,
            subject=subject,
            exam_type=exam_type,
            exam_year=parsed_year
        )
        return {
            "status": "success",
            "success": True,
            "suggested_title": parsed.suggested_title,
            "subject": parsed.subject,
            "exam_type": parsed.exam_type,
            "exam_year": parsed.exam_year,
            "syllabus_topics": parsed.syllabus_topics,
            "questions": [q.model_dump() for q in parsed.questions],
            "questions_count": len(parsed.questions),
            "extracted_char_count": 0,
        }

    # 2. Handle PDF documents
    if filename.endswith(".pdf"):
        try:
            import fitz
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            for page in doc:
                content_text += page.get_text() + "\n"

            if len(content_text.strip()) < 30 and len(doc) > 0:
                all_scanned_questions = []
                final_title = f"{exam_type} {parsed_year or ''} {subject} Drill".strip()
                all_topics = set()
                max_scanned_pages = min(len(doc), 20)
                for page_idx in range(max_scanned_pages):
                    try:
                        pix = doc[page_idx].get_pixmap(dpi=150)
                        img_bytes = pix.tobytes("png")
                        p_parsed = await parse_past_questions_from_image_with_ai(
                            image_bytes=img_bytes,
                            mime_type="image/png",
                            subject=subject,
                            exam_type=exam_type,
                            exam_year=parsed_year
                        )
                        if p_parsed.questions:
                            all_scanned_questions.extend(p_parsed.questions)
                        if p_parsed.suggested_title and not final_title:
                            final_title = p_parsed.suggested_title
                        all_topics.update(p_parsed.syllabus_topics)
                    except Exception as err:
                        print(f"[Space CBT PDF SCAN] Error parsing page {page_idx + 1}: {err}")

                doc.close()
                return {
                    "status": "success",
                    "success": True,
                    "suggested_title": final_title,
                    "subject": subject,
                    "exam_type": exam_type,
                    "exam_year": parsed_year,
                    "syllabus_topics": list(all_topics) or [subject],
                    "questions": [q.model_dump() for q in all_scanned_questions],
                    "questions_count": len(all_scanned_questions),
                    "extracted_char_count": 0,
                }
            doc.close()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to extract text from PDF: {e}")

    # 3. Handle Word documents (.docx)
    elif filename.endswith(".docx"):
        try:
            import zipfile
            import xml.etree.ElementTree as ET
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                xml_content = z.read("word/document.xml")
                tree = ET.fromstring(xml_content)
                texts = []
                for elem in tree.iter():
                    if elem.tag.endswith('}t'):
                        if elem.text:
                            texts.append(elem.text)
                    elif elem.tag.endswith('}p'):
                        texts.append('\n')
                content_text = "".join(texts)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to extract text from DOCX: {e}")

    # 4. Handle text/markdown documents
    else:
        content_text = file_bytes.decode("utf-8", errors="ignore")

    content_text = content_text.strip()
    if len(content_text) < 10:
        raise HTTPException(status_code=400, detail="Document text is empty or could not be extracted.")

    mode_clean = (mode or "auto").lower().strip()
    if mode_clean == "generate":
        parsed = await generate_cbt_questions_from_material_with_ai(
            raw_text=content_text,
            subject=subject,
            exam_type=exam_type,
            question_count=question_count,
            difficulty=difficulty
        )
    elif mode_clean == "extract":
        parsed = await parse_past_questions_with_ai(
            raw_text=content_text,
            subject=subject,
            exam_type=exam_type,
            exam_year=parsed_year
        )
        if not parsed.questions:
            parsed = await generate_cbt_questions_from_material_with_ai(
                raw_text=content_text,
                subject=subject,
                exam_type=exam_type,
                question_count=question_count,
                difficulty=difficulty
            )
    else:
        has_option_markers = bool(re.search(r"(?m)^\s*\(?[A-Da-d][\.\)\]\:\-]\s+.+", content_text))
        if has_option_markers:
            parsed = await parse_past_questions_with_ai(
                raw_text=content_text,
                subject=subject,
                exam_type=exam_type,
                exam_year=parsed_year
            )
            if not parsed.questions:
                parsed = await generate_cbt_questions_from_material_with_ai(
                    raw_text=content_text,
                    subject=subject,
                    exam_type=exam_type,
                    question_count=question_count,
                    difficulty=difficulty
                )
        else:
            parsed = await generate_cbt_questions_from_material_with_ai(
                raw_text=content_text,
                subject=subject,
                exam_type=exam_type,
                question_count=question_count,
                difficulty=difficulty
            )

    return {
        "status": "success",
        "success": True,
        "suggested_title": parsed.suggested_title,
        "subject": parsed.subject,
        "exam_type": parsed.exam_type,
        "exam_year": parsed.exam_year,
        "syllabus_topics": parsed.syllabus_topics,
        "questions": [q.model_dump() for q in parsed.questions],
        "questions_count": len(parsed.questions),
        "extracted_char_count": len(content_text),
    }


@router.post("/{space_id}/quizzes/upload-image")
async def upload_space_quiz_image(
    space_id: uuid.UUID,
    file: UploadFile = File(...),
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """
    (Tutor/Creator/Admin only) Upload an inline diagram/figure for a Space quiz question directly to BunnyCDN.
    """
    space = db.query(models.CourseSpace).filter(models.CourseSpace.id == space_id).first()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course Space not found.")
    _check_space_tutor_permission(db, space, user)

    from pathlib import Path
    ext = Path(file.filename).suffix if file.filename else ".png"
    if not ext:
        ext = ".png"
    safe_filename = f"space_diag_{uuid.uuid4().hex[:12]}{ext}"
    file_bytes = await file.read()
    try:
        cdn_url = await upload_bytes_to_bunny(file_bytes, safe_filename, folder_path=f"spaces/{space_id}/quiz_diagrams")
    except Exception as e:
        print(f"[Space Quiz Image Upload] CDN upload error: {e}")
        import os
        upload_dir = os.path.join("static", "uploads", "cbt_diagrams")
        os.makedirs(upload_dir, exist_ok=True)
        local_path = os.path.join(upload_dir, safe_filename)
        with open(local_path, "wb") as f:
            f.write(file_bytes)
        cdn_url = f"/static/uploads/cbt_diagrams/{safe_filename}"

    return {"status": "success", "url": cdn_url, "view_url": cdn_url, "filename": safe_filename}


@router.post("/{space_id}/quizzes", status_code=status.HTTP_201_CREATED)
def create_topic_quiz(
    space_id: uuid.UUID,
    payload: CreateCourseSpaceQuizRequest,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """
    (Tutors/Creators only) Create a topic-aligned practice drill / formative assessment
    tied to a specific course & module. Students can take these in any order at anytime.
    """
    space = db.query(models.CourseSpace).filter(models.CourseSpace.id == space_id).first()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course Space not found.")
    _check_space_tutor_permission(db, space, user)

    # Validate course belongs to space
    sc_link = db.query(models.CourseSpaceCourse).filter(
        models.CourseSpaceCourse.space_id == space.id,
        models.CourseSpaceCourse.course_id == payload.course_id
    ).first()
    if not sc_link:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Selected course is not linked to this Space.")

    course = db.query(models.Course).filter(models.Course.id == payload.course_id).first()
    course_title = _course_title(course)

    formatted_questions = _normalize_quiz_questions(payload.questions)

    quiz = models.CourseSpaceQuiz(
        space_id=space.id,
        course_id=payload.course_id,
        module_id=payload.module_id,
        title=payload.title.strip(),
        description=payload.description.strip() if payload.description else None,
        time_limit_minutes=payload.time_limit_minutes,
        passing_score_percentage=payload.passing_score_percentage,
        questions=formatted_questions,
        created_by=user.id
    )
    db.add(quiz)
    db.flush()

    # Dispatch notification to members
    _dispatch_space_notifications(
        db=db,
        space=space,
        sender=user,
        title=f"New Practice Drill: {payload.title}",
        body=f"A topic-aligned drill ({len(formatted_questions)} questions) for {course_title} is now available in {space.name}.",
        action_route=f"/spaces/{space_id}?tab=quizzes&quiz_id={quiz.id}",
        payload={
            "space_id": str(space_id),
            "course_id": str(payload.course_id),
            "quiz_id": str(quiz.id),
            "type": "quiz_published"
        }
    )

    db.commit()
    db.refresh(quiz)

    return {
        "status": "success",
        "message": "Topic drill published successfully.",
        "quiz": {
            "id": str(quiz.id),
            "title": quiz.title,
            "course_id": str(quiz.course_id),
            "module_id": str(quiz.module_id) if quiz.module_id else None,
            "question_count": len(formatted_questions),
            "time_limit_minutes": quiz.time_limit_minutes,
            "passing_score_percentage": quiz.passing_score_percentage,
            "created_at": quiz.created_at.isoformat() if quiz.created_at else None
        }
    }


@router.get("/{space_id}/quizzes")
def list_space_quizzes(
    space_id: uuid.UUID,
    course_id: Optional[uuid.UUID] = None,
    module_id: Optional[uuid.UUID] = None,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """
    List topic drills in the space, segmented by course & module.
    For the calling learner, includes their best score, pass status, and attempt count.
    """
    space = db.query(models.CourseSpace).filter(models.CourseSpace.id == space_id).first()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course Space not found.")

    query = (
        db.query(models.CourseSpaceQuiz)
        .filter(models.CourseSpaceQuiz.space_id == space_id)
        .options(
            joinedload(models.CourseSpaceQuiz.course),
            joinedload(models.CourseSpaceQuiz.module)
        )
    )

    if course_id:
        query = query.filter(models.CourseSpaceQuiz.course_id == course_id)
    if module_id:
        query = query.filter(models.CourseSpaceQuiz.module_id == module_id)

    quizzes = query.order_by(desc(models.CourseSpaceQuiz.created_at)).all()
    can_manage = _can_manage_space(db, space, user)

    results = []
    for q in quizzes:
        attempts = db.query(models.CourseSpaceQuizAttempt).filter(
            models.CourseSpaceQuizAttempt.quiz_id == q.id,
            models.CourseSpaceQuizAttempt.user_id == user.id
        ).all()

        best_score = max([a.percentage for a in attempts], default=None)
        has_passed = any(a.passed for a in attempts)
        total_attempts = len(attempts)
        last_attempt_at = max([a.completed_at for a in attempts if a.completed_at], default=None)

        all_attempts_count = 0
        if can_manage:
            all_attempts_count = db.query(func.count(models.CourseSpaceQuizAttempt.id)).filter(
                models.CourseSpaceQuizAttempt.quiz_id == q.id
            ).scalar() or 0

        results.append({
            "id": str(q.id),
            "title": q.title,
            "description": q.description,
            "course_id": str(q.course_id),
            "course_title": _course_title(q.course),
            "module_id": str(q.module_id) if q.module_id else None,
            "module_title": q.module.title if q.module else None,
            "question_count": len(q.questions or []),
            "time_limit_minutes": q.time_limit_minutes,
            "passing_score_percentage": q.passing_score_percentage,
            "my_best_score": round(best_score, 1) if best_score is not None else None,
            "my_has_passed": has_passed,
            "my_attempt_count": total_attempts,
            "my_last_attempt_at": last_attempt_at.isoformat() if last_attempt_at else None,
            "total_class_attempts": all_attempts_count,
            "created_at": q.created_at.isoformat() if q.created_at else None
        })

    return {"status": "success", "quizzes": results}


@router.get("/{space_id}/quizzes/{quiz_id}")
def get_quiz_for_taking(
    space_id: uuid.UUID,
    quiz_id: uuid.UUID,
    include_answers: bool = Query(False),
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get quiz details and questions for taking the drill.
    Note: 'correct_index' and 'explanation' are omitted for students unless include_answers=True and user is a tutor.
    """
    space = db.query(models.CourseSpace).filter(models.CourseSpace.id == space_id).first()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course Space not found.")

    quiz = (
        db.query(models.CourseSpaceQuiz)
        .filter(models.CourseSpaceQuiz.id == quiz_id, models.CourseSpaceQuiz.space_id == space_id)
        .options(
            joinedload(models.CourseSpaceQuiz.course),
            joinedload(models.CourseSpaceQuiz.module)
        )
        .first()
    )
    if not quiz:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quiz drill not found.")

    show_answers = include_answers and _can_manage_space(db, space, user)

    sanitized_questions = []
    for q in (quiz.questions or []):
        q_item = {
            "id": q.get("id"),
            "question_text": q.get("question_text"),
            "scenario_text": q.get("scenario_text"),
            "options": q.get("options", []),
            "points": q.get("points", 1.0),
            "topic": q.get("topic", "General"),
            "difficulty": q.get("difficulty", "standard"),
            "has_diagram": bool(q.get("has_diagram") or q.get("image_url")),
            "diagram_placeholder": q.get("diagram_placeholder"),
            "image_url": q.get("image_url")
        }
        if show_answers:
            c_idx = q.get("correct_index", q.get("answer_index", 0))
            q_item["correct_index"] = c_idx
            q_item["answer_index"] = c_idx
            q_item["explanation"] = q.get("explanation", "")
        sanitized_questions.append(q_item)

    return {
        "status": "success",
        "quiz": {
            "id": str(quiz.id),
            "title": quiz.title,
            "description": quiz.description,
            "course_id": str(quiz.course_id),
            "course_title": _course_title(quiz.course),
            "module_id": str(quiz.module_id) if quiz.module_id else None,
            "module_title": quiz.module.title if quiz.module else None,
            "time_limit_minutes": quiz.time_limit_minutes,
            "passing_score_percentage": quiz.passing_score_percentage,
            "questions": sanitized_questions
        }
    }


@router.put("/{space_id}/quizzes/{quiz_id}")
def update_topic_quiz(
    space_id: uuid.UUID,
    quiz_id: uuid.UUID,
    payload: UpdateCourseSpaceQuizRequest,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """(Tutor/Creator/Admin only) Update an existing topic drill's metadata or questions."""
    space = db.query(models.CourseSpace).filter(models.CourseSpace.id == space_id).first()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course Space not found.")
    _check_space_tutor_permission(db, space, user)

    quiz = db.query(models.CourseSpaceQuiz).filter(
        models.CourseSpaceQuiz.id == quiz_id,
        models.CourseSpaceQuiz.space_id == space_id
    ).first()
    if not quiz:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quiz drill not found.")

    if payload.course_id is not None:
        quiz.course_id = payload.course_id
    if payload.module_id is not None:
        quiz.module_id = payload.module_id
    if payload.title is not None and payload.title.strip():
        quiz.title = payload.title.strip()
    if payload.description is not None:
        quiz.description = payload.description.strip() if payload.description.strip() else None
    if payload.time_limit_minutes is not None:
        quiz.time_limit_minutes = max(0, payload.time_limit_minutes)
    if payload.passing_score_percentage is not None:
        quiz.passing_score_percentage = max(1.0, min(100.0, payload.passing_score_percentage))
    if payload.questions is not None:
        quiz.questions = _normalize_quiz_questions(payload.questions)

    db.commit()
    db.refresh(quiz)
    return {
        "status": "success",
        "message": "Topic drill updated successfully.",
        "quiz": {
            "id": str(quiz.id),
            "title": quiz.title,
            "question_count": len(quiz.questions or []),
            "time_limit_minutes": quiz.time_limit_minutes,
            "passing_score_percentage": quiz.passing_score_percentage
        }
    }


@router.delete("/{space_id}/quizzes/{quiz_id}")
def delete_topic_quiz(
    space_id: uuid.UUID,
    quiz_id: uuid.UUID,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """(Tutor/Creator/Admin only) Delete a topic drill and its attempts."""
    space = db.query(models.CourseSpace).filter(models.CourseSpace.id == space_id).first()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course Space not found.")
    _check_space_tutor_permission(db, space, user)

    quiz = db.query(models.CourseSpaceQuiz).filter(
        models.CourseSpaceQuiz.id == quiz_id,
        models.CourseSpaceQuiz.space_id == space_id
    ).first()
    if not quiz:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quiz drill not found.")

    db.delete(quiz)
    db.commit()
    return {"status": "success", "message": "Topic drill deleted successfully."}


@router.get("/{space_id}/quizzes/{quiz_id}/attempts")
def get_quiz_attempts(
    space_id: uuid.UUID,
    quiz_id: uuid.UUID,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get attempt history & analytics for a quiz drill.
    Tutors receive all student attempts + class performance analytics; learners receive their own attempts.
    """
    space = db.query(models.CourseSpace).filter(models.CourseSpace.id == space_id).first()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course Space not found.")

    quiz = db.query(models.CourseSpaceQuiz).filter(
        models.CourseSpaceQuiz.id == quiz_id,
        models.CourseSpaceQuiz.space_id == space_id
    ).first()
    if not quiz:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quiz drill not found.")

    can_manage = _can_manage_space(db, space, user)
    q = (
        db.query(models.CourseSpaceQuizAttempt)
        .filter(models.CourseSpaceQuizAttempt.quiz_id == quiz.id)
        .options(joinedload(models.CourseSpaceQuizAttempt.user))
    )
    if not can_manage:
        q = q.filter(models.CourseSpaceQuizAttempt.user_id == user.id)

    attempts = q.order_by(desc(models.CourseSpaceQuizAttempt.completed_at)).all()

    attempts_list = []
    for a in attempts:
        u = a.user
        learner_name = f"{u.first_name or ''} {u.last_name or ''}".strip() if u else "Learner"
        attempts_list.append({
            "id": str(a.id),
            "user_id": str(a.user_id),
            "learner_name": learner_name or "Learner",
            "learner_email": u.email if (u and can_manage) else None,
            "learner_avatar": getattr(u, "profile_picture_url", None) if u else None,
            "score": a.score,
            "max_score": a.max_score,
            "percentage": round(a.percentage or 0.0, 1),
            "passed": bool(a.passed),
            "duration_seconds": a.duration_seconds or 0,
            "completed_at": a.completed_at.isoformat() if a.completed_at else None
        })

    total_attempts = len(attempts_list)
    unique_learners = len({a["user_id"] for a in attempts_list})
    avg_pct = round(sum(a["percentage"] for a in attempts_list) / total_attempts, 1) if total_attempts else 0.0
    pass_rate = round(sum(1 for a in attempts_list if a["passed"]) / total_attempts * 100.0, 1) if total_attempts else 0.0

    return {
        "status": "success",
        "quiz_id": str(quiz.id),
        "title": quiz.title,
        "passing_score_percentage": quiz.passing_score_percentage,
        "summary": {
            "total_attempts": total_attempts,
            "unique_learners": unique_learners,
            "average_percentage": avg_pct,
            "pass_rate": pass_rate
        },
        "attempts": attempts_list
    }


@router.post("/{space_id}/quizzes/{quiz_id}/submit")
def submit_quiz_attempt(
    space_id: uuid.UUID,
    quiz_id: uuid.UUID,
    payload: SubmitQuizAttemptRequest,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """
    Grade a student's attempt, calculate score and percentage, persist attempt history,
    and return detailed answer breakdowns with explanations.
    """
    quiz = db.query(models.CourseSpaceQuiz).filter(
        models.CourseSpaceQuiz.id == quiz_id,
        models.CourseSpaceQuiz.space_id == space_id
    ).first()
    if not quiz:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quiz drill not found.")

    questions_map = {q.get("id"): q for q in (quiz.questions or [])}
    answers_map = {a.question_id: a.chosen_index for a in payload.answers}

    total_score = 0.0
    max_score = 0.0
    answers_breakdown = []

    for q_id, q_data in questions_map.items():
        q_points = float(q_data.get("points", 1.0))
        correct_idx = int(q_data.get("correct_index", q_data.get("answer_index", 0)))
        max_score += q_points

        chosen_idx = answers_map.get(q_id)
        is_correct = (chosen_idx is not None and chosen_idx == correct_idx)
        if is_correct:
            total_score += q_points

        answers_breakdown.append({
            "question_id": q_id,
            "question_text": q_data.get("question_text"),
            "scenario_text": q_data.get("scenario_text"),
            "options": q_data.get("options", []),
            "image_url": q_data.get("image_url"),
            "topic": q_data.get("topic"),
            "chosen_index": chosen_idx,
            "correct_index": correct_idx,
            "is_correct": is_correct,
            "points_earned": q_points if is_correct else 0.0,
            "max_points": q_points,
            "explanation": q_data.get("explanation")
        })

    percentage = round((total_score / max_score * 100.0), 1) if max_score > 0 else 0.0
    passed = percentage >= quiz.passing_score_percentage

    attempt = models.CourseSpaceQuizAttempt(
        quiz_id=quiz.id,
        user_id=user.id,
        score=total_score,
        max_score=max_score,
        percentage=percentage,
        passed=passed,
        answers=answers_breakdown,
        duration_seconds=payload.duration_seconds
    )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)

    return {
        "status": "success",
        "attempt_id": str(attempt.id),
        "score": total_score,
        "max_score": max_score,
        "percentage": percentage,
        "passed": passed,
        "passing_score_percentage": quiz.passing_score_percentage,
        "duration_seconds": attempt.duration_seconds,
        "answers_breakdown": answers_breakdown,
        "completed_at": attempt.completed_at.isoformat() if attempt.completed_at else None
    }


# =========================================================================
# 4. SCHEDULED CLASSES & LIVE MEETINGS (Google Meet / Zoom / etc.)
# =========================================================================

@router.post("/{space_id}/sessions", status_code=status.HTTP_201_CREATED)
def schedule_live_session(
    space_id: uuid.UUID,
    payload: CreateScheduledSessionRequest,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """
    (Tutors/Creators only) Schedule a live class or office hour session by pasting
    a Google Meet link (or Zoom link) and selecting day/time. Triggers notifications to students.
    """
    space = db.query(models.CourseSpace).filter(models.CourseSpace.id == space_id).first()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course Space not found.")
    _check_space_tutor_permission(db, space, user)

    # Basic URL sanitation
    meeting_link = payload.meeting_link.strip()
    if not (meeting_link.startswith("http://") or meeting_link.startswith("https://")):
        meeting_link = f"https://{meeting_link}"

    # Verify optional course belongs to space
    if payload.course_id:
        sc_link = db.query(models.CourseSpaceCourse).filter(
            models.CourseSpaceCourse.space_id == space.id,
            models.CourseSpaceCourse.course_id == payload.course_id
        ).first()
        if not sc_link:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Selected course is not linked to this Space.")

    session = models.CourseSpaceSession(
        space_id=space.id,
        course_id=payload.course_id,
        title=payload.title.strip(),
        description=payload.description.strip() if payload.description else None,
        meeting_link=meeting_link,
        start_time=payload.start_time,
        duration_minutes=payload.duration_minutes,
        created_by=user.id
    )
    db.add(session)
    db.flush()

    # Formatted session start for notification
    dt_formatted = payload.start_time.strftime("%b %d, %I:%M %p UTC")
    uploader_name = f"{user.first_name} {user.last_name}".strip()

    _dispatch_space_notifications(
        db=db,
        space=space,
        sender=user,
        title=f"Class Scheduled: {payload.title}",
        body=f"{uploader_name} scheduled a live meeting for {dt_formatted}. Click to view details and join.",
        action_route=f"/spaces/{space_id}?tab=sessions&session_id={session.id}",
        payload={
            "space_id": str(space_id),
            "session_id": str(session.id),
            "meeting_link": meeting_link,
            "type": "live_session_scheduled"
        }
    )

    db.commit()
    db.refresh(session)

    return {
        "status": "success",
        "message": "Class scheduled and students notified.",
        "session": {
            "id": str(session.id),
            "title": session.title,
            "description": session.description,
            "meeting_link": session.meeting_link,
            "start_time": session.start_time.isoformat() if session.start_time else None,
            "duration_minutes": session.duration_minutes,
            "course_id": str(session.course_id) if session.course_id else None,
            "created_at": session.created_at.isoformat() if session.created_at else None
        }
    }


@router.get("/{space_id}/sessions")
def list_scheduled_sessions(
    space_id: uuid.UUID,
    course_id: Optional[uuid.UUID] = None,
    upcoming_only: bool = False,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """
    List scheduled classes in the space, segmented optionally by course.
    Sorted chronologically by start_time.
    """
    space = db.query(models.CourseSpace).filter(models.CourseSpace.id == space_id).first()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course Space not found.")

    query = (
        db.query(models.CourseSpaceSession)
        .filter(models.CourseSpaceSession.space_id == space_id)
        .options(
            joinedload(models.CourseSpaceSession.creator),
            joinedload(models.CourseSpaceSession.course)
        )
    )

    if course_id:
        query = query.filter(models.CourseSpaceSession.course_id == course_id)
    if upcoming_only:
        now = datetime.now(timezone.utc)
        query = query.filter(models.CourseSpaceSession.start_time >= now, models.CourseSpaceSession.is_cancelled == False)

    sessions = query.order_by(models.CourseSpaceSession.start_time.asc()).all()

    now = datetime.now(timezone.utc)
    results = []
    for s in sessions:
        creator_name = f"{s.creator.first_name} {s.creator.last_name}" if s.creator else "Instructor"
        is_upcoming = s.start_time >= now if s.start_time else False

        results.append({
            "id": str(s.id),
            "title": s.title,
            "description": s.description,
            "meeting_link": s.meeting_link,
            "start_time": s.start_time.isoformat() if s.start_time else None,
            "duration_minutes": s.duration_minutes,
            "course_id": str(s.course_id) if s.course_id else None,
            "course_title": _course_title(s.course) if s.course else None,
            "creator_name": creator_name,
            "is_cancelled": s.is_cancelled,
            "is_upcoming": is_upcoming,
            "created_at": s.created_at.isoformat() if s.created_at else None
        })

    return {"status": "success", "sessions": results}


@router.delete("/{space_id}/sessions/{session_id}")
def cancel_scheduled_session(
    space_id: uuid.UUID,
    session_id: uuid.UUID,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """(Tutors/Creators only) Cancel or delete a scheduled class."""
    space = db.query(models.CourseSpace).filter(models.CourseSpace.id == space_id).first()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course Space not found.")
    _check_space_tutor_permission(db, space, user)

    session = db.query(models.CourseSpaceSession).filter(
        models.CourseSpaceSession.id == session_id,
        models.CourseSpaceSession.space_id == space_id
    ).first()
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scheduled session not found.")

    db.delete(session)
    db.commit()
    return {"status": "success", "message": "Scheduled class cancelled and removed."}

