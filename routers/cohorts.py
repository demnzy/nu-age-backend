import io
import csv
import uuid
from datetime import datetime, timezone
from typing import List, Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

import models
import schemas
from database import get_db
from services import auth

router = APIRouter(prefix="/organisations/{org_id}/cohorts", tags=["Cohorts & Trainings"])


# =========================================================================
# HELPER PERMISSION CHECKS
# =========================================================================

def _get_user_org_role(db: Session, org_id: uuid.UUID, user_id: uuid.UUID) -> Optional[str]:
    org = db.query(models.Organisation).filter(models.Organisation.id == org_id).first()
    if not org:
        return None
    if org.owner_id == user_id:
        return "OWNER"

    membership = db.query(models.OrganisationMember).filter(
        models.OrganisationMember.organisation_id == org_id,
        models.OrganisationMember.user_id == user_id
    ).first()
    if membership:
        return membership.role.upper()
    return None


def _require_org_admin_or_teacher(db: Session, org_id: uuid.UUID, user_id: uuid.UUID):
    role = _get_user_org_role(db, org_id, user_id)
    if not role or role not in ("OWNER", "ADMIN", "TEACHER", "STAFF", "INSTRUCTOR"):
        raise HTTPException(status_code=403, detail="Only organisation administrators and teachers can manage cohorts.")


# =========================================================================
# 1. COHORT CRUD
# =========================================================================

