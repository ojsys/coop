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
    NormalizedEvent, PaymentInitError, WebhookVerificationError, get_provider,
)


class WebhookError(Exception):
    pass


class SubaccountError(Exception):
    """A cooperative's settlement subaccount could not be created."""


class WalletError(Exception):
    """A wallet top-up could not be initiated or confirmed."""


class PayoutError(Exception):
    """Money could not be sent out."""


# ── What the provider says actually happened ─────────────────────────────────
# A transfer being *accepted* is not the same as it settling. Paystack can fail
# or reverse one afterwards, and its webhook can be missed or delayed — so a
# payout is never trusted once. Two things ask the provider: the webhook below,
# and an officer's explicit recheck. Both funnel through
# ``_apply_transfer_status`` so they cannot disagree.

_FINAL_STATUSES = ("success", "failed", "reversed")


@transaction.atomic
def _apply_transfer_status(payout, reported, *, raw=None, actor=None):
    """Move a payout to what the provider reports, compensating on failure.

    Idempotent, and deliberately so: Paystack resends webhooks, an officer may
    recheck twice, and a webhook may arrive while a recheck is in flight. A
    payout that has already reached a final state only records the newer payload
    and returns.

    On failure the wallet is made whole by reversing the payout's journal — the
    wallet was debited when the transfer was initiated, not when it settled —
    and the thing being paid for is put back too (see _compensate).
    """
    from django.utils import timezone

    from audit.services import record_action
    from ledger.services import reverse_journal
    from payments.models import Payout

    reported = (reported or "").strip().lower()
    if raw is not None:
        payout.payload = raw

    if payout.status in (Payout.Status.SUCCESS, Payout.Status.FAILED,
                         Payout.Status.REVERSED):
        # Already final. Record what we were told and leave it alone — never
        # reverse twice, never un-fail something.
        payout.save(update_fields=["payload", "updated_at"])
        return payout

    if reported == "success":
        payout.status = Payout.Status.SUCCESS
        payout.settled_at = timezone.now()
        payout.save(update_fields=["status", "settled_at", "payload",
                                   "updated_at"])
        record_action(
            cooperative=payout.cooperative, actor=actor,
            actor_label="" if actor else "System",
            action="payment.payout_settled", entity=payout,
            after={"reference": payout.reference, "amount": str(payout.amount)},
        )
        return payout

    if reported in ("failed", "reversed"):
        if payout.journal_id and not payout.reversal_journal_id:
            payout.reversal_journal = reverse_journal(
                payout.journal, created_by=actor,
                memo=f"Payout {payout.reference} {reported}")
        payout.status = (Payout.Status.REVERSED if reported == "reversed"
                         else Payout.Status.FAILED)
        payout.failure_reason = (
            str((raw or {}).get("message") or (raw or {}).get("gateway_response")
                or reported)[:255])
        payout.save(update_fields=["status", "reversal_journal",
                                   "failure_reason", "payload", "updated_at"])
        _compensate(payout, actor=actor)
        record_action(
            cooperative=payout.cooperative, actor=actor,
            actor_label="" if actor else "System",
            action="payment.payout_failed", entity=payout,
            after={"reference": payout.reference, "status": payout.status,
                   "reason": payout.failure_reason},
        )
        return payout

    # Still in flight (pending / otherwise). Record the payload, change nothing.
    payout.save(update_fields=["payload", "updated_at"])
    return payout


def _compensate(payout, *, actor=None):
    """Undo whatever the failed payout was paying for.

    Reversing the journal returns the money to the wallet, but the *thing* being
    paid also has to go back: a loan marked disbursed when no money arrived
    would start accruing a repayment schedule against a member who received
    nothing. A wallet withdrawal needs nothing beyond the reversal — the funds
    simply never left.
    """
    from payments.models import Payout

    if payout.kind != Payout.Kind.LOAN or not payout.object_id:
        return

    from loans.models import Loan
    from loans.services import revert_disbursement

    loan = Loan.all_objects.filter(pk=payout.object_id).first()
    if loan is not None:
        revert_disbursement(loan, reason=payout.failure_reason, actor=actor)


def handle_transfer_event(*, provider_name, payload):
    """Apply an inbound ``transfer.*`` webhook to its payout.

    Branched out of ``ingest_webhook`` before normalisation because the inbound
    path cannot describe this: ``normalize`` sets ``success`` only for
    ``charge.success`` and routes to a tenant by settlement subaccount, and a
    transfer has neither. Forced through it, every transfer webhook would be
    filed as "non-success event, ignored".
    """
    from payments.models import PaymentEvent, Payout

    data = payload.get("data") or {}
    reference = str(data.get("reference") or "")
    payout = (Payout.all_objects
              .filter(provider=provider_name, reference=reference).first()
              if reference else None)

    if payout is None:
        # Recorded rather than dropped: a transfer we cannot match is exactly
        # the kind of thing someone needs to find later.
        from decimal import Decimal

        event_id = str(data.get("id") or reference or "unknown")
        existing = PaymentEvent.all_objects.filter(
            provider=provider_name, event_id=event_id).first()
        if existing is not None:
            return existing
        return PaymentEvent.all_objects.create(
            provider=provider_name, event_id=event_id, reference=reference,
            amount=(Decimal(str(data.get("amount", 0))) / Decimal("100")),
            status=PaymentEvent.Status.UNMATCHED,
            note="Transfer webhook matches no payout on this platform.",
            payload=payload,
        )

    return _apply_transfer_status(
        payout,
        data.get("status") or payload.get("event", "").split(".")[-1],
        raw=data,
    )


