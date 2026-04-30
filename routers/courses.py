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
router = APIRouter(prefix="/courses")

#Create Courses
@router.post('/create')
async def create_course(payload: CourseBase, user = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    
    # 1. Permission Check
    if user.role != "Admin": # Note: Make sure this matches your exact Roles enum/string
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, 
            detail="You do not have the permission to perform this operation"
        )
    
    # 2. Extract data, explicitly excluding the image fields so SQLAlchemy doesn't crash
    course_data = payload.model_dump(exclude={"image_bytes", "image_filename"})
    course_data['admin_id'] = user.id
    
    # 3. Create the Course in the Database FIRST
    course = models.Course(**course_data)
    db.add(course)
    db.commit()
    db.refresh(course) # Now we have course.id!

    # 4. Handle the Image Upload to Bunny.net
    if payload.image_bytes and payload.image_filename:
        try:
            # Decode the base64 string
            raw_image_bytes = base64.b64decode(payload.image_bytes)
            
            # Sanitize the filename
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
            # The course is still created safely even if the image upload fails
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