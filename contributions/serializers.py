from __future__ import annotations

from django.utils.text import slugify
from rest_framework import serializers

from contributions.models import Contribution, ContributionType


class ContributionTypeSerializer(serializers.ModelSerializer):
    gl_account_name = serializers.CharField(
        source="gl_account.name", read_only=True,
    )
    # Derived from the name when omitted. The console never collected a slug, so
    # every create from it was refused with "This field is required" naming a
    # field the officer could not see or fill. Still writable for clients that
    # set it deliberately.
    slug = serializers.SlugField(max_length=60, required=False)

    class Meta:
        model = ContributionType
        fields = ["id", "name", "slug", "frequency", "kind",
                  "expected_amount", "gl_account", "gl_account_name", "active"]

    def validate(self, attrs):
        if self.instance is None and not attrs.get("slug"):
            attrs["slug"] = self._unique_slug(attrs.get("name", ""))
        return attrs

    def _unique_slug(self, name: str) -> str:
        """A free slug for the active cooperative.

        uniq_contribtype_slug_per_coop is a database constraint, so this checks
        what is taken rather than trusting a bare slugify. The default manager is
        tenant-scoped, which is exactly the scope of the constraint.
        """
        base = slugify(name)[:60] or "contribution"
        taken = set(ContributionType.objects.values_list("slug", flat=True))
        if base not in taken:
            return base
        n = 2
        while True:
            suffix = f"-{n}"
            candidate = f"{base[:60 - len(suffix)]}{suffix}"
            if candidate not in taken:
                return candidate
            n += 1


class ContributionSerializer(serializers.ModelSerializer):
    member_no = serializers.CharField(
        source="membership.member_no", read_only=True,
    )
    type_name = serializers.CharField(
        source="contribution_type.name", read_only=True,
    )

    class Meta:
        model = Contribution
        fields = ["id", "membership", "member_no", "contribution_type",
                  "type_name", "amount", "channel", "status", "psp_reference",
                  "occurred_at", "journal"]
        read_only_fields = ["status", "journal"]


class RecordContributionSerializer(serializers.Serializer):
    """Input for the ``record`` action; posts a balanced journal on save."""

    membership = serializers.IntegerField()
    contribution_type = serializers.IntegerField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    channel = serializers.ChoiceField(choices=Contribution.Channel.choices)
    psp_reference = serializers.CharField(required=False, allow_blank=True)
    occurred_at = serializers.DateTimeField(required=False)
