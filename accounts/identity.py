"""
Who a person is: a phone number, an email address, or both.

Most members of a farmers' or market traders' cooperative have a phone and no
email, so an account needs at least one of the two and either signs in. Phones
are stored in one form (E.164, ``+2348034412290``) so the number a member types
at sign-in matches the one an officer typed when adding them, however either
of them spaced it or whether they started with 0 or +234.
"""
from __future__ import annotations

import phonenumbers
from django.conf import settings
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q

# Numbers written without a country code are read as this country's, unless
# the society the person belongs to says otherwise.
DEFAULT_REGION = "NG"


class IdentityError(ValueError):
    """A phone or email that cannot identify anyone, or identifies two people."""


def normalize_phone(raw, region: str | None = None) -> str | None:
    """``raw`` as E.164, ``None`` when blank. Raises IdentityError if invalid."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        number = phonenumbers.parse(raw, (region or DEFAULT_REGION).upper())
    except phonenumbers.NumberParseException as exc:
        raise IdentityError(f"{raw} is not a phone number.") from exc
    if not phonenumbers.is_valid_number(number):
        raise IdentityError(f"{raw} is not a valid phone number.")
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def try_normalize_phone(raw, region: str | None = None) -> str | None:
    """Like normalize_phone, but ``None`` instead of raising. For lookups."""
    try:
        return normalize_phone(raw, region)
    except IdentityError:
        return None


def normalize_email(raw) -> str | None:
    raw = (raw or "").strip().lower()
    return raw or None


def identity_q(*, email=None, phone=None, prefix: str = "") -> Q:
    """Rows whose email or phone matches. Matches nothing when both are blank."""
    q = Q(pk__in=[])
    if email:
        q |= Q(**{f"{prefix}email__iexact": email})
    if phone:
        q |= Q(**{f"{prefix}phone": phone})
    return q


def find_user(*, email=None, phone=None):
    """The one account this email and/or phone belongs to, or ``None``.

    Raises IdentityError when the email is one person's and the phone another's:
    guessing which was meant would attach a membership to the wrong account.
    """
    from accounts.models import User

    if not email and not phone:
        return None
    matches = list(User.objects.filter(identity_q(email=email, phone=phone))[:2])
    if len(matches) > 1:
        raise IdentityError(
            "That email address and phone number belong to two different "
            "accounts. Use one of them, or correct the other.")
    return matches[0] if matches else None


def find_or_create_user(*, full_name, email=None, phone=None, password=None):
    """The account for this email/phone, created (with ``password``) if new.

    Returns ``(user, created)``. An existing account is returned unchanged: a
    public form or an officer must not be able to rename someone or attach a
    new phone to an account that already exists.
    """
    from accounts.models import User

    if not email and not phone:
        raise IdentityError("A phone number or an email address is required.")
    user = find_user(email=email, phone=phone)
    if user is not None:
        return user, False
    user = User.objects.create_user(email=email, phone=phone,
                                    full_name=full_name, password=password)
    if password is None:
        user.set_unusable_password()
        user.save(update_fields=["password"])
    return user, True


def region_for(cooperative) -> str:
    return (getattr(cooperative, "country", "") or DEFAULT_REGION).upper()


def payer_email(user) -> str:
    """An email Paystack will accept for this person.

    Paystack will not open a checkout without one. A member with no email gets
    a stand-in on a domain nobody receives mail for, so their receipt is
    Paystack's on screen and ours in the app.
    """
    if user.email:
        return user.email
    return f"member-{user.pk}@{settings.PAYER_EMAIL_DOMAIN}"


class EmailOrPhoneBackend(ModelBackend):
    """Sign in with the email address or the phone number on the account.

    The login form's ``username`` is whatever the person typed. An ``@`` makes
    it an email; anything else is read as a phone. A number without a country
    code is read as Nigerian; members elsewhere type theirs with + and the code.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        from accounts.models import User

        identifier = (username or kwargs.get(User.USERNAME_FIELD) or "").strip()
        if not identifier or password is None:
            return None
        if "@" in identifier:
            user = User.objects.filter(email__iexact=identifier).first()
        else:
            phone = try_normalize_phone(identifier)
            user = (User.objects.filter(phone=phone).first()
                    if phone else None)
        if user is None:
            # Same cost as a wrong password, so timing does not reveal whether
            # an account exists.
            User().set_password(password)
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
