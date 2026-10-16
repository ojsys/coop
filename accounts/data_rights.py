"""
A person's rights over their own data: a copy of everything, and deletion.

**Export** collects what every society holds about the person into one zip:
their account, each membership's profile, statement, contributions, loans,
savings, dividends, votes, notifications, photograph and documents.

**Deletion** follows the privacy notice (frontend PrivacyPage, "Deleting your
account and your data") to the letter, because that is what members were
promised:

* Deleted — sign-in credentials, contact details, date of birth, gender,
  address, occupation, next of kin, bank details, photograph, documents.
* Retained — ledger entries, contribution and loan history, audit records:
  the society's financial accounts, kept under its statutory duties and
  left attached only to a member number.

A person who still has money in, or owed to, a society cannot be deleted yet:
erasing the contact details of someone with an open loan or a savings balance
would leave the society unable to settle with them. Those are reported as
blockers, and an officer completes the deletion once they are cleared.
"""
from __future__ import annotations

import csv
import io
import json
import mimetypes
import os
import zipfile
from decimal import Decimal

from django.core import signing
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone

from accounts.models import AccountDeletionRequest, MemberDocument, Membership

DELETED_NAME = "Deleted member"
_TOKEN_SALT = "accounts.deletion-confirm"
TOKEN_MAX_AGE = 3 * 24 * 3600


class DeletionError(Exception):
    pass


def memberships_of(user):
    return (Membership.all_objects.filter(user=user)
            .select_related("cooperative", "role"))


# ── Export ──────────────────────────────────────────────────────────────────
def _json(data) -> str:
    return json.dumps(data, cls=DjangoJSONEncoder, indent=2, default=str)


def _rows(qs, exclude=()):
    return [{k: v for k, v in row.items() if k not in exclude}
            for row in qs.values()]


def _add_file(zf, field, arcname) -> None:
    if not field:
        return
    try:
        with field.open("rb") as fh:
            zf.writestr(arcname, fh.read())
    except Exception:  # noqa: BLE001 — a missing file must not sink the export
        zf.writestr(arcname + ".missing.txt",
                    "This file is recorded but could not be read from "
                    "storage.")


def export_user_data(user) -> bytes:
    """Everything held about ``user``, as a zip archive."""
    from communications.models import ChannelPreference, Notification
    from contributions.models import Contribution
    from dividends.models import DividendAllocation
    from governance.models import Attendance, Vote
    from ledger.services import member_statement
    from loans.models import Loan, LoanRepayment, RepaymentInstalment
    from savings.models import SavingsGoal, Withdrawal

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", (
            "Your data from CooperativeOS\n"
            f"Prepared {timezone.now():%d %B %Y %H:%M} UTC\n\n"
            "account.json        your sign-in account\n"
            "<society>/          one folder per cooperative you belong to:\n"
            "  profile.json      your member record and personal details\n"
            "  statement.csv     every movement on your savings, with a "
            "running balance\n"
            "  *.json            contributions, loans, savings, dividends, "
            "votes, notifications\n"
            "  photo.*, documents/   the files you or an officer uploaded\n"))
        zf.writestr("account.json", _json({
            "email": user.email, "full_name": user.full_name,
            "phone": user.phone, "national_id": user.national_id,
            "two_factor_enabled": user.two_factor_enabled,
            "created_at": user.created_at,
        }))

        for m in memberships_of(user):
            base = f"{m.cooperative.slug}/"
            profile = {
                "cooperative": m.cooperative.name,
                "member_no": m.member_no, "kind": m.kind,
                "role": m.role.name if m.role else None,
                "status": m.status, "share_capital": m.share_capital,
                "joined_at": m.joined_at, "exited_at": m.exited_at,
                "date_of_birth": m.date_of_birth, "gender": m.gender,
                "address": m.address, "occupation": m.occupation,
                "next_of_kin_name": m.next_of_kin_name,
                "next_of_kin_phone": m.next_of_kin_phone,
                "bank_name": m.bank_name,
                "bank_account_no": m.bank_account_no,
                "verification_status": m.verification_status,
                "savings_balance": m.savings_balance,
            }
            zf.writestr(base + "profile.json", _json(profile))

            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow(["date", "reference", "description", "credit",
                             "debit", "balance"])
            for line in member_statement(m):
                writer.writerow([line["date"], line["reference"],
                                 line["description"], line["credit"],
                                 line["debit"], line["balance"]])
            zf.writestr(base + "statement.csv", out.getvalue())

            loans = Loan.all_objects.filter(membership=m)
            sections = {
                "contributions": _rows(
                    Contribution.all_objects.filter(membership=m)),
                "loans": _rows(loans),
                "loan_instalments": _rows(
                    RepaymentInstalment.all_objects.filter(loan__in=loans)),
                "loan_repayments": _rows(
                    LoanRepayment.all_objects.filter(loan__in=loans)),
                "savings_goals": _rows(
                    SavingsGoal.all_objects.filter(membership=m)),
                "withdrawals": _rows(
                    Withdrawal.all_objects.filter(membership=m)),
                "dividends": _rows(
                    DividendAllocation.all_objects.filter(membership=m)),
                "votes": _rows(Vote.all_objects.filter(membership=m)),
                "meeting_attendance": _rows(
                    Attendance.all_objects.filter(membership=m)),
                "notifications": _rows(
                    Notification.all_objects.filter(membership=m)),
                "notification_preferences": _rows(
                    ChannelPreference.all_objects.filter(membership=m)),
            }
            for name, rows in sections.items():
                zf.writestr(f"{base}{name}.json", _json(rows))

            if m.photo:
                ext = os.path.splitext(m.photo.name)[1] or ".jpg"
                _add_file(zf, m.photo, f"{base}photo{ext}")
            for doc in MemberDocument.all_objects.filter(membership=m):
                ext = (os.path.splitext(doc.file.name)[1]
                       or mimetypes.guess_extension("application/pdf"))
                name = (doc.label or doc.get_doc_type_display()).replace(
                    "/", "-")
                _add_file(zf, doc.file, f"{base}documents/{doc.pk}-{name}{ext}")
    return buf.getvalue()


