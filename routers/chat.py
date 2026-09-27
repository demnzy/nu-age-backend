from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm import joinedload
from fastapi import WebSocket
from uuid import UUID
import models
from database import get_db
from services import auth
from services.auth import verify_ws_token 
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone
from typing import List
# 1. Pydantic schema for creating a group channel
router = APIRouter(prefix="/chat", tags=["Chat"])
# Create a new file or add this to your chat router file
class ChannelCreate(BaseModel):
    name: str
    type: str # "custom" or "organisation"
    org_id: Optional[UUID] = None
    is_announcement_only: bool = False
    member_ids: list[UUID] = [] # <--- CRITICAL: YOU MUST ADD THIS!

class ConnectionManager:
    def __init__(self):
        # Maps user_id -> List of active WebSockets
        self.active_connections: dict[str, list[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        if user_id not in self.active_connections:
            self.active_connections[user_id] = []
        self.active_connections[user_id].append(websocket)

    def disconnect(self, websocket: WebSocket, user_id: str):
        if user_id in self.active_connections:
            self.active_connections[user_id].remove(websocket)
            # Cleanup empty lists to save memory
            if not self.active_connections[user_id]:
                del self.active_connections[user_id]

    async def send_personal_message(self, message: dict, user_id: str):
        """Pushes a JSON payload to all active devices of a specific user."""
        if user_id in self.active_connections:
            for connection in list(self.active_connections[user_id]):
                try:
                    await connection.send_json(message)
                except Exception as e:
                    print(f"Dead connection dropped for user {user_id}: {e}")
                    self.disconnect(connection, user_id)

# Instantiate a single global manager
manager = ConnectionManager()


# 1. ADD THIS HELPER FUNCTION RIGHT ABOVE YOUR WEBSOCKET ENDPOINT
async def notify_presence(user_id: UUID, is_online: bool, db: Session):
    """Broadcasts online/offline status to all channels this user is a part of."""
    memberships = db.query(models.ChannelMember).filter_by(user_id=user_id).all()
    
    for membership in memberships:
        payload = {
            "type": "presence",
            "channel_id": str(membership.channel_id),
            "is_online": is_online
        }
        # Find everyone else in this channel and ping their websocket
        peers = db.query(models.ChannelMember.user_id).filter_by(channel_id=membership.channel_id).all()
        for peer in peers:
            peer_id_str = str(peer[0])
            if peer_id_str != str(user_id):
                await manager.send_personal_message(payload, peer_id_str)


# 2. UPDATE YOUR WEBSOCKET ENDPOINT TO TRIGGER THE HELPER
@router.websocket("/ws")
async def chat_websocket(
    websocket: WebSocket, 
    token: str, 
    db: Session = Depends(get_db)
):
    user = verify_ws_token(token, db)
    if not user:
        await websocket.close(code=1008)
        return

    user_id_str = str(user.id)
    await manager.connect(websocket, user_id_str)
    
    # Broadcast Online Status
    await notify_presence(user.id, True, db)

    try:
        while True:
            data = await websocket.receive_json()
            
            channel_id = data.get("channel_id")
            content = data.get("content")
            msg_type = data.get("type", "text")

            membership = db.query(models.ChannelMember).filter_by(
                channel_id=channel_id, user_id=user.id
            ).first()
            
            if not membership:
                await manager.send_personal_message({"error": "Unauthorized"}, user_id_str)
                continue
                
            channel = db.query(models.Channel).filter_by(id=channel_id).first()
            if channel.is_announcement_only and membership.role != "admin":
                continue
            # ==========================================
            # CRITICAL FIX: INTERCEPT TYPING BEFORE DB SAVE
            # ==========================================
            if msg_type == "read_receipt":
                membership.last_read_at = datetime.now(timezone.utc)
                db.commit()
                continue
                
            if msg_type == "typing":
                typing_payload = {
                    "id": "ephemeral",
                    "channel_id": str(channel_id),
                    "type": "typing",
                    "content": "typing...",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "sender": {
                        "id": str(user.id),
                        "name": f"{user.first_name} {user.last_name}"
                    }
                }
                channel_members = db.query(models.ChannelMember.user_id).filter_by(channel_id=channel_id).all()
                for member in channel_members:
                    await manager.send_personal_message(typing_payload, str(member[0]))
                continue  # <--- THIS STOPS THE DB CRASH!
            # ==========================================

            import json
            meta = data.get("metadata_payload")
            if isinstance(meta, str):
                try: meta_dict = json.loads(meta)
                except: meta_dict = None
            elif isinstance(meta, dict):
                meta_dict = meta
            else:
                meta_dict = None

            if msg_type == "poll_vote":
                poll_id = data.get("poll_id")
                if poll_id:
                    original_poll = db.query(models.Message).filter_by(id=poll_id).first()
                    if original_poll:
                        # Extract existing votes
                        current_meta = original_poll.metadata_payload or {}
                        if isinstance(current_meta, str):
                            try: current_meta = json.loads(current_meta)
                            except: current_meta = {}
                            
                        votes = current_meta.get("votes", {})
                        
                        # Handle toggle logic for votes based on single/multi select
                        option_voted = content
                        is_multi = current_meta.get("is_multi_select", False)
                        user_id_str = str(user.id)
                        
                        if is_multi:
                            # If multi-select, user's vote is a list of options
                            user_votes = votes.get(user_id_str, [])
                            if isinstance(user_votes, str): user_votes = [user_votes]
                            if option_voted in user_votes:
                                user_votes.remove(option_voted)
                            else:
                                user_votes.append(option_voted)
                                
                            if not user_votes:
                                if user_id_str in votes: del votes[user_id_str]
                            else:
                                votes[user_id_str] = user_votes
                        else:
                            # Single select: toggle off if same, otherwise set
                            if votes.get(user_id_str) == option_voted:
                                del votes[user_id_str]
                            else:
                                votes[user_id_str] = option_voted
                                
                        current_meta["votes"] = votes
                        original_poll.metadata_payload = current_meta
                        flag_modified(original_poll, 'metadata_payload')
                        db.commit()
                        db.refresh(original_poll)
                        
                        # Resolve sender name safely
                        sender_name = "Unknown"
                        if original_poll.sender:
                            first = original_poll.sender.first_name or ""
                            last = original_poll.sender.last_name or ""
                            sender_name = f"{first} {last}".strip()
                            if not sender_name: sender_name = getattr(original_poll.sender, "username", "Unknown")
                        else: sender_name = "System"
                            
                        broadcast_payload = {
                            "id": str(original_poll.id),
                            "channel_id": str(channel_id),
                            "type": "poll_update",
                            "content": original_poll.content,
                            "metadata_payload": original_poll.metadata_payload,
                            "created_at": original_poll.created_at.isoformat(),
                            "sender": {
                                "id": str(original_poll.sender.id) if original_poll.sender else "system",
                                "name": sender_name
                            }
                        }
                        channel_members = db.query(models.ChannelMember.user_id).filter_by(channel_id=channel_id).all()
                        for member in channel_members:
                            await manager.send_personal_message(broadcast_payload, str(member[0]))
                continue # Skip standard save

            # 5. Commit to the Database (Only real messages get here now)
            new_msg = models.Message(
                channel_id=channel_id,
                sender_id=user.id,
                content=content,
                type=msg_type,
                metadata_payload=meta_dict
            )
            db.add(new_msg)
            db.commit()
            db.refresh(new_msg)

            # 6. Broadcast the message
            sender_name = "Unknown"
            if user:
                first = user.first_name or ""
                last = user.last_name or ""
                sender_name = f"{first} {last}".strip()
                if not sender_name: sender_name = getattr(user, "username", "Unknown")

            broadcast_payload = {
                "id": str(new_msg.id),
                "channel_id": str(channel_id),
                "type": new_msg.type.value if hasattr(new_msg.type, 'value') else new_msg.type,
                "content": new_msg.content,
                "metadata_payload": new_msg.metadata_payload,
                "created_at": new_msg.created_at.isoformat(),
                "sender": {
                    "id": str(user.id),
                    "name": sender_name
                }
            }

            channel_members = db.query(models.ChannelMember.user_id).filter_by(channel_id=channel_id).all()
            for member in channel_members:
                await manager.send_personal_message(broadcast_payload, str(member[0]))

            # 7. Unified In-App & Push Notifications for Mentions (including @admin) and DMs
            from services.notifications import dispatch_notification
            chan_obj = db.query(models.Channel).filter_by(id=channel_id).first()
            chan_name = chan_obj.name if chan_obj and chan_obj.name else "Chat"
            chat_route = f"/nu-chat?channel={channel_id}"

            import re
            if not meta_dict:
                meta_dict = {}

            # Automatically extract mentions from message content as fallback
            content_mentions = re.findall(r'@([a-zA-Z0-9_.-]+)', new_msg.content or "")
            mentioned = list(meta_dict.get("mentions", []))
            for cm in content_mentions:
                if cm not in mentioned:
                    mentioned.append(cm)
            mention_ids = list(meta_dict.get("mention_ids", []))

            if mentioned or mention_ids:

                # 1. Fetch channel members for precise in-group mention resolution
                channel_member_users = (
                    db.query(models.User)
                    .join(models.ChannelMember, models.ChannelMember.user_id == models.User.id)
                    .filter(models.ChannelMember.channel_id == channel_id)
                    .all()
                )

                recipients_to_notify = set()

                # Process direct mention_ids if provided
                for m_uid in mention_ids:
                    try:
                        u_target = db.query(models.User).filter_by(id=m_uid).first()
                        if u_target and u_target.id != user.id:
                            recipients_to_notify.add(u_target.id)
                    except Exception:
                        pass

                for m_id in mentioned:
                    try:
                        m_clean = str(m_id).lstrip("@").strip().lower()
                        if m_clean == "admin":
                            admin_members = db.query(models.ChannelMember).filter_by(channel_id=channel_id, role="admin").all()
                            admin_ids = [m.user_id for m in admin_members if m.user_id != user.id]
                            if not admin_ids and chan_obj and chan_obj.created_by_id and chan_obj.created_by_id != user.id:
                                admin_ids = [chan_obj.created_by_id]
                            
                            if admin_ids:
                                dispatch_notification(
                                    db=db,
                                    recipient_user_ids=admin_ids,
                                    title=f"Admin Tag: {sender_name} in {chan_name}",
                                    body=f"{sender_name} tagged @admin: {new_msg.content[:120]}",
                                    category="chat",
                                    action_route=chat_route,
                                    sender_id=user.id,
                                    data_payload={"channel_id": str(channel_id), "type": "admin_mention"}
                                )
                        else:
                            # Match channel member users first
                            matched_u = None
                            clean_no_score = m_clean.replace("_", " ")
                            for c_u in channel_member_users:
                                if c_u.id == user.id:
                                    continue
                                c_uname = (c_u.username or "").lower().strip()
                                c_fname = (c_u.first_name or "").lower().strip()
                                c_lname = (c_u.last_name or "").lower().strip()
                                c_full = f"{c_fname} {c_lname}".strip()
                                c_full_under = f"{c_fname}_{c_lname}".strip()

                                if m_clean in (c_uname, c_fname, c_full_under) or clean_no_score in (c_full, c_fname):
                                    matched_u = c_u
                                    break
                                elif c_uname and (m_clean in c_uname or c_uname in m_clean):
                                    matched_u = c_u
                                    break
                                elif c_fname and (m_clean in c_fname or c_fname in m_clean):
                                    matched_u = c_u
                                    break

                            # Fallback to global user query if not matched among channel members
                            if not matched_u:
                                matched_u = db.query(models.User).filter(
                                    (models.User.username.ilike(m_clean)) |
                                    (models.User.first_name.ilike(m_clean)) |
                                    (models.User.first_name.ilike(clean_no_score)) |
                                    ((models.User.first_name + " " + models.User.last_name).ilike(clean_no_score))
                                ).first()

                            if matched_u and matched_u.id != user.id:
                                recipients_to_notify.add(matched_u.id)

                    except Exception as p_err:
                        print(f"[NuChat] Error resolving mention handle '{m_id}': {p_err}")

                if recipients_to_notify:
                    dispatch_notification(
                        db=db,
                        recipient_user_ids=list(recipients_to_notify),
                        title=f"{sender_name} tagged you in {chan_name}",
                        body=new_msg.content[:120],
                        category="chat",
                        action_route=chat_route,
                        sender_id=user.id,
                        data_payload={"channel_id": str(channel_id), "type": "mention"}
                    )

            elif chan_obj and (chan_obj.type.value == "direct" if hasattr(chan_obj.type, "value") else chan_obj.type == "direct"):
                # For direct messages, notify the recipient
                other_members = db.query(models.ChannelMember.user_id).filter(
                    models.ChannelMember.channel_id == channel_id,
                    models.ChannelMember.user_id != user.id
                ).all()
                other_ids = [str(m[0]) for m in other_members]
                if other_ids:
                    dispatch_notification(
                        db=db,
                        recipient_user_ids=other_ids,
                        title=sender_name,
                        body=new_msg.content[:120],
                        category="chat",
                        action_route=chat_route,
                        sender_id=user.id,
                        data_payload={"channel_id": str(channel_id), "type": "dm"}
                    )

    except WebSocketDisconnect:
        manager.disconnect(websocket, user_id_str)
        if user_id_str not in manager.active_connections:
            await notify_presence(user.id, False, db)
            
    except Exception as e:
        print(f"WS Backend Error: {e}") # Print the error if it crashes again!
        manager.disconnect(websocket, user_id_str)
        if user_id_str not in manager.active_connections:
            await notify_presence(user.id, False, db)

@router.get("/channels")
def get_user_channels(
    user = Depends(auth.get_current_user), 
    db: Session = Depends(get_db)
):
    """Fetches all channels the current user is a member of, WITH their latest messages."""
    
    # 1. Fetch memberships
    memberships = (
        db.query(models.ChannelMember)
        .filter(models.ChannelMember.user_id == user.id)
        .options(joinedload(models.ChannelMember.channel))
        .all()
    )
    
    results = []
    for membership in memberships:
        channel = membership.channel
        display_name = channel.name
        target_is_online = False
        
        # Determine DM names and online status
        if channel.type.value == "direct":
            other_user = (
                db.query(models.User)
                .join(models.ChannelMember, models.User.id == models.ChannelMember.user_id)
                .filter(
                    models.ChannelMember.channel_id == channel.id,
                    models.User.id != user.id 
                )
                .first()
            )
            
            if other_user:
                display_name = f"{other_user.first_name} {other_user.last_name}"
                target_is_online = str(other_user.id) in manager.active_connections
            else:
                display_name = "Unknown User"
                
        # ==========================================
        # CRITICAL FIX: FETCH THE LATEST MESSAGE
        # ==========================================
        # Query the messages table for this specific channel, order by newest first
        last_message = (
            db.query(models.Message)
            .filter(models.Message.channel_id == channel.id)
            .order_by(models.Message.created_at.desc())
            .first()
        )

        last_msg_content = ""
        last_msg_time = ""

        if last_message:
            # Format poll previews nicely so it doesn't show raw JSON
            msg_type_val = last_message.type.value if hasattr(last_message.type, 'value') else last_message.type
            
            if msg_type_val == "poll":
                last_msg_content = "📊 Poll: " + last_message.content.split("|||")[0]
            else:
                last_msg_content = last_message.content
            
            # Grab the ISO timestamp for Flet to format
            last_msg_time = last_message.created_at.isoformat()
        # Calculate unread count and check for unread mentions
        unread_count = 0
        has_unread_mention = False

        unread_filter = [
            models.Message.channel_id == channel.id,
            models.Message.sender_id != user.id
        ]
        if membership.last_read_at:
            unread_filter.append(models.Message.created_at > membership.last_read_at)

        unread_messages = db.query(models.Message).filter(*unread_filter).all()
        unread_count = len(unread_messages)

        if unread_count > 0:
            user_uname = (getattr(user, "username", "") or "").lower().strip()
            user_fname = (getattr(user, "first_name", "") or "").lower().strip()
            user_lname = (getattr(user, "last_name", "") or "").lower().strip()
            full_n = f"{user_fname} {user_lname}".strip()
            full_n_under = f"{user_fname}_{user_lname}".strip()
            is_group_admin = (membership.role == "admin" or (channel.created_by_id and str(channel.created_by_id) == str(user.id)))

            for um in unread_messages:
                um_meta = um.metadata_payload or {}
                if isinstance(um_meta, str):
                    try:
                        um_meta = json.loads(um_meta)
                    except Exception:
                        um_meta = {}
                um_mentions = um_meta.get("mentions", []) if isinstance(um_meta, dict) else []
                um_mention_ids = um_meta.get("mention_ids", []) if isinstance(um_meta, dict) else []

                # Direct ID match
                if str(user.id) in [str(m).strip().lower() for m in um_mention_ids]:
                    has_unread_mention = True
                    break

                # @admin match
                if is_group_admin and any(str(m).lstrip("@").strip().lower() == "admin" for m in um_mentions):
                    has_unread_mention = True
                    break

                # Check handle / name matches
                for m_str in um_mentions:
                    clean_m = str(m_str).lstrip("@").strip().lower()
                    if (user_uname and clean_m == user_uname) or (user_fname and clean_m == user_fname) or clean_m in (full_n, full_n_under):
                        has_unread_mention = True
                        break
                    elif (user_fname and user_fname in clean_m) or (user_uname and user_uname in clean_m):
                        has_unread_mention = True
                        break

                if has_unread_mention:
                    break
        # ==========================================
            
        results.append({
            "channel_id": str(channel.id),
            "name": display_name,
            "type": channel.type.value if hasattr(channel.type, 'value') else channel.type,
            "is_online": target_is_online,
            "last_msg": last_msg_content,
            "time": last_msg_time,
            "unread": unread_count,
            "has_unread_mention": has_unread_mention,
            "role": membership.role,
            "is_announcement_only": channel.is_announcement_only,
            "other_user_id": str(other_user.id) if (channel.type.value == "direct" if hasattr(channel.type, 'value') else channel.type == "direct") and other_user else None,
            "course_id": str(channel.course_id) if channel.course_id else None,
            "created_by_id": str(channel.created_by_id) if channel.created_by_id else None
        })
        
    # Finally, sort the entire list of channels so the ones with the newest messages are at the top
    results.sort(key=lambda x: x["time"], reverse=True)
        
    return results


@router.get("/channels/{channel_id}/messages")
def get_channel_messages(
    channel_id: UUID, 
    limit: int = Query(50, le=100), # Cap pagination at 100 max
    offset: int = Query(0, ge=0),
    user = Depends(auth.get_current_user), 
    db: Session = Depends(get_db)
):
    """Fetches paginated message history for a specific channel."""
    
    # 1. SECURITY CHECK: Verify the user is actually in this channel
    is_member = db.query(models.ChannelMember).filter_by(
        channel_id=channel_id, user_id=user.id
    ).first()
    
    if not is_member:
        raise HTTPException(status_code=403, detail="You do not have access to this chat.")

    # 2. Fetch the messages with pagination
    # We order by created_at DESC so the newest messages come first
    # We eagerly load the sender so we get their names without an N+1 problem
    messages = (
        db.query(models.Message)
        .filter(models.Message.channel_id == channel_id)
        .options(joinedload(models.Message.sender))
        .order_by(models.Message.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    # 3. Format the response
    formatted_messages = []
    for msg in messages:
        formatted_messages.append({
            "id": str(msg.id),
            "type": msg.type,
            "content": msg.content,
            "metadata_payload": msg.metadata_payload, # For the Polls!
            "created_at": msg.created_at,
            "sender": {
                "id": str(msg.sender.id),
                "name": f"{msg.sender.first_name} {msg.sender.last_name}"
            }
        })
        
    return formatted_messages




# 1. Update the Schema to accept member_ids
# UPDATE YOUR SCHEMA AT THE TOP OF chat.py


# UPDATE YOUR CREATE ENDPOINT IN chat.py
@router.post("/channels")
def create_group_channel(
    payload: ChannelCreate, 
    user = Depends(auth.get_current_user), 
    db: Session = Depends(get_db)
):
    """Creates a new Custom or Org-wide group chat."""
    
    new_channel = models.Channel(
        name=payload.name,
        type=payload.type,
        org_id=payload.org_id,
        is_announcement_only=payload.is_announcement_only,
        created_by_id=user.id
    )
    db.add(new_channel)
    db.flush() 

    # Add the creator as Admin
    admin_member = models.ChannelMember(
        channel_id=new_channel.id,
        user_id=user.id,
        role="admin"
    )
    db.add(admin_member)
    
    # CRITICAL FIX: Loop through the checked IDs and add them to the group!
    for member_id in payload.member_ids:
        if str(member_id) != str(user.id): # Prevent adding creator twice
            db.add(models.ChannelMember(
                channel_id=new_channel.id, 
                user_id=member_id, 
                role="member"
            ))

    db.commit()
    return {"message": "Channel created", "channel_id": str(new_channel.id)}

@router.post("/dms/{target_user_id}")
def get_or_create_direct_message(
    target_user_id: UUID,
    user = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """Finds an existing DM thread between two users, or creates a new one."""
    
    if user.id == target_user_id:
        raise HTTPException(status_code=400, detail="You cannot DM yourself.")

    # 1. Verify the target user actually exists
    target_user = db.query(models.User).filter(models.User.id == target_user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found.")

    # 2. Complex Query: Find an existing DM channel where BOTH users are members
    # First, find all DM channels the current user is in
    my_dm_channels = (
        db.query(models.Channel.id)
        .join(models.ChannelMember, models.Channel.id == models.ChannelMember.channel_id)
        .filter(models.ChannelMember.user_id == user.id, models.Channel.type == "direct")
        .subquery()
    )

    # Next, check if the target user is in any of those exact channels
    existing_dm = (
        db.query(models.ChannelMember.channel_id)
        .filter(models.ChannelMember.channel_id.in_(my_dm_channels))
        .filter(models.ChannelMember.user_id == target_user_id)
        .first()
    )

    # 3. If it exists, just return it!
    if existing_dm:
        return {"channel_id": str(existing_dm[0]), "is_new": False}

    # 4. If it doesn't exist, create a brand new DM channel
    new_dm_channel = models.Channel(type="direct", created_by_id=user.id)
    db.add(new_dm_channel)
    db.flush()

    # 5. Add BOTH users to this new room
    member_1 = models.ChannelMember(channel_id=new_dm_channel.id, user_id=user.id, role="member")
    member_2 = models.ChannelMember(channel_id=new_dm_channel.id, user_id=target_user_id, role="member")
    
    db.add_all([member_1, member_2])
    db.commit()

    return {"channel_id": str(new_dm_channel.id), "is_new": True}

class AddMembersPayload(BaseModel):
    member_ids: List[UUID]

# 2. Endpoint to fetch current members (so the UI can filter them out)
@router.get("/channels/{channel_id}/members")
def get_channel_members(
    channel_id: UUID, 
    user = Depends(auth.get_current_user), 
    db: Session = Depends(get_db)
):
    members = db.query(models.ChannelMember).options(joinedload(models.ChannelMember.user)).filter_by(channel_id=channel_id).all()
    channel = db.query(models.Channel).filter_by(id=channel_id).first()
    admin_id = str(channel.created_by_id) if channel and channel.created_by_id else None

    member_ids = []
    members_data = []
    for m in members:
        uid_str = str(m.user_id)
        member_ids.append(uid_str)
        u = m.user
        if u:
            first = u.first_name or ""
            last = u.last_name or ""
            dname = f"{first} {last}".strip() or getattr(u, "username", "Member")
            uname = getattr(u, "username", None) or dname.replace(" ", "_")
            is_adm = (m.role == "admin" or uid_str == admin_id)
            if is_adm and not admin_id:
                admin_id = uid_str
            members_data.append({
                "id": uid_str,
                "name": dname,
                "username": uname,
                "email": u.email,
                "role": m.role,
                "is_admin": is_adm
            })

    return {
        "member_ids": member_ids,
        "members": members_data,
        "admin_id": admin_id
    }

# 3. Endpoint to actually add the members and broadcast the System Message
@router.post("/channels/{channel_id}/members")
async def add_group_members(
    channel_id: UUID,
    payload: AddMembersPayload,
    user = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    # 1. Add the new users to the database
    added_names = []
    for m_id in payload.member_ids:
        exists = db.query(models.ChannelMember).filter_by(channel_id=channel_id, user_id=m_id).first()
        if not exists:
            db.add(models.ChannelMember(channel_id=channel_id, user_id=m_id, role="member"))
            new_u = db.query(models.User).filter_by(id=m_id).first()
            if new_u: 
                added_names.append(f"{new_u.first_name} {new_u.last_name}")

    if not added_names:
        return {"message": "No new members were added (they might already be in the group)."}

    # 2. Create the System Message
    names_string = ", ".join(added_names)
    sys_text = f"{user.first_name} added {names_string}"
    
    sys_msg = models.Message(
        channel_id=channel_id,
        sender_id=user.id,
        content=sys_text,
        type="system" # Ensure your database Enum allows "system", otherwise use "text"
    )
    db.add(sys_msg)
    db.commit()
    db.refresh(sys_msg)

    # 3. Broadcast the system message live to everyone (including the newly added people)
    broadcast_payload = {
        "id": str(sys_msg.id),
        "channel_id": str(channel_id),
        "type": "system", # This tells the frontend to render it differently
        "content": sys_msg.content,
        "created_at": sys_msg.created_at.isoformat(),
        "sender": {"id": "system", "name": "System"}
    }
    
    all_members = db.query(models.ChannelMember.user_id).filter_by(channel_id=channel_id).all()
    for m in all_members:
        await manager.send_personal_message(broadcast_payload, str(m[0]))

    return {"message": "Members added successfully"}

@router.delete("/channels/{channel_id}")
def delete_channel(
    channel_id: UUID,
    user = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """Permanently deletes a chat channel and all associated messages."""
    
    # 1. Verify the channel actually exists
    channel = db.query(models.Channel).filter(models.Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    # 2. Security Check: Is the user in the room, and do they have permission?
    membership = db.query(models.ChannelMember).filter_by(
        channel_id=channel_id, user_id=user.id
    ).first()

    if not membership:
        raise HTTPException(status_code=403, detail="You do not have access to this chat.")

    # Rule: For groups, only admins can delete. For 1-on-1 DMs, either person can delete.
    if channel.type.value in ["custom", "organisation", "course"] and membership.role != "admin":
        raise HTTPException(
            status_code=403, 
            detail="Only group administrators can delete this chat."
        )

    # 3. The Execution (PostgreSQL CASCADE handles the messages and members)
    db.delete(channel)
    db.commit()

    return {"message": "Chat completely deleted", "channel_id": str(channel_id)}


@router.delete("/{chat_id}/leave")
@router.delete("/channels/{chat_id}/leave")
def leave_group_chat(chat_id: UUID, db: Session = Depends(get_db), current_user = Depends(auth.get_current_user)):
    """Removes the current user from a group chat with course enrollment protection."""
    channel = db.query(models.Channel).filter_by(id=chat_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Chat channel not found.")

    # 1. Enforce Course Group Protection: Cannot leave if enrolled in the course
    is_course = (channel.course_id is not None or getattr(channel.type, "value", channel.type) == "course")
    if is_course and channel.course_id:
        enrollment = db.query(models.Enrollment).filter_by(
            course_id=channel.course_id,
            user_id=current_user.id
        ).first()
        if enrollment:
            raise HTTPException(
                status_code=400,
                detail="Cannot leave a course group while enrolled in the course. Please unenroll from the course to leave."
            )

    # 2. Find the membership row
    membership = db.query(models.ChannelMember).filter_by(
        channel_id=chat_id,
        user_id=current_user.id
    ).first()
    
    if not membership:
        raise HTTPException(status_code=404, detail="You are not a member of this chat.")
        
    db.delete(membership)
    db.commit()
    
    # 3. Clean up empty custom group
    remaining = db.query(models.ChannelMember).filter_by(channel_id=chat_id).count()
    if remaining == 0 and getattr(channel.type, "value", channel.type) == "custom":
        db.delete(channel)
        db.commit()
        return {"status": "success", "message": "Left chat. Chat was empty and has been deleted."}
        
    return {"status": "success", "message": "Successfully left the group chat."}

@router.post("/chat/dms/{target_user_id}")
def start_direct_message(
    target_user_id: UUID, 
    db: Session = Depends(get_db), 
    current_user = Depends(auth.get_current_user)
):
    if current_user.id == target_user_id:
        raise HTTPException(status_code=400, detail="Cannot DM yourself.")

    # 1. Find all Direct channels the current user is part of
    my_direct_channels = (
        db.query(models.ChannelMember.channel_id)
        .join(models.Channel)
        .filter(
            models.Channel.type == "direct",
            models.ChannelMember.user_id == current_user.id
        ).subquery()
    )

    # 2. Check if the target user is in any of those exact channels
    existing_dm = db.query(models.ChannelMember).filter(
        models.ChannelMember.channel_id.in_(my_direct_channels),
        models.ChannelMember.user_id == target_user_id
    ).first()

    if existing_dm:
        return {"channel_id": str(existing_dm.channel_id)}

    # 3. If no DM exists, create a new one safely
    new_channel = models.Channel(type="direct")
    db.add(new_channel)
    db.flush() # Flush to generate the new_channel.id

    # Add both users to the new DM channel
    db.add(models.ChannelMember(channel_id=new_channel.id, user_id=current_user.id, role="member"))
    db.add(models.ChannelMember(channel_id=new_channel.id, user_id=target_user_id, role="member"))
    
    db.commit()
    
    return {"channel_id": str(new_channel.id)}



