from __future__ import annotations

from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.context import get_current_cooperative
from core.permissions import IsPrivilegedOfficerOrReadOnly
from core.views import TenantScopedViewMixin
from payments.models import (PaymentEvent, Payout, ProviderAccount,
                            WalletTopUp)
from payments.providers import PaymentInitError, WebhookVerificationError
from payments.serializers import (
    PaymentEventSerializer, PayoutSerializer, ProviderAccountSerializer,
    WalletTopUpSerializer,
)
from payments.services import (
    PayoutError, WalletError, confirm_wallet_topup, ingest_webhook,
    initiate_wallet_topup, recheck_payout, reconcile_event,
    reconciliation_summary, wallet_balance, withdraw_wallet,
)


class WebhookView(APIView):
    """Inbound PSP webhook endpoint.

    Unauthenticated (PSPs don't carry our tokens) but signature-verified. The
    tenant is resolved from the settlement subaccount inside the payload, so no
    ``X-Cooperative-Id`` header is involved here.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]
    provider_name: str = ""

    def post(self, request, *args, **kwargs):
        try:
            event = ingest_webhook(
                provider_name=self.provider_name,
                raw_body=request.body,
                # DRF/Django header access is case-insensitive.
                headers={k.lower(): v for k, v in request.headers.items()},
            )
        except WebhookVerificationError:
            return Response({"detail": "Invalid signature."},
                            status=status.HTTP_401_UNAUTHORIZED)
        # Always 200 so the PSP does not retry a successfully received event.
        return Response({"status": event.status, "reference": event.reference})


class PaystackWebhookView(WebhookView):
    provider_name = "paystack"


class FlutterwaveWebhookView(WebhookView):
    provider_name = "flutterwave"


class ProviderAccountViewSet(TenantScopedViewMixin, viewsets.ModelViewSet):
    """A cooperative's PSP settlement subaccounts.

    Privileged officers only, reads included. This was ``IsAuthenticated``,
    which made it the most dangerous endpoint in the project: a subaccount code
    is *where collections settle*, so any authenticated member could POST a row
    carrying their own code and redirect every future payment to their own bank
    account. PATCH and DELETE were open too, and because ``initialize_payment``
    filters on ``connected=True``, flipping that one field silently reverted a
    society's settlement to the platform account.

    Reads stay open to any office-holder on purpose. The console is reachable
    by any member with a role — a Chairperson sits on it for approvals — and
    SettingsPage states the principle plainly: writable only by a privileged
    member, but don't render a card that will only ever 403. Closing reads here
    would break that page for an office-holder who is entitled to see, but not
    change, where their society's money settles.

    Rows are normally created by ``payments.services.ensure_subaccount`` (via
    POST /cooperatives/{id}/connect-settlement-account/), which creates the
    subaccount at the provider rather than trusting a hand-typed code.
    """

    serializer_class = ProviderAccountSerializer
    permission_classes = [IsPrivilegedOfficerOrReadOnly]

    def get_queryset(self):
        return ProviderAccount.objects.all()


class PaymentEventViewSet(TenantScopedViewMixin, mixins.ListModelMixin,
                          mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Settlement events + reconciliation tooling."""

    serializer_class = PaymentEventSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = PaymentEvent.objects.select_related(
            "matched_contribution__membership",
        )
        if self.request.query_params.get("exceptions") == "true":
            qs = qs.filter(status__in=[
                PaymentEvent.Status.UNMATCHED,
                PaymentEvent.Status.PARTIAL,
                PaymentEvent.Status.DUPLICATE,
            ])
        return qs

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """Reconciliation health for the active cooperative's settlement cycle."""
        return Response(reconciliation_summary(get_current_cooperative()))

    @action(detail=True, methods=["post"])
    def rematch(self, request, pk=None):
        """Re-run matching for an exception (e.g. after fixing the reference)."""
        event = self.get_object()
        reconcile_event(event)
        event.refresh_from_db()
        return Response(PaymentEventSerializer(event).data)

    @action(detail=False, methods=["post"], url_path="rematch-all")
    def rematch_all(self, request):
        """Re-run matching over every current exception in one go."""
        exception_statuses = [
            PaymentEvent.Status.UNMATCHED, PaymentEvent.Status.PARTIAL,
            PaymentEvent.Status.DUPLICATE,
        ]
        events = self.get_queryset().filter(status__in=exception_statuses)
        rematched = 0
        for event in events:
            reconcile_event(event)
            rematched += 1
        return Response({
            "rematched": rematched,
            "summary": reconciliation_summary(get_current_cooperative()),
        })


