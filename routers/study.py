from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form,BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import or_
from sqlalchemy.sql.expression import func
from typing import List, Optional
from datetime import datetime, timezone, timedelta
import uuid
import random
from pydantic import BaseModel
from services.ai_service import (
    process_and_generate_content,
    get_material_generation_progress,
    get_journal_generation_progress,
    process_and_generate_journal_content,
    ask_journal_tutor_response,
)
import models
import schemas
from database import get_db
from services import auth, ai_service
import fitz # PyMuPDF
import re
from services.bunny_service import upload_bytes_to_bunny

def _clean_question_text_prompt(text: str) -> str:
    """Strips difficulty prefixes like [Exam Level], [Exam-Level], [Tricky], etc. from question text."""
    if not text:
        return ""
    clean = re.sub(
        r'^\s*[\(\[]?\s*(?:exam[\s\-_]?level|tricky|straightforward|easy|medium|hard)\s*[\)\]]?[\s:\-–—]*',
        '',
        str(text),
        flags=re.IGNORECASE
    ).strip()
    return clean or str(text)

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
    
    try:
        from services import coin_service
        coin_service.record_metric_event(db, user.id, "practice_questions", 1)
    except Exception as ex:
        print(f"[OBJECTIVES] Error tracking practice_questions in review_card: {ex}")
    
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
    from services.subscription_service import check_material_upload_allowed
    sub = check_material_upload_allowed(user.id, db)

    # Passed check, increment uploaded count
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
    from services.subscription_service import check_material_upload_allowed, get_effective_subscription
    sub = check_material_upload_allowed(user.id, db)
    _, effective_plan, _, is_expired = get_effective_subscription(user.id, db)

    plan_limit = effective_plan.generations_limit if effective_plan and effective_plan.generations_limit is not None else None
    has_gen_quota = (
        not effective_plan
        or plan_limit is None
        or sub.generations_used < plan_limit
        or (sub.bonus_generations or 0) > 0
    )
    if not has_gen_quota:
        if is_expired:
            raise HTTPException(
                status_code=403,
                detail="Your subscription has expired. Please renew your plan or purchase credits to continue generating study content."
            )
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
        from database import Settings
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
        settings = Settings()
        completion = await client.chat.completions.create(
            model=settings.get_openai_model(),
            messages=[
                {"role": "system", "content": "You are a concise, structured educational assistant creating study notes from video context."},
                {"role": "user", "content": prompt}
            ],
            reasoning_effort=settings.get_openai_reasoning_effort("chat"),
            max_completion_tokens=1500,
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
    plan_limit = sub.plan.generations_limit if sub.plan and sub.plan.generations_limit is not None else None
    if plan_limit is not None and sub.generations_used >= plan_limit:
        if (sub.bonus_generations or 0) > 0:
            sub.bonus_generations -= 1
        else:
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "insufficient_generations",
                    "free_used": sub.generations_used,
                    "free_limit": plan_limit,
                    "bonus_generations": 0,
                    "message": "You're out of monthly generation quota. Exchange Nu-Coins for AI generation credits in the Store or top up.",
                }
            )
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
            "question": _clean_question_text_prompt(q.question_text),
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

def _compute_smart_exam_duration(qs, mat_titles_str="", auth_dur=None, total_pack_questions=None) -> int:
    """Computes exam duration cognizant of real-world exam standards (JAMB, WAEC, NECO, Post-UTME, etc.)."""
    if not qs:
        return 300

    # 1. Check explicit exam_type on questions
    exam_std = None
    for q in qs:
        et = str(getattr(q, "exam_type", None) or (q.get("exam_type") if isinstance(q, dict) else None) or "").strip().upper()
        if et:
            exam_std = et
            break

    # 2. Fallback: inspect material titles / subject keywords
    if not exam_std and mat_titles_str:
        t_up = mat_titles_str.upper()
        if "POST-UTME" in t_up or "PUTME" in t_up:
            exam_std = "POST-UTME"
        elif "JAMB" in t_up or "UTME" in t_up:
            exam_std = "JAMB"
        elif "WAEC" in t_up or "WASSCE" in t_up:
            exam_std = "WAEC"
        elif "NECO" in t_up:
            exam_std = "NECO"
        elif "SAT" in t_up:
            exam_std = "SAT"
        elif any(x in t_up for x in ("NCLEX", "MCAT", "USMLE")):
            exam_std = "NCLEX"

    # 3. Apply official testing board pacing specifications (seconds per question)
    if exam_std:
        if "POST-UTME" in exam_std or "PUTME" in exam_std:
            # Post-UTME speed screening: ~42s per question (50 Qs = 35 mins)
            sec_per_q = 42
        elif "JAMB" in exam_std or "UTME" in exam_std:
            # JAMB CBT standard: ~48s per question (50 Qs = 40 mins)
            sec_per_q = 48
        elif "WAEC" in exam_std or "WASSCE" in exam_std:
            # WAEC/WASSCE Objective: ~72s per question (50 Qs = 60 mins)
            sec_per_q = 72
        elif "NECO" in exam_std:
            # NECO Objective standard: ~60s per question (60 Qs = 60 mins)
            sec_per_q = 60
        elif "SAT" in exam_std:
            # SAT Digital standard: ~75s per question
            sec_per_q = 75
        elif any(x in exam_std for x in ("NCLEX", "MCAT", "USMLE")):
            # Clinical reasoning: ~84s per question
            sec_per_q = 84
        else:
            sec_per_q = 60
    elif auth_dur and auth_dur > 0 and total_pack_questions and total_pack_questions > 0:
        sec_per_q = max(30, round(auth_dur / total_pack_questions))
    else:
        sec_per_q = 60

    total_secs = len(qs) * sec_per_q
    mins = max(5, round(total_secs / 60))
    return mins * 60


