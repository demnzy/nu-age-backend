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
    recipient_user_ids: list = None,
    title: str = "",
    body: str = "",
    category: str = "general",
    action_route: str = None,
    sender_id = None,
    data_payload: dict = None,
    send_push: bool = True,
    allow_self_notify: bool = False,
    user_id = None,
    notification_type: str = None,
    collapse_id: str = None,
    **kwargs
) -> list:
    """
    Unified, reliable notification dispatcher:
    1. Deduplicates recipient user IDs and excludes sender unless allow_self_notify=True.
    2. Persists a UserNotification record in PostgreSQL for every recipient.
    3. Fires targeted push notifications via OneSignal & FCM with deep-link routing.
    4. Synchronizes with in-app notifications tab, bell badge, and mobile system trays.
    """
    import uuid
    import httpx

    # Normalize aliases & compatibility arguments
    if recipient_user_ids is None and user_id is not None:
        recipient_user_ids = [user_id]
    if notification_type and category == "general":
        category = notification_type

    # 1. Normalize and deduplicate recipients
    if not isinstance(recipient_user_ids, (list, tuple, set)):
        recipient_user_ids = [recipient_user_ids] if recipient_user_ids else []

    sender_str = str(sender_id).strip().lower() if sender_id else ""
    valid_recipients_uuid = []
    valid_recipients_str = []
    seen = set()
    for uid in recipient_user_ids:
        if not uid:
            continue
        try:
            u_obj = uuid.UUID(str(uid).strip()) if not isinstance(uid, uuid.UUID) else uid
            u_str = str(u_obj)
        except Exception:
            u_str = str(uid).strip()
            u_obj = None

        if (allow_self_notify or u_str.lower() != sender_str) and u_str.lower() not in seen:
            seen.add(u_str.lower())
            if u_obj:
                valid_recipients_uuid.append(u_obj)
            valid_recipients_str.append(u_str)

    if not valid_recipients_str:
        return []

    # 2. Normalize routing and payload
    clean_route = str(action_route).strip() if action_route else None
    if clean_route and not clean_route.startswith("/"):
        clean_route = "/" + clean_route

    clean_data = dict(data_payload or {})
    if collapse_id and "collapse_id" not in clean_data:
        clean_data["collapse_id"] = str(collapse_id).strip()
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

        # Prefer UUID objects for foreign key integrity in PostgreSQL
        target_ids_for_db = valid_recipients_uuid if valid_recipients_uuid else valid_recipients_str
        for target_id in target_ids_for_db:
            notif = models.UserNotification(
                user_id=target_id,
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
    onesignal_sent = False
    if send_push:
        try:
            settings = Settings()
            app_id = settings.get_onesignal_app_id()
            api_key = settings.get_onesignal_rest_api_key()

            if not app_id or not api_key:
                print(f"[OneSignal] WARNING: Push dispatch skipped. Missing credentials in Settings. "
                      f"ONESIGNAL_APP_ID={'configured (' + settings.mask_onesignal_app_id() + ')' if app_id else 'MISSING'}, "
                      f"ONESIGNAL_REST_API_KEY={'configured (' + settings.mask_onesignal_key() + ')' if api_key else 'MISSING'}")
            elif not valid_recipients_str:
                print(f"[OneSignal] Push dispatch skipped: No valid recipients to notify.")
            else:
                # Modern OneSignal REST API endpoint and Authorization: Key header
                onesignal_url = "https://api.onesignal.com/notifications"
                headers = {
                    "Authorization": f"Key {api_key}",
                    "Content-Type": "application/json; charset=utf-8",
                }
                body_payload = {
                    "app_id": app_id,
                    "include_aliases": {"external_id": valid_recipients_str},
                    "target_channel": "push",
                    "headings": {"en": title},
                    "contents": {"en": body},
                    "data": clean_data,
                }
                collapse_key = clean_data.get("collapse_id")
                if collapse_key:
                    body_payload["collapse_id"] = str(collapse_key).strip()

                print(f"[OneSignal] Dispatching push to {len(valid_recipients_str)} user(s)... "
                      f"app_id={settings.mask_onesignal_app_id()}, auth=Key {settings.mask_onesignal_key()}, "
                      f"title='{title[:30]}'")

                with httpx.Client(timeout=12.0) as client:
                    res = client.post(onesignal_url, json=body_payload, headers=headers)
                    res_text = res.text
                    try:
                        res_json = res.json()
                    except Exception:
                        res_json = {}

                    notif_id = res_json.get("id")
                    recipients = res_json.get("recipients", 0)
                    errors = res_json.get("errors")
                    warnings = res_json.get("warnings")

                    if res.status_code in (200, 201) and bool(notif_id) and not errors:
                        onesignal_sent = True
                        print(f"[OneSignal] Push SUCCESS: id={notif_id}, recipients={recipients}, warnings={warnings}")
                    else:
                        print(f"[OneSignal] Push issue: status={res.status_code}, id={notif_id}, recipients={recipients}, errors={errors}, warnings={warnings}, raw={res_text}")

                        # Resilient fallback: If alias targeting failed (e.g. invalid_aliases or 400), retry with legacy include_external_user_ids
                        if (res.status_code == 400 or (errors and isinstance(errors, dict) and "invalid_aliases" in errors)) and valid_recipients_str:
                            print(f"[OneSignal] Retrying with legacy include_external_user_ids targeting...")
                            fallback_payload = dict(body_payload)
                            fallback_payload.pop("include_aliases", None)
                            fallback_payload.pop("target_channel", None)
                            fallback_payload["include_external_user_ids"] = valid_recipients_str
                            res_fallback = client.post(onesignal_url, json=fallback_payload, headers=headers)
                            try:
                                fb_json = res_fallback.json()
                            except Exception:
                                fb_json = {}
                            fb_id = fb_json.get("id")
                            if res_fallback.status_code in (200, 201) and bool(fb_id) and not fb_json.get("errors"):
                                onesignal_sent = True
                                print(f"[OneSignal] Fallback SUCCESS: id={fb_id}, recipients={fb_json.get('recipients', 0)}")
                            else:
                                print(f"[OneSignal] Fallback response: status={res_fallback.status_code}, id={fb_id}, errors={fb_json.get('errors')}")

        except Exception as os_ex:
            print(f"[OneSignal] ERROR dispatching push notification: {os_ex!r}")

        # 5. Direct FCM fallback dispatch for registered device tokens (Exclusive fallback if OneSignal did not deliver)
        if not onesignal_sent:
            try:
                if valid_recipients_uuid:
                    tokens = db.query(models.DeviceToken).filter(
                        models.DeviceToken.user_id.in_(valid_recipients_uuid)
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
                            print(f"[notifications] Direct FCM fallback dispatched to {len(token_strings)} token(s).")
            except Exception as fcm_ex:
                print(f"[notifications] FCM fallback notice: {fcm_ex}")

    return created_notifs


def send_push_notification(db: Session, user_id, title: str, body: str, data_payload: dict = None):
    """Direct push notification dispatch that ensures both OneSignal and DB persistence."""
    return dispatch_notification(
        db=db,
        recipient_user_ids=[user_id],
        title=title,
        body=body,
        action_route=(data_payload or {}).get("route") or (data_payload or {}).get("action_route"),
        data_payload=data_payload,
        send_push=True
    )