from datetime import timedelta

from fastapi import *
from pytz import timezone
import resend
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
@router.post("/invite")
async def send_organisation_invite(
    request: InviteCreateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user= Depends(auth.get_current_user) # Assuming you have an auth dependency
):
    # 1. Verify the org exists and the current_user is the owner/admin
    org = db.query(models.Organisation).filter(models.Organisation.id == request.organisation_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organisation not found")
        
    # (Optional) Verify current_user has permission to invite people to this org here
    
    # 2. Create the Invitation record
    new_invite =    models.Invitations(
        target_email=request.target_email,
        organisation_id=request.organisation_id,
        uses_left=1, # Single-use for a direct email
        expires_at=datetime.now(timezone('UTC')) + timedelta(days=7), # Expires in 7 days
        created_by=current_user.id
    )
    
    db.add(new_invite)
    db.commit()
    db.refresh(new_invite)
    
    # 3. Construct the link to your Flet frontend
    frontend_link = f"https://learn.nu-age.name.ng/accept-invite/{new_invite.id}"
    
    # 4. Trigger background email sending (replace with your actual email logic)
    background_tasks.add_task(send_organisation_invite_email, request.target_email, frontend_link, org.name, request.role)
    
    return {"message": "Invite sent successfully", "token": new_invite.id}


def send_organisation_invite_email(email: str, invite_link: str, org_name: str, role: str = "student"):
    settings = Settings()
    resend.api_key = settings.RESEND_API_KEY
    print(invite_link)
    
    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1.0" />
      <meta http-equiv="X-UA-Compatible" content="IE=edge" />
      <title>You've been invited to {org_name}</title>
      <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body, table, td, a {{ -webkit-text-size-adjust: 100%; -ms-text-size-adjust: 100%; }}
        table, td {{ mso-table-lspace: 0pt; mso-table-rspace: 0pt; }}
        img {{ -ms-interpolation-mode: bicubic; border: 0; outline: none; text-decoration: none; }}
        body {{ background-color: #F2F7F1; font-family: Georgia, 'Times New Roman', serif; }}

        .email-wrapper {{ background-color: #F2F7F1; padding: 40px 16px; }}

        .email-card {{
          background-color: #ffffff;
          max-width: 620px;
          margin: 0 auto;
          border-radius: 12px;
          overflow: hidden;
          box-shadow: 0 4px 28px rgba(55,191,19,0.10), 0 1px 6px rgba(0,0,0,0.06);
        }}

        /* BANNER */
        .banner {{
          width: 100%;
          height: 140px;
          overflow: hidden;
          display: block;
        }}
        .banner img {{
          width: 100%;
          max-width: 620px;
          height: 140px;
          object-fit: cover;
          object-position: center center;
          display: block;
        }}

        /* HEADER STRIP */
        .header-strip {{
          background-color: #37BF13;
          padding: 14px 40px;
          text-align: center;
        }}
        .header-strip p {{
          color: #ffffff;
          font-family: 'Courier New', Courier, monospace;
          font-size: 11px;
          letter-spacing: 3px;
          text-transform: uppercase;
          opacity: 0.92;
        }}

        /* BODY */
        .email-body {{ padding: 40px 44px 36px; }}

        .greeting {{
          font-family: Georgia, serif;
          font-size: 24px;
          font-weight: normal;
          color: #111A0F;
          margin-bottom: 6px;
          line-height: 1.3;
        }}
        .greeting span {{ color: #37BF13; }}

        .divider {{
          width: 48px;
          height: 3px;
          background-color: #37BF13;
          margin: 16px 0 24px;
          border-radius: 2px;
        }}

        .body-text {{
          font-family: Georgia, serif;
          font-size: 15px;
          color: #374151;
          line-height: 1.8;
          margin-bottom: 16px;
        }}

        /* MAIN BUTTON */
        .btn-wrap {{ text-align: center; margin: 32px 0; }}
        .btn-primary {{
          display: inline-block;
          background-color: #37BF13;
          color: #ffffff !important;
          text-decoration: none;
          font-family: Georgia, serif;
          font-size: 16px;
          font-weight: bold;
          padding: 14px 36px;
          border-radius: 8px;
          letter-spacing: 0.5px;
          box-shadow: 0 4px 12px rgba(55,191,19,0.2);
        }}

        /* SIGNATURE */
        .signature {{
          margin-top: 32px;
          padding-top: 24px;
          border-top: 1px solid #E3EEE1;
        }}
        .signature .sign-name {{
          font-family: Georgia, serif;
          font-size: 16px;
          color: #111A0F;
          font-weight: bold;
        }}
        .signature .sign-role {{
          font-family: 'Courier New', Courier, monospace;
          font-size: 11px;
          letter-spacing: 1.5px;
          text-transform: uppercase;
          color: #37BF13;
          margin-top: 4px;
        }}

        /* FOOTER */
        .email-footer {{
          background-color: #111A0F;
          padding: 20px 44px;
          text-align: center;
        }}
        .email-footer p {{
          font-family: 'Courier New', Courier, monospace;
          font-size: 11px;
          color: #5A7A56;
          letter-spacing: 1px;
          line-height: 1.7;
        }}
        .email-footer a {{
          color: #37BF13;
          text-decoration: none;
        }}

        /* MOBILE */
        @media only screen and (max-width: 480px) {{
          .email-body {{ padding: 28px 24px 24px; }}
          .email-footer {{ padding: 16px 24px; }}
          .header-strip {{ padding: 12px 24px; }}
          .greeting {{ font-size: 20px; }}
        }}
      </style>
    </head>
    <body>
      <div class="email-wrapper">
        <div class="email-card">

          <div class="banner">
            <img src="https://nu-age-cdn.b-cdn.net/logos/Nu%20logo%20only.jpeg" alt="Nu-age Banner" />
          </div>

          <div class="header-strip">
            <p>Action Required &nbsp;&bull;&nbsp; Organization Invite</p>
          </div>

          <div class="email-body">

            <h1 class="greeting">You've been invited!</h1>
            <div class="divider"></div>

            <p class="body-text">Hi there,</p>
            <p class="body-text">You have been invited to join <strong>{org_name}</strong> on Nu Age as a <strong>{role}</strong>.</p>
            <p class="body-text">Nu Age is built to give you access to high-quality, practical learning without burning through your data. Click the button below to accept your invitation and set up your account.</p>

            <div class="btn-wrap">
              <a href="{invite_link}" class="btn-primary">Accept Invitation</a>
            </div>
            
            <p class="body-text" style="font-size: 13px; color: #6b7280; margin-top: -10px;">
              If the button doesn't work, copy and paste this link into your browser:<br>
              <span style="word-break: break-all; color: #37BF13;">{invite_link}</span>
            </p>

            <div class="signature">
              <div class="sign-name">Tobi</div>
              <div class="sign-role">The Nu Age Team</div>
            </div>

          </div>

          <div class="email-footer">
            <p>© 2026 Nu Age &nbsp;&bull;&nbsp; You received this because you were invited to join {org_name}.<br>
            Questions? <a href="#">Reply directly to this email.</a></p>
          </div>

        </div>
      </div>
    </body>
    </html>
    """

    params: resend.Emails.SendParams = {
        "from": "Tobi from Nu Age <support@nu-age.name.ng>",
        "to": [email],
        "subject": f"You're invited to join {org_name} on Nu Age 🚀",
        "html": html_content,
    }
    
    try:
        resend.Emails.send(params)
        print(f"Invite sent to {email} for {org_name}!")
    except Exception as e:
        print(f"Failed to send invite email to {email}: {e}")

class JoinProcessRequest(BaseModel):
    token: UUID

@router.post("/process-invite")
async def process_invitation_join(
    request: JoinProcessRequest,
    db: Session = Depends(get_db)
):
    # 1. Fetch and validate the token
    invite = db.query(models.Invitations).filter(models.Invitations.id == request.token).first()
    
    if not invite:
        raise HTTPException(status_code=404, detail="Invalid invitation token.")
        
    if invite.uses_left <= 0:
        raise HTTPException(status_code=400, detail="This invitation has already been used.")
        
    if invite.expires_at < datetime.now(timezone('UTC')):
        raise HTTPException(status_code=400, detail="This invitation has expired.")

    # 2. Check if the targeted email is already registered
    user = db.query(models.User).filter(models.User.email == invite.target_email).first()
    
    # --- SCENARIO B: User is NOT on the platform ---
    if not user:
        return {
            "status": "needs_signup",
            "email": invite.target_email,
            "org_id": invite.organisation_id,
            "message": "User not found. Route to signup."
        }
        
    # --- SCENARIO A: User IS on the platform ---
    existing_member = db.query(models.OrganisationMember).filter(
        models.OrganisationMember.user_id == user.id,
        models.OrganisationMember.organisation_id == invite.organisation_id
    ).first()
    
    if existing_member:
        return {"status": "already_member", "message": "User is already in this organisation."}
        
    try:
        # Create the junction table entry
        new_member = models.OrganisationMember(
            user_id=user.id,
            organisation_id=invite.organisation_id,
            role="student" 
        )
        db.add(new_member)
        
        # Burn the invite token
        invite.uses_left -= 1
        
        db.commit()
        
        return {
            "status": "success", 
            "message": "User successfully added to the organisation."
        }
        
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to join organisation.")
@router.get("/{org_id}/invitations/pending")
async def get_pending_invitations(
    org_id: UUID,
    db: Session = Depends(get_db),
    current_user = Depends(auth.get_current_user)
):
    # 1. Verify the organization exists (and optionally check if current_user is an admin)
    org = db.query(models.Organisation).filter(models.Organisation.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organisation not found")

    # 2. Query for active invitations
    now = datetime.now(timezone('UTC'))
    pending_invites = db.query(models.Invitations).filter(
        models.Invitations.organisation_id == org_id,
        models.Invitations.uses_left > 0,
        models.Invitations.expires_at > now
    ).all()

    # 3. Format the response to match your exact UI requirements
    result = []
    for inv in pending_invites:
        # Check if the role attribute exists, otherwise default to "STUDENT"
        role_value = getattr(inv, 'role', 'STUDENT').upper()
        
        result.append({
            "id": str(inv.id),
            # If target_email is null (like for a bulk WhatsApp link), return a fallback string
            "email": inv.target_email if inv.target_email else "Bulk Group Link",
            "role": role_value,
            "sent_at": inv.created_at.isoformat() if inv.created_at else now.isoformat()
        })

    return result


@router.delete("/invitations/{invite_id}/revoke")
async def revoke_invitation(
    invite_id: UUID,
    db: Session = Depends(get_db),
    current_user = Depends(auth.get_current_user)
):
    # 1. Find the invitation
    invite = db.query(models.Invitations).filter(models.Invitations.id == invite_id).first()
    
    if not invite:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail="Invitation not found or already revoked."
        )
        
    # Optional Security: Ensure the current user actually owns the org this invite belongs to!
    # org = db.query(models.Organisation).filter(models.Organisation.id == invite.organisation_id).first()
    # if org.owner_id != current_user.id:
    #     raise HTTPException(status_code=403, detail="Not authorized to revoke this invite.")

    # 2. Nuke it from the database
    db.delete(invite)
    db.commit()
    
    return {"status": "success", "message": "Invitation successfully revoked."}

@router.get('/joined')
async def get_joined_organisations(
    user = Depends(auth.get_current_user), 
    db: Session = Depends(get_db)
):
    """
    Returns a list of all organizations the current user is a member of,
    excluding any organizations they own.
    """
    joined_orgs = db.query(models.Organisation).join(
        models.OrganisationMember,
        models.Organisation.id == models.OrganisationMember.organisation_id
    ).filter(
        models.OrganisationMember.user_id == user.id,
        models.Organisation.owner_id != user.id # Strictly exclude orgs they own
    ).all()
    
    return joined_orgs