def recheck_payout(payout, *, actor=None):
    """Ask the provider what really happened to a payout.

    This is the answer to "it still says pending but the money has gone". A
    webhook can be missed or delayed; this asks directly and reconciles, using
    the same code path as the webhook so the two cannot disagree.

    Returns the payout unchanged when the provider has nothing to say about the
    reference — including on the dev placeholder key, where there is no live
    provider to ask.
    """
    detail = get_provider(payout.provider).fetch_transfer(payout.reference)
    if detail is None:
        return payout
    return _apply_transfer_status(payout, detail.get("status"),
                                  raw=detail.get("raw"), actor=actor)


@transaction.atomic
def withdraw_wallet(cooperative, *, amount, actor=None, reason=""):
    """Send money from the disbursement wallet back to the society's own bank.

    This is the promise the wallet rests on: the float a society deposits is its
    own money, and it can take it back. Without this the platform would be a
    one-way door, and the public wording about holding only a withdrawable float
    would not be true.

    One privileged officer, not maker-checker, and the reasoning matters: the
    only possible destination is the society's *own* collection account, which
    already took two officers to set (see
    tenants.services.propose_bank_detail_change). A single officer can therefore
    move the society's money to the society's own bank and nowhere else — the
    dual control is on the destination, not on this transfer. It also matches
    the rule used throughout: approval is for money *leaving* the cooperative,
    and this is money coming back.
    """
    from payments.models import Payout

    if not (cooperative.bank_code and cooperative.bank_account_no):
        raise PayoutError(
            "This cooperative has no bank code and account number on file, so "
            "there is nowhere to withdraw to. Add them under Settings \u2192 "
            "Collection account \u2014 a second officer approves the change."
        )

    # Money returns to the society's own bank, so the asset it lands in is 1010
    # Bank / PSP Settlement. The wallet (1020) is credited by send_payout.
    _, _, settlement = _wallet_accounts(cooperative)

    return send_payout(
        cooperative=cooperative,
        amount=amount,
        debit_account=settlement,
        kind=Payout.Kind.WALLET_WITHDRAWAL,
        destination_bank_name=cooperative.bank_name,
        destination_bank_code=cooperative.bank_code,
        destination_account_no=cooperative.bank_account_no,
        account_name=cooperative.bank_account_name or cooperative.name,
        reason=reason or "Disbursement wallet withdrawal",
        actor=actor,
    )


@transaction.atomic
def send_payout(*, cooperative, amount, debit_account, kind, object_id=None,
                destination_bank_name="", destination_bank_code="",
                destination_account_no="", account_name="", reason="",
                actor=None, membership=None, extra_lines=None,
                provider_name=Provider.PAYSTACK):
    """Send money out of the society's disbursement wallet. Returns the Payout.

    ``extra_lines`` are balanced ledger lines posted in the *same* journal as
    the payout — e.g. a loan's application fee, which is booked with the
    disbursement so that reversing a failed payout reverses the fee too.

    Every outbound payment goes through here — a loan disbursement, a society
    withdrawing its own wallet, later a savings payout — so the wallet check,
    the ledger posting and the audit trail cannot be forgotten by one caller.

    The journal is posted **when the transfer is initiated**, not when it
    settles:

        debit  <debit_account>              amount
        credit 1020 Disbursement Wallet     amount

    If the wallet were only debited on success, two payouts could each pass the
    balance check and together overdraw the real balance at the provider. A
    later failure reverses this journal rather than preventing it.

    Everything happens in one transaction with the provider call inside it, so a
    rejection rolls back cleanly and leaves no orphan journal. The residual
    risk is the reverse: the provider accepts and a local write then fails,
    leaving money gone with no record. That is exactly what the recheck action
    exists to find, and why ``reference`` is the provider's idempotency key.
    """
    from decimal import Decimal, InvalidOperation

    from django.utils import timezone

    from audit.services import record_action
    from ledger.services import Line, post_journal
    from payments.models import Payout

    try:
        amount = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    except (InvalidOperation, TypeError) as exc:
        raise PayoutError("Enter a valid amount.") from exc
    if amount <= Decimal("0"):
        raise PayoutError("A payout amount must be positive.")

    if not (destination_bank_code and destination_account_no):
        raise PayoutError(
            "This payout has no bank code and account number on file, so it "
            "cannot be sent electronically. Ask for the bank to be re-picked "
            "from the list, or pay it in cash."
        )

    available = wallet_balance(cooperative)
    if amount > available:
        raise PayoutError(
            f"The disbursement wallet holds {available:,.2f}, so {amount:,.2f} "
            f"cannot be sent. Fund the wallet first."
        )

    wallet, _, _ = _wallet_accounts(cooperative)
    if debit_account.cooperative_id != cooperative.id:
        raise PayoutError("That account belongs to another cooperative.")

    import secrets

    reference = f"PO-{cooperative.id}-{secrets.token_hex(6).upper()}"
    payout = Payout.all_objects.create(
        cooperative=cooperative,
        kind=kind,
        object_id=object_id,
        amount=amount,
        status=Payout.Status.QUEUED,
        destination_bank_name=destination_bank_name,
        destination_bank_code=destination_bank_code,
        destination_account_no=destination_account_no,
        destination_account_name=account_name,
        provider=provider_name,
        reference=reference,
        reason=reason,
        requested_by=actor,
    )

    provider = get_provider(provider_name)
    recipient = provider.create_transfer_recipient(
        name=account_name or destination_account_no,
        account_number=destination_account_no,
        bank_code=destination_bank_code,
    )
    result = provider.initiate_transfer(
        amount=amount, recipient_code=recipient, reference=reference,
        reason=reason,
    )

    journal = post_journal(
        cooperative=cooperative,
        reference=reference,
        memo=reason or f"{payout.get_kind_display()} {reference}",
        created_by=actor,
        lines=[
            # `membership` matters: the member statement is built from ledger
            # entries tagged with it, so a loan disbursed electronically would
            # be missing from the borrower's own statement without this, while
            # a cash one appeared.
            Line(account=debit_account, debit=amount, membership=membership,
                 description=reason or ""),
            Line(account=wallet, credit=amount,
                 description=f"Payout {reference}"),
            *(extra_lines or []),
        ],
    )

    reported = (result.get("status") or "pending").lower()
    payout.recipient_code = recipient or ""
    payout.transfer_code = result.get("transfer_code") or ""
    payout.payload = result.get("raw") or {}
    payout.journal = journal
    payout.sent_at = timezone.now()
    # "success" from the provider means *accepted*, and for a simulated dev
    # transfer it means nothing left at all. Either way a transfer can still
    # fail or reverse afterwards, so it is recorded as settled only when the
    # provider says so, and rechecked rather than trusted.
    if reported == "success":
        payout.status = Payout.Status.SUCCESS
        payout.settled_at = timezone.now()
    elif reported in ("failed", "reversed"):
        payout.status = Payout.Status.FAILED
        payout.failure_reason = str(result.get("raw") or "")[:255]
    else:
        payout.status = Payout.Status.PENDING
    payout.save()

    record_action(
        cooperative=cooperative, actor=actor,
        action="payment.payout_sent", entity=payout,
        after={"reference": reference, "amount": str(amount),
               "kind": kind, "status": payout.status,
               "destination": payout.describe()},
    )
    return payout


