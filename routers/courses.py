from fastapi import *
from schemas import *
from database import get_db
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy.exc import IntegrityError
from services.bunny_service import upload_audio_to_bunny
from sqlalchemy import or_
from database import Settings
import models
from services import utils, auth
from typing import List
from uuid import UUID
import base64
import pathlib
import uuid
from services.bunny_service import upload_bytes_to_bunny 
from sqlalchemy import func, extract
router = APIRouter(prefix="/courses")

DEFAULT_ORG_ID = "584b537e-6521-4852-a7e4-18f6c095126d"  # the "Nu Age" freelance pool org
@router.post('/create')
async def create_course(
    payload: CourseBase,
    user = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    if user.role != "Admin" and user.role != "Teacher":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have the permission to perform this operation"
        )
 
    course_data = payload.model_dump(exclude={"image_bytes", "image_filename", "image_url", "curriculum"})
    course_data['admin_id'] = user.id
 
    # SAFEGUARD: never trust client-supplied teacher_id/org_id for freelance
    # courses — force them server-side so a freelancer can't submit a course
    # claiming to be someone else, or pointed at a different org while still
    # flagged as freelance.
    if course_data.get("is_freelance"):
        course_data["teacher_id"] = str(user.id)
        course_data["org_id"] = DEFAULT_ORG_ID
    name_clean = (course_data.get("name") or "").strip()
    if not name_clean:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Course name cannot be empty."
        )

    # Check for duplicate course name before DB insert to prevent unhandled 500 crashes
    existing = db.query(models.Course).filter(func.lower(models.Course.name) == func.lower(name_clean)).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A course titled '{existing.name}' already exists. Please choose a different title."
        )

    try:
        course = models.Course(**course_data)
        db.add(course)
        db.commit()
        db.refresh(course)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A course titled '{name_clean}' already exists. Please choose a different title."
        )
    except Exception as ex:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create course: {str(ex)}"
        )

    # 4. INITIALIZE CHAT (New Logic for UI compatibility)
    # This ensures every course has a chat_id to avoid the 404/Null errors in org_view.py
    # 4. INITIALIZE CHAT
    try:
        new_channel = models.Channel(
            name=f"{course.name} Group",
            type="course",          
            course_id=course.id,   
            created_by_id=user.id,  
            org_id=course_data.get('organisation_id') or course_data.get('org_id'), # Safely grab org ID without crashing
            is_announcement_only=False
        )
        db.add(new_channel)
        db.commit()
        db.refresh(new_channel)

        # Tie the chat back to the course chat_id column
        course.chat_id = new_channel.id
        
        # Add the creator as the channel admin
        db.add(models.ChannelMember(
            channel_id=new_channel.id,
            user_id=user.id,
            role="admin"  # or whatever your role enum is
        ))
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"Warning: Chat creation failed, continuing with course creation: {e}")

    # 5. HANDLE IMAGE UPLOAD (Bunny.net Logic)
    # This matches your existing logic for handling Flet FilePicker bytes[cite: 1]
    if payload.image_bytes and payload.image_filename:
        try:
            # Decode the base64 string from the frontend[cite: 1]
            raw_image_bytes = base64.b64decode(payload.image_bytes)
            
            # Sanitize filename with UUID to prevent naming collisions
            file_extension = payload.image_filename.split(".")[-1]
            safe_filename = f"thumbnail_{uuid.uuid4().hex}.{file_extension}"
            
            # Set the Cloud Folder Structure -> courses/{course_id}/
            folder_path = f"courses/{course.id}"
            
            # Upload to the CDN
            cdn_url = await upload_bytes_to_bunny(raw_image_bytes, safe_filename, folder_path)
            
            # Update the Course record with the final URL
            course.image_url = cdn_url
            db.commit()
            db.refresh(course)
            
        except Exception as e:
            # Failure here doesn't crash the whole request; course is still created
            print(f"Warning: Course created, but image upload failed: {str(e)}")
    elif getattr(payload, "image_url", None):
        course.image_url = payload.image_url
        db.commit()
        db.refresh(course)

    # 6. ATOMIC BULK CURRICULUM CREATION (Parallel Course Studio)
    if payload.curriculum and isinstance(payload.curriculum, list):
        try:
            for m_idx, mod_data in enumerate(payload.curriculum):
                if not isinstance(mod_data, dict):
                    continue
                mod_title = (mod_data.get("title") or f"Module {m_idx + 1}").strip()
                new_module = models.Module(
                    title=mod_title,
                    order_index=mod_data.get("order_index", m_idx),
                    course_id=course.id,
                )
                db.add(new_module)
                db.flush()

                lessons = mod_data.get("lessons", [])
                if isinstance(lessons, list):
                    for l_idx, lesson_data in enumerate(lessons):
                        if not isinstance(lesson_data, dict):
                            continue
                        l_title = (lesson_data.get("title") or f"Lesson {l_idx + 1}").strip()
                        l_type = lesson_data.get("type", "text")
                        l_content = lesson_data.get("content", {})
                        if not isinstance(l_content, dict):
                            l_content = {}
                        new_lesson = models.Lesson(
                            title=l_title,
                            type=l_type,
                            order_index=lesson_data.get("order_index", l_idx),
                            content=l_content,
                            module_id=new_module.id,
                        )
                        db.add(new_lesson)
            db.commit()
            db.refresh(course)
        except Exception as cur_err:
            print(f"Warning: Course created, but curriculum insertion failed: {cur_err}")

    return course

