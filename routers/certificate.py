from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from uuid import UUID
from datetime import datetime
import uuid
import fitz  # PyMuPDF

import models
from database import get_db
from services import auth
from services.bunny_service import upload_bytes_to_bunny 

router = APIRouter(prefix="/certificates", tags=["Certificates"])

@router.post("/{course_id}/generate")
async def generate_and_upload_certificate(
    course_id: UUID, 
    user = Depends(auth.get_current_user), 
    db: Session = Depends(get_db)
):
    print("\n--- [DEBUG] STARTING CERTIFICATE GENERATION ---")
    
    # 1. Verify Completion
    enrollment = db.query(models.Enrollment).filter_by(
        student_id=user.id, course_id=course_id
    ).first()

    if not enrollment:
        print("[DEBUG] FAILED: User is not enrolled.")
        raise HTTPException(status_code=403, detail="Not enrolled in this course.")
        
    if enrollment.certificate_url:
        print(f"[DEBUG] EARLY RETURN: Certificate already exists in DB: {enrollment.certificate_url}")
        return {
            "message": "Certificate already generated", 
            "url": enrollment.certificate_url, 
            "credential_id": enrollment.credential_id
        }

    course = db.query(models.Course).filter_by(id=course_id).first()
    org = course.organisation
    org_name = org.name if org else "Nu-Age Platform"
    student_name = f"{user.first_name} {user.last_name}"
    credential_id = f"NU-{str(uuid.uuid4())[:8].upper()}"
    completion_date = datetime.now().strftime("%B %d, %Y")
    
    print(f"[DEBUG] Processing for Student: {student_name}, Course: {course.name}")
    
    # ==========================================
    # 3. THE PDF STAMPING PIPELINE
    # ==========================================
    try:
        template_path = "templates/blank_certificate.pdf"
        doc = fitz.open(template_path)
        page = doc[0] 
        
        # --- STAMP: Student Name ---
        # Centered correctly across the whole page width
        name_rect = fitz.Rect(0, 275, 842, 350) 
        page.insert_textbox(name_rect, student_name, fontsize=38, fontname="tiit", color=(0.1, 0.1, 0.1), align=fitz.TEXT_ALIGN_CENTER)
        
        # --- STAMP: Course Name ---
        course_rect = fitz.Rect(0, 375, 842, 415)
        page.insert_textbox(course_rect, course.name, fontsize=18, fontname="tibo", color=(0.01, 0.35, 0.0), align=fitz.TEXT_ALIGN_CENTER)
        
        # --- STAMP: Organization Line ---
        org_rect = fitz.Rect(0, 415, 842, 435)
        page.insert_textbox(org_rect, f"Offered by {org_name}", fontsize=10, fontname="helv", color=(0.53, 0.50, 0.44), align=fitz.TEXT_ALIGN_CENTER)

        # --- STAMP: Date ---
        # Placed precisely over the left line
        date_rect = fitz.Rect(100, 490, 217, 510)
        page.insert_textbox(date_rect, completion_date, fontsize=10, fontname="hebo", color=(0.26, 0.26, 0.26), align=fitz.TEXT_ALIGN_CENTER)

        # --- STAMP: Credential ID ---
        id_rect = fitz.Rect(455, 560, 842, 575)
        page.insert_textbox(id_rect, credential_id, fontsize=8, fontname="helv", color=(0.73, 0.73, 0.73), align=fitz.TEXT_ALIGN_LEFT)

        asset_bytes = doc.write()
        doc.close()
        print(f"[DEBUG] PDF generated successfully in memory. Size: {len(asset_bytes)} bytes.")

    except Exception as e:
        print(f"[DEBUG] CRITICAL ERROR DURING STAMPING: {e}")
        raise HTTPException(status_code=500, detail="Failed to process certificate template.")
    # ==========================================

    # 4. Upload to Bunny Storage
    safe_name = f"certificate_{credential_id}.pdf"
    folder_path = f"courses/{str(course_id)}/certificates" 
    
    print(f"[DEBUG] Attempting Bunny CDN Upload to folder: {folder_path} as {safe_name}")
    try:
        cert_url = await upload_bytes_to_bunny(asset_bytes, safe_name, folder_path)
        print(f"[DEBUG] BunnyCDN Upload SUCCESS. Returned URL: {cert_url}")
        
        # 5. Database Commit
        print("[DEBUG] Attempting to save to Database...")
        enrollment.certificate_url = cert_url
        enrollment.credential_id = credential_id
        db.commit()
        
        # Force SQLAlchemy to reload the row to verify it actually saved
        db.refresh(enrollment)
        print(f"[DEBUG] DB Commit Verified. Enrollment cert_url is now: {enrollment.certificate_url}")

        print("--- [DEBUG] CERTIFICATE PIPELINE COMPLETE ---\n")
        return {
            "message": "Certificate generated successfully", 
            "url": cert_url, 
            "credential_id": credential_id
        }
        
    except Exception as e:
        print(f"[DEBUG] CRITICAL ERROR DURING UPLOAD OR DB COMMIT: {e}")
        # Rollback just in case the DB locked up
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to finalize certificate upload.")