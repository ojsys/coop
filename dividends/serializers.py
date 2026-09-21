from __future__ import annotations

from rest_framework import serializers

from dividends.models import DividendAllocation, DividendDeclaration


class DividendAllocationSerializer(serializers.ModelSerializer):
    member_no = serializers.CharField(source="membership.member_no",
                                      read_only=True)
    member_name = serializers.CharField(source="membership.user.full_name",
                                        read_only=True)

    class Meta:
        model = DividendAllocation
        fields = ["id", "membership", "member_no", "member_name",
                  "share_capital", "amount"]


class MemberDividendSerializer(serializers.ModelSerializer):
    """A member's own dividend allocation, with the period it belongs to."""
    period_label = serializers.CharField(source="declaration.period_label",
                                         read_only=True)
    status = serializers.CharField(source="declaration.status", read_only=True)
    declared_at = serializers.DateTimeField(source="declaration.created_at",
                                            read_only=True)

    class Meta:
        model = DividendAllocation
        fields = ["id", "period_label", "status", "share_capital", "amount",
                  "declared_at"]


class DividendDeclarationSerializer(serializers.ModelSerializer):
    allocations = DividendAllocationSerializer(many=True, read_only=True)
    member_count = serializers.IntegerField(source="allocations.count",
                                            read_only=True)

    class Meta:
        model = DividendDeclaration
        fields = ["id", "period_label", "total_amount", "note", "status",
                  "posted_at", "member_count", "allocations", "created_at"]
        read_only_fields = ["status", "posted_at"]