# ── The disbursement wallet ─────────────────────────────────────────────────
# Money a society deliberately deposits with the platform so it can lend
# electronically. It is the only money the platform holds: collections settle to
# each society's own bank through its subaccount (see ensure_subaccount), and
# this balance is the society's own.
#
# The balance is *derived* from the ledger, never stored — the same rule the
# rest of this project follows. There is no field to drift out of step.

WALLET_ACCOUNT_CODE = "1020"
CHARGES_ACCOUNT_CODE = "5000"


def _wallet_accounts(cooperative):
    """The three accounts a top-up touches, created on first use.

    Lazily get-or-created rather than assumed, because societies provisioned
    before 1020/5000 joined the baseline chart do not have them. Same approach
    as loans.services._account for 1200 Loans Receivable.
    """
    from ledger.models import Account

    def account(code, name, kind):
        obj, _ = Account.all_objects.get_or_create(
            cooperative=cooperative, code=code,
            defaults={"name": name, "kind": kind, "system": True})
        return obj

    return (
        account(WALLET_ACCOUNT_CODE, "Disbursement Wallet",
                Account.Kind.ASSET),
        account(CHARGES_ACCOUNT_CODE, "Payment Charges",
                Account.Kind.EXPENSE),
        account("1010", "Bank / PSP Settlement", Account.Kind.ASSET),
    )


def wallet_account(cooperative):
    """The society's disbursement wallet account."""
    wallet, _, _ = _wallet_accounts(cooperative)
    return wallet


def wallet_balance(cooperative):
    """What the society can currently disburse, derived from the ledger.

    Quantised to two places because the figure is money and is serialised
    straight into API responses. Decimal keeps whatever scale the arithmetic
    produced, so without this the same endpoint returns "49250.00" after one
    top-up and "150000" after a round-numbered payout — the balance looking
    like a different kind of value depending on the history behind it.
    """
    from decimal import Decimal

    from ledger.services import account_balance

    return account_balance(wallet_account(cooperative)).quantize(
        Decimal("0.01"))


