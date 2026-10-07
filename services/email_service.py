"""
Multi-provider email sender with fallback (Amazon SES via boto3 -> Resend SDK -> SendKit via requests).

Sync on purpose: your email functions run through FastAPI BackgroundTasks, which
runs plain `def` functions in a threadpool, so blocking HTTP calls are fine and
there's no need for httpx.

Save as services/email_service.py.
"""
import logging
import time

from fastapi.concurrency import run_in_threadpool

import boto3
import requests
import resend
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from database import Settings

log = logging.getLogger("email_service")
settings = Settings()

# Order providers are tried in. A provider is skipped automatically if its key
# isn't configured. If Resend is on the free plan (100/day), put it LAST.
# Override without a code change via EMAIL_PROVIDER_ORDER, e.g. "ses" (SES only,
# for testing), "ses,resend,sendkit" (SES first, then the old ones as fallback).
PROVIDER_ORDER = [
    p.strip()
    for p in (getattr(settings, "EMAIL_PROVIDER_ORDER", None) or "ses,resend,sendkit").split(",")
    if p.strip()
]

# Each provider needs a "from" on a domain verified AT THAT provider.
FROM_ADDRESSES = {
    # Must be on a domain/address verified in SES **in eu-north-1 (Stockholm)**.
    "ses": "Tobi from Nu Age <support@nu-age.com.ng>",
    "resend": "Tobi from Nu Age <support@nu-age.name.ng>",
    "sendkit": "Tobi from Nu Age <support@nu-age.com.ng>",
}

# Settings fields each provider needs (all must be non-empty).
REQUIRED_SETTINGS = {
    "ses": ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"],
    "resend": ["RESEND_API_KEY"],
    "sendkit": ["SENDKIT_API_KEY"],
}

TIMEOUT = (3, 8)  # connect, read (seconds). Keeps a hung provider from tying up a worker.
_http = requests.Session()

# Resend SDK setup (same calls your original code used). Newer SDK versions let
# us shorten the default 30s request timeout; older ones don't have the hook, so
# we only set it when it exists.
resend.api_key = getattr(settings, "RESEND_API_KEY", None)
if hasattr(resend, "RequestsClient") and hasattr(resend, "default_http_client"):
    resend.default_http_client = resend.RequestsClient(timeout=8)


# Amazon SES client (region-specific: identities live in eu-north-1 only).
# Built only if keys exist. boto3's own retries are off so our fallback decides.
_ses_client = None
if getattr(settings, "AWS_ACCESS_KEY_ID", None) and getattr(settings, "AWS_SECRET_ACCESS_KEY", None):
    _ses_client = boto3.client(
        "ses",
        region_name=getattr(settings, "AWS_REGION", None) or "eu-north-1",
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        config=Config(connect_timeout=3, read_timeout=8, retries={"max_attempts": 1}),
    )


class ProviderError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _check_http(r: requests.Response) -> str:
    """Success = 2xx AND a message id in the body. Anything else counts as failure."""
    if not r.ok:
        raise ProviderError(f"HTTP {r.status_code}: {r.text[:300]}", r.status_code)
    try:
        msg_id = r.json().get("id")
    except ValueError:
        msg_id = None
    if not msg_id:
        raise ProviderError(f"HTTP {r.status_code} but no message id: {r.text[:300]}", r.status_code)
    return msg_id


def _send_ses(to: str, subject: str, html: str) -> str:
    if _ses_client is None:
        raise ProviderError("SES client not configured")
    try:
        resp = _ses_client.send_email(
            Source=FROM_ADDRESSES["ses"],
            Destination={"ToAddresses": [to]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Html": {"Data": html, "Charset": "UTF-8"}},
            },
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("AccessDenied", "AccessDeniedException", "InvalidClientTokenId",
                    "SignatureDoesNotMatch", "UnrecognizedClientException"):
            status = 403   # bad/unauthorised keys -> long cooldown
        elif code in ("Throttling", "ThrottlingException"):
            status = 429   # rate or daily quota hit -> long cooldown
        elif code == "MessageRejected":
            status = 422   # per-message problem (e.g. unverified recipient in sandbox) -> no cooldown
        else:
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        raise ProviderError(f"SES {code}: {e}", status) from None
    except BotoCoreError as e:
        raise ProviderError(f"{type(e).__name__}: {e}") from None
    return resp["MessageId"]


def _send_resend(to: str, subject: str, html: str) -> str:
    params: resend.Emails.SendParams = {
        "from": FROM_ADDRESSES["resend"],
        "to": [to],
        "subject": subject,
        "html": html,
    }
    try:
        resp = resend.Emails.send(params)
    except Exception as e:
        # ResendError carries the HTTP status in `code`; network errors don't.
        status = None
        try:
            status = int(getattr(e, "code", None))
        except (TypeError, ValueError):
            pass
        raise ProviderError(f"{type(e).__name__}: {e}", status) from None

    msg_id = resp.get("id") if isinstance(resp, dict) else getattr(resp, "id", None)
    if not msg_id:
        raise ProviderError(f"Resend returned no message id: {resp!r}"[:300])
    return msg_id


def _send_sendkit(to: str, subject: str, html: str) -> str:
    r = _http.post(
        "https://api.sendkit.dev/emails",
        headers={"Authorization": f"Bearer {settings.SENDKIT_API_KEY}"},
        json={"from": FROM_ADDRESSES["sendkit"], "to": to, "subject": subject, "html": html},
        timeout=TIMEOUT,
    )
    return _check_http(r)


SENDERS = {"ses": _send_ses, "resend": _send_resend, "sendkit": _send_sendkit}

# Simple circuit breaker: after a provider fails, try it last for a while so a
# dead or rate-limited provider doesn't add latency to every single email.
_cooldown_until: dict[str, float] = {}


def _cooldown_seconds(err: Exception) -> int:
    if isinstance(err, ProviderError) and err.status == 422:
        return 0  # one bad message, not a provider outage
    if isinstance(err, ProviderError) and err.status in (401, 403, 429):
        return 600  # bad key / quota exhausted: unlikely to fix itself soon
    return 60  # timeout, 5xx, network blip


def _enabled(name: str) -> bool:
    return all(getattr(settings, k, None) for k in REQUIRED_SETTINGS[name])


def send_email(to: str, subject: str, html: str):
    """
    Try each configured provider in order. Returns (provider, message_id) on
    success, or None if every provider failed (and logs an error).
    """
    names = [n for n in PROVIDER_ORDER if _enabled(n)]
    if not names:
        log.error("No email provider is configured")
        return None

    now = time.monotonic()
    ready = [n for n in names if _cooldown_until.get(n, 0) <= now]
    # Providers in cooldown are still tried, but only as a last resort.
    order = ready + [n for n in names if n not in ready]

    for name in order:
        try:
            msg_id = SENDERS[name](to, subject, html)
            log.info("email sent via %s to %s (id=%s)", name, to, msg_id)
            return name, msg_id
        except Exception as e:
            _cooldown_until[name] = time.monotonic() + _cooldown_seconds(e)
            log.warning("provider %s failed for %s: %s", name, to, e)

    log.error("ALL email providers failed for %s (subject=%r)", to, subject)
    return None


async def send_email_async(to: str, subject: str, html: str):
    """
    Safe to `await` from any `async def` route/function. Runs the blocking
    send_email() in the threadpool so the event loop is never blocked.
    Inside BackgroundTasks with a plain `def`, keep using send_email() directly.
    """
    return await run_in_threadpool(send_email, to, subject, html)