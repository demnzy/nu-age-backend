from fastapi import APIRouter, Depends, HTTPException, Request, Query, status
from sqlalchemy.orm import Session
from datetime import datetime, timezone, timedelta
from uuid import UUID
import hmac
import json
import os

import models
import schemas
from database import get_db
from services import auth
from monetization_models import (
    CreditBalance, CreditPack, CreditPurchase, CreditLedgerEntry,
    PlanAIDraftingConfig, OrganisationAIDraftingUsage, OrgDraftLedgerEntry,
    RevenueCatWebhookEvent, DraftStatus,
)

router = APIRouter(prefix="/monetization", tags=["Monetization"])

REVENUECAT_WEBHOOK_SECRET = os.environ["REVENUECAT_WEBHOOK_SECRET"]
RC_WEB_CHECKOUT_URL = os.environ["RC_WEB_CHECKOUT_URL"]           # student credit packs
RC_WEB_CHECKOUT_URL_ORG = os.environ["RC_WEB_CHECKOUT_URL_ORG"]   # org plan upgrades

CYCLE_LENGTH = timedelta(days=30)


# ==========================================
# 1. STUDENT CREDITS — AI Self-Study Hub
# ==========================================
#
# NOTE ON WIRING: study.py's existing POST /study/generate already does its
# own free-quota check + increment inline (sub.generations_used += 1) and
# 403s when exhausted. To add the credits fallback, replace that block in
# study.py with the exact snippet in the comment at the bottom of this
# file — it falls back to CreditBalance instead of hard-403ing once the
# free quota is gone. Nothing here is called BY study.py automatically;
# you wire it in directly.

@router.get("/credits/balance")
def get_credit_balance(db: Session = Depends(get_db), user = Depends(auth.get_current_user)):
    balance = db.query(CreditBalance).filter(CreditBalance.user_id == user.id).first()
    if not balance:
        balance = CreditBalance(user_id=user.id, balance=0)
        db.add(balance)
        db.commit()
        db.refresh(balance)

    sub = db.query(models.UserSubscription).filter(models.UserSubscription.user_id == user.id).first()

    return {
        "credits_remaining": balance.balance,
        "free_used": sub.generations_used if sub else 0,
        "free_limit": sub.plan.generations_limit if sub else None,
    }


@router.get("/credits/packs")
def list_credit_packs(db: Session = Depends(get_db)):
    """Public — powers the paywall screen's list of purchasable packs."""
    return db.query(CreditPack).filter(CreditPack.active == True).all()


@router.get("/credits/checkout-url")
def get_credit_checkout_url(
    product_id: str = Query(..., description="CreditPack.id, e.g. 'credits_10'"),
    db: Session = Depends(get_db),
    user = Depends(auth.get_current_user)
):
    """Flet opens this via page.launch_url() when the user taps Buy on a pack."""
    pack = db.query(CreditPack).filter(CreditPack.id == product_id, CreditPack.active == True).first()
    if not pack:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown or inactive credit pack")

    url = f"{RC_WEB_CHECKOUT_URL}?app_user_id={user.id}&product_id={pack.id}"
    return {
        "checkout_url": url,
        "pack": {"id": pack.id, "label": pack.label, "credits": pack.credits, "price_display": pack.price_display}
    }


@router.post("/credits/refresh")
def refresh_credit_balance(db: Session = Depends(get_db), user = Depends(auth.get_current_user)):
    """
    Call on app resume / return from checkout tab. Does NOT grant anything
    itself — the webhook is the source of truth, this just re-reads state
    so the UI isn't stuck showing a stale balance while the webhook lands.
    """
    balance = db.query(CreditBalance).filter(CreditBalance.user_id == user.id).first()
    return {"credits_remaining": balance.balance if balance else 0}


# ==========================================
# 2. ORG AI COURSE DRAFTING
# ==========================================

@router.get("/org-drafts/usage")
def get_org_draft_usage(
    id: UUID = Query(..., description="Organisation id"),
    db: Session = Depends(get_db),
    user = Depends(auth.get_current_user)
):
    org = db.query(models.Organisation).filter(
        models.Organisation.id == id,
        models.Organisation.owner_id == user.id
    ).first()
    if not org:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not admin of this organisation")

    config = db.query(PlanAIDraftingConfig).filter(PlanAIDraftingConfig.plan_id == org.plan_id).first() if org.plan_id else None
    usage = db.query(OrganisationAIDraftingUsage).filter(OrganisationAIDraftingUsage.org_id == org.id).first()

    return {
        "entitled": bool(config and config.entitled),
        "monthly_limit": config.monthly_draft_limit if config else None,
        "used_this_cycle": usage.drafts_used_this_cycle if usage else 0,
        "cycle_start_date": usage.cycle_start_date if usage else None,
    }


@router.get("/org-drafts/plans")
def list_drafting_plans(db: Session = Depends(get_db)):
    """Powers the org upgrade screen — plans that include AI drafting."""
    configs = db.query(PlanAIDraftingConfig).filter(PlanAIDraftingConfig.entitled == True).all()
    result = []
    for config in configs:
        plan = db.query(models.Plan).filter(models.Plan.id == config.plan_id).first()
        if plan:
            result.append({
                "plan_id": str(plan.id),
                "name": plan.name,
                "monthly_draft_limit": config.monthly_draft_limit,
            })
    return result


