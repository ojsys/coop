"""
The platform's Legal pages screen: the legal team editing policy documents
(``LegalDocument``) without a deployment or the Django admin.

* ``/platform-legal/`` — list, create, edit, delete.

Two slugs are wired to routes — ``privacy`` (registered with Google Play) and
``terms`` — so those documents can be edited and unpublished but never renamed
or deleted. Every save is in the audit log, so "who changed the terms, and
when" has an answer.
"""
from __future__ import annotations

from rest_framework import serializers, status
from rest_framework.response import Response

from platform_admin.models import LegalDocument
from platform_admin.views import PlatformViewSetBase

FIXED_SLUGS = {"privacy", "terms"}


class LegalDocumentSerializer(serializers.ModelSerializer):
    fixed = serializers.SerializerMethodField()

    class Meta:
        model = LegalDocument
        fields = ["id", "slug", "title", "body", "effective_date", "published",
                  "order", "fixed", "created_at", "updated_at"]

    def get_fixed(self, obj) -> bool:
        return obj.slug in FIXED_SLUGS

    def validate_slug(self, value):
        if self.instance and self.instance.slug in FIXED_SLUGS \
                and value != self.instance.slug:
            raise serializers.ValidationError(
                f"/{self.instance.slug} is wired into the site and cannot be "
                f"renamed.")
        return value


class PlatformLegalDocumentViewSet(PlatformViewSetBase):
    serializer_class = LegalDocumentSerializer
    pagination_class = None

    def get_queryset(self):
        return LegalDocument.objects.all()

    def _audit(self, verb, doc, before=None):
        from audit.services import record_action
        record_action(actor=self.request.user, action=f"legal.{verb}",
                      entity=doc, before=before,
                      after={"slug": doc.slug, "title": doc.title,
                             "published": doc.published,
                             "effective_date": str(doc.effective_date or "")})

    def perform_create(self, serializer):
        self._audit("create", serializer.save())

    def perform_update(self, serializer):
        old = serializer.instance
        before = {"title": old.title, "published": old.published,
                  "body_length": len(old.body)}
        self._audit("update", serializer.save(), before=before)

    def destroy(self, request, *args, **kwargs):
        doc = self.get_object()
        if doc.slug in FIXED_SLUGS:
            return Response({"detail": f"/{doc.slug} is wired into the site "
                                       "and cannot be deleted. Unpublish it "
                                       "instead."},
                            status=status.HTTP_400_BAD_REQUEST)
        self._audit("delete", doc)
        return super().destroy(request, *args, **kwargs)