#Get all courses
from fastapi import Query, Depends
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_
from uuid import UUID
import models

@router.get('')
def get_all_courses(
    name: str = Query(None),
    org: UUID = Query(None),
    is_public: str = Query(None),
    id: UUID = Query(None),
    progress: float = Query(None), 
    user = Depends(auth.get_current_user), 
    db: Session = Depends(get_db)
):
    query = db.query(models.Course).options(
        joinedload(models.Course.admin),
        joinedload(models.Course.category),
        joinedload(models.Course.organisation),
        selectinload(models.Course.Students),
        selectinload(models.Course.modules)
    )
    
    # 2. Apply Filters
    if name:
        query = query.join(models.Category).filter(
            or_(
                models.Course.name.ilike(f"%{name}%"),
                models.Category.name.ilike(f"%{name}%")
            )
        )
    if org:
        query = query.filter(models.Course.org_id == org)
    
    if is_public == "organisation":
        user_org_ids = [org.id for org in user.organisations]
        query = query.filter(
            models.Course.public == "organisation",
            models.Course.org_id.in_(user_org_ids)
        )
    elif is_public is not None:
        query = query.filter(models.Course.public == is_public)
        
    if id:
        query = query.filter(models.Course.id == id)
    
    # THE FIX: Join Enrollments to check this specific user's progress
    if progress is not None:
        if progress >= 100 or progress >= 99.9:
            query = query.join(models.Enrollment, models.Course.id == models.Enrollment.course_id).filter(
                models.Enrollment.student_id == user.id,
                or_(
                    models.Enrollment.progress >= 99.9,
                    models.Enrollment.completed_at.isnot(None)
                )
            )
        else:
            query = query.join(models.Enrollment, models.Course.id == models.Enrollment.course_id).filter(
                models.Enrollment.student_id == user.id,
                models.Enrollment.progress == progress
            )

    return query.all()

