"""
Access codes by text message.

A member with only a phone asks "Forgot password" for a code and gets it by
SMS; an officer's code is texted to the member too. Termii is the provider
(communications.sms); tests use the in-memory backend.
"""
from __future__ import annotations

import re
from datetime import timedelta

import pytest
import requests
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.access_codes import SMS_PER_DAY, AccessCode
from accounts.models import Membership, Role, User
from communications import sms
from communications.models import SmsMessage
from core.context import use_tenant

pytestmark = pytest.mark.django_db

PHONE = "+2348034412290"
NEW_PASSWORD = "maize-harvest-2026"


@pytest.fixture(autouse=True)
def _clear_throttle_cache():
    cache.clear()


@pytest.fixture
def farmer(coop):
    with use_tenant(coop):
        user = User.objects.create_user(phone="08034412290",
                                        full_name="Musa Bello",
                                        password="old-password-2026")
        return Membership.objects.create(user=user, member_no="IMC-0100")


@pytest.fixture
def secretary(coop):
    with use_tenant(coop):
        user = User.objects.create_user(email="sec@imole.test",
                                        full_name="Secretary", password="x")
        return Membership.objects.create(
            user=user, member_no="IMC-sec",
            role=Role.objects.get(slug="secretary"))


def _forgot(username="0803 441 2290"):
    return APIClient().post("/api/v1/auth/password-reset/",
                            {"username": username}, format="json")


def _code_in(text) -> str:
    return re.search(r"(\d{4}) (\d{4})", text).group(0)


def _redeem(code, username="0803 441 2290"):
    return APIClient().post("/api/v1/auth/access-code/", {
        "username": username, "code": code, "password": NEW_PASSWORD},
        format="json")


# ── Forgot password by phone ────────────────────────────────────────────────
def test_forgot_password_texts_a_code_that_sets_the_password(farmer,
                                                             _sms_outbox):
    resp = _forgot()
    assert resp.status_code == 200
    assert resp.json()["next"] == "access_code"
    assert [m["to"] for m in _sms_outbox] == [PHONE]

    code = _code_in(_sms_outbox[0]["body"])
    assert _redeem(code).status_code == 200
    assert APIClient().post("/api/v1/auth/token/", {
        "username": PHONE, "password": NEW_PASSWORD},
        format="json").status_code == 200


def test_a_texted_code_is_short_lived_and_logged_without_its_text(
        farmer, _sms_outbox):
    _forgot()
    row = AccessCode.objects.get(user=farmer.user)
    assert row.origin == AccessCode.Origin.SMS and row.issued_by is None
    assert row.expires_at < timezone.now() + timedelta(hours=1)

    log = SmsMessage.objects.get()
    assert (log.to, log.status, log.cooperative) == (
        PHONE, SmsMessage.Status.SENT, farmer.cooperative)
    code = _code_in(_sms_outbox[0]["body"]).replace(" ", "")
    assert code not in " ".join(str(v) for v in log.__dict__.values())


def test_an_unknown_number_gets_the_same_reply_and_no_text(farmer,
                                                           _sms_outbox):
    known = _forgot()
    cache.clear()
    unknown = _forgot("0809 999 9999")
    assert known.json() == unknown.json()
    assert len(_sms_outbox) == 1


def test_a_number_is_not_texted_twice_within_a_minute(farmer, _sms_outbox):
    _forgot()
    _forgot()
    assert len(_sms_outbox) == 1


def test_a_number_gets_only_so_many_texts_a_day(farmer, _sms_outbox):
    for _ in range(SMS_PER_DAY + 2):
        _forgot()
        cache.clear()
        SmsMessage.objects.update(
            created_at=timezone.now() - timedelta(minutes=5))
    assert len(_sms_outbox) == SMS_PER_DAY


def test_the_daily_limit_caps_every_text(farmer, settings, _sms_outbox):
    settings.SMS_DAILY_LIMIT = 0
    _forgot()
    assert _sms_outbox == []


def test_foreign_numbers_are_not_texted_but_get_their_email(coop,
                                                            _sms_outbox):
    with use_tenant(coop):
        Membership.objects.create(
            user=User.objects.create_user(phone="+233241234567",
                                          email="ada@example.com",
                                          full_name="Ada", password="x"),
            member_no="IMC-0101")
    resp = _forgot("+233 24 123 4567")
    assert resp.json()["next"] == "access_code"
    assert _sms_outbox == []
    assert [m.to for m in mail.outbox] == [["ada@example.com"]]


