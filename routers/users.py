from fastapi import *
from fastapi.security.oauth2 import OAuth2PasswordRequestForm
from schemas import *
from database import get_db,Settings
from sqlalchemy import or_, func
from sqlalchemy.orm import Session
import models
from services import utils, auth
from typing import List
from datetime import datetime, timezone, timedelta
import random
from services.email_service import send_email
import pytz
import base64
import uuid
from services.bunny_service import upload_bytes_to_bunny

class UserDirectorySchema(BaseModel):
    id: UUID
    name: str
    email: str
    profile_picture_url: Optional[str] = None

class AvatarUploadSchema(BaseModel):
    image_bytes: str
    image_filename: str = "avatar.jpg"

# NEW: request bodies for the refresh-token endpoints below.
class RefreshRequest(BaseModel):
    refresh_token: str
    device_label: str | None = None

class LogoutRequest(BaseModel):
    refresh_token: str

router = APIRouter(prefix=('/users'))

# create a user
# Move the email sending logic into a separate background function
def send_background_otp(email: str, code: str):
    send_email(
        email,
        "Verify your Nu Age Account",
        f"Thank you for signing up! Your OTP code is: <strong>{code}</strong>. Please note this code expires in 15 minutes. Not you? You can ignore this email.",
    )

def send_password_reset_otp(email: str, code: str):
    send_email(
        email,
        "Reset your Nu Age Password",
        f"You have requested to reset your Nu Age password. Your OTP code is: <strong>{code}</strong>. Please note this code expires in 15 minutes. Not you? You can ignore this email.",
    )

class DeviceTokenSchema(BaseModel):
    token: str
    device_type: str = "web" # default

@router.post("/device-token")
@router.post("/users/device-token")
async def register_device_token(
    payload: DeviceTokenSchema, 
    current_user= Depends(auth.get_current_user), 
    db: Session = Depends(get_db)
):
    # Check if this exact token already exists
    existing_token = db.query(models.DeviceToken).filter(models.DeviceToken.token == payload.token).first()
    
    if existing_token:
        # If it exists but belongs to someone else (e.g., a friend logged into their account on this phone), reassign it.
        if existing_token.user_id != current_user.id:
            existing_token.user_id = current_user.id
            db.commit()
        return {"message": "Token registered."}
    
    # Create new token record
    new_token = models.DeviceToken(
        user_id=current_user.id,
        token=payload.token,
        device_type=payload.device_type
    )
    db.add(new_token)
    db.commit()
    
    return {"message": "Device token saved successfully."}

from services.notifications import send_push_notification

@router.get("/test-firebase")
async def test_firebase_connection():
    # We are intentionally passing a fake token to see how Google responds
    fake_token = "this_is_a_fake_device_token_12345"
    
    # Construct the message manually just for this test
    from firebase_admin import messaging
    message = messaging.MulticastMessage(
        notification=messaging.Notification(
            title="Test from Nu-age",
            body="If you see this, the code is running.",
        ),
        tokens=[fake_token],
    )

    try:
        response = messaging.send_each_for_multicast(message)
        
        # We want to see what Google says about our fake token
        for resp in response.responses:
            if not resp.success:
                print(f"FIREBASE RESPONSE: {resp.exception.code}")
                
        return {"status": "Test executed. Check your terminal logs."}
    except Exception as e:
        return {"error": str(e)}
    
@router.post('/auth/reset-password')
async def reset_password(
    email: str,
    background_tasks: BackgroundTasks, # Inject background tasks here
    db: Session = Depends(get_db)
):
    user = db.query(models.User).filter(models.User.email == email).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # Generate the OTP
    code = str(random.randint(100000, 999999))
    expires = datetime.now(timezone.utc) + timedelta(minutes=15)

    # Save OTP to the database
    otp_record = db.query(models.PasswordResetOTP).filter(models.PasswordResetOTP.email == email).first()
    if otp_record:
        otp_record.code = code
        otp_record.expires_at = expires
    else:
        db.add(models.PasswordResetOTP(email=email, code=code, expires_at=expires))

    db.commit()

    # Send the password reset OTP via email
    background_tasks.add_task(send_password_reset_otp, email, code)

    return {"message": "Password reset OTP sent."}

@router.post('/auth/verify-password')
async def verify_password(
    email: str,
    new_password: str,
    otp: str,
    db: Session = Depends(get_db)
):
    user = db.query(models.User).filter(models.User.email == email).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    otp_record = db.query(models.PasswordResetOTP).filter(models.PasswordResetOTP.email == email).first()
    
    if not otp_record or otp_record.code != otp:
        raise HTTPException(status_code=400, detail="Invalid verification code.")

    # 3. Check if it is expired
    now_utc = datetime.now(timezone.utc)
    expires_at = otp_record.expires_at

    # Safely force the database time to be timezone-aware if it isn't already
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if now_utc > expires_at:
        raise HTTPException(status_code=400, detail="Code expired. Please request a new one.")

    # 3. Destroy the OTP so it can't be reused
    db.delete(otp_record)
    user.password = utils.hash_password(new_password)
    db.commit()
    db.refresh(user)

    return {"message": "Password reset OTP sent."}

