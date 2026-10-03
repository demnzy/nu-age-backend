import uuid
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, status, Query, BackgroundTasks
from pydantic import BaseModel, Field
from sqlalchemy import or_, and_, func, desc
from sqlalchemy.orm import Session

from database import get_db
import models
from services import auth
from services.notifications import send_push_notification
from routers.platform_admin import get_current_super_admin
from monetization_models import CreditBalance, CreditLedgerEntry

router = APIRouter(prefix="/study/marketplace", tags=["Study Marketplace & Hub"])


# ─────────────────────────────────────────────────────────────────────────────
# PYDANTIC SCHEMAS
# ─────────────────────────────────────────────────────────────────────────────

class PackSubmitPayload(BaseModel):
    material_id: uuid.UUID
    title: str = Field(..., min_length=3, max_length=255)
    description: Optional[str] = None
    category: str = Field(default="General", max_length=100)
    theme_gradient: str = Field(default="purple_indigo", max_length=100)
    price_coins: int = Field(default=0, ge=0)


class AdminCuratedPackPayload(BaseModel):
    title: str = Field(..., min_length=3, max_length=255)
    description: Optional[str] = None
    category: str = Field(default="General", max_length=100)
    theme_gradient: str = Field(default="emerald_teal", max_length=100)
    cover_image_url: Optional[str] = None
    price_coins: int = Field(default=0, ge=0)
    material_content: Optional[str] = ""
    flashcards: List[Dict[str, Any]] = Field(default_factory=list)
    questions: List[Dict[str, Any]] = Field(default_factory=list)


class AdminReviewPayload(BaseModel):
    action: str = Field(..., pattern="^(approve|reject)$")
    reward_coins: int = Field(default=50, ge=0)
    review_notes: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# HELPER: WALLET LEDGER
# ─────────────────────────────────────────────────────────────────────────────

def _credit_wallet(db: Session, user_id: uuid.UUID, delta: int, reason: str, ref_id: Optional[str] = None) -> int:
    """Safely increments or decrements user CreditBalance and logs a CreditLedgerEntry."""
    cb = db.query(CreditBalance).filter(CreditBalance.user_id == user_id).first()
    if not cb:
        cb = CreditBalance(user_id=user_id, balance=0)
        db.add(cb)
        db.flush()

    new_bal = cb.balance + delta
    if new_bal < 0:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f"Insufficient Nu-Coins balance ({cb.balance} available, {abs(delta)} needed)."
        )

    cb.balance = new_bal
    ledger = CreditLedgerEntry(
        user_id=user_id,
        delta=delta,
        reason=reason,
        reference_id=ref_id,
        balance_after=new_bal
    )
    db.add(ledger)
    db.flush()
    return new_bal


