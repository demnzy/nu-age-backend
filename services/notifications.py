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

def send_push_notification(db: Session, user_id: int, title: str, body: str, data_payload: dict = None):
    """
    Looks up all devices for a user and sends a push notification to them
    via OneSignal (external_id targeting) and/or direct Firebase Cloud Messaging (device tokens).
    """
    # ── A. OneSignal Dispatch (Targets external user ID) ──────────────────────
    try:
        import httpx
        settings = Settings()
        app_id = getattr(settings, "ONESIGNAL_APP_ID", "")
        api_key = getattr(settings, "ONESIGNAL_REST_API_KEY", "")
        if app_id and api_key:
            headers = {
                "Authorization": f"Basic {api_key}",
                "Content-Type": "application/json",
            }
            body_payload = {
                "app_id": app_id,
                "include_aliases": {"external_id": [str(user_id)]},
                "target_channel": "push",
                "headings": {"en": title},
                "contents": {"en": body},
                "data": data_payload or {},
            }
            with httpx.Client(timeout=10.0) as client:
                res = client.post("https://onesignal.com/api/v1/notifications", json=body_payload, headers=headers)
                print(f"[OneSignal] Dispatched notification to user {user_id}: status={res.status_code}")
    except Exception as os_ex:
        print(f"[OneSignal] Error dispatching push notification: {os_ex}")

    # ── B. Direct Firebase Cloud Messaging (FCM) Dispatch ─────────────────────
    # 1. Get all active tokens for this user
    tokens = db.query(models.DeviceToken).filter(models.DeviceToken.user_id == user_id).all()
    
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