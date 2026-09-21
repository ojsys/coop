"""
Webhook ingestion + the reconciliation engine.

Ingestion is idempotent (dedup on provider+event_id) and signature-verified.
Reconciliation matches each settlement to an expected (pending) contribution
and classifies the outcome so exceptions surface for manual resolution:

    matched    — settlement confirmed a pending contribution (posts to ledger)
    duplicate  — the contribution was already confirmed/settled
    partial    — reference matched but the amount differs
    unmatched  — no expected contribution for this reference
    ignored    — a non-success event
"""
from __future__ import annotations

import json

from django.db import transaction

from django.conf import settings

from contributions.models import Contribution
from contributions.services import confirm_contribution
from payments.models import PaymentEvent, Provider, ProviderAccount
from payments.providers import (
    NormalizedEvent, WebhookVerificationError, get_provider,
)


class WebhookError(Exception):
    pass


def initialize_payment(contribution, *, email, callback_url=None):
    """Start a Paystack checkout for a PENDING contribution.

    Routes settlement to the cooperative's own Paystack subaccount and reuses the
    contribution's ``psp_reference`` so the eventual ``charge.success`` webhook
    reconciles back to this exact contribution. Returns the checkout URL to send
    the payer to (``None`` in dev / when no live key is configured).
    """
    subaccount = None
    if settings.PAYSTACK_USE_SUBACCOUNT:
        account = (
            ProviderAccount.all_objects
            .filter(cooperative_id=contribution.cooperative_id,
                    provider=Provider.PAYSTACK, connected=True)
            .first()
        )
        subaccount = account.subaccount_code if account else None

    provider = get_provider(Provider.PAYSTACK)
    return provider.initialize_transaction(
        email=email,
        amount=contribution.amount,
        reference=contribution.psp_reference,
        subaccount_code=subaccount,
        callback_url=callback_url or (settings.PAYSTACK_CALLBACK_URL or None),
    )


def initialize_loan_payment(repayment, *, email, callback_url=None):
    """Start a Paystack checkout for a PENDING loan repayment.

    Routes settlement to the cooperative's own subaccount and reuses the
    repayment's ``psp_reference`` so ``verify_loan_payment`` can settle it.
    Returns the checkout URL (``None`` in dev / when no live key is configured).
    """
    subaccount = None
    if settings.PAYSTACK_USE_SUBACCOUNT:
        account = (
            ProviderAccount.all_objects
            .filter(cooperative_id=repayment.cooperative_id,
                    provider=Provider.PAYSTACK, connected=True)
            .first()
        )
        subaccount = account.subaccount_code if account else None

    provider = get_provider(Provider.PAYSTACK)
    return provider.initialize_transaction(
        email=email,
        amount=repayment.amount,
        reference=repayment.psp_reference,
        subaccount_code=subaccount,
        callback_url=callback_url or (settings.PAYSTACK_CALLBACK_URL or None),
    )


def verify_loan_payment(cooperative, reference):
    """Verify a Paystack loan repayment and, on success, settle the matching
    PENDING repayment — posting it to the ledger immediately. Idempotent.

    Returns the settled :class:`LoanRepayment` (or the pending one unchanged if
    the charge did not succeed, or ``None`` if there is nothing to settle)."""
    from loans.models import LoanRepayment
    from loans.services import confirm_loan_repayment

    repayment = (
        LoanRepayment.all_objects
        .filter(cooperative=cooperative, psp_reference=reference)
        .first()
    )
    if repayment is None:
        return None
    if repayment.status == LoanRepayment.Status.CONFIRMED:
        return repayment

    provider = get_provider(Provider.PAYSTACK)
    if provider.verify_transaction(reference):
        return confirm_loan_repayment(repayment)
    return repayment


def verify_payment(cooperative, reference):
    """Verify a Paystack payment and, on success, confirm the matching PENDING
    contribution — posting it to the ledger *immediately* so the member, admin
    console and platform all see it at once (no waiting on the webhook).

    Idempotent: an already-confirmed contribution is returned unchanged, and the
    later ``charge.success`` webhook is a harmless no-op. Returns the contribution
    (still PENDING if the charge did not succeed).
    """
    contribution = (
        Contribution.all_objects
        .filter(cooperative=cooperative, psp_reference=reference)
        .first()
    )
    if contribution is None:
        return None
    if contribution.status == Contribution.Status.CONFIRMED:
        return contribution

    provider = get_provider(Provider.PAYSTACK)
    if provider.verify_transaction(reference):
        return confirm_contribution(contribution)
    return contribution


