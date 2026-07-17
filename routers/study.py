from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form,BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy.sql.expression import func
from typing import List, Optional
from datetime import datetime, timezone, timedelta
import uuid
from services.ai_service import process_and_generate_content
import models
import schemas
from database import get_db
from services import auth, ai_service # Assuming this is your auth dependency
from services.ai_service import process_and_generate_content
import fitz # PyMuPDF
from services.bunny_service import upload_bytes_to_bunny

router = APIRouter(prefix="/study", tags=["Self Study"])

# ==========================================
# 1. FLASHCARDS & SRS ENGINE
# ==========================================

@router.get("/cards/due", response_model=List[schemas.FlashcardResponse])
def get_due_cards(material_ids: Optional[str] = None, db: Session = Depends(get_db), user = Depends(auth.get_current_user)):
    query = db.query(models.Flashcard).filter(
        models.Flashcard.user_id == user.id,
        models.Flashcard.next_review_date <= datetime.now()
    )
    if material_ids:
        ids_list = [uuid.UUID(i.strip()) for i in material_ids.split(",")]
        query = query.filter(models.Flashcard.material_id.in_(ids_list))
    due_cards = query.all()
    
    response = []
    for card in due_cards:
        response.append({
            "id": card.id,
            "front": card.front,
            "back": card.back,
            "srs_state": {
                "interval": card.interval_days,
                "ease_factor": card.ease_factor,
                "repetitions": card.repetitions
            }
        })
    return response

@router.post("/review", response_model=schemas.ReviewResponse)
def review_card(payload: schemas.ReviewPayload, db: Session = Depends(get_db), user = Depends(auth.get_current_user)):
    """Executes the SM-2 Spaced Repetition Algorithm."""
    card = db.query(models.Flashcard).filter(
        models.Flashcard.id == payload.card_id, 
        models.Flashcard.user_id == user.id
    ).first()
    
    if not card:
        raise HTTPException(status_code=404, detail="Card not found.")

    q = payload.quality

    # Handle correct responses (quality >= 3) vs incorrect (quality < 3)
    if q >= 3:
        if card.repetitions == 0:
            card.interval_days = 1
        elif card.repetitions == 1:
            card.interval_days = 6
        else:
            card.interval_days = round(card.interval_days * card.ease_factor)
        card.repetitions += 1
    else:
        card.repetitions = 0
        card.interval_days = 1 # Reset to see it tomorrow
    
    # Calculate new Ease Factor (Minimum ease is 1.3)
    card.ease_factor = card.ease_factor + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))
    card.ease_factor = max(1.3, card.ease_factor)

    # Set new review date
    card.next_review_date = datetime.now(timezone.utc) + timedelta(days=card.interval_days)
    
    db.commit()
    db.refresh(card)
    
    return {"next_review_date": card.next_review_date, "interval_days": card.interval_days}

@router.post("/cards/save", response_model=dict)
def save_custom_card(payload: schemas.SaveFlashcardPayload, db: Session = Depends(get_db), user = Depends(auth.get_current_user)):
    """Creates a custom flashcard directly."""
    new_card = models.Flashcard(
        user_id=user.id,
        material_id=payload.source_material_id,
        front=payload.front,
        back=payload.back,
        next_review_date=datetime.now(timezone.utc)
    )
    db.add(new_card)
    db.commit()
    return {"id": str(new_card.id)}

# ==========================================
# 2. STUDY MATERIALS & UPLOADS
# ==========================================

@router.get("/materials", response_model=List[schemas.MaterialResponse])
def get_materials(db: Session = Depends(get_db), user = Depends(auth.get_current_user)):
    return db.query(models.StudyMaterial).filter(models.StudyMaterial.user_id == user.id).all()

@router.post("/materials/upload", response_model=schemas.UploadResponse)
async def upload_material(
    title: str = Form(...),
    pasted_text: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    user = Depends(auth.get_current_user)
):
    # --- 1. THE USAGE CHECK & INCREMENT ---
    sub = db.query(models.UserSubscription).filter(
        models.UserSubscription.user_id == user.id
    ).first()
    
    # If they don't have a sub, make a free one
    if not sub:
        sub = models.UserSubscription(user_id=user.id, plan_id="free")
        db.add(sub)
        db.commit()
        db.refresh(sub)
        
    # Check if they hit the limit (and ignore if limit is None for 'unlimited')
    if sub.plan.materials_limit is not None and sub.materials_uploaded >= sub.plan.materials_limit:
        raise HTTPException(
            status_code=403, 
            detail="You have reached your material upload limit. Please upgrade your plan."
        )

    # They passed the check, increment the counter!
    sub.materials_uploaded += 1
    db.commit()
    # --------------------------------------

    source_type = "text"
    content_text = ""
    file_url = None
    
    if file:
        source_type = file.filename.split(".")[-1].lower()
        file_bytes = await file.read()
        
        # 1. Extract Text from PDF
        if source_type == "pdf":
            try:
                doc = fitz.open(stream=file_bytes, filetype="pdf")
                for page in doc:
                    content_text += page.get_text("text") + "\n"
                doc.close()
                print(f"[DEBUG] Extracted {len(content_text)} characters from PDF.")
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Failed to read PDF: {e}")
        else:
            # If it is a TXT file
            content_text = file_bytes.decode('utf-8', errors='ignore')

        # 2. Upload the raw file to BunnyCDN
        safe_name = f"material_{str(uuid.uuid4())[:8]}_{file.filename}"
        folder_path = f"users/{str(user.id)}/materials"
        
        try:
            print(f"[DEBUG] Uploading {safe_name} to BunnyCDN...")
            file_url = await upload_bytes_to_bunny(file_bytes, safe_name, folder_path)
            print(f"[DEBUG] Upload successful: {file_url}")
        except Exception as e:
            import traceback
            traceback.print_exc() # This will print the exact line and HTTP error code!
            print(f"[ERROR] BunnyCDN Upload failed: {repr(e)}") # repr() forces it to show the object
            raise HTTPException(status_code=500, detail="Failed to upload file to CDN.")

    elif pasted_text:
        source_type = "pasted_text"
        content_text = pasted_text
    else:
        raise HTTPException(status_code=400, detail="Must provide text or file.")

    # 3. Save Material to Database
    new_mat = models.StudyMaterial(
        user_id=user.id, 
        title=title, 
        source_type=source_type, 
        content=content_text,
        file_url=file_url # Save the CDN link!
    )
    db.add(new_mat)
    db.commit()
    db.refresh(new_mat)
    
    return {"material_id": new_mat.id, "message": "Material saved and uploaded successfully."}

