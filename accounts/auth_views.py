"""
Password reset over the API.

Django ships password-reset *views*, but they are HTML-form based; the SPA needs
JSON. These reuse Django's own :data:`default_token_generator`, so links expire
exactly as Django's do (``PASSWORD_RESET_TIMEOUT``) and are invalidated by a
password change or a login — no bespoke token table to get wrong.
"""
from __future__ import annotations

from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import serializers, status
from rest_framework.authtoken.views import ObtainAuthToken
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from accounts.models import Membership, User
from communications.email import send_password_reset_email
from communications.sms import sms_enabled

# Deliberately identical whether or not the address is registered: a differing
# response lets anyone test which members belong to a society.
GENERIC_RESPONSE = {
    "detail": "If that account has an email address, a reset link is on its "
              "way. If you signed up with only a phone number, ask your "
              "society's secretary for an access code.",
}

# The reply to a phone number when SMS is on. ``next`` tells the app and the
# website to open the code form; it depends only on what was typed, never on
# whether the number is registered.
TEXTED_RESPONSE = {
    "detail": "If that number has an account, we've texted it a code. Enter "
              "it to choose a new password. No text after a few minutes? Ask "
              "your society's secretary for an access code.",
    "next": "access_code",
}


class LoginView(ObtainAuthToken):
    """`POST /auth/token/` — exchange credentials for a token.

    DRF's stock ``obtain_auth_token`` is unthrottled, which left the only
    endpoint that accepts guessed passwords wide open while password reset and
    public signup were both rate limited.

    A per-account backoff would be the stronger control if this is ever
    targeted in earnest; per-IP is the blunt version, chosen here because it
    cannot be used to lock a member out of their own account by guessing at
    their address.

    No authentication classes: the credentials in the body are the whole
    authorisation. With the default SessionAuthentication, a browser still
    holding a Django session (signed in to /admin, say) had DRF enforce CSRF
    here, and the SPA — which sends no CSRF token — could not sign in at all.
    """

    authentication_classes: list = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"


class PasswordResetRequestSerializer(serializers.Serializer):
    # The phone or email the person signs in with. ``email`` is what clients
    # sent before phones could sign in.
    username = serializers.CharField(max_length=254, required=False)
    email = serializers.CharField(max_length=254, required=False)

    def validate(self, attrs):
        identifier = (attrs.get("username") or attrs.get("email") or "").strip()
        if not identifier:
            raise serializers.ValidationError(
                {"username": ["Enter your phone number or email address."]})
        return {"identifier": identifier}


class PasswordResetConfirmSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    password = serializers.CharField(min_length=8, write_only=True)


