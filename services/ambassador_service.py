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
from sqlalchemy import func
from sqlalchemy.orm import Session
from fastapi import HTTPException, status

import models
from services.email_service import send_email


def _build_ambassador_invite_html(email: str, university: str, accept_link: str, candidate_name: Optional[str] = None) -> str:
    """Renders a responsive, high-converting HTML email invitation in Nu Age brand colors."""
    greeting = f"Hello {candidate_name}," if candidate_name else "Hello,"
    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>Campus Ambassador Invitation — Nu Age</title>
      <style>
        body {{
          font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
          background-color: #F4F8F4;
          color: #0F172A;
          margin: 0;
          padding: 32px 16px;
        }}
        .email-container {{
          max-width: 560px;
          margin: 0 auto;
        }}
        .card {{
          background-color: #FFFFFF;
          border: 1px solid #E2E8F0;
          border-radius: 16px;
          padding: 36px 32px;
          box-shadow: 0 8px 24px rgba(15, 23, 42, 0.04);
        }}
        .brand-header {{
          display: flex;
          align-items: center;
          gap: 10px;
          margin-bottom: 22px;
        }}
        .brand-logo {{
          width: 34px;
          height: 34px;
          border-radius: 8px;
          vertical-align: middle;
        }}
        .brand-title {{
          font-size: 18px;
          font-weight: 800;
          color: #0F172A;
          letter-spacing: -0.3px;
        }}
        .brand-title span {{
          color: #37BF14;
        }}
        .badge {{
          display: inline-block;
          background-color: #EDF7EE;
          color: #156637;
          border: 1px solid rgba(55, 191, 20, 0.35);
          font-weight: 700;
          font-size: 11px;
          padding: 4px 12px;
          border-radius: 20px;
          text-transform: uppercase;
          letter-spacing: 0.6px;
          margin-bottom: 14px;
        }}
        h1 {{
          font-size: 22px;
          font-weight: 800;
          margin: 0 0 14px 0;
          color: #0F172A;
          line-height: 1.3;
        }}
        p {{
          font-size: 14.5px;
          line-height: 1.65;
          color: #334155;
          margin: 0 0 16px 0;
        }}
        .perks-container {{
          margin: 20px 0 24px 0;
          display: flex;
          flex-direction: column;
          gap: 10px;
        }}
        .perk-item {{
          background: #F8FAF8;
          border: 1px solid #E2E8F0;
          border-left: 3.5px solid #37BF14;
          border-radius: 10px;
          padding: 12px 14px;
          text-align: left;
        }}
        .perk-item strong {{
          color: #0F172A;
          font-size: 13.5px;
        }}
        .perk-item p {{
          margin: 3px 0 0 0;
          font-size: 12.5px;
          color: #64748B;
          line-height: 1.45;
        }}
        .cta-wrap {{
          text-align: center;
          margin: 28px 0 20px 0;
        }}
        .cta-btn {{
          display: inline-block;
          background-color: #156637;
          color: #FFFFFF !important;
          text-decoration: none;
          font-weight: 700;
          font-size: 15px;
          padding: 14px 34px;
          border-radius: 12px;
          box-shadow: 0 4px 14px rgba(21, 102, 55, 0.25);
        }}
        .link-fallback {{
          font-size: 12px;
          color: #64748B;
          background: #F8FAFC;
          border: 1px dashed #CBD5E1;
          border-radius: 8px;
          padding: 12px 14px;
          word-break: break-all;
          line-height: 1.5;
        }}
        .link-fallback a {{
          color: #156637;
          font-weight: 600;
          text-decoration: underline;
        }}
        .signature {{
          border-top: 1px solid #E2E8F0;
          margin-top: 28px;
          padding-top: 20px;
        }}
        .signature .name {{
          font-size: 14px;
          font-weight: 700;
          color: #0F172A;
        }}
        .signature .role {{
          font-size: 12px;
          color: #156637;
          font-weight: 600;
          margin-top: 2px;
        }}
        .footer {{
          font-size: 11.5px;
          color: #64748B;
          text-align: center;
          margin-top: 20px;
          line-height: 1.6;
        }}
      </style>
    </head>
    <body>
      <div class="email-container">
        <div class="card">
          <table cellpadding="0" cellspacing="0" border="0" style="margin-bottom: 18px;">
            <tr>
              <td style="vertical-align: middle; padding-right: 10px;">
                <img src="https://nu-age-cdn.b-cdn.net/logos/icon.png" alt="Nu Age Logo" class="brand-logo" />
              </td>
              <td style="vertical-align: middle;">
                <span class="brand-title">Nu Age<span style="color:#37BF14;">.</span></span>
              </td>
            </tr>
          </table>

          <div class="badge">Official Invitation</div>
          <h1>Become a Campus Ambassador for Nu Age</h1>

          <p>{greeting}</p>
          <p>You have been officially invited to lead student growth as a <strong>Campus Ambassador</strong> representing <strong>{university}</strong> on Nu Age.</p>

          <p>As a verified campus ambassador, you will unlock:</p>

          <div class="perks-container">
            <div class="perk-item">
              <strong>🏅 Verified Ambassador Badge</strong>
              <p>Distinguished profile badge displayed across student chats, course discussions, and community leaderboards.</p>
            </div>
            <div class="perk-item">
              <strong>🎟️ Dedicated Promo Code</strong>
              <p>Your unique code for students at {university} with custom discounts and bonus study packs.</p>
            </div>
            <div class="perk-item">
              <strong>💰 Nu-Coin Commissions &amp; AI Credits</strong>
              <p>Earn Nu-Coins and AI credits directly to your account for student sign-ups, course redemptions, and active study groups.</p>
            </div>
            <div class="perk-item">
              <strong>🤝 Direct Mentorship &amp; Executive Network</strong>
              <p>Monthly leadership calls, product roadmap previews, priority support, and official recommendation letters.</p>
            </div>
          </div>

          <div class="cta-wrap">
            <a href="{accept_link}" class="cta-btn">Accept Ambassador Invitation &rarr;</a>
          </div>

          <div class="link-fallback">
            If the button above does not open, copy and paste this link into your browser:<br>
            <a href="{accept_link}">{accept_link}</a>
          </div>

          <div class="signature">
            <div class="name">Tobi Davies</div>
            <div class="role">Founder &amp; Engineering &bull; Nu Age</div>
          </div>
        </div>

        <div class="footer">
          &copy; 2026 Nu Age &bull; The African Learning Ecosystem &bull; Offline-First Continuity<br>
          If you were not expecting this invitation, you can safely ignore this email.
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
    Invites a learner to become a campus ambassador.
    Mirrors the organisation invitation model: candidates may already be registered
    or may register after receiving the invitation.
    """
    clean_email = (email or "").strip().lower()
    clean_uni = (university or "").strip()

    if not clean_email or "@" not in clean_email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A valid email address is required.")

    user = db.query(models.User).filter(func.lower(models.User.email) == clean_email).first()
    if not clean_uni:
        clean_uni = getattr(user, "university", None) or "Campus Partner"

    # Check for existing ambassador record
    existing = db.query(models.CampusAmbassador).filter(func.lower(models.CampusAmbassador.email) == clean_email).first()
    if existing and existing.status == "active":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This user is already an active Campus Ambassador.")

    invite_token = secrets.token_urlsafe(32)

    if existing:
        ambassador = existing
        ambassador.university = clean_uni
        ambassador.invite_token = invite_token
        ambassador.status = "invited"
        ambassador.invited_at = datetime.now(timezone.utc)
        ambassador.user_id = user.id if user else None
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
            user_id=user.id if user else None,
        )
        db.add(ambassador)

    db.commit()
    db.refresh(ambassador)

    # Dispatch email invitation
    candidate_name = f"{user.first_name or ''} {user.last_name or ''}".strip() if user else None
    accept_url = f"https://nu-age.name.ng/ambassadors/accept?token={invite_token}"
    html_content = _build_ambassador_invite_html(clean_email, clean_uni, accept_url, candidate_name=candidate_name)
    subject = f"Invitation: Campus Ambassador for {clean_uni} - Nu Age"

    try:
        send_email(to=clean_email, subject=subject, html=html_content)
    except Exception as ex:
        print(f"[Ambassador] Email send warning: {ex}")

    return ambassador


def process_ambassador_invitation(db: Session, token: str) -> Dict[str, Any]:
    """
    Public endpoint logic mirroring /organisations/process-invite.
    Evaluates invitation token, checks user presence, and returns:
    - 'success': Activates ambassador role and awards badge.
    - 'already_active': Idempotent return for already-active ambassador.
    - 'needs_signup': User not registered yet, directs to sign up first.
    """
    clean_tok = (token or "").strip()
    if not clean_tok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Token is required.")

    ambassador = db.query(models.CampusAmbassador).filter(models.CampusAmbassador.invite_token == clean_tok).first()
    if not ambassador:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid or expired invitation token.")

    if ambassador.status == "active":
        return {
            "status": "already_active",
            "university": ambassador.university,
            "email": ambassador.email,
            "message": "Invitation already accepted.",
        }

    # Check if targeted email is registered on platform
    user = db.query(models.User).filter(func.lower(models.User.email) == func.lower(ambassador.email)).first()
    if not user:
        return {
            "status": "needs_signup",
            "email": ambassador.email,
            "university": ambassador.university,
            "message": f"Account not found for {ambassador.email}. Please sign up on Nu Age first.",
        }

    # Activate ambassador
    ambassador.status = "active"
    ambassador.user_id = user.id
    ambassador.accepted_at = datetime.now(timezone.utc)

    # Note: Promo code binding is intentionally NOT done automatically here (managed manually).

    # Award campus_ambassador badge
    badge = db.query(models.MemberBadge).filter(models.MemberBadge.slug == "campus_ambassador").first()
    if badge:
        user_badge = db.query(models.UserBadge).filter(
            models.UserBadge.user_id == user.id,
            models.UserBadge.badge_id == badge.id
        ).first()
        if not user_badge:
            user_badge = models.UserBadge(
                user_id=user.id,
                badge_id=badge.id,
                is_primary=True
            )
            db.add(user_badge)

    db.commit()

    candidate_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or user.username
    return {
        "status": "success",
        "university": ambassador.university,
        "name": candidate_name,
        "email": ambassador.email,
        "message": f"Congratulations! You are now an active Campus Ambassador for {ambassador.university}.",
    }


def accept_ambassador_invitation(db: Session, token: str, user_id: uuid.UUID) -> Dict[str, Any]:
    """
    Accepts an invitation using the secure invite token and binds to the user's account.
    Maintained for authenticated in-app calls.
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
                is_primary=True
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


