"""
Loans & credit. A loan moves pending → approved → disbursed → repaid.
Disbursement and repayments post balanced journals through the ledger, so the
loan book always reconciles with the double-entry accounts.
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models

from core.models import TenantScopedModel, TimeStampedModel

ZERO = Decimal("0.00")


class LoanProduct(TenantScopedModel, TimeStampedModel):
    name = models.CharField(max_length=120)
    interest_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        help_text="Flat interest rate (%) — a one-off charge on the amount "
                  "borrowed (e.g. 10% of a ₦300,000 loan = ₦30,000), spread "
                  "evenly across the term. Members repay a fixed amount monthly.")
    max_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    max_term_months = models.PositiveIntegerField(default=12)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Loan(TenantScopedModel, TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        DISBURSED = "disbursed", "Disbursed"
        REPAID = "repaid", "Repaid"

    membership = models.ForeignKey(
        "accounts.Membership", on_delete=models.PROTECT, related_name="loans")
    product = models.ForeignKey(
        LoanProduct, on_delete=models.PROTECT, related_name="loans")
    principal = models.DecimalField(max_digits=14, decimal_places=2)
    interest_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    term_months = models.PositiveIntegerField(default=12)
    purpose = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.PENDING)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        blank=True, related_name="decided_loans")
    decided_at = models.DateTimeField(null=True, blank=True)
    disbursed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Loan {self.id} · {self.membership.member_no}"

    def _schedule(self):
        from loans.amortization import flat_schedule
        return flat_schedule(self.principal, self.interest_rate,
                             self.term_months)

    @property
    def interest(self) -> Decimal:
        """Total (flat) interest over the loan's term."""
        return sum((row.interest for row in self._schedule()), ZERO)

    @property
    def total_repayable(self) -> Decimal:
        return self.principal + self.interest

    @property
    def repaid_amount(self) -> Decimal:
        from django.db.models import Sum
        # Only settled (confirmed) repayments count toward the balance; a
        # member-initiated PSP repayment stays ``pending`` until verified.
        agg = LoanRepayment.all_objects.filter(
            loan=self, status=LoanRepayment.Status.CONFIRMED,
        ).aggregate(s=Sum("amount"))
        return agg["s"] or ZERO

    @property
    def outstanding(self) -> Decimal:
        if self.status in (self.Status.DISBURSED, self.Status.REPAID):
            return self.total_repayable - self.repaid_amount
        return ZERO

    @property
    def monthly_instalment(self) -> Decimal:
        """The fixed monthly repayment."""
        rows = self._schedule()
        return rows[0].payment if rows else ZERO

    @property
    def next_due(self):
        """The earliest unpaid instalment, or ``None`` when fully settled."""
        return self.instalments.exclude(
            status=RepaymentInstalment.Status.PAID).order_by("sequence").first()


class RepaymentInstalment(TenantScopedModel, TimeStampedModel):
    """One scheduled repayment on a disbursed loan.

    The schedule is built at disbursement (even split of the total repayable
    across the term). Repayments are allocated to the earliest unpaid
    instalment first, so ``amount_paid`` fills each row in order.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"

    loan = models.ForeignKey(
        Loan, on_delete=models.CASCADE, related_name="instalments")
    sequence = models.PositiveIntegerField()
    due_date = models.DateField()
    amount_due = models.DecimalField(max_digits=14, decimal_places=2)
    # Reducing-balance split of amount_due (principal_component + interest ==
    # amount_due); used to book interest as it is earned on each repayment.
    principal_component = models.DecimalField(max_digits=14, decimal_places=2,
                                              default=ZERO)
    interest_component = models.DecimalField(max_digits=14, decimal_places=2,
                                             default=ZERO)
    amount_paid = models.DecimalField(max_digits=14, decimal_places=2,
                                      default=ZERO)
    status = models.CharField(max_length=8, choices=Status.choices,
                              default=Status.PENDING)

    class Meta:
        ordering = ["loan", "sequence"]
        constraints = [
            models.UniqueConstraint(fields=["loan", "sequence"],
                                    name="uniq_loan_instalment_sequence"),
        ]

    def __str__(self) -> str:
        return f"Loan {self.loan_id} · instalment {self.sequence}"

    @property
    def outstanding(self) -> Decimal:
        return max(self.amount_due - self.amount_paid, ZERO)

    @property
    def is_overdue(self) -> bool:
        from django.utils import timezone
        return (self.status != self.Status.PAID
                and self.due_date < timezone.localdate())


class LoanRepayment(TenantScopedModel, TimeStampedModel):
    class Channel(models.TextChoices):
        CASH = "cash", "Cash"
        TRANSFER = "transfer", "Bank transfer"
        PSP = "psp", "Online (Paystack)"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"      # PSP initiated, not yet verified
        CONFIRMED = "confirmed", "Confirmed"

    loan = models.ForeignKey(
        Loan, on_delete=models.CASCADE, related_name="repayments")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    channel = models.CharField(max_length=10, choices=Channel.choices,
                               default=Channel.CASH)
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.CONFIRMED)
    psp_reference = models.CharField(max_length=64, blank=True, db_index=True)
    # A member-reported bank-transfer note (e.g. sending bank, date, teller ref)
    # so an officer can match it against the society's account before confirming.
    note = models.CharField(max_length=255, blank=True)
    journal = models.ForeignKey(
        "ledger.Journal", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="loan_repayments")

    class Meta:
        ordering = ["-created_at"]
