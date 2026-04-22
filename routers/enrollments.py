from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, select, and_, delete
from typing import List
from uuid import UUID

# Adjust these imports to match your project structure
import models
from schemas import EnrollmentBase, CourseOut # Keep your existing schemas
from database import get_db
from services import auth

router = APIRouter()

# --- New Schemas for Bulk Actions (Move to schemas.py if desired) ---
class StudentEnrollmentState(BaseModel):
    id: str
    name: str
    email: str
    is_enrolled: bool

class EnrollmentActionPayload(BaseModel):
    student_ids: List[str]

# --- Helper Function ---
def add_to_db(item, db):
    db.add(item)
    db.commit()
    db.refresh(item)
    return item

# ==========================================
# EXISTING ROUTES (Fixed & Optimized)
# ==========================================
  
@router.post('/courses/{id}/enrol')
def enrol(id: UUID, enrollment: EnrollmentBase = None, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    course = db.query(models.Course).filter(models.Course.id == id).first()
    if not course:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course not found")
        
    if not course.public:
        # Private enrollment (Admin adding a specific student)
        if user.id != course.admin_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot enrol in this course yet")
            
        target_student = enrollment.student_id if enrollment else user.id
        is_enrolled = db.query(models.Enrollment).filter(
            models.Enrollment.course_id == id, 
            models.Enrollment.student_id == target_student
        ).first()
        
        if is_enrolled:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Student is already enrolled in this course")
            
        enrol_record = models.Enrollment(student_id=target_student, course_id=id)
        
    else:
        # Self (public) enrollment
        is_enrolled = db.query(models.Enrollment).filter(
            models.Enrollment.course_id == id, 
            models.Enrollment.student_id == user.id
        ).first()
        
        if is_enrolled:
             raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="You are already enrolled in this course")
             
        enrol_record = models.Enrollment(student_id=user.id, course_id=id)
        
    db.add(enrol_record)
    db.commit()
    db.refresh(enrol_record)
    return enrol_record

@router.get('/courses/{id}/enrolled')    
def get_enrolled(id: UUID, db: Session = Depends(get_db)):
    enrolled = db.query(models.Course).filter(models.Course.id == id).first()
    if not enrolled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course not found")
        
    enrollments = enrolled.Students
    if not enrollments:
        return {"detail": "No students are enrolled in this course"}
    return enrollments           

@router.get('/courses/enrolled', response_model=List[CourseOut])
def get_enrolled_student(id: UUID = Query(None), user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    target_user_id = id if id else user.id
    
    enrollments = db.query(models.Enrollment).filter(
        models.Enrollment.student_id == target_user_id
    ).options(
        joinedload(models.Enrollment.course).joinedload(models.Course.admin),
        joinedload(models.Enrollment.course).joinedload(models.Course.category)
    ).all()

    if not enrollments:
        return []

    enrolled_courses = []
    for enrollment in enrollments:
        course_obj = enrollment.course
        course_obj.progress = enrollment.progress 
        enrolled_courses.append(course_obj)

    return enrolled_courses       
                  
@router.delete('/courses/{id}/unenroll')
def unenroll(id: UUID, student_id: UUID = Query(None), user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    course = db.query(models.Course).filter(models.Course.id == id).first()
    if not course:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course not found")

    target_student = student_id if student_id else user.id
    
    # If trying to unenroll someone else, verify admin rights
    if student_id and student_id != user.id:
        if user.id != course.admin_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to perform this action")
            
    enrolled = db.query(models.Enrollment).filter(
        models.Enrollment.course_id == id,
        models.Enrollment.student_id == target_student
    ).first()
    
    if not enrolled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Enrollment record not found")
        
    db.delete(enrolled)
    db.commit()
    return {"detail": "Successfully unenrolled"}

# ==========================================
# NEW BULK ROUTES FOR FLET UI
# ==========================================

@router.get("/courses/{course_id}/enrollments/org-students", response_model=List[StudentEnrollmentState])

@router.get("/{course_id}/enrollments/org-students", response_model=List[StudentEnrollmentState])
def get_org_students_for_course(course_id: UUID, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """Fetches all org members with the 'student' role and flags if they are enrolled in this specific course."""
    course = db.query(models.Course).filter(models.Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
        
    # FIX: Correctly check if user is either the admin OR the teacher
    if user.id not in [course.admin_id, course.teacher_id]:
        raise HTTPException(status_code=403, detail="Not authorized to view course enrollments")

    query = (
        select(
            models.User.id,
            models.User.first_name,
            models.User.last_name,
            models.User.email,
            models.Enrollment.student_id.isnot(None).label("is_enrolled")
        )
        .join(models.OrganisationMember, models.OrganisationMember.user_id == models.User.id)
        .outerjoin(
            models.Enrollment, 
            and_(models.User.id == models.Enrollment.student_id, models.Enrollment.course_id == course_id)
        )
        .where(
            and_(
                models.OrganisationMember.organisation_id == course.org_id,
                # Explicitly filter by the student role within this organization
                models.OrganisationMember.role == "student" 
            )
        )
    )
    
    results = db.execute(query).all()
    
    return [
        {
            "id": str(r.id),
            "name": f"{r.first_name} {r.last_name}",
            "email": r.email,
            "is_enrolled": r.is_enrolled
        }
        for r in results
    ]

@router.post("/courses/{course_id}/enrollments/bulk-enroll")
def bulk_enroll_students(course_id: UUID, payload: EnrollmentActionPayload, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """Bulk enrolls multiple students from the Flet UI."""
    course = db.query(models.Course).filter(models.Course.id == course_id).first()
    if not course or user.id != (course.admin_id or course.teacher_id):
        raise HTTPException(status_code=403, detail="Not authorized")

    if not payload.student_ids:
        return {"message": "No students provided."}
        
    for s_id in payload.student_ids:
        exists = db.query(models.Enrollment).filter_by(student_id=s_id, course_id=course_id).first()
        if not exists:
            new_enrollment = models.Enrollment(student_id=s_id, course_id=course_id)
            db.add(new_enrollment)
            
    db.commit()
    return {"success": True, "message": f"Enrolled {len(payload.student_ids)} students."}

@router.post("/courses/{course_id}/enrollments/bulk-unenroll")
def bulk_unenroll_students(course_id: UUID, payload: EnrollmentActionPayload, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """Bulk unenrolls multiple students from the Flet UI."""
    course = db.query(models.Course).filter(models.Course.id == course_id).first()
    if not course or user.id != (course.admin_id or course.teacher_id):
        raise HTTPException(status_code=403, detail="Not authorized")

    if not payload.student_ids:
        return {"message": "No students provided."}
        
    stmt = delete(models.Enrollment).where(
        and_(
            models.Enrollment.course_id == course_id,
            models.Enrollment.student_id.in_(payload.student_ids)
        )
    )
    db.execute(stmt)
    db.commit()
    return {"success": True, "message": f"Unenrolled {len(payload.student_ids)} students."}

