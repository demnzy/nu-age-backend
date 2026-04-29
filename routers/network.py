from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_, not_,func
import models
import schemas
from database import get_db
from services import auth 
import uuid

router = APIRouter(prefix="/network", tags=["Network"])

# ══════════════════════════════════════════════════════════════════════════════
# 1. MY NETWORK (The Roster)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/friends", response_model=list[schemas.NetworkUserResponse])
def get_friends(skip: int = 0, limit: int = 50, db: Session = Depends(get_db), current_user = Depends(auth.get_current_user)):
    """Fetches all accepted friends."""
    
    # Find all accepted connections where the user is EITHER the requester OR the addressee
    connections = db.query(models.Connection).filter(
        models.Connection.status == "accepted",
        or_(
            models.Connection.requester_id == current_user.id,
            models.Connection.addressee_id == current_user.id
        )
    ).offset(skip).limit(limit).all()

    friends_list = []
    for conn in connections:
        # If the user requested it, the friend is the addressee. Otherwise, the friend is the requester.
        friend_user = conn.addressee if conn.requester_id == current_user.id else conn.requester
        friends_list.append(friend_user)

    return friends_list

# ══════════════════════════════════════════════════════════════════════════════
# 2. CONNECTION REQUESTS (The Inbox)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/requests/incoming", response_model=list[schemas.ConnectionRequestResponse])
def get_incoming_requests(db: Session = Depends(get_db), current_user = Depends(auth.get_current_user)):
    requests = db.query(models.Connection).filter(
        models.Connection.addressee_id == current_user.id,
        models.Connection.status == "pending"
    ).all()
    
    return [{"id": req.id, "user": req.requester, "created_at": req.created_at} for req in requests]

@router.post("/requests/{target_user_id}")
def send_request(target_user_id: uuid.UUID, db: Session = Depends(get_db), current_user = Depends(auth.get_current_user)):
    if target_user_id == current_user.id:
        raise HTTPException(status_code=400, detail="You cannot friend yourself.")
        
    # Check if a connection already exists (in either direction)
    existing = db.query(models.Connection).filter(
        or_(
            and_(models.Connection.requester_id == current_user.id, models.Connection.addressee_id == target_user_id),
            and_(models.Connection.requester_id == target_user_id, models.Connection.addressee_id == current_user.id)
        )
    ).first()
    
    if existing:
        raise HTTPException(status_code=400, detail="A connection or pending request already exists.")
        
    new_request = models.Connection(requester_id=current_user.id, addressee_id=target_user_id)
    db.add(new_request)
    db.commit()
    
    return {"status": "success", "request_id": str(new_request.id), "message": "Friend request sent."}

@router.post("/requests/{request_id}/accept")
def accept_request(request_id: uuid.UUID, db: Session = Depends(get_db), current_user = Depends(auth.get_current_user)):
    req = db.query(models.Connection).filter(
        models.Connection.id == request_id,
        models.Connection.addressee_id == current_user.id, # Ensure they are the recipient
        models.Connection.status == "pending"
    ).first()
    
    if not req:
        raise HTTPException(status_code=404, detail="Request not found.")
        
    req.status = "accepted"
    db.commit()
    return {"status": "success", "message": "Friend request accepted."}

# ══════════════════════════════════════════════════════════════════════════════
# 3. DISCOVER (The Growth Engine)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/discover/peers", response_model=list[schemas.NetworkUserResponse])
def discover_peers(limit: int = 10, db: Session = Depends(get_db), current_user = Depends(auth.get_current_user)):
    """Finds people in the same university, excluding current friends/pending requests."""
    
    if not current_user.university:
        return [] # Can't discover peers if they haven't set their university

    # 1. Build a list of IDs the user is ALREADY connected to (friends, pending, or declined)
    existing_connections = db.query(models.Connection).filter(
        or_(
            models.Connection.requester_id == current_user.id,
            models.Connection.addressee_id == current_user.id
        )
    ).all()
    
    excluded_ids = [current_user.id] # Always exclude yourself
    for conn in existing_connections:
        excluded_ids.append(conn.addressee_id if conn.requester_id == current_user.id else conn.requester_id)

    # 2. Query the User table: Match university (e.g., FUTMinna), but exclude the known IDs
    peers = db.query(models.User).filter(
        models.User.university == current_user.university,
        not_(models.User.id.in_(excluded_ids))
    ).limit(limit).all()

    return peers

@router.delete("/friends/{friend_id}")
def remove_friend(friend_id: uuid.UUID, db: Session = Depends(get_db), current_user = Depends(auth.get_current_user)):
    """Removes an accepted friend from the user's network."""
    
    # Find the accepted connection between these two users
    connection = db.query(models.Connection).filter(
        models.Connection.status == "accepted",
        or_(
            and_(models.Connection.requester_id == current_user.id, models.Connection.addressee_id == friend_id),
            and_(models.Connection.requester_id == friend_id, models.Connection.addressee_id == current_user.id)
        )
    ).first()
    
    if not connection:
        raise HTTPException(status_code=404, detail="Friend connection not found.")
        
    db.delete(connection)
    db.commit()
    
    return {"status": "success", "message": "Friend removed from network."}