@router.post("/create")
def create_cohort(
    org_id: uuid.UUID,
    data: schemas.CohortCreate,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    _require_org_admin_or_teacher(db, org_id, user.id)

    if data.end_date <= data.start_date:
        raise HTTPException(status_code=400, detail="End date must be after start date.")

    now = datetime.now(timezone.utc)
    computed_status = data.status or "upcoming"
    if data.start_date <= now <= data.end_date:
        computed_status = "active"
    elif now > data.end_date:
        computed_status = "completed"

    cohort = models.Cohort(
        organisation_id=org_id,
        name=data.name.strip(),
        description=data.description,
        start_date=data.start_date,
        end_date=data.end_date,
        status=computed_status,
        banner_url=data.banner_url,
        created_by=user.id,
    )
    db.add(cohort)
    db.flush()

    # Link initial courses
    if data.course_ids:
        for idx, c_id in enumerate(data.course_ids):
            db.add(models.CohortCourse(cohort_id=cohort.id, course_id=c_id, order_index=idx))
        db.flush()

    # Add initial members and enroll them into all cohort courses
    if data.member_ids:
        for u_id in data.member_ids:
            db.add(models.CohortMember(cohort_id=cohort.id, user_id=u_id, status="enrolled"))
            # Auto-enroll in cohort courses
            if data.course_ids:
                for c_id in data.course_ids:
                    existing = db.query(models.Enrollment).filter_by(student_id=u_id, course_id=c_id).first()
                    if not existing:
                        db.add(models.Enrollment(student_id=u_id, course_id=c_id, progress=0.0))
        db.flush()

    db.commit()
    db.refresh(cohort)

    return {
        "message": "Cohort created successfully",
        "cohort_id": str(cohort.id),
        "name": cohort.name,
        "status": cohort.status,
    }


@router.get("/")
def list_cohorts(
    org_id: uuid.UUID,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    role = _get_user_org_role(db, org_id, user.id)
    if not role:
        raise HTTPException(status_code=403, detail="You are not a member of this organisation.")

    is_admin = role in ("OWNER", "ADMIN", "TEACHER", "STAFF", "INSTRUCTOR")

    cohorts_query = db.query(models.Cohort).filter(models.Cohort.organisation_id == org_id)

    # If student, show cohorts they belong to, or active public cohorts
    if not is_admin:
        cohorts_query = cohorts_query.join(
            models.CohortMember, models.CohortMember.cohort_id == models.Cohort.id
        ).filter(models.CohortMember.user_id == user.id)

    cohorts = cohorts_query.order_by(models.Cohort.start_date.desc()).all()

    now = datetime.now(timezone.utc)
    results = []
    for c in cohorts:
        # Dynamic status adjustment
        st = c.status
        if c.status != "archived":
            if c.end_date < now:
                st = "completed"
            elif c.start_date <= now <= c.end_date:
                st = "active"
            else:
                st = "upcoming"

        members_count = db.query(func.count(models.CohortMember.id)).filter_by(cohort_id=c.id).scalar() or 0
        courses_count = db.query(func.count(models.CohortCourse.id)).filter_by(cohort_id=c.id).scalar() or 0
        exams_count = db.query(func.count(models.CohortExam.id)).filter_by(cohort_id=c.id).scalar() or 0

        # Check user's own status in this cohort
        user_membership = db.query(models.CohortMember).filter_by(cohort_id=c.id, user_id=user.id).first()

        results.append({
            "id": str(c.id),
            "name": c.name,
            "description": c.description,
            "start_date": c.start_date.isoformat() if c.start_date else None,
            "end_date": c.end_date.isoformat() if c.end_date else None,
            "status": st,
            "banner_url": c.banner_url,
            "members_count": members_count,
            "courses_count": courses_count,
            "exams_count": exams_count,
            "is_enrolled": user_membership is not None,
            "member_status": user_membership.status if user_membership else None,
        })

    return results


@router.get("/{cohort_id}")
def get_cohort_details(
    org_id: uuid.UUID,
    cohort_id: uuid.UUID,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    cohort = db.query(models.Cohort).filter(
        models.Cohort.id == cohort_id,
        models.Cohort.organisation_id == org_id
    ).first()
    if not cohort:
        raise HTTPException(status_code=404, detail="Cohort not found.")

    role = _get_user_org_role(db, org_id, user.id)
    is_admin = role in ("OWNER", "ADMIN", "TEACHER", "STAFF", "INSTRUCTOR")

    user_membership = db.query(models.CohortMember).filter_by(cohort_id=cohort_id, user_id=user.id).first()
    if not is_admin and not user_membership:
        raise HTTPException(status_code=403, detail="You do not have access to this cohort.")

    # Fetch assigned courses
    cohort_courses = (
        db.query(models.CohortCourse)
        .options(joinedload(models.CohortCourse.course))
        .filter_by(cohort_id=cohort_id)
        .order_by(models.CohortCourse.order_index.asc())
        .all()
    )
    courses_payload = []
    for cc in cohort_courses:
        if cc.course:
            total_lessons = db.query(func.count(models.Lesson.id)).join(
                models.Module, models.Module.id == models.Lesson.module_id
            ).filter(models.Module.course_id == cc.course.id).scalar() or 0

            courses_payload.append({
                "id": str(cc.course.id),
                "name": cc.course.name,
                "description": cc.course.description,
                "image_url": cc.course.image_url,
                "total_lessons": total_lessons,
                "rating": cc.course.rating,
                "order_index": cc.order_index,
            })

    # Fetch members (if admin/teacher, fetch all members; if student, basic count or member roster)
    members_payload = []
    cohort_members = (
        db.query(models.CohortMember)
        .options(joinedload(models.CohortMember.user))
        .filter_by(cohort_id=cohort_id)
        .all()
    )
    for cm in cohort_members:
        u = cm.user
        if u:
            # calculate average progress across cohort courses
            course_ids = [cc.course_id for cc in cohort_courses]
            avg_prog = 0.0
            if course_ids:
                enrollments = db.query(models.Enrollment.progress).filter(
                    models.Enrollment.student_id == u.id,
                    models.Enrollment.course_id.in_(course_ids)
                ).all()
                if enrollments:
                    avg_prog = round(sum(e[0] or 0.0 for e in enrollments) / len(course_ids), 1)

            members_payload.append({
                "user_id": str(u.id),
                "first_name": u.first_name,
                "last_name": u.last_name,
                "email": u.email,
                "role": u.role,
                "status": cm.status,
                "enrolled_at": cm.enrolled_at.isoformat() if cm.enrolled_at else None,
                "avg_progress": avg_prog,
            })

    # Fetch exams
    exams = db.query(models.CohortExam).filter_by(cohort_id=cohort_id).order_by(models.CohortExam.opens_at.asc()).all()
    exams_payload = []
    now = datetime.now(timezone.utc)
    for ex in exams:
        o_at = ex.opens_at
        c_at = ex.closes_at
        if o_at and o_at.tzinfo is None:
            o_at = o_at.replace(tzinfo=timezone.utc)
        if c_at and c_at.tzinfo is None:
            c_at = c_at.replace(tzinfo=timezone.utc)

        if c_at and c_at < now:
            ex_status = "CLOSED"
        elif o_at and c_at and o_at <= now <= c_at:
            ex_status = "OPEN_NOW"
        else:
            ex_status = "SCHEDULED"

        question_count = db.query(func.count(models.CohortExamQuestion.id)).filter_by(exam_id=ex.id).scalar() or 0
        user_subs = (
            db.query(models.CohortExamSubmission)
            .filter_by(exam_id=ex.id, user_id=user.id)
            .order_by(
                models.CohortExamSubmission.score.desc(),
                models.CohortExamSubmission.submitted_at.desc(),
            )
            .all()
        )
        in_progress_sub = next((s for s in user_subs if s.status == "in_progress"), None)
        is_in_progress = (in_progress_sub is not None)
        completed_attempts_count = sum(
            1 for s in user_subs if s.status in ("submitted", "graded", "timed_out", "flagged_violation")
        )
        user_sub = user_subs[0] if user_subs else None

        exams_payload.append({
            "id": str(ex.id),
            "title": ex.title,
            "description": ex.description,
            "instructions": ex.instructions,
            "opens_at": ex.opens_at.isoformat(),
            "closes_at": ex.closes_at.isoformat(),
            "duration_minutes": ex.duration_minutes,
            "pass_percentage": ex.pass_percentage,
            "max_attempts": ex.max_attempts,
            "completed_attempts": completed_attempts_count,
            "attempts_left": max(0, ex.max_attempts - completed_attempts_count),
            "is_in_progress": is_in_progress,
            "in_progress_submission": {
                "id": str(in_progress_sub.id),
                "started_at": in_progress_sub.started_at.isoformat() if in_progress_sub.started_at else None,
                "attempt_number": in_progress_sub.attempt_number,
            } if in_progress_sub else None,
            "calculator_type": getattr(ex, "calculator_type", "none") or "none",
            "security_mode": ex.security_mode or "monitored",
            "max_violations": ex.max_violations or 2,
            "shuffle_questions": ex.shuffle_questions if ex.shuffle_questions is not None else True,
            "show_immediate_results": ex.show_immediate_results if ex.show_immediate_results is not None else False,
            "question_count": question_count,
            "status": ex_status,
            "user_submission": {
                "id": str(user_sub.id),
                "status": user_sub.status,
                "score": user_sub.score,
                "percentage": user_sub.percentage,
                "passed": user_sub.passed,
                "submitted_at": user_sub.submitted_at.isoformat() if user_sub.submitted_at else None,
                "attempt_number": user_sub.attempt_number,
                "total_attempts": completed_attempts_count,
            } if user_sub else None,
        })

    # Dynamic status adjustment for cohort
    c_status = cohort.status
    if cohort.status != "archived":
        s_dt = cohort.start_date
        e_dt = cohort.end_date
        if s_dt and s_dt.tzinfo is None:
            s_dt = s_dt.replace(tzinfo=timezone.utc)
        if e_dt and e_dt.tzinfo is None:
            e_dt = e_dt.replace(tzinfo=timezone.utc)
        if e_dt and e_dt < now:
            c_status = "completed"
        elif s_dt and e_dt and s_dt <= now <= e_dt:
            c_status = "active"
        else:
            c_status = "upcoming"

    return {
        "id": str(cohort.id),
        "organisation_id": str(cohort.organisation_id),
        "name": cohort.name,
        "description": cohort.description,
        "start_date": cohort.start_date.isoformat() if cohort.start_date else None,
        "end_date": cohort.end_date.isoformat() if cohort.end_date else None,
        "status": c_status,
        "banner_url": cohort.banner_url,
        "courses": courses_payload,
        "members": members_payload if is_admin else [],
        "members_count": len(members_payload),
        "exams": exams_payload,
        "is_admin": is_admin,
    }


@router.put("/{cohort_id}")
def update_cohort(
    org_id: uuid.UUID,
    cohort_id: uuid.UUID,
    data: schemas.CohortUpdate,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    _require_org_admin_or_teacher(db, org_id, user.id)

    cohort = db.query(models.Cohort).filter(
        models.Cohort.id == cohort_id,
        models.Cohort.organisation_id == org_id
    ).first()
    if not cohort:
        raise HTTPException(status_code=404, detail="Cohort not found.")

    if data.name is not None:
        cohort.name = data.name.strip()
    if data.description is not None:
        cohort.description = data.description
    if data.start_date is not None:
        cohort.start_date = data.start_date
    if data.end_date is not None:
        cohort.end_date = data.end_date
    if data.status is not None:
        cohort.status = data.status
    if data.banner_url is not None:
        cohort.banner_url = data.banner_url

    if cohort.end_date <= cohort.start_date:
        raise HTTPException(status_code=400, detail="End date must be after start date.")

    db.commit()
    return {"message": "Cohort updated successfully"}


@router.delete("/{cohort_id}")
def delete_cohort(
    org_id: uuid.UUID,
    cohort_id: uuid.UUID,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    _require_org_admin_or_teacher(db, org_id, user.id)

    cohort = db.query(models.Cohort).filter(
        models.Cohort.id == cohort_id,
        models.Cohort.organisation_id == org_id
    ).first()
    if not cohort:
        raise HTTPException(status_code=404, detail="Cohort not found.")

    db.delete(cohort)
    db.commit()
    return {"message": "Cohort deleted successfully"}


# =========================================================================
# 2. COHORT COURSES & MEMBERS MANAGEMENT
# =========================================================================

@router.post("/{cohort_id}/courses")
def add_courses_to_cohort(
    org_id: uuid.UUID,
    cohort_id: uuid.UUID,
    data: schemas.CohortCourseAdd,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    _require_org_admin_or_teacher(db, org_id, user.id)

    cohort = db.query(models.Cohort).filter_by(id=cohort_id, organisation_id=org_id).first()
    if not cohort:
        raise HTTPException(status_code=404, detail="Cohort not found.")

    # Get current max order
    current_max = db.query(func.max(models.CohortCourse.order_index)).filter_by(cohort_id=cohort_id).scalar() or 0

    added_count = 0
    members = db.query(models.CohortMember.user_id).filter_by(cohort_id=cohort_id).all()
    member_user_ids = [m[0] for m in members]

    for c_id in data.course_ids:
        existing = db.query(models.CohortCourse).filter_by(cohort_id=cohort_id, course_id=c_id).first()
        if not existing:
            current_max += 1
            db.add(models.CohortCourse(cohort_id=cohort_id, course_id=c_id, order_index=current_max))
            added_count += 1

            # Auto-enroll all cohort members in this newly added course
            for u_id in member_user_ids:
                enr = db.query(models.Enrollment).filter_by(student_id=u_id, course_id=c_id).first()
                if not enr:
                    db.add(models.Enrollment(student_id=u_id, course_id=c_id, progress=0.0))

    db.commit()
    return {"message": f"Added {added_count} course(s) to cohort."}


@router.delete("/{cohort_id}/courses/{course_id}")
def remove_course_from_cohort(
    org_id: uuid.UUID,
    cohort_id: uuid.UUID,
    course_id: uuid.UUID,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    _require_org_admin_or_teacher(db, org_id, user.id)

    cc = db.query(models.CohortCourse).filter_by(cohort_id=cohort_id, course_id=course_id).first()
    if not cc:
        raise HTTPException(status_code=404, detail="Course is not mapped to this cohort.")

    db.delete(cc)
    db.commit()
    return {"message": "Course removed from cohort."}


@router.post("/{cohort_id}/members")
def add_members_to_cohort(
    org_id: uuid.UUID,
    cohort_id: uuid.UUID,
    data: schemas.CohortMemberAdd,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    _require_org_admin_or_teacher(db, org_id, user.id)

    cohort = db.query(models.Cohort).filter_by(id=cohort_id, organisation_id=org_id).first()
    if not cohort:
        raise HTTPException(status_code=404, detail="Cohort not found.")

    cohort_courses = db.query(models.CohortCourse.course_id).filter_by(cohort_id=cohort_id).all()
    course_ids = [c[0] for c in cohort_courses]

    added_count = 0
    for u_id in data.user_ids:
        existing = db.query(models.CohortMember).filter_by(cohort_id=cohort_id, user_id=u_id).first()
        if not existing:
            db.add(models.CohortMember(cohort_id=cohort_id, user_id=u_id, status="enrolled"))
            added_count += 1

            # Auto-enroll in all cohort courses
            for c_id in course_ids:
                enr = db.query(models.Enrollment).filter_by(student_id=u_id, course_id=c_id).first()
                if not enr:
                    db.add(models.Enrollment(student_id=u_id, course_id=c_id, progress=0.0))

    db.commit()
    return {"message": f"Added {added_count} member(s) to cohort."}


@router.delete("/{cohort_id}/members/{user_id}")
def remove_member_from_cohort(
    org_id: uuid.UUID,
    cohort_id: uuid.UUID,
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    _require_org_admin_or_teacher(db, org_id, user.id)

    cm = db.query(models.CohortMember).filter_by(cohort_id=cohort_id, user_id=user_id).first()
    if not cm:
        raise HTTPException(status_code=404, detail="Member is not enrolled in this cohort.")

    db.delete(cm)
    db.commit()
    return {"message": "Member removed from cohort."}


# =========================================================================
# 3. SCHEDULED COHORT EXAMS
# =========================================================================

@router.post("/{cohort_id}/exams")
def create_cohort_exam(
    org_id: uuid.UUID,
    cohort_id: uuid.UUID,
    data: schemas.CohortExamCreate,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    _require_org_admin_or_teacher(db, org_id, user.id)

    cohort = db.query(models.Cohort).filter_by(id=cohort_id, organisation_id=org_id).first()
    if not cohort:
        raise HTTPException(status_code=404, detail="Cohort not found.")

    if data.closes_at <= data.opens_at:
        raise HTTPException(status_code=400, detail="Exam close time must be after open time.")

    exam = models.CohortExam(
        cohort_id=cohort_id,
        title=data.title.strip(),
        description=data.description,
        instructions=data.instructions,
        opens_at=data.opens_at,
        closes_at=data.closes_at,
        duration_minutes=data.duration_minutes,
        pass_percentage=data.pass_percentage,
        max_attempts=data.max_attempts,
        shuffle_questions=data.shuffle_questions,
        show_immediate_results=data.show_immediate_results if data.show_immediate_results is not None else False,
        calculator_type=data.calculator_type or "none",
        security_mode=data.security_mode or "monitored",
        max_violations=data.max_violations or 2,
        created_by=user.id,
    )
    db.add(exam)
    db.commit()
    db.refresh(exam)

    return {
        "message": "Exam scheduled successfully",
        "exam_id": str(exam.id),
        "title": exam.title,
    }


@router.get("/exams/template")
@router.get("/{cohort_id}/exams/template")
def download_exam_question_template(cohort_id: Optional[uuid.UUID] = None):
    """Generates a downloadable CSV template for bulk question upload."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Scenario (Optional)",
        "Question",
        "Option A",
        "Option B",
        "Option C",
        "Option D",
        "Option E",
        "Correct Answer",
        "Explanation",
        "Points"
    ])
    writer.writerow([
        "A 48-year-old patient presents with acute episodic vertigo, fluctuating sensorineural hearing loss, and tinnitus lasting 2 hours. Audiometry reveals low-frequency sensorineural deficit in the left ear.",
        "Which of the following is the most likely initial diagnosis?",
        "Meniere's Disease",
        "Benign Paroxysmal Positional Vertigo",
        "Vestibular Neuritis",
        "Acoustic Neuroma",
        "",
        "A",
        "The triad of episodic vertigo, low-frequency sensorineural hearing loss, and tinnitus is classic for Meniere's Disease.",
        "2.0"
    ])
    writer.writerow([
        "",
        "What is the standard port number used for secure HTTPS traffic?",
        "443",
        "80",
        "22",
        "8080",
        "",
        "A",
        "Port 443 is the standard port allocated for encrypted HTTPS traffic.",
        "1.0"
    ])

    csv_data = output.getvalue()
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=exam_questions_template.csv"}
    )


@router.get("/{cohort_id}/exams/{exam_id}")
def get_cohort_exam(
    org_id: uuid.UUID,
    cohort_id: uuid.UUID,
    exam_id: uuid.UUID,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    exam = db.query(models.CohortExam).filter_by(id=exam_id, cohort_id=cohort_id).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found.")

    role = _get_user_org_role(db, org_id, user.id)
    is_admin = role in ("OWNER", "ADMIN", "TEACHER", "STAFF", "INSTRUCTOR")

    questions = db.query(models.CohortExamQuestion).filter_by(exam_id=exam_id).order_by(models.CohortExamQuestion.order_index.asc()).all()

    questions_payload = []
    for q in questions:
        item = {
            "id": str(q.id),
            "scenario_text": getattr(q, "scenario_text", None),
            "question_text": q.question_text,
            "options": q.options,
            "points": q.points,
            "order_index": q.order_index,
        }
        if is_admin:
            item["correct_index"] = q.correct_index
            item["explanation"] = q.explanation
        questions_payload.append(item)

    now = datetime.now(timezone.utc)
    if exam.closes_at < now:
        ex_status = "CLOSED"
    elif exam.opens_at <= now <= exam.closes_at:
        ex_status = "OPEN_NOW"
    else:
        ex_status = "SCHEDULED"

    return {
        "id": str(exam.id),
        "cohort_id": str(exam.cohort_id),
        "title": exam.title,
        "description": exam.description,
        "instructions": exam.instructions,
        "opens_at": exam.opens_at.isoformat(),
        "closes_at": exam.closes_at.isoformat(),
        "duration_minutes": exam.duration_minutes,
        "pass_percentage": exam.pass_percentage,
        "max_attempts": exam.max_attempts,
        "shuffle_questions": exam.shuffle_questions,
        "show_immediate_results": exam.show_immediate_results,
        "calculator_type": getattr(exam, "calculator_type", "none") or "none",
        "security_mode": exam.security_mode or "monitored",
        "max_violations": exam.max_violations or 2,
        "status": ex_status,
        "questions": questions_payload,
        "is_admin": is_admin,
    }


@router.put("/{cohort_id}/exams/{exam_id}")
def update_cohort_exam(
    org_id: uuid.UUID,
    cohort_id: uuid.UUID,
    exam_id: uuid.UUID,
    data: schemas.CohortExamUpdate,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    _require_org_admin_or_teacher(db, org_id, user.id)

    exam = db.query(models.CohortExam).filter_by(id=exam_id, cohort_id=cohort_id).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found.")

    if data.title is not None:
        exam.title = data.title.strip()
    if data.description is not None:
        exam.description = data.description
    if data.instructions is not None:
        exam.instructions = data.instructions
    if data.opens_at is not None:
        exam.opens_at = data.opens_at
    if data.closes_at is not None:
        exam.closes_at = data.closes_at
    if data.duration_minutes is not None:
        exam.duration_minutes = data.duration_minutes
    if data.pass_percentage is not None:
        exam.pass_percentage = data.pass_percentage
    if data.max_attempts is not None:
        exam.max_attempts = data.max_attempts
    if data.shuffle_questions is not None:
        exam.shuffle_questions = data.shuffle_questions
    if data.show_immediate_results is not None:
        exam.show_immediate_results = data.show_immediate_results
    if data.calculator_type is not None:
        exam.calculator_type = data.calculator_type
    if data.security_mode is not None:
        exam.security_mode = data.security_mode
    if data.max_violations is not None:
        exam.max_violations = data.max_violations

    if exam.closes_at <= exam.opens_at:
        raise HTTPException(status_code=400, detail="Exam close time must be after open time.")

    db.commit()
    return {"message": "Exam updated successfully"}


@router.delete("/{cohort_id}/exams/{exam_id}")
def delete_cohort_exam(
    org_id: uuid.UUID,
    cohort_id: uuid.UUID,
    exam_id: uuid.UUID,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    _require_org_admin_or_teacher(db, org_id, user.id)

    exam = db.query(models.CohortExam).filter_by(id=exam_id, cohort_id=cohort_id).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found.")

    db.delete(exam)
    db.commit()
    return {"message": "Exam deleted successfully"}


# =========================================================================
# 4. QUESTION BANK & BULK EXCEL/CSV IMPORT
# =========================================================================

@router.post("/{cohort_id}/exams/{exam_id}/questions")
def add_exam_question(
    org_id: uuid.UUID,
    cohort_id: uuid.UUID,
    exam_id: uuid.UUID,
    data: schemas.CohortExamQuestionCreate,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    _require_org_admin_or_teacher(db, org_id, user.id)

    exam = db.query(models.CohortExam).filter_by(id=exam_id, cohort_id=cohort_id).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found.")

    if not data.options or len(data.options) < 2:
        raise HTTPException(status_code=400, detail="A question must have at least 2 options.")

    if data.correct_index < 0 or data.correct_index >= len(data.options):
        raise HTTPException(status_code=400, detail="Correct answer index is out of range.")

    current_count = db.query(func.count(models.CohortExamQuestion.id)).filter_by(exam_id=exam_id).scalar() or 0

    sc_text = data.scenario_text.strip() if data.scenario_text and data.scenario_text.strip() else None

    q = models.CohortExamQuestion(
        exam_id=exam_id,
        scenario_text=sc_text,
        question_text=data.question_text.strip(),
        options=[opt.strip() for opt in data.options],
        correct_index=data.correct_index,
        explanation=data.explanation,
        points=data.points,
        order_index=data.order_index or (current_count + 1),
    )
    db.add(q)
    db.commit()
    db.refresh(q)

    return {"message": "Question added successfully", "question_id": str(q.id)}


@router.delete("/{cohort_id}/exams/{exam_id}/questions/{q_id}")
def delete_exam_question(
    org_id: uuid.UUID,
    cohort_id: uuid.UUID,
    exam_id: uuid.UUID,
    q_id: uuid.UUID,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    _require_org_admin_or_teacher(db, org_id, user.id)

    q = db.query(models.CohortExamQuestion).filter_by(id=q_id, exam_id=exam_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="Question not found.")

    db.delete(q)
    db.commit()
    return {"message": "Question deleted successfully"}


@router.put("/{cohort_id}/exams/{exam_id}/questions/{q_id}")
def update_exam_question(
    org_id: uuid.UUID,
    cohort_id: uuid.UUID,
    exam_id: uuid.UUID,
    q_id: uuid.UUID,
    data: schemas.CohortExamQuestionUpdate,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    _require_org_admin_or_teacher(db, org_id, user.id)

    q = db.query(models.CohortExamQuestion).filter_by(id=q_id, exam_id=exam_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="Question not found.")

    if "scenario_text" in data.model_fields_set:
        q.scenario_text = data.scenario_text.strip() if data.scenario_text and data.scenario_text.strip() else None
    if data.question_text is not None:
        q.question_text = data.question_text.strip()
    if data.options is not None:
        if len(data.options) < 2:
            raise HTTPException(status_code=400, detail="A question must have at least 2 options.")
        q.options = [opt.strip() for opt in data.options]
    if data.correct_index is not None:
        opts_len = len(data.options) if data.options is not None else len(q.options)
        if data.correct_index < 0 or data.correct_index >= opts_len:
            raise HTTPException(status_code=400, detail="Correct answer index is out of range.")
        q.correct_index = data.correct_index
    if "explanation" in data.model_fields_set:
        q.explanation = data.explanation.strip() if data.explanation and data.explanation.strip() else None
    if data.points is not None:
        q.points = max(0.5, float(data.points))
    if data.order_index is not None:
        q.order_index = data.order_index

    db.commit()
    db.refresh(q)
    return {
        "message": "Question updated successfully",
        "question": {
            "id": str(q.id),
            "scenario_text": q.scenario_text,
            "question_text": q.question_text,
            "options": q.options,
            "correct_index": q.correct_index,
            "explanation": q.explanation,
            "points": q.points,
            "order_index": q.order_index,
        }
    }


@router.get("/{cohort_id}/exams/template")
def get_exam_question_template(
    org_id: uuid.UUID,
    cohort_id: uuid.UUID,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    _require_org_admin_or_teacher(db, org_id, user.id)

    sample_rows = [
        ["Scenario (Optional)", "Question", "Option A", "Option B", "Option C", "Option D", "Correct Answer", "Explanation", "Points"],
        ["A 45-year-old patient presents to the clinic with sudden onset severe retrosternal chest pain radiating to the left jaw. Blood pressure is 85/50 mmHg, heart rate is 112 bpm, and oxygen saturation is 92% on room air. ECG reveals ST-elevation in leads II, III, and aVF.", "Which coronary artery is most likely occluded based on the clinical presentation and ECG findings?", "Right Coronary Artery (RCA)", "Left Anterior Descending (LAD)", "Left Circumflex (LCx)", "Left Main Coronary Artery", "A", "ST-elevation in leads II, III, and aVF indicates an inferior wall myocardial infarction, typically supplied by the RCA.", "2.0"],
        ["", "What is the primary time complexity of binary search on a sorted array of length N?", "O(log N)", "O(N)", "O(1)", "O(N log N)", "A", "Binary search repeatedly divides the search interval in half, achieving logarithmic time complexity.", "1.0"],
        ["A financial analyst is evaluating two mutually exclusive projects. Project Alpha has an NPV of $45,000 and an IRR of 18%. Project Beta has an NPV of $52,000 and an IRR of 14%. The company's cost of capital is 10%.", "Assuming no capital rationing, which project should the firm select and why?", "Project Beta, because it yields the higher Net Present Value (NPV)", "Project Alpha, because it has a superior Internal Rate of Return (IRR)", "Both projects simultaneously", "Neither project, since their metrics conflict", "A", "Under mutually exclusive conditions without capital rationing, NPV is the preferred criterion as it maximizes total shareholder value.", "1.5"],
    ]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerows(sample_rows)
    csv_bytes = output.getvalue().encode("utf-8")

    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=exam_questions_template.csv"},
    )





@router.post("/{cohort_id}/exams/{exam_id}/upload-questions")
async def upload_exam_questions(
    org_id: uuid.UUID,
    cohort_id: uuid.UUID,
    exam_id: uuid.UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    _require_org_admin_or_teacher(db, org_id, user.id)

    exam = db.query(models.CohortExam).filter_by(id=exam_id, cohort_id=cohort_id).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found.")

    contents = await file.read()
    filename = (file.filename or "").lower()

    try:
        if filename.endswith(".xlsx") or filename.endswith(".xls"):
            df = pd.read_excel(io.BytesIO(contents))
        else:
            # Fallback to CSV (detect UTF-8 / latin-1)
            try:
                text = contents.decode("utf-8")
            except UnicodeDecodeError:
                text = contents.decode("latin-1")
            df = pd.read_csv(io.StringIO(text))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse file: {str(e)}")

    # Clean column headers
    df.columns = [str(c).strip().lower() for c in df.columns]

    # Required columns check
    q_col = next((c for c in df.columns if "question" in c), None)
    ans_col = next((c for c in df.columns if "correct" in c or "answer" in c), None)

    if not q_col or not ans_col:
        raise HTTPException(
            status_code=400,
            detail="Spreadsheet must include at least 'Question' and 'Correct Answer' columns."
        )

    # Find option columns (e.g. option a, option b, opt a, a, b...)
    option_cols = []
    for letter in ["a", "b", "c", "d", "e", "f"]:
        col = next((c for c in df.columns if f"option {letter}" in c or f"opt {letter}" in c or c == letter), None)
        if col:
            option_cols.append((letter, col))

    if len(option_cols) < 2:
        raise HTTPException(
            status_code=400,
            detail="Spreadsheet must include at least 2 options (e.g. 'Option A' and 'Option B')."
        )

    scenario_col = next((c for c in df.columns if any(k in c for k in ("scenario", "context", "passage", "case", "vignette"))), None)
    explanation_col = next((c for c in df.columns if "explanation" in c), None)
    points_col = next((c for c in df.columns if "point" in c), None)

    current_order = db.query(func.max(models.CohortExamQuestion.order_index)).filter_by(exam_id=exam_id).scalar() or 0

    imported_count = 0
    errors = []

    for row_idx, row in df.iterrows():
        row_num = row_idx + 2  # Excel 1-based header
        if pd.isna(row[q_col]):
            continue
        q_text = str(row[q_col]).strip()
        if not q_text or q_text.lower() in ("nan", "none"):
            continue

        # Gather non-empty options
        row_options = []
        option_letter_map = {}
        for idx, (letter, col_name) in enumerate(option_cols):
            raw_val = row.get(col_name)
            if not pd.isna(raw_val):
                val = str(raw_val).strip()
                if val and val.lower() not in ("nan", "none"):
                    row_options.append(val)
                    option_letter_map[letter.upper()] = len(row_options) - 1

        if len(row_options) < 2:
            errors.append(f"Row {row_num}: Must have at least 2 valid options.")
            continue

        # Resolve correct answer
        raw_ans = str(row[ans_col]).strip().upper()
        correct_idx = None

        if raw_ans in option_letter_map:
            correct_idx = option_letter_map[raw_ans]
        elif raw_ans.isdigit():
            val_int = int(raw_ans)
            if 1 <= val_int <= len(row_options):
                correct_idx = val_int - 1
            elif 0 <= val_int < len(row_options):
                correct_idx = val_int
        else:
            # Check if answer text matches one of the options
            for opt_i, opt_str in enumerate(row_options):
                if opt_str.strip().lower() == raw_ans.lower():
                    correct_idx = opt_i
                    break

        if correct_idx is None:
            errors.append(f"Row {row_num}: Correct answer '{raw_ans}' does not match any provided options.")
            continue

        # Scenario
        scen = None
        if scenario_col and not pd.isna(row[scenario_col]):
            raw_scen = str(row[scenario_col]).strip()
            if raw_scen and raw_scen.lower() not in ("nan", "none"):
                scen = raw_scen

        # Points
        pts = 1.0
        if points_col and not pd.isna(row[points_col]):
            try:
                pts = float(row[points_col])
                if pts <= 0: pts = 1.0
            except Exception:
                pts = 1.0

        expl = None
        if explanation_col and not pd.isna(row[explanation_col]):
            raw_expl = str(row[explanation_col]).strip()
            if raw_expl and raw_expl.lower() not in ("nan", "none"):
                expl = raw_expl

        current_order += 1
        q_obj = models.CohortExamQuestion(
            exam_id=exam_id,
            scenario_text=scen,
            question_text=q_text,
            options=row_options,
            correct_index=correct_idx,
            explanation=expl,
            points=pts,
            order_index=current_order,
        )
        db.add(q_obj)
        imported_count += 1

    db.commit()

    return {
        "message": f"Successfully imported {imported_count} questions.",
        "imported_count": imported_count,
        "errors": errors[:20],
    }


# =========================================================================
# 5. CANDIDATE EXAM EXECUTION & SUBMISSION
# =========================================================================

@router.get("/{cohort_id}/exams/{exam_id}/take")
def start_or_resume_exam(
    org_id: uuid.UUID,
    cohort_id: uuid.UUID,
    exam_id: uuid.UUID,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    # Verify enrollment in cohort
    cm = db.query(models.CohortMember).filter_by(cohort_id=cohort_id, user_id=user.id).first()
    if not cm:
        raise HTTPException(status_code=403, detail="You must be an enrolled member of this cohort to take this exam.")

    exam = db.query(models.CohortExam).filter_by(id=exam_id, cohort_id=cohort_id).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found.")

    now = datetime.now(timezone.utc)
    if now < exam.opens_at:
        raise HTTPException(status_code=403, detail=f"This exam has not started yet. It opens on {exam.opens_at.strftime('%b %d, %Y at %H:%M UTC')}.")
    if now > exam.closes_at:
        raise HTTPException(status_code=403, detail="This exam window has closed.")

    # Check previous attempts
    completed_attempts = db.query(models.CohortExamSubmission).filter(
        models.CohortExamSubmission.exam_id == exam_id,
        models.CohortExamSubmission.user_id == user.id,
        models.CohortExamSubmission.status.in_(["submitted", "graded", "timed_out", "flagged_violation"])
    ).count()

    if completed_attempts >= exam.max_attempts:
        raise HTTPException(status_code=403, detail=f"You have reached the maximum allowed attempts ({exam.max_attempts}) for this exam.")

    # Check for active in_progress submission or create a new one
    active_sub = db.query(models.CohortExamSubmission).filter_by(
        exam_id=exam_id, user_id=user.id, status="in_progress"
    ).first()
    is_resumed = (active_sub is not None)

    session_token = str(uuid.uuid4())
    if not active_sub:
        raw_questions = db.query(models.CohortExamQuestion).filter_by(exam_id=exam_id).all()
        if getattr(exam, "shuffle_questions", True):
            import random
            random.shuffle(raw_questions)
        q_order = [str(q.id) for q in raw_questions]

        active_sub = models.CohortExamSubmission(
            exam_id=exam_id,
            cohort_id=cohort_id,
            user_id=user.id,
            attempt_number=completed_attempts + 1,
            started_at=now,
            status="in_progress",
            session_token=session_token,
            answers=[{"question_id": qid, "chosen_index": None} for qid in q_order],
        )
        db.add(active_sub)
        db.commit()
        db.refresh(active_sub)
        questions = raw_questions
    else:
        active_sub.session_token = session_token
        db.commit()
        # Restore deterministic question order saved for this attempt
        saved_order = [
            a.get("question_id")
            for a in (active_sub.answers or [])
            if isinstance(a, dict) and a.get("question_id")
        ]
        all_q = db.query(models.CohortExamQuestion).filter_by(exam_id=exam_id).all()
        q_by_id = {str(q.id): q for q in all_q}
        if saved_order:
            questions = [q_by_id[qid] for qid in saved_order if qid in q_by_id]
            for q in all_q:
                if str(q.id) not in saved_order:
                    questions.append(q)
        else:
            questions = all_q

    # Calculate remaining time in seconds
    sub_started = active_sub.started_at
    if sub_started and sub_started.tzinfo is None:
        sub_started = sub_started.replace(tzinfo=timezone.utc)

    total_allowed_seconds = max(60, (exam.duration_minutes or 60) * 60)
    elapsed_seconds = max(0, int((now - sub_started).total_seconds())) if sub_started else 0
    remaining_seconds = max(0, total_allowed_seconds - elapsed_seconds)

    ans_map = {}
    if active_sub.answers and isinstance(active_sub.answers, list):
        for a in active_sub.answers:
            if isinstance(a, dict) and a.get("question_id"):
                ans_map[str(a["question_id"])] = a.get("chosen_index")

    questions_payload = []
    for q in questions:
        questions_payload.append({
            "id": str(q.id),
            "scenario_text": getattr(q, "scenario_text", None),
            "question_text": q.question_text,
            "options": q.options,
            "points": q.points,
            "chosen_index": ans_map.get(str(q.id)),
        })

    return {
        "submission_id": str(active_sub.id),
        "exam_title": exam.title,
        "instructions": exam.instructions,
        "duration_minutes": exam.duration_minutes,
        "total_duration_minutes": exam.duration_minutes,
        "total_duration_seconds": total_allowed_seconds,
        "remaining_seconds": remaining_seconds,
        "attempt_number": active_sub.attempt_number,
        "max_attempts": exam.max_attempts,
        "pass_percentage": exam.pass_percentage,
        "security_mode": exam.security_mode or "monitored",
        "max_violations": exam.max_violations or 2,
        "calculator_type": getattr(exam, "calculator_type", "none") or "none",
        "show_immediate_results": exam.show_immediate_results,
        "is_resumed": is_resumed,
        "is_in_progress": is_resumed,
        "session_token": active_sub.session_token,
        "questions": questions_payload,
    }


@router.post("/{cohort_id}/exams/{exam_id}/save-progress")
def save_exam_progress(
    org_id: uuid.UUID,
    cohort_id: uuid.UUID,
    exam_id: uuid.UUID,
    data: schemas.CohortExamProgressSave,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    sub = db.query(models.CohortExamSubmission).filter_by(
        exam_id=exam_id, user_id=user.id, status="in_progress"
    ).first()
    if not sub:
        raise HTTPException(status_code=404, detail="No active exam submission found.")

    sub.answers = data.answers
    if data.violations_count is not None:
        sub.violations_count = data.violations_count
    if data.violation_log is not None:
        sub.violation_log = data.violation_log
    db.commit()
    return {"status": "saved"}



@router.post("/{cohort_id}/exams/{exam_id}/submit")
def submit_exam(
    org_id: uuid.UUID,
    cohort_id: uuid.UUID,
    exam_id: uuid.UUID,
    data: schemas.CohortExamSubmissionCreate,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    exam = db.query(models.CohortExam).filter_by(id=exam_id, cohort_id=cohort_id).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found.")

    sub = db.query(models.CohortExamSubmission).filter_by(
        exam_id=exam_id, user_id=user.id, status="in_progress"
    ).first()

    now = datetime.now(timezone.utc)
    if not sub:
        # Create submission if not already tracked
        sub = models.CohortExamSubmission(
            exam_id=exam_id,
            cohort_id=cohort_id,
            user_id=user.id,
            started_at=now,
            status="in_progress",
        )
        db.add(sub)
        db.flush()

    # Fetch all questions for grading
    all_questions = db.query(models.CohortExamQuestion).filter_by(exam_id=exam_id).all()
    q_dict = {str(q.id): q for q in all_questions}

    # Map candidate answers
    ans_map = {}
    for a in data.answers:
        q_id = str(a.get("question_id", ""))
        ans_map[q_id] = a.get("chosen_index")

    total_points_earned = 0.0
    max_possible_points = 0.0
    detailed_results = []

    for q in all_questions:
        q_id = str(q.id)
        max_possible_points += (q.points or 1.0)
        chosen = ans_map.get(q_id)
        is_correct = (chosen is not None and int(chosen) == int(q.correct_index))
        points_awarded = (q.points or 1.0) if is_correct else 0.0
        total_points_earned += points_awarded

        detailed_results.append({
            "question_id": q_id,
            "scenario_text": getattr(q, "scenario_text", None),
            "question_text": q.question_text,
            "options": q.options,
            "chosen_index": chosen,
            "correct_index": q.correct_index,
            "is_correct": is_correct,
            "points_earned": points_awarded,
            "max_points": q.points or 1.0,
            "explanation": q.explanation,
        })

    percentage = round((total_points_earned / max(max_possible_points, 1.0)) * 100.0, 2)
    passed = percentage >= (exam.pass_percentage or 70.0)

    sub.score = round(total_points_earned, 2)
    sub.max_score = round(max_possible_points, 2)
    sub.percentage = percentage
    sub.passed = passed
    sub.duration_seconds = data.duration_seconds
    sub.submitted_at = now
    sub.violations_count = data.violations_count or 0
    sub.violation_log = data.violation_log or []

    # Flag if violations exceeded allowance or strict mode was violated
    if (exam.security_mode == "strict" and sub.violations_count > 0) or (sub.violations_count > (exam.max_violations or 2)):
        sub.status = "flagged_violation"
    else:
        sub.status = "submitted"

    sub.answers = detailed_results

    db.commit()

    # Find user's best attempt across all completed submissions (Highest Score Preserved)
    best_sub = (
        db.query(models.CohortExamSubmission)
        .filter(
            models.CohortExamSubmission.exam_id == exam_id,
            models.CohortExamSubmission.user_id == user.id,
            models.CohortExamSubmission.status.in_(["submitted", "graded", "flagged_violation", "timed_out"]),
        )
        .order_by(
            models.CohortExamSubmission.score.desc(),
            models.CohortExamSubmission.submitted_at.desc(),
        )
        .first()
    ) or sub

    if exam.show_immediate_results:
        response_data = {
            "message": "Exam submitted and graded successfully.",
            "status": sub.status,
            "show_immediate_results": True,
            "score": sub.score,
            "max_score": sub.max_score,
            "percentage": sub.percentage,
            "passed": sub.passed,
            "best_score": best_sub.score,
            "best_percentage": best_sub.percentage,
            "best_passed": best_sub.passed,
            "attempt_number": sub.attempt_number,
            "duration_seconds": sub.duration_seconds,
            "submitted_at": sub.submitted_at.isoformat(),
            "breakdown": detailed_results,
        }
    else:
        response_data = {
            "message": "Exam submitted successfully.",
            "status": sub.status,
            "show_immediate_results": False,
            "duration_seconds": sub.duration_seconds,
            "submitted_at": sub.submitted_at.isoformat(),
            "attempt_number": sub.attempt_number,
            "questions_answered": sum(1 for a in data.answers if a.get("chosen_index") is not None),
            "total_questions": len(all_questions),
            "note": "Your assessment has been securely recorded. Official evaluation, scores, and feedback will be published following instructor review.",
        }

    return response_data


# =========================================================================
# 6. GRADEBOOK & RESULTS ANALYTICS
# =========================================================================

@router.get("/{cohort_id}/exams/{exam_id}/results")
def get_exam_gradebook(
    org_id: uuid.UUID,
    cohort_id: uuid.UUID,
    exam_id: uuid.UUID,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    _require_org_admin_or_teacher(db, org_id, user.id)

    exam = db.query(models.CohortExam).filter_by(id=exam_id, cohort_id=cohort_id).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found.")

    # All cohort members
    members = (
        db.query(models.CohortMember)
        .options(joinedload(models.CohortMember.user))
        .filter_by(cohort_id=cohort_id)
        .all()
    )

    # Submissions for this exam ordered by score desc (highest score preserved)
    submissions = (
        db.query(models.CohortExamSubmission)
        .filter_by(exam_id=exam_id)
        .order_by(
            models.CohortExamSubmission.score.desc(),
            models.CohortExamSubmission.submitted_at.desc(),
        )
        .all()
    )
    # Map each candidate to their highest-scoring completed submission and count completed attempts
    sub_map = {}
    attempts_map = {}
    for s in submissions:
        uid = str(s.user_id)
        if s.status in ("submitted", "graded", "timed_out", "flagged_violation"):
            attempts_map[uid] = attempts_map.get(uid, 0) + 1
        if uid not in sub_map:
            sub_map[uid] = s
        elif sub_map[uid].status == "in_progress" and s.status != "in_progress":
            sub_map[uid] = s

    roster = []
    scores = []
    passed_count = 0

    for cm in members:
        u = cm.user
        if not u: continue
        u_id = str(u.id)
        sub = sub_map.get(u_id)

        if sub and sub.status in ("submitted", "graded", "timed_out", "flagged_violation"):
            status = "PASSED" if sub.passed else "FAILED"
            if sub.status == "flagged_violation":
                status = "FLAGGED_VIOLATION"
            if sub.passed and sub.status != "flagged_violation": passed_count += 1
            scores.append(sub.percentage)
            roster.append({
                "user_id": u_id,
                "name": f"{u.first_name} {u.last_name}",
                "email": u.email,
                "status": status,
                "score": sub.score,
                "max_score": sub.max_score,
                "percentage": sub.percentage,
                "passed": sub.passed,
                "attempt_number": sub.attempt_number,
                "total_attempts": attempts_map.get(u_id, 1),
                "violations_count": sub.violations_count or 0,
                "violation_log": sub.violation_log or [],
                "duration_seconds": sub.duration_seconds,
                "submitted_at": sub.submitted_at.isoformat() if sub.submitted_at else None,
                "submission_id": str(sub.id),
            })
        elif sub and sub.status == "in_progress":
            roster.append({
                "user_id": u_id,
                "name": f"{u.first_name} {u.last_name}",
                "email": u.email,
                "status": "IN_PROGRESS",
                "score": 0.0,
                "max_score": 0.0,
                "percentage": 0.0,
                "passed": False,
                "duration_seconds": 0,
                "submitted_at": None,
                "submission_id": str(sub.id),
            })
        else:
            roster.append({
                "user_id": u_id,
                "name": f"{u.first_name} {u.last_name}",
                "email": u.email,
                "status": "NOT_ATTEMPTED",
                "score": None,
                "max_score": None,
                "percentage": None,
                "passed": False,
                "duration_seconds": None,
                "submitted_at": None,
                "submission_id": None,
            })

    # Sort roster by percentage descending
    roster.sort(key=lambda x: (x["percentage"] is not None, x["percentage"] or 0), reverse=True)

    # Assign ranks
    for idx, r in enumerate(roster):
        r["rank"] = idx + 1 if r["percentage"] is not None else "-"

    total_candidates = len(members)
    attempted_count = len(scores)
    avg_score = round(sum(scores) / max(attempted_count, 1), 1) if attempted_count > 0 else 0.0
    pass_rate = round((passed_count / max(attempted_count, 1)) * 100.0, 1) if attempted_count > 0 else 0.0
    top_score = max(scores) if scores else 0.0

    return {
        "exam_title": exam.title,
        "pass_percentage": exam.pass_percentage,
        "total_candidates": total_candidates,
        "attempted_count": attempted_count,
        "passed_count": passed_count,
        "pass_rate": pass_rate,
        "average_score": avg_score,
        "top_score": top_score,
        "roster": roster,
    }


@router.get("/{cohort_id}/exams/{exam_id}/export")
def export_exam_gradebook(
    org_id: uuid.UUID,
    cohort_id: uuid.UUID,
    exam_id: uuid.UUID,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    _require_org_admin_or_teacher(db, org_id, user.id)

    exam = db.query(models.CohortExam).filter_by(id=exam_id, cohort_id=cohort_id).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found.")

    gradebook_data = get_exam_gradebook(org_id, cohort_id, exam_id, db, user)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Rank", "Candidate Name", "Email", "Status", "Score", "Max Score", "Percentage", "Time Taken (min)", "Submission Date"])

    for r in gradebook_data["roster"]:
        duration_min = round(r["duration_seconds"] / 60, 1) if r.get("duration_seconds") else "-"
        writer.writerow([
            r.get("rank", "-"),
            r.get("name", ""),
            r.get("email", ""),
            r.get("status", ""),
            r.get("score", "-") if r.get("score") is not None else "-",
            r.get("max_score", "-") if r.get("max_score") is not None else "-",
            f"{r['percentage']}%" if r.get("percentage") is not None else "-",
            duration_min,
            r.get("submitted_at", "-") or "-",
        ])

    csv_data = output.getvalue()
    clean_title = "".join(c for c in exam.title if c.isalnum() or c in (" ", "_", "-")).strip().replace(" ", "_")
    filename = f"{clean_title}_results.csv"

    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# =========================================================================
# 7. LEARNER-FACING COHORTS & EXAMS AGGREGATION
# =========================================================================

learner_router = APIRouter(prefix="/organisations/learner", tags=["Learner Cohorts"])

@learner_router.get("/my-cohorts")
def get_learner_cohorts(
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    cohort_members = (
        db.query(models.CohortMember)
        .filter(models.CohortMember.user_id == user.id)
        .all()
    )
    if not cohort_members:
        return {"cohorts": [], "active_urgent_exams": []}

    cohort_ids = [cm.cohort_id for cm in cohort_members]
    cohorts = (
        db.query(models.Cohort)
        .options(joinedload(models.Cohort.organisation))
        .filter(models.Cohort.id.in_(cohort_ids))
        .all()
    )

    now = datetime.now(timezone.utc)
    cohorts_list = []
    active_urgent_exams = []

    for c in cohorts:
        org_name = c.organisation.name if c.organisation else "Organisation"
        org_logo = c.organisation.logo if c.organisation else None

        cohort_courses = (
            db.query(models.CohortCourse)
            .options(joinedload(models.CohortCourse.course))
            .filter_by(cohort_id=c.id)
            .order_by(models.CohortCourse.order_index.asc())
            .all()
        )
        courses_data = []
        for cc in cohort_courses:
            if cc.course:
                enr = db.query(models.Enrollment.progress).filter_by(student_id=user.id, course_id=cc.course.id).first()
                prog = enr[0] if enr else 0.0
                courses_data.append({
                    "id": str(cc.course.id),
                    "name": cc.course.name,
                    "description": cc.course.description,
                    "image_url": cc.course.image_url,
                    "progress": prog,
                })

        exams = db.query(models.CohortExam).filter_by(cohort_id=c.id).order_by(models.CohortExam.opens_at.asc()).all()
        exams_data = []
        for ex in exams:
            if ex.closes_at < now:
                ex_st = "CLOSED"
            elif ex.opens_at <= now <= ex.closes_at:
                ex_st = "OPEN_NOW"
            else:
                ex_st = "SCHEDULED"

            user_subs = (
                db.query(models.CohortExamSubmission)
                .filter_by(exam_id=ex.id, user_id=user.id)
                .order_by(
                    models.CohortExamSubmission.score.desc(),
                    models.CohortExamSubmission.submitted_at.desc(),
                )
                .all()
            )
            user_sub = user_subs[0] if user_subs else None
            in_progress_sub = next((s for s in user_subs if s.status == "in_progress"), None)
            is_in_progress = (in_progress_sub is not None)
            is_completed = user_sub is not None and user_sub.status in ("submitted", "graded", "flagged_violation", "timed_out")
            completed_attempts_count = sum(
                1 for s in user_subs if s.status in ("submitted", "graded", "timed_out", "flagged_violation")
            )

            ex_item = {
                "id": str(ex.id),
                "cohort_id": str(c.id),
                "org_id": str(c.organisation_id),
                "org_name": org_name,
                "title": ex.title,
                "description": ex.description,
                "instructions": ex.instructions,
                "opens_at": ex.opens_at.isoformat(),
                "closes_at": ex.closes_at.isoformat(),
                "duration_minutes": ex.duration_minutes,
                "pass_percentage": ex.pass_percentage,
                "max_attempts": ex.max_attempts,
                "completed_attempts": completed_attempts_count,
                "attempts_left": max(0, ex.max_attempts - completed_attempts_count),
                "calculator_type": getattr(ex, "calculator_type", "none") or "none",
                "question_count": len(ex.questions) if hasattr(ex, "questions") and ex.questions is not None else 0,
                "security_mode": ex.security_mode or "monitored",
                "max_violations": ex.max_violations or 2,
                "status": ex_st,
                "is_completed": is_completed,
                "is_in_progress": is_in_progress,
                "in_progress_submission": {
                    "id": str(in_progress_sub.id),
                    "started_at": in_progress_sub.started_at.isoformat() if in_progress_sub.started_at else None,
                    "attempt_number": in_progress_sub.attempt_number,
                } if in_progress_sub else None,
                "submission": {
                    "score": user_sub.score,
                    "percentage": user_sub.percentage,
                    "passed": user_sub.passed,
                    "status": user_sub.status,
                    "attempt_number": user_sub.attempt_number,
                    "total_attempts": completed_attempts_count,
                    "violations_count": user_sub.violations_count or 0,
                } if user_sub else None,
            }
            exams_data.append(ex_item)

            if (ex_st == "OPEN_NOW" and (completed_attempts_count < ex.max_attempts or is_in_progress)) or is_in_progress:
                active_urgent_exams.append(ex_item)
            elif ex_st == "SCHEDULED" and (ex.opens_at - now).total_seconds() <= 172800:
                active_urgent_exams.append(ex_item)

        cohorts_list.append({
            "id": str(c.id),
            "organisation_id": str(c.organisation_id),
            "organisation_name": org_name,
            "organisation_logo": org_logo,
            "name": c.name,
            "description": c.description,
            "start_date": c.start_date.isoformat() if c.start_date else None,
            "end_date": c.end_date.isoformat() if c.end_date else None,
            "status": c.status,
            "courses": courses_data,
            "exams": exams_data,
        })

    return {
        "cohorts": cohorts_list,
        "active_urgent_exams": active_urgent_exams,
    }
