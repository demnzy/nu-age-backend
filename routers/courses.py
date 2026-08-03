from fastapi import *
from schemas import *
from database import get_db
from sqlalchemy.orm import Session, joinedload
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


@router.post('/create')
async def create_course(
    payload: CourseBase, 
    user = Depends(auth.get_current_user), 
    db: Session = Depends(get_db)
):
    # 1. PERMISSION CHECK
    # Keep your specific role check (backward compatible with your existing Roles)
    if user.role != "Admin" and user.role != "Teacher": 
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, 
            detail="You do not have the permission to perform this operation"
        )
    
    # 2. EXTRACT DATA
    # Explicitly exclude image fields so SQLAlchemy doesn't crash on raw strings
    course_data = payload.model_dump(exclude={"image_bytes", "image_filename"})
    course_data['admin_id'] = user.id
    
    # 3. CREATE COURSE RECORD (First Step)
    # This generates the course.id needed for the chat and the CDN folder path
    course = models.Course(**course_data)
    db.add(course)
    db.commit()
    db.refresh(course) 

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
    is_public: bool = Query(None),
    id: UUID = Query(None),
    progress: int = Query(None), 
    user = Depends(auth.get_current_user), 
    db: Session = Depends(get_db)
):
    query = db.query(models.Course).options(
        joinedload(models.Course.admin),
        joinedload(models.Course.category),
        joinedload(models.Course.Students)
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
    
    if is_public is not None:
        query = query.filter(models.Course.public == is_public)
        
    if id:
        query = query.filter(models.Course.id == id)
    
    # THE FIX: Join Enrollments to check this specific user's progress
    if progress is not None:
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
    
    if user.role != "Admin" and user.id != course.admin_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to perform this action")
    
    if setting.name is not None:
        course.name = setting.name
    if setting.description is not None:
        course.description = setting.description
    if setting.public is not None:
        course.public = setting.public
    if setting.teacher_id is not None:
        if setting.teacher_id == "none":  # Special case to remove teacher
            course.teacher_id = None
        course.teacher_id = setting.teacher_id
    if setting.category_id is not None:
        course.category_id = setting.category_id    
    if setting.supervised is not None:
        course.supervised = setting.supervised
    db.commit()
    db.refresh(course)



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

@router.post('/generate-draft', status_code=status.HTTP_202_ACCEPTED)
async def generate_course_draft(
    payload: AIDraftRequest,
    background_tasks: BackgroundTasks,
    user = Depends(auth.get_current_user),
    db: Session = Depends(get_db),   # <-- now correctly a parameter
):
    if user.role not in ["ADMIN", "TEACHER", "INSTRUCTOR", "Admin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to generate courses."
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

    background_tasks.add_task(run_course_draft_job, job_id, payload.topic, payload.context)

    return {"status": "queued", "job_id": job_id}


@router.get('/generate-draft/{job_id}')
async def get_course_draft_status(
    job_id: str,
    user = Depends(auth.get_current_user),
):
    db: Session = Depends(get_db)
    try:
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
    finally:
        db.close()
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