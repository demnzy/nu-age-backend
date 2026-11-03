from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm import joinedload
from uuid import UUID
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
import models
from database import get_db
from services import auth, promo_service
from services.auth import verify_ws_token
from pydantic import BaseModel
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


async def _synthesize_chat_ai_reply(
    channel_id: UUID,
    user_id: UUID,
    prompt_msg_id: UUID,
    query_text: str,
    sender_name: str,
):
    """Generates an academic, grounded AI response for an inline /ai chat query and broadcasts it."""
    from database import SessionLocal
    from services import ai_service
    from services.subscription_service import record_chat_query_usage
    
    db_async = SessionLocal()
    try:
        channel = db_async.query(models.Channel).filter_by(id=channel_id).first()
        chan_title = channel.name if channel and channel.name else "Study Group"
        
        reply = await ai_service.ask_ai_tutor_response(
            query=query_text,
            course_title=chan_title,
            module_title="Interactive Chat Query",
            lesson_title=f"Asked by {sender_name}",
            lesson_content="",
            conversation_history=[],
            is_assessment=False,
        )
        # Record quota consumption for the prompter
        try:
            record_chat_query_usage(user_id, db_async)
        except Exception as qe:
            print(f"[chat_ai] Error recording quota: {qe}")
        
        # Create AI message
        ai_msg = models.Message(
            channel_id=channel_id,
            sender_id=user_id,
            content=reply,
            type="ai_response",
            metadata_payload={
                "is_ai": True,
                "prompt": query_text,
                "reply_to": {
                    "id": str(prompt_msg_id),
                    "sender_name": sender_name,
                    "snippet": (query_text or "")[:120],
                }
            }
        )
        db_async.add(ai_msg)
        db_async.commit()
        db_async.refresh(ai_msg)
        
        broadcast_payload = {
            "id": str(ai_msg.id),
            "channel_id": str(channel_id),
            "type": "ai_response",
            "content": ai_msg.content,
            "metadata_payload": ai_msg.metadata_payload,
            "created_at": ai_msg.created_at.isoformat(),
            "status": "delivered",
            "sender": {
                "id": "ai_assistant",
                "name": "Nu Study AI",
                "profile_picture_url": None,
                "primary_badge": {"name": "AI Study Companion", "color": "#8B5CF6"},
            }
        }
        
        channel_members = db_async.query(models.ChannelMember.user_id).filter_by(channel_id=channel_id).all()
        for member in channel_members:
            await manager.send_personal_message(broadcast_payload, str(member[0]))
    except Exception as ex:
        print(f"[chat_ai_reply] Error generating chat AI response: {ex}")
    finally:
        db_async.close()


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
                receipt_payload = {
                    "id": "read_receipt",
                    "channel_id": str(channel_id),
                    "type": "read_receipt",
                    "user_id": user_id_str,
                    "read_at": membership.last_read_at.isoformat()
                }
                channel_members = db.query(models.ChannelMember.user_id).filter_by(channel_id=channel_id).all()
                for member in channel_members:
                    if str(member[0]) != user_id_str:
                        await manager.send_personal_message(receipt_payload, str(member[0]))
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
                        "name": f"{user.first_name} {user.last_name}",
                        "profile_picture_url": getattr(user, "profile_picture_url", None)
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
                                "name": sender_name,
                                "profile_picture_url": getattr(original_poll.sender, "profile_picture_url", None) if original_poll.sender else None
                            }
                        }
                        channel_members = db.query(models.ChannelMember.user_id).filter_by(channel_id=channel_id).all()
                        for member in channel_members:
                            await manager.send_personal_message(broadcast_payload, str(member[0]))
                continue # Skip standard save

            # ==========================================
            # UPGRADE 1: REACTION TOGGLE
            # ==========================================
            if msg_type == "reaction":
                target_msg_id = data.get("message_id")
                emoji = (data.get("emoji") or "").strip()
                if target_msg_id and emoji:
                    target_msg = db.query(models.Message).filter_by(id=target_msg_id, channel_id=channel_id).first()
                    if target_msg:
                        current_meta = target_msg.metadata_payload or {}
                        if isinstance(current_meta, str):
                            try: current_meta = json.loads(current_meta)
                            except: current_meta = {}
                        reactions = current_meta.get("reactions", {})
                        if not isinstance(reactions, dict):
                            reactions = {}
                        u_list = list(reactions.get(emoji, []))
                        found_idx = next((i for i, u in enumerate(u_list) if str(u).strip().lower() == user_id_str.strip().lower()), None)
                        if found_idx is not None:
                            u_list.pop(found_idx)
                            if not u_list:
                                reactions.pop(emoji, None)
                            else:
                                reactions[emoji] = u_list
                        else:
                            u_list.append(user_id_str)
                            reactions[emoji] = u_list
                        current_meta["reactions"] = reactions
                        target_msg.metadata_payload = current_meta
                        flag_modified(target_msg, 'metadata_payload')
                        db.commit()
                        db.refresh(target_msg)
                        
                        reaction_broadcast = {
                            "id": "reaction_update",
                            "channel_id": str(channel_id),
                            "type": "reaction_update",
                            "message_id": str(target_msg.id),
                            "reactions": reactions,
                            "user_id": user_id_str,
                            "emoji": emoji
                        }
                        channel_members = db.query(models.ChannelMember.user_id).filter_by(channel_id=channel_id).all()
                        for member in channel_members:
                            await manager.send_personal_message(reaction_broadcast, str(member[0]))
                continue

            # ==========================================
            # UPGRADE 2: PIN / UNPIN MESSAGES (Admins only)
            # ==========================================
            if msg_type in ("pin_message", "unpin_message"):
                is_admin = (
                    channel.type == "direct" or
                    membership.role == "admin" or
                    (channel and channel.created_by_id == user.id) or
                    str(getattr(user, "role", "")).upper() in ("ADMIN", "OWNER", "SUPERADMIN")
                )
                if not is_admin:
                    await manager.send_personal_message({"error": "Only channel admins can pin or unpin messages."}, user_id_str)
                    continue
                
                target_msg_id = data.get("message_id")
                
                if msg_type == "unpin_message":
                    all_pinned = db.query(models.Message).filter(
                        models.Message.channel_id == channel_id
                    ).all()
                    for p in all_pinned:
                        pm = p.metadata_payload
                        if isinstance(pm, dict) and pm.get("is_pinned"):
                            pm.pop("is_pinned", None)
                            pm.pop("pinned_at", None)
                            pm.pop("pinned_by", None)
                            p.metadata_payload = pm
                            flag_modified(p, 'metadata_payload')
                    db.commit()
                    pin_broadcast = {
                        "id": "pin_update",
                        "channel_id": str(channel_id),
                        "type": "pin_update",
                        "message_id": None,
                        "pinned_message": None
                    }
                    channel_members = db.query(models.ChannelMember.user_id).filter_by(channel_id=channel_id).all()
                    for member in channel_members:
                        await manager.send_personal_message(pin_broadcast, str(member[0]))
                    continue

                if target_msg_id:
                    target_msg = db.query(models.Message).filter_by(id=target_msg_id, channel_id=channel_id).first()
                    if target_msg:
                        # Clear existing pinned in this channel
                        all_pinned = db.query(models.Message).filter(
                            models.Message.channel_id == channel_id
                        ).all()
                        for p in all_pinned:
                            pm = p.metadata_payload
                            if isinstance(pm, dict) and pm.get("is_pinned"):
                                pm.pop("is_pinned", None)
                                pm.pop("pinned_at", None)
                                pm.pop("pinned_by", None)
                                p.metadata_payload = pm
                                flag_modified(p, 'metadata_payload')
                        
                        current_meta = target_msg.metadata_payload or {}
                        if isinstance(current_meta, str):
                            try: current_meta = json.loads(current_meta)
                            except: current_meta = {}
                        
                        first = user.first_name or ""
                        last = user.last_name or ""
                        p_name = f"{first} {last}".strip() or getattr(user, "username", "Admin")
                        
                        current_meta["is_pinned"] = True
                        current_meta["pinned_at"] = datetime.now(timezone.utc).isoformat()
                        current_meta["pinned_by"] = {"id": user_id_str, "name": p_name}
                        
                        target_msg.metadata_payload = current_meta
                        flag_modified(target_msg, 'metadata_payload')
                        db.commit()
                        
                        pin_broadcast = {
                            "id": "pin_update",
                            "channel_id": str(channel_id),
                            "type": "pin_update",
                            "message_id": str(target_msg.id),
                            "pinned_message": {
                                "id": str(target_msg.id),
                                "content": target_msg.content,
                                "type": target_msg.type,
                                "pinned_at": current_meta.get("pinned_at"),
                                "pinned_by": current_meta.get("pinned_by"),
                            }
                        }
                        channel_members = db.query(models.ChannelMember.user_id).filter_by(channel_id=channel_id).all()
                        for member in channel_members:
                            await manager.send_personal_message(pin_broadcast, str(member[0]))
                continue

            # ==========================================
            # UPGRADE 3: DELETE MESSAGE FOR EVERYONE
            # ==========================================
            if msg_type == "delete_message":
                target_msg_id = data.get("message_id")
                if target_msg_id:
                    target_msg = db.query(models.Message).filter_by(id=target_msg_id, channel_id=channel_id).first()
                    if target_msg:
                        is_sender = (target_msg.sender_id == user.id)
                        is_admin = (
                            membership.role == "admin" or
                            (channel and channel.created_by_id == user.id) or
                            str(getattr(user, "role", "")).upper() in ("ADMIN", "OWNER", "SUPERADMIN")
                        )
                        # Check 24-hour limit for sender
                        created_dt = target_msg.created_at
                        if created_dt and created_dt.tzinfo is None:
                            created_dt = created_dt.replace(tzinfo=timezone.utc)
                        within_24h = (datetime.now(timezone.utc) - created_dt).total_seconds() <= 86400 if created_dt else False
                        
                        if not (is_admin or (is_sender and within_24h)):
                            await manager.send_personal_message({"error": "Cannot delete messages older than 24 hours."}, user_id_str)
                            continue
                        
                        target_msg.content = "This message was deleted"
                        current_meta = target_msg.metadata_payload or {}
                        if isinstance(current_meta, str):
                            try: current_meta = json.loads(current_meta)
                            except: current_meta = {}
                        current_meta["is_deleted"] = True
                        current_meta["deleted_by"] = user_id_str
                        target_msg.metadata_payload = current_meta
                        flag_modified(target_msg, 'metadata_payload')
                        db.commit()
                        
                        del_broadcast = {
                            "id": str(target_msg.id),
                            "channel_id": str(channel_id),
                            "type": "message_deleted",
                            "message_id": str(target_msg.id),
                            "content": "This message was deleted"
                        }
                        channel_members = db.query(models.ChannelMember.user_id).filter_by(channel_id=channel_id).all()
                        for member in channel_members:
                            await manager.send_personal_message(del_broadcast, str(member[0]))
                continue

            # ==========================================
            # UPGRADE 4: AI STUDY ASSISTANT QUOTA CHECK
            # ==========================================
            is_ai_prompt = bool(content and content.strip().lower().startswith("/ai "))
            if is_ai_prompt:
                from services.subscription_service import assert_chat_quota_available
                try:
                    assert_chat_quota_available(user.id, db)
                except HTTPException as q_ex:
                    await manager.send_personal_message({
                        "id": "ai_quota_exceeded",
                        "channel_id": str(channel_id),
                        "type": "system_error",
                        "content": "Daily AI query limit reached. Please upgrade in Store for unlimited queries!"
                    }, user_id_str)
                    continue

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

            client_msg_id = data.get("client_msg_id") or (meta_dict.get("client_msg_id") if isinstance(meta_dict, dict) else None)

            broadcast_payload = {
                "id": str(new_msg.id),
                "client_msg_id": client_msg_id,
                "channel_id": str(channel_id),
                "type": new_msg.type.value if hasattr(new_msg.type, 'value') else new_msg.type,
                "content": new_msg.content,
                "metadata_payload": new_msg.metadata_payload,
                "created_at": new_msg.created_at.isoformat(),
                "status": "delivered",
                "sender": {
                    "id": str(user.id),
                    "name": sender_name,
                    "profile_picture_url": getattr(user, "profile_picture_url", None),
                    "primary_badge": promo_service.resolve_user_primary_badge(db, user.id) if user else None,
                }
            }

            channel_members = db.query(models.ChannelMember.user_id).filter_by(channel_id=channel_id).all()
            for member in channel_members:
                await manager.send_personal_message(broadcast_payload, str(member[0]))

            # Dispatch background AI synthesis if this was an /ai query
            if is_ai_prompt:
                prompt_q = (content or "").strip()[4:].strip()
                if prompt_q:
                    asyncio.create_task(_synthesize_chat_ai_reply(
                        channel_id=channel_id,
                        user_id=user.id,
                        prompt_msg_id=new_msg.id,
                        query_text=prompt_q,
                        sender_name=sender_name,
                    ))

            # 7. Unified In-App & Push Notifications for Group Messages, DMs, and Mentions
            from services.notifications import dispatch_notification
            chan_obj = db.query(models.Channel).filter_by(id=channel_id).first()
            chan_name = chan_obj.name if chan_obj and chan_obj.name else "Chat"
            chat_route = f"/nu-chat?channel={channel_id}"

            import re
            if not meta_dict:
                meta_dict = {}

            # Format message snippet for preview
            msg_type_str = new_msg.type.value if hasattr(new_msg.type, 'value') else str(new_msg.type)
            if msg_type_str == "poll":
                notif_body = f"📊 Poll: {new_msg.content.split('|||')[0]}"
            elif msg_type_str in ("image", "file", "attachment"):
                notif_body = "Sent an attachment"
            else:
                notif_body = (new_msg.content or "Sent a message")[:120]

            is_dm = chan_obj and (chan_obj.type.value == "direct" if hasattr(chan_obj.type, "value") else chan_obj.type == "direct")

            if is_dm:
                # Direct message: notify the recipient (other user in DM)
                other_members = db.query(models.ChannelMember.user_id).filter(
                    models.ChannelMember.channel_id == channel_id,
                    models.ChannelMember.user_id != user.id
                ).all()
                other_ids = [str(m[0]) for m in other_members]
                if other_ids:
                    dm_body = f"{sender_name}: {notif_body}"
                    dispatch_notification(
                        db=db,
                        recipient_user_ids=other_ids,
                        title=sender_name,
                        body=dm_body,
                        category="chat",
                        action_route=chat_route,
                        sender_id=user.id,
                        data_payload={
                            "channel_id": str(channel_id),
                            "type": "dm",
                            "collapse_id": f"chat_{channel_id}",
                        }
                    )
            else:
                # Group / Channel message: notify all members in the channel (sender excluded)
                all_channel_members = db.query(models.ChannelMember.user_id).filter(
                    models.ChannelMember.channel_id == channel_id,
                    models.ChannelMember.user_id != user.id
                ).all()
                candidate_member_ids = {m[0] for m in all_channel_members}
                notified_user_ids = set()

                # Automatically extract mentions from message content and metadata
                raw_mentions = list(meta_dict.get("mentions", [])) + re.findall(r'@([\w.-]+)', new_msg.content or "")
                mentioned = []
                seen_handles = set()
                for mh in raw_mentions:
                    cl = str(mh).lstrip("@").strip().lower()
                    if cl and cl not in seen_handles:
                        seen_handles.add(cl)
                        mentioned.append(cl)
                mention_ids = list(meta_dict.get("mention_ids", []))

                # Check if sender is an admin of this channel (or platform admin)
                sender_membership = db.query(models.ChannelMember).filter_by(channel_id=channel_id, user_id=user.id).first()
                is_sender_admin = (
                    (sender_membership and sender_membership.role == "admin") or
                    (chan_obj and chan_obj.created_by_id == user.id) or
                    (str(getattr(user, "role", "")).upper() in ("ADMIN", "OWNER", "SUPERADMIN"))
                )

                # A. Handle @admin tag
                if "admin" in mentioned:
                    admin_members = db.query(models.ChannelMember).filter_by(channel_id=channel_id, role="admin").all()
                    admin_ids = [m.user_id for m in admin_members if m.user_id != user.id]
                    if not admin_ids and chan_obj and chan_obj.created_by_id and chan_obj.created_by_id != user.id:
                        admin_ids = [chan_obj.created_by_id]
                    if admin_ids:
                        dispatch_notification(
                            db=db,
                            recipient_user_ids=admin_ids,
                            title=f"Admin Tag: {sender_name} in {chan_name}",
                            body=f"{sender_name} tagged @admin: {notif_body}",
                            category="chat",
                            action_route=chat_route,
                            sender_id=user.id,
                            data_payload={
                                "channel_id": str(channel_id),
                                "type": "admin_mention",
                                "collapse_id": f"chat_{channel_id}",
                            }
                        )
                        notified_user_ids.update(admin_ids)

                # B. Handle @all tag (Admins only)
                if "all" in mentioned and is_sender_admin:
                    all_targets = [uid for uid in candidate_member_ids if uid not in notified_user_ids]
                    if all_targets:
                        dispatch_notification(
                            db=db,
                            recipient_user_ids=all_targets,
                            title=f"📢 {sender_name} in {chan_name}",
                            body=f"@all {notif_body}",
                            category="chat",
                            action_route=chat_route,
                            sender_id=user.id,
                            data_payload={
                                "channel_id": str(channel_id),
                                "type": "all_mention",
                                "collapse_id": f"chat_{channel_id}",
                            }
                        )
                        notified_user_ids.update(all_targets)

                # C. Handle specific user mentions (@handle and mention_ids)
                user_mentions = [m for m in mentioned if m not in ("admin", "all")]
                if user_mentions or mention_ids:
                    channel_member_users = (
                        db.query(models.User)
                        .join(models.ChannelMember, models.ChannelMember.user_id == models.User.id)
                        .filter(models.ChannelMember.channel_id == channel_id)
                        .all()
                    )
                    specific_recipients = set()

                    channel_member_ids = {u.id for u in channel_member_users}
                    for m_uid in mention_ids:
                        try:
                            u_target = db.query(models.User).filter_by(id=m_uid).first()
                            if u_target and u_target.id != user.id and u_target.id in channel_member_ids and u_target.id not in notified_user_ids:
                                specific_recipients.add(u_target.id)
                        except Exception:
                            pass

                    for m_id in user_mentions:
                        try:
                            m_clean = str(m_id).lstrip("@").strip().lower()
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

                            if not matched_u:
                                matched_u = db.query(models.User).filter(
                                    (models.User.username.ilike(m_clean)) |
                                    (models.User.first_name.ilike(m_clean)) |
                                    (models.User.first_name.ilike(clean_no_score)) |
                                    ((models.User.first_name + " " + models.User.last_name).ilike(clean_no_score))
                                ).first()

                            if matched_u and matched_u.id != user.id and matched_u.id not in notified_user_ids:
                                specific_recipients.add(matched_u.id)

                        except Exception as p_err:
                            print(f"[NuChat] Error resolving mention handle '{m_id}': {p_err}")

                    if specific_recipients:
                        dispatch_notification(
                            db=db,
                            recipient_user_ids=list(specific_recipients),
                            title=f"{sender_name} tagged you in {chan_name}",
                            body=notif_body,
                            category="chat",
                            action_route=chat_route,
                            sender_id=user.id,
                            data_payload={
                                "channel_id": str(channel_id),
                                "type": "mention",
                                "collapse_id": f"chat_{channel_id}",
                            }
                        )
                        notified_user_ids.update(specific_recipients)

                # D. General group message notification for all other channel members
                general_recipients = [uid for uid in candidate_member_ids if uid not in notified_user_ids]
                if general_recipients:
                    dispatch_notification(
                        db=db,
                        recipient_user_ids=general_recipients,
                        title=f"{sender_name} in {chan_name}",
                        body=notif_body,
                        category="chat",
                        action_route=chat_route,
                        sender_id=user.id,
                        data_payload={
                            "channel_id": str(channel_id),
                            "type": "channel_message",
                            "collapse_id": f"chat_{channel_id}",
                        }
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
        .options(
            joinedload(models.ChannelMember.channel)
            .joinedload(models.Channel.course)
        )
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

                # @all match (Admin broadcast mentions all channel members)
                if any(str(m).lstrip("@").strip().lower() == "all" for m in um_mentions):
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

        # Resolve avatar: course image for course chats, profile pic for DMs
        _is_dm = (channel.type.value == "direct" if hasattr(channel.type, 'value') else channel.type == "direct")
        _dm_avatar = getattr(other_user, "profile_picture_url", None) if _is_dm and other_user else None
        _course_avatar = None
        if channel.course_id and channel.course:
            _course_avatar = channel.course.image_url

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
            "other_user_id": str(other_user.id) if _is_dm and other_user else None,
            "other_user_avatar": _dm_avatar,
            "avatar_url": _course_avatar or _dm_avatar,
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

    # 3. Format the response with delivery/read status telemetry
    other_members = db.query(models.ChannelMember).filter(
        models.ChannelMember.channel_id == channel_id,
        models.ChannelMember.user_id != user.id
    ).all()
    max_other_read_at = None
    for om in other_members:
        if om.last_read_at:
            if max_other_read_at is None or om.last_read_at > max_other_read_at:
                max_other_read_at = om.last_read_at

    formatted_messages = []
    for msg in messages:
        msg_created = msg.created_at
        if msg_created and msg_created.tzinfo is None:
            msg_created = msg_created.replace(tzinfo=timezone.utc)

        is_ai_check = (msg.type == "ai_response") or (isinstance(msg.metadata_payload, dict) and msg.metadata_payload.get("is_ai"))
        is_me = (msg.sender_id == user.id) and not is_ai_check
        if is_me:
            if max_other_read_at and msg_created and max_other_read_at >= msg_created:
                msg_status = "read"
            else:
                msg_status = "delivered"
        else:
            msg_status = "delivered"

        is_ai = (msg.type == "ai_response") or (isinstance(msg.metadata_payload, dict) and msg.metadata_payload.get("is_ai"))
        if is_ai:
            sender_dict = {
                "id": "ai_assistant",
                "name": "Nu Study AI",
                "profile_picture_url": None,
                "primary_badge": {"name": "AI Study Companion", "color": "#8B5CF6"},
            }
        else:
            sender_dict = {
                "id": str(msg.sender.id) if msg.sender else str(msg.sender_id),
                "name": f"{msg.sender.first_name} {msg.sender.last_name}" if msg.sender else "User",
                "profile_picture_url": getattr(msg.sender, "profile_picture_url", None) if msg.sender else None,
                "primary_badge": promo_service.resolve_user_primary_badge(db, msg.sender.id) if msg.sender else None,
            }

        formatted_messages.append({
            "id": str(msg.id),
            "type": msg.type,
            "content": msg.content,
            "metadata_payload": msg.metadata_payload, # For the Polls!
            "created_at": msg.created_at,
            "status": msg_status,
            "sender": sender_dict
        })
        
    return formatted_messages


class MessageCreatePayload(BaseModel):
    content: str
    type: str = "text"
    metadata_payload: Optional[dict] = None


@router.post("/channels/{channel_id}/messages")
async def send_channel_message_rest(
    channel_id: UUID,
    payload: MessageCreatePayload,
    user = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    """Sends a message to a channel via REST with instant WebSocket broadcast."""
    is_member = db.query(models.ChannelMember).filter_by(
        channel_id=channel_id, user_id=user.id
    ).first()
    if not is_member:
        raise HTTPException(status_code=403, detail="You do not have access to this chat.")

    channel = db.query(models.Channel).filter_by(id=channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found.")
    if channel.is_announcement_only and is_member.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can post in announcement channels.")

    new_msg = models.Message(
        channel_id=channel_id,
        sender_id=user.id,
        content=payload.content,
        type=payload.type,
        metadata_payload=payload.metadata_payload,
    )
    db.add(new_msg)
    db.commit()
    db.refresh(new_msg)

    first = user.first_name or ""
    last = user.last_name or ""
    sender_name = f"{first} {last}".strip() or getattr(user, "username", "Unknown")

    client_msg_id = (payload.metadata_payload or {}).get("client_msg_id")

    broadcast_payload = {
        "id": str(new_msg.id),
        "client_msg_id": client_msg_id,
        "channel_id": str(channel_id),
        "type": new_msg.type.value if hasattr(new_msg.type, "value") else new_msg.type,
        "content": new_msg.content,
        "metadata_payload": new_msg.metadata_payload,
        "created_at": new_msg.created_at.isoformat(),
        "status": "delivered",
        "sender": {
            "id": str(user.id),
            "name": sender_name,
            "profile_picture_url": getattr(user, "profile_picture_url", None),
            "primary_badge": promo_service.resolve_user_primary_badge(db, user.id) if user else None,
        },
    }

    channel_members = db.query(models.ChannelMember.user_id).filter_by(channel_id=channel_id).all()
    for member in channel_members:
        await manager.send_personal_message(broadcast_payload, str(member[0]))

    is_ai_prompt = bool(payload.content and payload.content.strip().lower().startswith("/ai "))
    if is_ai_prompt:
        prompt_q = payload.content.strip()[4:].strip()
        if prompt_q:
            asyncio.create_task(_synthesize_chat_ai_reply(
                channel_id=channel_id,
                user_id=user.id,
                prompt_msg_id=new_msg.id,
                query_text=prompt_q,
                sender_name=sender_name
            ))

    return broadcast_payload




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
    channel = db.query(models.Channel).filter_by(id=channel_id).first()

    # Automatically synchronize enrolled course members if this is a course channel
    if channel and channel.course_id:
        try:
            enrolled = db.query(models.Enrollment.user_id).filter_by(course_id=channel.course_id).all()
            existing_uids = {
                row[0] for row in db.query(models.ChannelMember.user_id).filter_by(channel_id=channel_id).all()
            }
            new_members = []
            for (e_uid,) in enrolled:
                if e_uid not in existing_uids:
                    new_members.append(models.ChannelMember(channel_id=channel_id, user_id=e_uid, role="member"))
                    existing_uids.add(e_uid)
            if new_members:
                db.bulk_save_objects(new_members)
                db.commit()
        except Exception as sync_err:
            print(f"[chat] Course member auto-sync notice: {sync_err}")
            db.rollback()

    members = db.query(models.ChannelMember).options(joinedload(models.ChannelMember.user)).filter_by(channel_id=channel_id).all()
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
                "is_admin": is_adm,
                "profile_picture_url": getattr(u, "profile_picture_url", None)
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





# ==========================================
# UPGRADE 5: LIGHTWEIGHT MEDIA UPLOAD (Images & Audio <= 6MB)
# ==========================================
@router.post("/upload_media")
async def upload_chat_media(
    channel_id: UUID = Form(...),
    file: UploadFile = File(...),
    user = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """Uploads lightweight images or audio notes directly to BunnyCDN."""
    is_member = db.query(models.ChannelMember).filter_by(
        channel_id=channel_id, user_id=user.id
    ).first()
    if not is_member:
        raise HTTPException(status_code=403, detail="Not authorized in this channel.")

    allowed_images = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}
    allowed_audio = {"audio/webm", "audio/mp4", "audio/mpeg", "audio/ogg", "audio/wav", "audio/m4a", "audio/x-m4a"}

    content_type = (file.content_type or "").lower()
    if content_type not in allowed_images and content_type not in allowed_audio:
        raise HTTPException(status_code=400, detail="Only lightweight images and voice notes/audio are supported.")

    raw_bytes = await file.read()
    if len(raw_bytes) > 6 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File exceeds lightweight 6MB limit.")

    ext = file.filename.split(".")[-1].lower() if "." in (file.filename or "") else ("jpg" if content_type in allowed_images else "webm")
    safe_filename = f"{uuid.uuid4().hex[:12]}.{ext}"
    folder_path = f"chat/{channel_id}"

    from services.bunny_service import upload_bytes_to_bunny
    cdn_url = await upload_bytes_to_bunny(raw_bytes, safe_filename, folder_path)

    media_kind = "image" if content_type in allowed_images else "audio"
    return {
        "url": cdn_url,
        "filename": file.filename,
        "content_type": content_type,
        "type": media_kind,
        "size_bytes": len(raw_bytes)
    }


# ==========================================
# UPGRADE 6: GET PINNED MESSAGE
# ==========================================
@router.get("/channels/{channel_id}/pinned")
def get_pinned_message(
    channel_id: UUID,
    user = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """Fetches the active pinned message in a channel, if any."""
    is_member = db.query(models.ChannelMember).filter_by(
        channel_id=channel_id, user_id=user.id
    ).first()
    if not is_member:
        raise HTTPException(status_code=403, detail="Not authorized in this channel.")

    pinned_candidates = (
        db.query(models.Message)
        .filter(models.Message.channel_id == channel_id)
        .order_by(models.Message.created_at.desc())
        .all()
    )
    pinned = None
    for m in pinned_candidates:
        pm = m.metadata_payload
        if isinstance(pm, dict) and pm.get("is_pinned"):
            pinned = m
            break

    if not pinned:
        return None

    first = pinned.sender.first_name or "" if pinned.sender else ""
    last = pinned.sender.last_name or "" if pinned.sender else ""
    sender_name = f"{first} {last}".strip() or getattr(pinned.sender, "username", "Unknown") if pinned.sender else "System"

    return {
        "id": str(pinned.id),
        "content": pinned.content,
        "type": pinned.type,
        "created_at": pinned.created_at,
        "pinned_at": (pinned.metadata_payload or {}).get("pinned_at"),
        "pinned_by": (pinned.metadata_payload or {}).get("pinned_by"),
        "sender": {
            "id": str(pinned.sender.id) if pinned.sender else "system",
            "name": sender_name
        }
    }


# ==========================================
# UPGRADE 7: IN-ROOM CHANNEL MESSAGE SEARCH
# ==========================================
@router.get("/channels/{channel_id}/search")
def search_channel_messages(
    channel_id: UUID,
    q: str = Query(..., min_length=2),
    limit: int = Query(30, le=50),
    user = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    """Searches messages inside a specific channel by text content."""
    is_member = db.query(models.ChannelMember).filter_by(
        channel_id=channel_id, user_id=user.id
    ).first()
    if not is_member:
        raise HTTPException(status_code=403, detail="Not authorized in this channel.")

    query_filter = f"%{q.strip()}%"
    results = (
        db.query(models.Message)
        .filter(
            models.Message.channel_id == channel_id,
            models.Message.content.ilike(query_filter)
        )
        .options(joinedload(models.Message.sender))
        .order_by(models.Message.created_at.desc())
        .limit(limit)
        .all()
    )

    matched = []
    for msg in results:
        first = msg.sender.first_name or "" if msg.sender else ""
        last = msg.sender.last_name or "" if msg.sender else ""
        sender_name = f"{first} {last}".strip() or getattr(msg.sender, "username", "Unknown") if msg.sender else "System"
        matched.append({
            "id": str(msg.id),
            "content": msg.content,
            "type": msg.type,
            "created_at": msg.created_at,
            "sender_name": sender_name
        })
    return matched