@router.get("/exam/categories")
def get_exam_categories(
    material_ids: Optional[str] = None,
    db: Session = Depends(get_db),
    user = Depends(auth.get_current_user)
):
    """Returns available topics/categories, question counts per topic,
    total available question count, and detected exam type for the target materials."""
    ids_list = []
    if material_ids:
        ids_list = [uuid.UUID(i.strip()) for i in material_ids.split(",") if i.strip()]

    # 1. Discover any associated StudyPacks (direct pack ID, download record, or matching material title)
    candidate_packs = []
    if ids_list:
        # Check direct pack IDs
        packs_by_id = db.query(models.StudyPack).filter(models.StudyPack.id.in_(ids_list)).all()
        candidate_packs.extend(packs_by_id)

        # Check downloaded pack mappings
        dls = db.query(models.StudyPackDownload).filter(
            models.StudyPackDownload.imported_material_id.in_(ids_list)
        ).all()
        if dls:
            packs_by_dl = db.query(models.StudyPack).filter(
                models.StudyPack.id.in_([d.pack_id for d in dls])
            ).all()
            candidate_packs.extend(packs_by_dl)

        # Check materials and find matching packs by title
        materials = db.query(models.StudyMaterial).filter(models.StudyMaterial.id.in_(ids_list)).all()
        for mat in materials:
            m_title = (mat.title or "").strip()
            if m_title:
                matching_title_packs = db.query(models.StudyPack).filter(
                    or_(
                        func.lower(models.StudyPack.title) == func.lower(m_title),
                        func.lower(models.StudyPack.title).contains(func.lower(m_title.replace("Questions", "Past Questions"))),
                        func.lower(models.StudyPack.title).contains(func.lower(m_title.replace("Past Questions", "Questions"))),
                    )
                ).all()
                candidate_packs.extend(matching_title_packs)

    unique_packs = list({p.id: p for p in candidate_packs}.values())

    # Tally questions from candidate packs
    pack_topics = {}
    pack_q_count = 0
    pack_exam_type = None
    for p in unique_packs:
        if p.pack_data and p.pack_data.get("questions"):
            pqs = p.pack_data.get("questions", [])
            pack_q_count += len(pqs)
            if not pack_exam_type and p.pack_data.get("exam_type"):
                pack_exam_type = p.pack_data.get("exam_type")
            for q in pqs:
                t = (q.get("topic") or "General").strip()
                pack_topics[t] = pack_topics.get(t, 0) + 1
                if not pack_exam_type and q.get("exam_type"):
                    pack_exam_type = q.get("exam_type")

    # 2. Tally questions from relational database
    query = db.query(models.Question)
    if ids_list:
        query = query.filter(models.Question.material_id.in_(ids_list))
    else:
        query = query.filter(models.Question.user_id == user.id)

    topic_rows = (
        query.with_entities(
            func.coalesce(func.nullif(models.Question.topic, ''), 'General').label("topic_name"),
            func.count(models.Question.id).label("q_count"),
            func.max(models.Question.exam_type).label("ex_type")
        )
        .group_by("topic_name")
        .order_by(func.count(models.Question.id).desc())
        .all()
    )

    db_total = sum(int(r.q_count) for r in topic_rows)
    db_exam_type = next((r.ex_type for r in topic_rows if r.ex_type), None)

    # Prefer pack questions when pack contains more items (e.g. 401 in pack vs 10 in cloned material)
    if pack_q_count > db_total:
        categories = [
            {"name": name, "count": cnt}
            for name, cnt in sorted(pack_topics.items(), key=lambda x: x[1], reverse=True)
        ]
        return {
            "total_questions": pack_q_count,
            "exam_type": pack_exam_type or "General CBT",
            "categories": categories,
        }

    categories = [
        {"name": r.topic_name, "count": int(r.q_count)}
        for r in topic_rows
    ]
    return {
        "total_questions": db_total,
        "exam_type": db_exam_type or pack_exam_type or "General CBT",
        "categories": categories,
    }