@router.post('/auth/register', response_model=UserBase)
async def register_user(
    user: UserReg, 
    background_tasks: BackgroundTasks, # Inject background tasks here
    db: Session = Depends(get_db)
):
    if user.role == "Teacher":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="You can't Signup as an instructor!")
    # Check for existing email safely
    # 1. Check if the username is taken by ANYONE ELSE
    existing_username = db.query(models.User).filter(models.User.username == user.username).first()
    
    if existing_username:
        # If the username exists, but the email is different, it belongs to someone else. Block it.
        # If the email is the same, we ignore the error because it's their own ghost account.
        if existing_username.email != user.email:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, 
                detail="Username already exists, choose another one"
            )

    # 2. Check the email status
    existing_user = db.query(models.User).filter(models.User.email == user.email).first()
    
    if existing_user:
        if getattr(existing_user, 'is_verified', False): 
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, 
                detail="Email is associated with another verified account"
            )
        else:
            # It's their own unverified ghost account. Overwrite it.
            user_to_save = existing_user
            user_to_save.password = utils.hash_password(user.password)
            user_to_save.username = user.username
            # Update any other fields (first_name, last_name, etc.) here
    else:
        # 3. Brand new user (Email doesn't exist, and username is free)
        user.password = utils.hash_password(user.password)
        user_to_save = models.User(**user.model_dump())
        user_to_save.is_verified = False 
        db.add(user_to_save)
    
    db.commit()

    # Generate the OTP
    code = str(random.randint(100000, 999999))
    expires = datetime.now(timezone.utc) + timedelta(minutes=15)

    # Save OTP to the database
    otp_record = db.query(models.SignupOTP).filter(models.SignupOTP.email == user.email).first()
    if otp_record:
        otp_record.code = code
        otp_record.expires_at = expires
    else:
        db.add(models.SignupOTP(email=user.email, code=code, expires_at=expires))
    
    db.commit()
    db.refresh(user_to_save)

    # Send the email in the background so the frontend doesn't hang
    background_tasks.add_task(send_background_otp, user.email, code)

    return user_to_save

