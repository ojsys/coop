"""
Receipts for money movements, and alerts to the officers who need to act.

Two audiences with different needs:

* **Members** get a receipt — a record they keep, and sometimes print. The
  figures are laid out plainly rather than buried in a sentence.
* **Officers** get an alert — read quickly on a phone, so the facts carry it.

Neither ever raises: a notification failing must not roll back the business
event that produced it. Failures are logged instead.
"""
from __future__ import annotations

import logging

from django.utils import timezone

logger = logging.getLogger("api.errors")


def money(value) -> str:
    """Email is UTF-8, so the Naira sign renders here (unlike the PDF fonts)."""
    return f"₦{value:,.2f}"


def officer_emails(cooperative) -> list[str]:
    """Addresses of the society's privileged officers."""
    from accounts.models import Membership

    officers = (
        Membership.all_objects
        .filter(cooperative=cooperative, status=Membership.Status.ACTIVE)
        .select_related("user", "role")
    )
    return [m.user.email for m in officers
            if m.role and m.role.is_privileged and m.user.email]


def notify_officers_by_email(cooperative, *, subject, heading, intro="",
                             rows=None) -> int:
    """Email every privileged officer. Returns how many were reached."""
    from communications.email import send_alert_email

    recipients = officer_emails(cooperative)
    if not recipients:
        logger.warning(
            "No privileged officer with an email address for %s — '%s' was "
            "not delivered to anyone", cooperative, subject,
        )
        return 0

    send_alert_email(to=recipients, subject=subject, heading=heading,
                     intro=intro, rows=rows or [], cooperative=cooperative)
    return len(recipients)


# ── Member receipts ─────────────────────────────────────────────────────────
def send_contribution_receipt(contribution) -> bool:
    """Confirm a recorded contribution to the member who made it."""
    from communications.email import send_branded_email

    membership = contribution.membership
    user = membership.user
    if not user.email:
        return False

    occurred = contribution.occurred_at or timezone.now()
    rows = [
        ("Contribution type", contribution.contribution_type.name),
        ("Member number", membership.member_no),
        ("Date", occurred.strftime("%d %B %Y")),
        ("Method", contribution.get_channel_display()),
    ]
    if contribution.psp_reference:
        rows.append(("Reference", contribution.psp_reference))
    if contribution.journal_id:
        rows.append(("Ledger reference", contribution.journal.reference))

    return send_branded_email(
        to=user.email,
        subject=f"Receipt — {money(contribution.amount)} received",
        template="emails/receipt.html",
        cooperative=membership.cooperative,
        context={
            "full_name": user.full_name,
            "headline": "Contribution received",
            "intro": "Thank you — your payment has been recorded against your "
                     "membership.",
            "amount": money(contribution.amount),
            "occurred_on": occurred.strftime("%d %B %Y"),
            "rows": rows,
            "balance_label": "Your savings balance",
            "balance": money(membership.savings_balance),
        },
    )


def send_loan_repayment_receipt(loan, amount) -> bool:
    """Confirm a settled loan repayment to the borrower."""
    from communications.email import send_branded_email

    membership = loan.membership
    user = membership.user
    if not user.email:
        return False

    settled = loan.status == loan.Status.REPAID
    return send_branded_email(
        to=user.email,
        subject=f"Receipt — {money(amount)} repayment received",
        template="emails/receipt.html",
        cooperative=membership.cooperative,
        context={
            "full_name": user.full_name,
            "headline": "Loan repayment received",
            "intro": ("Thank you — this loan is now fully repaid."
                      if settled else
                      "Thank you — your repayment has been applied to your loan."),
            "amount": money(amount),
            "occurred_on": timezone.now().strftime("%d %B %Y"),
            "rows": [
                ("Loan", loan.product.name),
                ("Member number", membership.member_no),
                ("Date", timezone.now().strftime("%d %B %Y")),
                ("Total repayable", money(loan.total_repayable)),
                ("Repaid so far", money(loan.repaid_amount)),
            ],
            "balance_label": "Still outstanding",
            "balance": money(loan.outstanding),
        },
    )
