from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form,BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy.sql.expression import func
from typing import List, Optional
from datetime import datetime, timezone, timedelta
import uuid
from pydantic import BaseModel
from services.ai_service import process_and_generate_content, get_material_generation_progress
import models
import schemas
from database import get_db
from services import auth, ai_service
import fitz # PyMuPDF
from services.bunny_service import upload_bytes_to_bunny

router = APIRouter(prefix="/study", tags=["Self Study"])

# ==========================================
# 1. FLASHCARDS & SRS ENGINE
# ==========================================

@router.get("/cards/due", response_model=List[schemas.FlashcardResponse])
def get_due_cards(material_ids: Optional[str] = None, all_cards: bool = False, db: Session = Depends(get_db), user = Depends(auth.get_current_user)):
    query = db.query(models.Flashcard).filter(
        models.Flashcard.user_id == user.id,
    )
    if not all_cards:
        query = query.filter(models.Flashcard.next_review_date <= datetime.now(timezone.utc))
    if material_ids:
        ids_list = [uuid.UUID(i.strip()) for i in material_ids.split(",")]
        query = query.filter(models.Flashcard.material_id.in_(ids_list))
    cards_list = query.order_by(models.Flashcard.created_at.asc()).all()
    
    response = []
    for card in cards_list:
        response.append({
            "id": card.id,
            "front": card.front,
            "back": card.back,
            "material_id": card.material_id,
            "srs_state": {
                "interval": card.interval_days,
                "ease_factor": card.ease_factor,
                "repetitions": card.repetitions
            }
        })
    return response

@router.get("/cards/all", response_model=List[schemas.FlashcardResponse])
def get_all_cards(material_ids: Optional[str] = None, db: Session = Depends(get_db), user = Depends(auth.get_current_user)):
    return get_due_cards(material_ids=material_ids, all_cards=True, db=db, user=user)

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
    files: Optional[List[UploadFile]] = File(None),
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
    
    # Consolidate single file and multi files
    all_incoming_files = []
    if files:
        all_incoming_files.extend(files)
    if file and file not in all_incoming_files:
        all_incoming_files.insert(0, file)

    image_exts = {"png", "jpg", "jpeg", "webp"}
    collected_images = []

    if all_incoming_files:
        # Check if the upload contains images
        for f in all_incoming_files:
            ext = f.filename.split(".")[-1].lower() if f.filename else ""
            if ext in image_exts:
                f_bytes = await f.read()
                mime = f.content_type or (f"image/{ext}" if ext != "jpg" else "image/jpeg")
                collected_images.append({
                    "bytes": f_bytes,
                    "mime_type": mime,
                    "name": f.filename or "image.jpg"
                })

        if collected_images:
            source_type = "photo"
            print(f"[DEBUG] Processing cluster of {len(collected_images)} study images with Vision AI...")
            try:
                content_text = await ai_service.extract_text_from_study_images(collected_images)
                print(f"[DEBUG] Vision AI extracted {len(content_text)} characters of Markdown content.")
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Failed to transcribe study images: {e}")
            # Note: per user decision, images do not need to be uploaded to CDN
        else:
            # Single non-image document (PDF, TXT, MD)
            primary_file = all_incoming_files[0]
            source_type = primary_file.filename.split(".")[-1].lower()
            file_bytes = await primary_file.read()

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
                # TXT / MD file
                content_text = file_bytes.decode('utf-8', errors='ignore')

            # 2. Upload the raw document file to BunnyCDN
            safe_name = f"material_{str(uuid.uuid4())[:8]}_{primary_file.filename}"
            folder_path = f"users/{str(user.id)}/materials"
            try:
                print(f"[DEBUG] Uploading {safe_name} to BunnyCDN...")
                file_url = await upload_bytes_to_bunny(file_bytes, safe_name, folder_path)
                print(f"[DEBUG] Upload successful: {file_url}")
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"[ERROR] BunnyCDN Upload failed: {repr(e)}")
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
        file_url=file_url
    )
    db.add(new_mat)
    db.commit()
    db.refresh(new_mat)
    
    return {"material_id": new_mat.id, "message": "Material saved and uploaded successfully."}