@router.get("/exam/questions", response_model=schemas.ExamResponse)
def get_exam_questions(
    material_ids: Optional[str] = None,
    limit: Optional[int] = 50,
    topics: Optional[str] = None,
    duration_seconds: Optional[int] = None,
    db: Session = Depends(get_db),
    user = Depends(auth.get_current_user)
):
    """Pulls random questions for a full exam simulation,
    scoped to the given materials and topics, with duration cognizant of official exam standards (JAMB, WAEC, NECO, etc.)."""
    effective_limit = min(max(5, int(limit or 50)), 500)

    # Parse topics filter if supplied
    topic_list = []
    if topics:
        topic_list = [t.strip() for t in topics.split(",") if t.strip()]

    ids_list = []
    if material_ids:
        ids_list = [uuid.UUID(i.strip()) for i in material_ids.split(",") if i.strip()]

    # 1. Discover associated StudyPacks (direct ID, download record, or matching material title)
    candidate_packs = []
    if ids_list:
        packs_by_id = db.query(models.StudyPack).filter(models.StudyPack.id.in_(ids_list)).all()
        candidate_packs.extend(packs_by_id)

        dls = db.query(models.StudyPackDownload).filter(
            models.StudyPackDownload.imported_material_id.in_(ids_list)
        ).all()
        if dls:
            packs_by_dl = db.query(models.StudyPack).filter(
                models.StudyPack.id.in_([d.pack_id for d in dls])
            ).all()
            candidate_packs.extend(packs_by_dl)

        materials = db.query(models.StudyMaterial).filter(models.StudyMaterial.id.in_(ids_list)).all()
        for mat in materials:
            m_title = (mat.title or "").strip()
            if m_title:
                matching_title_packs = db.query(models.StudyPack).filter(
                    or_(
                        func.lower(models.StudyPack.title) == func.lower(m_title),
                        func.lower(models.StudyPack.title).contains(func.lower(m_title.replace("Questions", "Past Questions"))),
                        func.lower(models.StudyPack.title).contains(func.lower(m_title.replace("Past Questions", "Questions"))),
                    )
                ).all()
                candidate_packs.extend(matching_title_packs)

    unique_packs = list({p.id: p for p in candidate_packs}.values())

    # Count DB questions for comparison
    query_db_cnt = db.query(models.Question)
    if ids_list:
        query_db_cnt = query_db_cnt.filter(models.Question.material_id.in_(ids_list))
    else:
        query_db_cnt = query_db_cnt.filter(models.Question.user_id == user.id)
    db_count = query_db_cnt.count()

    # Check if candidate packs have more questions
    all_pack_qs = []
    def _match_pack_topic(q):
        if not topic_list or any(t.lower() in ("all", "all topics") for t in topic_list):
            return True
        q_top = (q.get("topic") or "General").strip()
        if "General" in topic_list and not q_top:
            return True
        return q_top in topic_list

    for p in unique_packs:
        if p.pack_data and p.pack_data.get("questions"):
            for q in p.pack_data.get("questions", []):
                if _match_pack_topic(q):
                    all_pack_qs.append((p, q))

    if len(all_pack_qs) > db_count and len(all_pack_qs) > 0:
        sample_size = min(effective_limit, len(all_pack_qs))
        sampled = random.sample(all_pack_qs, sample_size)
        primary_pack = unique_packs[0]
        detected_exam_type = primary_pack.pack_data.get("exam_type") if primary_pack.pack_data else "General CBT"

        question_payload = [
            {
                "id": q.get("id") or str(uuid.uuid4()),
                "material_id": str(p.id),
                "subject": p.title,
                "question": _clean_question_text_prompt(q.get("question_text", "")),
                "options": q.get("options", []),
                "answer": q.get("answer_index", 0),
                "explanation": q.get("explanation", ""),
                "image_url": q.get("image_url"),
                "topic": q.get("topic"),
                "exam_type": q.get("exam_type") or detected_exam_type,
                "exam_year": q.get("exam_year"),
            }
            for p, q in sampled
        ]

        if duration_seconds and int(duration_seconds) > 0:
            computed_duration = int(duration_seconds)
        else:
            sec_per_q = 48 if "JAMB" in str(detected_exam_type).upper() else 60
            computed_duration = max(300, sample_size * sec_per_q)

        return {
            "questions": question_payload,
            "duration_seconds": computed_duration,
        }

    # Otherwise fallback to database questions
    authoritative_duration = None
    total_pack_questions = None
    questions = []
    mat_map = {}

    def _apply_topic_filter(q):
        if not topic_list or "all" in [t.lower() for t in topic_list] or "all topics" in [t.lower() for t in topic_list]:
            return q
        if "General" in topic_list:
            other = [t for t in topic_list if t != "General"]
            return q.filter(or_(models.Question.topic.in_(other), models.Question.topic == None, models.Question.topic == ''))
        return q.filter(models.Question.topic.in_(topic_list))

    if ids_list:
        materials = db.query(models.StudyMaterial).filter(models.StudyMaterial.id.in_(ids_list)).all()
        mat_map = {m.id: m.title for m in materials}

        if len(ids_list) > 1:
            per_sub = max(5, effective_limit // len(ids_list))
            total_duration_acc = 0
            for mid in ids_list:
                mat_q_query = db.query(models.Question).filter(
                    models.Question.material_id == mid
                )
                mat_q_query = _apply_topic_filter(mat_q_query)
                mat_qs = mat_q_query.order_by(func.random()).limit(per_sub).all()
                questions.extend(mat_qs)

                dl = db.query(models.StudyPackDownload).filter(
                    models.StudyPackDownload.imported_material_id == mid
                ).first()
                if dl:
                    pack = db.query(models.StudyPack).filter(models.StudyPack.id == dl.pack_id).first()
                    if pack and pack.pack_data and pack.pack_data.get("duration_seconds"):
                        total_duration_acc += int(pack.pack_data["duration_seconds"])
                    else:
                        total_duration_acc += 1800
                else:
                    total_duration_acc += 1800
            if total_duration_acc > 0:
                authoritative_duration = min(total_duration_acc, 7200)
        else:
            mid = ids_list[0]
            q_query = db.query(models.Question).filter(
                models.Question.material_id == mid
            )
            q_query = _apply_topic_filter(q_query)
            questions = q_query.order_by(func.random()).limit(effective_limit).all()

            dl = db.query(models.StudyPackDownload).filter(
                models.StudyPackDownload.imported_material_id == mid
            ).first()
            if dl:
                pack = db.query(models.StudyPack).filter(models.StudyPack.id == dl.pack_id).first()
                if pack and pack.pack_data:
                    if pack.pack_data.get("duration_seconds"):
                        authoritative_duration = int(pack.pack_data["duration_seconds"])
                    if pack.pack_data.get("questions"):
                        total_pack_questions = len(pack.pack_data["questions"])
    else:
        q_query = db.query(models.Question).filter(models.Question.user_id == user.id)
        q_query = _apply_topic_filter(q_query)
        questions = q_query.order_by(func.random()).limit(effective_limit).all()

    question_payload = [
        {
            "id": q.id,
            "material_id": q.material_id,
            "subject": mat_map.get(q.material_id),
            "question": _clean_question_text_prompt(q.question_text),
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

    mat_titles_combined = " ".join(str(t) for t in mat_map.values())
    if duration_seconds and int(duration_seconds) > 0:
        computed_duration = int(duration_seconds)
    else:
        computed_duration = _compute_smart_exam_duration(
            questions, mat_titles_combined, authoritative_duration, total_pack_questions
        )

    return {
        "questions": question_payload,
        "duration_seconds": computed_duration,
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
    from services.subscription_service import get_effective_subscription
    sub, effective_plan, status, is_expired = get_effective_subscription(user.id, db)

    credit_source = "free_quota"
    credit_balance = None  # only set if we end up spending a credit

    plan_gen_limit = effective_plan.generations_limit if effective_plan and effective_plan.generations_limit is not None else None
    if plan_gen_limit is not None and sub.generations_used >= plan_gen_limit:
        # Plan monthly quota exhausted — check bonus_generations (explicitly purchased or exchanged)
        if (sub.bonus_generations or 0) > 0:
            sub.bonus_generations -= 1
            credit_source = "bonus_generations"
        else:
            if is_expired:
                raise HTTPException(
                    status_code=402,
                    detail={
                        "error": "subscription_expired",
                        "free_used": sub.generations_used,
                        "free_limit": plan_gen_limit,
                        "bonus_generations": 0,
                        "message": "Your subscription has expired. Renew your plan or exchange Nu-Coins for AI generation credits in the Store.",
                    }
                )
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "insufficient_generations",
                    "free_used": sub.generations_used,
                    "free_limit": plan_gen_limit,
                    "bonus_generations": 0,
                    "message": "You're out of monthly generation quota. Exchange Nu-Coins for AI generation credits in the Store or top up.",
                }
            )
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


class StudySessionLogPayload(BaseModel):
    minutes: int = 0
    questions: int = 0
    minutes_spent: Optional[int] = None
    questions_completed: Optional[int] = None
    activity_type: Optional[str] = "self_study"


@router.post("/log-session")
def log_study_session(
    payload: StudySessionLogPayload,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
):
    """Logs active self-study minutes and practice questions directly to the platform objectives tracker and active duo partnership."""
    from services import coin_service
    mins = payload.minutes_spent if payload.minutes_spent is not None else payload.minutes
    qs = payload.questions_completed if payload.questions_completed is not None else payload.questions

    if mins > 0:
        coin_service.record_metric_event(db, user.id, "study_time", mins)
    if qs > 0:
        coin_service.record_metric_event(db, user.id, "practice_questions", qs)

    # Automatically advance active duo partnership daily minutes and streak if user has an accountability partner
    if mins > 0 or qs > 0:
        try:
            from routers.study_partners import _record_partner_study_activity
            active_p = db.query(models.StudyPartnership).filter(
                models.StudyPartnership.status == "active",
                or_(
                    models.StudyPartnership.user_a_id == user.id,
                    models.StudyPartnership.user_b_id == user.id,
                )
            ).first()
            if active_p:
                _record_partner_study_activity(db, active_p, user.id, minutes=mins, questions=qs)
                db.commit()
        except Exception as ex:
            db.rollback()
            print(f"[log_study_session] error advancing duo partnership: {ex}")

    return {
        "status": "success",
        "logged_minutes": mins,
        "logged_questions": qs,
    }


# ==========================================
# 4. COURSE NOTEBOOKS / JOURNALS
# ==========================================

def _get_journal_counts(db: Session, journal_id: uuid.UUID, user_id: uuid.UUID) -> tuple[int, int, int]:
    """Computes (materials_count, flashcards_count, questions_count) for a Journal.
    Journals focus strictly on Mock Exam questions and AI Tutor; flashcards count is 0."""
    links = db.query(models.JournalMaterial.material_id).filter(
        models.JournalMaterial.journal_id == journal_id
    ).all()
    mids = [l[0] for l in links]
    mat_count = len(mids)
    fc_count = 0
    q_count = db.query(models.Question).filter(models.Question.journal_id == journal_id).count()
    if q_count == 0 and mids:
        q_count = db.query(models.Question).filter(
            models.Question.material_id.in_(mids),
            models.Question.user_id == user_id
        ).count()
    return mat_count, fc_count, q_count


@router.get("/journals", response_model=List[schemas.JournalResponse])
def get_user_journals(
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user)
):
    """Lists all Course Journals for the current authenticated learner."""
    journals = db.query(models.Journal).filter(
        models.Journal.user_id == user.id
    ).order_by(models.Journal.created_at.desc()).all()

    results = []
    for j in journals:
        mat_count, fc_count, q_count = _get_journal_counts(db, j.id, user.id)
        results.append(schemas.JournalResponse(
            id=j.id,
            title=j.title,
            description=j.description,
            color=j.color or "teal",
            icon=j.icon or "book",
            is_generating=j.is_generating,
            materials_count=mat_count,
            flashcards_count=fc_count,
            questions_count=q_count,
            created_at=j.created_at,
            updated_at=j.updated_at
        ))
    return results


@router.post("/journals", response_model=schemas.JournalResponse)
def create_journal(
    payload: schemas.JournalCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user)
):
    """
    Creates a new Course Journal container and groups optional existing vault materials.
    Capped strictly at 5 sources.
    """
    initial_mids = payload.material_ids or []
    if len(initial_mids) > 5:
        raise HTTPException(status_code=400, detail="A Journal is capped at a maximum of 5 sources.")

    journal = models.Journal(
        user_id=user.id,
        title=payload.title.strip(),
        description=payload.description.strip() if payload.description else None,
        color=payload.color or "teal",
        icon=payload.icon or "book",
    )
    db.add(journal)
    db.flush()

    added_count = 0
    if initial_mids:
        # Verify ownership of materials
        mats = db.query(models.StudyMaterial).filter(
            models.StudyMaterial.id.in_(initial_mids),
            models.StudyMaterial.user_id == user.id
        ).all()
        for idx, m in enumerate(mats):
            db.add(models.JournalMaterial(
                journal_id=journal.id,
                material_id=m.id,
                order_index=idx
            ))
            added_count += 1

    db.commit()
    db.refresh(journal)

    mat_count, fc_count, q_count = _get_journal_counts(db, journal.id, user.id)

    return schemas.JournalResponse(
        id=journal.id,
        title=journal.title,
        description=journal.description,
        color=journal.color or "teal",
        icon=journal.icon or "book",
        is_generating=journal.is_generating,
        materials_count=mat_count,
        flashcards_count=fc_count,
        questions_count=q_count,
        created_at=journal.created_at,
        updated_at=journal.updated_at
    )