# ==========================================
# 3. ASSESSMENTS (QUIZZES & EXAMS)
# ==========================================

@router.get("/quiz/questions", response_model=List[schemas.QuestionResponse])
def get_quiz_questions(material_ids: Optional[str] = None, db: Session = Depends(get_db), user = Depends(auth.get_current_user)):
    """Pulls 10 random questions for a quick quiz."""
    query = db.query(models.Question).filter(models.Question.user_id == user.id)
    
    if material_ids:
        ids_list = [uuid.UUID(i.strip()) for i in material_ids.split(",")]
        query = query.filter(models.Question.material_id.in_(ids_list))
        
    questions = query.order_by(func.random()).limit(10).all()
    
    return [{"id": q.id, "question": q.question_text, "options": q.options, "answer": q.answer_index, "explanation": q.explanation} for q in questions]

@router.get("/exam/questions", response_model=schemas.ExamResponse)
def get_exam_questions(
    material_ids: Optional[str] = None,
    db: Session = Depends(get_db),
    user = Depends(auth.get_current_user)
):
    """Pulls up to 50 random questions for a full exam simulation,
    scoped to the given materials, with a duration computed server-side."""
    query = db.query(models.Question).filter(models.Question.user_id == user.id)

    if material_ids:
        ids_list = [uuid.UUID(i.strip()) for i in material_ids.split(",")]
        query = query.filter(models.Question.material_id.in_(ids_list))

    questions = query.order_by(func.random()).limit(50).all()

    question_payload = [
        {
            "id": q.id,
            "question": q.question_text,
            "options": q.options,
            "answer": q.answer_index,
            "explanation": q.explanation,
        }
        for q in questions
    ]

    # 90 sec/question, 5 min floor — same policy the frontend used to apply
    # client-side; now it's authoritative and server-controlled instead.
    duration_seconds = max(len(question_payload) * 60, 300)

    return {
        "questions": question_payload,
        "duration_seconds": duration_seconds,
    }

# ==========================================
# 4. AI GENERATION STUB
# ==========================================


@router.get("/materials/{material_id}/status")
def get_material_status(
    material_id: uuid.UUID, 
    db: Session = Depends(get_db), 
    user = Depends(auth.get_current_user)
):
    """The endpoint the frontend polls every 4 seconds."""
    material = db.query(models.StudyMaterial).filter(
        models.StudyMaterial.id == material_id,
        models.StudyMaterial.user_id == user.id
    ).first()
    
    if not material:
        raise HTTPException(status_code=404, detail="Material not found.")
        
    return {
        "status": "processing" if material.is_generating else "completed"
    }


@router.post("/generate")
async def generate_study_content(
    payload: schemas.GeneratePayload, 
    background_tasks: BackgroundTasks, 
    db: Session = Depends(get_db), 
    user = Depends(auth.get_current_user)
):
    # --- 1. THE GENERATION LIMIT CHECK ---
    sub = db.query(models.UserSubscription).filter(
        models.UserSubscription.user_id == user.id
    ).first()
    
    if not sub:
        sub = models.UserSubscription(user_id=user.id, plan_id="free")
        db.add(sub)
        db.commit()
        db.refresh(sub)

    if sub.plan.generations_limit is not None and sub.generations_used >= sub.plan.generations_limit:
        raise HTTPException(
            status_code=403, 
            detail="You have reached your AI generation limit. Please upgrade your plan."
        )

    # Charge them for the generation!
    sub.generations_used += 1
    # --------------------------------------

    materials = db.query(models.StudyMaterial).filter(
        models.StudyMaterial.id.in_(payload.material_ids)
    ).all()
    
    if not materials:
        # If the material isn't found, refund the generation we just charged them
        sub.generations_used -= 1
        db.commit()
        raise HTTPException(status_code=404, detail="Materials not found.")

    # Lock the materials in the database
    for mat in materials:
        mat.is_generating = True
        
    # Commit both the lock AND the usage increment at the same time
    db.commit()

    combined_text = "\n\n".join([m.content for m in materials if m.content])
    
    # 2. Queue the heavy AI lifting
    background_tasks.add_task(
        process_and_generate_content, 
        user_id=str(user.id),
        material_ids=[str(m.id) for m in materials], # Pass IDs to unlock them later
        content_text=combined_text,
        types_requested=payload.types
    )

    # 3. Return instantly
    return {"message": "AI Generation started", "status": "processing"}

