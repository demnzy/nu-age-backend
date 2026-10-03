import hmac
import hashlib
import json
import httpx
from typing import Dict, Any, Optional
from database import Settings

settings = Settings()

PAYSTACK_BASE_URL = "https://api.paystack.co"


class PaymentGateway:
    """Abstract interface for pluggable payment processing gateways."""

    async def initialize_payment(
        self,
        email: str,
        amount: float,
        reference: str,
        callback_url: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError

    async def verify_payment(self, reference: str) -> Dict[str, Any]:
        raise NotImplementedError

    def verify_webhook_signature(self, payload_bytes: bytes, signature_header: str) -> bool:
        raise NotImplementedError


class PaystackGateway(PaymentGateway):
    def __init__(self):
        self.secret_key = settings.get_paystack_secret_key()
        self.public_key = settings.get_paystack_public_key()
        self.webhook_secret = settings.get_paystack_webhook_secret()

    async def initialize_payment(
        self,
        email: str,
        amount: float,
        reference: str,
        callback_url: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Initializes a transaction with Paystack.
        Amount is in Naira (major unit) and is multiplied by 100 to convert to Kobo.
        """
        if not self.secret_key:
            raise ValueError("Paystack secret key is not configured in settings.")

        url = f"{PAYSTACK_BASE_URL}/transaction/initialize"
        headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json",
        }
        # Paystack requires amount in Kobo (integer)
        amount_kobo = int(round(amount * 100))

        payload: Dict[str, Any] = {
            "email": email,
            "amount": amount_kobo,
            "reference": reference,
        }
        if callback_url:
            payload["callback_url"] = callback_url
        if metadata:
            payload["metadata"] = metadata

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            data = resp.json()
            if not resp.is_success or not data.get("status"):
                msg = data.get("message") or f"Paystack init failed with code {resp.status_code}"
                raise ValueError(msg)
            return data.get("data", {})

    async def verify_payment(self, reference: str) -> Dict[str, Any]:
        """
        Queries Paystack REST API to verify the definitive status of a transaction.
        """
        if not self.secret_key:
            raise ValueError("Paystack secret key is not configured in settings.")

        url = f"{PAYSTACK_BASE_URL}/transaction/verify/{reference}"
        headers = {
            "Authorization": f"Bearer {self.secret_key}",
        }

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url, headers=headers)
            data = resp.json()
            if not resp.is_success or not data.get("status"):
                msg = data.get("message") or f"Paystack verify failed with code {resp.status_code}"
                raise ValueError(msg)
            return data.get("data", {})

    def verify_webhook_signature(self, payload_bytes: bytes, signature_header: str) -> bool:
        """
        Validates HMAC-SHA512 webhook signature against Paystack secret.
        """
        secret = self.webhook_secret or self.secret_key
        if not secret or not signature_header:
            return False

        computed = hmac.new(
            secret.encode("utf-8"),
            msg=payload_bytes,
            digestmod=hashlib.sha512
        ).hexdigest()

        return hmac.compare_digest(computed.lower(), signature_header.strip().lower())


# Factory helper
def get_payment_gateway(gateway_name: str = "paystack") -> PaymentGateway:
    gw = gateway_name.lower().strip()
    if gw == "paystack":
        return PaystackGateway()
    raise NotImplementedError(f"Gateway '{gateway_name}' is not currently supported.")