def submit_ambassador_application(db: Session, payload: Dict[str, Any]) -> models.CampusAmbassadorApplication:
    """
    Persists a public ambassador application from nu-web/ambassadors.html.
    """
    clean_email = (payload.get("email") or "").strip().lower()
    clean_name = (payload.get("full_name") or "").strip()
    clean_uni = (payload.get("university") or "").strip()
    clean_dept = (payload.get("department") or "").strip()
    clean_level = (payload.get("level") or "").strip()
    clean_phone = (payload.get("phone") or "").strip() or None
    clean_notes = (payload.get("notes") or "").strip() or None

    if not clean_email or "@" not in clean_email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A valid email address is required.")
    if not clean_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Full name is required.")
    if not clean_uni:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="University is required.")
    if not clean_dept:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Department is required.")
    if not clean_level:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Level of study is required.")

    # Check for existing pending application with this email to avoid duplicate rows
    existing = db.query(models.CampusAmbassadorApplication).filter(
        func.lower(models.CampusAmbassadorApplication.email) == clean_email,
        models.CampusAmbassadorApplication.status == "pending"
    ).first()

    if existing:
        existing.full_name = clean_name
        existing.university = clean_uni
        existing.department = clean_dept
        existing.level = clean_level
        existing.phone = clean_phone
        existing.notes = clean_notes
        app = existing
    else:
        app = models.CampusAmbassadorApplication(
            full_name=clean_name,
            email=clean_email,
            university=clean_uni,
            department=clean_dept,
            level=clean_level,
            phone=clean_phone,
            notes=clean_notes,
            status="pending",
        )
        db.add(app)

    db.commit()
    db.refresh(app)
    return app


