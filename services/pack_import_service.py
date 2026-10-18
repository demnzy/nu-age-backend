"""
services/pack_import_service.py
──────────────────────────────────────────────────────────────────────────────
Shared service for cloning a StudyPack into a user's vault, including:
- Generating StudyMaterial
- Deep cloning Flashcards with fresh SM-2 SRS review intervals
- Deep cloning CBT Questions
- Recording or updating StudyPackDownload tracking record
──────────────────────────────────────────────────────────────────────────────
"""

import re
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from sqlalchemy.orm import Session

import models


def import_pack_into_vault(
    db: Session,
    pack: models.StudyPack,
    user_id: uuid.UUID,
    coins_spent: int = 0,
    commit: bool = True,
) -> models.StudyMaterial:
    """
    Clones the content of `pack` into `user_id`'s study vault.
    Idempotent on existing downloads: updates or creates StudyPackDownload.
    """
    existing_dl = db.query(models.StudyPackDownload).filter(
        models.StudyPackDownload.pack_id == pack.id,
        models.StudyPackDownload.user_id == user_id,
    ).first()

    if existing_dl and existing_dl.imported_material_id:
        existing_mat = db.query(models.StudyMaterial).filter(
            models.StudyMaterial.id == existing_dl.imported_material_id
        ).first()
        if existing_mat:
            return existing_mat

    pd = pack.pack_data or {}
    mat_content = pd.get("material_content", "") or ""
    mat_title = re.sub(r"(?i)\s*[\[\(]\s*pack\s*import\s*[\]\)]\s*[:\-–—]?\s*", " ", pack.title)
    mat_title = re.sub(r"(?i)^pack\s*import\s*[:\-–—]?\s*", "", mat_title).strip() or "Study Pack"

    is_cbt = (
        pd.get("source_type") in ("cbt_pack", "cbt")
        or bool(pd.get("exam_type"))
        or (len(pd.get("questions", [])) > 0 and len(pd.get("flashcards", [])) == 0)
        or bool(re.search(r"(?i)\b(CBT|JAMB|WAEC|NECO|UTME|WASSCE|SSCE|POST-UTME|SAT|GRE|GMAT)\b", str(pack.category or "") + " " + str(pack.title or "")))
    )
    mat_source_type = "cbt_pack" if is_cbt else "pack_import"

    new_mat = models.StudyMaterial(
        user_id=user_id,
        title=mat_title,
        source_type=mat_source_type,
        content=mat_content,
        is_generating=False,
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
                user_id=user_id,
                material_id=new_mat.id,
                front=front_text,
                back=back_text,
                repetitions=0,
                ease_factor=2.5,
                interval_days=0,
                next_review_date=now_utc,
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
                user_id=user_id,
                material_id=new_mat.id,
                question_text=q_text,
                options=opts,
                answer_index=ans_idx,
                explanation=expl,
                difficulty=diff,
                image_url=q_dict.get("image_url"),
                topic=q_dict.get("topic"),
                exam_type=q_dict.get("exam_type"),
                exam_year=q_dict.get("exam_year"),
            ))

    # Record or update Download entry
    if existing_dl:
        existing_dl.imported_material_id = new_mat.id
    else:
        new_dl = models.StudyPackDownload(
            pack_id=pack.id,
            user_id=user_id,
            coins_spent=coins_spent,
            imported_material_id=new_mat.id,
        )
        db.add(new_dl)

    pack.downloads_count = (pack.downloads_count or 0) + 1

    if commit:
        db.commit()
    else:
        db.flush()

    return new_mat