class YouTubeImportRequest(BaseModel):
    url: str
    title: Optional[str] = None


@router.post("/materials/youtube", response_model=schemas.UploadResponse)
async def import_youtube_material(
    payload: YouTubeImportRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user = Depends(auth.get_current_user)
):
    import re
    import urllib.parse
    import httpx
    from database import Settings

    raw_url = payload.url.strip()
    match = re.search(
        r"(?:https?:\/\/)?(?:www\.|m\.)?(?:youtube\.com\/(?:watch\?v=|embed\/|v\/|shorts\/)|youtu\.be\/)([a-zA-Z0-9_-]{11})",
        raw_url,
        re.IGNORECASE
    )
    if not match:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL format.")
    video_id = match.group(1)

    # Usage check
    sub = db.query(models.UserSubscription).filter(
        models.UserSubscription.user_id == user.id
    ).first()
    if not sub:
        sub = models.UserSubscription(user_id=user.id, plan_id="free")
        db.add(sub)
        db.commit()
        db.refresh(sub)
    if sub.plan and sub.plan.materials_limit is not None and sub.materials_uploaded >= sub.plan.materials_limit:
        raise HTTPException(
            status_code=403,
            detail="You have reached your material upload limit. Please upgrade your plan."
        )

    from monetization_models import CreditBalance, CreditLedgerEntry
    credit_bal = db.query(CreditBalance).filter(CreditBalance.user_id == user.id).first()
    has_gen_quota = (
        not sub.plan
        or sub.plan.generations_limit is None
        or sub.generations_used < sub.plan.generations_limit
        or (credit_bal and credit_bal.balance > 0)
    )
    if not has_gen_quota:
        raise HTTPException(
            status_code=403,
            detail="You have reached your AI generation limit for this cycle. Please upgrade your plan or purchase credits."
        )

    # Fetch YouTube snippet
    yt_key = getattr(Settings(), "YOUTUBE_API_KEY", "") or ""
    video_title = payload.title or ""
    video_desc = ""
    channel = ""

    if yt_key:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client_http:
                resp = await client_http.get(
                    "https://www.googleapis.com/youtube/v3/videos",
                    params={"part": "snippet", "id": video_id, "key": yt_key}
                )
                if resp.status_code == 200:
                    items = resp.json().get("items", [])
                    if items:
                        snippet = items[0].get("snippet", {})
                        if not video_title:
                            video_title = snippet.get("title", "")
                        video_desc = snippet.get("description", "")
                        channel = snippet.get("channelTitle", "")
        except Exception as yt_err:
            print(f"[WARNING] YouTube Data API lookup failed: {yt_err}")

    if not video_title:
        video_title = f"YouTube Study Video ({video_id})"

    # Synthesize educational study notes from video context
    study_content = f"# {video_title}\n\n"
    if channel:
        study_content += f"**Channel:** {channel}\n\n"
    study_content += f"**Source Video:** {raw_url}\n\n"

    try:
        from services.ai_service import client
        prompt = (
            f"You are an expert pedagogical study coach. Analyze this YouTube educational video context and produce "
            f"comprehensive, structured self-study material.\n\n"
            f"Video Title: {video_title}\n"
            f"Channel: {channel}\n"
            f"Video Description / Context:\n{video_desc[:2500]}\n\n"
            f"Generate structured study notes in clean Markdown covering:\n"
            f"1. Core Conceptual Overview\n"
            f"2. Key Terminology & Definitions\n"
            f"3. 3-5 Foundational Takeaways\n"
            f"4. Practice & Self-Check Review Questions with Brief Answers\n"
        )
        completion = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a concise, structured educational assistant creating study notes from video context."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=1500,
        )
        ai_notes = completion.choices[0].message.content.strip()
        study_content += ai_notes
    except Exception as ai_err:
        print(f"[WARNING] AI synthesis from video failed, using raw description fallback: {ai_err}")
        study_content += f"## Video Summary & Notes\n\n{video_desc or 'Educational video notes from YouTube.'}"

    # Increment and save
    sub.materials_uploaded += 1
    db.commit()

    new_mat = models.StudyMaterial(
        user_id=user.id,
        title=video_title,
        source_type="youtube",
        content=study_content,
        file_url=raw_url
    )
    db.add(new_mat)
    db.commit()
    db.refresh(new_mat)

    # Queue generation engine for the newly imported YouTube material and count toward student quota
    if sub.plan and sub.plan.generations_limit is not None and sub.generations_used >= sub.plan.generations_limit:
        if credit_bal and credit_bal.balance > 0:
            credit_bal.balance -= 1
            db.add(CreditLedgerEntry(
                user_id=user.id,
                delta=-1,
                reason="generation_spend",
                reference_id=str(new_mat.id),
                balance_after=credit_bal.balance,
            ))
    else:
        sub.generations_used += 1

    new_mat.is_generating = True
    db.commit()

    background_tasks.add_task(
        process_and_generate_content,
        user_id=str(user.id),
        material_ids=[str(new_mat.id)],
        content_text=study_content,
        types_requested=["flashcards", "quiz", "exam"]
    )

    return {"material_id": new_mat.id, "message": "YouTube video imported and study generation initiated."}


