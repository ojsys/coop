from __future__ import annotations

from django.conf import settings
from django.db import IntegrityError
from django.db.models import ProtectedError
from rest_framework import mixins, permissions, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from rest_framework.parsers import FormParser, JSONParser, MultiPartParser

from accounts.models import MemberDocument, Membership, Role, User
from accounts.serializers import (
    MemberDocumentSerializer, MembershipSerializer, RoleSerializer,
    UserSerializer,
)
from core.tenancy import resolve_cooperative
from core.views import TenantScopedViewMixin
from ledger.services import member_statement


class MeView(viewsets.ViewSet):
    """The authenticated user's own profile."""

    def list(self, request):
        return Response(UserSerializer(request.user).data)


class CanManageRoles(permissions.BasePermission):
    """Anyone may read roles; only a privileged member of the active
    cooperative (or a platform admin) may create/edit/delete them."""

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if request.method in permissions.SAFE_METHODS:
            return True
        if user.is_platform_admin:
            return True
        # The tenant isn't bound until the view's initial() runs (after this
        # permission check), so resolve it the same way here.
        coop = resolve_cooperative(
            user, request.META.get(settings.TENANT_HEADER))
        if coop is None:
            return False
        return any(
            m.role and m.role.is_privileged
            for m in Membership.all_objects.filter(
                user=user, cooperative=coop).select_related("role")
        )


class RoleViewSet(TenantScopedViewMixin, viewsets.ModelViewSet):
    """Roles available in the active cooperative (CRUD for privileged members)."""

    serializer_class = RoleSerializer
    permission_classes = [CanManageRoles]

    def get_queryset(self):
        return Role.objects.all()

    def create(self, request, *args, **kwargs):
        try:
            return super().create(request, *args, **kwargs)
        except IntegrityError:
            return Response(
                {"detail": "A role with that slug already exists."},
                status=409,
            )

    def destroy(self, request, *args, **kwargs):
        # Roles are protected by memberships (on_delete=PROTECT); refuse to
        # delete a role that is still assigned rather than 500.
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return Response(
                {"detail": "This role is still assigned to members."},
                status=409,
            )


class MembershipViewSet(TenantScopedViewMixin, viewsets.ModelViewSet):
    """Members of the active cooperative. Tenant-scoped automatically."""

    serializer_class = MembershipSerializer
    permission_classes = [IsAuthenticated]
    # Accept multipart so a headshot can be uploaded alongside member fields.
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        qs = Membership.objects.select_related("user", "role")
        status = self.request.query_params.get("status")
        if status:
            qs = qs.filter(status=status)
        return qs

    @action(detail=True, methods=["get"])
    def statement(self, request, pk=None):
        """The member's dated ledger statement with running balance.

        Rows are listed most-recent-first for on-screen reading; each row still
        carries its running balance as of that transaction (the balance is
        computed oldest-first by the service, then the list is reversed).
        """
        membership = self.get_object()
        return Response({
            "member_no": membership.member_no,
            "savings_balance": membership.savings_balance,
            "lines": list(reversed(member_statement(membership))),
        })

    @action(detail=False, methods=["post"], url_path="import")
    def bulk_import(self, request):
        """Create members in bulk from an uploaded CSV.

        Columns: member_no, full_name, email, phone, share_capital, role_slug.
        Returns per-row results so the admin can fix and re-upload failures.
        """
        import csv
        import io

        from core.context import get_current_cooperative

        upload = request.FILES.get("file")
        if upload is None:
            return Response({"detail": "No file uploaded."}, status=400)

        coop = get_current_cooperative()
        roles = {r.slug: r for r in Role.objects.all()}
        text = io.StringIO(upload.read().decode("utf-8-sig"))
        reader = csv.DictReader(text)

        created, errors = 0, []
        for i, row in enumerate(reader, start=2):  # row 1 is the header
            member_no = (row.get("member_no") or "").strip()
            full_name = (row.get("full_name") or "").strip()
            email = (row.get("email") or "").strip().lower()
            if not member_no or not full_name or not email:
                errors.append({"row": i, "error": "member_no, full_name and "
                                                  "email are required"})
                continue
            if Membership.objects.filter(member_no=member_no).exists():
                errors.append({"row": i,
                               "error": f"member_no {member_no} already exists"})
                continue
            try:
                user, _ = User.objects.get_or_create(
                    email=email,
                    defaults={"full_name": full_name,
                              "phone": (row.get("phone") or "").strip()})
                if Membership.all_objects.filter(cooperative=coop,
                                                 user=user).exists():
                    errors.append({"row": i,
                                   "error": f"{email} is already a member"})
                    continue
                Membership.objects.create(
                    user=user, member_no=member_no,
                    share_capital=(row.get("share_capital") or 0) or 0,
                    role=roles.get((row.get("role_slug") or "").strip()))
                created += 1
            except Exception as exc:  # noqa: BLE001 — surface any row error
                errors.append({"row": i, "error": str(exc)})

        return Response({"created": created, "errors": errors,
                         "total": created + len(errors)})


class MemberDocumentViewSet(TenantScopedViewMixin,
                            mixins.ListModelMixin, mixins.CreateModelMixin,
                            mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """KYC / supporting documents for members of the active cooperative."""

    serializer_class = MemberDocumentSerializer
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        qs = MemberDocument.objects.select_related("membership")
        membership = self.request.query_params.get("membership")
        if membership:
            qs = qs.filter(membership_id=membership)
        return qs