def test_platform_staff_are_never_texted_a_code(farmer, _sms_outbox):
    farmer.user.is_platform_admin = True
    farmer.user.save()
    _forgot()
    assert _sms_outbox == []
    assert not AccessCode.objects.exists()


def test_no_text_for_someone_with_no_active_membership(farmer, _sms_outbox):
    Membership.all_objects.filter(pk=farmer.pk).update(
        status=Membership.Status.EXITED)
    _forgot()
    assert _sms_outbox == []


def test_a_texted_code_does_not_cancel_the_secretarys(coop, secretary,
                                                      farmer, _sms_outbox):
    client = APIClient()
    client.force_authenticate(secretary.user)
    client.credentials(HTTP_X_COOPERATIVE_ID=str(coop.id))
    officer_code = client.post(
        f"/api/v1/members/{farmer.pk}/access-code/").json()["code"]
    SmsMessage.objects.update(created_at=timezone.now() - timedelta(minutes=5))

    _forgot()  # a stranger asking texts for the farmer's number
    assert _redeem(officer_code).status_code == 200


def test_with_sms_off_a_phone_gets_the_old_reply(farmer, settings,
                                                 _sms_outbox):
    settings.SMS_BACKEND = ""
    resp = _forgot()
    assert "next" not in resp.json()
    assert _sms_outbox == []


# ── An officer's code, texted too ───────────────────────────────────────────
def test_an_officers_code_is_texted_to_the_member(coop, secretary, farmer,
                                                  _sms_outbox):
    client = APIClient()
    client.force_authenticate(secretary.user)
    client.credentials(HTTP_X_COOPERATIVE_ID=str(coop.id))
    body = client.post(f"/api/v1/members/{farmer.pk}/access-code/").json()
    assert body["texted_to"] == PHONE
    assert "texted" in body["detail"]
    assert _code_in(_sms_outbox[0]["body"]).replace(" ", "") == body["code"]
    assert coop.name in _sms_outbox[0]["body"]


def test_with_sms_off_the_officer_reads_the_code_out(coop, secretary, farmer,
                                                     settings, _sms_outbox):
    settings.SMS_BACKEND = ""
    client = APIClient()
    client.force_authenticate(secretary.user)
    client.credentials(HTTP_X_COOPERATIVE_ID=str(coop.id))
    body = client.post(f"/api/v1/members/{farmer.pk}/access-code/").json()
    assert body["texted_to"] == ""
    assert _sms_outbox == []


# ── Termii ──────────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data


def _termii(settings):
    settings.SMS_BACKEND = "termii"
    settings.TERMII_API_KEY = "test-key"
    settings.TERMII_SENDER_ID = "CoopOS"
    settings.TERMII_BASE_URL = "https://termii.test/"
    settings.TERMII_CHANNEL = "dnd"


def test_termii_is_sent_the_number_without_its_plus(settings, monkeypatch):
    _termii(settings)
    calls = []

    def fake_post(url, json, timeout):
        calls.append((url, json))
        return _Resp(200, {"message_id": "3017544054459493", "code": "ok"})

    monkeypatch.setattr(requests, "post", fake_post)
    assert sms.send_sms(PHONE, "hello", purpose="test")
    url, body = calls[0]
    assert url == "https://termii.test/api/sms/send"
    assert body == {"api_key": "test-key", "to": "2348034412290",
                    "from": "CoopOS", "sms": "hello", "type": "plain",
                    "channel": "dnd"}
    assert SmsMessage.objects.get().provider_id == "3017544054459493"


def test_a_termii_refusal_is_logged_not_raised(settings, monkeypatch):
    _termii(settings)
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(
        400, {"message": "Sender ID not approved"}))
    assert sms.send_sms(PHONE, "hello", purpose="test") is False
    log = SmsMessage.objects.get()
    assert log.status == SmsMessage.Status.FAILED
    assert log.error == "Sender ID not approved"


def test_termii_unreachable_is_logged_not_raised(settings, monkeypatch):
    _termii(settings)

    def down(*a, **k):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(requests, "post", down)
    assert sms.send_sms(PHONE, "hello", purpose="test") is False
    assert SmsMessage.objects.get().status == SmsMessage.Status.FAILED


def test_check_warns_when_termii_has_no_sender_id(settings, capsys):
    from django.core.management.base import SystemCheckError

    _termii(settings)
    settings.TERMII_SENDER_ID = ""
    try:
        call_command("check", "--fail-level", "WARNING")
    except SystemCheckError as exc:
        assert "core.W005" in str(exc)
    else:
        pytest.fail("expected core.W005")