@router.post("/requests/{request_id}/decline")
def decline_request(request_id: uuid.UUID, db: Session = Depends(get_db), current_user = Depends(auth.get_current_user)):
    """Marks a pending incoming request as declined."""
    
    req = db.query(models.Connection).filter(
        models.Connection.id == request_id,
        models.Connection.addressee_id == current_user.id, # Ensure they are the intended recipient
        models.Connection.status == "pending"
    ).first()
    
    if not req:
        raise HTTPException(status_code=404, detail="Pending request not found.")
        
    req.status = "declined"
    db.commit()
    
    return {"status": "success", "message": "Friend request declined."}

@router.delete("/requests/{request_id}/cancel")
def cancel_outgoing_request(request_id: uuid.UUID, db: Session = Depends(get_db), current_user = Depends(auth.get_current_user)):
    """Hard-deletes a pending request that the current user sent."""
    
    req = db.query(models.Connection).filter(
        models.Connection.id == request_id,
        models.Connection.requester_id == current_user.id, # Ensure they are the sender
        models.Connection.status == "pending"
    ).first()
    
    if not req:
        raise HTTPException(status_code=404, detail="Pending outgoing request not found.")
        
    db.delete(req)
    db.commit()
    
    return {"status": "success", "message": "Outgoing request canceled."}

@router.get("/requests/sent", response_model=list[schemas.ConnectionRequestResponse])
def get_sent_requests(db: Session = Depends(get_db), current_user = Depends(auth.get_current_user)):
    """Fetches all pending requests the authenticated user has initiated."""
    
    requests = db.query(models.Connection).filter(
        models.Connection.requester_id == current_user.id,
        models.Connection.status == "pending"
    ).all()
    
    # Notice we map req.addressee to the "user" field, because the current_user is the requester!
    return [{"id": req.id, "user": req.addressee, "created_at": req.created_at} for req in requests]

def get_excluded_user_ids(db: Session, user_id: uuid.UUID):
    # Fetch ONLY the requester and addressee columns
    connections = db.query(models.Connection.requester_id, models.Connection.addressee_id).filter(
        or_(
            models.Connection.requester_id == user_id,
            models.Connection.addressee_id == user_id
        )
    ).all()
    
    excluded_ids = {user_id}
    # conn is now just a lightweight tuple: (requester_id, addressee_id)
    for req_id, add_id in connections:
        excluded_ids.add(req_id)
        excluded_ids.add(add_id)
        
    return excluded_ids


@router.get("/discover/peers", response_model=list[schemas.NetworkUserResponse])
def get_discover_peers(limit: int = 10, db: Session = Depends(get_db), current_user = Depends(auth.get_current_user)):
    """Finds people at the same university, excluding existing connections."""
    
    # If the user hasn't set their university, return an empty list or handle accordingly
    if not current_user.university:
        return []

    excluded_ids = get_excluded_user_ids(db, current_user.id)
    
    peers = db.query(models.User).filter(
        models.User.university == current_user.university,
        models.User.id.notin_(excluded_ids)
    ).order_by(func.random()).limit(limit).all()
    
    return peers


@router.get("/discover/organization", response_model=list[schemas.NetworkUserResponse])
def get_discover_org(limit: int = 10, db: Session = Depends(get_db), current_user = Depends(auth.get_current_user)):
    """Finds people who share AT LEAST ONE organisation with the current user."""
    excluded_ids = get_excluded_user_ids(db, current_user.id)
    
    # 1. Create a subquery of all Organisation IDs the current user belongs to
    user_org_ids = db.query(models.OrganisationMember.organisation_id).filter(
        models.OrganisationMember.user_id == current_user.id
    ).subquery()
    
    # 2. Use the 'organisations' relationship to find users who are in any of those org IDs
    org_peers = db.query(models.User).filter(
        models.User.organisations.any(
            models.Organisation.id.in_(user_org_ids)
        ),
        models.User.id.notin_(excluded_ids)
    ).order_by(func.random()).limit(limit).all()
    
    return org_peers


@router.get("/discover/trending", response_model=list[schemas.NetworkUserResponse])
def get_discover_trending(limit: int = 10, db: Session = Depends(get_db), current_user = Depends(auth.get_current_user)):
    """Finds globally active learners ordered by their study streak."""
    excluded_ids = get_excluded_user_ids(db, current_user.id)
    
    # Your User model has a 'streak' column, so we use that for trending
    trending = db.query(models.User).filter(
        models.User.id.notin_(excluded_ids)
    ).order_by(models.User.streak.desc().nulls_last()).limit(limit).all()
    
    return trending