@router.get("/youtube/recommendations")
async def get_youtube_recommendations(
    query: str,
    limit: int = 4,
    db: Session = Depends(get_db),
    user = Depends(auth.get_current_user)
):
    """Finds educational YouTube recommendations related to a study query."""
    import httpx
    import urllib.parse
    from database import Settings

    clean_q = query.strip()
    if not clean_q:
        return []

    yt_key = getattr(Settings(), "YOUTUBE_API_KEY", "") or ""
    results = []
    if yt_key:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client_http:
                resp = await client_http.get(
                    "https://www.googleapis.com/youtube/v3/search",
                    params={
                        "part": "snippet",
                        "q": f"{clean_q} tutorial",
                        "type": "video",
                        "maxResults": limit,
                        "key": yt_key
                    }
                )
                if resp.status_code == 200:
                    for item in resp.json().get("items", []):
                        vid_id = item.get("id", {}).get("videoId")
                        snippet = item.get("snippet", {})
                        if vid_id and snippet:
                            results.append({
                                "video_id": vid_id,
                                "url": f"https://www.youtube.com/watch?v={vid_id}",
                                "title": snippet.get("title", ""),
                                "channel": snippet.get("channelTitle", ""),
                                "thumbnail": snippet.get("thumbnails", {}).get("medium", {}).get("url", "")
                            })
        except Exception as e:
            print(f"[WARNING] YouTube search failed: {e}")

    if not results:
        # Fallback to search query link
        encoded = urllib.parse.quote(f"{clean_q} tutorial")
        results.append({
            "video_id": "",
            "url": f"https://www.youtube.com/results?search_query={encoded}",
            "title": f"YouTube search: {clean_q} tutorial",
            "channel": "YouTube Search",
            "thumbnail": ""
        })

    return results


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
    
    return [
        {
            "id": q.id,
            "question": q.question_text,
            "options": q.options,
            "answer": q.answer_index,
            "explanation": q.explanation,
            "image_url": q.image_url,
            "topic": q.topic,
            "exam_type": q.exam_type,
            "exam_year": q.exam_year,
        }
        for q in questions
    ]