# ─────────────────────────────────────────────────────────────────────────────
# 1. BROWSE & DISCOVER STUDY PACKS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/packs")
def list_marketplace_packs(
    category: Optional[str] = None,
    search: Optional[str] = None,
    official_only: bool = False,
    free_only: bool = False,
    sort_by: str = Query("popular", pattern="^(popular|newest|price_low|price_high|likes)$"),
    page: int = Query(1, ge=1),
    limit: int = Query(24, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: Optional[models.User] = Depends(auth.get_current_user_optional)
):
    """
    Returns public approved study packs with creator information,
    download/like counts, and flags indicating if the caller already owns or liked it.
    """
    query = db.query(models.StudyPack).filter(models.StudyPack.status == "approved")

    if official_only:
        query = query.filter(models.StudyPack.is_official.is_(True))
    if free_only:
        query = query.filter(models.StudyPack.price_coins == 0)
    if category and category.lower() not in ("all", ""):
        query = query.filter(func.lower(models.StudyPack.category) == category.lower())
    if search and search.strip():
        term = f"%{search.strip().lower()}%"
        query = query.filter(
            or_(
                func.lower(models.StudyPack.title).like(term),
                func.lower(models.StudyPack.description).like(term),
                func.lower(models.StudyPack.category).like(term),
            )
        )

    # Sorting
    if sort_by == "newest":
        query = query.order_by(models.StudyPack.approved_at.desc().nullslast(), models.StudyPack.created_at.desc())
    elif sort_by == "price_low":
        query = query.order_by(models.StudyPack.price_coins.asc(), models.StudyPack.downloads_count.desc())
    elif sort_by == "price_high":
        query = query.order_by(models.StudyPack.price_coins.desc(), models.StudyPack.downloads_count.desc())
    elif sort_by == "likes":
        query = query.order_by(models.StudyPack.likes_count.desc(), models.StudyPack.downloads_count.desc())
    else:  # popular
        query = query.order_by(models.StudyPack.downloads_count.desc(), models.StudyPack.likes_count.desc())

    total = query.count()
    packs = query.offset((page - 1) * limit).limit(limit).all()

    # User download & like mappings
    user_downloaded_ids = set()
    user_liked_ids = set()
    user_balance = 0

    if current_user:
        pack_ids = [p.id for p in packs]
        if pack_ids:
            dl_records = db.query(models.StudyPackDownload.pack_id).filter(
                models.StudyPackDownload.user_id == current_user.id,
                models.StudyPackDownload.pack_id.in_(pack_ids)
            ).all()
            user_downloaded_ids = {r[0] for r in dl_records}

            like_records = db.query(models.StudyPackLike.pack_id).filter(
                models.StudyPackLike.user_id == current_user.id,
                models.StudyPackLike.pack_id.in_(pack_ids)
            ).all()
            user_liked_ids = {r[0] for r in like_records}

        cb = db.query(CreditBalance).filter(CreditBalance.user_id == current_user.id).first()
        if cb:
            user_balance = cb.balance

    items = []
    for p in packs:
        pd = p.pack_data or {}
        fc_count = len(pd.get("flashcards", []))
        q_count = len(pd.get("questions", []))

        creator_name = "Nu-Age Official"
        creator_username = "admin"
        creator_avatar = None

        if p.creator:
            creator_name = f"{p.creator.first_name} {p.creator.last_name}".strip()
            creator_username = p.creator.username
            creator_avatar = p.creator.profile_picture_url

        items.append({
            "id": str(p.id),
            "title": p.title,
            "description": p.description or "",
            "category": p.category,
            "theme_gradient": p.theme_gradient or "purple_indigo",
            "cover_image_url": p.cover_image_url,
            "price_coins": p.price_coins,
            "is_official": p.is_official,
            "downloads_count": p.downloads_count,
            "likes_count": p.likes_count,
            "flashcards_count": fc_count,
            "questions_count": q_count,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "approved_at": p.approved_at.isoformat() if p.approved_at else None,
            "creator": {
                "id": str(p.creator_id),
                "name": creator_name,
                "username": creator_username,
                "profile_picture_url": creator_avatar,
            },
            "is_owned": (p.id in user_downloaded_ids) or (current_user and p.creator_id == current_user.id),
            "is_liked": p.id in user_liked_ids,
        })

    # Available categories summary
    categories_agg = db.query(
        models.StudyPack.category,
        func.count(models.StudyPack.id)
    ).filter(models.StudyPack.status == "approved").group_by(models.StudyPack.category).all()

    category_counts = {c[0]: c[1] for c in categories_agg}

    return {
        "items": items,
        "total": total,
        "page": page,
        "limit": limit,
        "category_counts": category_counts,
        "user_balance": user_balance,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. PACK DETAIL & INTERACTIVE PREVIEW
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/packs/{pack_id}")
def get_marketplace_pack_detail(
    pack_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: Optional[models.User] = Depends(auth.get_current_user_optional)
):
    """
    Returns full pack metadata along with interactive sample flashcards (up to 3)
    and sample questions (up to 2) for deep user previews before downloading.
    """
    pack = db.query(models.StudyPack).filter(models.StudyPack.id == pack_id).first()
    if not pack:
        raise HTTPException(status_code=404, detail="Study pack not found.")

    # Only show non-approved packs to admin or original creator
    if pack.status != "approved":
        is_owner = current_user and current_user.id == pack.creator_id
        is_admin = current_user and (getattr(current_user, "role", None) == models.Roles.ADMIN)
        if not (is_owner or is_admin):
            raise HTTPException(status_code=403, detail="This study pack is currently under review.")

    pd = pack.pack_data or {}
    all_flashcards = pd.get("flashcards", [])
    all_questions = pd.get("questions", [])

    is_owned = False
    is_liked = False
    user_balance = 0

    if current_user:
        dl = db.query(models.StudyPackDownload).filter(
            models.StudyPackDownload.pack_id == pack.id,
            models.StudyPackDownload.user_id == current_user.id
        ).first()
        is_owned = bool(dl) or (pack.creator_id == current_user.id)

        lk = db.query(models.StudyPackLike).filter(
            models.StudyPackLike.pack_id == pack.id,
            models.StudyPackLike.user_id == current_user.id
        ).first()
        is_liked = bool(lk)

        cb = db.query(CreditBalance).filter(CreditBalance.user_id == current_user.id).first()
        if cb:
            user_balance = cb.balance

    creator_name = "Nu-Age Official"
    creator_username = "admin"
    creator_avatar = None
    if pack.creator:
        creator_name = f"{pack.creator.first_name} {pack.creator.last_name}".strip()
        creator_username = pack.creator.username
        creator_avatar = pack.creator.profile_picture_url

    # Sample items for interactive preview
    preview_cards = all_flashcards[:3]
    preview_questions = all_questions[:2]

    # Material content preview snippet
    mat_content = pd.get("material_content", "") or ""
    content_snippet = mat_content[:600] + ("…" if len(mat_content) > 600 else "")

    return {
        "id": str(pack.id),
        "title": pack.title,
        "description": pack.description or "",
        "category": pack.category,
        "theme_gradient": pack.theme_gradient or "purple_indigo",
        "cover_image_url": pack.cover_image_url,
        "price_coins": pack.price_coins,
        "is_official": pack.is_official,
        "status": pack.status,
        "downloads_count": pack.downloads_count,
        "likes_count": pack.likes_count,
        "created_at": pack.created_at.isoformat() if pack.created_at else None,
        "creator": {
            "id": str(pack.creator_id),
            "name": creator_name,
            "username": creator_username,
            "profile_picture_url": creator_avatar,
        },
        "stats": {
            "flashcards_count": len(all_flashcards),
            "questions_count": len(all_questions),
            "has_material_notes": bool(mat_content.strip()),
        },
        "content_snippet": content_snippet,
        "preview_flashcards": preview_cards,
        "preview_questions": preview_questions,
        "is_owned": is_owned,
        "is_liked": is_liked,
        "user_balance": user_balance,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 3. SUBMIT PACK FOR REVIEW (STUDENTS) OR AUTO-APPROVE (ADMINS)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/packs/submit")
def submit_pack_for_review(
    payload: PackSubmitPayload,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Submits a study material from the caller's vault to the Marketplace.
    Takes an immutable snapshot of the material, flashcards, and questions into pack_data.
    Admins are auto-approved as official packs; students enter the pending_review queue.
    """
    # 1. Fetch material
    mat = db.query(models.StudyMaterial).filter(
        models.StudyMaterial.id == payload.material_id,
        models.StudyMaterial.user_id == current_user.id
    ).first()

    if not mat:
        raise HTTPException(status_code=404, detail="Material not found in your vault.")

    # 2. Fetch associated flashcards and questions
    flashcards = db.query(models.Flashcard).filter(models.Flashcard.material_id == mat.id).all()
    questions = db.query(models.Question).filter(models.Question.material_id == mat.id).all()

    if len(flashcards) < 2 and len(questions) < 2:
        raise HTTPException(
            status_code=400,
            detail="Your material needs at least 2 flashcards or 2 questions to be published as a Study Pack."
        )

    # 3. Build frozen JSONB snapshot
    pack_data = {
        "material_title": mat.title,
        "material_content": mat.content or "",
        "source_type": mat.source_type or "text",
        "flashcards": [
            {"front": f.front, "back": f.back}
            for f in flashcards
        ],
        "questions": [
            {
                "question_text": q.question_text,
                "options": q.options,
                "answer_index": q.answer_index,
                "explanation": q.explanation,
                "difficulty": q.difficulty or "standard"
            }
            for q in questions
        ]
    }

    # 4. Check if caller has super-admin privileges
    role_str = str(getattr(current_user, "role", "")).upper()
    is_admin = (getattr(current_user, "role", None) == models.Roles.ADMIN or "ADMIN" in role_str)

    new_pack = models.StudyPack(
        creator_id=current_user.id,
        source_material_id=mat.id,
        title=payload.title.strip(),
        description=payload.description.strip() if payload.description else "",
        category=payload.category.strip() or "General",
        theme_gradient=payload.theme_gradient or "purple_indigo",
        price_coins=payload.price_coins,
        is_official=is_admin,
        status="approved" if is_admin else "pending_review",
        approved_at=datetime.now(timezone.utc) if is_admin else None,
        pack_data=pack_data
    )

    db.add(new_pack)
    db.commit()
    db.refresh(new_pack)

    return {
        "id": str(new_pack.id),
        "title": new_pack.title,
        "status": new_pack.status,
        "is_official": new_pack.is_official,
        "flashcards_count": len(flashcards),
        "questions_count": len(questions),
        "message": (
            "Official pack published successfully to the marketplace!"
            if is_admin
            else "Study pack submitted for admin review! You will earn Nu-Coins once approved."
        )
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4. DOWNLOAD & IMPORT TO VAULT (COIN ECONOMY & CLONER)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/packs/{pack_id}/download")
def download_and_import_pack(
    pack_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Purchases/downloads a study pack:
    1. Checks if already downloaded (idempotent; returns existing imported material).
    2. Validates & debits buyer's CreditBalance (if price > 0).
    3. Credits creator's CreditBalance with 70% author royalty (if price > 0 and not official).
    4. Clones material, flashcards (fresh SM-2 interval), and questions into buyer's vault.
    """
    pack = db.query(models.StudyPack).filter(models.StudyPack.id == pack_id).first()
    if not pack:
        raise HTTPException(status_code=404, detail="Study pack not found.")

    if pack.status != "approved" and pack.creator_id != current_user.id:
        raise HTTPException(status_code=400, detail="This pack is not approved for download.")

    # Check for existing download
    existing_dl = db.query(models.StudyPackDownload).filter(
        models.StudyPackDownload.pack_id == pack.id,
        models.StudyPackDownload.user_id == current_user.id
    ).first()

    if existing_dl and existing_dl.imported_material_id:
        # Check if the imported material still exists
        existing_mat = db.query(models.StudyMaterial).filter(
            models.StudyMaterial.id == existing_dl.imported_material_id
        ).first()
        if existing_mat:
            return {
                "success": True,
                "message": "Pack already in your vault!",
                "imported_material_id": str(existing_mat.id),
                "coins_spent": 0,
                "balance_after": _get_user_balance(db, current_user.id)
            }

    # If it costs coins and the user is NOT the creator, process transaction
    coins_spent = 0
    balance_after = _get_user_balance(db, current_user.id)

    if pack.price_coins > 0 and pack.creator_id != current_user.id:
        coins_spent = pack.price_coins
        balance_after = _credit_wallet(
            db,
            user_id=current_user.id,
            delta=-pack.price_coins,
            reason="marketplace_pack_download",
            ref_id=str(pack.id)
        )

        # Award creator royalty (70% of price)
        if not pack.is_official and pack.creator_id:
            royalty = int(pack.price_coins * 0.70)
            if royalty > 0:
                _credit_wallet(
                    db,
                    user_id=pack.creator_id,
                    delta=royalty,
                    reason="pack_royalty_reward",
                    ref_id=str(pack.id)
                )

    # ── CLONE INTO BUYER'S VAULT ─────────────────────────────────────────────
    pd = pack.pack_data or {}
    mat_content = pd.get("material_content", "") or ""
    mat_title = f"{pack.title} (Pack Import)"

    new_mat = models.StudyMaterial(
        user_id=current_user.id,
        title=mat_title,
        source_type="pack_import",
        content=mat_content,
        is_generating=False
    )
    db.add(new_mat)
    db.flush()

    # Clone Flashcards with fresh SM-2 SRS state
    now_utc = datetime.now(timezone.utc)
    for card_dict in pd.get("flashcards", []):
        front_text = card_dict.get("front", "").strip()
        back_text = card_dict.get("back", "").strip()
        if front_text and back_text:
            db.add(models.Flashcard(
                user_id=current_user.id,
                material_id=new_mat.id,
                front=front_text,
                back=back_text,
                repetitions=0,
                ease_factor=2.5,
                interval_days=0,
                next_review_date=now_utc
            ))

    # Clone Questions
    for q_dict in pd.get("questions", []):
        q_text = q_dict.get("question_text", "").strip()
        opts = q_dict.get("options", [])
        ans_idx = q_dict.get("answer_index", 0)
        expl = q_dict.get("explanation", "")
        diff = q_dict.get("difficulty", "standard")
        if q_text and opts:
            db.add(models.Question(
                user_id=current_user.id,
                material_id=new_mat.id,
                question_text=q_text,
                options=opts,
                answer_index=ans_idx,
                explanation=expl,
                difficulty=diff
            ))

    # Record or update Download entry
    if existing_dl:
        existing_dl.imported_material_id = new_mat.id
    else:
        new_dl = models.StudyPackDownload(
            pack_id=pack.id,
            user_id=current_user.id,
            coins_spent=coins_spent,
            imported_material_id=new_mat.id
        )
        db.add(new_dl)

    pack.downloads_count = (pack.downloads_count or 0) + 1
    db.commit()

    return {
        "success": True,
        "message": f"Successfully unlocked and imported '{pack.title}' into your vault!",
        "imported_material_id": str(new_mat.id),
        "coins_spent": coins_spent,
        "balance_after": balance_after
    }


def _get_user_balance(db: Session, user_id: uuid.UUID) -> int:
    cb = db.query(CreditBalance).filter(CreditBalance.user_id == user_id).first()
    return cb.balance if cb else 0


# ─────────────────────────────────────────────────────────────────────────────
# 5. LIKE / UPVOTE TOGGLE
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/packs/{pack_id}/like")
def toggle_like_pack(
    pack_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    pack = db.query(models.StudyPack).filter(models.StudyPack.id == pack_id).first()
    if not pack:
        raise HTTPException(status_code=404, detail="Study pack not found.")

    existing_like = db.query(models.StudyPackLike).filter(
        models.StudyPackLike.pack_id == pack.id,
        models.StudyPackLike.user_id == current_user.id
    ).first()

    if existing_like:
        db.delete(existing_like)
        pack.likes_count = max(0, (pack.likes_count or 1) - 1)
        liked = False
    else:
        db.add(models.StudyPackLike(pack_id=pack.id, user_id=current_user.id))
        pack.likes_count = (pack.likes_count or 0) + 1
        liked = True

    db.commit()
    return {"liked": liked, "likes_count": pack.likes_count}


# ─────────────────────────────────────────────────────────────────────────────
# 6. MY SUBMISSIONS (CREATOR PORTFOLIO)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/my-submissions")
def get_my_submissions(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """Lists study packs submitted by the current user across all review states."""
    packs = db.query(models.StudyPack).filter(
        models.StudyPack.creator_id == current_user.id
    ).order_by(models.StudyPack.created_at.desc()).all()

    items = []
    total_earned = 0
    for p in packs:
        pd = p.pack_data or {}
        fc_count = len(pd.get("flashcards", []))
        q_count = len(pd.get("questions", []))
        total_earned += (p.reward_coins_granted or 0)

        items.append({
            "id": str(p.id),
            "title": p.title,
            "description": p.description or "",
            "category": p.category,
            "theme_gradient": p.theme_gradient,
            "price_coins": p.price_coins,
            "status": p.status,
            "admin_review_notes": p.admin_review_notes,
            "reward_coins_granted": p.reward_coins_granted,
            "downloads_count": p.downloads_count,
            "likes_count": p.likes_count,
            "flashcards_count": fc_count,
            "questions_count": q_count,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "approved_at": p.approved_at.isoformat() if p.approved_at else None,
        })

    return {
        "items": items,
        "total_packs": len(items),
        "total_coins_earned": total_earned,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 7. ADMIN REVIEW QUEUE & CURATED PACK CREATION
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/admin/submissions")
def get_admin_pending_submissions(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_super_admin)
):
    """Super Admin: View pending study packs submitted for community review."""
    query = db.query(models.StudyPack).filter(
        models.StudyPack.status == "pending_review"
    ).order_by(models.StudyPack.created_at.asc())

    total = query.count()
    packs = query.offset((page - 1) * limit).limit(limit).all()

    items = []
    for p in packs:
        pd = p.pack_data or {}
        creator_name = f"{p.creator.first_name} {p.creator.last_name}".strip() if p.creator else "Unknown"

        items.append({
            "id": str(p.id),
            "title": p.title,
            "description": p.description or "",
            "category": p.category,
            "theme_gradient": p.theme_gradient,
            "price_coins": p.price_coins,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "creator": {
                "id": str(p.creator_id),
                "name": creator_name,
                "username": p.creator.username if p.creator else "user",
                "email": p.creator.email if p.creator else "",
            },
            "stats": {
                "flashcards_count": len(pd.get("flashcards", [])),
                "questions_count": len(pd.get("questions", [])),
                "content_length": len(pd.get("material_content", "")),
            },
            "sample_flashcards": pd.get("flashcards", [])[:3],
            "sample_questions": pd.get("questions", [])[:2],
        })

    return {
        "items": items,
        "total": total,
        "page": page,
        "limit": limit
    }


@router.post("/admin/submissions/{pack_id}/review")
def review_pack_submission(
    pack_id: uuid.UUID,
    payload: AdminReviewPayload,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_super_admin)
):
    """
    Super Admin: Approves or rejects a study pack.
    When approved, grants Nu-Coin reward to creator and dispatches push notification.
    """
    pack = db.query(models.StudyPack).filter(models.StudyPack.id == pack_id).first()
    if not pack:
        raise HTTPException(status_code=404, detail="Study pack not found.")

    if payload.action == "approve":
        pack.status = "approved"
        pack.approved_at = datetime.now(timezone.utc)
        pack.reward_coins_granted = payload.reward_coins
        pack.admin_review_notes = payload.review_notes or "Approved by Platform Super Admin"

        # Award reward coins to creator
        if payload.reward_coins > 0 and pack.creator_id:
            _credit_wallet(
                db,
                user_id=pack.creator_id,
                delta=payload.reward_coins,
                reason="pack_approval_reward",
                ref_id=str(pack.id)
            )

        # Send push notification
        if pack.creator_id:
            background_tasks.add_task(
                send_push_notification,
                db=db,
                user_ids=[pack.creator_id],
                title="🎉 Study Pack Approved!",
                body=f"Your pack '{pack.title}' has been approved and published! You earned {payload.reward_coins} Nu-Coins.",
                action_route="/study",
                category="study_pack_approved"
            )

    else:  # reject
        pack.status = "rejected"
        pack.admin_review_notes = payload.review_notes or "Did not meet quality guidelines."

        if pack.creator_id:
            background_tasks.add_task(
                send_push_notification,
                db=db,
                user_ids=[pack.creator_id],
                title="Study Pack Feedback",
                body=f"Your pack '{pack.title}' was reviewed: {pack.admin_review_notes}",
                action_route="/study",
                category="study_pack_rejected"
            )

    db.commit()
    return {
        "success": True,
        "pack_id": str(pack.id),
        "status": pack.status,
        "reward_coins": pack.reward_coins_granted
    }


@router.post("/admin/packs/create-curated")
def create_official_curated_pack(
    payload: AdminCuratedPackPayload,
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_super_admin)
):
    """
    Super Admin: Directly publish an official curated generation pack with preloaded content.
    """
    pack_data = {
        "material_title": payload.title,
        "material_content": payload.material_content or "",
        "source_type": "curated_official",
        "flashcards": payload.flashcards,
        "questions": payload.questions
    }

    new_pack = models.StudyPack(
        creator_id=admin.id,
        title=payload.title.strip(),
        description=payload.description or "",
        category=payload.category.strip() or "General",
        theme_gradient=payload.theme_gradient or "emerald_teal",
        cover_image_url=payload.cover_image_url,
        price_coins=payload.price_coins,
        is_official=True,
        status="approved",
        approved_at=datetime.now(timezone.utc),
        pack_data=pack_data
    )

    db.add(new_pack)
    db.commit()
    db.refresh(new_pack)

    return {
        "id": str(new_pack.id),
        "title": new_pack.title,
        "is_official": True,
        "status": "approved",
        "message": "Official curated pack created and published successfully!"
    }
