"""
services/ambassador_service.py
──────────────────────────────────────────────────────────────────────────────
Campus Ambassador Lifecycle Engine for Nu-Age:
- Persistent invitation dispatch with secure tokens.
- Branded Resend/SES email notifications.
- Tokenized acceptance workflow.
- Status management (invited, active, declined, suspended).
──────────────────────────────────────────────────────────────────────────────
"""

import secrets
import uuid
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from fastapi import HTTPException, status

import models
from services.email_service import send_email


def _build_ambassador_invite_html(email: str, university: str, accept_link: str, candidate_name: Optional[str] = None) -> str:
    """Renders a responsive, high-converting HTML email invitation."""
    greeting = f"Hello {candidate_name}," if candidate_name else "Hello,"
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #0F1117; color: #FFFFFF; margin: 0; padding: 24px; }}
        .card {{ background-color: #181B24; border: 1px solid rgba(255,255,255,0.1); border-radius: 16px; padding: 32px; max-width: 540px; margin: 0 auto; }}
        .badge {{ display: inline-block; background-color: rgba(76, 175, 80, 0.15); color: #4CAF50; font-weight: 700; font-size: 11px; padding: 4px 10px; border-radius: 6px; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 12px; }}
        h1 {{ font-size: 22px; margin: 0 0 12px 0; color: #FFFFFF; }}
        p {{ font-size: 14px; line-height: 1.6; color: #94A3B8; margin: 0 0 16px 0; }}
        .highlight {{ color: #4CAF50; font-weight: 600; }}
        .cta {{ display: inline-block; background-color: #4CAF50; color: #FFFFFF; text-decoration: none; font-weight: 700; font-size: 14px; padding: 12px 28px; border-radius: 10px; margin-top: 12px; margin-bottom: 24px; }}
        .footer {{ font-size: 11px; color: #64748B; border-top: 1px solid rgba(255,255,255,0.08); padding-top: 16px; }}
      </style>
    </head>
    <body>
      <div class="card">
        <div class="badge">Official Invitation</div>
        <h1>Become a Campus Ambassador for Nu-Age</h1>
        <p>{greeting}</p>
        <p>You have been officially invited to lead student growth as a <strong>Campus Ambassador</strong> representing <strong>{university}</strong> on Nu-Age.</p>
        <p>As a verified ambassador, you will receive:</p>
        <ul style="color: #94A3B8; font-size: 14px; line-height: 1.6; padding-left: 20px;">
          <li>An exclusive <strong style="color:#FFF;">Campus Ambassador</strong> badge displayed on your profile, chat messages, and leaderboards.</li>
          <li>A dedicated promo code to distribute to students at {university} with custom study perks.</li>
          <li>Nu-Coin commissions and AI credits for every student onboarding and redemption.</li>
          <li>Direct mentorship and access to the Nu-Age leadership network.</li>
        </ul>
        <div style="text-align: center;">
          <a href="{accept_link}" class="cta">Accept Ambassador Role</a>
        </div>
        <p style="font-size: 12px; color: #64748B;">Or copy and paste this link into your browser:<br><span style="color:#4CAF50;">{accept_link}</span></p>
        <div class="footer">
          Nu-Age Learning Technologies • Bridging Higher Education and Intelligence<br>
          If you did not expect this invitation, you can safely ignore this email.
        </div>
      </div>
    </body>
    </html>
    """


def invite_campus_ambassador(
    db: Session,
    email: str,
    university: Optional[str] = None,
    notes: Optional[str] = None,
    assigned_promo_code_id: Optional[uuid.UUID] = None
) -> models.CampusAmbassador:
    """
    Invites a registered learner to become a campus ambassador.
    Enforces that the email belongs to an existing platform user in models.User.
    """
    clean_email = (email or "").strip().lower()
    clean_uni = (university or "").strip()

    if not clean_email or "@" not in clean_email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A valid email address is required.")

    # Strictly require candidate to be an existing registered learner
    user = db.query(models.User).filter(models.User.email == clean_email).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No registered Nu-Age account found with email '{clean_email}'. Campus Ambassadors must already be registered learners on the platform."
        )

    if not clean_uni:
        clean_uni = getattr(user, "university", None) or "Campus Partner"

    # Check for existing ambassador record
    existing = db.query(models.CampusAmbassador).filter(models.CampusAmbassador.email == clean_email).first()
    if existing and existing.status == "active":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This user is already an active Campus Ambassador.")

    invite_token = secrets.token_urlsafe(32)

    if existing:
        ambassador = existing
        ambassador.university = clean_uni
        ambassador.invite_token = invite_token
        ambassador.status = "invited"
        ambassador.invited_at = datetime.now(timezone.utc)
        ambassador.user_id = user.id
        if notes:
            ambassador.notes = notes
        if assigned_promo_code_id:
            ambassador.assigned_promo_code_id = assigned_promo_code_id
    else:
        ambassador = models.CampusAmbassador(
            email=clean_email,
            university=clean_uni,
            invite_token=invite_token,
            status="invited",
            notes=notes,
            assigned_promo_code_id=assigned_promo_code_id,
            user_id=user.id,
        )
        db.add(ambassador)

    db.commit()
    db.refresh(ambassador)

    # Dispatch email invitation
    candidate_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or user.username
    accept_url = f"https://nu-age.name.ng/ambassador/accept?token={invite_token}"
    html_content = _build_ambassador_invite_html(clean_email, clean_uni, accept_url, candidate_name=candidate_name)
    subject = f"Invitation: Campus Ambassador for {clean_uni} - Nu-Age"

    try:
        send_email(to=clean_email, subject=subject, html=html_content)
    except Exception as ex:
        print(f"[Ambassador] Email send warning: {ex}")

    return ambassador


def accept_ambassador_invitation(db: Session, token: str, user_id: uuid.UUID) -> Dict[str, Any]:
    """
    Accepts an invitation using the secure invite token and binds to the user's account.
    Awards the 'campus_ambassador' badge immediately.
    """
    clean_tok = (token or "").strip()
    if not clean_tok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Token is required.")

    ambassador = db.query(models.CampusAmbassador).filter(models.CampusAmbassador.invite_token == clean_tok).first()
    if not ambassador:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid or expired invitation token.")

    if ambassador.status == "active":
        return {
            "status": "success",
            "message": "Invitation already accepted.",
            "university": ambassador.university,
        }

    ambassador.status = "active"
    ambassador.user_id = user_id
    ambassador.accepted_at = datetime.now(timezone.utc)

    # Link assigned promo code if exists
    if ambassador.assigned_promo_code_id:
        promo = db.query(models.PromoCode).filter(models.PromoCode.id == ambassador.assigned_promo_code_id).first()
        if promo and not promo.ambassador_id:
            promo.ambassador_id = user_id

    # Award campus_ambassador badge
    badge = db.query(models.MemberBadge).filter(models.MemberBadge.slug == "campus_ambassador").first()
    if badge:
        user_badge = db.query(models.UserBadge).filter(
            models.UserBadge.user_id == user_id,
            models.UserBadge.badge_id == badge.id
        ).first()
        if not user_badge:
            user_badge = models.UserBadge(
                user_id=user_id,
                badge_id=badge.id,
                is_primary=True  # Automatically set as primary
            )
            db.add(user_badge)

    db.commit()

    return {
        "status": "success",
        "message": f"Congratulations! You are now an active Campus Ambassador for {ambassador.university}.",
        "university": ambassador.university,
    }


def list_campus_ambassadors(db: Session) -> List[Dict[str, Any]]:
    """Returns all campus ambassador records with user summaries and redemptions statistics."""
    from sqlalchemy import func
    ambassadors = db.query(models.CampusAmbassador).order_by(models.CampusAmbassador.invited_at.desc()).all()
    results = []

    for a in ambassadors:
        user_name = "—"
        user_username = "—"
        if a.user:
            fn = a.user.first_name or ""
            ln = a.user.last_name or ""
            user_name = f"{fn} {ln}".strip() or a.user.username or "—"
            user_username = a.user.username or "—"

        code_str = a.promo_code.code if a.promo_code else None
        uses_count = a.promo_code.uses_count if a.promo_code else 0

        # Sum earned coins from referral commissions
        total_coins = 0
        if a.user_id:
            coin_sum = db.query(func.coalesce(func.sum(models.CoinTransaction.amount), 0)).filter(
                models.CoinTransaction.user_id == a.user_id,
                models.CoinTransaction.source_type == "ambassador_referral",
            ).scalar()
            total_coins = int(coin_sum or 0)

        results.append({
            "id": str(a.id),
            "email": a.email,
            "university": a.university,
            "status": a.status,
            "user_id": str(a.user_id) if a.user_id else None,
            "name": user_name,
            "user_name": user_name,
            "user_username": user_username,
            "promo_code": code_str,
            "assigned_promo_code_id": str(a.assigned_promo_code_id) if a.assigned_promo_code_id else None,
            "total_referrals": uses_count,
            "students_referred_count": uses_count,
            "total_coins_earned": total_coins,
            "invited_at": a.invited_at.isoformat() if a.invited_at else None,
            "accepted_at": a.accepted_at.isoformat() if a.accepted_at else None,
            "notes": a.notes,
        })

    return results


def toggle_ambassador_status(db: Session, ambassador_id: uuid.UUID, new_status: str) -> Dict[str, Any]:
    """Updates the ambassador's status ('active', 'suspended', 'declined', 'invited')."""
    ambassador = db.query(models.CampusAmbassador).filter(models.CampusAmbassador.id == ambassador_id).first()
    if not ambassador:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ambassador record not found.")

    clean_status = (new_status or "").strip().lower()
    if clean_status == "inactive":
        clean_status = "suspended"

    valid_statuses = {"invited", "active", "suspended", "declined"}
    if clean_status not in valid_statuses:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Status must be one of {valid_statuses}")

    ambassador.status = clean_status
    db.commit()
    db.refresh(ambassador)

    return {
        "status": "success",
        "message": f"Ambassador status updated to {clean_status}.",
        "ambassador_id": str(ambassador.id),
        "new_status": ambassador.status,
    }