@router.get("/exam/questions", response_model=schemas.ExamResponse)
def get_exam_questions(
    material_ids: Optional[str] = None,
    db: Session = Depends(get_db),
    user = Depends(auth.get_current_user)
):
    """Pulls up to 50 random questions for a full exam simulation,
    scoped to the given materials, with duration respecting pack settings or computed server-side."""
    query = db.query(models.Question).filter(models.Question.user_id == user.id)

    authoritative_duration = None
    if material_ids:
        ids_list = [uuid.UUID(i.strip()) for i in material_ids.split(",") if i.strip()]
        query = query.filter(models.Question.material_id.in_(ids_list))

        # Check if any material was imported from a curated study pack with authoritative exam duration
        dl = db.query(models.StudyPackDownload).filter(
            models.StudyPackDownload.user_id == user.id,
            models.StudyPackDownload.imported_material_id.in_(ids_list)
        ).first()
        if dl:
            pack = db.query(models.StudyPack).filter(models.StudyPack.id == dl.pack_id).first()
            if pack and pack.pack_data and pack.pack_data.get("duration_seconds"):
                authoritative_duration = int(pack.pack_data["duration_seconds"])

    questions = query.order_by(func.random()).limit(50).all()

    question_payload = [
        {
            "id": q.id,
            "question": q.question_text,
            "options": q.options,
            "answer": q.answer_index,
            "explanation": q.explanation,
            "image_url": q.image_url,
            "topic": q.topic,
            "exam_type": q.exam_type,
            "exam_year": q.exam_year,
        }
        for q in questions
    ]

    duration_seconds = authoritative_duration if authoritative_duration else max(len(question_payload) * 60, 300)

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
    """The endpoint the frontend polls for granular generation progress and counts."""
    material = db.query(models.StudyMaterial).filter(
        models.StudyMaterial.id == material_id,
        models.StudyMaterial.user_id == user.id
    ).first()
    
    if not material:
        raise HTTPException(status_code=404, detail="Material not found.")
        
    prog = get_material_generation_progress(str(material_id))
    fc_count = db.query(models.Flashcard).filter(models.Flashcard.material_id == material_id).count()
    q_count = db.query(models.Question).filter(models.Question.material_id == material_id).count()

    is_generating = bool(material.is_generating or prog.get("is_generating", False))
    return {
        "status": "processing" if is_generating else "completed",
        "is_generating": is_generating,
        "progress_percent": prog.get("progress_percent", 100 if not is_generating else 25),
        "stage": prog.get("stage", "Ready to study" if not is_generating else "Generating study engine..."),
        "flashcards_count": max(fc_count, prog.get("flashcards_count", 0)),
        "questions_count": max(q_count, prog.get("questions_count", 0)),
    }


# Drop-in replacement for generate_study_content() in study.py.
# Only this one function changes — nothing else in study.py needs to move.
# Add this import near the top of study.py, alongside the existing imports:
#
from monetization_models import CreditBalance, CreditLedgerEntry

@router.post("/generate")
async def generate_study_content(
    payload: schemas.GeneratePayload,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user = Depends(auth.get_current_user)
):
    # --- 1. THE GENERATION LIMIT CHECK (free quota, then credits fallback) ---
    sub = db.query(models.UserSubscription).filter(
        models.UserSubscription.user_id == user.id
    ).first()

    if not sub:
        sub = models.UserSubscription(user_id=user.id, plan_id="free")
        db.add(sub)
        db.commit()
        db.refresh(sub)

    credit_source = "free_quota"
    credit_balance = None  # only set if we end up spending a credit

    if sub.plan.generations_limit is not None and sub.generations_used >= sub.plan.generations_limit:
        # Free quota exhausted — fall back to purchased credits instead of
        # hard-403ing, same idea as the old check but with a paid path.
        credit_balance = db.query(CreditBalance).filter(CreditBalance.user_id == user.id).first()

        if not credit_balance or credit_balance.balance <= 0:
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "insufficient_generations",
                    "free_used": sub.generations_used,
                    "free_limit": sub.plan.generations_limit,
                    "credits_remaining": credit_balance.balance if credit_balance else 0,
                    "message": "You're out of free generations and credits. Buy a credit pack to continue.",
                }
            )

        credit_balance.balance -= 1
        db.add(CreditLedgerEntry(
            user_id=user.id,
            delta=-1,
            reason="generation_spend",
            reference_id=",".join(str(m) for m in payload.material_ids),
            balance_after=credit_balance.balance,
        ))
        credit_source = "credits"
    else:
        sub.generations_used += 1
    # --------------------------------------

    materials = db.query(models.StudyMaterial).filter(
        models.StudyMaterial.id.in_(payload.material_ids)
    ).all()

    if not materials:
        # If the material isn't found, refund whatever we just charged —
        # credits if that's what was spent, otherwise the free quota tick.
        if credit_source == "credits":
            credit_balance.balance += 1
            db.add(CreditLedgerEntry(
                user_id=user.id,
                delta=1,
                reason="generation_refund",
                reference_id=",".join(str(m) for m in payload.material_ids),
                balance_after=credit_balance.balance,
            ))
        else:
            sub.generations_used -= 1
        db.commit()
        raise HTTPException(status_code=404, detail="Materials not found.")

    # Lock the materials in the database
    for mat in materials:
        mat.is_generating = True

    # Commit both the lock AND the usage/credit change at the same time
    db.commit()

    combined_text = "\n\n".join([m.content for m in materials if m.content])

    # 2. Queue the heavy AI lifting
    background_tasks.add_task(
        process_and_generate_content,
        user_id=str(user.id),
        material_ids=[str(m.id) for m in materials],  # Pass IDs to unlock them later
        content_text=combined_text,
        types_requested=payload.types
    )

    # 3. Return instantly
    return {"message": "AI Generation started", "status": "processing", "source": credit_source}