class PayoutViewSet(TenantScopedViewMixin, mixins.ListModelMixin,
                    mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """`/payouts/` — money sent out, and whether it actually landed.

    A payout is marked sent when the provider *accepts* the transfer, which is
    not the same as it settling. Provider webhooks can be missed or delayed, so
    a payout can sit here saying "pending" while the money has in fact already
    gone — or, worse, while it quietly failed.

    ``recheck`` is the answer to that: it asks the provider directly and
    reconciles through the same code path the webhook uses, so the two cannot
    disagree. Safe to press repeatedly — a payout that has reached a final state
    only records the newer payload.

    Reads are open to any office-holder (they need to see whether a member has
    been paid); rechecking is a privileged action because it can reverse a
    journal and send a loan back to approved.
    """

    serializer_class = PayoutSerializer
    permission_classes = [IsPrivilegedOfficerOrReadOnly]

    def get_queryset(self):
        qs = Payout.objects.select_related("requested_by", "journal")
        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        if self.request.query_params.get("unsettled"):
            qs = qs.filter(status__in=[Payout.Status.QUEUED,
                                       Payout.Status.PENDING])
        return qs

    @action(detail=True, methods=["post"])
    def recheck(self, request, pk=None):
        """Ask the provider what really happened to this payout."""
        payout = self.get_object()
        before = payout.status
        try:
            payout = recheck_payout(payout, actor=request.user)
        except PaymentInitError as exc:
            # The provider could not be reached. Not the officer's fault and
            # not a change of state — say so plainly.
            return Response({"detail": str(exc)}, status=502)

        changed = payout.status != before
        return Response({
            "detail": (
                f"The provider reports this payout as "
                f"{payout.get_status_display().lower()}."
                if changed else
                "No change — the provider still reports the same status."
            ),
            "changed": changed,
            "payout": PayoutSerializer(payout).data,
        })

    @action(detail=False, methods=["post"], url_path="recheck-all")
    def recheck_all(self, request):
        """Recheck every payout still in flight.

        For the common case: a batch of disbursements went out, some webhooks
        never arrived, and an officer wants the list to tell the truth.
        """
        pending = list(
            Payout.objects.filter(status__in=[Payout.Status.QUEUED,
                                              Payout.Status.PENDING])
        )
        changed = 0
        unreachable = 0
        for payout in pending:
            before = payout.status
            try:
                payout = recheck_payout(payout, actor=request.user)
            except PaymentInitError:
                unreachable += 1
                continue
            if payout.status != before:
                changed += 1

        return Response({
            "checked": len(pending),
            "changed": changed,
            "unreachable": unreachable,
            "detail": (f"Checked {len(pending)}; {changed} changed."
                       + (f" {unreachable} could not be reached."
                          if unreachable else "")),
        })


class WalletViewSet(TenantScopedViewMixin, mixins.ListModelMixin,
                    viewsets.GenericViewSet):
    """`/wallet/` — the society's disbursement wallet.

    The wallet is the money a society has deliberately deposited with the
    platform in order to lend electronically. Collections are unaffected: those
    settle to the society's own bank through its subaccount.

    Reads are open to any office-holder — a Chairperson needs to see whether
    there is anything to lend from, and SettingsPage's principle is "writable
    only by a privileged member, but do not render a card that will only ever
    403". Funding it is a privileged action.

    The balance is derived from the ledger (account 1020), never stored, so it
    cannot drift from the journals behind it.
    """

    serializer_class = WalletTopUpSerializer
    permission_classes = [IsPrivilegedOfficerOrReadOnly]

    def get_queryset(self):
        return WalletTopUp.objects.select_related("initiated_by", "journal")

    def list(self, request, *args, **kwargs):
        """Balance, the funding that actually arrived, and the other attempts.

        Clicking "Fund wallet" creates a record before any money moves, so most
        rows are checkouts that were never paid. Mixed together, those buried
        the real funding history — and twenty abandoned clicks could push every
        successful top-up off the list. ``topups`` is therefore confirmed
        funding only; ``attempts`` holds the pending and failed ones, kept for
        anyone reconciling.
        """
        cooperative = get_current_cooperative()
        qs = self.get_queryset()
        confirmed = qs.filter(status=WalletTopUp.Status.CONFIRMED)
        others = qs.exclude(status=WalletTopUp.Status.CONFIRMED)
        return Response({
            "balance": str(wallet_balance(cooperative)),
            "currency": cooperative.base_currency,
            "topups": WalletTopUpSerializer(confirmed[:50], many=True).data,
            "attempts": WalletTopUpSerializer(others[:50], many=True).data,
            "attempt_counts": {
                "pending": others.filter(
                    status=WalletTopUp.Status.PENDING).count(),
                "failed": others.filter(
                    status=WalletTopUp.Status.FAILED).count(),
            },
        })

    @action(detail=False, methods=["post"])
    def topup(self, request):
        """Start a checkout that funds the wallet.

        Returns the provider's checkout URL (``None`` when no live key is
        configured). Nothing is credited until the charge is confirmed.
        """
        cooperative = get_current_cooperative()
        email = (request.data.get("email")
                 or getattr(request.user, "email", "") or "")
        try:
            topup, url = initiate_wallet_topup(
                cooperative,
                amount=request.data.get("amount"),
                email=email,
                actor=request.user,
                callback_url=request.data.get("callback_url"),
            )
        except WalletError as exc:
            return Response({"detail": str(exc)}, status=400)
        except PaymentInitError as exc:
            # The provider refused to create the checkout. Surfaced verbatim:
            # "Paystack rejected the transaction" is actionable, a generic
            # failure is not.
            return Response({"detail": str(exc)}, status=502)

        return Response({
            "detail": "Checkout created. The wallet is credited once the "
                      "payment is confirmed.",
            "authorization_url": url,
            "topup": WalletTopUpSerializer(topup).data,
        }, status=201)

    @action(detail=False, methods=["post"])
    def verify(self, request):
        """Confirm a top-up against the provider and credit the wallet.

        Idempotent, and safe to call from the payment return URL: a replayed
        confirmation returns the already-confirmed record rather than crediting
        the wallet twice.
        """
        reference = request.data.get("reference")
        if not reference:
            return Response({"detail": "A payment reference is required."},
                            status=400)

        cooperative = get_current_cooperative()
        try:
            topup = confirm_wallet_topup(cooperative, reference,
                                         actor=request.user)
        except WalletError as exc:
            return Response({"detail": str(exc)}, status=400)

        if topup is None:
            return Response(
                {"detail": "That reference matches no top-up for this "
                           "cooperative."},
                status=404)

        return Response({
            "balance": str(wallet_balance(cooperative)),
            "topup": WalletTopUpSerializer(topup).data,
        })

    @action(detail=False, methods=["post"])
    def withdraw(self, request):
        """Send wallet funds back to the society's own bank account.

        The float is the society's own money; this is how it takes it back. One
        privileged officer suffices because the destination can only be the
        society's own collection account, which already required two officers to
        set.
        """
        cooperative = get_current_cooperative()
        try:
            payout = withdraw_wallet(
                cooperative,
                amount=request.data.get("amount"),
                actor=request.user,
                reason=request.data.get("reason", ""),
            )
        except PayoutError as exc:
            return Response({"detail": str(exc)}, status=400)
        except PaymentInitError as exc:
            # The provider refused the transfer. Surfaced verbatim: its message
            # names the cause, and it is not the officer's input that was wrong.
            return Response({"detail": str(exc)}, status=502)

        return Response({
            "detail": "Withdrawal sent to the society's own account.",
            "balance": str(wallet_balance(cooperative)),
            "payout": PayoutSerializer(payout).data,
        }, status=201)


class FeeQuoteView(APIView):
    """`GET /fee-quote/?amount=` — what paying ``amount`` online costs.

    The member pays Paystack's fee on top of a contribution or loan repayment,
    so the society receives the whole amount. Shown before checkout, from the
    same calculation the checkout uses.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from decimal import Decimal, InvalidOperation

        from payments.fees import gross_up

        try:
            amount = Decimal(request.query_params.get("amount") or "0")
        except InvalidOperation:
            return Response({"detail": "Enter a valid amount."}, status=400)
        if amount <= 0:
            return Response({"detail": "Enter an amount above zero."},
                            status=400)
        amount = amount.quantize(Decimal("0.01"))
        total = gross_up(amount)
        return Response({"amount": str(amount), "fee": str(total - amount),
                         "total": str(total)})