def send_general_welcome_email(email: str, first_name: str = "there"):
    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1.0" />
      <meta http-equiv="X-UA-Compatible" content="IE=edge" />
      <title>Welcome to Nu Age</title>
      <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body, table, td, a {{ -webkit-text-size-adjust: 100%; -ms-text-size-adjust: 100%; }}
        table, td {{ mso-table-lspace: 0pt; mso-table-rspace: 0pt; }}
        img {{ -ms-interpolation-mode: bicubic; border: 0; outline: none; text-decoration: none; }}
        body {{ background-color: #F2F7F1; font-family: Georgia, 'Times New Roman', serif; }}

        .email-wrapper {{ background-color: #F2F7F1; padding: 40px 16px; }}

        .email-card {{
          background-color: #ffffff;
          max-width: 620px;
          margin: 0 auto;
          border-radius: 12px;
          overflow: hidden;
          box-shadow: 0 4px 28px rgba(55,191,19,0.10), 0 1px 6px rgba(0,0,0,0.06);
        }}

        /* BANNER */
        .banner {{
          width: 100%;
          height: 140px;
          overflow: hidden;
          display: block;
        }}
        .banner img {{
          width: 100%;
          max-width: 620px;
          height: 140px;
          object-fit: cover;
          object-position: center center;
          display: block;
        }}

        /* HEADER STRIP */
        .header-strip {{
          background-color: #37BF13;
          padding: 14px 40px;
          text-align: center;
        }}
        .header-strip p {{
          color: #ffffff;
          font-family: 'Courier New', Courier, monospace;
          font-size: 11px;
          letter-spacing: 3px;
          text-transform: uppercase;
          opacity: 0.92;
        }}

        /* BODY */
        .email-body {{ padding: 40px 44px 36px; }}

        .greeting {{
          font-family: Georgia, serif;
          font-size: 24px;
          font-weight: normal;
          color: #111A0F;
          margin-bottom: 6px;
          line-height: 1.3;
        }}
        .greeting span {{ color: #37BF13; }}

        .divider {{
          width: 48px;
          height: 3px;
          background-color: #37BF13;
          margin: 16px 0 24px;
          border-radius: 2px;
        }}

        .body-text {{
          font-family: Georgia, serif;
          font-size: 15px;
          color: #374151;
          line-height: 1.8;
          margin-bottom: 16px;
        }}

        /* MAIN BUTTON */
        .btn-wrap {{ text-align: center; margin: 32px 0; }}
        .btn-primary {{
          display: inline-block;
          background-color: #37BF13;
          color: #ffffff !important;
          text-decoration: none;
          font-family: Georgia, serif;
          font-size: 16px;
          font-weight: bold;
          padding: 14px 36px;
          border-radius: 8px;
          letter-spacing: 0.5px;
          box-shadow: 0 4px 12px rgba(55,191,19,0.2);
        }}

        /* SIGNATURE */
        .signature {{
          margin-top: 32px;
          padding-top: 24px;
          border-top: 1px solid #E3EEE1;
        }}
        .signature .sign-name {{
          font-family: Georgia, serif;
          font-size: 16px;
          color: #111A0F;
          font-weight: bold;
        }}
        .signature .sign-role {{
          font-family: 'Courier New', Courier, monospace;
          font-size: 11px;
          letter-spacing: 1.5px;
          text-transform: uppercase;
          color: #37BF13;
          margin-top: 4px;
        }}

        /* FOOTER */
        .email-footer {{
          background-color: #111A0F;
          padding: 20px 44px;
          text-align: center;
        }}
        .email-footer p {{
          font-family: 'Courier New', Courier, monospace;
          font-size: 11px;
          color: #5A7A56;
          letter-spacing: 1px;
          line-height: 1.7;
        }}
        .email-footer a {{
          color: #37BF13;
          text-decoration: none;
        }}

        /* MOBILE */
        @media only screen and (max-width: 480px) {{
          .email-body {{ padding: 28px 24px 24px; }}
          .email-footer {{ padding: 16px 24px; }}
          .header-strip {{ padding: 12px 24px; }}
          .greeting {{ font-size: 20px; }}
        }}
      </style>
    </head>
    <body>
      <div class="email-wrapper">
        <div class="email-card">

          <div class="banner">
            <img src="https://nu-age-cdn.b-cdn.net/logos/Nu%20logo%20only.jpeg" alt="Nu-age Banner" />
          </div>

          <div class="header-strip">
            <p>Account Verified &nbsp;&bull;&nbsp; Registration Complete</p>
          </div>

          <div class="email-body">

            <h1 class="greeting">Welcome to <span>Nu-age!</span></h1>
            <div class="divider"></div>

            <p class="body-text">Hi {first_name},</p>
            <p class="body-text">Your account is officially set up and ready to go.</p>
            <p class="body-text">Nu Age is built to give you access to high-quality, practical learning without burning through your data. Learners of all ages, shapes, and sizes are welcome! </p>

            <p class="body-text">If you ever get stuck or have questions, just reply directly to this email.</p>
            <p class="body-text">Let's get to work.</p>

            <div class="signature">
              <div class="sign-name">Tobi</div>
              <div class="sign-role">The Nu Age Team</div>
            </div>

          </div>

          <div class="email-footer">
            <p>© 2026 Nu Age &nbsp;&bull;&nbsp; You received this because you created an account.<br>
            Questions? <a href="#">Reply directly to this email.</a></p>
          </div>

        </div>
      </div>
    </body>
    </html>
    """

    send_email(email, "Welcome to Nu Age 🚀", html_content)

@router.post("/auth/verify-email")
async def verify_email(payload: VerifyEmailSchema, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    clean_email = payload.email.strip().lower()
    clean_code = payload.code.strip()

    # 1. Look up the OTP
    otp_record = db.query(models.SignupOTP).filter(func.lower(models.SignupOTP.email) == clean_email).first()
    
    if not otp_record or otp_record.code.strip() != clean_code:
        raise HTTPException(status_code=400, detail="Invalid verification code.")

    # 2. Check if it is expired
    now_utc = datetime.now(timezone.utc)
    expires_at = otp_record.expires_at

    # Safely force the database time to be timezone-aware if it isn't already
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if now_utc > expires_at:
        raise HTTPException(status_code=400, detail="Code expired. Please request a new one.")

    # 3. Find the user and unlock the account
    user = db.query(models.User).filter(func.lower(models.User.email) == clean_email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    user.is_verified = True

    # 4. Destroy the OTP so it can't be reused
    db.delete(otp_record)
    db.commit()

    # 5. Initialize user starting wallet (100 welcome coins backend-side once)
    try:
        from services.coin_service import init_new_user_wallet
        init_new_user_wallet(db, user.id, starting_balance=100)
    except Exception as e:
        print(f"[CoinService] Failed to init new user wallet: {e}")

    background_tasks.add_task(send_general_welcome_email, user.email, user.first_name)
    return {
        "message": "Email verified successfully!", 
    }

@router.post("/auth/resend-verification-otp")
async def resend_verification_otp(
    email: Optional[str] = None,
    payload: Optional[ResendVerificationSchema] = None,
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: Session = Depends(get_db)
):
    target = email or (payload.email if payload else None) or (payload.identifier if payload else None)
    if not target or not target.strip():
        raise HTTPException(status_code=400, detail="Email or username is required.")
    
    clean_target = target.strip()
    user = (
        db.query(models.User).filter(func.lower(models.User.email) == clean_target.lower()).first() or
        db.query(models.User).filter(func.lower(models.User.username) == clean_target.lower()).first()
    )
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No account found with that email or username.")
    
    if getattr(user, "is_verified", False):
        return {
            "message": "Account is already verified. You can log in directly.",
            "already_verified": True,
            "email": user.email
        }
    
    code = str(random.randint(100000, 999999))
    expires = datetime.now(timezone.utc) + timedelta(minutes=15)
    
    otp_record = db.query(models.SignupOTP).filter(func.lower(models.SignupOTP.email) == user.email.lower()).first()
    if otp_record:
        otp_record.code = code
        otp_record.expires_at = expires
    else:
        db.add(models.SignupOTP(email=user.email, code=code, expires_at=expires))
    db.commit()
    
    background_tasks.add_task(send_background_otp, user.email, code)
    return {
        "message": f"A new 6-digit verification code has been sent to {user.email}.",
        "already_verified": False,
        "email": user.email
    }

@router.post('/auth/login', response_model=TokenResponse)
def user_login(
    user: OAuth2PasswordRequestForm = Depends(),
    device_label: str | None = None,  # NEW: optional, e.g. "Android - Pixel 7"
    db: Session = Depends(get_db),
):
    # 1. Find the user by either email OR username
    actual_user = (
        db.query(models.User).filter(models.User.email == user.username).first() or 
        db.query(models.User).filter(models.User.username == user.username).first()
    )
    
    if not actual_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        
    # 2. Verify Password
    if not utils.verify_password(user.password, actual_user.password):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Incorrect password")

    # 3. --- OTP VERIFICATION CHECK ---
    # We check this BEFORE updating streaks or issuing tokens.
    if getattr(actual_user, 'is_verified', None) is False:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, 
            detail=f"Account not verified. Email: {actual_user.email}"
        )
    # Grab the current time in Nigeria (UTC+1) so streaks reset exactly at midnight local time
    wat_tz = pytz.timezone('Africa/Lagos')
    today = datetime.now(wat_tz).date()
    
    if actual_user.last_login_date == today:
        # They already logged in today. Do nothing to the streak.
        pass 
    elif actual_user.last_login_date == today - timedelta(days=1):
        # They logged in yesterday. Increment streak!
        actual_user.streak += 1
    else:
        # They missed yesterday, or this is their very first login. Reset to 1.
        actual_user.streak = 1

    # Update the last login date to today
    actual_user.last_login_date = today
    try:
        from services import coin_service
        coin_service.record_metric_event(db, actual_user.id, "login_streak", actual_user.streak or 1, set_absolute=True)
    except Exception as ex:
        print(f"[OBJECTIVES] Error tracking login_streak: {ex}")
    db.commit()
    # -----------------------

    # 5. Generate Token
    token = auth.create_access_token({"email": actual_user.email})

    # NEW: also issue a refresh token so the client can stay silently
    # logged in past the short access-token expiry.
    refresh_token = auth.create_refresh_token(db, actual_user.id, device_label=device_label)

    return {
        "access_token": token["access_token"],
        "refresh_token": refresh_token,
        "type": token["type"],
    }


# NEW: exchange a refresh token for a new access token. Does NOT depend on
# get_current_user — no access token is needed, since the whole point is
# that the access token is presumed expired/gone.
@router.post('/auth/refresh', response_model=TokenResponse)
def refresh_token_endpoint(payload: RefreshRequest, db: Session = Depends(get_db)):
    user, new_refresh_token = auth.verify_and_rotate_refresh_token(
        db, payload.refresh_token, device_label=payload.device_label
    )

    if not user:
        # Covers: unknown token, expired token, reused/revoked token.
        # Client should treat this the same as a dead session — clear
        # local tokens and prompt login.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token invalid or expired. Please log in again.",
        )

    new_access_token = auth.create_access_token({"email": user.email})

    return {
        "access_token": new_access_token["access_token"],
        # IMPORTANT: refresh tokens rotate on every use. The client MUST
        # overwrite its stored refresh token with this new value — reusing
        # the old one will trip reuse-detection and log the user out of
        # every device (see verify_and_rotate_refresh_token).
        "refresh_token": new_refresh_token,
        "type": new_access_token["type"],
    }


# NEW: revoke just this device's refresh token (server-side logout).
@router.post('/auth/logout', status_code=status.HTTP_204_NO_CONTENT)
def logout(payload: LogoutRequest, db: Session = Depends(get_db)):
    auth.revoke_refresh_token(db, payload.refresh_token)
    return None


# NEW: revoke ALL refresh tokens for this user (log out everywhere).
# Designed for the "Log Out All Devices" button in the profile view.
@router.post('/auth/logout-all', status_code=status.HTTP_200_OK)
def logout_all_devices(
    current_user=Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    revoked_count = (
        db.query(models.RefreshToken)
        .filter(
            models.RefreshToken.user_id == current_user.id,
            models.RefreshToken.revoked_at.is_(None),
        )
        .update({"revoked_at": now})
    )
    db.commit()
    return {
        "message": f"All sessions revoked. {revoked_count} active session(s) were logged out.",
        "revoked_count": revoked_count,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GOOGLE OAUTH 2.0 (UNIVERSAL BACKEND-BROKERED FLOW)
# ─────────────────────────────────────────────────────────────────────────────
import secrets
import json
import urllib.parse
import httpx
from fastapi.responses import HTMLResponse, RedirectResponse

@router.get('/auth/google/config')
def get_google_auth_config():
    """Returns whether Google OAuth is enabled on this backend instance."""
    settings = Settings()
    client_id = settings.get_google_client_id()
    return {
        "enabled": bool(client_id),
        "client_id": client_id if client_id else None,
    }

@router.get('/auth/google/start')
def start_google_auth(
    platform: Optional[str] = "web",
    persona: Optional[str] = "student",
):
    """Generates the secure Google OAuth authorization URL for the client."""
    settings = Settings()
    client_id = settings.get_google_client_id()
    if not client_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Google OAuth is not configured on the server. Please set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.",
        )

    clean_platform = (platform if isinstance(platform, str) else "web").lower().strip()
    clean_persona = (persona if isinstance(persona, str) else "student").lower().strip()

    # Package state payload with random nonce to prevent CSRF
    state_payload = {
        "nonce": secrets.token_urlsafe(16),
        "platform": clean_platform,
        "persona": clean_persona,
        "ts": int(datetime.now(timezone.utc).timestamp()),
    }
    state_token = base64.urlsafe_b64encode(json.dumps(state_payload).encode("utf-8")).decode("utf-8")

    redirect_uri = settings.get_google_redirect_uri()
    auth_params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state_token,
        "access_type": "offline",
        "prompt": "select_account",
    }
    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(auth_params)}"
    return {"auth_url": auth_url}

@router.get('/auth/google/callback')
async def google_auth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Handles the OAuth redirect from Google, reconciles the user, and redirects to the client."""
    settings = Settings()
    platform = "web"
    persona = "student"

    if state and isinstance(state, str):
        try:
            raw_state = base64.urlsafe_b64decode(state.encode("utf-8")).decode("utf-8")
            parsed_state = json.loads(raw_state)
            platform = str(parsed_state.get("platform", "web")).lower()
            persona = str(parsed_state.get("persona", "student")).lower()
        except Exception as ex:
            print(f"[GoogleAuth] Warning: Could not parse OAuth state: {ex}")

    # Handle user cancellation or denial
    is_err = bool(error and isinstance(error, str))
    if is_err or not code:
        err_msg = str(error) if is_err else "Authorization code was missing."
        if platform in ("android", "apk"):
            return HTMLResponse(
                f"""<!DOCTYPE html><html><body style="font-family:sans-serif;text-align:center;padding:40px;background:#111;color:#fff;">
                <h3>Sign in cancelled</h3><p>{err_msg}</p>
                <a href="nuage://auth-callback?error={urllib.parse.quote(err_msg)}" style="display:inline-block;padding:10px 20px;background:#4CAF50;color:#000;text-decoration:none;border-radius:8px;">Return to Nu Age</a>
                </body></html>""",
                status_code=400,
            )
        elif platform == "desktop":
            return HTMLResponse(
                f"""<!DOCTYPE html><html><body style="font-family:sans-serif;text-align:center;padding:40px;background:#111;color:#fff;">
                <h3>Sign in cancelled</h3><p>{err_msg}</p>
                <p>You can close this tab and return to the Nu Age desktop app.</p>
                </body></html>""",
                status_code=400,
            )
        else:
            frontend_url = settings.get_frontend_app_url().rstrip("/")
            return RedirectResponse(f"{frontend_url}/#/auth-callback?error={urllib.parse.quote(err_msg)}")

    # Exchange authorization code for tokens with Google
    client_id = settings.get_google_client_id()
    client_secret = settings.get_google_client_secret()
    redirect_uri = settings.get_google_redirect_uri()

    token_url = "https://oauth2.googleapis.com/token"
    token_payload = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }

    async with httpx.AsyncClient(timeout=15.0) as http_client:
        token_resp = await http_client.post(token_url, data=token_payload)
        if token_resp.status_code != 200:
            err_detail = token_resp.text
            print(f"[GoogleAuth] Token exchange failed ({token_resp.status_code}): {err_detail}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Google token exchange failed: {err_detail}",
            )

        token_data = token_resp.json()
        google_access_token = token_data.get("access_token")

        # Fetch verified user profile
        userinfo_resp = await http_client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {google_access_token}"},
        )
        if userinfo_resp.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to retrieve profile information from Google.",
            )

        user_info = userinfo_resp.json()

    user_email = (user_info.get("email") or "").strip().lower()
    if not user_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google account did not provide a valid email address.",
        )

    given_name = user_info.get("given_name") or ""
    family_name = user_info.get("family_name") or ""
    full_name = user_info.get("name") or ""
    picture = user_info.get("picture")

    # 1. Look up existing user
    existing_user = db.query(models.User).filter(func.lower(models.User.email) == user_email).first()

    wat_tz = pytz.timezone('Africa/Lagos')
    today = datetime.now(wat_tz).date()

    if existing_user:
        actual_user = existing_user
        # Verified by Google identity
        actual_user.is_verified = True
        if picture and not actual_user.profile_picture_url:
            actual_user.profile_picture_url = picture

        # Streak logic
        if actual_user.last_login_date == today - timedelta(days=1):
            actual_user.streak = (actual_user.streak or 0) + 1
        elif actual_user.last_login_date != today:
            actual_user.streak = 1
        actual_user.last_login_date = today

        try:
            from services import coin_service
            coin_service.record_metric_event(db, actual_user.id, "login_streak", actual_user.streak or 1, set_absolute=True)
        except Exception:
            pass

        db.commit()
    else:
        # 2. Provision new learner
        first_n = given_name or (full_name.split()[0] if full_name else "Learner")
        last_n = family_name or (full_name.split()[-1] if full_name and len(full_name.split()) > 1 else "Nu")

        # Generate unique username
        base_username = (given_name or user_email.split("@")[0]).lower()
        clean_prefix = "".join(c for c in base_username if c.isalnum() or c == "_")[:10] or "learner"
        candidate_username = f"{clean_prefix}_{secrets.token_hex(3)}"
        while db.query(models.User).filter(models.User.username == candidate_username).first():
            candidate_username = f"{clean_prefix}_{secrets.token_hex(3)}"

        # Generate random password hash (account is protected by Google OAuth)
        random_pwd = secrets.token_urlsafe(24)
        hashed_pwd = utils.hash_password(random_pwd)

        actual_user = models.User(
            first_name=first_n,
            last_name=last_n,
            gender=models.Gender.CUSTOM,
            email=user_email,
            password=hashed_pwd,
            username=candidate_username,
            role=models.Roles.STUDENT,
            profile_picture_url=picture,
            is_verified=True,
            streak=1,
            last_login_date=today,
        )
        db.add(actual_user)
        db.commit()
        db.refresh(actual_user)

        # Initialize welcome wallet with 100 Nu-Coins
        try:
            from services.coin_service import init_new_user_wallet
            init_new_user_wallet(db, actual_user.id, starting_balance=100)
        except Exception as e:
            print(f"[CoinService] Failed to init Google user wallet: {e}")

    # Generate access and refresh tokens
    access_token_data = auth.create_access_token({"email": actual_user.email})
    access_token = access_token_data["access_token"]
    refresh_token = auth.create_refresh_token(
        db, actual_user.id, device_label=f"Google Auth ({platform})"
    )

    # Dispatch response to appropriate client platform
    if platform in ("android", "apk"):
        deep_link = f"nuage://auth-callback?access_token={urllib.parse.quote(access_token)}&refresh_token={urllib.parse.quote(refresh_token)}"
        html_content = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Redirecting to Nu Age...</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="0;url={deep_link}">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; text-align: center; padding: 48px 24px; background: #0F1711; color: #E8F5E9; }}
    .card {{ max-width: 420px; margin: 0 auto; background: #1B241C; padding: 32px 24px; border-radius: 16px; border: 1px solid #2B3A2C; box-shadow: 0 8px 32px rgba(0,0,0,0.4); }}
    .btn {{ display: inline-block; margin-top: 24px; padding: 14px 28px; background: #37BF14; color: #000; text-decoration: none; font-weight: 700; border-radius: 12px; font-size: 15px; }}
  </style>
</head>
<body>
  <div class="card">
    <h2>Welcome to Nu Age! 🚀</h2>
    <p>Signing you in automatically...</p>
    <a class="btn" href="{deep_link}">Open Nu Age App</a>
  </div>
  <script>
    window.location.href = "{deep_link}";
  </script>
</body>
</html>"""
        return HTMLResponse(content=html_content)

    elif platform == "desktop":
        deep_link = f"nuage://auth-callback?access_token={urllib.parse.quote(access_token)}&refresh_token={urllib.parse.quote(refresh_token)}"
        html_content = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Nu Age - Logged In</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; text-align: center; padding: 60px 20px; background: #0F1711; color: #E8F5E9; }}
    .card {{ max-width: 440px; margin: 0 auto; background: #1B241C; padding: 36px 28px; border-radius: 16px; border: 1px solid #2B3A2C; box-shadow: 0 8px 32px rgba(0,0,0,0.4); }}
    .btn {{ display: inline-block; margin-top: 20px; padding: 12px 28px; background: #37BF14; color: #000; text-decoration: none; font-weight: 700; border-radius: 10px; font-size: 14px; }}
  </style>
</head>
<body>
  <div class="card">
    <h2>Welcome to Nu Age! 🎉</h2>
    <p>You have signed in with Google successfully. You can return to your Nu Age app now.</p>
    <a class="btn" href="{deep_link}">Return to App</a>
  </div>
  <script>
    try {{ window.location.href = "{deep_link}"; }} catch(e) {{}}
  </script>
</body>
</html>"""
        return HTMLResponse(content=html_content)

    else:
        # Web deployment redirect
        frontend_url = settings.get_frontend_app_url().rstrip("/")
        redirect_target = f"{frontend_url}/#/auth-callback?access_token={urllib.parse.quote(access_token)}&refresh_token={urllib.parse.quote(refresh_token)}"
        return RedirectResponse(url=redirect_target)


@router.post('/auth/google', response_model=TokenResponse)
async def exchange_google_token(
    payload: GoogleAuthPayload,
    db: Session = Depends(get_db),
):
    """Direct token exchange endpoint if the client possesses an ID token or access token."""
    token = payload.id_token or payload.access_token
    if not token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either id_token or access_token is required.",
        )

    user_info = None

    # Try verifying as an id_token first via Google Auth SDK
    try:
        from google.oauth2 import id_token as google_id_token
        from google.auth.transport import requests as google_requests

        settings = Settings()
        client_id = settings.get_google_client_id()
        user_info = google_id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            audience=client_id or None,
        )
    except Exception:
        # Fall back to userinfo endpoint with bearer token
        try:
            async with httpx.AsyncClient(timeout=10.0) as http_client:
                resp = await http_client.get(
                    "https://www.googleapis.com/oauth2/v3/userinfo",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code == 200:
                    user_info = resp.json()
        except Exception as ex:
            print(f"[GoogleAuth] Direct token verification failed: {ex}")

    if not user_info or not user_info.get("email"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired Google token.",
        )

    user_email = user_info["email"].strip().lower()
    given_name = user_info.get("given_name") or ""
    family_name = user_info.get("family_name") or ""
    full_name = user_info.get("name") or ""
    picture = user_info.get("picture")

    wat_tz = pytz.timezone('Africa/Lagos')
    today = datetime.now(wat_tz).date()

    existing_user = db.query(models.User).filter(func.lower(models.User.email) == user_email).first()
    if existing_user:
        actual_user = existing_user
        actual_user.is_verified = True
        if picture and not actual_user.profile_picture_url:
            actual_user.profile_picture_url = picture
        if actual_user.last_login_date == today - timedelta(days=1):
            actual_user.streak = (actual_user.streak or 0) + 1
        elif actual_user.last_login_date != today:
            actual_user.streak = 1
        actual_user.last_login_date = today
        db.commit()
    else:
        first_n = given_name or (full_name.split()[0] if full_name else "Learner")
        last_n = family_name or (full_name.split()[-1] if full_name and len(full_name.split()) > 1 else "Nu")
        base_username = (given_name or user_email.split("@")[0]).lower()
        clean_prefix = "".join(c for c in base_username if c.isalnum() or c == "_")[:10] or "learner"
        candidate_username = f"{clean_prefix}_{secrets.token_hex(3)}"
        while db.query(models.User).filter(models.User.username == candidate_username).first():
            candidate_username = f"{clean_prefix}_{secrets.token_hex(3)}"

        random_pwd = secrets.token_urlsafe(24)
        hashed_pwd = utils.hash_password(random_pwd)

        actual_user = models.User(
            first_name=first_n,
            last_name=last_n,
            gender=models.Gender.CUSTOM,
            email=user_email,
            password=hashed_pwd,
            username=candidate_username,
            role=models.Roles.STUDENT,
            profile_picture_url=picture,
            is_verified=True,
            streak=1,
            last_login_date=today,
        )
        db.add(actual_user)
        db.commit()
        db.refresh(actual_user)

        try:
            from services.coin_service import init_new_user_wallet
            init_new_user_wallet(db, actual_user.id, starting_balance=100)
        except Exception:
            pass

    access_token_data = auth.create_access_token({"email": actual_user.email})
    refresh_token = auth.create_refresh_token(
        db, actual_user.id, device_label=payload.device_label or "Google Auth Direct"
    )

    return {
        "access_token": access_token_data["access_token"],
        "refresh_token": refresh_token,
        "type": access_token_data["type"],
    }


# Admin get all users
@router.get('', response_model= List[UserBase])
def get_all_users(name: str | None = Query(None, description="search a user by name filter"), db: Session = Depends(get_db),user = Depends(auth.get_current_user)):
    if not user:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='You do not have permission to access this information')
    users=db.query(models.User).all()
    if name:
        users=db.query(models.User).filter((models.User.first_name.ilike(f'%{name}%')) | (models.User.last_name.ilike(f'%{name}%')) | (models.User.username.ilike(f'%{name}%'))).all()
    return users
    #raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='You do not have permission to access this information')

# Get one user by identifier
@router.get('/one', response_model=UserProfile)
def get_one_user(identifier: str | UUID | None= Query(None, description = "get a user by username or id"), db:Session = Depends(get_db)):
    user = db.query(models.User).filter(or_(models.User.username==identifier, models.User.id==identifier)).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail= "User not found")
    
    active = db.query(models.Enrollment).filter(models.Enrollment.student_id == user.id, models.Enrollment.completed_at == None).count()
    finished = db.query(models.Enrollment).filter(models.Enrollment.student_id == user.id, models.Enrollment.completed_at != None).count()
    
    res = UserProfile.model_validate(user)
    res.active_count = active
    res.finished_count = finished
    return res

@router.get('/me', response_model=UserBase)
def get_current_user(user = Depends(auth.get_current_user), db:Session = Depends(get_db)):
    return user
# Update user email

@router.patch('/me/update', response_model=UserBase)
async def update_profile(
    profile_data: ProfileUpdate, 
    db: Session = Depends(get_db), 
    user = Depends(auth.get_current_user)
):
    # 1. Convert the Pydantic model to a dict, excluding unset fields
    update_data = profile_data.model_dump(exclude_unset=True)

    if not update_data:
        raise HTTPException(status_code=400, detail="No fields provided for update")

    # 2. Validation Checks for Unique Fields
    if 'email' in update_data:
        new_email = update_data['email']
        if user.email == new_email:
            raise HTTPException(status_code=409, detail="This is already your current email")
        if db.query(models.User).filter(models.User.email == new_email).first():
            raise HTTPException(status_code=409, detail="Email is already taken")
        user.email = new_email

    if 'username' in update_data:
        new_username = update_data['username']
        if user.username == new_username:
             raise HTTPException(status_code=409, detail="This is already your current username")
        if db.query(models.User).filter(models.User.username == new_username).first():
            raise HTTPException(status_code=409, detail="Username is already taken")
        user.username = new_username

    # 3. Handle simple fields (first_name, last_name, gender)
    if 'first_name' in update_data:
        user.first_name = update_data['first_name']
    
    if 'last_name' in update_data:
        user.last_name = update_data['last_name']

    if 'gender' in update_data:
        user.gender = update_data['gender']

    # 4. Handle profile picture upload / removal
    if update_data.get('remove_picture'):
        user.profile_picture_url = None
    elif update_data.get('image_bytes') and update_data.get('image_filename'):
        try:
            raw_bytes = base64.b64decode(update_data['image_bytes'])
            if len(raw_bytes) > 5 * 1024 * 1024:
                raise HTTPException(status_code=400, detail="Profile picture exceeds 5MB size limit")
            ext = update_data['image_filename'].rsplit(".", 1)[-1].lower() if "." in update_data['image_filename'] else "jpg"
            if ext not in ("jpg", "jpeg", "png", "webp", "gif"):
                ext = "jpg"
            safe_filename = f"avatar_{uuid.uuid4().hex[:12]}.{ext}"
            folder_path = f"avatars/{user.id}"
            cdn_url = await upload_bytes_to_bunny(raw_bytes, safe_filename, folder_path)
            user.profile_picture_url = cdn_url
        except HTTPException:
            raise
        except Exception as e:
            print(f"Warning: Profile update succeeded but CDN avatar upload failed: {e}")
    elif 'profile_picture_url' in update_data:
        user.profile_picture_url = update_data['profile_picture_url']

    # 5. Save Changes
    db.commit()
    db.refresh(user)
    return user

@router.post('/me/avatar', response_model=UserBase)
async def upload_avatar(
    payload: AvatarUploadSchema,
    db: Session = Depends(get_db),
    user = Depends(auth.get_current_user)
):
    try:
        raw_bytes = base64.b64decode(payload.image_bytes)
        if len(raw_bytes) > 5 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Profile picture exceeds 5MB size limit")
        ext = payload.image_filename.rsplit(".", 1)[-1].lower() if "." in payload.image_filename else "jpg"
        if ext not in ("jpg", "jpeg", "png", "webp", "gif"):
            ext = "jpg"
        safe_filename = f"avatar_{uuid.uuid4().hex[:12]}.{ext}"
        folder_path = f"avatars/{user.id}"
        cdn_url = await upload_bytes_to_bunny(raw_bytes, safe_filename, folder_path)
        user.profile_picture_url = cdn_url
        db.commit()
        db.refresh(user)
        return user
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to upload avatar: {str(e)}")

@router.delete('/me/avatar', response_model=UserBase)
def remove_avatar(
    db: Session = Depends(get_db),
    user = Depends(auth.get_current_user)
):
    user.profile_picture_url = None
    db.commit()
    db.refresh(user)
    return user
# Delete user 
@router.delete('/me', status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user= Depends(auth.get_current_user), db: Session = Depends(get_db)): 
    db.delete(user)
    db.commit()


from sqlalchemy import or_, and_

@router.get("/directory", response_model=List[UserDirectorySchema])
def get_user_directory(
    db: Session = Depends(get_db), 
    current_user = Depends(auth.get_current_user)
):
    """
    Returns a list of the user's accepted friends. 
    Formatted as (id, name, email) to preserve existing frontend UI components.
    """
    # 1. Fetch only the accepted connections for the current user
    connections = db.query(models.Connection).filter(
        models.Connection.status == "accepted",
        or_(
            models.Connection.requester_id == current_user.id,
            models.Connection.addressee_id == current_user.id
        )
    ).all()
    
    friends_list = []
    
    # 2. Extract the friend and format it to match the old Directory Schema
    for conn in connections:
        # If the current user sent the request, the friend is the addressee. Otherwise, they are the requester.
        friend_user = conn.addressee if conn.requester_id == current_user.id else conn.requester
        
        friends_list.append({
            "id": friend_user.id, 
            "name": f"{friend_user.first_name} {friend_user.last_name}",
            "email": friend_user.email,
            "profile_picture_url": friend_user.profile_picture_url
        })
        
    return friends_list

@router.delete('/delete', status_code=status.HTTP_204_NO_CONTENT)
def delete_user(email:str, user= Depends(auth.get_current_user), db: Session = Depends(get_db)): 
    if user.role != "Admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail= "You do not have the required permission to perorm this action")
    User = db.query(models.User).filter(models.User.email == email).first()
    if not User:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail= "User Not found")
    db.delete(User)
    db.commit()