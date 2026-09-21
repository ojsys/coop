from __future__ import annotations

from rest_framework import serializers

from contributions.models import Contribution, ContributionType


class ContributionTypeSerializer(serializers.ModelSerializer):
    gl_account_name = serializers.CharField(
        source="gl_account.name", read_only=True,
    )

    class Meta:
        model = ContributionType
        fields = ["id", "name", "slug", "frequency", "kind",
                  "expected_amount", "gl_account", "gl_account_name", "active"]


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