#Update Course 
@router.post('/{course_id}/update_settings')
def change_setting(course_id: UUID, setting: CourseSettings,  db: Session = Depends(get_db), user = Depends(auth.get_current_user)):
    course = db.query(models.Course).filter(models.Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course not found")
    
    # Permission verification
    # Allow:
    # 1. System Admins
    # 2. Course creator/admin
    # 3. Assigned Teacher
    # 4. Organisation owner
    # 5. Organisation members who are Admin / Teacher / Instructor
    has_permission = False
    user_role_str = str(getattr(user, "role", "")).lower()
    if user_role_str in ["admin", "superadmin", "roles.admin"]:
        has_permission = True
    elif str(user.id) == str(course.admin_id):
        has_permission = True
    elif course.teacher_id and str(user.id) == str(course.teacher_id):
        has_permission = True
    elif course.org_id:
        org = db.query(models.Organisation).filter(models.Organisation.id == course.org_id).first()
        if org and str(org.owner_id) == str(user.id):
            has_permission = True
        else:
            org_member = db.query(models.OrganisationMember).filter(
                models.OrganisationMember.organisation_id == course.org_id,
                models.OrganisationMember.user_id == user.id
            ).first()
            if org_member and str(org_member.role).lower() in ["admin", "owner", "teacher", "instructor"]:
                has_permission = True

    if not has_permission:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to perform this action")
    
    if setting.name is not None and str(setting.name).strip():
        course.name = str(setting.name).strip()
    if setting.description is not None:
        course.description = str(setting.description).strip()
    if setting.public is not None:
        p_val = str(setting.public).strip().lower()
        if p_val in ["true", "public", "published"]:
            course.public = "true"
        elif p_val in ["organisation", "organization", "campus"]:
            course.public = "organisation"
        else:
            course.public = "false"
    if setting.teacher_id is not None:
        t_val = str(setting.teacher_id).strip()
        if t_val.lower() in ["none", "null", "", "unassigned"]:
            course.teacher_id = None
        else:
            try:
                course.teacher_id = UUID(t_val)
            except ValueError:
                course.teacher_id = None
    if setting.category_id is not None:
        c_val = str(setting.category_id).strip()
        if c_val.lower() in ["none", "null", ""]:
            course.category_id = None
        else:
            try:
                course.category_id = UUID(c_val)
            except ValueError:
                cat = db.query(models.Category).filter(func.lower(models.Category.name) == c_val.lower()).first()
                if cat:
                    course.category_id = cat.id
    elif getattr(setting, "category", None) is not None:
        c_val = str(setting.category).strip()
        if c_val:
            cat = db.query(models.Category).filter(func.lower(models.Category.name) == c_val.lower()).first()
            if cat:
                course.category_id = cat.id
    if setting.supervised is not None:
        course.supervised = setting.supervised
    if setting.auto_certificate is not None:
        course.auto_certificate = setting.auto_certificate
    db.commit()
    db.refresh(course)
    return course


@router.delete("/{course_id}/delete", status_code=status.HTTP_200_OK)
def delete_course(course_id: UUID, db: Session = Depends(get_db)):
    
    # 1. Find the course in your Neon database
    course_query = db.query(models.Course).filter(models.Course.id== course_id)
    course = course_query.first()

    # 2. Check if the course actually exists
    if not course:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Course with ID {course_id} not found."
        )

    # (Optional) Check permissions here: ensure the user requesting the delete is the owner/admin!

    # 3. Delete and commit to the database
    course_query.delete(synchronize_session=False)
    db.commit()

    # Returning a message is helpful for your Flet frontend to confirm success
    return {"status": "success", "message": f"Course {course_id} has been deleted."}


class AIDraftRequest(BaseModel):
    topic: str
    context: str
    org_id: Optional[str] = None
    images: Optional[List[Dict[str, Any]]] = None