@router.post("/journals/create-multi", response_model=schemas.JournalResponse)
async def create_multi_journal(
    background_tasks: BackgroundTasks,
    title: str = Form(...),
    description: Optional[str] = Form(None),
    color: Optional[str] = Form("teal"),
    icon: Optional[str] = Form("book"),
    pasted_texts: Optional[List[str]] = Form(None),
    pasted_titles: Optional[List[str]] = Form(None),
    files: Optional[List[UploadFile]] = File(None),
    auto_generate: bool = Form(True),
    generate_types: Optional[List[str]] = Form(None),
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user)
):
    """
    Batch creates a Course Journal from up to 5 source materials simultaneously.
    - Capped at 5 sources strictly.
    - Upload Cost: Consumes 3 Material Upload Slots (the 3x Rule).
    - Generation Cost: Consumes 3 AI Generation Credits if auto_generate is true.
    - Excludes YouTube URLs per specification (files and text notes only).
    """
    all_files = files or []
    all_texts = [t.strip() for t in (pasted_texts or []) if t and t.strip()]

    total_sources = len(all_files) + len(all_texts)
    if total_sources == 0:
        raise HTTPException(status_code=400, detail="At least 1 source material is required to create a Journal.")
    if total_sources > 5:
        raise HTTPException(status_code=400, detail="Course Journals are capped at a maximum of 5 sources.")

    # 1. Quota & Limits: Consumes 3 upload slots
    from services.subscription_service import check_material_upload_allowed, get_effective_subscription
    sub = check_material_upload_allowed(user.id, db)
    _, effective_plan, _, is_expired = get_effective_subscription(user.id, db)

    # Increment 3 upload slots
    sub.materials_uploaded += 3

    # Check 3 generation credits if auto_generate is enabled
    req_types = generate_types or ["flashcards", "quiz", "exam"]
    if auto_generate:
        plan_gen_limit = effective_plan.generations_limit if effective_plan and effective_plan.generations_limit is not None else None
        if plan_gen_limit is not None and (sub.generations_used + 3) > plan_gen_limit:
            if (sub.bonus_generations or 0) >= 3:
                sub.bonus_generations -= 3
            else:
                if is_expired:
                    raise HTTPException(
                        status_code=402,
                        detail={
                            "error": "subscription_expired",
                            "message": "Your subscription has expired. Generating a Journal requires 3 credits. Please renew or purchase credits."
                        }
                    )
                raise HTTPException(
                    status_code=402,
                    detail={
                        "error": "insufficient_generations",
                        "message": "Generating a Course Journal requires 3 AI generation credits. Please upgrade or top up in the Store."
                    }
                )
        else:
            sub.generations_used += 3

    db.commit()

    # 2. Create Journal
    journal = models.Journal(
        user_id=user.id,
        title=title.strip(),
        description=description.strip() if description else None,
        color=color or "teal",
        icon=icon or "book",
        is_generating=auto_generate,
    )
    db.add(journal)
    db.flush()

    created_materials = []
    combined_corpus_parts = []
    order_idx = 0

    # Process uploaded files (PDFs, Images, TXT)
    image_exts = {"png", "jpg", "jpeg", "webp"}
    for f in all_files:
        fname = f.filename or f"Module {order_idx + 1}"
        ext = fname.split(".")[-1].lower() if "." in fname else "txt"
        file_bytes = await f.read()
        content_text = ""

        if ext in image_exts:
            source_type = "photo"
            try:
                content_text = await ai_service.extract_text_from_study_images([{
                    "bytes": file_bytes,
                    "mime_type": f.content_type or (f"image/{ext}" if ext != "jpg" else "image/jpeg"),
                    "name": fname
                }])
            except Exception as ocr_err:
                content_text = f"[Image Study Note: {fname}]"
        elif ext == "pdf":
            source_type = "pdf"
            try:
                doc = fitz.open(stream=file_bytes, filetype="pdf")
                for page_pdf in doc:
                    content_text += page_pdf.get_text("text") + "\n"
                doc.close()
            except Exception:
                content_text = f"[PDF Document: {fname}]"
        else:
            source_type = "text"
            try:
                content_text = file_bytes.decode("utf-8", errors="ignore")
            except Exception:
                content_text = f"[Text Document: {fname}]"

        clean_title = re.sub(r"\.[a-zA-Z0-9]+$", "", fname)
        mat = models.StudyMaterial(
            user_id=user.id,
            title=clean_title,
            source_type=source_type,
            content=content_text,
            is_generating=auto_generate,
        )
        db.add(mat)
        db.flush()

        db.add(models.JournalMaterial(
            journal_id=journal.id,
            material_id=mat.id,
            order_index=order_idx
        ))
        created_materials.append(mat)
        combined_corpus_parts.append(f"## Module: {clean_title}\n{content_text}")
        order_idx += 1

    # Process pasted texts
    all_titles = pasted_titles or []
    for t_idx, txt in enumerate(all_texts):
        custom_t = all_titles[t_idx].strip() if t_idx < len(all_titles) and all_titles[t_idx] and all_titles[t_idx].strip() else ""
        clean_title = custom_t or f"{title.strip()} - Note {order_idx + 1}"
        mat = models.StudyMaterial(
            user_id=user.id,
            title=clean_title,
            source_type="text",
            content=txt,
            is_generating=auto_generate,
        )
        db.add(mat)
        db.flush()

        db.add(models.JournalMaterial(
            journal_id=journal.id,
            material_id=mat.id,
            order_index=order_idx
        ))
        created_materials.append(mat)
        combined_corpus_parts.append(f"## Module: {clean_title}\n{txt}")
        order_idx += 1

    db.commit()
    db.refresh(journal)

    # 3. Queue Background AI Generation
    if auto_generate and created_materials:
        combined_text = "\n\n".join(combined_corpus_parts)
        background_tasks.add_task(
            process_and_generate_journal_content,
            user_id=str(user.id),
            journal_id=str(journal.id),
            material_ids=[str(m.id) for m in created_materials],
            content_text=combined_text,
            types_requested=req_types
        )

    return schemas.JournalResponse(
        id=journal.id,
        title=journal.title,
        description=journal.description,
        color=journal.color or "teal",
        icon=journal.icon or "book",
        is_generating=journal.is_generating,
        materials_count=len(created_materials),
        flashcards_count=0,
        questions_count=0,
        created_at=journal.created_at,
        updated_at=journal.updated_at
    )


