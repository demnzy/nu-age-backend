from fastapi import *
from fastapi.security.oauth2 import OAuth2PasswordRequestForm
from schemas import *
from database import get_db
from sqlalchemy import or_
from sqlalchemy.orm import Session
import models
from services import utils, auth
from typing import List
from datetime import datetime, timezone, timedelta


class UserDirectorySchema(BaseModel):
    id: UUID
    name: str
    email: str

router = APIRouter(prefix=('/users'))

# create a user
@router.post('/auth/register', response_model= UserBase)
async def register_user(user:UserReg, db: Session = Depends(get_db)):
    user.password = utils.hash_password(user.password)
    user1 = models.User(**user.model_dump())
    if user1.role == "Teacher":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="You can't Signup as an instructor!")
    if db.query(models.User).filter(models.User.username==user.username).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists, choose another one")
    if db.query(models.User).filter(models.User.email==user.email).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email is associated with another account")
    db.add(user1)
    db.commit()
    db.refresh(user1)
    return user1

# user logic

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

    # 3. --- STREAK LOGIC ---
    # Get today's date in UTC to prevent timezone bugs
    today = datetime.now(timezone.utc).date()
    
    if actual_user.last_login_date == today:
        # They already logged in today. Do nothing to the streak.
        pass 
    elif actual_user.last_login_date == today - timedelta(days=1):
        # They logged in yesterday. Increment streak!
        actual_user.streak += 1
    else:
        # They missed a day, or this is their very first login. Reset to 1.
        actual_user.streak = 1

    # Update the last login date to today
    actual_user.last_login_date = today
    db.commit()
    # -----------------------

    # 4. Generate Token
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
    