# ── Deletion ────────────────────────────────────────────────────────────────
def deletion_blockers(user) -> list[dict]:
    """What must be settled before the account can be deleted."""
    from loans.models import Loan

    if user.is_platform_admin:
        return [{"cooperative": None,
                 "message": "Platform staff accounts are closed by the "
                            "platform team."}]

    blockers = []
    for m in memberships_of(user):
        coop = m.cooperative.name

        def add(message, coop=coop):
            blockers.append({"cooperative": coop, "message": message})

        open_loans = Loan.all_objects.filter(
            membership=m, status__in=[Loan.Status.PENDING,
                                      Loan.Status.APPROVED,
                                      Loan.Status.DISBURSED])
        for loan in open_loans:
            add(f"Loan #{loan.pk} is {loan.get_status_display().lower()} — "
                "to be repaid, or the application withdrawn.")
        balance = m.savings_balance or Decimal("0")
        if balance > 0:
            add(f"₦{balance:,.2f} savings to be paid out.")
        if (m.share_capital or 0) > 0:
            add(f"₦{m.share_capital:,.2f} share capital to be returned.")
        if m.role and m.role.is_officer:
            add(f"Holds the office of {m.role.name} — to be handed to another "
                "member.")
    return blockers


def pending_request(user):
    return AccountDeletionRequest.objects.filter(
        user=user, status=AccountDeletionRequest.Status.PENDING).first()


def request_deletion(user, *, source=AccountDeletionRequest.Source.APP,
                     reason="") -> AccountDeletionRequest:
    """Record the request (idempotent) and tell the officers and the person."""
    existing = pending_request(user)
    if existing:
        return existing
    req = AccountDeletionRequest.objects.create(
        user=user, source=source, reason=(reason or "")[:500])
    transaction.on_commit(lambda: _announce_request(req))
    return req


def _announce_request(req):
    from communications.email import send_alert_email
    from communications.receipts import notify_officers_by_email

    user = req.user
    for m in memberships_of(user):
        notify_officers_by_email(
            m.cooperative,
            subject=f"Account deletion requested: {user.full_name}",
            heading="A member has asked for their account to be deleted",
            intro="Settle anything listed below (pay out savings, return "
                  "share capital, close loans), then complete the deletion "
                  "from Deletion requests in the console. Our "
                  "privacy notice promises this within 30 days.",
            rows=[("Member", user.full_name), ("Member no.", m.member_no),
                  ("Reason", req.reason or "—")],
        )
    send_alert_email(
        to=user.email,
        subject="We've received your account deletion request",
        heading="Your deletion request has been received",
        intro="An officer of your cooperative will settle any money due to "
              "or from you, then delete your account — within 30 days. You "
              "can cancel from your profile until then. If you didn't ask "
              "for this, sign in and cancel it, or contact your cooperative.",
    )