def list_ambassador_applications_admin(db: Session, status_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Returns campus ambassador applications left-joined with models.User to surface platform learner metrics.
    """
    query = db.query(
        models.CampusAmbassadorApplication,
        models.User
    ).outerjoin(
        models.User,
        func.lower(models.User.email) == func.lower(models.CampusAmbassadorApplication.email)
    )

    if status_filter and status_filter.strip() and status_filter.strip().lower() != "all":
        query = query.filter(models.CampusAmbassadorApplication.status == status_filter.strip().lower())

    records = query.order_by(models.CampusAmbassadorApplication.created_at.desc()).all()
    results = []

    for app, user in records:
        is_on_platform = bool(user is not None)
        user_id = str(user.id) if user else None
        user_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or user.username if user else None
        username = user.username if user else None
        avatar_url = getattr(user, "profile_picture_url", None) if user else None
        registered_at = user.created_at.isoformat() if (user and getattr(user, "created_at", None)) else None
        coins_balance = getattr(user, "coins_balance", 0) if user else 0
        streak = getattr(user, "streak", 0) if user else 0
        is_verified = bool(getattr(user, "is_verified", False)) if user else False

        # Course enrollments count
        courses_enrolled_count = 0
        if user:
            courses_enrolled_count = db.query(func.count(models.Enrollment.course_id)).filter(
                models.Enrollment.student_id == user.id
            ).scalar() or 0

        results.append({
            "id": str(app.id),
            "full_name": app.full_name,
            "email": app.email,
            "university": app.university,
            "department": app.department,
            "level": app.level,
            "phone": app.phone,
            "notes": app.notes,
            "status": app.status,
            "created_at": app.created_at.isoformat() if app.created_at else None,
            "reviewed_at": app.reviewed_at.isoformat() if app.reviewed_at else None,
            "reviewed_by": str(app.reviewed_by) if app.reviewed_by else None,
            # Platform user presence & stats
            "is_on_platform": is_on_platform,
            "user_id": user_id,
            "user_name": user_name,
            "username": username,
            "avatar_url": avatar_url,
            "registered_at": registered_at,
            "courses_enrolled_count": courses_enrolled_count,
            "coins_balance": coins_balance,
            "streak": streak,
            "is_verified": is_verified,
        })

    return results


def review_ambassador_application(
    db: Session,
    application_id: uuid.UUID,
    action: str,
    admin_user_id: Optional[uuid.UUID] = None
) -> Dict[str, Any]:
    """
    Approves or rejects a campus ambassador application.
    When approved, automatically creates a CampusAmbassador record and sends invitation email.
    """
    app = db.query(models.CampusAmbassadorApplication).filter(models.CampusAmbassadorApplication.id == application_id).first()
    if not app:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found.")

    clean_action = (action or "").strip().lower()
    if clean_action not in ("approve", "reject"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Action must be 'approve' or 'reject'.")

    now = datetime.now(timezone.utc)
    if clean_action == "approve":
        app.status = "approved"
        app.reviewed_at = now
        app.reviewed_by = admin_user_id
        db.commit()

        # Automatically dispatch ambassador invitation
        amb = invite_campus_ambassador(
            db=db,
            email=app.email,
            university=app.university,
            notes=f"Approved application from {app.full_name}",
        )
        return {
            "status": "success",
            "message": f"Application approved! Invitation email sent to {app.email}.",
            "ambassador_id": str(amb.id),
            "application_status": "approved",
        }
    else:
        app.status = "rejected"
        app.reviewed_at = now
        app.reviewed_by = admin_user_id
        db.commit()
        return {
            "status": "success",
            "message": f"Application for {app.email} rejected.",
            "application_status": "rejected",
        }

