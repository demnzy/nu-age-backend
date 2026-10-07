"""
Run INSIDE your backend container (Coolify -> your app -> Terminal), from the
folder that contains main.py:

    python diag_email.py you@example.com

It prints what's installed, which keys are set (never the full key), then sends
one real test email through each provider separately and shows the exact error.
"""
import sys
import traceback
from importlib.metadata import version, PackageNotFoundError

print("python:", sys.version.split()[0])
for pkg in ("resend", "requests", "fastapi", "pydantic"):
    try:
        print(f"{pkg}:", version(pkg))
    except PackageNotFoundError:
        print(f"{pkg}: NOT INSTALLED  <-- problem")

try:
    from database import Settings
    s = Settings()
except Exception:
    print("\nCould not load Settings:")
    traceback.print_exc()
    sys.exit(1)

print()
for key in ("RESEND_API_KEY", "SENDKIT_API_KEY"):
    v = getattr(s, key, None)
    print(key + ":", f"set (starts {v[:3]!r}, length {len(v)})" if v else "MISSING or EMPTY  <-- problem")

to = sys.argv[1] if len(sys.argv) > 1 else None
if not to:
    print("\nAdd a recipient to send test emails:  python diag_email.py you@example.com")
    sys.exit(0)

try:
    import services.email_service as es
except Exception:
    print("\nCould not import services/email_service.py (this would also crash the app on startup):")
    traceback.print_exc()
    sys.exit(1)

print("\nProvider order:", es.PROVIDER_ORDER)
for name in es.PROVIDER_ORDER:
    print(f"\n--- {name} ---")
    try:
        print("OK, message id:", es.SENDERS[name](to, "Nu Age diagnostic", "<p>diagnostic test</p>"))
    except Exception as e:
        print("FAILED:", repr(e), "| http status:", getattr(e, "status", None))