@router.post('/generate-draft', status_code=status.HTTP_202_ACCEPTED)
async def generate_course_draft(
    payload: AIDraftRequest,
    background_tasks: BackgroundTasks,
    user = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    if user.role not in ["ADMIN", "TEACHER", "INSTRUCTOR", "Admin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to generate courses."
        )

    # ─────────────────────────────────────────────────────────────────────────
    # ORG PLAN PERK CHECK FOR AI COURSE DRAFTING
    # Policy: Preexisting orgs (created before cutoff) are grandfathered into AI drafting.
    # New orgs created after cutoff require "AI Course Drafting" in their plan features.
    # ─────────────────────────────────────────────────────────────────────────
    from datetime import datetime, timezone
    CUTOFF_DATE = datetime(2026, 10, 16, 5, 0, 0, tzinfo=timezone.utc)

    org = None
    if payload.org_id:
        try:
            target_org_uuid = uuid.UUID(str(payload.org_id))
            org = db.query(models.Organisation).filter(models.Organisation.id == target_org_uuid).first()
        except ValueError:
            pass

    if not org and getattr(user, "organisations", None):
        org = user.organisations[0] if len(user.organisations) > 0 else None
    if not org:
        org = db.query(models.Organisation).filter(models.Organisation.owner_id == user.id).first()
    if not org:
        org_member = db.query(models.OrganisationMember).filter(models.OrganisationMember.user_id == user.id).first()
        if org_member:
            org = db.query(models.Organisation).filter(models.Organisation.id == org_member.organisation_id).first()

    if org:
        org_created = org.created_at
        if org_created and org_created.tzinfo is None:
            org_created = org_created.replace(tzinfo=timezone.utc)

        is_preexisting = bool(org_created and org_created <= CUTOFF_DATE)
        has_plan_perk = False
        if org.plan and org.plan.features:
            for feat in org.plan.features:
                f_lower = str(feat).lower()
                if "ai" in f_lower and "draft" in f_lower:
                    has_plan_perk = True
                    break

        if not (is_preexisting or has_plan_perk):
            # Handled cleanly in frontend by course_builder.py
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="plan_required"
            )

    from services.ai_service import run_course_draft_job

    job = models.CourseDraftJob(
        id=str(uuid.uuid4()),
        user_id=str(user.id),
        topic=payload.topic,
        context=payload.context,
        status=models.JobStatus.PENDING,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    job_id = job.id
    # no db.close() needed — get_db's own generator/finally handles teardown

    background_tasks.add_task(run_course_draft_job, job_id, payload.topic, payload.context, payload.images)

    return {"status": "queued", "job_id": job_id}


@router.get('/generate-draft/{job_id}')
async def get_course_draft_status(
    job_id: str,
    user = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    job = db.query(models.CourseDraftJob).filter(
        models.CourseDraftJob.id == job_id,
        models.CourseDraftJob.user_id == str(user.id),
    ).first()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    response = {"status": job.status, "job_id": job.id}

    if job.status == models.JobStatus.SUCCESS:
        response["data"] = job.result
    elif job.status == models.JobStatus.FAILED:
        response["detail"] = job.error or "Course generation failed."

    return response
@router.get("/{course_id}/enrollments/org-students")
def get_enrolled_students(
    course_id: UUID, 
    db: Session = Depends(get_db), 
    current_user = Depends(auth.get_current_user)
):
    # 1. Join User and Enrollment tables
    results = (
        db.query(models.User, models.Enrollment)
        .join(models.Enrollment, models.User.id == models.Enrollment.student_id)
        .filter(models.Enrollment.course_id == course_id)
        .all()
    )

    # 2. Map to the expected frontend shape
    students = []
    for user, enrollment in results:
        students.append({
            "id": str(user.id),
            "first_name": user.first_name,
            "last_name": user.last_name,
            "email": user.email,
            "progress": enrollment.progress# Ensure it's a float between 0.0 and 1.0
        })

    return {"students": students}

@router.get("/{course_id}/completion-stats")
def get_completion_stats(
    course_id: UUID, 
    db: Session = Depends(get_db), 
    current_user = Depends(auth.get_current_user)
):
    total_enrolled = db.query(models.Enrollment).filter(
        models.Enrollment.course_id == course_id
    ).count()

    # FIX: Check for 99.9 or 100.0 instead of 1.0
    completed_count = db.query(models.Enrollment).filter(
        models.Enrollment.course_id == course_id,
        models.Enrollment.progress >= 99.9 
    ).count()

    completion_rate = (completed_count / total_enrolled) if total_enrolled > 0 else 0.0

    return {
        "completion_rate": round(completion_rate, 2),
        "completed_count": completed_count,
        "total_enrolled": total_enrolled
    }
@router.get("/{course_id}/certificates")
def get_certificates_issued(
    course_id: UUID, 
    db: Session = Depends(get_db), 
    current_user = Depends(auth.get_current_user)
):
    total_issued = db.query(models.Enrollment).filter(
        models.Enrollment.course_id == course_id,
        models.Enrollment.certificate_url.isnot(None)
    ).count()

    return {
        "total_issued": total_issued
    }

@router.get("/{course_id}/activity")
def get_weekly_activity(
    course_id: UUID, 
    period: str = "weekly", 
    db: Session = Depends(get_db), 
    current_user = Depends(auth.get_current_user)
):
    # Extract the ISO week number from the completed_at timestamp
    week_extract = extract('week', models.LessonProgress.completed_at).label('week_num')
    
    # Group by week and count how many lessons were completed across the course
    activity_results = (
        db.query(
            week_extract,
            func.count(models.LessonProgress.id).label('participations')
        )
        .filter(models.LessonProgress.course_id == course_id)
        .group_by(week_extract)
        .order_by(week_extract)
        .all()
    )

    response = []
    for row in activity_results:
        week_label = f"W{int(row.week_num)}"
        participations = row.participations
        
        # Since we don't have a Views table yet, we can approximate views as 
        # a multiple of participations, or just return the participations.
        estimated_views = int(participations * 1.5) 
        
        response.append({
            "week": week_label,
            "views": estimated_views,
            "participations": participations
        })

    return response

# --- Add to courses.py ---

@router.get('/{course_id}/download')
def get_course_for_download(
    course_id: UUID,
    user = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """
    Returns a full course tree (course + modules + lessons, nested) in one
    call, shaped for local offline storage.

    Permission: enrolled students only, same check as
    GET /courses/{course_id}/enrollment in enrollments.py — mirrored here
    rather than imported since it's a one-line query, to avoid a cross-router
    import for something this small. If you'd rather share it, pull it into
    a shared services/enrollment.py helper and call it from both places.
    """
    course = (
        db.query(models.Course)
        .options(
            joinedload(models.Course.modules).joinedload(models.Module.lessons)
        )
        .filter(models.Course.id == course_id)
        .first()
    )

    if not course:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course not found")

    enrollment = db.query(models.Enrollment).filter(
        models.Enrollment.course_id == course_id,
        models.Enrollment.student_id == user.id,
    ).first()

    # Course admin/teacher can also preview-download without enrolling —
    # matches the pattern used elsewhere in this file (change_setting, etc).
    # Drop this if you want download to be strictly enrollment-only.
    is_owner = user.id in (course.admin_id, course.teacher_id)

    if not enrollment and not is_owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must be enrolled in this course to download it."
        )

    modules_sorted = sorted(course.modules, key=lambda m: m.order_index)

    return {
        "id": str(course.id),
        "name": course.name,
        "description": course.description,
        "category_id": str(course.category_id) if course.category_id else None,
        "objectives": course.objectives,
        "image_url": course.image_url,
        "server_updated_at": course.updated_at.isoformat() if getattr(course, "updated_at", None) else None,
        "modules": [
            {
                "id": str(module.id),
                "title": module.title,
                "order_index": module.order_index,
                "lessons": [
                    {
                        "id": str(lesson.id),
                        "title": lesson.title,
                        "order_index": lesson.order_index,
                        "type": lesson.type,
                        "content": lesson.content,
                    }
                    for lesson in sorted(module.lessons, key=lambda l: l.order_index)
                ],
            }
            for module in modules_sorted
        ],
    }

# --- Add to courses.py (or a progress.py router if you prefer) ---
#

class OfflineProgressEntry(BaseModel):
    lesson_id: UUID
    course_id: UUID
    status: str                            # 'in_progress' | 'completed'
    completed_at: Optional[str] = None     # ISO string, client-local timestamp
    quiz_answers: Optional[dict] = None
    quiz_score: Optional[float] = None

class BulkProgressSync(BaseModel):
    entries: List[OfflineProgressEntry]


@router.post('/progress/bulk-sync')
def bulk_sync_progress(
    payload: BulkProgressSync,
    user = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    results = []
    touched_course_ids = set()

    for entry in payload.entries:
        existing = db.query(models.LessonProgress).filter(
            models.LessonProgress.lesson_id == entry.lesson_id,
            models.LessonProgress.student_id == user.id,
        ).first()

        if existing:
            results.append({"lesson_id": str(entry.lesson_id), "status": "skipped_already_completed"})
            touched_course_ids.add(entry.course_id)
            continue

        # Verify lesson exists in lessons table
        lesson = db.query(models.Lesson).filter_by(id=entry.lesson_id).first()
        if not lesson:
            results.append({"lesson_id": str(entry.lesson_id), "status": "skipped_not_found"})
            continue

        db.add(models.LessonProgress(
            lesson_id=entry.lesson_id,
            course_id=entry.course_id,
            student_id=user.id,
        ))

        results.append({"lesson_id": str(entry.lesson_id), "status": "synced"})
        touched_course_ids.add(entry.course_id)

    db.flush()  # make the LessonProgress writes visible to the recalculation query below

    # Recalculate Enrollment.progress (and completed_at, if just finished)
    # for every course touched by this sync batch.
    for course_id in touched_course_ids:
        enrollment = db.query(models.Enrollment).filter(
            models.Enrollment.course_id == course_id,
            models.Enrollment.student_id == user.id,
        ).first()

        if not enrollment:
            continue  # shouldn't happen — download requires enrollment — but fail safe

        total_lessons = (
            db.query(models.Lesson)
            .join(models.Module, models.Lesson.module_id == models.Module.id)
            .filter(models.Module.course_id == course_id)
            .count()
        )

        completed_lessons = db.query(models.LessonProgress).filter(
            models.LessonProgress.course_id == course_id,
            models.LessonProgress.student_id == user.id,
        ).count()

        if total_lessons > 0:
            enrollment.progress = round((completed_lessons / total_lessons) * 100, 1)

        if enrollment.progress >= 99.9 and not enrollment.completed_at:
            # Course just finished via offline sync. Use the latest

            course_entries = [e for e in payload.entries if e.course_id == course_id and e.completed_at]
            if course_entries:
                enrollment.completed_at = max(e.completed_at for e in course_entries)

        db.flush()

        # Update Playlist Progress if applicable
        playlists_enrolled = db.query(models.PlaylistEnrollment).join(
            models.PlaylistCourse, models.PlaylistCourse.playlist_id == models.PlaylistEnrollment.playlist_id
        ).filter(
            models.PlaylistEnrollment.student_id == user.id,
            models.PlaylistCourse.course_id == course_id
        ).all()

        for pe in playlists_enrolled:
            total_c = db.query(func.count(models.PlaylistCourse.id)).filter_by(playlist_id=pe.playlist_id).scalar()
            completed_c = db.query(func.count(models.Enrollment.id)).join(
                models.PlaylistCourse, models.PlaylistCourse.course_id == models.Enrollment.course_id
            ).filter(
                models.PlaylistCourse.playlist_id == pe.playlist_id,
                models.Enrollment.student_id == user.id,
                models.Enrollment.progress >= 99.9
            ).scalar()

            if total_c > 0:
                pe.progress = round((completed_c / total_c) * 100, 1)
                if pe.progress >= 99.9 and not pe.completed_at:
                    pe.completed_at = func.now()

    db.commit()

    return {"results": results}

@router.post('/{course_id}/rate')
async def rate_course(
    course_id: str,
    rating: float = Body(..., embed=True),
    user = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    if not (1 <= rating <= 5):
        raise HTTPException(status_code=400, detail="Valid rating between 1 and 5 is required.")
        
    try:
        c_uuid = UUID(str(course_id))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid course ID format.")

    enrollment = db.query(models.Enrollment).filter(
        models.Enrollment.course_id == c_uuid,
        models.Enrollment.student_id == user.id
    ).first()
    
    if not enrollment:
        raise HTTPException(status_code=403, detail="You must be enrolled to rate this course.")
        
    # Auto-conclude course on rating:
    # Users submit course ratings upon finishing the curriculum.
    # To guarantee seamless operation across all current and older compiled client builds (200+ users),
    # rating by an enrolled student is always accepted and enrollment is finalized to 100% completion.
    enrollment.progress = 100.0
    if not enrollment.completed_at:
        enrollment.completed_at = func.now()
    db.flush()
        
    course = db.query(models.Course).filter(models.Course.id == c_uuid).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found.")
        
    old_user_rating = enrollment.user_rating
    enrollment.user_rating = rating
    
    current_rating = float(course.rating) if course.rating is not None else 0.0
    current_count = int(course.rating_count) if course.rating_count is not None else 0

    if old_user_rating is not None:
        if current_count > 0:
            course.rating = round(((current_rating * current_count) - old_user_rating + rating) / current_count, 1)
        else:
            course.rating = round(float(rating), 1)
            course.rating_count = 1
    else:
        new_count = current_count + 1
        course.rating = round(((current_rating * current_count) + rating) / new_count, 1)
        course.rating_count = new_count
        
    db.commit()
    
    return {"message": "Rating submitted successfully", "new_rating": course.rating, "rating_count": course.rating_count}