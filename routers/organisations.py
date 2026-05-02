from fastapi import *
from schemas import *
from database import get_db,Settings
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_, select,func
import models
from services import utils, auth
from typing import List
import base64
from services.bunny_service import upload_bytes_to_bunny
import uuid
router = APIRouter(prefix="/organisations")
@router.post('/create')
async def create_org(payload: orgbase, user= Depends(auth.get_current_user), db:Session = Depends(get_db)):
    
    # 1. Check Uniqueness First (Fail fast before doing any work)
    if db.query(models.Organisation).filter(models.Organisation.name == payload.name).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Organisation name already exists, choose another one")
    
    if db.query(models.Organisation).filter(models.Organisation.email == payload.email).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email is associated with another Organisation")

    # 2. Prepare the database model
    # We use exclude=() so Pydantic doesn't try to pass "logo_bytes" into the SQLAlchemy model!
    org_data = payload.model_dump(exclude={"logo_bytes", "logo_filename"})
    org_data['owner_id'] = user.id  
    
    # 3. Create and save the base Organization
    org = models.Organisation(**org_data)
    db.add(org)
    db.commit()
    db.refresh(org) # This assigns org.id from PostgreSQL

    # 4. Handle the Logo Upload (Only if the user provided one)
    if payload.logo_bytes and payload.logo_filename:
        try:
            # Decode the Base64 string back into raw bytes for Bunny.net
            raw_image_bytes = base64.b64decode(payload.logo_bytes)
            
            # Sanitize the filename and define the folder
            file_extension = payload.logo_filename.split(".")[-1]
            safe_filename = f"logo_{uuid.uuid4().hex}.{file_extension}"
            folder_path = f"logos/{org.id}"

            # Upload to Bunny.net
            cdn_url = await upload_bytes_to_bunny(raw_image_bytes, safe_filename, folder_path)
            
            # Update the organization with the new CDN URL
            org.logo = cdn_url
            db.commit()
            db.refresh(org)
            
        except Exception as e:
            # If the image upload fails, the organization is still safely created!
            # We just print the error and return the org without a logo.
            print(f"Warning: Org created, but logo upload failed: {str(e)}")

    # 5. Return the final organization
    return org
    
@router.get('/me')
async def get_user_organisation(user= Depends(auth.get_current_user), db:Session = Depends(get_db)):
    try:
        org = db.query(models.Organisation).filter(models.Organisation.owner_id == user.id).first()
        
        # Exact Error: 404 if the user hasn't created an organization yet
        if not org:
            return None

        # 1. Calculate your stats (Implementation unchanged)
        member_count = db.query(models.OrganisationMember).filter(
            models.OrganisationMember.organisation_id == org.id
        ).count()

        course_count = db.query(models.Course).filter(
            models.Course.org_id == org.id
        ).count()

        staff_count = db.query(models.User).join(
            models.OrganisationMember, models.User.id == models.OrganisationMember.user_id
        ).filter(
            models.OrganisationMember.organisation_id == org.id,
            models.User.role == Roles.TEACHER 
        ).count()
        
        student_count = db.query(models.User).join(
            models.OrganisationMember, models.User.id == models.OrganisationMember.user_id
        ).filter(
            models.OrganisationMember.organisation_id == org.id,
            models.User.role == Roles.STUDENT 
        ).count()

        # 2. Extract the base organization data (Implementation unchanged)
        org_data = {column.name: getattr(org, column.name) for column in org.__table__.columns}

        # 3. Inject the stats (Implementation unchanged)
        org_data["members"] = member_count
        org_data["courses"] = course_count
        org_data["staff"] = staff_count
        org_data["students"] = student_count

        # 4. Extract Plan data using the relationship (Implementation unchanged)
        if org.plan:
            org_data["plan"] = {column.name: getattr(org.plan, column.name) for column in org.plan.__table__.columns}
        else:
            org_data["plan"] = None

        return org_data

    # --- ROBUST ERROR HANDLING ---

    except HTTPException:
        # If it's our 404 from above, let it pass through normally
        raise

    except SQLAlchemyError as db_error:
        # Exact Error: 500 if the database connection drops or a query fails
        print(f"Database Error in /me: {str(db_error)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="A database error occurred while fetching organization data."
        )

    except Exception as e:
        # Exact Error: 500 Catch-all for any Python/dict comprehension crashes
        print(f"Unexpected Error in /me: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while processing your request."
        )

