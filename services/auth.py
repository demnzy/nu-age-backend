from jose import jwt, JWTError # FIX 1: Import JWTError directly
from datetime import datetime, timedelta, timezone
from typing import Optional
import os
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

def create_access_token(data: dict):
    to_encode = data.copy()
    
    # FIX 2: Explicitly state "minutes=" so it doesn't default to days!
    to_expire = datetime.now(timezone.utc) + timedelta(minutes=expire) 
    
    to_encode.update({"exp": to_expire})
    encoded_jwt = jwt.encode(to_encode, key, algorithm=ALGORITHM)
    return {"access_token": encoded_jwt, "type": "Bearer"}

def verify_access_token(token: str, credentials_exception):
    try:
        # FIX 3: python-jose expects algorithms as a list
        payload = jwt.decode(token, key=key, algorithms=[ALGORITHM])
        
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
        user_email = payload.get("email") 
        
        if not user_email:
            return None
            
        # Fetch the user from the database
        user = db.query(models.User).filter(models.User.email == user_email).first()
        return user
        
    except JWTError:
        # Token is expired, forged, or completely invalid
        return None