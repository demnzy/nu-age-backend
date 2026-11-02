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
    correct_index: int
    explanation: Optional[str] = None
    points: float = 1.0


class CreateCourseSpaceQuizRequest(BaseModel):
    course_id: uuid.UUID
    module_id: Optional[uuid.UUID] = None
    title: str = Field(..., min_length=2, max_length=255)
    description: Optional[str] = None
    time_limit_minutes: int = 0
    passing_score_percentage: float = 70.0
    questions: List[CreateQuizQuestion]


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


def _check_space_tutor_permission(db: Session, space: models.CourseSpace, user: models.User):
    """Enforce that the user is the creator or a tutor of the space."""
    if space.created_by == user.id:
        return True
    role = _get_user_space_role(db, space.id, user.id)
    if role in ("tutor", "manager"):
        return True
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only tutors or creators can perform this action.")


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
    if payload.course_ids:
        for idx, c_id in enumerate(payload.course_ids):
            # Verify course exists
            course = db.query(models.Course).filter(models.Course.id == c_id).first()
            if course:
                link = models.CourseSpaceCourse(
                    space_id=space.id,
                    course_id=c_id,
                    order_index=idx
                )
                db.add(link)

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
            "created_by": str(space.created_by),
            "course_count": len(payload.course_ids or []),
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
        )
        .order_by(models.CourseSpaceMember.joined_at.desc())
        .all()
    )

    now = datetime.now(timezone.utc)
    results = []
    for m in memberships:
        sp = m.space
        if not sp:
            continue

        c_count = len(sp.courses)
        m_count = db.query(func.count(models.CourseSpaceMaterial.id)).filter(models.CourseSpaceMaterial.space_id == sp.id).scalar() or 0
        q_count = db.query(func.count(models.CourseSpaceQuiz.id)).filter(models.CourseSpaceQuiz.space_id == sp.id).scalar() or 0
        s_count = db.query(func.count(models.CourseSpaceSession.id)).filter(
            models.CourseSpaceSession.space_id == sp.id,
            models.CourseSpaceSession.start_time >= now,
            models.CourseSpaceSession.is_cancelled == False
        ).scalar() or 0
        mem_count = db.query(func.count(models.CourseSpaceMember.id)).filter(models.CourseSpaceMember.space_id == sp.id).scalar() or 0

        creator_name = f"{sp.creator.first_name} {sp.creator.last_name}" if sp.creator else "Instructor"

        results.append({
            "id": str(sp.id),
            "name": sp.name,
            "description": sp.description,
            "code": sp.code,
            "is_open": sp.is_open,
            "banner_url": sp.banner_url,
            "role": m.role,
            "is_creator": sp.created_by == user.id,
            "creator_name": creator_name,
            "member_count": mem_count,
            "course_count": c_count,
            "material_count": m_count,
            "quiz_count": q_count,
            "upcoming_session_count": s_count,
            "joined_at": m.joined_at.isoformat() if m.joined_at else None,
            "created_at": sp.created_at.isoformat() if sp.created_at else None
        })

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
            models.CourseSpace.description.ilike(f"%{query}%")
        ))

    spaces = (
        q.options(
            joinedload(models.CourseSpace.creator),
            joinedload(models.CourseSpace.courses)
        )
        .order_by(models.CourseSpace.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    my_space_ids = set(
        db.query(models.CourseSpaceMember.space_id)
        .filter(models.CourseSpaceMember.user_id == user.id)
        .all()
    )
    my_space_ids = {str(x[0]) for x in my_space_ids}

    results = []
    for sp in spaces:
        c_count = len(sp.courses)
        mem_count = db.query(func.count(models.CourseSpaceMember.id)).filter(models.CourseSpaceMember.space_id == sp.id).scalar() or 0
        m_count = db.query(func.count(models.CourseSpaceMaterial.id)).filter(models.CourseSpaceMaterial.space_id == sp.id).scalar() or 0
        q_count = db.query(func.count(models.CourseSpaceQuiz.id)).filter(models.CourseSpaceQuiz.space_id == sp.id).scalar() or 0

        creator_name = f"{sp.creator.first_name} {sp.creator.last_name}" if sp.creator else "Instructor"
        results.append({
            "id": str(sp.id),
            "name": sp.name,
            "description": sp.description,
            "code": sp.code,
            "is_open": sp.is_open,
            "banner_url": sp.banner_url,
            "creator_name": creator_name,
            "member_count": mem_count,
            "course_count": c_count,
            "material_count": m_count,
            "quiz_count": q_count,
            "is_enrolled": str(sp.id) in my_space_ids,
            "created_at": sp.created_at.isoformat() if sp.created_at else None
        })

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
        if not space.is_open and space.created_by != user.id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This Course Space is private. An invite code is required to join.")
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Either 'code' or 'space_id' must be provided.")

    # Check if already a member
    existing_member = db.query(models.CourseSpaceMember).filter(
        models.CourseSpaceMember.space_id == space.id,
        models.CourseSpaceMember.user_id == user.id
    ).first()

    if not existing_member:
        membership = models.CourseSpaceMember(
            space_id=space.id,
            user_id=user.id,
            role="student"
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
    """Get full details of a Course Space including linked courses and user permissions."""
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
    is_member = role is not None or is_creator

    # Check access permission: public spaces or members can view
    if not space.is_open and not is_member:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You must join this Space to view its contents.")

    # Format linked courses with module count & student progress
    courses_data = []
    for sc in space.courses:
        c = sc.course
        if not c:
            continue
        m_count = db.query(func.count(models.Module.id)).filter(models.Module.course_id == c.id).scalar() or 0
        enr = db.query(models.Enrollment).filter(
            models.Enrollment.student_id == user.id,
            models.Enrollment.course_id == c.id
        ).first()

        courses_data.append({
            "id": str(c.id),
            "title": _course_title(c),
            "description": c.description,
            "thumbnail_url": getattr(c, "image_url", None) or getattr(c, "course_banner", None),
            "module_count": m_count,
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

    creator_name = f"{space.creator.first_name} {space.creator.last_name}" if space.creator else "Instructor"
    creator_avatar = getattr(space.creator, "profile_picture_url", None)

    return {
        "status": "success",
        "space": {
            "id": str(space.id),
            "name": space.name,
            "description": space.description,
            "code": space.code,
            "is_open": space.is_open,
            "banner_url": space.banner_url,
            "creator_id": str(space.created_by),
            "creator_name": creator_name,
            "creator_avatar": creator_avatar,
            "role": "creator" if is_creator else (role or "guest"),
            "is_member": is_member,
            "is_creator": is_creator,
            "can_manage": is_creator or role in ("tutor", "manager"),
            "member_count": mem_count,
            "material_count": m_count,
            "quiz_count": q_count,
            "upcoming_session_count": s_count,
            "courses": courses_data,
            "created_at": space.created_at.isoformat() if space.created_at else None
        }
    }


@router.post("/{space_id}/courses")
def add_courses_to_space(
    space_id: uuid.UUID,
    payload: AddCoursesToSpaceRequest,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """(Tutor/Creator only) Link existing courses to the Space."""
    space = db.query(models.CourseSpace).filter(models.CourseSpace.id == space_id).first()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course Space not found.")
    _check_space_tutor_permission(db, space, user)

    added_count = 0
    highest_order = (
        db.query(func.max(models.CourseSpaceCourse.order_index))
        .filter(models.CourseSpaceCourse.space_id == space.id)
        .scalar() or 0
    )

    for c_id in payload.course_ids:
        # Check course exists
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

    db.commit()
    return {"status": "success", "message": f"Added {added_count} courses to the space."}


@router.delete("/{space_id}/courses/{course_id}")
def remove_course_from_space(
    space_id: uuid.UUID,
    course_id: uuid.UUID,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """(Tutor/Creator only) Unlink a course from the space."""
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
    is_space_creator = space.created_by == user.id
    user_role = _get_user_space_role(db, space.id, user.id)

    if not (is_uploader or is_space_creator or user_role in ("tutor", "manager")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied to delete this material.")

    db.delete(material)
    db.commit()
    return {"status": "success", "message": "Material deleted successfully."}


# =========================================================================
# 3. TOPIC-ALIGNED QUIZZES & FORMATIVE DRILLS
# =========================================================================

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

    # Validate questions
    if not payload.questions:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Quiz must contain at least one question.")

    formatted_questions = []
    for idx, q in enumerate(payload.questions):
        if len(q.options) < 2:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Question {idx+1} must have at least 2 options.")
        if q.correct_index < 0 or q.correct_index >= len(q.options):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Question {idx+1} has an invalid correct_index.")

        formatted_questions.append({
            "id": q.id or f"q_{idx+1}_{uuid.uuid4().hex[:6]}",
            "question_text": q.question_text.strip(),
            "scenario_text": q.scenario_text.strip() if q.scenario_text else None,
            "options": [opt.strip() for opt in q.options],
            "correct_index": q.correct_index,
            "explanation": q.explanation.strip() if q.explanation else None,
            "points": float(q.points) if q.points > 0 else 1.0
        })

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
        body=f"A topic-aligned drill for {course_title} is now available in {space.name}.",
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

    results = []
    for q in quizzes:
        # Get attempts for current learner
        attempts = db.query(models.CourseSpaceQuizAttempt).filter(
            models.CourseSpaceQuizAttempt.quiz_id == q.id,
            models.CourseSpaceQuizAttempt.user_id == user.id
        ).all()

        best_score = max([a.percentage for a in attempts], default=None)
        has_passed = any(a.passed for a in attempts)
        total_attempts = len(attempts)
        last_attempt_at = max([a.completed_at for a in attempts if a.completed_at], default=None)

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
            "created_at": q.created_at.isoformat() if q.created_at else None
        })

    return {"status": "success", "quizzes": results}


@router.get("/{space_id}/quizzes/{quiz_id}")
def get_quiz_for_taking(
    space_id: uuid.UUID,
    quiz_id: uuid.UUID,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get quiz details and questions for taking the drill.
    Note: 'correct_index' and 'explanation' are omitted so students cannot inspect client-side answers!
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

    # Strip answer keys for the student taking the drill
    sanitized_questions = []
    for q in (quiz.questions or []):
        sanitized_questions.append({
            "id": q.get("id"),
            "question_text": q.get("question_text"),
            "scenario_text": q.get("scenario_text"),
            "options": q.get("options", []),
            "points": q.get("points", 1.0)
        })

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
        correct_idx = int(q_data.get("correct_index", 0))
        max_score += q_points

        chosen_idx = answers_map.get(q_id)
        is_correct = (chosen_idx is not None and chosen_idx == correct_idx)
        if is_correct:
            total_score += q_points

        answers_breakdown.append({
            "question_id": q_id,
            "question_text": q_data.get("question_text"),
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