@router.get("/journals/{journal_id}", response_model=schemas.JournalDetailResponse)
def get_journal_detail(
    journal_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user)
):
    """Fetches details of a specific Course Journal including all constituent materials and study guide."""
    journal = db.query(models.Journal).filter(
        models.Journal.id == journal_id,
        models.Journal.user_id == user.id
    ).first()
    if not journal:
        raise HTTPException(status_code=404, detail="Course Journal not found.")

    links = db.query(models.JournalMaterial).filter(
        models.JournalMaterial.journal_id == journal.id
    ).order_by(models.JournalMaterial.order_index).all()

    materials_list = []
    for link in links:
        m = db.query(models.StudyMaterial).filter(models.StudyMaterial.id == link.material_id).first()
        if m:
            materials_list.append(schemas.JournalMaterialItem(
                id=m.id,
                title=m.title,
                source_type=m.source_type,
                created_at=m.created_at,
                order_index=link.order_index,
                content=m.content
            ))

    _, fc_count, q_count = _get_journal_counts(db, journal.id, user.id)

    return schemas.JournalDetailResponse(
        id=journal.id,
        title=journal.title,
        description=journal.description,
        color=journal.color or "teal",
        icon=journal.icon or "book",
        is_generating=journal.is_generating,
        study_guide=journal.study_guide,
        materials_count=len(materials_list),
        flashcards_count=fc_count,
        questions_count=q_count,
        created_at=journal.created_at,
        updated_at=journal.updated_at,
        materials=materials_list
    )


