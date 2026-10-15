"""
Lesson Text-to-Speech (TTS) Router for Nu-Age LMS.
Communicates with the standalone Nu-Voice microservice to stream audio in real-time,
handles client disconnect cancellation, and manages Bunny.net CDN caching in PostgreSQL.
"""

import asyncio
import hashlib
import logging
from typing import Optional, List, Dict, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, BackgroundTasks, status
from fastapi.responses import StreamingResponse, RedirectResponse, JSONResponse
from sqlalchemy.orm import Session

import models
from database import get_db, Settings
from services import auth
from services.voice_client import stream_lesson_speech, get_voice_service_url, check_voice_service_health

logger = logging.getLogger("nu_age.tts_router")
router = APIRouter(prefix="/courses/lessons", tags=["Lesson TTS Audio"])

# Lesson types that cannot be meaningfully narrated
UNSUPPORTED_LESSON_TYPES = {"video", "document", "code_lab"}
settings = Settings()
_active_generation_tasks: set = set()


def extract_lesson_narratable_text(lesson: models.Lesson) -> str:
    """
    Intelligently extracts all readable lesson text across different lesson types:
    articles, notes, scenarios, steppers, and flashcards.
    """
    if lesson.type in UNSUPPORTED_LESSON_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Audio narration is not supported for lesson type '{lesson.type}'.",
        )

    content = lesson.content or {}
    narrative_parts: List[str] = []

    # 1. Lesson Title
    if lesson.title:
        narrative_parts.append(f"{lesson.title}.")

    # 2. Main text or article
    if content.get("text"):
        narrative_parts.append(str(content["text"]))

    # 3. Notes block
    if content.get("notes"):
        narrative_parts.append(str(content["notes"]))

    # 4. Stepper / Walkthrough
    if lesson.type == "stepper" or content.get("steps"):
        if content.get("title"):
            narrative_parts.append(str(content["title"]))
        if content.get("intro"):
            narrative_parts.append(str(content["intro"]))
        for step in content.get("steps", []):
            if step.get("headline"):
                narrative_parts.append(f"Step: {step['headline']}.")
            if step.get("content"):
                narrative_parts.append(str(step["content"]))
            if step.get("takeaway"):
                narrative_parts.append(f"Key takeaway: {step['takeaway']}.")

    # 5. Decision Scenario
    if lesson.type == "scenario" or content.get("choices"):
        if content.get("scenario"):
            narrative_parts.append(str(content["scenario"]))
        choices = content.get("choices", [])
        if choices:
            narrative_parts.append("Consider your options:")
            for c in choices:
                if c.get("text"):
                    narrative_parts.append(f"Option: {c['text']}.")

    # 6. Flashcard Deck
    if content.get("cards"):
        narrative_parts.append("Flashcard review.")
        for idx, card in enumerate(content.get("cards", [])):
            q = card.get("question") or card.get("front") or ""
            a = card.get("answer") or card.get("back") or ""
            if q or a:
                narrative_parts.append(f"Card {idx+1}: {q}. Answer: {a}.")

    full_text = "\n\n".join(narrative_parts).strip()
    if not full_text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This lesson has no readable text content to narrate.",
        )

    return full_text


