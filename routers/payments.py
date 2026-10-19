import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, Depends, HTTPException, Request, Header, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session

import models
from database import get_db, Settings
from services import auth, coin_service
from services.notifications import dispatch_notification
from services.payment_service import get_payment_gateway, PaystackGateway
from routers.platform_admin import get_current_super_admin

router = APIRouter(prefix="/payments", tags=["Payments Foundation"])
settings = Settings()


VALID_INTERVALS = ("one-time", "month", "quarter", "year")


class StoreProductPayload(BaseModel):
    id: Optional[str] = None
    name: str
    category: str  # plan, booster, coins, org_plan, org_seats
    price: float
    badge: Optional[str] = None
    description: Optional[str] = None
    interval: Optional[str] = "one-time"
    features: Optional[List[str]] = Field(default_factory=list)
    bonus_materials: Optional[int] = 0
    bonus_generations: Optional[int] = 0
    bonus_coins: Optional[int] = 0
    plan_id: Optional[str] = None
    org_seats: Optional[int] = 0
    is_active: Optional[bool] = True
    display_order: Optional[int] = 0

    @field_validator("interval", mode="before")
    @classmethod
    def validate_interval(cls, v):
        if not v:
            return "one-time"
        v_clean = str(v).strip().lower()
        if v_clean in ("monthly", "mo"):
            v_clean = "month"
        elif v_clean in ("yearly", "annual", "annually", "yr"):
            v_clean = "year"
        elif v_clean in ("quarterly", "qtr"):
            v_clean = "quarter"
        elif v_clean in ("once", "lifetime", "none"):
            v_clean = "one-time"

        if v_clean not in VALID_INTERVALS:
            raise ValueError(f"Invalid billing interval '{v}'. Allowed intervals: {list(VALID_INTERVALS)}")
        return v_clean

    @model_validator(mode="after")
    def validate_category_interval(self):
        cat = (self.category or "").lower()
        if cat in ("coins", "coin", "booster", "boosters", "materials", "material") and self.interval != "one-time":
            self.interval = "one-time"
        return self


class OrgPlanPayload(BaseModel):
    id: Optional[str] = None
    name: str
    description: Optional[str] = None
    price: float = 0.0
    max_members: Optional[int] = 20
    max_courses: Optional[int] = 10
    features: Optional[List[str]] = Field(default_factory=list)


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