def _resolve_cooperative(event: NormalizedEvent):
    if not event.subaccount_code:
        return None
    account = ProviderAccount.all_objects.filter(
        provider=event.provider, subaccount_code=event.subaccount_code,
    ).select_related("cooperative").first()
    return account.cooperative if account else None


@transaction.atomic
def ingest_webhook(*, provider_name: str, raw_body: bytes, headers) -> PaymentEvent:
    """Verify, deduplicate, persist, and reconcile an inbound PSP webhook.

    Returns the (existing or new) :class:`PaymentEvent`. Raises
    :class:`payments.providers.WebhookVerificationError` on a bad signature.
    """
    provider = get_provider(provider_name)
    provider.verify(raw_body, headers)  # raises on tamper

    payload = json.loads(raw_body.decode() or "{}")
    event = provider.normalize(payload)

    # Idempotency — a replayed event returns the original record untouched.
    existing = PaymentEvent.all_objects.filter(
        provider=event.provider, event_id=event.event_id,
    ).first()
    if existing is not None:
        return existing

    cooperative = _resolve_cooperative(event)
    payment = PaymentEvent(
        cooperative=cooperative,
        provider=event.provider,
        event_id=event.event_id,
        reference=event.reference,
        amount=event.amount,
        currency=event.currency,
        payload=payload,
    )

    if cooperative is None:
        payment.status = PaymentEvent.Status.UNMATCHED
        payment.note = "Unknown subaccount — cannot route to a cooperative."
        payment.save()
        return payment

    if not event.success:
        payment.status = PaymentEvent.Status.IGNORED
        payment.note = "Non-success event."
        payment.save()
        return payment

    payment.save()
    reconcile_event(payment)

    from audit.services import record_action
    record_action(
        cooperative=cooperative, actor_label="System",
        action=f"Ingested PSP settlement {payment.reference} ({payment.status})",
        entity=payment,
    )
    return payment


@transaction.atomic
def reconcile_event(payment: PaymentEvent) -> PaymentEvent:
    """Match a received settlement to an expected contribution and classify it."""
    contribution = Contribution.all_objects.filter(
        cooperative=payment.cooperative, psp_reference=payment.reference,
    ).first()

    if contribution is None:
        payment.status = PaymentEvent.Status.UNMATCHED
        payment.note = "No expected contribution for this reference."
    elif contribution.status == Contribution.Status.CONFIRMED:
        payment.status = PaymentEvent.Status.DUPLICATE
        payment.matched_contribution = contribution
        payment.note = "Contribution already settled."
    elif contribution.status == Contribution.Status.REVERSED:
        payment.status = PaymentEvent.Status.UNMATCHED
        payment.note = "Matching contribution was reversed."
    elif payment.amount != contribution.amount:
        payment.status = PaymentEvent.Status.PARTIAL
        payment.matched_contribution = contribution
        payment.note = (
            f"Amount mismatch: settled {payment.amount} vs "
            f"expected {contribution.amount}."
        )
    else:
        confirm_contribution(contribution)
        payment.status = PaymentEvent.Status.MATCHED
        payment.matched_contribution = contribution
        payment.note = ""

    payment.save(update_fields=["status", "matched_contribution", "note",
                                "updated_at"])
    return payment


def reconciliation_summary(cooperative, *, since=None, until=None) -> dict:
    """Aggregate reconciliation health for a cooperative (dashboard §6.3)."""
    qs = PaymentEvent.all_objects.filter(cooperative=cooperative)
    if since is not None:
        qs = qs.filter(received_at__gte=since)
    if until is not None:
        qs = qs.filter(received_at__lte=until)

    counts = {status: 0 for status, _ in PaymentEvent.Status.choices}
    for row in qs.values("status"):
        counts[row["status"]] += 1

    ingested = sum(counts.values())
    matched = counts[PaymentEvent.Status.MATCHED]
    exceptions = (
        counts[PaymentEvent.Status.UNMATCHED]
        + counts[PaymentEvent.Status.PARTIAL]
        + counts[PaymentEvent.Status.DUPLICATE]
    )
    # Denominator excludes ignored (non-success) events.
    considered = ingested - counts[PaymentEvent.Status.IGNORED]
    accuracy = (matched / considered * 100) if considered else 100.0

    return {
        "ingested": ingested,
        "matched": matched,
        "exceptions": exceptions,
        "match_accuracy": round(accuracy, 2),
        "by_status": counts,
    }