@router.get("/journals/{journal_id}/status")
def get_journal_status(
    journal_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user)
):
    """The endpoint the frontend polls for granular journal generation progress and counts."""
    journal = db.query(models.Journal).filter(
        models.Journal.id == journal_id,
        models.Journal.user_id == user.id
    ).first()
    if not journal:
        raise HTTPException(status_code=404, detail="Course Journal not found.")

    prog = get_journal_generation_progress(str(journal_id))
    _, fc_count, q_count = _get_journal_counts(db, journal.id, user.id)

    # Self-healing timeout detection:
    # If journal DB says is_generating=True, but progress says is_generating=False or status=error/completed,
    # OR if journal was updated > 180 seconds ago and prog is empty/inactive, self-heal and release lock!
    now_utc = datetime.now(timezone.utc)
    is_stale_lock = False
    if journal.is_generating and journal.updated_at:
        try:
            up_time = journal.updated_at
            if up_time.tzinfo is None:
                up_time = up_time.replace(tzinfo=timezone.utc)
            age_seconds = (now_utc - up_time).total_seconds()
            if age_seconds > 180 and not prog.get("is_generating"):
                is_stale_lock = True
        except Exception:
            pass

    if is_stale_lock or (journal.is_generating and prog.get("status") in ("completed", "error")):
        journal.is_generating = False
        db.commit()

    is_generating = bool(journal.is_generating or prog.get("is_generating", False))
    raw_status = prog.get("status")
    if not is_generating:
        status = "error" if raw_status == "error" else "completed"
    else:
        status = "processing"

    return {
        "status": status,
        "is_generating": is_generating,
        "progress_percent": prog.get("progress_percent", 100 if not is_generating else 25),
        "stage": prog.get("stage", "Ready to study" if not is_generating else "Synthesizing course notebook..."),
        "flashcards_count": 0,
        "questions_count": max(q_count, prog.get("questions_count", 0)),
        "has_study_guide": bool(journal.study_guide),
    }


