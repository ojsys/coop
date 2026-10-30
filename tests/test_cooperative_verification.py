"""
A cooperative's KYC, and what waits on it.

A society is live from signup. These tests pin down the other half of that
bargain: the platform moves no money for a society until a reviewer has
approved who runs it — and the checklist, the submission and the review are
hard to get round.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core import mail
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.email_verification import make_token
from accounts.models import MemberDocument, Membership, Role, User
from audit.models import AuditLog
from core.context import use_tenant
from tenants import verification as kyc
from tenants.models import CooperativeDocument, CooperativeVerification

pytestmark = pytest.mark.django_db

Status = CooperativeVerification.Status


def _unverify(coop):
    coop.verification.status = Status.NOT_STARTED
    coop.verification.save(update_fields=["status", "updated_at"])
    return coop


def _officer(coop, slug, n, *, with_id=True, email_verified=True):
    with use_tenant(coop):
        user = User.objects.create_user(
            email=f"{slug}{n}@exco.test", full_name=f"{slug.title()} {n}",
            password="x")
        if email_verified:
            user.email_verified_at = timezone.now()
            user.save(update_fields=["email_verified_at"])
        membership = Membership.objects.create(
            user=user, member_no=f"EX-{n:03d}",
            role=Role.objects.get(slug=slug))
        if with_id:
            MemberDocument.objects.create(
                membership=membership, doc_type=MemberDocument.DocType.ID,
                file="member_documents/id.pdf")
    return membership


@pytest.fixture
def exco(coop):
    """An unverified society with a full exco, each holding an ID."""
    _unverify(coop)
    return {slug: _officer(coop, slug, i)
            for i, slug in enumerate(kyc.EXCO_ROLES, start=1)}


@pytest.fixture
def certificate(coop):
    with use_tenant(coop):
        return CooperativeDocument.objects.create(
            doc_type=CooperativeDocument.DocType.REGISTRATION_CERTIFICATE,
            file="coop_documents/cert.pdf")


@pytest.fixture
def platform_admin():
    return User.objects.create_user(
        email="ops@platform.test", full_name="Ops", password="x",
        is_platform_admin=True)


def _client(user, coop=None):
    client = APIClient()
    client.force_authenticate(user)
    if coop is not None:
        client.credentials(HTTP_X_COOPERATIVE_ID=str(coop.id))
    return client


# ── The checklist ───────────────────────────────────────────────────────────
def test_a_new_society_is_told_everything_it_still_needs(coop):
    _unverify(coop)
    checklist = kyc.checklist(coop)

    assert not checklist["ready"]
    assert "Appoint a Chairperson." in checklist["missing"]
    assert "Appoint a Treasurer." not in checklist["missing"]
    assert any("certificate of registration" in m
               for m in checklist["missing"])


def test_a_chairperson_without_an_id_is_named(coop, certificate):
    _unverify(coop)
    _officer(coop, "chairperson", 1, with_id=False)

    assert kyc.checklist(coop)["missing"] == [
        "The Chairperson must upload a means of ID."]


def test_a_one_person_society_can_submit(coop, certificate):
    """Only the Chairperson is required; Secretary and Treasurer may be vacant."""
    _unverify(coop)
    chair = _officer(coop, "chairperson", 1)

    checklist = kyc.checklist(coop)
    assert checklist["ready"], checklist["missing"]
    offices = {o["role_slug"]: o for o in checklist["officers"]}
    assert offices["chairperson"]["required"]
    assert not offices["treasurer"]["required"]
    assert offices["treasurer"]["holders"] == []

    kyc.submit(coop, actor=chair.user)
    assert kyc.verification_for(coop).status == Status.SUBMITTED


def test_optional_officers_without_ids_do_not_block(coop, certificate):
    _unverify(coop)
    _officer(coop, "chairperson", 1)
    _officer(coop, "treasurer", 3, with_id=False)

    checklist = kyc.checklist(coop)
    assert checklist["ready"], checklist["missing"]
    offices = {o["role_slug"]: o for o in checklist["officers"]}
    assert offices["treasurer"]["holders"][0]["has_id"] is False


def test_a_full_exco_with_the_certificate_is_ready(coop, exco, certificate):
    checklist = kyc.checklist(coop)
    assert checklist["ready"], checklist["missing"]
    assert all(o["done"] for o in checklist["officers"])


# ── Submitting ──────────────────────────────────────────────────────────────
def test_submitting_an_incomplete_checklist_is_refused(coop, exco):
    with pytest.raises(kyc.VerificationError, match="certificate"):
        kyc.submit(coop, actor=exco["chairperson"].user)
    assert coop.verification.status == Status.NOT_STARTED


def test_submitting_needs_a_confirmed_email(coop, exco, certificate):
    user = exco["chairperson"].user
    user.email_verified_at = None
    user.save(update_fields=["email_verified_at"])

    with pytest.raises(kyc.VerificationError, match="Confirm your email"):
        kyc.submit(coop, actor=user)


def test_an_officer_submits_over_the_api(coop, exco, certificate,
                                         django_capture_on_commit_callbacks):
    mail.outbox.clear()
    client = _client(exco["chairperson"].user, coop)
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post("/api/v1/verification/submit/")

    assert resp.status_code == 200, resp.data
    assert resp.data["status"] == Status.SUBMITTED
    assert any("Verification to review" in m.subject for m in mail.outbox), \
        "the platform team is told there is something to review"
    assert AuditLog.all_objects.filter(
        cooperative=coop, action="cooperative.kyc_submitted").exists()


def test_an_ordinary_member_cannot_see_or_submit(coop, exco, certificate,
                                                 member):
    client = _client(member.user, coop)
    assert client.get("/api/v1/verification/").status_code == 403
    assert client.post("/api/v1/verification/submit/").status_code == 403


def test_papers_under_review_cannot_be_removed(coop, exco, certificate):
    kyc.submit(coop, actor=exco["chairperson"].user)
    client = _client(exco["chairperson"].user, coop)
    resp = client.delete(f"/api/v1/cooperative-documents/{certificate.id}/")
    assert resp.status_code == 400
    assert CooperativeDocument.all_objects.filter(pk=certificate.pk).exists()


# ── Reviewing ───────────────────────────────────────────────────────────────
def test_the_platform_approves_and_the_officers_are_told(
        coop, exco, certificate, platform_admin,
        django_capture_on_commit_callbacks):
    kyc.submit(coop, actor=exco["chairperson"].user)
    mail.outbox.clear()

    client = _client(platform_admin)
    detail = client.get(f"/api/v1/platform-verifications/{coop.id}/")
    assert detail.status_code == 200
    assert len(detail.data["officer_documents"]) == 3, \
        "the reviewer sees each officer's ID"
    assert len(detail.data["documents"]) == 1

    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(
            f"/api/v1/platform-verifications/{coop.id}/approve/")
    assert resp.status_code == 200, resp.data
    assert kyc.is_verified(coop)
    assert any("is verified" in m.subject for m in mail.outbox)


def test_requesting_changes_needs_a_note(coop, exco, certificate,
                                         platform_admin):
    kyc.submit(coop, actor=exco["chairperson"].user)
    client = _client(platform_admin)

    assert client.post(
        f"/api/v1/platform-verifications/{coop.id}/request-changes/",
        {"note": "  "}, format="json").status_code == 400

    resp = client.post(
        f"/api/v1/platform-verifications/{coop.id}/request-changes/",
        {"note": "The certificate is cut off."}, format="json")
    assert resp.status_code == 200
    checklist = kyc.checklist(coop)
    assert checklist["status"] == Status.CHANGES_REQUESTED
    assert checklist["reviewer_note"] == "The certificate is cut off."


def test_a_society_can_resubmit_after_changes(coop, exco, certificate,
                                              platform_admin):
    kyc.submit(coop, actor=exco["chairperson"].user)
    kyc.request_changes(coop, actor=platform_admin, note="Blurry ID")
    kyc.submit(coop, actor=exco["chairperson"].user)
    assert coop.verification.__class__.objects.get(
        cooperative=coop).status == Status.SUBMITTED


def test_only_a_submission_can_be_decided(coop, exco, platform_admin):
    with pytest.raises(kyc.VerificationError):
        kyc.approve(coop, actor=platform_admin)


def test_officers_cannot_reach_the_review_queue(coop, exco):
    client = _client(exco["chairperson"].user, coop)
    assert client.get("/api/v1/platform-verifications/").status_code == 403
    assert client.post(
        f"/api/v1/platform-verifications/{coop.id}/approve/"
    ).status_code == 403


# ── What waits on verification ──────────────────────────────────────────────
def test_online_contributions_wait_for_verification(coop, member, dues_type):
    _unverify(coop)
    resp = _client(member.user, coop).post(
        "/api/v1/contributions/initiate/",
        {"membership": member.id, "contribution_type": dues_type.id,
         "amount": "5000", "channel": "psp"},
        format="json")

    assert resp.status_code == 403
    assert resp.data["detail"].code == "kyc_required"
    from contributions.models import Contribution
    assert not Contribution.all_objects.filter(cooperative=coop).exists(), \
        "a refusal must leave no pending row behind"


def test_recording_a_cash_contribution_still_works(coop, member, dues_type):
    """Bookkeeping needs no verification — only moving money does."""
    from contributions.services import record_contribution

    _unverify(coop)
    contribution = record_contribution(
        cooperative=coop, membership=member, contribution_type=dues_type,
        amount=Decimal("5000.00"), channel="cash",
        occurred_at=timezone.now())
    assert contribution.pk


def test_funding_the_wallet_waits_for_verification(coop):
    from payments.services import initiate_wallet_topup

    _unverify(coop)
    with pytest.raises(kyc.VerificationRequired):
        initiate_wallet_topup(coop, amount="5000", email="t@x.co")


def test_the_settlement_account_waits_for_verification(coop):
    from payments.services import ensure_subaccount

    _unverify(coop)
    coop.bank_name, coop.bank_code, coop.bank_account_no = (
        "Access Bank", "044", "0123456789")
    coop.save()
    with pytest.raises(kyc.VerificationRequired):
        ensure_subaccount(coop)


def test_electronic_loan_payouts_wait_for_verification(coop):
    from loans.services import LoanError, disburse_loan_electronically

    _unverify(coop)

    class _Loan:  # only what the checks before the payout read
        status = "approved"
        cooperative = coop

    with pytest.raises(LoanError, match="verified"):
        disburse_loan_electronically(_Loan())


# ── Confirming an email ─────────────────────────────────────────────────────
def test_the_link_confirms_the_address():
    user = User.objects.create_user(email="new@x.test", full_name="N",
                                    password="x")
    resp = APIClient().post("/api/v1/auth/verify-email/",
                            {"token": make_token(user)}, format="json")
    assert resp.status_code == 200
    user.refresh_from_db()
    assert user.email_verified_at is not None


def test_a_link_for_an_old_address_does_not_confirm_the_new_one():
    user = User.objects.create_user(email="old@x.test", full_name="N",
                                    password="x")
    token = make_token(user)
    user.email = "new@x.test"
    user.save(update_fields=["email"])

    resp = APIClient().post("/api/v1/auth/verify-email/", {"token": token},
                            format="json")
    assert resp.status_code == 400
    user.refresh_from_db()
    assert user.email_verified_at is None


def test_a_forged_token_is_refused():
    resp = APIClient().post("/api/v1/auth/verify-email/",
                            {"token": "not-a-token"}, format="json")
    assert resp.status_code == 400


def test_a_signed_in_person_can_ask_for_a_new_link():
    user = User.objects.create_user(email="again@x.test", full_name="N",
                                    password="x")
    mail.outbox.clear()
    resp = _client(user).post("/api/v1/me/verify-email/")
    assert resp.status_code == 200
    assert mail.outbox and "/verify-email?token=" in mail.outbox[0].body


# ── Trial reminders ─────────────────────────────────────────────────────────
@pytest.fixture
def trial(coop):
    from platform_admin.models import Plan
    from platform_admin.billing import ensure_subscription

    Plan.objects.create(name="Starter", tier=Plan.Tier.SMALL,
                        price_monthly=Decimal("10000"))
    return ensure_subscription(coop)


def _run(today, coop):
    from platform_admin.billing import run_billing_cycle

    return run_billing_cycle(today=today, cooperative=coop)


def test_a_trial_is_reminded_a_week_out_and_only_once(coop, trial):
    coop.contact_email = "office@imole.test"
    coop.save(update_fields=["contact_email"])
    week_out = trial.current_period_end - timedelta(days=7)

    mail.outbox.clear()
    assert _run(week_out, coop)["trial_reminded"]
    assert any("free trial ends in 7 days" in m.subject for m in mail.outbox)

    mail.outbox.clear()
    assert not _run(week_out + timedelta(days=1), coop)["trial_reminded"], \
        "six days out is still the week-out reminder, already sent"
    assert not mail.outbox


def test_a_missed_reminder_is_caught_up(coop, trial):
    coop.contact_email = "office@imole.test"
    coop.save(update_fields=["contact_email"])
    _run(trial.current_period_end - timedelta(days=7), coop)

    two_out = trial.current_period_end - timedelta(days=2)
    report = _run(two_out, coop)
    assert report["trial_reminded"], "the three-day mark was never sent"
    trial.refresh_from_db()
    assert trial.trial_reminded_days == 3


def test_the_reminder_nudges_an_unverified_society(coop, trial):
    _unverify(coop)
    coop.contact_email = "office@imole.test"
    coop.save(update_fields=["contact_email"])
    mail.outbox.clear()
    _run(trial.current_period_end - timedelta(days=1), coop)

    body = mail.outbox[0].body
    assert "tomorrow" in mail.outbox[0].subject
    assert "not completed verification" in body
