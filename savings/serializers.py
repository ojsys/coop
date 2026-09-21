from __future__ import annotations

from rest_framework import serializers

from savings.models import SavingsGoal, SavingsProduct


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
