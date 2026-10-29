"""
The platform's Email page: messages from the platform team to cooperatives'
officers (``platform_admin.mailing``).

* ``/platform-emails/``                    — drafts and sent messages.
* ``POST /platform-emails/preview/``       — who a message would go to.
* ``POST /platform-emails/<id>/test/``     — a copy to the admin themself.
* ``POST /platform-emails/<id>/send/``     — start (or continue) sending; each
  call delivers for a few seconds and reports what is left.
* ``POST /platform-emails/<id>/retry-failed/``
* ``GET  /platform-emails/<id>/recipients/``
"""
from __future__ import annotations

from django.db.models import Count, Q
from rest_framework import serializers, status
from rest_framework.decorators import action
from rest_framework.response import Response

from core.pagination import StandardPagination
from platform_admin import mailing
from platform_admin.models import PlatformEmail, PlatformEmailRecipient
from platform_admin.views import PlatformViewSetBase

_SENT = Q(recipients__status=PlatformEmailRecipient.Status.SENT)
_FAILED = Q(recipients__status=PlatformEmailRecipient.Status.FAILED)
_PENDING = Q(recipients__status=PlatformEmailRecipient.Status.PENDING)


class PlatformEmailSerializer(serializers.ModelSerializer):
    audience_display = serializers.CharField(source="get_audience_display",
                                             read_only=True)
    status_display = serializers.CharField(source="get_status_display",
                                           read_only=True)
    cooperative_names = serializers.SerializerMethodField()
    created_by_name = serializers.CharField(source="created_by.full_name",
                                            default=None, read_only=True)
    sent_by_name = serializers.CharField(source="sent_by.full_name",
                                         default=None, read_only=True)
    recipient_count = serializers.IntegerField(read_only=True, default=0)
    sent_count = serializers.IntegerField(read_only=True, default=0)
    failed_count = serializers.IntegerField(read_only=True, default=0)
    pending_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = PlatformEmail
        fields = ["id", "subject", "body", "audience", "audience_display",
                  "cooperatives", "cooperative_names", "only_users",
                  "include_contact_email", "status", "status_display",
                  "created_by_name", "sent_by_name", "sent_at", "finished_at",
                  "recipient_count", "sent_count", "failed_count",
                  "pending_count", "created_at", "updated_at"]
        read_only_fields = ["status", "sent_at", "finished_at"]
        extra_kwargs = {"cooperatives": {"required": False},
                        "only_users": {"required": False}}

    def get_cooperative_names(self, obj):
        return [c.name for c in obj.cooperatives.all()]

    def validate(self, attrs):
        if self.instance and self.instance.status != PlatformEmail.Status.DRAFT:
            raise serializers.ValidationError(
                "A sent email cannot be changed.")
        return attrs


class PreviewSerializer(serializers.Serializer):
    audience = serializers.ChoiceField(choices=PlatformEmail.Audience.choices)
    cooperatives = serializers.ListField(child=serializers.IntegerField(),
                                         required=False, default=list)
    only_users = serializers.ListField(child=serializers.IntegerField(),
                                       required=False, default=list)
    include_contact_email = serializers.BooleanField(default=True)


class RecipientSerializer(serializers.ModelSerializer):
    cooperative_name = serializers.CharField(source="cooperative.name",
                                             default=None, read_only=True)
    status_display = serializers.CharField(source="get_status_display",
                                           read_only=True)

    class Meta:
        model = PlatformEmailRecipient
        fields = ["id", "address", "name", "capacity", "cooperative",
                  "cooperative_name", "status", "status_display", "sent_at"]


def _error(exc):
    return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class PlatformEmailViewSet(PlatformViewSetBase):
    serializer_class = PlatformEmailSerializer
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = (PlatformEmail.objects
              .select_related("created_by", "sent_by")
              .prefetch_related("cooperatives")
              .annotate(recipient_count=Count("recipients"),
                        sent_count=Count("recipients", filter=_SENT),
                        failed_count=Count("recipients", filter=_FAILED),
                        pending_count=Count("recipients", filter=_PENDING)))
        wanted = self.request.query_params.get("status")
        if wanted:
            qs = qs.filter(status=wanted)
        return qs

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def destroy(self, request, *args, **kwargs):
        if self.get_object().status != PlatformEmail.Status.DRAFT:
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
        recipients = mailing.resolve_recipients(
            audience=d["audience"], cooperative_ids=d["cooperatives"],
            only_user_ids=d["only_users"],
            include_contact_email=d["include_contact_email"])
        coops = {r.cooperative_id for r in recipients}
        return Response({
            "count": len(recipients),
            "cooperative_count": len(coops),
            "recipients": [r.__dict__ for r in recipients],
        })

    @action(detail=True, methods=["post"])
    def test(self, request, pk=None):
        email = self.get_object()
        if not (email.subject.strip() and email.body.strip()):
            return _error("Write a subject and a message first.")
        ok = mailing.send_one(email, address=request.user.email,
                              name=request.user.full_name,
                              cooperative_name="Your Cooperative")
        if not ok:
            return Response({"detail": "The test could not be sent — check "
                                       "the mail settings."}, status=502)
        return Response({"detail": f"Test sent to {request.user.email}."})

    @action(detail=True, methods=["post"])
    def send(self, request, pk=None):
        """Start sending a draft, or continue one already sending.

        Delivers for a few seconds per call; the page calls again while
        ``pending_count`` is above zero.
        """
        email = self.get_object()
        try:
            if email.status == PlatformEmail.Status.DRAFT:
                mailing.start_sending(email, actor=request.user)
                email.refresh_from_db()
            if email.status == PlatformEmail.Status.SENDING:
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
        rows = (self.get_object().recipients.select_related("cooperative")
                .order_by("status", "id"))
        wanted = request.query_params.get("status")
        if wanted:
            rows = rows.filter(status=wanted)
        page = self.paginate_queryset(rows)
        return self.get_paginated_response(
            RecipientSerializer(page, many=True).data)
