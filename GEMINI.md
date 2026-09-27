# Nu-age Backend Development Rules

## 1. Environment & Settings Conventions
- **Single Source of Truth**: All environment configuration MUST be declared in `database.py:Settings`.
- **Flexible Service Aliases**: For third-party credentials (e.g., OneSignal, Firebase, Resend), define both underscore and unseparated variants (`ONESIGNAL_APP_ID` and `ONE_SIGNAL_APP_ID`, `ONESIGNAL_REST_API_KEY`, `ONE_SIGNAL_REST_API_KEY`, `ONESIGNAL_API_KEY`).
- **Resilient Getters**: Always access service credentials via helper methods (e.g., `settings.get_onesignal_app_id()` and `settings.get_onesignal_rest_api_key()`) that inspect model attributes and fall back to `os.getenv()`.
- **Permissive Config**: `SettingsConfigDict` must always include `extra="ignore"` so container environment variables (e.g. on Fly.io/Coolify/Docker) never raise `ValidationError`.

## 2. Notification Dispatch & Push Invariants
- **Decoupled Delivery**: External push delivery via OneSignal or FCM must remain completely independent of relational database writes. Database logging failures or table schema issues must NEVER halt push dispatch.
- **OneSignal Segments**: When broadcasting to all active subscribers, always specify `"included_segments": ["Subscribed Users"]`. Never use dashboard-only labels like `"Total Subscriptions"`.
- **Targeting Alias Invariant**: When targeting individual user IDs via OneSignal, use `"include_aliases": {"external_id": [str(uid) for uid in user_ids]}` with `"target_channel": "push"`.
- **Audience "all" Optimization**: For full broadcasts, avoid executing unbounded queries like `DeviceToken.user_id.in_(all_user_ids)`; let OneSignal broadcast via segment and query `db.query(DeviceToken).all()` directly.