def initiate_wallet_topup(cooperative, *, amount, email, actor=None,
                          callback_url=None):
    """Start a checkout that funds the society's disbursement wallet.

    Returns ``(topup, authorization_url)``. The url is ``None`` when no live key
    is configured, exactly as the contribution flow behaves.

    Nothing is posted yet: like a contribution, the journal is written only when
    the charge is confirmed, because until then no money has moved.

    **No subaccount is sent, deliberately.** Every other checkout in this
    project routes settlement to the society's own subaccount; doing that here
    would send the society's money straight back to its own bank instead of the
    platform balance the wallet represents — funding nothing.
    """
    import secrets
    from decimal import Decimal, InvalidOperation

    from core.entitlements import (ELECTRONIC_PAYOUTS, has_feature,
                                   not_included_message)
    from payments.models import WalletTopUp

    # Funding is new use of the wallet, so it follows the plan. Withdrawing
    # what is already there never does: it is the society's own money.
    if not has_feature(cooperative, ELECTRONIC_PAYOUTS):
        raise WalletError(not_included_message(cooperative,
                                               ELECTRONIC_PAYOUTS))

    try:
        amount = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    except (InvalidOperation, TypeError) as exc:
        raise WalletError("Enter a valid amount.") from exc
    if amount <= Decimal("0"):
        raise WalletError("A top-up amount must be positive.")

    reference = f"WLT-{cooperative.id}-{secrets.token_hex(6).upper()}"
    topup = WalletTopUp.all_objects.create(
        cooperative=cooperative,
        amount=amount,
        psp_reference=reference,
        initiated_by=actor,
    )

    provider = get_provider(Provider.PAYSTACK)
    url = provider.initialize_transaction(
        email=email,
        amount=amount,
        reference=reference,
        subaccount_code=None,   # see the docstring — this must stay None
        callback_url=callback_url or (settings.PAYSTACK_CALLBACK_URL or None),
    )
    return topup, url


@transaction.atomic
def confirm_wallet_topup(cooperative, reference, *, actor=None):
    """Confirm a top-up against the provider and credit the wallet. Idempotent.

    Books three legs, because the society pays the gross and the platform
    balance receives the gross *less the provider's fee*:

        debit  1020 Disbursement Wallet    net
        debit  5000 Payment Charges        fee
        credit 1010 Bank / PSP Settlement  gross

    The fee is read from the provider rather than estimated. Crediting the
    wallet with the gross would claim money that never arrived, and the first
    disbursement would fail at the provider for insufficient funds — a failure
    that would surface as a broken loan payout, far from its cause.

    Returns the top-up unchanged when the charge did not succeed, so a cancelled
    checkout leaves a PENDING record and no ledger entry.
    """
    from payments.models import WalletTopUp

    topup = WalletTopUp.all_objects.filter(
        cooperative=cooperative, psp_reference=reference).first()
    if topup is None:
        return None
    return recheck_wallet_topup(topup, actor=actor)


def provider_is_simulated(provider_name=Provider.PAYSTACK) -> bool:
    """True when there is no live key and the provider only pretends.

    With the dev placeholder key ``fetch_transaction`` reports *every* reference
    as paid, which keeps the demo flow working but would credit unpaid top-ups
    if a bulk recheck ran against it. Bulk callers check this first.
    """
    secret = settings.PAYSTACK_SECRET_KEY if provider_name == Provider.PAYSTACK \
        else ""
    return not secret or secret.startswith("sk_test_dev")


@transaction.atomic
def recheck_wallet_topup(topup, *, actor=None):
    """Ask the provider what happened to a top-up, and apply the answer.

    The one path every confirmation takes — the officer's return from checkout,
    the ``charge.success`` webhook, the admin's recheck action and the
    ``recheck_pending_payments`` command — so they cannot disagree about whether
    the money arrived.

    It exists because the return from checkout is the only thing that used to
    confirm a top-up. An officer who paid and then closed the tab, or whose
    connection dropped on the way back, left a PENDING row and an uncredited
    wallet although the provider held the money.

    Only a definitive ``failed`` marks the row FAILED. "abandoned" and the like
    mean the checkout has not completed *yet* — Paystack lets a payer resume it
    — so those stay PENDING and can be rechecked later.

    The row is locked first: a webhook and an officer's verify can arrive
    together, and the wallet must be credited once.
    """
    from decimal import Decimal

    from django.utils import timezone

    from audit.services import record_action
    from ledger.services import Line, post_journal
    from payments.models import WalletTopUp

    topup = WalletTopUp.all_objects.select_for_update().get(pk=topup.pk)
    if topup.status != WalletTopUp.Status.PENDING:
        return topup

    cooperative = topup.cooperative
    reference = topup.psp_reference
    detail = get_provider(topup.provider).fetch_transaction(reference)
    if detail is None:
        return topup
    if not detail.get("success"):
        if detail.get("status") == "failed":
            topup.status = WalletTopUp.Status.FAILED
            topup.save(update_fields=["status", "updated_at"])
            record_action(
                cooperative=cooperative, actor=actor,
                actor_label="" if actor else "System",
                action="wallet.topup_failed", entity=topup,
                after={"reference": reference},
            )
        return topup

    gross = detail.get("amount") or topup.amount
    fee = detail.get("fee") or Decimal("0.00")
    net = gross - fee
    if net <= Decimal("0"):
        raise WalletError(
            f"The provider's fee ({fee}) is not less than the amount paid "
            f"({gross}), so there is nothing to credit.")

    wallet, charges, settlement = _wallet_accounts(cooperative)
    lines = [
        Line(account=wallet, debit=net, description="Wallet funded"),
        Line(account=settlement, credit=gross,
             description=f"Top-up {reference}"),
    ]
    if fee > Decimal("0"):
        lines.insert(1, Line(account=charges, debit=fee,
                             description="Payment provider fee"))

    journal = post_journal(
        cooperative=cooperative,
        reference=f"WLT-{topup.pk}",
        memo=f"Disbursement wallet top-up {reference}",
        created_by=actor or topup.initiated_by,
        lines=lines,
    )

    topup.fee = fee
    topup.net_amount = net
    topup.journal = journal
    topup.status = WalletTopUp.Status.CONFIRMED
    topup.confirmed_at = timezone.now()
    topup.save(update_fields=["fee", "net_amount", "journal", "status",
                              "confirmed_at", "updated_at"])

    record_action(
        cooperative=cooperative, actor=actor or topup.initiated_by,
        action="wallet.topup_confirmed", entity=topup,
        after={"amount": str(gross), "fee": str(fee), "net": str(net),
               "reference": reference},
    )
    return topup