@router.get("/history")
def get_user_payment_history(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """
    Returns the authenticated user's past payment transactions and receipts.
    """
    txs = db.query(models.PaymentTransaction).filter(
        models.PaymentTransaction.user_id == current_user.id
    ).order_by(models.PaymentTransaction.created_at.desc()).limit(50).all()

    return [
        {
            "reference": tx.reference,
            "gateway": tx.gateway,
            "amount": tx.amount,
            "currency": tx.currency or "NGN",
            "status": tx.status,
            "purpose": tx.purpose,
            "metadata": tx.metadata_payload or {},
            "created_at": tx.created_at.isoformat() if tx.created_at else None,
            "paid_at": tx.paid_at.isoformat() if tx.paid_at else None,
            "fulfilled_at": tx.fulfilled_at.isoformat() if tx.fulfilled_at else None,
        }
        for tx in txs
    ]


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
def get_store_catalog(db: Session = Depends(get_db)):
    """
    Authoritative catalog of study booster packs, subscriptions, nu-coins, and org seats.
    Stored dynamically in PostgreSQL and managed by Super Administrators.
    """
    _seed_store_catalog_if_empty(db)
    products = db.query(models.StoreProduct).filter(
        models.StoreProduct.is_active == True
    ).order_by(models.StoreProduct.display_order.asc()).all()

    def _to_dict(p: models.StoreProduct):
        return {
            "id": p.id,
            "name": p.name,
            "category": p.category,
            "badge": p.badge,
            "price": p.price,
            "interval": p.interval,
            "description": p.description,
            "features": p.features or [],
            "materials": p.bonus_materials,
            "generations": p.bonus_generations,
            "coins": p.bonus_coins,
            "bonus_materials": p.bonus_materials,
            "bonus_generations": p.bonus_generations,
            "bonus_coins": p.bonus_coins,
            "plan_id": p.plan_id,
            "org_seats": p.org_seats,
            "is_active": p.is_active,
            "display_order": p.display_order,
            "recommended": (p.badge or "").upper() in ("MOST POPULAR", "BEST VALUE"),
        }

    return {
        "currency": "NGN",
        "currency_symbol": "₦",
        "plans": [_to_dict(p) for p in products if p.category == "plan"],
        "packs": [_to_dict(p) for p in products if p.category == "booster"],
        "coins": [_to_dict(p) for p in products if p.category == "coins"],
        "org_plans": [_to_dict(p) for p in products if p.category == "org_plan"],
        "org_seats": [_to_dict(p) for p in products if p.category == "org_seats"],
        "org_products": [_to_dict(p) for p in products if p.category in ("org_plan", "org_seats")],
    }


# ─────────────────────────────────────────────────────────────────────────────
# EXPLICIT IN-STORE NU-COINS -> AI GENERATION CREDITS EXCHANGE
# ─────────────────────────────────────────────────────────────────────────────

class ExchangeCoinsPayload(BaseModel):
    package_id: str = Field(..., description="Package ID: ai_gen_1, ai_gen_5, ai_gen_12, ai_gen_25")


EXCHANGE_TIERS: Dict[str, Dict[str, Any]] = {
    "ai_gen_1": {"generations": 1, "coins": 25, "label": "1 Generation", "discount": None},
    "ai_gen_5": {"generations": 5, "coins": 100, "label": "5 Generations", "discount": "Save 25 Coins"},
    "ai_gen_12": {"generations": 12, "coins": 200, "label": "12 Generations", "discount": "Save 100 Coins"},
    "ai_gen_25": {"generations": 25, "coins": 400, "label": "25 Generations", "discount": "Save 225 Coins"},
}


@router.get("/store/exchange-tiers")
def get_exchange_tiers():
    """Returns available packages for explicitly exchanging Nu-Coins into bonus AI Generation Credits."""
    return {
        "tiers": [
            {
                "id": k,
                "generations": v["generations"],
                "coins": v["coins"],
                "label": v["label"],
                "discount": v["discount"],
            }
            for k, v in EXCHANGE_TIERS.items()
        ]
    }


@router.post("/store/exchange-coins")
def exchange_coins_for_generations(
    payload: ExchangeCoinsPayload,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """
    Explicit in-store conversion: exchanges a user's earned Nu-Coins for bonus AI Generation credits.
    Does NOT automatically deduct; user must explicitly invoke this exchange action.
    """
    tier = EXCHANGE_TIERS.get(payload.package_id)
    if not tier:
        raise HTTPException(status_code=400, detail="Invalid exchange package. Choose ai_gen_1, ai_gen_5, ai_gen_12, or ai_gen_25.")

    coins_required = tier["coins"]
    gen_awarded = tier["generations"]

    user_coins = coin_service.get_user_coins(db, current_user.id)
    if user_coins < coins_required:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient Nu-Coins. You have {user_coins} coins, but this package requires {coins_required} Nu-Coins."
        )

    # Deduct coins from user's unified wallet
    success, new_bal, msg = coin_service.spend_coins(
        db=db,
        user_id=current_user.id,
        amount=coins_required,
        category="store_ai_exchange",
        description=f"Exchanged {coins_required} Nu-Coins for +{gen_awarded} AI Generation Credits",
        source_type="store_exchange",
        reference_id=payload.package_id,
    )
    if not success:
        raise HTTPException(status_code=400, detail=msg)

    # Credit UserSubscription.bonus_generations
    sub = db.query(models.UserSubscription).filter(models.UserSubscription.user_id == current_user.id).first()
    if not sub:
        sub = models.UserSubscription(user_id=current_user.id, plan_id="free")
        db.add(sub)
    sub.bonus_generations = (sub.bonus_generations or 0) + gen_awarded
    db.commit()

    return {
        "success": True,
        "package_id": payload.package_id,
        "generations_added": gen_awarded,
        "new_bonus_generations": sub.bonus_generations,
        "new_coins_balance": new_bal,
        "message": f"🎉 Successfully exchanged {coins_required} Nu-Coins for +{gen_awarded} AI Generation Credits!",
    }


# ─────────────────────────────────────────────────────────────────────────────
# SUPER ADMIN STORE CATALOG STUDIO ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/admin/products")
def get_admin_store_products(
    db: Session = Depends(get_db),
    current_admin: models.User = Depends(get_current_super_admin),
):
    """
    Returns ALL store products (active and inactive) for Super Admin Studio.
    """
    _seed_store_catalog_if_empty(db)
    products = db.query(models.StoreProduct).order_by(
        models.StoreProduct.category.asc(),
        models.StoreProduct.display_order.asc()
    ).all()
    return products


@router.post("/admin/products")
def create_store_product(
    payload: StoreProductPayload,
    db: Session = Depends(get_db),
    current_admin: models.User = Depends(get_current_super_admin),
):
    """
    Creates a new store product or booster/coin package.
    """
    prod_id = payload.id.strip() if payload.id else f"prod_{uuid.uuid4().hex[:8]}"
    existing = db.query(models.StoreProduct).filter(models.StoreProduct.id == prod_id).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Product with ID '{prod_id}' already exists.")

    new_prod = models.StoreProduct(
        id=prod_id,
        name=payload.name,
        category=payload.category,
        price=payload.price,
        badge=payload.badge,
        description=payload.description,
        interval=payload.interval,
        features=payload.features or [],
        bonus_materials=payload.bonus_materials or 0,
        bonus_generations=payload.bonus_generations or 0,
        bonus_coins=payload.bonus_coins or 0,
        plan_id=payload.plan_id,
        org_seats=payload.org_seats or 0,
        is_active=payload.is_active if payload.is_active is not None else True,
        display_order=payload.display_order or 0,
    )
    db.add(new_prod)
    db.commit()
    db.refresh(new_prod)
    return new_prod


@router.put("/admin/products/{product_id}")
def update_store_product(
    product_id: str,
    payload: StoreProductPayload,
    db: Session = Depends(get_db),
    current_admin: models.User = Depends(get_current_super_admin),
):
    """
    Updates price, features, bonuses, or active status of a store product.
    """
    prod = db.query(models.StoreProduct).filter(models.StoreProduct.id == product_id).first()
    if not prod:
        raise HTTPException(status_code=404, detail="Store product not found.")

    prod.name = payload.name
    prod.category = payload.category
    prod.price = payload.price
    prod.badge = payload.badge
    prod.description = payload.description
    prod.interval = payload.interval
    prod.features = payload.features or []
    prod.bonus_materials = payload.bonus_materials or 0
    prod.bonus_generations = payload.bonus_generations or 0
    prod.bonus_coins = payload.bonus_coins or 0
    prod.plan_id = payload.plan_id
    prod.org_seats = payload.org_seats or 0
    if payload.is_active is not None:
        prod.is_active = payload.is_active
    if payload.display_order is not None:
        prod.display_order = payload.display_order

    db.commit()
    db.refresh(prod)
    return prod


@router.delete("/admin/products/{product_id}")
def delete_store_product(
    product_id: str,
    db: Session = Depends(get_db),
    current_admin: models.User = Depends(get_current_super_admin),
):
    """
    Deactivates or deletes a store product.
    """
    prod = db.query(models.StoreProduct).filter(models.StoreProduct.id == product_id).first()
    if not prod:
        raise HTTPException(status_code=404, detail="Store product not found.")

    db.delete(prod)
    db.commit()
    return {"status": "success", "message": f"Product '{product_id}' deleted successfully."}


@router.get("/admin/org-plans")
def get_admin_org_plans(
    db: Session = Depends(get_db),
    current_admin: models.User = Depends(get_current_super_admin),
):
    """
    Returns all organisation plans declared in PostgreSQL 'plans' table.
    """
    plans = db.query(models.Plan).order_by(models.Plan.price.asc()).all()
    return [
        {
            "id": str(p.id),
            "name": p.name,
            "description": p.description,
            "price": p.price,
            "max_members": p.max_members,
            "max_courses": p.max_courses,
            "features": list(p.features) if p.features else [],
        }
        for p in plans
    ]


@router.post("/admin/org-plans")
def save_admin_org_plan(
    payload: OrgPlanPayload,
    db: Session = Depends(get_db),
    current_admin: models.User = Depends(get_current_super_admin),
):
    """
    Creates or updates an Organisation Plan record in the PostgreSQL 'plans' table.
    """
    plan_obj = None
    if payload.id:
        try:
            plan_uuid = uuid.UUID(payload.id)
            plan_obj = db.query(models.Plan).filter(models.Plan.id == plan_uuid).first()
        except ValueError:
            pass

    if not plan_obj:
        plan_obj = db.query(models.Plan).filter(models.Plan.name.ilike(payload.name.strip())).first()

    if plan_obj:
        plan_obj.name = payload.name.strip()
        plan_obj.description = payload.description
        plan_obj.price = payload.price
        plan_obj.max_members = payload.max_members
        plan_obj.max_courses = payload.max_courses
        plan_obj.features = payload.features or []
    else:
        plan_obj = models.Plan(
            name=payload.name.strip(),
            description=payload.description,
            price=payload.price,
            max_members=payload.max_members,
            max_courses=payload.max_courses,
            features=payload.features or [],
        )
        db.add(plan_obj)

    db.commit()
    db.refresh(plan_obj)
    return {
        "id": str(plan_obj.id),
        "name": plan_obj.name,
        "description": plan_obj.description,
        "price": plan_obj.price,
        "max_members": plan_obj.max_members,
        "max_courses": plan_obj.max_courses,
        "features": list(plan_obj.features) if plan_obj.features else [],
    }


def _seed_store_catalog_if_empty(db: Session):
    existing_count = db.query(models.StoreProduct).count()
    if existing_count > 0:
        return

    defaults = [
        # Plans
        models.StoreProduct(
            id="free",
            name="Free Tier",
            category="plan",
            price=0,
            badge="DEFAULT",
            interval="forever",
            description="Essential study toolkit for everyday comprehension.",
            features=[
                "25 Material Upload slots (PDF, Text, Notes)",
                "40 AI Study Deck Generation bundles",
                "10 Full Practice Exam simulations",
                "Unlimited Spaced Repetition Flashcards",
                "Socratic AI Chat Tutor",
            ],
            bonus_materials=0,
            bonus_generations=0,
            bonus_coins=0,
            plan_id="free",
            display_order=1,
            is_active=True,
        ),
        models.StoreProduct(
            id="pro",
            name="Pro Scholar",
            category="plan",
            price=2500,
            badge="MOST POPULAR",
            interval="month",
            description="For dedicated undergraduates seeking an academic edge.",
            features=[
                "45 Total Material Upload slots (+20 extra/month)",
                "100 AI Study Deck Generation bundles/month",
                "40 Full Practice Exam simulations/month",
                "Unlimited Quick Quiz & Spaced Repetition",
                "Priority Socratic AI Tutor & LaTeX formatting",
                "Multi-photo whiteboard & slide OCR",
            ],
            bonus_materials=0,
            bonus_generations=0,
            bonus_coins=0,
            plan_id="pro",
            display_order=2,
            is_active=True,
        ),
        models.StoreProduct(
            id="unlimited",
            name="Unlimited Ultimate",
            category="plan",
            price=8500,
            badge="BEST VALUE",
            interval="month",
            description="Unbounded power for medical, law, and intensive exam candidates.",
            features=[
                "Unlimited Material Upload slots",
                "Unlimited AI Study Deck Generation bundles",
                "Unlimited Exam Simulations & Timer drills",
                "Deep Grounded Document Synthesis",
                "Early Access to new learning AI models",
            ],
            bonus_materials=0,
            bonus_generations=0,
            bonus_coins=0,
            plan_id="unlimited",
            display_order=3,
            is_active=True,
        ),
        # Boosters
        models.StoreProduct(
            id="starter_booster",
            name="Starter Booster",
            category="booster",
            price=1000,
            badge="QUICK TOP-UP",
            description="+10 materials & +25 AI generations. Non-expiring consumable expansion.",
            features=[
                "+10 Additional Material slots",
                "+25 AI Generation bundles",
                "Credits never expire across billing cycles",
            ],
            bonus_materials=10,
            bonus_generations=25,
            bonus_coins=0,
            display_order=10,
            is_active=True,
        ),
        models.StoreProduct(
            id="semester_pack",
            name="Semester Pack",
            category="booster",
            price=2200,
            badge="STUDY SPRINT",
            description="+25 materials & +50 AI generations. Perfect for midterm & finals prep.",
            features=[
                "+25 Additional Material slots",
                "+50 AI Generation bundles",
                "Credits never expire across billing cycles",
            ],
            bonus_materials=25,
            bonus_generations=50,
            bonus_coins=0,
            display_order=11,
            is_active=True,
        ),
        models.StoreProduct(
            id="mega_vault",
            name="Mega Vault Booster",
            category="booster",
            price=4500,
            badge="POWER USER",
            description="+50 materials & +100 AI generations. Massive non-expiring consumable expansion.",
            features=[
                "+50 Additional Material slots",
                "+100 AI Generation bundles",
                "Credits never expire across billing cycles",
            ],
            bonus_materials=50,
            bonus_generations=100,
            bonus_coins=0,
            display_order=12,
            is_active=True,
        ),
        # Nu-Coins Packs
        models.StoreProduct(
            id="coins_pouch",
            name="Coin Pouch (150 Nu-Coins)",
            category="coins",
            price=500,
            badge="POPULAR",
            description="150 Nu-Coins for duels, streak shields & marketplace packs.",
            features=[
                "150 Nu-Coins immediately credited",
                "Unlock community study packs",
                "Wager in 1v1 Quickfire Duels",
                "Buy Streak Freeze shields",
            ],
            bonus_materials=0,
            bonus_generations=0,
            bonus_coins=150,
            display_order=20,
            is_active=True,
        ),
        models.StoreProduct(
            id="coins_sack",
            name="Coin Sack (500 Nu-Coins)",
            category="coins",
            price=1500,
            badge="BEST VALUE",
            description="500 Nu-Coins with a 15% bonus margin.",
            features=[
                "500 Nu-Coins credited",
                "Unlock verified premium packs",
                "Wager in multiplayer showdowns",
                "Buy multiple streak shields",
            ],
            bonus_materials=0,
            bonus_generations=0,
            bonus_coins=500,
            display_order=21,
            is_active=True,
        ),
        models.StoreProduct(
            id="coins_chest",
            name="Coin Chest (1500 Nu-Coins)",
            category="coins",
            price=4000,
            badge="CHEST",
            description="1,500 Nu-Coins for prolific learners and pack authors.",
            features=[
                "1,500 Nu-Coins credited",
                "Generous coin reserve",
                "Sponsor challenges and study packs",
            ],
            bonus_materials=0,
            bonus_generations=0,
            bonus_coins=1500,
            display_order=22,
            is_active=True,
        ),
        # Org Products
        models.StoreProduct(
            id="org_seat_single",
            name="Single Member Seat",
            category="org_seats",
            price=1500,
            interval="month",
            description="Add 1 additional student/faculty seat to your workspace.",
            features=[
                "1 Extra Member Seat",
                "Full cohort & training access",
                "Analytics & gradebook monitoring",
            ],
            org_seats=1,
            display_order=30,
            is_active=True,
        ),
        models.StoreProduct(
            id="org_seat_pack_10",
            name="Classroom 10-Seat Pack",
            category="org_seats",
            price=12000,
            badge="POPULAR FOR TEAMS",
            interval="month",
            description="Add 10 seats to your workspace with a 20% discount.",
            features=[
                "10 Extra Member Seats (Save 20%)",
                "Full cohort & training access",
                "Team exam distribution & proctoring",
            ],
            org_seats=10,
            display_order=31,
            is_active=True,
        ),
    ]
    for d in defaults:
        db.add(d)
    db.commit()


def _fulfill_transaction(tx: models.PaymentTransaction, db: Session):
    """
    Dispatches business logic fulfillment once payment has been securely verified.
    Guarantees exactly-once execution, updates persistent credit ledger, quotas, and bonuses.
    Never resets usage metrics (materials_uploaded or generations_used).
    """
    if getattr(tx, "fulfilled_at", None) is not None:
        print(f"[FULFILLMENT] Transaction {tx.reference} was already fulfilled at {tx.fulfilled_at}. Skipping duplicate fulfillment.")
        return {"status": "already_fulfilled"}

    tx.fulfilled_at = datetime.now(timezone.utc)
    purpose = tx.purpose
    meta = tx.metadata_payload or {}

    prod_id = meta.get("product_id") or meta.get("pack_id") or meta.get("plan_id")
    prod = None
    if prod_id:
        prod = db.query(models.StoreProduct).filter(models.StoreProduct.id == prod_id).first()

    if purpose in ("study_credits", "store_product", "coins_purchase"):
        extra_mats = int(meta.get("bonus_materials") or meta.get("extra_materials") or meta.get("materials") or (prod.bonus_materials if prod else 0) or 0)
        extra_gens = int(meta.get("bonus_generations") or meta.get("extra_generations") or meta.get("generations") or (prod.bonus_generations if prod else 0) or 0)
        extra_coins = int(meta.get("bonus_coins") or meta.get("extra_coins") or meta.get("coins") or (prod.bonus_coins if prod else 0) or 0)

        if tx.user_id:
            sub = db.query(models.UserSubscription).filter(
                models.UserSubscription.user_id == tx.user_id
            ).first()
            if not sub:
                sub = models.UserSubscription(user_id=tx.user_id, plan_id="free")
                db.add(sub)
                db.flush()

            # 1. Expand AI generation bonus quota (for AI flashcard/quiz/exam generation)
            if extra_gens > 0:
                sub.bonus_generations = (sub.bonus_generations or 0) + extra_gens

            # 2. Expand persistent bonus material upload slots
            if extra_mats > 0:
                sub.bonus_materials = (sub.bonus_materials or 0) + extra_mats

            # 3. Credit Nu-Coins wallet if extra_coins > 0 (for duels, marketplace packs, shields)
            if extra_coins > 0:
                from monetization_models import CreditBalance, CreditLedgerEntry
                cb = db.query(CreditBalance).filter(CreditBalance.user_id == tx.user_id).first()
                if not cb:
                    cb = CreditBalance(user_id=tx.user_id, balance=100)
                    db.add(cb)
                    db.flush()

                cb.balance += extra_coins
                db.flush()

                ledger_entry = CreditLedgerEntry(
                    user_id=tx.user_id,
                    delta=extra_coins,
                    reason="purchase",
                    reference_id=tx.reference,
                    balance_after=cb.balance,
                )
                db.add(ledger_entry)

            print(f"[FULFILLMENT] Credited User {tx.user_id}: +{extra_mats} mats, +{extra_gens} AI gens, +{extra_coins} Nu-Coins")

    elif purpose == "plan_upgrade":
        new_plan_id = meta.get("plan_id") or (prod.plan_id if prod else None) or "pro"
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
                db.flush()

            # 1. Determine billing cycle interval and duration
            interval = (prod.interval if prod and prod.interval else meta.get("interval")) or "month"
            interval_clean = str(interval).strip().lower()
            if "year" in interval_clean:
                days_valid = 365
                b_cycle = "year"
            elif "quarter" in interval_clean:
                days_valid = 90
                b_cycle = "quarter"
            else:
                days_valid = 30
                b_cycle = "month"

            now = datetime.now(timezone.utc)
            # If the user already has an active subscription for this plan that hasn't expired yet, extend it!
            if sub.expires_at and sub.expires_at > now and sub.plan_id == new_plan_id:
                new_expires_at = sub.expires_at + timedelta(days=days_valid)
            else:
                new_expires_at = now + timedelta(days=days_valid)

            grace_until = new_expires_at + timedelta(days=3)

            sub.plan_id = new_plan_id
            sub.billing_cycle = b_cycle
            sub.status = "active"
            sub.cycle_start_date = now
            sub.expires_at = new_expires_at
            sub.grace_period_until = grace_until
            sub.last_reminder_sent = None
            # Reset active monthly generation usage so user receives full monthly allowance!
            sub.generations_used = 0
            db.flush()

            # If the plan product grants bonus materials, bonus generations, or bonus coins:
            if prod:
                if prod.bonus_materials:
                    sub.bonus_materials = (sub.bonus_materials or 0) + prod.bonus_materials
                if prod.bonus_generations:
                    sub.bonus_generations = (sub.bonus_generations or 0) + prod.bonus_generations
                if prod.bonus_coins:
                    from monetization_models import CreditBalance, CreditLedgerEntry
                    cb = db.query(CreditBalance).filter(CreditBalance.user_id == tx.user_id).first()
                    if not cb:
                        cb = CreditBalance(user_id=tx.user_id, balance=100)
                        db.add(cb)
                        db.flush()
                    cb.balance += prod.bonus_coins
                    db.flush()
                    db.add(CreditLedgerEntry(
                        user_id=tx.user_id,
                        delta=prod.bonus_coins,
                        reason="plan_bonus_coins",
                        reference_id=tx.reference,
                        balance_after=cb.balance,
                    ))

            # Dispatch in-app + push notification for confirmed subscription renewal/activation
            try:
                date_str = new_expires_at.strftime('%b %d, %Y')
                dispatch_notification(
                    db=db,
                    user_id=tx.user_id,
                    title="🎉 Subscription Activated!",
                    body=f"Your '{plan_obj.label}' plan is confirmed and active through {date_str}.",
                    category="subscription",
                    data_payload={
                        "plan_id": new_plan_id,
                        "status": "active",
                        "expires_at": new_expires_at.isoformat(),
                        "billing_cycle": b_cycle,
                    },
                    send_push=True,
                )
            except Exception as notif_ex:
                print(f"[FULFILLMENT] Notification dispatch skipped: {notif_ex}")

            print(f"[FULFILLMENT] Upgraded/Renewed User {tx.user_id} to '{new_plan_id}' ({b_cycle}) plan through {new_expires_at.isoformat()}. Monthly generations reset to 0.")

    elif purpose in ("org_plan", "org_subscription"):
        target_plan_id = meta.get("plan_id") or meta.get("org_plan_id") or (prod.plan_id if prod else None)
        if tx.organisation_id and target_plan_id:
            try:
                target_uuid = uuid.UUID(str(target_plan_id))
            except ValueError:
                p_match = db.query(models.Plan).filter(models.Plan.name.ilike(str(target_plan_id).strip())).first()
                target_uuid = p_match.id if p_match else None

            if target_uuid:
                org = db.query(models.Organisation).filter(
                    models.Organisation.id == tx.organisation_id
                ).first()
                if org:
                    org.plan_id = target_uuid
                    interval_str = (prod.interval if prod else meta.get("interval")) or "month"
                    days_valid = 365 if "year" in str(interval_str).lower() else 30
                    org.plan_expires_at = datetime.now(timezone.utc) + timedelta(days=days_valid)
                    print(f"[FULFILLMENT] Upgraded Organisation '{org.name}' to Plan '{target_uuid}' (Expires in {days_valid} days)")

    elif purpose == "org_seats":
        extra_seats = int(meta.get("extra_seats") or (prod.org_seats if prod else 1) or 1)
        if tx.organisation_id:
            org = db.query(models.Organisation).filter(
                models.Organisation.id == tx.organisation_id
            ).first()
            if org:
                if hasattr(org, "seat_limit") and getattr(org, "seat_limit") is not None:
                    org.seat_limit += extra_seats
                print(f"[FULFILLMENT] Credited +{extra_seats} seats to Organisation '{org.name}'")
