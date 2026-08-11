from jose import jwt, JWTError # FIX 1: Import JWTError directly
from datetime import datetime, timedelta, timezone
from typing import Optional
import os
import secrets
import hashlib
from dotenv import load_dotenv
from database import get_db, Settings
from sqlalchemy.orm import Session
from fastapi import Depends, HTTPException, status
import models
from fastapi.security import OAuth2PasswordBearer

load_dotenv()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl='login')

ALGORITHM = Settings().ALGORITHM
key = Settings().KEY
# Convert to integer in case dotenv pulled it in as a string
expire = int(Settings().EXPIRE)

# NEW: refresh token lifetime, in days. Add REFRESH_EXPIRE_DAYS to your .env
# and to the Settings class in database.py (same pattern as ALGORITHM/KEY/EXPIRE).
# Reading via os.getenv here (not getattr(Settings(), ...)) because if
# Settings is a pydantic BaseSettings, a missing field raises at instantiation
# time rather than being catchable by getattr's default — os.getenv degrades
# safely to 30 days if the env var isn't set yet.
REFRESH_EXPIRE_DAYS = int(os.getenv("REFRESH_EXPIRE_DAYS", "30"))


def create_access_token(data: dict):
    to_encode = data.copy()

    # FIX 2: Explicitly state "minutes=" so it doesn't default to days!
    to_expire = datetime.now(timezone.utc) + timedelta(minutes=expire)

    # NEW: explicit "type" claim. Without this, nothing distinguishes an
    # access token from any other JWT this app might issue in future, and
    # nothing stops a refresh token (if it were ever a JWT) from being
    # replayed against endpoints that only check get_current_user.
    to_encode.update({"exp": to_expire, "type": "access"})
    encoded_jwt = jwt.encode(to_encode, key, algorithm=ALGORITHM)
    return {"access_token": encoded_jwt, "type": "Bearer"}


# ──────────────────────────────────────────────────────────────
# NEW: refresh tokens.
#
# These are opaque random strings, NOT JWTs. The server stores only a
# SHA-256 hash of each one (same principle as password hashing: if the DB
# leaks, raw tokens in plaintext are instant account takeover). Storing
# them server-side (rather than a long-lived JWT) also means individual
# sessions/devices can be revoked on demand — logout, password change,
# "log out everywhere", or automatic revocation on detected token reuse.
# ──────────────────────────────────────────────────────────────

def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def create_refresh_token(db: Session, user_id, device_label: Optional[str] = None) -> str:
    """
    Generates a new refresh token, stores its hash in the DB, and returns
    the RAW token to send to the client. The raw value is never persisted —
    only its hash — so this function is the only place that ever sees it
    in plaintext.
    """
    raw_token = secrets.token_urlsafe(48)  # ~64 chars, high entropy
    token_hash = _hash_token(raw_token)
    expires_at = datetime.now(timezone.utc) + timedelta(days=REFRESH_EXPIRE_DAYS)

    record = models.RefreshToken(
        user_id=user_id,
        token_hash=token_hash,
        expires_at=expires_at,
        device_label=device_label,
    )
    db.add(record)
    db.commit()

    return raw_token


def verify_and_rotate_refresh_token(db: Session, raw_token: str, device_label: Optional[str] = None):
    """
    Validates a refresh token and, if valid, ROTATES it: the old one is
    marked revoked and a brand new one is issued. Returns
    (user, new_raw_refresh_token) on success, or (None, None) on failure.

    Rotation matters: if a refresh token is ever stolen, the attacker and
    the legitimate user are now racing to use the same token. Whoever uses
    it first "wins" and gets a new valid token; the other's next attempt
    fails because their copy is already revoked. That failure is itself a
    detectable signal of compromise (see the reuse-detection block below).
    """
    token_hash = _hash_token(raw_token)
    record = db.query(models.RefreshToken).filter(
        models.RefreshToken.token_hash == token_hash
    ).first()

    if not record:
        return None, None  # unknown token

    now = datetime.now(timezone.utc)
    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if record.revoked_at is not None:
        # SECURITY: this token was already used/rotated (or revoked via
        # logout) once before, and someone is trying to use it AGAIN.
        # Under normal client behavior this should never happen — treat it
        # as a compromise signal and kill every active refresh token this
        # user has, forcing a fresh login on all devices.
        db.query(models.RefreshToken).filter(
            models.RefreshToken.user_id == record.user_id,
            models.RefreshToken.revoked_at.is_(None),
        ).update({"revoked_at": now})
        db.commit()
        return None, None

    if now > expires_at:
        return None, None  # genuinely expired, nothing suspicious

    # Valid — rotate. Mark old one revoked, issue a new one.
    record.revoked_at = now
    db.commit()

    user = db.query(models.User).filter(models.User.id == record.user_id).first()
    if not user:
        return None, None

    new_raw_token = create_refresh_token(db, user.id, device_label=device_label)
    return user, new_raw_token


def revoke_refresh_token(db: Session, raw_token: str):
    """Used on logout — kills this one session/device."""
    token_hash = _hash_token(raw_token)
    record = db.query(models.RefreshToken).filter(
        models.RefreshToken.token_hash == token_hash
    ).first()
    if record and record.revoked_at is None:
        record.revoked_at = datetime.now(timezone.utc)
        db.commit()


def verify_access_token(token: str, credentials_exception):
    try:
        # FIX 3: python-jose expects algorithms as a list
        payload = jwt.decode(token, key=key, algorithms=[ALGORITHM])

        # NEW: reject anything that isn't explicitly an access token. Any
        # access token issued before this change lacks a "type" claim and
        # will fail here — self-healing within one EXPIRE window since
        # these tokens are short-lived, so no migration needed.
        if payload.get("type") != "access":
            raise credentials_exception

        # .get() prevents a loud KeyError crash if "email" is missing
        user_email = payload.get("email")
        if not user_email:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    return user_email

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    # FIX 4: This MUST be an HTTPException. 
    # Your previous code used a base Python 'Exception', which causes a 
    # 500 Internal Server Crash if a token is fake or expired.
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    email = verify_access_token(token, credentials_exception)
    user = db.query(models.User).filter(models.User.email == email).first()
    
    if not user:
        # If they somehow have a token but were deleted from the DB
        raise credentials_exception 
        
    return user
# Add this to the bottom of auth.py

def verify_ws_token(token: str, db: Session):
    """
    Specialized token verifier for WebSockets.
    Instead of raising an HTTP 401 Exception (which crashes WebSockets),
    this safely returns None so the endpoint can close the connection cleanly.
    """
    try:
        # Decode the token using the exact same logic as your HTTP routes
        payload = jwt.decode(token, key=key, algorithms=[ALGORITHM])

        # NEW: same type check as verify_access_token, for consistency.
        if payload.get("type") != "access":
            return None

        user_email = payload.get("email") 
        
        if not user_email:
            return None
            
        # Fetch the user from the database
        user = db.query(models.User).filter(models.User.email == user_email).first()
        return user
        
    except JWTError:
        # Token is expired, forged, or completely invalid
        return None