@router.get("/org-drafts/checkout-url")
def get_org_plan_checkout_url(
    id: UUID = Query(..., description="Organisation id"),
    plan_id: UUID = Query(..., description="Target Plan.id"),
    db: Session = Depends(get_db),
    user = Depends(auth.get_current_user)
):
    org = db.query(models.Organisation).filter(
        models.Organisation.id == id,
        models.Organisation.owner_id == user.id
    ).first()
    if not org:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not admin of this organisation")

    plan = db.query(models.Plan).filter(models.Plan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown plan")

    # app_user_id is still the requesting USER (RevenueCat's identity unit),
    # org_id is passed through so the webhook knows which Organisation to update.
    url = f"{RC_WEB_CHECKOUT_URL_ORG}?app_user_id={user.id}&org_id={org.id}&plan_id={plan.id}"
    return {"checkout_url": url, "plan": {"id": str(plan.id), "name": plan.name}}


@router.post("/org-drafts/refresh")
def refresh_org_plan(
    id: UUID = Query(..., description="Organisation id"),
    db: Session = Depends(get_db),
    user = Depends(auth.get_current_user)
):
    org = db.query(models.Organisation).filter(
        models.Organisation.id == id,
        models.Organisation.owner_id == user.id
    ).first()
    if not org:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not admin of this organisation")

    config = db.query(PlanAIDraftingConfig).filter(PlanAIDraftingConfig.plan_id == org.plan_id).first() if org.plan_id else None
    usage = db.query(OrganisationAIDraftingUsage).filter(OrganisationAIDraftingUsage.org_id == org.id).first()

    return {
        "plan_id": str(org.plan_id) if org.plan_id else None,
        "entitled": bool(config and config.entitled),
        "monthly_limit": config.monthly_draft_limit if config else None,
        "used_this_cycle": usage.drafts_used_this_cycle if usage else 0,
    }


@router.post("/org-drafts/request")
def request_org_draft(
    payload: schemas.OrgDraftRequest,
    db: Session = Depends(get_db),
    user = Depends(auth.get_current_user)
):
    """Gate check + ledger entry for AI course drafting, run before enqueuing the actual CourseDraftJob."""
    org = db.query(models.Organisation).filter(
        models.Organisation.id == payload.org_id,
        models.Organisation.owner_id == user.id
    ).first()
    if not org:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not admin of this organisation")

    if not org.plan_id:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={"error": "drafting_not_entitled", "message": "AI course drafting isn't included in your organisation's current plan. Upgrade to unlock it."}
        )

    config = db.query(PlanAIDraftingConfig).filter(PlanAIDraftingConfig.plan_id == org.plan_id).first()
    if not config or not config.entitled:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={"error": "drafting_not_entitled", "message": "AI course drafting isn't included in your organisation's current plan. Upgrade to unlock it."}
        )

    # Get-or-create usage row, with lazy cycle reset
    usage = db.query(OrganisationAIDraftingUsage).filter(OrganisationAIDraftingUsage.org_id == org.id).first()
    if not usage:
        usage = OrganisationAIDraftingUsage(org_id=org.id, drafts_used_this_cycle=0)
        db.add(usage)
        db.commit()
        db.refresh(usage)

    now = datetime.now(timezone.utc)
    cycle_start = usage.cycle_start_date if usage.cycle_start_date.tzinfo else usage.cycle_start_date.replace(tzinfo=timezone.utc)
    if now - cycle_start >= CYCLE_LENGTH:
        usage.drafts_used_this_cycle = 0
        usage.cycle_start_date = now
        db.commit()

    if config.monthly_draft_limit is not None and usage.drafts_used_this_cycle >= config.monthly_draft_limit:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error": "drafting_quota_exceeded",
                "used": usage.drafts_used_this_cycle,
                "limit": config.monthly_draft_limit,
                "message": f"Your organisation has used all {config.monthly_draft_limit} AI drafts this cycle.",
            }
        )

    # Charge the quota
    usage.drafts_used_this_cycle += 1

    ledger_entry = OrgDraftLedgerEntry(
        org_id=org.id,
        requested_by_user_id=user.id,
        topic=payload.topic,
        status=DraftStatus.PENDING,
    )
    db.add(ledger_entry)
    db.commit()
    db.refresh(ledger_entry)

    # Create the actual job using your existing CourseDraftJob model
    job = models.CourseDraftJob(
        user_id=str(user.id),
        topic=payload.topic,
        context=payload.context,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    ledger_entry.draft_job_id = job.id
    ledger_entry.status = DraftStatus.RUNNING
    db.commit()

    # TODO: enqueue job.id onto your actual background worker here,
    # same pattern as study.py's background_tasks.add_task(process_and_generate_content, ...)

    return {"job_id": job.id, "ledger_entry_id": str(ledger_entry.id)}


# ==========================================
# 3. REVENUECAT WEBHOOK
# ==========================================

def _verify_signature(signature_header):
    """
    RevenueCat sends the Authorization header value you configure yourself
    in the RevenueCat dashboard webhook settings — check current RC docs
    for the exact header/scheme at integration time.
    """
    if signature_header is None:
        return False
    expected = f"Bearer {REVENUECAT_WEBHOOK_SECRET}"
    return hmac.compare_digest(signature_header, expected)


@router.post("/webhooks/revenuecat")
async def revenuecat_webhook(request: Request, db: Session = Depends(get_db)):
    raw_body = await request.body()
    auth_header = request.headers.get("Authorization")

    if not _verify_signature(auth_header):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature")

    payload = json.loads(raw_body)
    event = payload.get("event", {})
    event_id = event.get("id")
    event_type = event.get("type")
    app_user_id = event.get("app_user_id")  # == str(User.id)
    product_id = event.get("product_id")
    transaction_id = event.get("transaction_id") or event.get("original_transaction_id")

    if not event_id or not app_user_id:
        print(f"Warning: RevenueCat webhook missing event_id or app_user_id: {payload}")
        return {"status": "ignored", "reason": "missing event_id or app_user_id"}

    # Global idempotency gate — check before processing anything
    existing = db.query(RevenueCatWebhookEvent).filter(RevenueCatWebhookEvent.event_id == event_id).first()
    if existing:
        return {"status": "already_processed"}

    db.add(RevenueCatWebhookEvent(
        event_id=event_id,
        event_type=event_type,
        app_user_id=app_user_id,
        raw_payload=raw_body.decode("utf-8"),
    ))
    db.commit()

    if event_type == "NON_RENEWING_PURCHASE":
        # Consumable credit pack purchase for a student
        already_granted = db.query(CreditPurchase).filter(CreditPurchase.rc_event_id == event_id).first()
        if not already_granted:
            pack = db.query(CreditPack).filter(CreditPack.id == product_id).first()
            if not pack:
                print(f"Warning: Unknown credit pack product_id in webhook: {product_id}")
            else:
                balance = db.query(CreditBalance).filter(CreditBalance.user_id == UUID(app_user_id)).first()
                if not balance:
                    balance = CreditBalance(user_id=UUID(app_user_id), balance=0)
                    db.add(balance)
                    db.commit()
                    db.refresh(balance)

                balance.balance += pack.credits
                db.add(CreditPurchase(
                    user_id=UUID(app_user_id),
                    rc_event_id=event_id,
                    rc_transaction_id=transaction_id,
                    product_id=product_id,
                    credits_granted=pack.credits,
                ))
                db.add(CreditLedgerEntry(
                    user_id=UUID(app_user_id),
                    delta=pack.credits,
                    reason="purchase",
                    reference_id=event_id,
                    balance_after=balance.balance,
                ))
                db.commit()

    elif event_type in ("INITIAL_PURCHASE", "RENEWAL", "PRODUCT_CHANGE"):
        # Org subscription purchase/renewal for a plan that includes AI drafting.
        # Hook into whatever already updates Organisation.plan_id / plan_expires_at
        # on a successful org subscription elsewhere in your codebase — this
        # branch is a placeholder so that logic isn't duplicated here.
        pass

    elif event_type in ("CANCELLATION", "EXPIRATION"):
        # Same note — reuse whatever downgrades Organisation.plan_id back to
        # a free/no-drafting plan today.
        pass

    return {"status": "ok"}


# ==========================================
# SNIPPET: wire the credits fallback into study.py's existing /generate
# ==========================================
#
# Replace this block in study.py's generate_study_content():
#
#     if sub.plan.generations_limit is not None and sub.generations_used >= sub.plan.generations_limit:
#         raise HTTPException(
#             status_code=403,
#             detail="You have reached your AI generation limit. Please upgrade your plan."
#         )
#     sub.generations_used += 1
#
# With:
#
#     from monetization_models import CreditBalance, CreditLedgerEntry
#
#     credit_source = "free_quota"
#     if sub.plan.generations_limit is not None and sub.generations_used >= sub.plan.generations_limit:
#         # Free quota exhausted — fall back to purchased credits
#         balance = db.query(CreditBalance).filter(CreditBalance.user_id == user.id).first()
#         if not balance or balance.balance <= 0:
#             raise HTTPException(
#                 status_code=402,
#                 detail={
#                     "error": "insufficient_generations",
#                     "message": "You're out of free generations and credits. Buy a credit pack to continue.",
#                 }
#             )
#         balance.balance -= 1
#         db.add(CreditLedgerEntry(
#             user_id=user.id, delta=-1, reason="generation_spend",
#             reference_id=str(payload.material_ids), balance_after=balance.balance,
#         ))
#         credit_source = "credits"
#     else:
#         sub.generations_used += 1
#
# (Keep everything else in the endpoint — the materials lookup, the
# is_generating lock, the refund-on-404-materials logic — exactly as is.
# On the 404-materials refund path, also refund credits if credit_source
# was "credits" instead of always doing sub.generations_used -= 1.)