from __future__ import annotations

from rest_framework import serializers

from savings.models import SavingsGoal, SavingsProduct, Withdrawal


class SavingsProductSerializer(serializers.ModelSerializer):
    contribution_type_name = serializers.CharField(
        source="contribution_type.name", read_only=True, default=None)
    goal_count = serializers.IntegerField(source="goals.count", read_only=True)

    class Meta:
        model = SavingsProduct
        fields = ["id", "name", "description", "interest_rate",
                  "contribution_type", "contribution_type_name", "active",
                  "goal_count", "created_at"]


class SavingsGoalSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)
    member_no = serializers.CharField(source="membership.member_no",
                                      read_only=True)
    saved_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True)
    progress_pct = serializers.SerializerMethodField()

    class Meta:
        model = SavingsGoal
        fields = ["id", "membership", "member_no", "product", "product_name",
                  "name", "target_amount", "target_date", "saved_amount",
                  "progress_pct", "created_at"]

    def get_progress_pct(self, obj) -> int:
        if not obj.target_amount:
            return 0
        return min(round(obj.saved_amount / obj.target_amount * 100), 100)


class WithdrawalSerializer(serializers.ModelSerializer):
    member_no = serializers.CharField(source="membership.member_no",
                                      read_only=True)
    member_name = serializers.CharField(source="membership.user.full_name",
                                        read_only=True)
    channel_display = serializers.CharField(source="get_channel_display",
                                            read_only=True)
    requested_by_name = serializers.CharField(
        source="requested_by.full_name", read_only=True, default=None)
    is_paid = serializers.BooleanField(read_only=True)
    summary = serializers.CharField(source="describe", read_only=True)

    class Meta:
        model = Withdrawal
        # Everything except the request itself is read-only: a withdrawal is
        # created through savings.services.request_withdrawal (which checks the
        # balance and raises the approval) and is then only ever *paid*, never
        # edited. The destination fields are a snapshot, not an input.
        fields = ["id", "membership", "member_no", "member_name", "amount",
                  "channel", "channel_display", "reason",
                  "destination_bank_name", "destination_account_no",
                  "requested_by", "requested_by_name", "paid_at", "is_paid",
                  "journal", "summary", "created_at"]
        read_only_fields = ["destination_bank_name", "destination_account_no",
                            "requested_by", "paid_at", "journal"]