class AIDoubtPayload(BaseModel):
    query: str
    module_title: Optional[str] = "Module"
    lesson_title: Optional[str] = "Lesson"
    course_title: Optional[str] = "Course"
    lesson_content: Optional[str] = ""
    is_assessment: Optional[bool] = False
    conversation_history: Optional[List[dict]] = None
    material_id: Optional[str] = None


@router.post("/ai-tutor")
async def ask_ai_tutor(
    payload: AIDoubtPayload,
    user = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    """
    Contextual, guarded AI Doubt Assistant answering student queries
    grounded in the active course module and lesson via OpenAI pipeline.
    """
    try:
        content_to_use = payload.lesson_content or ""
        if payload.material_id:
            try:
                mat_record = db.query(models.StudyMaterial).filter(
                    models.StudyMaterial.id == payload.material_id,
                    models.StudyMaterial.user_id == user.id,
                ).first()
                if mat_record and mat_record.content:
                    content_to_use = mat_record.content
            except Exception as mat_err:
                print(f"[ask_ai_tutor] Error loading material content: {mat_err}")

        reply = await ai_service.ask_ai_tutor_response(
            query=payload.query,
            course_title=payload.course_title,
            module_title=payload.module_title,
            lesson_title=payload.lesson_title,
            lesson_content=content_to_use,
            conversation_history=payload.conversation_history or [],
            is_assessment=payload.is_assessment or False,
        )
        return {"reply": reply}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI Tutor service error: {str(e)}")


@router.delete("/materials/{material_id}")
def delete_study_material(
    material_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user)
):
    """
    Deletes a study material (uploaded note, video, or downloaded study pack)
    and cascades deletion of all associated flashcards and questions from the user's vault.
    If the material was a downloaded pack, cleans up the download record so the marketplace state is fresh.
    """
    mat = db.query(models.StudyMaterial).filter(
        models.StudyMaterial.id == material_id,
        models.StudyMaterial.user_id == user.id
    ).first()

    if not mat:
        raise HTTPException(status_code=404, detail="Material not found in your vault.")

    # 1. If it was imported from a study pack, remove the download association
    dl = db.query(models.StudyPackDownload).filter(
        models.StudyPackDownload.imported_material_id == material_id,
        models.StudyPackDownload.user_id == user.id
    ).first()
    if dl:
        db.delete(dl)

    # 2. Delete associated flashcards & questions
    db.query(models.Flashcard).filter(models.Flashcard.material_id == material_id).delete(synchronize_session=False)
    db.query(models.Question).filter(models.Question.material_id == material_id).delete(synchronize_session=False)

    # 3. Delete the material itself
    db.delete(mat)

    # 4. Decrement uploaded materials count if applicable
    sub = db.query(models.UserSubscription).filter(models.UserSubscription.user_id == user.id).first()
    if sub and sub.materials_uploaded and sub.materials_uploaded > 0:
        sub.materials_uploaded = max(0, sub.materials_uploaded - 1)

    db.commit()
    return {"success": True, "message": "Material removed from vault."}