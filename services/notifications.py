import firebase_admin
from firebase_admin import credentials, messaging
from sqlalchemy.orm import Session
import models
import base64
import json
from database import Settings

if not firebase_admin._apps:
    base64_key = Settings().FIREBASE_BASE64_KEY
    
    if base64_key:
        # We are on Coolify! Decode the string back into a dictionary
        decoded_key = base64.b64decode(base64_key).decode('utf-8')
        key_dict = json.loads(decoded_key)
        cred = credentials.Certificate(key_dict)
        
    firebase_admin.initialize_app(cred)

def send_push_notification(db: Session, user_id: int, title: str, body: str, data_payload: dict = None):
    """
    Looks up all devices for a user and sends a push notification to them.
    """
    # 1. Get all active tokens for this user
    tokens = db.query(models.DeviceToken).filter(models.DeviceToken.user_id == user_id).all()
    
    if not tokens:
        print(f"No device tokens found for user {user_id}")
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