@router.patch("/journals/{journal_id}", response_model=schemas.JournalResponse)
def update_journal(
    journal_id: uuid.UUID,
    payload: schemas.JournalUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user)
):
    """Updates Course Journal metadata (title, description, color, icon)."""
    journal = db.query(models.Journal).filter(
        models.Journal.id == journal_id,
        models.Journal.user_id == user.id
    ).first()
    if not journal:
        raise HTTPException(status_code=404, detail="Course Journal not found.")

    if payload.title is not None and payload.title.strip():
        journal.title = payload.title.strip()
    if payload.description is not None:
        journal.description = payload.description.strip() or None
    if payload.color is not None:
        journal.color = payload.color
    if payload.icon is not None:
        journal.icon = payload.icon

    db.commit()
    db.refresh(journal)

    mat_count, fc_count, q_count = _get_journal_counts(db, journal.id, user.id)

    return schemas.JournalResponse(
        id=journal.id,
        title=journal.title,
        description=journal.description,
        color=journal.color or "teal",
        icon=journal.icon or "book",
        is_generating=journal.is_generating,
        materials_count=mat_count,
        flashcards_count=fc_count,
        questions_count=q_count,
        created_at=journal.created_at,
        updated_at=journal.updated_at
    )


@router.delete("/journals/{journal_id}")
def delete_journal(
    journal_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user)
):
    """
    Deletes a Course Journal.
    Detaches associations and removes generated journal study cards,
    while safely preserving all constituent study materials in the user's vault.
    """
    journal = db.query(models.Journal).filter(
        models.Journal.id == journal_id,
        models.Journal.user_id == user.id
    ).first()
    if not journal:
        raise HTTPException(status_code=404, detail="Course Journal not found.")

    # Remove associations
    db.query(models.JournalMaterial).filter(models.JournalMaterial.journal_id == journal_id).delete(synchronize_session=False)

    # Delete journal-specific cards/questions
    db.query(models.Flashcard).filter(models.Flashcard.journal_id == journal_id).delete(synchronize_session=False)
    db.query(models.Question).filter(models.Question.journal_id == journal_id).delete(synchronize_session=False)

    # Delete journal
    db.delete(journal)
    db.commit()

    return {"success": True, "message": "Course Journal deleted. Constituent materials remain safely in your vault."}


@router.post("/journals/{journal_id}/materials", response_model=schemas.JournalDetailResponse)
def add_materials_to_journal(
    journal_id: uuid.UUID,
    payload: schemas.AddMaterialsToJournalPayload,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user)
):
    """
    Attaches existing vault study materials into the Course Journal.
    Strictly enforces maximum of 5 sources.
    """
    journal = db.query(models.Journal).filter(
        models.Journal.id == journal_id,
        models.Journal.user_id == user.id
    ).first()
    if not journal:
        raise HTTPException(status_code=404, detail="Course Journal not found.")

    current_count = db.query(models.JournalMaterial).filter(models.JournalMaterial.journal_id == journal.id).count()
    new_mids = payload.material_ids or []
    if current_count + len(new_mids) > 5:
        raise HTTPException(status_code=400, detail=f"A Journal cannot exceed 5 sources. Currently has {current_count}.")

    existing_links = {
        row.material_id for row in db.query(models.JournalMaterial.material_id).filter(
            models.JournalMaterial.journal_id == journal.id
        ).all()
    }

    order_idx = current_count
    for mid in new_mids:
        if mid not in existing_links:
            # Verify material belongs to user
            mat = db.query(models.StudyMaterial).filter(
                models.StudyMaterial.id == mid,
                models.StudyMaterial.user_id == user.id
            ).first()
            if mat:
                db.add(models.JournalMaterial(
                    journal_id=journal.id,
                    material_id=mat.id,
                    order_index=order_idx
                ))
                order_idx += 1

    db.commit()
    return get_journal_detail(journal_id, db, user)


@router.delete("/journals/{journal_id}/materials/{material_id}")
def remove_material_from_journal(
    journal_id: uuid.UUID,
    material_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user)
):
    """
    Removes a study material association from the Journal without deleting it from the vault.
    """
    journal = db.query(models.Journal).filter(
        models.Journal.id == journal_id,
        models.Journal.user_id == user.id
    ).first()
    if not journal:
        raise HTTPException(status_code=404, detail="Course Journal not found.")

    link = db.query(models.JournalMaterial).filter(
        models.JournalMaterial.journal_id == journal_id,
        models.JournalMaterial.material_id == material_id
    ).first()
    if link:
        db.delete(link)
        db.commit()

    return {"success": True, "message": "Material unlinked from Course Journal."}


@router.post("/journals/{journal_id}/generate")
async def generate_journal_content(
    journal_id: uuid.UUID,
    payload: schemas.JournalGeneratePayload,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user)
):
    """
    Triggers unified AI generation across all attached materials in the Journal.
    Consumes 3 AI Generation Credits (The 3x Rule).
    """
    journal = db.query(models.Journal).filter(
        models.Journal.id == journal_id,
        models.Journal.user_id == user.id
    ).first()
    if not journal:
        raise HTTPException(status_code=404, detail="Course Journal not found.")

    links = db.query(models.JournalMaterial).filter(
        models.JournalMaterial.journal_id == journal.id
    ).order_by(models.JournalMaterial.order_index).all()
    if not links:
        raise HTTPException(status_code=400, detail="Please attach at least 1 material before generating.")

    mats = db.query(models.StudyMaterial).filter(
        models.StudyMaterial.id.in_([l.material_id for l in links])
    ).all()

    # Credit check: Consumes 3 generation credits
    from services.subscription_service import get_effective_subscription
    sub, effective_plan, _, is_expired = get_effective_subscription(user.id, db)

    credit_source = "free_quota"
    plan_gen_limit = effective_plan.generations_limit if effective_plan and effective_plan.generations_limit is not None else None
    if plan_gen_limit is not None and (sub.generations_used + 3) > plan_gen_limit:
        if (sub.bonus_generations or 0) >= 3:
            sub.bonus_generations -= 3
            credit_source = "bonus_generations"
        else:
            if is_expired:
                raise HTTPException(
                    status_code=402,
                    detail={
                        "error": "subscription_expired",
                        "message": "Your subscription has expired. Journal generation requires 3 credits. Please renew your plan or top up in the Store."
                    }
                )
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "insufficient_generations",
                    "message": "Journal generation requires 3 credits. You do not have enough monthly quota or bonus credits. Please top up in the Store."
                }
            )
    else:
        sub.generations_used += 3

    journal.is_generating = True
    for m in mats:
        m.is_generating = True
    db.commit()

    combined_text = "\n\n".join([f"## Module: {m.title}\n{m.content or ''}" for m in mats])

    background_tasks.add_task(
        process_and_generate_journal_content,
        user_id=str(user.id),
        journal_id=str(journal.id),
        material_ids=[str(m.id) for m in mats],
        content_text=combined_text,
        types_requested=payload.types or ["exam", "study_guide"],
        replace_existing=bool(getattr(payload, "replace_existing", False))
    )

    return {"message": "Journal AI Generation started", "status": "processing", "source": credit_source}


