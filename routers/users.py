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

router = APIRouter(prefix=('/users'))

# create a user
# Move the email sending logic into a separate background function
def send_background_otp(email: str, code: str):
    settings = Settings()
    resend.api_key = settings.RESEND_API_KEY
    params: resend.Emails.SendParams = {
        "from": "Tobi <support@nu-age.name.ng>",
        "to": [email],
        "subject": "Verify your Nu-age Account",
        "html": f"<strong>Welcome to Nu-age! Your OTP code is: {code}. Please note this expires in 15 minutes.</strong>",
    }
    try:
        resend.Emails.send(params)
    except Exception as e:
        print(f"Failed to send email to {email}: {e}")

@router.post('/auth/register', response_model=UserBase)
async def register_user(
    user: UserReg, 
    background_tasks: BackgroundTasks, # Inject background tasks here
    db: Session = Depends(get_db)
):
    if user.role == "Teacher":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="You can't Signup as an instructor!")
    
    if db.query(models.User).filter(models.User.username == user.username).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists, choose another one")

    # Check for existing email safely
    existing_user = db.query(models.User).filter(models.User.email == user.email).first()
    if existing_user:
        # If they exist and ARE verified, block them.
        if getattr(existing_user, 'is_verified', False): 
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email is associated with another account")
        # If they exist but ARE NOT verified, we will overwrite their data and resend the code below
        else:
            user_to_save = existing_user
            user_to_save.password = utils.hash_password(user.password)
            user_to_save.username = user.username
            # Update any other fields as necessary
    else:
        # Brand new user
        user.password = utils.hash_password(user.password)
        user_to_save = models.User(**user.model_dump())
        # Make sure they default to unverified
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

@router.post("/auth/verify-email")
async def verify_email(payload: VerifyEmailSchema, db: Session = Depends(get_db)):
    # 1. Look up the OTP
    otp_record = db.query(models.SignupOTP).filter(models.SignupOTP.email == payload.email).first()
    
    if not otp_record or otp_record.code != payload.code:
        raise HTTPException(status_code=400, detail="Invalid verification code.")

    if datetime.now(timezone.utc).replace(tzinfo=None) > otp_record.expires_at:
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
    
    return {
        "message": "Email verified successfully!", 
        # "access_token": access_token, 
        # "token_type": "bearer"
    }

@router.post('/auth/login', response_model=TokenResponse)
def user_login(user: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
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
    return token
    
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
    

