"""
Replays the kinds of requests your frontend makes (harmless ones: bad email, wrong
password, garbage token) and reports status, time, and whether the reply is JSON.
It creates no accounts and changes no data.

Run it against local, then against production, and compare:

    python check_api.py http://127.0.0.1:8000
    python check_api.py https://YOUR-API-DOMAIN

What the frontend does with each outcome:
  * no answer within 15s  -> "The server is waking up"
  * reply that is not JSON (502/503 page, "Internal Server Error") -> "Server error or waking up"
"""
import sys
import time

import requests

if len(sys.argv) < 2:
    sys.exit("usage: python check_api.py <base-url>")
BASE = sys.argv[1].rstrip("/")
FRONTEND_TIMEOUT = 15  # seconds, your signup_request read timeout

CHECKS = [
    ("health", "GET", "/health", {}),
    ("me without token", "GET", "/users/me", {}),
    ("register: bad email (expect 422)", "POST", "/users/auth/register", {"json": {
        "username": "checkapi_user", "email": "not-an-email", "password": "Passw0rd!123",
        "first_name": "Check", "last_name": "Api", "gender": "other", "role": "student"}}),
    ("login: wrong password", "POST", "/users/auth/login", {"data": {
        "username": "nobody@example.invalid", "password": "wrong-password"}}),
    ("refresh: garbage token", "POST", "/users/auth/refresh", {"json": {"refresh_token": "garbage"}}),
]

problems = 0
print(f"Target: {BASE}\n")
for label, method, path, kwargs in CHECKS:
    start = time.monotonic()
    try:
        r = requests.request(method, BASE + path, timeout=(5, 30), **kwargs)
    except requests.RequestException as e:
        print(f"[FAIL] {label:36} no response: {type(e).__name__}")
        problems += 1
        continue
    secs = time.monotonic() - start

    notes = []
    try:
        body = r.json()
        detail = body.get("detail") if isinstance(body, dict) else None
        shape = f"detail is {type(detail).__name__}" if detail is not None else "json"
        preview = str(body)[:110]
    except ValueError:
        body, shape, preview = None, "NOT JSON", r.text[:110].replace("\n", " ")
        notes.append("frontend shows 'Server error or waking up'")
    if secs > FRONTEND_TIMEOUT:
        notes.append(f"SLOWER than the frontend's {FRONTEND_TIMEOUT}s timeout -> 'server is waking up'")
    elif secs > 5:
        notes.append("slow")
    if r.status_code in (502, 503, 504):
        notes.append("proxy/gateway error")

    bad = bool(notes) and ("NOT JSON" in shape or secs > FRONTEND_TIMEOUT or r.status_code >= 500)
    problems += bad
    print(f"[{'FAIL' if bad else ' ok '}] {label:36} {r.status_code}  {secs:5.1f}s  {shape}")
    print(f"       {preview}")
    for n in notes:
        print(f"       ! {n}")

print(f"\n{'No problems found.' if not problems else f'{problems} problem(s) found.'}")