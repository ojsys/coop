"""
Provider abstraction over Paystack and Flutterwave.

Each provider knows how to (a) verify that a webhook really came from the PSP
and (b) normalise its payload into a common :class:`NormalizedEvent`, so the
ingestion/reconciliation code is provider-agnostic (PRD §7 "provider
abstraction layer").
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from decimal import Decimal

import requests
from django.conf import settings

from payments.models import Provider


@dataclass
class NormalizedEvent:
    provider: str
    event_id: str          # stable id used for idempotency
    reference: str         # the transaction reference we reconcile against
    amount: Decimal        # in major units (Naira), not kobo
    currency: str
    subaccount_code: str   # routes to the cooperative (tenant)
    success: bool          # False → non-success event, will be ignored


class WebhookVerificationError(Exception):
    """Raised when a webhook signature does not verify."""


class PaymentInitError(Exception):
    """Raised when a checkout could not be created with the provider."""


class BaseProvider:
    name: str

    def verify(self, raw_body: bytes, headers) -> None:
        raise NotImplementedError

    def normalize(self, payload: dict) -> NormalizedEvent:
        raise NotImplementedError

    def initialize_transaction(self, *, email, amount, reference,
                               subaccount_code=None, callback_url=None):
        """Create a hosted checkout and return its authorization URL (or ``None``
        when no live credentials are configured — dev/test)."""
        raise NotImplementedError

    def verify_transaction(self, reference) -> bool:
        """Return ``True`` if the transaction for ``reference`` succeeded."""
        raise NotImplementedError


class PaystackProvider(BaseProvider):
    name = Provider.PAYSTACK

    def verify(self, raw_body: bytes, headers) -> None:
        # Paystack signs the raw body with HMAC-SHA512 using the secret key.
        signature = headers.get("x-paystack-signature", "")
        expected = hmac.new(
            settings.PAYSTACK_SECRET_KEY.encode(), raw_body, hashlib.sha512,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise WebhookVerificationError("Invalid Paystack signature.")

    def normalize(self, payload: dict) -> NormalizedEvent:
        data = payload.get("data", {})
        return NormalizedEvent(
            provider=self.name,
            event_id=str(data.get("id") or data.get("reference")),
            reference=str(data.get("reference", "")),
            # Paystack amounts are in kobo.
            amount=(Decimal(str(data.get("amount", 0))) / Decimal("100")),
            currency=data.get("currency", settings.DEFAULT_CURRENCY),
            subaccount_code=(data.get("subaccount") or {}).get(
                "subaccount_code", "",
            ) if isinstance(data.get("subaccount"), dict)
            else str(data.get("subaccount", "")),
            success=payload.get("event") == "charge.success"
            and data.get("status") == "success",
        )

    def initialize_transaction(self, *, email, amount, reference,
                               subaccount_code=None, callback_url=None):
        secret = settings.PAYSTACK_SECRET_KEY
        # No live key configured (the dev placeholder) → skip the network call so
        # dev and tests still create the PENDING contribution and can be
        # confirmed via a simulated webhook. Returns None (no checkout URL).
        if not secret or secret.startswith("sk_test_dev"):
            return None

        # Paystack works in kobo; route the settlement to the coop's subaccount.
        payload = {
            "email": email,
            "amount": int((Decimal(str(amount)) * 100).to_integral_value()),
            "reference": reference,
            "currency": settings.DEFAULT_CURRENCY,
        }
        if subaccount_code:
            payload["subaccount"] = subaccount_code
        if callback_url:
            payload["callback_url"] = callback_url

        try:
            resp = requests.post(
                f"{settings.PAYSTACK_BASE_URL}/transaction/initialize",
                json=payload,
                headers={"Authorization": f"Bearer {secret}"},
                timeout=15,
            )
            resp.raise_for_status()
            body = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise PaymentInitError(f"Paystack initialize failed: {exc}") from exc

        if not body.get("status"):
            raise PaymentInitError(
                body.get("message", "Paystack rejected the transaction."))
        return body.get("data", {}).get("authorization_url")

    def verify_transaction(self, reference) -> bool:
        secret = settings.PAYSTACK_SECRET_KEY
        # DEV ONLY: with the placeholder key there is no live Paystack, so treat
        # the charge as successful — this makes the end-to-end flow demoable
        # (member taps Pay → confirmed) without real credentials. A real key
        # performs an authoritative server-side verification instead.
        if not secret or secret.startswith("sk_test_dev"):
            return True

        try:
            resp = requests.get(
                f"{settings.PAYSTACK_BASE_URL}/transaction/verify/{reference}",
                headers={"Authorization": f"Bearer {secret}"},
                timeout=15,
            )
            resp.raise_for_status()
            body = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise PaymentInitError(f"Paystack verify failed: {exc}") from exc

        data = body.get("data", {})
        return bool(body.get("status")) and data.get("status") == "success"


class FlutterwaveProvider(BaseProvider):
    name = Provider.FLUTTERWAVE

    def verify(self, raw_body: bytes, headers) -> None:
        # Flutterwave sends a static secret hash in the verif-hash header.
        received = headers.get("verif-hash", "")
        if not hmac.compare_digest(settings.FLUTTERWAVE_SECRET_HASH, received):
            raise WebhookVerificationError("Invalid Flutterwave verif-hash.")

    def normalize(self, payload: dict) -> NormalizedEvent:
        data = payload.get("data", {})
        return NormalizedEvent(
            provider=self.name,
            event_id=str(data.get("id") or data.get("tx_ref")),
            reference=str(data.get("tx_ref", "")),
            amount=Decimal(str(data.get("amount", 0))),  # major units already
            currency=data.get("currency", settings.DEFAULT_CURRENCY),
            subaccount_code=str(data.get("subaccount_id", "")),
            success=(payload.get("event") in {"charge.completed", None})
            and str(data.get("status", "")).lower() == "successful",
        )


_PROVIDERS = {
    Provider.PAYSTACK: PaystackProvider(),
    Provider.FLUTTERWAVE: FlutterwaveProvider(),
}


def get_provider(name: str) -> BaseProvider:
    try:
        return _PROVIDERS[name]
    except KeyError as exc:
        raise ValueError(f"Unknown payment provider: {name!r}") from exc
