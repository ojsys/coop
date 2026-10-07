from __future__ import annotations

from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from core.context import get_current_cooperative
from core.permissions import (IsPrivilegedOfficer,
                              IsPrivilegedOfficerOrOfficeHolderReadOnly,
                              IsPrivilegedOfficerOrReadOnly)
from core.views import TenantScopedViewMixin
from loans.models import Loan, LoanProduct, LoanRepayment
from loans.serializers import (
    LoanProductSerializer, LoanSerializer, RepaymentClaimSerializer,
)
from loans.services import (
    LoanError, approve_loan, confirm_loan_repayment, disburse_loan,
    record_repayment, reject_repayment,
)


class LoanProductViewSet(TenantScopedViewMixin, viewsets.ModelViewSet):
    serializer_class = LoanProductSerializer
    # Reads are harmless — a product catalogue is not member data. Writes are
    # not: this is where interest rates, maximum amounts and terms are set, and
    # under IsAuthenticated any member could rewrite the price of credit for
    # the whole society, or raise their own borrowing limit before applying.
    permission_classes = [IsPrivilegedOfficerOrReadOnly]

    def get_queryset(self):
        return LoanProduct.objects.all()


class LoanViewSet(TenantScopedViewMixin, viewsets.ModelViewSet):
    """The officer-side loan book. Members use ``/me/loans/`` instead.

    Privileged officers only, **reads included** — not merely the write actions.
    Two separate problems were open under ``IsAuthenticated``:

    * ``approve`` and ``disburse`` were callable by any authenticated member, so
      a member could approve their own pending loan and then disburse it,
      posting a real journal that moves the principal out of Cash. The
      /approvals/ queue advertises dual control, but these direct endpoints were
      an unguarded parallel path straight past it.
    * ``LoanSerializer`` renders ``member_bank_account_no``, phone, email, share
      capital and photograph, and this queryset is scoped to the cooperative
      rather than to the caller — so listing it handed any member every other
      member's bank account number.
    """

    serializer_class = LoanSerializer
    # Office-holders read, privileged officers act. A Chairperson sits on the
    # console to approve things and needs to see the loan book and the
    # awaiting-payment queue — but holds none of PRIVILEGED_PERMISSIONS, so they
    # cannot approve, disburse or record a repayment here.
    #
    # Not plain ...OrReadOnly: this queryset is scoped to the cooperative rather
    # than to the caller, and LoanSerializer renders every member's bank account
    # number, phone and email. Opening reads to *any* authenticated member would
    # hand that to all of them.
    permission_classes = [IsPrivilegedOfficerOrOfficeHolderReadOnly]
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self):
        qs = Loan.objects.select_related(
            "membership__user", "product",
        ).prefetch_related("instalments", "repayments")
        member = self.request.query_params.get("membership")
        status = self.request.query_params.get("status")
        if member:
            qs = qs.filter(membership_id=member)
        if status:
            qs = qs.filter(status=status)
        return qs

    def _act(self, fn, *args, **kwargs):
        loan = self.get_object()
        try:
            fn(loan, *args, **kwargs)
        except LoanError as exc:
            return Response({"detail": str(exc)}, status=400)
        loan.refresh_from_db()
        return Response(self.get_serializer(loan).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        """Approve a loan — and pay it out, where it can be paid.

        The response carries ``auto_disbursement_error`` when the money could
        not be sent, so the officer learns why at the moment they approve
        rather than discovering an unpaid approved loan days later.
        """
        loan = self.get_object()
        try:
            approve_loan(loan, actor=request.user, approve=True)
        except LoanError as exc:
            return Response({"detail": str(exc)}, status=400)

        # Read the transient attributes before refresh_from_db discards them.
        payout = getattr(loan, "auto_disbursement", None)
        error = getattr(loan, "auto_disbursement_error", None)

        loan.refresh_from_db()
        data = self.get_serializer(loan).data
        data["auto_disbursement_error"] = error
        data["payout_reference"] = payout.reference if payout else None
        return Response(data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        return self._act(approve_loan, actor=request.user, approve=False)

    @action(detail=True, methods=["post"])
    def disburse(self, request, pk=None):
        """Record a **cash** disbursement — money handed over, not transferred.

        Deliberately separate from pay_electronically rather than one action
        with a mode flag: these are different real-world events, and a loan
        already settled in cash outside the system must be *recorded*, not paid
        a second time. The button an officer presses should say which it is.
        """
        return self._act(disburse_loan, actor=request.user)

    @action(detail=True, methods=["post"], url_path="pay-electronically")
    def pay_electronically(self, request, pk=None):
        """Transfer an already-approved loan to the member's bank.

        Approval pays out automatically where it can, so this is for the ones it
        could not: approved before that existed, or approved when the wallet was
        short or the bank code missing.
        """
        from loans.services import disburse_loan_electronically
        from payments.providers import PaymentInitError
        from payments.services import PayoutError

        loan = self.get_object()
        try:
            payout = disburse_loan_electronically(loan, actor=request.user)
        except (LoanError, PayoutError) as exc:
            return Response({"detail": str(exc)}, status=400)
        except PaymentInitError as exc:
            # The provider refused. Not the officer's input, so pass its words on.
            return Response({"detail": str(exc)}, status=502)

        loan.refresh_from_db()
        data = self.get_serializer(loan).data
        data["payout_reference"] = payout.reference
        return Response(data)

    @action(detail=True, methods=["post"], url_path="refresh-destination")
    def refresh_destination(self, request, pk=None):
        """Re-point an approved loan at the member's current bank details.

        Needed because approval freezes the destination, which is the control
        that stops a payout being redirected away from the account an officer
        vetted — but also leaves a loan approved before the member had a usable
        account permanently unpayable. The member fills in their bank code and
        the loan still carries the blank snapshot it was approved with.

        Privileged and audited: it changes where money will go, so it is an
        explicit act by a named officer, never a side effect.
        """
        from loans.services import refresh_loan_destination

        loan = self.get_object()
        try:
            refresh_loan_destination(loan, actor=request.user)
        except LoanError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(self.get_serializer(loan).data)

    @action(detail=True, methods=["get"])
    def disbursement(self, request, pk=None):
        """Was this loan actually disbursed? The evidence, not just the status.

        A read, so any office-holder may ask — including a Chairperson, who is
        often the one a member complains to.
        """
        from loans.verification import disbursement_evidence

        return Response(disbursement_evidence(self.get_object()))

    @action(detail=True, methods=["post"], url_path="recheck-disbursement")
    def recheck_disbursement(self, request, pk=None):
        """Ask the provider directly, then report what it said.

        A write, and privileged: the answer is applied through the same path as a
        webhook, so a failed transfer reverses its journal and puts the loan back
        to approved. That is money moving, not a lookup.
        """
        from payments.providers import PaymentInitError
        from loans.verification import recheck_disbursement as _recheck

        try:
            evidence = _recheck(self.get_object(), actor=request.user)
        except PaymentInitError as exc:
            # The provider could not be reached. Say so plainly rather than
            # letting it read as "not disbursed".
            return Response({"detail": str(exc)}, status=502)
        return Response(evidence)

    @action(detail=False, methods=["get"])
    def health(self, request):
        """Loans whose state does not add up, plus the awaiting-payment queue.

        Read-only. The two are returned together but counted apart: approved
        loans that were never paid are a work queue, not damage.
        """
        from loans.health import loan_health

        report = loan_health(get_current_cooperative())
        return Response({
            **{key: value for key, value in report.items()
               if key != "findings"},
            "findings": [finding.as_dict() for finding in report["findings"]],
        })

    @action(detail=True, methods=["post"])
    def repay(self, request, pk=None):
        amount = request.data.get("amount")
        channel = request.data.get("channel", "cash")
        return self._act(record_repayment, amount=amount, actor=request.user,
                         channel=channel)


class LoanRepaymentViewSet(TenantScopedViewMixin, mixins.ListModelMixin,
                           viewsets.GenericViewSet):
    """Officer view of repayments — used to verify & confirm member-reported
    bank transfers (``?status=pending``)."""

    serializer_class = RepaymentClaimSerializer
    # Office-holders read, privileged officers act. A Chairperson can see which
    # claims are waiting — part of knowing the state of the society's lending —
    # but ``confirm`` posts a repayment to the ledger, which stays privileged.
    #
    # Not plain ...OrReadOnly: the list shows other members' claims and
    # RepaymentClaimSerializer carries their bank details. Members report their
    # own transfers through /me/loans/{id}/report-transfer/ and never need this.
    permission_classes = [IsPrivilegedOfficerOrOfficeHolderReadOnly]

    def get_queryset(self):
        qs = LoanRepayment.objects.select_related(
            "loan__membership__user", "loan__product")
        status = self.request.query_params.get("status")
        if status:
            qs = qs.filter(status=status)
        return qs

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        """Post a pending repayment — but only once the money is known to exist.

        An online (Paystack) repayment is confirmed **by Paystack**, never by
        the officer's say-so: this asks Paystack and posts only if it received
        the full amount. Previously the button posted any pending row, so an
        abandoned checkout — no money at all — could be marked repaid.

        A reported bank transfer is the one case Paystack cannot see, since the
        member paid the society's own bank; there the officer's check against
        the statement is the confirmation.
        """
        from payments.providers import PaymentInitError
        from payments.services import PaymentNotReceived, check_loan_payment

        repayment = self.get_object()
        if repayment.status != LoanRepayment.Status.PENDING:
            return Response({"detail": "This repayment is already confirmed."},
                            status=400)

        if repayment.channel == LoanRepayment.Channel.PSP:
            try:
                settled = check_loan_payment(repayment)
            except PaymentNotReceived as exc:
                return Response({"detail": str(exc),
                                 "payment_status": exc.provider_status},
                                status=400)
            except PaymentInitError as exc:
                return Response({"detail": str(exc)}, status=502)
            if settled is None:
                return Response(
                    {"detail": "Paystack received this payment, but the loan "
                               "was already fully repaid, so nothing was "
                               "posted. Refund the member."}, status=409)
            return Response({"status": "confirmed",
                             "verified_by": "paystack"})

        try:
            confirm_loan_repayment(repayment, actor=request.user)
        except LoanError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response({"status": "confirmed", "verified_by": "officer"})

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        """Dismiss a reported transfer that couldn't be verified."""
        repayment = self.get_object()
        try:
            reject_repayment(repayment, actor=request.user)
        except LoanError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response({"status": "rejected"})