@transaction.atomic
def ensure_subaccount(cooperative, *, actor=None,
                      provider_name=Provider.PAYSTACK):
    """Create the cooperative's settlement subaccount, or return the existing one.

    Returns ``(provider_account, created)``. Idempotent: a society that already
    has a connected account gets it back untouched and the provider is never
    called again, so a double-clicked button cannot create two subaccounts.

    Requires the bank **code**, not just the name — Paystack identifies the
    settlement bank by code, and a name alone cannot create a subaccount. For
    societies whose bank details predate bank selection the code is blank, so
    this refuses and names what is missing rather than failing at the provider.

    Nothing about collections changes until ``PAYSTACK_USE_SUBACCOUNT`` is on:
    ``initialize_payment`` looks up a connected account only then, and falls
    back to the platform account when there is none. So creating a subaccount
    is safe to do per-society, ahead of flipping that switch.
    """
    from audit.services import record_action

    existing = (
        ProviderAccount.all_objects
        .filter(cooperative=cooperative, provider=provider_name,
                connected=True)
        .first()
    )
    if existing is not None:
        return existing, False

    missing = [
        label for label, value in (
            ("bank name", cooperative.bank_name),
            ("bank code", cooperative.bank_code),
            ("account number", cooperative.bank_account_no),
        ) if not value
    ]
    if missing:
        raise SubaccountError(
            "This cooperative's bank details are incomplete — missing "
            f"{', '.join(missing)}. Add them under Settings → Collection "
            "account (a second officer approves the change), then connect the "
            "settlement account."
        )

    code = get_provider(provider_name).create_subaccount(
        business_name=cooperative.bank_account_name or cooperative.name,
        bank_code=cooperative.bank_code,
        account_number=cooperative.bank_account_no,
        percentage_charge=settings.PAYSTACK_SUBACCOUNT_PERCENTAGE,
    )
    if not code:
        # No live key: returning None here is "not configured", not "failed".
        # Recording a row with no real subaccount behind it would tell the
        # society its collections settle to its own bank when they do not.
        raise SubaccountError(
            "No live payment-provider key is configured, so no subaccount was "
            "created. Collections continue settling to the platform account "
            "until this is run against a live key."
        )

    account = ProviderAccount.all_objects.create(
        cooperative=cooperative,
        provider=provider_name,
        subaccount_code=code,
        bank_name=cooperative.bank_name,
        connected=True,
    )
    record_action(
        cooperative=cooperative, actor=actor,
        action="cooperative.subaccount_connected", entity=account,
        actor_label="" if actor else "System",
        after={"provider": provider_name, "subaccount_code": code,
               "bank_name": cooperative.bank_name,
               "bank_code": cooperative.bank_code},
    )
    return account, True


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

    from payments.fees import gross_up

    provider = get_provider(Provider.PAYSTACK)
    # The member pays Paystack's fee on top, so the society receives the
    # whole repayment (see payments.fees).
    return provider.initialize_transaction(
        email=email,
        amount=gross_up(repayment.amount),
        reference=repayment.psp_reference,
        subaccount_code=subaccount,
        callback_url=callback_url or (settings.PAYSTACK_CALLBACK_URL or None),
    )


class PaymentNotReceived(PaymentInitError):
    """The provider has not received (all of) a payment, so nothing is posted.

    Subclasses ``PaymentInitError`` so every caller that already reports a
    provider problem to the user reports this one too, rather than treating a
    quiet return as success.
    """

    def __init__(self, message, *, provider_status=""):
        super().__init__(message)
        self.provider_status = provider_status