def cancel_request(req: AccountDeletionRequest) -> None:
    if req.status != AccountDeletionRequest.Status.PENDING:
        raise DeletionError("This request is no longer pending.")
    req.status = AccountDeletionRequest.Status.CANCELLED
    req.save(update_fields=["status", "updated_at"])


@transaction.atomic
def complete_deletion(req: AccountDeletionRequest, *, actor) -> None:
    """Erase the personal data the privacy notice lists; keep the accounts."""
    from audit.services import record_action
    from communications.email import send_alert_email
    from communications.models import ChannelPreference, Notification
    from rest_framework.authtoken.models import Token

    from accounts.join_models import JoinRequest

    if req.status != AccountDeletionRequest.Status.PENDING:
        raise DeletionError("This request is no longer pending.")
    user = req.user
    blockers = deletion_blockers(user)
    if blockers:
        raise DeletionError("Settle these first: " + " ".join(
            f"{b['cooperative']}: {b['message']}" if b["cooperative"]
            else b["message"] for b in blockers))

    old_email = user.email
    today = timezone.localdate()
    for m in memberships_of(user):
        if m.photo:
            m.photo.delete(save=False)
        for doc in MemberDocument.all_objects.filter(membership=m):
            doc.file.delete(save=False)
            doc.delete()
        Notification.all_objects.filter(membership=m).delete()
        ChannelPreference.all_objects.filter(membership=m).delete()
        for field in ("gender", "address", "occupation", "next_of_kin_name",
                      "next_of_kin_phone", "bank_name", "bank_code",
                      "bank_account_no", "verification_note"):
            setattr(m, field, "")
        m.date_of_birth = None
        m.photo = None
        m.verification = Membership.Verification.AUTOMATIC
        m.verified_by = None
        m.verified_at = None
        m.status = Membership.Status.EXITED
        m.exited_at = m.exited_at or today
        m.save()
        record_action(cooperative=m.cooperative, actor=actor,
                      action="account.deleted", entity=m,
                      after={"member_no": m.member_no,
                             "deletion_request": req.pk})

    JoinRequest.all_objects.filter(email__iexact=old_email).update(
        full_name=DELETED_NAME, email="", phone="", message="")
    Token.objects.filter(user=user).delete()

    user.full_name = DELETED_NAME
    user.email = f"deleted-{user.pk}@deleted.invalid"
    user.phone = ""
    user.national_id = ""
    user.is_active = False
    user.two_factor_enabled = False
    user.set_unusable_password()
    user.save()

    req.status = AccountDeletionRequest.Status.COMPLETED
    req.completed_at = timezone.now()
    req.completed_by = actor
    req.reason = ""
    req.save()

    transaction.on_commit(lambda: send_alert_email(
        to=old_email, subject="Your account has been deleted",
        heading="Your account has been deleted",
        intro="Your sign-in, contact details, personal details, bank "
              "details, photo and documents have been deleted. Your "
              "cooperative keeps its financial records (ledger, contribution "
              "and loan history) under a member number, as its record-keeping "
              "duties require. This is the last email we will send you.",
    ))


# ── Confirming a request made from the public web form ─────────────────────
def deletion_token(user) -> str:
    # The password hash is folded in so that a token stops working the moment
    # the person changes their password.
    return signing.dumps({"u": user.pk, "p": user.password[-12:]},
                         salt=_TOKEN_SALT)


def user_from_token(token: str):
    from accounts.models import User

    try:
        data = signing.loads(token, salt=_TOKEN_SALT, max_age=TOKEN_MAX_AGE)
    except signing.BadSignature:
        return None
    user = User.objects.filter(pk=data.get("u"), is_active=True).first()
    if user is None or user.password[-12:] != data.get("p"):
        return None
    return user


def send_confirmation(email: str) -> None:
    """Email a confirm link if the address has an account. Silent otherwise,
    so the form cannot be used to test which addresses are registered."""
    from accounts.models import User
    from communications.email import _absolute, send_alert_email

    user = User.objects.filter(email__iexact=email.strip(),
                               is_active=True).first()
    if user is None:
        return
    send_alert_email(
        to=user.email,
        subject="Confirm your account deletion request",
        heading="Confirm you want your account deleted",
        intro="Someone — hopefully you — asked for the CooperativeOS account "
              "for this address to be deleted. Confirm below to send the "
              "request to your cooperative. If it wasn't you, ignore this "
              "email; nothing happens without the confirmation. The link "
              "works for 3 days.",
        cta_url=_absolute(
            f"/account/delete/confirm?token={deletion_token(user)}"),
        cta_label="Confirm deletion request",
    )
