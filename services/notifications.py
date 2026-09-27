import firebase_admin
from firebase_admin import credentials, messaging
from sqlalchemy.orm import Session
import models
import base64
import json
from database import Settings

try:
    if not firebase_admin._apps:
        base64_key = getattr(Settings(), "FIREBASE_BASE64_KEY", None)
        if base64_key:
            decoded_key = base64.b64decode(base64_key).decode('utf-8')
            key_dict = json.loads(decoded_key)
            cred = credentials.Certificate(key_dict)
            firebase_admin.initialize_app(cred)
except Exception as fb_init_err:
    print(f"[notifications] Firebase initialization warning: {fb_init_err}")

def dispatch_notification(
    db: Session,
    recipient_user_ids: list,
    title: str,
    body: str,
    category: str = "general",
    action_route: str = None,
    sender_id = None,
    data_payload: dict = None,
    send_push: bool = True
) -> list:
    """
    Unified, reliable notification dispatcher:
    1. Deduplicates recipient user IDs and excludes sender.
    2. Persists a UserNotification record in PostgreSQL for every recipient.
    3. Fires targeted push notifications via OneSignal & FCM with deep-link routing.
    4. Synchronizes with in-app notifications tab, bell badge, and mobile system trays.
    """
    import uuid
    import httpx

    # 1. Normalize and deduplicate recipients
    if not isinstance(recipient_user_ids, (list, tuple, set)):
        recipient_user_ids = [recipient_user_ids] if recipient_user_ids else []

    sender_str = str(sender_id).strip().lower() if sender_id else ""
    valid_recipients = []
    seen = set()
    for uid in recipient_user_ids:
        if not uid:
            continue
        uid_str = str(uid).strip()
        if uid_str.lower() != sender_str and uid_str.lower() not in seen:
            seen.add(uid_str.lower())
            valid_recipients.append(uid_str)

    if not valid_recipients:
        return []

    # 2. Normalize routing and payload
    clean_route = str(action_route).strip() if action_route else None
    if clean_route and not clean_route.startswith("/"):
        clean_route = "/" + clean_route

    clean_data = dict(data_payload or {})
    if clean_route:
        clean_data["route"] = clean_route
        clean_data["action_route"] = clean_route
    clean_data["category"] = category

    # 3. Persist UserNotification records in PostgreSQL
    created_notifs = []
    try:
        sid_uuid = None
        if sender_id:
            try:
                sid_uuid = uuid.UUID(sender_str) if not isinstance(sender_id, uuid.UUID) else sender_id
            except Exception:
                sid_uuid = None

        for uid_str in valid_recipients:
            try:
                uid_uuid = uuid.UUID(uid_str)
            except Exception:
                uid_uuid = uid_str

            notif = models.UserNotification(
                user_id=uid_uuid,
                sender_id=sid_uuid,
                title=title,
                body=body,
                category=category,
                action_route=clean_route,
                data_payload=clean_data,
                is_read=False
            )
            db.add(notif)
            created_notifs.append(notif)

        db.commit()
        for n in created_notifs:
            try:
                db.refresh(n)
            except Exception:
                pass
    except Exception as db_err:
        print(f"[notifications] Error saving user_notifications to DB: {db_err}")
        try:
            db.rollback()
        except Exception:
            pass

    # 4. Dispatch Push Notifications via OneSignal (Targeting external_ids)
    if send_push:
        try:
            settings = Settings()
            app_id = getattr(settings, "ONESIGNAL_APP_ID", "")
            api_key = getattr(settings, "ONESIGNAL_REST_API_KEY", "")
            if app_id and api_key and valid_recipients:
                headers = {
                    "Authorization": f"Basic {api_key}",
                    "Content-Type": "application/json",
                }
                body_payload = {
                    "app_id": app_id,
                    "include_aliases": {"external_id": valid_recipients},
                    "target_channel": "push",
                    "headings": {"en": title},
                    "contents": {"en": body},
                    "data": clean_data,
                }
                with httpx.Client(timeout=10.0) as client:
                    res = client.post("https://onesignal.com/api/v1/notifications", json=body_payload, headers=headers)
                    print(f"[OneSignal] Dispatched notification to {len(valid_recipients)} users: status={res.status_code}")
                    if res.status_code not in (200, 201):
                        print(f"[OneSignal] Error response: {res.status_code} - {res.text}")
        except Exception as os_ex:
            print(f"[OneSignal] Error dispatching push notification: {os_ex}")

        # 5. Direct FCM fallback dispatch for registered device tokens
        try:
            tokens = db.query(models.DeviceToken).filter(
                models.DeviceToken.user_id.in_(valid_recipients)
            ).all()
            if tokens and firebase_admin._apps:
                token_strings = [t.token for t in tokens if t.token]
                if token_strings:
                    fcm_msg = messaging.MulticastMessage(
                        notification=messaging.Notification(title=title, body=body),
                        data={k: str(v) for k, v in clean_data.items()},
                        tokens=token_strings,
                    )
                    messaging.send_each_for_multicast(fcm_msg)
        except Exception as fcm_ex:
            print(f"[notifications] FCM dispatch notice: {fcm_ex}")

    return created_notifs


def send_push_notification(db: Session, user_id, title: str, body: str, data_payload: dict = None):
    """Legacy compatibility wrapper that dispatches both push and DB persistence."""
    return dispatch_notification(
        db=db,
        recipient_user_ids=[user_id],
        title=title,
        body=body,
        action_route=(data_payload or {}).get("route") or (data_payload or {}).get("action_route"),
        data_payload=data_payload,
        send_push=True
    )
    
    if not tokens:
        print(f"No device tokens found in DB for user {user_id}")
        return

    # 2. Extract just the token strings
    token_strings = [t.token for t in tokens]

    # 3. Construct the message
    # 'data' is the invisible payload your frontend can use (e.g., {"course_id": "123"})
    # 'notification' is the visible alert the user sees on their lock screen
    message = messaging.MulticastMessage(
        notification=messaging.Notification(
            title=title,
            body=body,
        ),
        data=data_payload or {},
        tokens=token_strings,
    )

    try:
        # 4. Send the message via Google's servers
        response = messaging.send_each_for_multicast(message)
        print(f"Successfully sent {response.success_count} messages.")
        
        # 5. Clean up dead tokens (Crucial for performance)
        # If a student uninstalls the app, their token becomes invalid. 
        # We must delete it so we don't keep pinging a dead phone.
        if response.failure_count > 0:
            responses = response.responses
            for idx, resp in enumerate(responses):
                if not resp.success:
                    # 'Unregistered' means the app was uninstalled or token expired
                    if resp.exception.code == 'messaging/registration-token-not-registered':
                        dead_token = token_strings[idx]
                        db.query(models.DeviceToken).filter(models.DeviceToken.token == dead_token).delete()
                        db.commit()
                        print(f"Deleted dead token: {dead_token}")

    except Exception as e:
        print(f"Error sending push notification: {e}")