def check_loan_payment(repayment):
    """Ask Paystack about one online repayment; settle it only if fully paid.

    The single gate every path goes through — the member's return from
    checkout, an officer's "Check with Paystack", the admin recheck and the
    bulk command — so none of them can post a repayment the provider has not
    actually received. Two conditions, both checked:

    * Paystack reports the charge as ``success``. A cancelled or abandoned
      checkout still redirects back to the app, so arriving back proves
      nothing.
    * The amount Paystack received covers the repayment. A charge for less
      would otherwise clear more of the loan than was paid.

    Returns the settled repayment (or ``None`` if the loan had already been
    repaid in full). Raises :class:`PaymentNotReceived` otherwise.
    """
    from loans.models import LoanRepayment
    from loans.services import confirm_loan_repayment

    if repayment.status == LoanRepayment.Status.CONFIRMED:
        return repayment
    if repayment.status == LoanRepayment.Status.REVERSED:
        # Reversed because Paystack had no payment. Should one turn up after
        # all, re-posting it silently could double-count; an operator decides.
        raise PaymentNotReceived(
            "This repayment was reversed. If Paystack now shows it as paid, "
            "an officer should record it again.", provider_status="reversed")
    if repayment.channel != LoanRepayment.Channel.PSP:
        # A member-reported bank transfer went to the society's own bank, which
        # Paystack never sees. An officer verifies that against the statement.
        raise PaymentNotReceived(
            "This is a reported bank transfer, not an online payment, so "
            "Paystack cannot confirm it.")

    detail = get_provider(Provider.PAYSTACK).fetch_transaction(
        repayment.psp_reference)
    if detail is None:
        raise PaymentNotReceived(
            "Paystack has no payment with this reference. Nothing was posted.",
            provider_status="unknown")
    status = detail.get("status") or ("success" if detail.get("success")
                                      else "unknown")
    if not detail.get("success"):
        raise PaymentNotReceived(
            f"Paystack has not received this payment (status: {status}). "
            f"Nothing was posted.", provider_status=status)

    paid = detail.get("amount")
    if paid is not None and paid < repayment.amount:
        raise PaymentNotReceived(
            f"Paystack received {paid:,.2f}, less than the {repayment.amount:,.2f} "
            f"this repayment is for. Nothing was posted; check the payment in "
            f"the Paystack dashboard.", provider_status="partial")

    # The detail says what fee Paystack took and where the money settled, so
    # the repayment is booked into the wallet (or bank) for the right amount.
    return confirm_loan_repayment(repayment, receipt=detail)


def recheck_settled_repayment(repayment, *, actor=None):
    """Ask Paystack whether a *confirmed* online repayment was really paid.

    The other half of :func:`check_loan_payment`. That one stops an unpaid
    repayment being posted; this one finds those that were posted anyway —
    before that gate existed, an officer's Confirm posted any pending row — and
    reverses them, which returns the loan to the member to repay.

    Returns ``(repayment, outcome)``:

    * ``"kept"``     — Paystack received the full amount; nothing changes.
    * ``"reversed"`` — Paystack has no successful payment for it; reversed.
    * ``"short"``    — Paystack received *some* money, less than was posted.
      Not reversed automatically: real money arrived, and wiping the whole
      repayment would hide it. Left for an operator.

    Raises ``PaymentInitError`` when Paystack cannot be asked. That must never
    read as "no record" — an outage would otherwise reverse genuine payments.
    """
    from loans.models import LoanRepayment
    from loans.services import reverse_repayment

    if (repayment.status != LoanRepayment.Status.CONFIRMED
            or repayment.channel != LoanRepayment.Channel.PSP
            or not repayment.psp_reference):
        return repayment, "kept"

    detail = get_provider(Provider.PAYSTACK).fetch_transaction(
        repayment.psp_reference)
    if detail is None:
        reason = "Paystack has no record of this payment."
    elif not detail.get("success"):
        reason = (f"Paystack reports the payment as "
                  f"{detail.get('status') or 'not successful'}.")
    else:
        paid = detail.get("amount")
        if paid is not None and paid < repayment.amount:
            return repayment, "short"
        return repayment, "kept"

    return reverse_repayment(repayment, reason=reason, actor=actor), "reversed"


def verify_loan_payment(cooperative, reference):
    """Settle the online repayment with this reference if Paystack was paid.

    Idempotent: an already-settled repayment is returned unchanged. Returns
    ``None`` when the reference matches no repayment of this cooperative.
    Raises :class:`PaymentNotReceived` when Paystack has not received the
    payment in full — callers must surface that, not report success.
    """
    from loans.models import LoanRepayment

    matches = LoanRepayment.all_objects.filter(
        cooperative=cooperative, psp_reference=reference)
    repayment = (matches.filter(status=LoanRepayment.Status.CONFIRMED).first()
                 or matches.first())
    if repayment is None:
        return None
    return check_loan_payment(repayment)


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
        settled = confirm_contribution(contribution)
        # Recorded here rather than inside confirm_contribution: reconcile_event
        # calls that too, and hooking it there would make the webhook path
        # create a second event for the settlement it is already reconciling.
        record_internal_settlement(
            settled,
            note="Confirmed on return from the payment provider "
                 "(no webhook received).",
        )
        return settled
    return contribution


def _resolve_cooperative(event: NormalizedEvent):
    """Which cooperative an inbound settlement belongs to.

    The subaccount code is authoritative when split settlement is on — but
    PAYSTACK_USE_SUBACCOUNT is off by default, so no subaccount was ever sent,
    every webhook failed to resolve, and every event was stored with
    cooperative=NULL. Tenant-scoped reconciliation then showed nothing, for
    every cooperative, however much money had moved.

    The reference is the second signal, and a sound one: we generated it when
    the payment was initiated and stored it on the record it belongs to, and
    the payload's signature has already been verified by the time this runs. So
    a reference we issued identifies the tenant that issued it.
    """
    from contributions.models import Contribution
    from loans.models import LoanRepayment

    if event.subaccount_code:
        account = ProviderAccount.all_objects.filter(
            provider=event.provider, subaccount_code=event.subaccount_code,
        ).select_related("cooperative").first()
        if account is not None:
            return account.cooperative

    if event.reference:
        contribution = (
            Contribution.all_objects
            .filter(psp_reference=event.reference)
            .select_related("cooperative").first()
        )
        if contribution is not None:
            return contribution.cooperative

        repayment = (
            LoanRepayment.all_objects
            .filter(psp_reference=event.reference)
            .select_related("cooperative").first()
        )
        if repayment is not None:
            return repayment.cooperative

        from payments.models import WalletTopUp

        topup = (
            WalletTopUp.all_objects
            .filter(provider=event.provider, psp_reference=event.reference)
            .select_related("cooperative").first()
        )
        if topup is not None:
            return topup.cooperative

    return None


