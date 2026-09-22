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
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from accounts.models import Membership, User
from communications.email import send_password_reset_email

# Deliberately identical whether or not the address is registered: a differing
# response lets anyone test which members belong to a society.
GENERIC_RESPONSE = {
    "detail": "If that address has an account, a reset link is on its way.",
}


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    password = serializers.CharField(min_length=8, write_only=True)


class PasswordResetRequestView(APIView):
    """`POST /auth/password-reset/` — email a reset link."""

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_reset"

    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = User.objects.filter(
            email__iexact=serializer.validated_data["email"], is_active=True,
        ).first()
        if user is not None:
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

        return Response(GENERIC_RESPONSE)


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
        user.save(update_fields=["password", "updated_at"])

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