@router.post("/journals/{journal_id}/ai-tutor")
async def ask_journal_tutor(
    journal_id: uuid.UUID,
    payload: AIDoubtPayload,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user)
):
    """
    Grounded Notebook AI Tutor answering queries across all materials in the Course Journal.
    """
    journal = db.query(models.Journal).filter(
        models.Journal.id == journal_id,
        models.Journal.user_id == user.id
    ).first()
    if not journal:
        raise HTTPException(status_code=404, detail="Course Journal not found.")

    links = db.query(models.JournalMaterial).filter(
        models.JournalMaterial.journal_id == journal.id
    ).order_by(models.JournalMaterial.order_index).all()

    mats = db.query(models.StudyMaterial).filter(
        models.StudyMaterial.id.in_([l.material_id for l in links])
    ).all() if links else []

    # Ensure modules maintain their defined user order
    link_order = {l.material_id: l.order_index for l in links}
    mats.sort(key=lambda m: link_order.get(m.id, 0))

    attached_summary = [{"title": m.title, "source_type": m.source_type} for m in mats]
    materials_list = [
        {
            "id": str(m.id),
            "title": m.title,
            "source_type": m.source_type or "note",
            "content": m.content or "",
        }
        for m in mats
    ]
    combined_content = "\n\n".join([f"## Module: {m.title}\n{m.content or ''}" for m in mats])

    try:
        reply = await ai_service.ask_journal_tutor_response(
            query=payload.query,
            journal_title=journal.title,
            attached_materials=attached_summary,
            combined_content=combined_content,
            materials_list=materials_list,
            conversation_history=payload.conversation_history or []
        )
        return {"reply": reply}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Journal Tutor error: {str(e)}")


@router.get("/journals/{journal_id}/due-cards", response_model=List[schemas.FlashcardResponse])
def get_journal_due_cards(
    journal_id: uuid.UUID,
    all_cards: bool = True,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user)
):
    """Fetches all flashcards generated for this Course Journal."""
    cards = db.query(models.Flashcard).filter(
        models.Flashcard.journal_id == journal_id,
        models.Flashcard.user_id == user.id
    ).all()

    # Fallback to constituent material cards if journal-tagged cards haven't been generated yet
    if not cards:
        links = db.query(models.JournalMaterial.material_id).filter(
            models.JournalMaterial.journal_id == journal_id
        ).all()
        mids = [l[0] for l in links]
        if mids:
            cards = db.query(models.Flashcard).filter(
                models.Flashcard.material_id.in_(mids),
                models.Flashcard.user_id == user.id
            ).all()

    results = []
    for c in cards:
        results.append(schemas.FlashcardResponse(
            id=c.id,
            front=c.front,
            back=c.back,
            material_id=c.material_id,
            srs_state=schemas.SRSState(
                interval=c.interval_days,
                ease_factor=c.ease_factor,
                repetitions=c.repetitions
            )
        ))
    return results


@router.get("/journals/{journal_id}/questions", response_model=List[schemas.QuestionResponse])
def get_journal_questions(
    journal_id: uuid.UUID,
    limit: int = 15,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user)
):
    """Fetches questions for quiz/exam across this Course Journal."""
    journal = db.query(models.Journal).filter(
        models.Journal.id == journal_id,
        models.Journal.user_id == user.id
    ).first()
    j_title = journal.title if journal else "Course Journal"

    questions = db.query(models.Question).filter(
        models.Question.journal_id == journal_id,
        models.Question.user_id == user.id
    ).order_by(func.random()).limit(limit).all()

    # Fallback to constituent materials if needed
    if not questions:
        links = db.query(models.JournalMaterial.material_id).filter(
            models.JournalMaterial.journal_id == journal_id
        ).all()
        mids = [l[0] for l in links]
        if mids:
            questions = db.query(models.Question).filter(
                models.Question.material_id.in_(mids),
                models.Question.user_id == user.id
            ).order_by(func.random()).limit(limit).all()

    results = []
    for q in questions:
        results.append(schemas.QuestionResponse(
            id=q.id,
            material_id=q.material_id,
            subject=j_title,
            question=_clean_question_text_prompt(q.question_text),
            options=q.options,
            answer=q.answer_index,
            explanation=q.explanation,
            image_url=q.image_url,
            topic=q.topic,
            exam_type=q.exam_type,
            exam_year=q.exam_year
        ))
    return results