def compute_content_hash(text: str) -> str:
    """Computes a 16-character content hash to invalidate cache if lesson text changes."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def get_stream_user(
    token_query: Optional[str] = Query(None, alias="token"),
    bearer_token: Optional[str] = Depends(auth.oauth2_scheme_optional),
    db: Session = Depends(get_db),
) -> models.User:
    """
    Flexible authenticator for streaming endpoints.
    Allows authentication via standard 'Authorization: Bearer <token>' header
    OR '?token=<token>' query parameter (essential for audio/video media players).
    """
    raw_token = bearer_token or token_query
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication token required via header or ?token= query parameter",
            headers={"WWW-Authenticate": "Bearer"},
        )
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    email = auth.verify_access_token(raw_token, credentials_exception)
    user = db.query(models.User).filter(models.User.email == email).first()
    if not user:
        raise credentials_exception
    return user


def _get_lesson_or_404(db: Session, lesson_id: str) -> models.Lesson:
    """Safely sanitizes and resolves a Lesson by UUID or string, returning 404 if missing."""
    clean_id = str(lesson_id or "").strip().strip('"').strip("'")
    if not clean_id:
        raise HTTPException(status_code=404, detail="Lesson ID is required.")
    try:
        lesson_uuid = UUID(clean_id)
        lesson = db.query(models.Lesson).filter(models.Lesson.id == lesson_uuid).first()
    except (ValueError, TypeError):
        lesson = db.query(models.Lesson).filter(models.Lesson.id == clean_id).first()
    if not lesson:
        logger.warning(f"[TTS] Lesson '{clean_id}' not found in database.")
        raise HTTPException(status_code=404, detail="Lesson not found.")
    return lesson


# ==============================================================================
# 1. STREAM LESSON AUDIO ENDPOINT
# ==============================================================================

@router.get("/{lesson_id}/narrate/stream")
async def stream_lesson_audio(
    lesson_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    lang: str = Query("en", description="Target speech language: 'en' (English), 'ha' (Hausa), 'fr' (French)"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_stream_user),
):
    """
    Streams a lesson's narrative text in English, French, or Hausa.
    - If cached: Returns 307 Redirect directly to Bunny.net CDN (<50ms, 0 CPU).
    - If new: Streams sentence-by-sentence via the standalone Nu-Voice microservice.
    - If the student navigates away or pauses, detects socket drop and cancels immediately.
    - Once completed, records the Bunny CDN URL in PostgreSQL.
    """
    lang = (lang or "en").lower().strip()
    if lang not in ("en", "ha", "fr"):
        raise HTTPException(status_code=400, detail="Supported languages are 'en', 'ha', and 'fr'.")

    # 1. Fetch Lesson
    lesson = _get_lesson_or_404(db, lesson_id)

    raw_text = extract_lesson_narratable_text(lesson)
    content_hash = compute_content_hash(raw_text)

    # 2. Check Database Cache
    cache_record = db.query(models.LessonAudioCache).filter_by(
        lesson_id=lesson.id,
        language=lang,
        content_hash=content_hash,
    ).first()

    if cache_record and cache_record.audio_url:
        logger.info(f"Cache HIT for lesson {lesson_id} [{lang}]: {cache_record.audio_url}")
        if request.headers.get("accept") == "application/json":
            return JSONResponse({
                "cached": True,
                "audio_url": cache_record.audio_url,
                "lesson_id": str(lesson.id),
                "language": lang,
            })
        return RedirectResponse(
            url=cache_record.audio_url,
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        )

    # 3. Stream from Standalone Nu-Voice Microservice with Pre-Flight First Chunk
    try:
        stream_gen = stream_lesson_speech(
            lesson_id=str(lesson.id),
            title=lesson.title or "",
            content_text=raw_text,
            target_lang=lang,
            source_lang="en",
            audio_format="mp3",
        )
        # Pre-flight the first chunk before returning 200 StreamingResponse.
        # If Nu-Voice is offline or in error, this cleanly triggers an HTTP 503 instead of a broken stream.
        first_chunk = await stream_gen.__anext__()
    except StopAsyncIteration:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Voice service produced no audio.",
        )
    except Exception as e:
        logger.error(f"Error connecting to Nu-Voice for lesson {lesson_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Speech synthesis service is currently starting up or temporarily unavailable. Please try again shortly.",
        )

    async def audio_stream_proxy():
        chunk_count = 1
        all_completed = True
        yield first_chunk
        try:
            async for chunk in stream_gen:
                if await request.is_disconnected():
                    logger.info(f"Student disconnected during stream of lesson {lesson_id}. Aborting proxy.")
                    all_completed = False
                    break
                chunk_count += 1
                yield chunk

        except Exception as e:
            logger.warning(f"Audio streaming interrupted for lesson {lesson_id}: {e}")
            all_completed = False
            return

        # 4. If all chunks streamed cleanly, record the CDN cache in Postgres
        if all_completed and chunk_count > 0:
            pull_zone = (getattr(settings, "PULL_ZONE_URL", "") or "").rstrip("/")
            if pull_zone:
                cdn_url = f"{pull_zone}/lessons/audio/{lesson.id}_{lang}_{content_hash}.mp3"
                background_tasks.add_task(
                    _record_audio_cache,
                    lesson_id=str(lesson.id),
                    lang=lang,
                    content_hash=content_hash,
                    cdn_url=cdn_url,
                )

    return StreamingResponse(
        audio_stream_proxy(),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Transfer-Encoding": "chunked",
            "X-Lesson-Id": str(lesson.id),
            "X-Language": lang,
        },
    )


async def _record_audio_cache(
    lesson_id: str,
    lang: str,
    content_hash: str,
    cdn_url: str,
):
    """Background task to store the cache entry in PostgreSQL with atomic upsert."""
    from database import SessionLocal
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    clean_lid = str(lesson_id).strip().strip('"').strip("'")
    db = SessionLocal()
    try:
        stmt = pg_insert(models.LessonAudioCache).values(
            lesson_id=UUID(clean_lid),
            language=lang,
            content_hash=content_hash,
            audio_url=cdn_url,
            format="mp3",
        ).on_conflict_do_update(
            index_elements=["lesson_id", "language", "content_hash"],
            set_={
                "audio_url": cdn_url,
                "format": "mp3",
            },
        )
        db.execute(stmt)
        db.commit()
        logger.info(f"Successfully recorded LessonAudioCache for lesson {lesson_id} [{lang}] -> {cdn_url}")
    except Exception as e:
        db.rollback()
        # Resilient fallback to query-and-update
        try:
            existing = db.query(models.LessonAudioCache).filter_by(
                lesson_id=UUID(clean_lid),
                language=lang,
                content_hash=content_hash,
            ).first()
            if existing:
                existing.audio_url = cdn_url
                db.commit()
                logger.info(f"Updated LessonAudioCache for lesson {lesson_id} [{lang}] -> {cdn_url}")
            else:
                logger.warning(f"Notice while recording lesson audio cache in DB: {e}")
        except Exception as ex:
            db.rollback()
            logger.warning(f"Fallback cache record notice: {ex}")
    finally:
        db.close()


async def _background_synthesize_and_cache(
    lesson_id: str,
    title: str,
    content_text: str,
    lang: str,
    content_hash: str,
):
    """
    Robust background worker that consumes speech synthesis from Nu-Voice to completion,
    independent of any client HTTP connection, ensuring the lesson audio is saved to Bunny CDN
    and indexed in PostgreSQL (models.LessonAudioCache) so any learner gets instant playback.
    """
    task_key = (str(lesson_id), str(lang))
    try:
        logger.info(f"Background audio synthesis initiated for lesson {lesson_id} [{lang}]")
        pull_zone = (getattr(settings, "PULL_ZONE_URL", "") or "").rstrip("/")
        cdn_url = f"{pull_zone}/lessons/audio/{lesson_id}_{lang}_{content_hash}.mp3" if pull_zone else ""

        chunk_count = 0
        async for chunk in stream_lesson_speech(
            lesson_id=str(lesson_id),
            title=title or "",
            content_text=content_text,
            target_lang=lang,
            source_lang="en",
            audio_format="mp3",
        ):
            chunk_count += 1

        if chunk_count > 0 and cdn_url:
            await _record_audio_cache(
                lesson_id=str(lesson_id),
                lang=lang,
                content_hash=content_hash,
                cdn_url=cdn_url,
            )
            logger.info(f"Background synthesis completed & cached in DB for lesson {lesson_id} [{lang}]")
        else:
            logger.warning(f"Background synthesis produced no audio chunks for lesson {lesson_id} [{lang}]")
    except Exception as e:
        logger.error(f"Background synthesis failed for lesson {lesson_id} [{lang}]: {e}")
    finally:
        _active_generation_tasks.discard(task_key)


# ==============================================================================
# 2. AUDIO PREPARATION & CACHE WARMING ENDPOINT
# ==============================================================================

@router.post("/{lesson_id}/narrate/prepare")
@router.get("/{lesson_id}/narrate/prepare")
async def prepare_lesson_audio(
    lesson_id: str,
    background_tasks: BackgroundTasks,
    lang: str = Query("en", description="Target speech language: 'en', 'ha', 'fr'"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_stream_user),
):
    """
    Asynchronously triggers synthesis and database caching for a lesson audio.
    - If already cached in DB: returns immediately with status='ready' and CDN URL.
    - If in progress: returns status='generating'.
    - If new: schedules background synthesis to Bunny CDN & PostgreSQL and returns status='generating'.
    """
    lang = (lang or "en").lower().strip()
    if lang not in ("en", "ha", "fr"):
        raise HTTPException(status_code=400, detail="Supported languages are 'en', 'ha', and 'fr'.")

    lesson = _get_lesson_or_404(db, lesson_id)

    raw_text = extract_lesson_narratable_text(lesson)
    content_hash = compute_content_hash(raw_text)

    # 1. Check if already in DB cache
    cache_record = db.query(models.LessonAudioCache).filter_by(
        lesson_id=lesson.id,
        language=lang,
        content_hash=content_hash,
    ).first()

    if cache_record and cache_record.audio_url:
        return {
            "status": "ready",
            "cached": True,
            "lesson_id": str(lesson.id),
            "language": lang,
            "audio_url": cache_record.audio_url,
        }

    # 2. Check if already actively generating
    task_key = (str(lesson.id), str(lang))
    if task_key in _active_generation_tasks:
        return {
            "status": "generating",
            "cached": False,
            "lesson_id": str(lesson.id),
            "language": lang,
            "message": "Audio synthesis is currently in progress.",
        }

    # 3. Schedule robust background generation
    _active_generation_tasks.add(task_key)
    background_tasks.add_task(
        _background_synthesize_and_cache,
        lesson_id=str(lesson.id),
        title=lesson.title or "",
        content_text=raw_text,
        lang=lang,
        content_hash=content_hash,
    )

    return {
        "status": "generating",
        "cached": False,
        "lesson_id": str(lesson.id),
        "language": lang,
        "message": "Audio synthesis queued in background.",
    }


# ==============================================================================
# 3. AUDIO METADATA & SUPPORT INFO ENDPOINT
# ==============================================================================

@router.get("/{lesson_id}/narrate/info")
async def get_lesson_audio_info(
    lesson_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_stream_user),
):
    """Returns available audio translations, cached status, and active generation state for a lesson."""
    lesson = _get_lesson_or_404(db, lesson_id)

    is_supported = lesson.type not in UNSUPPORTED_LESSON_TYPES

    caches = db.query(models.LessonAudioCache).filter(
        models.LessonAudioCache.lesson_id == lesson.id
    ).all()

    cached_languages = {c.language: c.audio_url for c in caches}
    generating_languages = [l for (lid, l) in _active_generation_tasks if lid == str(lesson.id)]
    health = await check_voice_service_health()

    return {
        "lesson_id": str(lesson.id),
        "lesson_type": lesson.type,
        "is_audio_supported": is_supported,
        "available_languages": ["en", "ha", "fr"],
        "cached_languages": cached_languages,
        "generating_languages": generating_languages,
        "voice_service_status": health.get("status", "unknown"),
    }