@transaction.atomic
def ingest_webhook(*, provider_name: str, raw_body: bytes, headers) -> PaymentEvent:
    """Verify, deduplicate, persist, and reconcile an inbound PSP webhook.

    Returns the (existing or new) :class:`PaymentEvent`. Raises
    :class:`payments.providers.WebhookVerificationError` on a bad signature.
    """
    provider = get_provider(provider_name)
    provider.verify(raw_body, headers)  # raises on tamper

    payload = json.loads(raw_body.decode() or "{}")

    # Outbound transfers take a different path entirely: normalize() below only
    # recognises charge.success and routes to a tenant by settlement subaccount,
    # neither of which a transfer has. Handled before that, not squeezed through
    # it.
    if str(payload.get("event", "")).startswith("transfer."):
        return handle_transfer_event(provider_name=provider_name,
                                     payload=payload)

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
        payment.note = (
            "Could not route to a cooperative: no known subaccount, and the "
            "reference matches no contribution, loan repayment or wallet "
            "top-up."
        )
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
    if _reconcile_wallet_topup(payment) or _reconcile_loan_repayment(payment):
        return payment

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


def _reconcile_wallet_topup(payment: PaymentEvent) -> bool:
    """Settle a wallet top-up from its ``charge.success`` webhook.

    Returns ``False`` when the reference is not a top-up, leaving the event to
    the contribution matching below. Without this branch the webhook for a
    top-up was filed as unmatched and the top-up stayed PENDING unless the
    officer happened to come back through the checkout's return URL.

    The webhook's own amount is not booked: ``recheck_wallet_topup`` asks the
    provider, because only the provider's verify reports the fee it deducted.
    """
    from payments.models import WalletTopUp
    from payments.providers import PaymentInitError

    topup = WalletTopUp.all_objects.filter(
        cooperative=payment.cooperative, provider=payment.provider,
        psp_reference=payment.reference).first()
    if topup is None:
        return False

    was_pending = topup.status == WalletTopUp.Status.PENDING
    try:
        topup = recheck_wallet_topup(topup)
    except (PaymentInitError, WalletError) as exc:
        # The webhook must still be acknowledged and recorded; the top-up stays
        # PENDING for the admin recheck to pick up.
        payment.status = PaymentEvent.Status.UNMATCHED
        payment.note = f"Wallet top-up {topup.psp_reference}: {exc}"[:255]
    else:
        if topup.is_confirmed and was_pending:
            payment.status = PaymentEvent.Status.MATCHED
            payment.note = f"Wallet top-up {topup.psp_reference} confirmed."
        elif topup.is_confirmed:
            payment.status = PaymentEvent.Status.DUPLICATE
            payment.note = "Wallet top-up already confirmed."
        else:
            payment.status = PaymentEvent.Status.UNMATCHED
            payment.note = (f"Wallet top-up {topup.psp_reference} is still "
                            f"{topup.status} at the provider.")

    payment.save(update_fields=["status", "note", "updated_at"])
    return True


def _reconcile_loan_repayment(payment: PaymentEvent) -> bool:
    """Settle an online loan repayment from its ``charge.success`` webhook.

    Returns ``False`` when the reference is not an online repayment, leaving
    the event to the contribution matching below. Without this branch the
    webhook for a repayment was filed as unmatched, and the repayment settled
    only if the member came back through the checkout's return URL — one who
    paid and closed the tab stayed in arrears with the money already taken.

    Only ``psp`` repayments are considered: a member-reported bank transfer
    carries the member's own teller reference, which is not ours and must not
    be settled by a webhook. Classified the same way as a contribution, so a
    settled amount that differs is left for an operator rather than guessed at.
    """
    from loans.models import LoanRepayment
    from loans.services import confirm_loan_repayment

    repayments = LoanRepayment.all_objects.filter(
        cooperative=payment.cooperative, psp_reference=payment.reference,
        channel=LoanRepayment.Channel.PSP)
    repayment = (repayments.filter(status=LoanRepayment.Status.PENDING).first()
                 or repayments.first())
    if repayment is None:
        return False

    if repayment.status == LoanRepayment.Status.CONFIRMED:
        payment.status = PaymentEvent.Status.DUPLICATE
        payment.note = "Loan repayment already settled."
    elif repayment.status == LoanRepayment.Status.REVERSED:
        payment.status = PaymentEvent.Status.UNMATCHED
        payment.note = ("Payment arrived for a loan repayment that had been "
                        "reversed; record it again if it is genuine.")
    elif payment.amount != repayment.amount:
        payment.status = PaymentEvent.Status.PARTIAL
        payment.note = (
            f"Loan repayment amount mismatch: settled {payment.amount} vs "
            f"expected {repayment.amount}.")
    else:
        # The webhook carries the same fee and split the verify call reports,
        # so the money is booked where it landed without asking again.
        from decimal import Decimal

        data = (payment.payload or {}).get("data") or {}
        sub = data.get("subaccount")
        receipt = {
            "amount": Decimal(str(data.get("amount") or 0)) / Decimal("100"),
            "fee": Decimal(str(data.get("fees") or 0)) / Decimal("100"),
            "subaccount": bool(sub.get("subaccount_code")
                               if isinstance(sub, dict) else sub),
        }
        settled = confirm_loan_repayment(repayment, receipt=receipt)
        if settled is None:
            # The loan was already paid off by other repayments, so the money
            # arrived with nothing to apply it to: an operator must refund it.
            payment.status = PaymentEvent.Status.UNMATCHED
            payment.note = ("Loan repayment arrived after the loan was "
                            "fully repaid; nothing was posted.")
        else:
            payment.status = PaymentEvent.Status.MATCHED
            payment.note = f"Loan repayment for loan #{settled.loan_id}."

    payment.save(update_fields=["status", "note", "updated_at"])
    return True


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


