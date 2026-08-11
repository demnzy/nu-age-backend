from fastapi import *
from fastapi.security.oauth2 import OAuth2PasswordRequestForm
from schemas import *
from database import get_db,Settings
from sqlalchemy import or_
from sqlalchemy.orm import Session
import models
from services import utils, auth
from typing import List
from datetime import datetime, timezone, timedelta
import random
import resend
import pytz
class UserDirectorySchema(BaseModel):
    id: UUID
    name: str
    email: str

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
    settings = Settings()
    resend.api_key = settings.RESEND_API_KEY
    params: resend.Emails.SendParams = {
        "from": "Tobi from Nu Age <support@nu-age.name.ng>",
        "to": [email],
        "subject": "Verify your Nu Age Account",
        "html": f"Thank you for signing up! Your OTP code is: <strong>{code}</strong>. Please note this code expires in 15 minutes. Not you? You can ignore this email.",
    }
    try:
        resend.Emails.send(params)
    except Exception as e:
        print(f"Failed to send email to {email}: {e}")

def send_password_reset_otp(email: str, code: str):
    settings = Settings()
    resend.api_key = settings.RESEND_API_KEY
    params: resend.Emails.SendParams = {
        "from": "Tobi from Nu Age <support@nu-age.name.ng>",
        "to": [email],
        "subject": "Reset your Nu Age Password",
        "html": f"You have requested to reset your Nu Age password. Your OTP code is: <strong>{code}</strong>. Please note this code expires in 15 minutes. Not you? You can ignore this email.",
    }
    try:
        resend.Emails.send(params)
    except Exception as e:
        print(f"Failed to send email to {email}: {e}")

class DeviceTokenSchema(BaseModel):
    token: str
    device_type: str = "web" # default

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
    settings = Settings()
    resend.api_key = settings.RESEND_API_KEY
    
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

    params: resend.Emails.SendParams = {
        "from": " Tobi from Nu Age <support@nu-age.name.ng>",
        "to": [email],
        "subject": "Welcome to Nu Age 🚀",
        "html": html_content,
    }
    
    try:
        resend.Emails.send(params)
        print("I sent!")
    except Exception as e:
        print(f"Failed to send welcome email to {email}: {e}")

@router.post("/auth/verify-email")
async def verify_email( payload: VerifyEmailSchema,background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    # 1. Look up the OTP
    otp_record = db.query(models.SignupOTP).filter(models.SignupOTP.email == payload.email).first()
    
    if not otp_record or otp_record.code != payload.code:
        raise HTTPException(status_code=400, detail="Invalid verification code.")

    # 3. Check if it is expired
    now_utc = datetime.now(timezone.utc)
    expires_at = otp_record.expires_at

    # Safely force the database time to be timezone-aware if it isn't already
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if now_utc > expires_at:
        raise HTTPException(status_code=400, detail="Code expired. Please request a new one.")

    # 2. Find the user and unlock the account
    user = db.query(models.User).filter(models.User.email == payload.email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    user.is_verified = True

    # 3. Destroy the OTP so it can't be reused
    db.delete(otp_record)
    db.commit()

    # 4. Generate the JWT Token so they are instantly logged into the dashboard
    # access_token = create_access_token(data={"sub": user.email})
    background_tasks.add_task(send_general_welcome_email, user.email, user.first_name)
    return {
        "message": "Email verified successfully!", 
        # "access_token": access_token, 
        # "token_type": "bearer"
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
            detail="Account not verified. Please check your email for the OTP code."
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
    
# Admin get all users
@router.get('', response_model= List[UserBase])
def get_all_users(name: str | None = Query(None, description="search a user by name filter"), db: Session = Depends(get_db), ): #user = Depends(auth.get_current_user)):
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
    return user

@router.get('/me', response_model=UserBase)
def get_current_user(user = Depends(auth.get_current_user), db:Session = Depends(get_db)):
    return user
# Update user email

@router.patch('/me/update', response_model=UserBase)
def update_profile(
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

    # 3. Handle simple fields (first_name, last_name)
    if 'first_name' in update_data:
        user.first_name = update_data['first_name']
    
    if 'last_name' in update_data:
        user.last_name = update_data['last_name']

    # 4. Save Changes
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
            "email": friend_user.email
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