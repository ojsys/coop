from __future__ import annotations

from django.db import IntegrityError
from django.db.models import ProtectedError, Q
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from rest_framework.parsers import FormParser, JSONParser, MultiPartParser

from accounts.models import MemberDocument, Membership, Role, User
from accounts.serializers import (
    MemberDocumentSerializer, MembershipSerializer, RoleSerializer,
    UserSerializer,
)
from core.permissions import IsPrivilegedOfficerOrReadOnly, is_office_holder
from core.views import TenantScopedViewMixin
from ledger.services import member_statement


class MeView(viewsets.ViewSet):
    """The authenticated user's own profile."""

    def list(self, request):
        return Response(UserSerializer(request.user).data)


class RoleViewSet(TenantScopedViewMixin, viewsets.ModelViewSet):
    """Roles available in the active cooperative (CRUD for privileged members)."""

    serializer_class = RoleSerializer
    # Any member may read the roles; only a privileged officer may create, edit
    # or delete one. Under IsAuthenticated a member could mint themselves a role
    # carrying privileged permissions.
    permission_classes = [IsPrivilegedOfficerOrReadOnly]

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
    # Reads open to the cooperative; writes to privileged officers only.
    # MembershipSerializer exposes role, status, member_no and share_capital as
    # writable, so under IsAuthenticated alone any member could PATCH their own
    # membership onto a privileged role and take the society over, or demote its
    # real officers. (MemberSelfSerializer makes those read-only, but it is only
    # wired into /me/profile/.)
    #
    # Reading every member's record — date of birth, address, next of kin,
    # bank account — is for office-holders. An ordinary member reads only their
    # own (the member app fetches its statement here).
    permission_classes = [IsPrivilegedOfficerOrReadOnly]
    # Accept multipart so a headshot can be uploaded alongside member fields.
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    # The console searches, filters and counts the whole list on the client,
    # reading it in pages of up to 500 (core.pagination). Balances and document
    # counts are annotated (Membership.with_list_figures), so each page is one
    # query however large the society.

    def get_queryset(self):
        qs = Membership.with_list_figures(
            Membership.objects.select_related("user", "role", "verified_by"))
        if not is_office_holder(self.request.user, self.request):
            qs = qs.filter(user=self.request.user)
        status = self.request.query_params.get("status")
        if status:
            qs = qs.filter(status=status)
        kind = self.request.query_params.get("kind")
        if kind:
            qs = qs.filter(kind=kind)
        search = (self.request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(Q(user__full_name__icontains=search)
                           | Q(member_no__icontains=search)
                           | Q(user__email__icontains=search)
                           | Q(user__phone__icontains=search))
        return qs.order_by("member_no")

    def retrieve(self, request, *args, **kwargs):
        """A member's full profile. When platform staff open one — for KYC or a
        support check — that look at regulated personal data is audited."""
        membership = self.get_object()
        if request.user.is_platform_admin:
            from audit.services import record_action

            record_action(cooperative=membership.cooperative,
                          actor=request.user,
                          action="member.profile_viewed_by_platform",
                          entity=membership)
        return Response(self.get_serializer(membership).data)

    @action(detail=True, methods=["post"], url_path="convert-to-member")
    def convert_to_member(self, request, pk=None):
        """Make a non-member a full member: they may then vote, hold office,
        hold shares and receive dividends. Their loans, savings and history
        carry over unchanged — it is the same record."""
        from audit.services import record_action

        membership = self.get_object()
        if membership.is_member:
            return Response({"detail": "This person is already a member."},
                            status=400)
        membership.kind = Membership.Kind.MEMBER
        membership.save(update_fields=["kind", "updated_at"])
        record_action(
            cooperative=membership.cooperative, actor=request.user,
            action="member.converted_to_member", entity=membership,
            before={"kind": Membership.Kind.NON_MEMBER},
            after={"kind": Membership.Kind.MEMBER},
        )
        return Response(self.get_serializer(membership).data)

    @action(detail=True, methods=["post"])
    def verify(self, request, pk=None):
        """Give the member the verified badge — after checking their ID, or
        for an officer who knows them in person."""
        from accounts.verification import decide

        membership = decide(self.get_object(), actor=request.user,
                            verify=True)
        return Response(self.get_serializer(membership).data)

    @action(detail=True, methods=["post"])
    def unverify(self, request, pk=None):
        """Decline a member awaiting review, or withdraw their badge. The
        ``note`` is shown to the member, so say what to fix."""
        from accounts.verification import decide

        membership = decide(self.get_object(), actor=request.user,
                            verify=False,
                            note=(request.data.get("note") or "").strip())
        return Response(self.get_serializer(membership).data)

    def perform_create(self, serializer):
        """Create the member, then invite them to set their own password.

        The invitation carries a set-password link rather than a generated
        password — a password mailed in plaintext lives forever in an inbox.
        Delivery never blocks the member being created.
        """
        membership = serializer.save()
        # Imported here: communications imports accounts.models, so a
        # module-level import would close a cycle at app-loading time.
        from communications.email import send_welcome_email

        send_welcome_email(membership)

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
    # KYC files: officers manage them. An ordinary member sees only their own,
    # and uploads through /me/documents/ — never deletes anyone's.
    permission_classes = [IsPrivilegedOfficerOrReadOnly]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        qs = MemberDocument.objects.select_related("membership")
        if not is_office_holder(self.request.user, self.request):
            qs = qs.filter(membership__user=self.request.user)
        membership = self.request.query_params.get("membership")
        if membership:
            qs = qs.filter(membership_id=membership)
        return qs