# ── Platform subscription billing ───────────────────────────────────────────
# These charge a *cooperative* on behalf of the platform, which is the opposite
# direction from everything above: no subaccount is set, so settlement lands in
# the platform's own Paystack account rather than the cooperative's.
def initialize_invoice_payment(invoice, *, email, callback_url=None):
    """Start a Paystack checkout for a subscription invoice.

    Mints a fresh reference on every attempt and stores it on the invoice.
    Reusing one reference across retries earns "Duplicate Transaction
    Reference" from Paystack the moment a previous attempt succeeded, which
    would leave an officer unable to pay a second time after a browser mishap.

    Returns the checkout URL, or ``None`` when no live key is configured.
    """
    import secrets

    reference = f"SUB-{invoice.pk}-{secrets.token_hex(6).upper()}"
    invoice.psp_reference = reference
    invoice.save(update_fields=["psp_reference", "updated_at"])

    provider = get_provider(Provider.PAYSTACK)
    return provider.initialize_transaction(
        email=email,
        amount=invoice.amount,
        reference=reference,
        subaccount_code=None,
        callback_url=callback_url or (settings.PAYSTACK_CALLBACK_URL or None),
    )


def verify_invoice_payment(reference):
    """Confirm a subscription payment and settle the invoice. Idempotent.

    Returns the invoice (unchanged if the charge did not succeed), or ``None``
    when the reference matches nothing — a stale or forged return URL.
    """
    from platform_admin.billing import settle_invoice
    from platform_admin.models import Invoice

    invoice = Invoice.objects.filter(psp_reference=reference).first()
    if invoice is None:
        return None
    if invoice.status == Invoice.Status.PAID:
        return invoice

    provider = get_provider(Provider.PAYSTACK)
    if not provider.verify_transaction(reference):
        return invoice
    return settle_invoice(invoice)


# ── Settlements that never came through a webhook ───────────────────────────
# Reconciliation counts PaymentEvent rows and nothing else. Three paths settle
# money without producing one, which is why the screen was blank while the
# ledger and the contribution list were both right:
#
#   * verify_payment  — a card payment confirmed via the browser return URL
#   * record_contribution — cash or a bank transfer entered by an officer
#   * any historical contribution confirmed before this existed (see the
#     backfill_payment_events command)
#
# These are recorded as Provider.INTERNAL so they are never mistaken for a
# genuine PSP settlement, and carry a deterministic event_id derived from the
# contribution so re-running anything cannot duplicate a row.
def internal_event_id(contribution) -> str:
    """A stable event id for a settlement with no provider event of its own."""
    return f"ctb-{contribution.pk}"


def record_internal_settlement(contribution, *, note: str = "",
                               reconstructed: bool = False) -> PaymentEvent:
    """Record a settlement that arrived without a PSP webhook. Idempotent.

    Returns the existing row when one is already present, so a retried callback
    or a re-run backfill is harmless. Always MATCHED: the contribution it
    settles is the one it was created from, so there is nothing to search for
    and nothing that can fail to match.
    """
    event_id = internal_event_id(contribution)
    existing = PaymentEvent.all_objects.filter(
        provider=Provider.INTERNAL, event_id=event_id,
    ).first()
    if existing is not None:
        return existing

    # Cash and transfers have no psp_reference, so fall back to the ledger
    # journal's reference — which is what an officer would actually trace this
    # settlement by. `journal` is nullable, hence the final fallback rather than
    # an attribute access that would raise on the very path this exists to fix.
    reference = contribution.psp_reference
    if not reference and contribution.journal_id:
        reference = contribution.journal.reference
    if not reference:
        reference = event_id

    if not note:
        note = (
            "Reconstructed from the contribution record."
            if reconstructed
            else f"Settled by {contribution.get_channel_display().lower()}."
        )

    return PaymentEvent.all_objects.create(
        cooperative=contribution.cooperative,
        provider=Provider.INTERNAL,
        event_id=event_id,
        reference=reference,
        amount=contribution.amount,
        currency=contribution.cooperative.base_currency,
        status=PaymentEvent.Status.MATCHED,
        matched_contribution=contribution,
        note=note,
        payload={
            "source": "internal",
            "channel": contribution.channel,
            "reconstructed": reconstructed,
        },
    )
