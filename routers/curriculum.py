from typing import List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session,joinedload
from pydantic import BaseModel
from sqlalchemy import func
from uuid import UUID
import models
from database import get_db
from services import auth
from uuid import UUID
# Adjusted the prefix so it naturally matches the /courses/... path from your logs
router = APIRouter(prefix="/courses", tags=["Curriculum"])

# ==========================================
# PYDANTIC SCHEMAS
# ==========================================

class BulkLessonCreate(BaseModel):
    title: str
    type: str
    order_index: int
    content: Dict[str, Any] # Accepts ANY valid dictionary for JSONB

class BulkModuleCreate(BaseModel):
    title: str
    order_index: int
    lessons: List[BulkLessonCreate] = []

class BulkCurriculumPayload(BaseModel):
    modules: List[BulkModuleCreate]



# 1. Lesson Schema (matches your JSONB content)
class LessonRead(BaseModel):
    id: UUID
    title: str
    type: str
    order_index: int
    content: Dict[str, Any]
    is_completed: bool # <-- New field
    

    class Config:
        from_attributes = True

# 2. Module Schema (contains a list of LessonRead)
class ModuleRead(BaseModel):
    id: UUID
    title: str
    order_index: int
    lessons: List[LessonRead] = []

    class Config:
        from_attributes = True

# 3. The Final Nested Wrapper
class CourseCurriculumRead(BaseModel):
    course_id: UUID
    course_title:str
    modules: List[ModuleRead]
    completed_lesson_ids: List[str] # <-- New field
# ==========================================
# THE BULK PUBLISH ENDPOINT
# ==========================================

@router.post('/{course_id}/curriculum/bulk')
async def save_bulk_curriculum(
    course_id: str,
    payload: BulkCurriculumPayload,
    user = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """
    Takes a deeply nested JSON dictionary and saves the entire curriculum.
    Uses 'Wipe & Replace' with a transaction rollback for total safety.
    """
    # 1. Verify Course exists
    course = db.query(models.Course).filter(models.Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    # (Optional: Add a check here to ensure `user.id` owns/manages this course)

    try:
        # 2. WIPE & REPLACE STRATEGY
        # Delete existing modules for this course. 
        # (Postgres CASCADE will automatically delete the attached lessons).
        db.query(models.Module).filter(models.Module.course_id == course_id).delete(synchronize_session=False)
        db.flush() 

        created_modules_count = 0
        created_lessons_count = 0

        # 3. Iterate through the new Modules
        for mod_data in payload.modules:
            new_module = models.Module(
                title=mod_data.title,
                order_index=mod_data.order_index,
                course_id=course_id
            )
            db.add(new_module)
            
            # Flush to generate the new module's UUID instantly
            db.flush() 
            created_modules_count += 1

            # 4. Iterate through the Lessons inside this Module
            for les_data in mod_data.lessons:
                new_lesson = models.Lesson(
                    title=les_data.title,
                    type=les_data.type,
                    order_index=les_data.order_index,
                    content=les_data.content, 
                    module_id=new_module.id # Inject the freshly flushed Module ID
                )
                db.add(new_lesson)
                created_lessons_count += 1

        # 5. THE FINAL COMMIT
        # Lock it all in permanently.
        db.commit()

        return {
            "message": "Curriculum published successfully!", 
            "stats": {
                "modules_created": created_modules_count,
                "lessons_created": created_lessons_count
            }
        }

    except Exception as e:
        # If anything fails (bad data, DB crash), rollback the whole transaction.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail=f"Curriculum save failed: {str(e)}"
        )

@router.get('/{course_id}/curriculum', response_model=CourseCurriculumRead)
async def get_course_curriculum(
    course_id: str,
    db: Session = Depends(get_db),
    user = Depends(auth.get_current_user)
):
    # 1. Fetch the course first to guarantee we have the title and handle 404s
    course = db.query(models.Course).filter(models.Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    # 2. Fetch overall progress from the Enrollment ledger
    enrollment = db.query(models.Enrollment).filter_by(course_id=course_id, student_id=user.id).first()
    overall_progress = enrollment.progress if enrollment else 0.0

    # 3. Fetch all lesson IDs this specific student has completed
    completed_lesson_records = db.query(models.LessonProgress.lesson_id).filter_by(
        course_id=course_id, student_id=user.id
    ).all()
    completed_lesson_ids = [str(record[0]) for record in completed_lesson_records]

    # 4. Fetch modules and EAGERLY load their lessons (Your N+1 optimization)
    modules = db.query(models.Module).filter(
        models.Module.course_id == course_id
    ).options(
        joinedload(models.Module.lessons)
    ).order_by(models.Module.order_index.asc()).all()

    # 5. Assemble the response payload
    modules_list = []
    for mod in modules:
        # Sort the lessons inside each module
        mod.lessons.sort(key=lambda x: x.order_index)
        
        lessons_list = []
        for lesson in mod.lessons:
            lessons_list.append({
                "id": str(lesson.id),
                "title": lesson.title,
                "type": lesson.type,
                "order_index": lesson.order_index,
                "content": lesson.content,
                # Dynamically calculate if this specific user finished this lesson
                "is_completed": str(lesson.id) in completed_lesson_ids 
            })
            
        modules_list.append({
            "id": str(mod.id),
            "title": mod.title,
            "order_index": mod.order_index,
            "lessons": lessons_list
        })

    return {
        "course_id": course_id,
        "course_title": course.name,
        "overall_progress": overall_progress,
        "completed_lesson_ids": completed_lesson_ids,
        "modules": modules_list
    }

@router.post("/{course_id}/lessons/{lesson_id}/complete")
def mark_lesson_complete(
    course_id: UUID, 
    lesson_id: UUID, 
    user=Depends(auth.get_current_user), 
    db: Session=Depends(get_db)
):
    """Marks a lesson as complete and updates overall course progress."""
    
    # 1. Verify Enrollment
    enrollment = db.query(models.Enrollment).filter_by(
        course_id=course_id, student_id=user.id
    ).first()
    
    if not enrollment:
        raise HTTPException(status_code=403, detail="You are not enrolled in this course.")

    # 2. Check if already marked complete (Idempotency)
    existing_progress = db.query(models.LessonProgress).filter_by(
        student_id=user.id, lesson_id=lesson_id
    ).first()
    
    if not existing_progress:
        # Create the progress record
        new_progress = models.LessonProgress(
            student_id=user.id, 
            lesson_id=lesson_id, 
            course_id=course_id
        )
        db.add(new_progress)
        db.flush() # Flush to get it into the transaction before calculating totals

    # 3. Recalculate Overall Course Progress
    # Count total lessons in the course
    total_lessons = (
        db.query(func.count(models.Lesson.id))
        .join(models.Module, models.Lesson.module_id == models.Module.id)
        .filter(models.Module.course_id == course_id)
        .scalar()
    )

    # Count lessons completed by THIS student
    completed_lessons = db.query(func.count(models.LessonProgress.id)).filter_by(
        course_id=course_id, student_id=user.id
    ).scalar()

    # Calculate percentage safely
    if total_lessons > 0:
        new_percentage = round((completed_lessons / total_lessons) * 100.0, 2)
        enrollment.progress = new_percentage
        if new_percentage >= 100.0 and enrollment.completed_at is None:
            enrollment.completed_at = func.now()
        
    db.commit()

    return {
        "message": "Lesson completed", 
        "course_progress": enrollment.progress
    }