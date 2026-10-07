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

    def list_banks(self) -> list[dict]:
        """The provider's bank catalogue: ``[{code, name, slug, currency}]``.

        Returns ``[]`` when the provider cannot be reached or no live key is
        configured. Callers must treat an empty list as "no verified catalogue"
        and fall back to free-text entry rather than blocking the form — the
        same approach core.reference_data takes for subdivisions.
        """
        return []

    def create_subaccount(self, *, business_name, bank_code, account_number,
                          percentage_charge=0) -> str | None:
        """Create a settlement subaccount and return its code.

        Unlike ``list_banks`` this raises rather than returning a soft default
        for an unsupported provider: a missing catalogue degrades to free text,
        but a silently absent subaccount would leave a society believing its
        collections settle to its own bank when they do not.

        Returns ``None`` only when no live key is configured, so callers can
        distinguish "not configured" from "failed".
        """
        raise NotImplementedError

    def fetch_transaction(self, reference) -> dict | None:
        """Authoritative detail for one transaction.

        ``verify_transaction`` answers only "did it succeed", which is not
        enough to book a wallet top-up: the provider deducts its fee before the
        money reaches the balance, so the ledger needs the **actual** fee and
        settled amount, not the amount we asked for.

        Returns ``{"success": bool, "status": str, "amount": Decimal,
        "fee": Decimal}`` in major units, or ``None`` when the reference is
        unknown. ``status`` is the provider's own word for the charge
        ("success", "failed", "abandoned", ...), so a caller can tell a charge
        that definitively failed from one that simply has not completed yet.
        """
        raise NotImplementedError

    # ── Outbound transfers ──────────────────────────────────────────────────
    # Everything above moves money *in*. These move it out, and they all raise
    # NotImplementedError on a provider that cannot: an absent payout rail must
    # fail loudly rather than look like a silent success.

    def resolve_account(self, *, account_number, bank_code) -> str | None:
        """The account holder's name, per the bank. ``None`` if unresolvable.

        Used to show an officer whose account they are about to pay before the
        money leaves, which is the only check that catches a transposed digit
        in an otherwise valid account number.
        """
        raise NotImplementedError

    def create_transfer_recipient(self, *, name, account_number,
                                  bank_code) -> str | None:
        """Register a destination and return its recipient code."""
        raise NotImplementedError

    def initiate_transfer(self, *, amount, recipient_code, reference,
                          reason="") -> dict:
        """Send money. Returns ``{"status", "transfer_code", "raw"}``.

        ``reference`` is the idempotency key: re-sending the same one must not
        move money twice.
        """
        raise NotImplementedError

    def fetch_transfer(self, reference) -> dict | None:
        """The provider's current view of one transfer.

        This is what a "recheck" asks: a webhook can be missed or delayed, so a
        payout may sit pending here while the money has already left.
        """
        raise NotImplementedError

    def balance(self):
        """Funds available to transfer, in major units, or ``None``."""
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

    def list_banks(self) -> list[dict]:
        """Paystack's bank list for the default currency.

        Unauthenticated-ish in effect (the endpoint is public) but still sent
        with the key so it behaves consistently with every other call here.
        Returns ``[]`` on the dev placeholder key or any failure, so a caller
        falls back to free text rather than surfacing an outage as a broken
        form.
        """
        secret = settings.PAYSTACK_SECRET_KEY
        if not secret or secret.startswith("sk_test_dev"):
            return []

        try:
            resp = requests.get(
                f"{settings.PAYSTACK_BASE_URL}/bank",
                params={"currency": settings.DEFAULT_CURRENCY,
                        "perPage": 500},
                headers={"Authorization": f"Bearer {secret}"},
                timeout=15,
            )
            resp.raise_for_status()
            body = resp.json()
        except (requests.RequestException, ValueError):
            # Deliberately swallowed: refreshing a catalogue is housekeeping,
            # and the caller's contract is "empty means no verified list".
            return []

        if not body.get("status"):
            return []
        return [row for row in (body.get("data") or []) if isinstance(row, dict)]

    def create_subaccount(self, *, business_name, bank_code, account_number,
                          percentage_charge=0) -> str | None:
        """Create a Paystack subaccount so collections settle to the society.

        ``percentage_charge`` comes from settings and defaults to 0 — see
        PAYSTACK_SUBACCOUNT_PERCENTAGE for why that default is deliberate.

        Paystack verifies the account number against the bank code, so a wrong
        pairing fails here with the provider's own message rather than silently
        creating a subaccount that never settles.
        """
        secret = settings.PAYSTACK_SECRET_KEY
        if not secret or secret.startswith("sk_test_dev"):
            return None

        payload = {
            "business_name": business_name,
            "settlement_bank": bank_code,
            "account_number": account_number,
            "percentage_charge": percentage_charge,
        }
        try:
            resp = requests.post(
                f"{settings.PAYSTACK_BASE_URL}/subaccount",
                json=payload,
                headers={"Authorization": f"Bearer {secret}"},
                timeout=15,
            )
            resp.raise_for_status()
            body = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise PaymentInitError(
                f"Paystack subaccount creation failed: {exc}") from exc

        if not body.get("status"):
            raise PaymentInitError(
                body.get("message", "Paystack rejected the subaccount."))
        return (body.get("data") or {}).get("subaccount_code")

    def fetch_transaction(self, reference) -> dict | None:
        """Amount and fee actually charged, for booking a wallet top-up.

        With the dev placeholder key there is no live Paystack, so this reports
        success with a zero fee — matching ``verify_transaction``'s dev
        behaviour so the end-to-end top-up flow stays demoable. A zero fee is
        also the only honest stand-in: inventing one would put a fabricated
        expense in the ledger.
        """
        secret = settings.PAYSTACK_SECRET_KEY
        if not secret or secret.startswith("sk_test_dev"):
            return {"success": True, "status": "success", "amount": None,
                    "fee": Decimal("0.00")}

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

        if not body.get("status"):
            return None

        data = body.get("data") or {}
        hundred = Decimal("100")
        return {
            "success": data.get("status") == "success",
            "status": str(data.get("status") or "").lower(),
            # Paystack reports both in kobo.
            "amount": Decimal(str(data.get("amount", 0))) / hundred,
            "fee": Decimal(str(data.get("fees") or 0)) / hundred,
        }


    # ── Outbound transfers ──────────────────────────────────────────────────
    def _dev_key(self) -> bool:
        secret = settings.PAYSTACK_SECRET_KEY
        return not secret or secret.startswith("sk_test_dev")

    def _get(self, path, params=None):
        resp = requests.get(
            f"{settings.PAYSTACK_BASE_URL}{path}",
            params=params,
            headers={"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}"},
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, path, payload):
        resp = requests.post(
            f"{settings.PAYSTACK_BASE_URL}{path}",
            json=payload,
            headers={"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}"},
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()

    def resolve_account(self, *, account_number, bank_code) -> str | None:
        """Ask the bank who owns this account.

        Returns ``None`` rather than raising when the pairing cannot be
        resolved: a name check is advisory, and failing it should warn an
        officer, not block a payout the provider itself would accept.
        """
        if self._dev_key():
            return None
        try:
            body = self._get("/bank/resolve",
                             {"account_number": account_number,
                              "bank_code": bank_code})
        except (requests.RequestException, ValueError):
            return None
        if not body.get("status"):
            return None
        return (body.get("data") or {}).get("account_name") or None

    def create_transfer_recipient(self, *, name, account_number,
                                  bank_code) -> str | None:
        if self._dev_key():
            return None
        try:
            body = self._post("/transferrecipient", {
                "type": "nuban",
                "name": name,
                "account_number": account_number,
                "bank_code": bank_code,
                "currency": settings.DEFAULT_CURRENCY,
            })
        except (requests.RequestException, ValueError) as exc:
            raise PaymentInitError(
                f"Paystack recipient creation failed: {exc}") from exc
        if not body.get("status"):
            raise PaymentInitError(
                body.get("message", "Paystack rejected the recipient."))
        return (body.get("data") or {}).get("recipient_code")

    def initiate_transfer(self, *, amount, recipient_code, reference,
                          reason="") -> dict:
        """Send money from the Paystack balance to a registered recipient.

        With the dev placeholder key this reports a simulated success, matching
        ``verify_transaction``'s dev behaviour so the end-to-end flow stays
        demoable without live credentials. The ``simulated`` flag is recorded on
        the payout so a demo payout is never mistaken for a real one.

        Note the provider's "success" here means *accepted*, not settled —
        Paystack may still fail or reverse a transfer afterwards, which is why
        payouts are rechecked rather than trusted once.
        """
        if self._dev_key():
            return {"status": "success", "transfer_code": "",
                    "raw": {"simulated": True}}

        try:
            body = self._post("/transfer", {
                "source": "balance",
                "amount": int((Decimal(str(amount)) * 100).to_integral_value()),
                "recipient": recipient_code,
                "reference": reference,
                "reason": reason or "Cooperative payout",
            })
        except (requests.RequestException, ValueError) as exc:
            raise PaymentInitError(f"Paystack transfer failed: {exc}") from exc
        if not body.get("status"):
            raise PaymentInitError(
                body.get("message", "Paystack rejected the transfer."))

        data = body.get("data") or {}
        return {"status": data.get("status") or "pending",
                "transfer_code": data.get("transfer_code") or "",
                "raw": data}

    def fetch_transfer(self, reference) -> dict | None:
        if self._dev_key():
            return None
        try:
            body = self._get(f"/transfer/verify/{reference}")
        except (requests.RequestException, ValueError) as exc:
            raise PaymentInitError(
                f"Paystack transfer lookup failed: {exc}") from exc
        if not body.get("status"):
            return None
        data = body.get("data") or {}
        return {"status": data.get("status") or "", "raw": data}

    def balance(self):
        if self._dev_key():
            return None
        try:
            body = self._get("/balance")
        except (requests.RequestException, ValueError):
            return None
        rows = body.get("data") or []
        for row in rows:
            if row.get("currency") == settings.DEFAULT_CURRENCY:
                return Decimal(str(row.get("balance", 0))) / Decimal("100")
        return None


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
