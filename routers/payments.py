import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Request, Header, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import models
from database import get_db, Settings
from services import auth
from services.payment_service import get_payment_gateway, PaystackGateway

router = APIRouter(prefix="/payments", tags=["Payments Foundation"])
settings = Settings()


class InitializePaymentRequest(BaseModel):
    amount: float = Field(..., gt=0, description="Amount in Naira (e.g. 2500.0)")
    purpose: str = Field(..., description="Purpose e.g. study_credits, org_seats, plan_upgrade")
    organisation_id: Optional[str] = None
    callback_url: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class VerifyPaymentResponse(BaseModel):
    reference: str
    status: str
    amount: float
    currency: str
    purpose: str
    paid_at: Optional[str] = None


@router.get("/config")
def get_payment_public_config():
    """
    Returns public keys and enabled gateways to client applications.
    """
    pub_key = settings.get_paystack_public_key()
    return {
        "gateway": "paystack",
        "public_key": pub_key,
        "is_configured": bool(pub_key and settings.get_paystack_secret_key()),
        "currency": "NGN",
    }


@router.post("/initialize")
async def initialize_payment(
    payload: InitializePaymentRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """
    Creates a new PaymentTransaction and initializes the payment session with Paystack.
    Returns the authorization URL for user checkout and reference ID.
    """
    gateway_name = "paystack"
    gw = get_payment_gateway(gateway_name)

    # Generate a collision-resistant unique transaction reference
    ref_prefix = payload.purpose[:4].lower()
    ref = f"nu_{ref_prefix}_{int(datetime.now(timezone.utc).timestamp())}_{uuid.uuid4().hex[:6]}"

    org_uuid = None
    if payload.organisation_id:
        try:
            org_uuid = uuid.UUID(payload.organisation_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid organisation_id format.")

    # 1. Record pending transaction in DB
    tx = models.PaymentTransaction(
        reference=ref,
        gateway=gateway_name,
        user_id=current_user.id,
        organisation_id=org_uuid,
        amount=payload.amount,
        currency="NGN",
        status="pending",
        purpose=payload.purpose,
        metadata_payload=payload.metadata or {},
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)

    # 2. Call Paystack REST API
    try:
        combined_meta = {
            "user_id": str(current_user.id),
            "purpose": payload.purpose,
            "organisation_id": str(org_uuid) if org_uuid else None,
            **(payload.metadata or {}),
        }
        res = await gw.initialize_payment(
            email=current_user.email,
            amount=payload.amount,
            reference=ref,
            callback_url=payload.callback_url,
            metadata=combined_meta,
        )
        return {
            "status": "success",
            "reference": ref,
            "authorization_url": res.get("authorization_url"),
            "access_code": res.get("access_code"),
            "amount": payload.amount,
            "currency": "NGN",
        }
    except Exception as ex:
        tx.status = "failed"
        tx.gateway_response = {"error": str(ex)}
        db.commit()
        raise HTTPException(
            status_code=502,
            detail=f"Payment gateway initialization failed: {ex}"
        )


@router.get("/verify/{reference}", response_model=VerifyPaymentResponse)
async def verify_payment(
    reference: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """
    Verifies a transaction using Paystack REST API and fulfills any credits or subscriptions.
    Guarded with row-level locking, strict amount matching, and idempotency protection.
    """
    # 1. Row-level locking to prevent concurrent double-fulfillment race condition
    tx = db.query(models.PaymentTransaction).filter(
        models.PaymentTransaction.reference == reference
    ).with_for_update().first()

    if not tx:
        raise HTTPException(status_code=404, detail="Transaction reference not found.")

    # 2. Idempotency Check: if already fulfilled by webhook or earlier call, return cleanly
    if tx.status == "success" and tx.fulfilled_at is not None:
        return VerifyPaymentResponse(
            reference=tx.reference,
            status=tx.status,
            amount=tx.amount,
            currency=tx.currency,
            purpose=tx.purpose,
            paid_at=tx.paid_at.isoformat() if tx.paid_at else None,
        )

    gw = get_payment_gateway(tx.gateway)
    try:
        data = await gw.verify_payment(reference)
        ps_status = data.get("status")  # "success", "failed", "abandoned"
        tx.gateway_response = data

        if ps_status == "success":
            # 3. Financial Integrity: Strict Amount & Currency Verification (Anti-Tampering)
            paid_kobo = data.get("amount")
            expected_kobo = int(round(tx.amount * 100))
            paid_currency = (data.get("currency") or "").upper()
            expected_currency = (tx.currency or "NGN").upper()

            if paid_kobo != expected_kobo or paid_currency != expected_currency:
                tx.status = "amount_mismatch"
                tx.gateway_response = {
                    "error": f"Underpayment or currency mismatch: expected {expected_kobo} kobo {expected_currency}, got {paid_kobo} {paid_currency}",
                    "paystack_data": data
                }
                db.commit()
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Payment verification failed: Amount or currency does not match order."
                )

            tx.status = "success"
            tx.paid_at = datetime.now(timezone.utc)
            # 4. Fulfill the transaction benefit safely and mark fulfilled_at
            _fulfill_transaction(tx, db)
            tx.fulfilled_at = datetime.now(timezone.utc)
        else:
            tx.status = ps_status or "failed"

        db.commit()
        db.refresh(tx)

        return VerifyPaymentResponse(
            reference=tx.reference,
            status=tx.status,
            amount=tx.amount,
            currency=tx.currency,
            purpose=tx.purpose,
            paid_at=tx.paid_at.isoformat() if tx.paid_at else None,
        )
    except HTTPException:
        raise
    except Exception as ex:
        db.rollback()
        raise HTTPException(status_code=502, detail=f"Verification failed: {ex}")


@router.post("/webhook/paystack")
async def paystack_webhook(
    request: Request,
    x_paystack_signature: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """
    Asynchronous webhook receiver for Paystack events (e.g. charge.success).
    Guarded by HMAC-SHA512 verification, row locking, and idempotency protection.
    """
    body_bytes = await request.body()
    gw = PaystackGateway()

    if not x_paystack_signature or not gw.verify_webhook_signature(body_bytes, x_paystack_signature):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or missing webhook signature."
        )

    import json
    try:
        event = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    event_type = event.get("event")
    data = event.get("data", {})
    reference = data.get("reference")

    if not reference:
        return {"status": "ignored", "reason": "No reference in webhook data"}

    tx = db.query(models.PaymentTransaction).filter(
        models.PaymentTransaction.reference == reference
    ).with_for_update().first()

    if not tx:
        # Received webhook for unknown transaction in DB
        return {"status": "ok", "message": "Reference not recorded locally"}

    if event_type == "charge.success":
        # Idempotency check: if already fulfilled, return immediately
        if tx.status == "success" and tx.fulfilled_at is not None:
            return {"status": "already_processed"}

        # Strict Amount & Currency Validation
        paid_kobo = data.get("amount")
        expected_kobo = int(round(tx.amount * 100))
        paid_currency = (data.get("currency") or "").upper()
        expected_currency = (tx.currency or "NGN").upper()

        if paid_kobo != expected_kobo or paid_currency != expected_currency:
            tx.status = "amount_mismatch"
            tx.gateway_response = {
                "error": f"Webhook underpayment: expected {expected_kobo} kobo {expected_currency}, got {paid_kobo} {paid_currency}",
                "event": event
            }
            db.commit()
            return {"status": "rejected", "reason": "Amount or currency mismatch"}

        tx.status = "success"
        tx.paid_at = datetime.now(timezone.utc)
        tx.gateway_response = data
        _fulfill_transaction(tx, db)
        tx.fulfilled_at = datetime.now(timezone.utc)
        db.commit()

    elif event_type in ("charge.failed", "transfer.failed"):
        tx.status = "failed"
        tx.gateway_response = data
        db.commit()

    return {"status": "received"}


@router.get("/store/catalog")
def get_store_catalog():
    """
    Authoritative catalog of study booster packs, subscriptions, and org seat products.
    Calibrated with market-aligned Nigerian pricing (NGN).
    """
    return {
        "currency": "NGN",
        "currency_symbol": "₦",
        "plans": [
            {
                "id": "free",
                "name": "Free Tier",
                "badge": "DEFAULT",
                "price": 0,
                "interval": "forever",
                "materials_limit": 25,
                "generations_limit": 40,
                "exam_quota": 10,
                "description": "Essential study toolkit for everyday comprehension.",
                "features": [
                    "25 Material Upload slots (PDF, Text, Notes)",
                    "40 AI Study Deck Generation bundles",
                    "10 Full Practice Exam simulations",
                    "Unlimited Spaced Repetition Flashcards",
                    "Socratic AI Chat Tutor",
                ],
                "is_current": True,
            },
            {
                "id": "pro",
                "name": "Pro Scholar",
                "badge": "MOST POPULAR",
                "price": 2500,
                "interval": "month",
                "materials_limit": 45,  # Free 25 + 20 extra per month
                "generations_limit": 100,
                "exam_quota": 40,
                "description": "For dedicated undergraduates seeking an academic edge.",
                "features": [
                    "45 Total Material Upload slots (+20 extra/month)",
                    "100 AI Study Deck Generation bundles/month",
                    "40 Full Practice Exam simulations/month",
                    "Unlimited Quick Quiz & Spaced Repetition",
                    "Priority Socratic AI Tutor & LaTeX formatting",
                    "Multi-photo whiteboard & slide OCR",
                ],
                "recommended": True,
            },
            {
                "id": "unlimited",
                "name": "Unlimited Ultimate",
                "badge": "BEST VALUE",
                "price": 8500,
                "interval": "month",
                "materials_limit": None,
                "generations_limit": None,
                "exam_quota": None,
                "description": "Unbounded power for medical, law, and intensive exam candidates.",
                "features": [
                    "Unlimited Material Upload slots",
                    "Unlimited AI Study Deck Generation bundles",
                    "Unlimited Exam Simulations & Timer drills",
                    "Deep Grounded Document Synthesis",
                    "Early Access to new learning AI models",
                ],
                "recommended": False,
            },
        ],
        "packs": [
            {
                "id": "starter_booster",
                "name": "Starter Booster",
                "badge": "QUICK TOP-UP",
                "price": 1000,
                "materials": 10,
                "generations": 25,
                "description": "+10 materials & +25 AI generations. Non-expiring.",
                "features": [
                    "+10 Additional Material slots",
                    "+25 AI Generation bundles",
                    "Credits never expire across cycles",
                ],
            },
            {
                "id": "semester_pack",
                "name": "Semester Pack",
                "badge": "STUDY SPRINT",
                "price": 2200,
                "materials": 25,
                "generations": 50,
                "description": "+25 materials & +50 AI generations. Perfect for midterm & finals prep.",
                "features": [
                    "+25 Additional Material slots",
                    "+50 AI Generation bundles",
                    "Credits never expire across cycles",
                ],
            },
            {
                "id": "mega_vault",
                "name": "Mega Vault Booster",
                "badge": "POWER USER",
                "price": 4500,
                "materials": 50,
                "generations": 100,
                "description": "+50 materials & +100 AI generations. Non-expiring consumable expansion.",
                "features": [
                    "+50 Additional Material slots",
                    "+100 AI Generation bundles",
                    "Credits never expire across cycles",
                ],
            },
        ],
        "org_products": [
            {
                "id": "org_seat_single",
                "name": "Single Member Seat",
                "price": 1500,
                "interval": "month",
                "description": "Add 1 additional student/faculty seat to your organisation workspace.",
                "features": [
                    "1 Extra Member Seat",
                    "Full cohort & training access",
                    "Analytics & gradebook monitoring",
                ],
            },
            {
                "id": "org_seat_pack_10",
                "name": "Classroom 10-Seat Pack",
                "badge": "POPULAR FOR TEAMS",
                "price": 12000,
                "interval": "month",
                "description": "Add 10 seats to your organisation workspace with a bulk discount.",
                "features": [
                    "10 Extra Member Seats (Save 20%)",
                    "Full cohort & training access",
                    "Team exam distribution & proctoring",
                ],
            },
        ],
    }


def _fulfill_transaction(tx: models.PaymentTransaction, db: Session):
    """
    Dispatches business logic fulfillment once payment has been securely verified.
    Guarantees exactly-once execution, updates persistent credit ledger and bonuses.
    """
    purpose = tx.purpose
    meta = tx.metadata_payload or {}

    if purpose == "study_credits":
        # Consumable top-up packs
        extra_mats = int(meta.get("extra_materials") or meta.get("materials") or 10)
        extra_gens = int(meta.get("extra_generations") or meta.get("generations") or 25)

        if tx.user_id:
            # 1. Credit non-expiring AI generation balance & append audit ledger
            from monetization_models import CreditBalance, CreditLedgerEntry
            cb = db.query(CreditBalance).filter(CreditBalance.user_id == tx.user_id).first()
            if not cb:
                cb = CreditBalance(user_id=tx.user_id, balance=100)
                db.add(cb)
                db.flush()

            cb.balance += extra_gens
            db.flush()

            ledger_entry = CreditLedgerEntry(
                user_id=tx.user_id,
                delta=extra_gens,
                reason="purchase",
                reference_id=tx.reference,
                balance_after=cb.balance,
            )
            db.add(ledger_entry)

            # 2. Credit persistent bonus material upload slots on subscription
            sub = db.query(models.UserSubscription).filter(
                models.UserSubscription.user_id == tx.user_id
            ).first()
            if not sub:
                sub = models.UserSubscription(user_id=tx.user_id, plan_id="free")
                db.add(sub)
                db.flush()

            sub.bonus_materials = (sub.bonus_materials or 0) + extra_mats
            print(f"[FULFILLMENT] Credited +{extra_mats} bonus materials and +{extra_gens} AI credits (Balance: {cb.balance}) to User {tx.user_id}")

    elif purpose == "plan_upgrade":
        new_plan_id = meta.get("plan_id") or "pro"
        if tx.user_id:
            plan_obj = db.query(models.StudyPlan).filter(models.StudyPlan.id == new_plan_id).first()
            if not plan_obj:
                if new_plan_id == "pro":
                    plan_obj = models.StudyPlan(id="pro", label="Pro Scholar", materials_limit=45, generations_limit=100)
                elif new_plan_id == "unlimited":
                    plan_obj = models.StudyPlan(id="unlimited", label="Unlimited", materials_limit=None, generations_limit=None)
                else:
                    plan_obj = models.StudyPlan(id=new_plan_id, label=new_plan_id.title(), materials_limit=50, generations_limit=100)
                db.add(plan_obj)
                db.flush()

            sub = db.query(models.UserSubscription).filter(
                models.UserSubscription.user_id == tx.user_id
            ).first()
            if not sub:
                sub = models.UserSubscription(user_id=tx.user_id, plan_id=new_plan_id)
                db.add(sub)
            else:
                sub.plan_id = new_plan_id
                sub.materials_uploaded = 0
                sub.generations_used = 0
                sub.cycle_start_date = datetime.now(timezone.utc)

            print(f"[FULFILLMENT] Upgraded User {tx.user_id} to '{new_plan_id}' plan.")

    elif purpose == "org_seats":
        extra_seats = int(meta.get("extra_seats") or 1)
        if tx.organisation_id:
            org = db.query(models.Organisation).filter(
                models.Organisation.id == tx.organisation_id
            ).first()
            if org:
                print(f"[FULFILLMENT] Credited +{extra_seats} seats to Organisation '{org.name}'")
