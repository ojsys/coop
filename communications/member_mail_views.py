"""
The console's Email page: a society's officers writing to its members
(``communications.member_mailing``).

* ``/member-emails/``                    — drafts and sent messages.
* ``POST /member-emails/preview/``       — who a message would go to.
* ``POST /member-emails/<id>/test/``     — a copy to the officer themself.
* ``POST /member-emails/<id>/send/``     — start (or continue) sending; each
  call delivers for a few seconds and reports what is left.
* ``POST /member-emails/<id>/retry-failed/``
* ``GET  /member-emails/<id>/recipients/``

Officers only, reads included: the recipient lists are members' addresses.
"""
from __future__ import annotations

from django.db.models import Count, Q
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.models import Membership
from communications import member_mailing as mailing
from communications.models import MemberEmail, MemberEmailRecipient
from core.context import get_current_cooperative
from core.pagination import StandardPagination
from core.permissions import IsPrivilegedOfficer
from core.views import TenantScopedViewMixin

_SENT = Q(recipients__status=MemberEmailRecipient.Status.SENT)
_FAILED = Q(recipients__status=MemberEmailRecipient.Status.FAILED)
_PENDING = Q(recipients__status=MemberEmailRecipient.Status.PENDING)


class MemberEmailSerializer(serializers.ModelSerializer):
    purpose_display = serializers.CharField(source="get_purpose_display",
                                            read_only=True)
    audience_display = serializers.CharField(source="get_audience_display",
                                             read_only=True)
    status_display = serializers.CharField(source="get_status_display",
                                           read_only=True)
    audience_role_name = serializers.CharField(source="audience_role.name",
                                               default=None, read_only=True)
    created_by_name = serializers.CharField(source="created_by.full_name",
                                            default=None, read_only=True)
    sent_by_name = serializers.CharField(source="sent_by.full_name",
                                         default=None, read_only=True)
    recipient_count = serializers.IntegerField(read_only=True, default=0)
    sent_count = serializers.IntegerField(read_only=True, default=0)
    failed_count = serializers.IntegerField(read_only=True, default=0)
    pending_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = MemberEmail
        fields = ["id", "purpose", "purpose_display", "subject", "body",
                  "audience", "audience_display", "audience_role",
                  "audience_role_name", "members", "status", "status_display",
                  "created_by_name", "sent_by_name", "sent_at", "finished_at",
                  "recipient_count", "sent_count", "failed_count",
                  "pending_count", "created_at", "updated_at"]
        read_only_fields = ["status", "sent_at", "finished_at"]
        # The generated fields query the tenant-scoped default manager per
        # request, so an id from another society does not validate. (A
        # queryset declared on the class would be fixed before any tenant is
        # bound, and empty.)
        extra_kwargs = {"members": {"required": False},
                        "audience_role": {"required": False}}

    def validate(self, attrs):
        if self.instance and self.instance.status != MemberEmail.Status.DRAFT:
            raise serializers.ValidationError(
                "A sent email cannot be changed.")
        return attrs


class PreviewSerializer(serializers.Serializer):
    audience = serializers.ChoiceField(choices=MemberEmail.Audience.choices)
    audience_role = serializers.IntegerField(required=False, allow_null=True)
    members = serializers.ListField(child=serializers.IntegerField(),
                                    required=False, default=list)


class RecipientSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display",
                                           read_only=True)

    class Meta:
        model = MemberEmailRecipient
        fields = ["id", "address", "name", "member_no", "membership",
                  "status", "status_display", "sent_at"]


def _error(exc):
    return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class MemberEmailViewSet(TenantScopedViewMixin, viewsets.ModelViewSet):
    serializer_class = MemberEmailSerializer
    permission_classes = [IsPrivilegedOfficer]
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = (MemberEmail.objects.order_by("-created_at")
              .select_related("created_by", "sent_by", "audience_role")
              .prefetch_related("members")
              .annotate(recipient_count=Count("recipients"),
                        sent_count=Count("recipients", filter=_SENT),
                        failed_count=Count("recipients", filter=_FAILED),
                        pending_count=Count("recipients", filter=_PENDING)))
        wanted = self.request.query_params.get("status")
        if wanted:
            qs = qs.filter(status=wanted)
        return qs

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user,
                        cooperative=get_current_cooperative())

    def destroy(self, request, *args, **kwargs):
        if self.get_object().status != MemberEmail.Status.DRAFT:
            return Response({"detail": "Only a draft can be deleted — a sent "
                                       "email is the record of what went."},
                            status=status.HTTP_400_BAD_REQUEST)
        return super().destroy(request, *args, **kwargs)

    def _fresh(self, pk):
        return self.get_serializer(self.get_queryset().get(pk=pk)).data

    @action(detail=False, methods=["post"])
    def preview(self, request):
        """Who a message with these settings would go to, before it exists."""
        params = PreviewSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        d = params.validated_data
        resolved = mailing.resolve_recipients(
            get_current_cooperative(), audience=d["audience"],
            role_id=d.get("audience_role"), member_ids=d["members"])
        return Response({
            "count": len(resolved.recipients),
            "opted_out": resolved.opted_out,
            "no_email": resolved.no_email,
            "recipients": [r.__dict__ for r in resolved.recipients],
        })

    @action(detail=True, methods=["post"])
    def test(self, request, pk=None):
        """A copy to the officer, filled in with their own member record."""
        email = self.get_object()
        if not (email.subject.strip() and email.body.strip()):
            return _error("Write a subject and a message first.")
        own = Membership.objects.filter(user=request.user).first()
        values = mailing.values_for(
            email, name=request.user.full_name,
            member_no=own.member_no if own else "", membership=own)
        ok = mailing.send_one(email, address=request.user.email, values=values)
        if not ok:
            return Response({"detail": "The test could not be sent — please "
                                       "try again shortly."}, status=502)
        return Response({"detail": f"Test sent to {request.user.email}."})

    @action(detail=True, methods=["post"])
    def send(self, request, pk=None):
        """Start sending a draft, or continue one already sending.

        Delivers for a few seconds per call; the page calls again while
        ``pending_count`` is above zero.
        """
        email = self.get_object()
        try:
            if email.status == MemberEmail.Status.DRAFT:
                mailing.start_sending(email, actor=request.user)
                email.refresh_from_db()
            if email.status == MemberEmail.Status.SENDING:
                mailing.deliver_pending(email)
        except mailing.MailingError as exc:
            return _error(exc)
        return Response(self._fresh(email.pk))

    @action(detail=True, methods=["post"], url_path="retry-failed")
    def retry_failed(self, request, pk=None):
        try:
            mailing.retry_failed(self.get_object())
        except mailing.MailingError as exc:
            return _error(exc)
        return Response(self._fresh(pk))

    @action(detail=True, methods=["get"])
    def recipients(self, request, pk=None):
        rows = (MemberEmailRecipient.objects
                .filter(email=self.get_object()).order_by("status", "id"))
        wanted = request.query_params.get("status")
        if wanted:
            rows = rows.filter(status=wanted)
        page = self.paginate_queryset(rows)
        return self.get_paginated_response(
            RecipientSerializer(page, many=True).data)