# Make sure your models file is imported properly

@router.get('/members')
async def get_organisation_members(
    id: str = Query(...), 
    students: bool = Query(False, description="Filter to show students"),
    teachers: bool = Query(False, description="Filter to show teachers"),
    user = Depends(auth.get_current_user), 
    db: Session = Depends(get_db)
):
    # 1. Ensure the organisation exists
    org = db.query(models.Organisation).filter(models.Organisation.id == id).first()
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")

    # 2. Base query: Start with Users, but JOIN the OrganisationMember table 
    # so we can filter based on the connection
    query = db.query(models.User).join(
        models.OrganisationMember, 
        models.User.id == models.OrganisationMember.user_id
    ).filter(
        models.OrganisationMember.organisation_id == id
    )

    # 3. Build the role filter list based on the query parameters
    target_roles = []
    if students:
        target_roles.append("student") 
    if teachers:
        target_roles.append("teacher") 

    # 4. If any roles were requested, apply the filter to the association table's role column
    if target_roles:
        query = query.filter(models.OrganisationMember.role.in_(target_roles))

    # 5. Execute and return the list of User objects
    return query.all()

@router.get('/courses')
async def get_organisation_courses(
    id: UUID = Query(...),  # Change from Query(None) to Query(...) with UUID type
    user=Depends(auth.get_current_user), 
    db: Session = Depends(get_db)
):
    # 1. Verify the organization exists
    org = db.query(models.Organisation).filter(models.Organisation.id == id).first()
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")

    # 2. THE FIX: Create an isolated subquery that ONLY handles counting
    enrollment_counts = (
        db.query(
            models.Enrollment.course_id,
            func.count(models.Enrollment.student_id).label('student_count')
        )
        .group_by(models.Enrollment.course_id)
        .subquery()
    )

    # 3. Main query: Fetch courses (which automatically loads categories/admins) 
    # and join the counts subquery cleanly
    results = (
        db.query(
            models.Course,
            # Coalesce ensures we get 0 instead of Null if there are no enrollments
            func.coalesce(enrollment_counts.c.student_count, 0).label("total_students")
        )
        .outerjoin(enrollment_counts, models.Course.id == enrollment_counts.c.course_id)
        .filter(models.Course.org_id == id)
        .all()
    )

    # 4. Attach the database-calculated count directly to the course objects
    courses_with_counts = []
    for course, count in results:
        course.total_students = count
        courses_with_counts.append(course)

    return courses_with_counts

@router.post("/{org_id}/join")
def join_organization(
    org_id: UUID, 
    user_id: UUID= Query(...),
    db: Session = Depends(get_db), 
    
):
    """Adds the current user to a specific organization."""
    
    # 1. Verify the Organization actually exists
    org = db.query(models.Organisation).filter(models.Organisation.id == org_id).first()
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail="Organization not found."
        )

    # 2. Prevent Duplicate Memberships
    existing_membership = db.query(models.OrganisationMember).filter(
        models.OrganisationMember.organisation_id == org_id,
        models.OrganisationMember.user_id == user_id
    ).first()
    
    if existing_membership:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, 
            detail="You are already a member of this organization."
        )

    # 3. Create the Membership
    # Note: If your OrganisationMember model requires additional fields 
    # (like role="student" or joined_at), add them here!
    new_member = models.OrganisationMember(
        organisation_id=org_id,
        user_id=user_id,
        role= "student"
    )
    
    db.add(new_member)
    db.commit()
    
    return {
        "status": "success", 
        "message": f"Successfully joined {org.name}."
    }