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
    status_display = serializers.CharField(source="get_status_display",
                                           read_only=True)
    created_by_name = serializers.CharField(source="created_by.full_name",
                                            read_only=True, default=None)
    # True while a draft waits for a second officer to approve posting it.
    awaiting_approval = serializers.SerializerMethodField()

    class Meta:
        model = DividendDeclaration
        fields = ["id", "period_label", "method", "rate", "total_amount",
                  "note", "status", "status_display", "awaiting_approval",
                  "posted_at", "reversed_at", "reversal_reason",
                  "member_count", "allocations", "created_by_name",
                  "created_at"]
        read_only_fields = fields

    def get_awaiting_approval(self, obj) -> bool:
        from dividends.services import pending_approval

        return (obj.status == DividendDeclaration.Status.DRAFT
                and pending_approval(obj) is not None)