class PasswordResetRequestView(APIView):
    """`POST /auth/password-reset/` — email a reset link, or text a code.

    A phone number gets a code by text when SMS is on
    (accounts.access_codes.text_me_a_code). A number we cannot text, a
    foreign one say, falls back to the account's email if it has one.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_reset"

    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        identifier = serializer.validated_data["identifier"]
        user = user_for_identifier(identifier)
        by_phone = "@" not in identifier
        if by_phone and sms_enabled():
            from accounts.access_codes import text_me_a_code
            if not text_me_a_code(user) and user is not None and user.email:
                self._email_link(user)
            return Response(TEXTED_RESPONSE)

        # Someone with no email gets an access code from their society
        # instead; the reply is the same so it reveals nothing either way.
        if user is not None and user.email:
            self._email_link(user)
        return Response(GENERIC_RESPONSE)

    @staticmethod
    def _email_link(user):
        # Brand the mail with the society when the user belongs to exactly
        # one; otherwise it goes out under the platform's own brand.
        # all_objects, not user.memberships: a reverse manager inherits
        # TenantManager, which returns nothing unless a tenant is bound —
        # and a password reset is unauthenticated, so none ever is.
        memberships = list(
            Membership.all_objects.filter(user=user)
            .select_related("cooperative")[:2]
        )
        cooperative = (memberships[0].cooperative
                       if len(memberships) == 1 else None)
        send_password_reset_email(user, cooperative=cooperative)


class PasswordResetConfirmView(APIView):
    """`POST /auth/password-reset/confirm/` — set a new password."""

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_reset"

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = self._user_from_uid(data["uid"])
        if user is None or not default_token_generator.check_token(
                user, data["token"]):
            return Response(
                {"detail": "This reset link is invalid or has expired. "
                           "Request a new one."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            validate_password(data["password"], user)
        except DjangoValidationError as exc:
            return Response({"password": list(exc.messages)},
                            status=status.HTTP_400_BAD_REQUEST)

        user.set_password(data["password"])
        # The link arrived in their inbox, so following it proves the address
        # as surely as a verification link would.
        fields = ["password", "updated_at"]
        if user.email_verified_at is None:
            from django.utils import timezone
            user.email_verified_at = timezone.now()
            fields.append("email_verified_at")
        user.save(update_fields=fields)

        from audit.services import record_action
        record_action(cooperative=None, actor=user,
                      action="account.password_reset", entity=user)

        return Response({"detail": "Your password has been changed. "
                                   "You can now sign in."})

    @staticmethod
    def _user_from_uid(uid: str):
        try:
            pk = force_str(urlsafe_base64_decode(uid))
        except (TypeError, ValueError, OverflowError, UnicodeDecodeError):
            return None
        return User.objects.filter(pk=pk, is_active=True).first()


def user_for_identifier(identifier: str):
    """The active account a typed phone or email signs in to, or ``None``."""
    from accounts.identity import try_normalize_phone

    identifier = (identifier or "").strip()
    if "@" in identifier:
        return User.objects.filter(email__iexact=identifier,
                                   is_active=True).first()
    phone = try_normalize_phone(identifier)
    return (User.objects.filter(phone=phone, is_active=True).first()
            if phone else None)


class AccessCodeRedeemSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=254)
    code = serializers.CharField(max_length=32)
    password = serializers.CharField(write_only=True, max_length=128,
                                     trim_whitespace=False)


class AccessCodeRedeemView(APIView):
    """`POST /auth/access-code/` — choose a password with an access code.

    For a member with no email: an officer of their society issued the code
    (accounts.access_codes). A wrong phone and a wrong code get the same
    answer, so the form cannot be used to find out who is registered.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "access_code"

    def post(self, request):
        from accounts.access_codes import AccessCodeError, redeem_access_code

        serializer = AccessCodeRedeemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = user_for_identifier(data["username"])
        try:
            validate_password(data["password"], user)
        except DjangoValidationError as exc:
            return Response({"password": list(exc.messages)},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            if user is None:
                raise AccessCodeError
            redeem_access_code(user=user, code=data["code"],
                               password=data["password"])
        except AccessCodeError:
            return Response(
                {"detail": "That code is not right, or it has expired. Check "
                           "the phone number, or ask for a new code."},
                status=status.HTTP_400_BAD_REQUEST)
        return Response({"detail": "Your password is set. You can now sign "
                                   "in.",
                         "username": user.phone or user.email})


class PasswordChangeSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(min_length=8, write_only=True)


class PasswordChangeView(APIView):
    """`POST /me/password/` — change your own password while signed in.

    Asks for the current password even though the caller holds a token: a
    phone left unlocked on a counter should not be enough to take the account
    over.

    The token is rotated and the new one returned. A user has one token,
    shared by every device they are signed in on, so this signs the others
    out — which is the point when a password is changed because someone else
    knew it — while the device making the change carries straight on.
    """

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_change"

    def post(self, request):
        serializer = PasswordChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        user = request.user

        if not user.check_password(data["current_password"]):
            return Response(
                {"current_password": ["Your current password is not correct."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            validate_password(data["new_password"], user)
        except DjangoValidationError as exc:
            return Response({"new_password": list(exc.messages)},
                            status=status.HTTP_400_BAD_REQUEST)

        user.set_password(data["new_password"])
        user.save(update_fields=["password", "updated_at"])

        from rest_framework.authtoken.models import Token
        Token.objects.filter(user=user).delete()
        token = Token.objects.create(user=user)

        from audit.services import record_action
        record_action(cooperative=None, actor=user,
                      action="account.password_changed", entity=user)

        return Response({"detail": "Your password has been changed.",
                         "token": token.key})


# ── Confirming an email address ─────────────────────────────────────────────
class EmailVerifyView(APIView):
    """`POST /auth/verify-email/` — follow the link sent to an address.

    Public: the link is opened from an inbox, often on a phone where the person
    is not signed in. The signed token is the whole authorisation.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_reset"

    def post(self, request):
        from accounts.email_verification import mark_verified, user_for_token

        user = user_for_token(str(request.data.get("token", "")))
        if user is None:
            return Response(
                {"detail": "This confirmation link is invalid or has expired. "
                           "Sign in and ask for a new one."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        mark_verified(user)
        return Response({"detail": "Thank you — your email is confirmed.",
                         "email": user.email})


class EmailVerifyResendView(APIView):
    """`POST /me/verify-email/` — send the confirmation link again."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_reset"

    def post(self, request):
        from communications.email import send_email_verification

        user = request.user
        if user.email_verified_at is not None:
            return Response({"detail": "Your email is already confirmed."})
        send_email_verification(user)
        return Response({"detail": f"We've sent a new link to {user.email}."})
