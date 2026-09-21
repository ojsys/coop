from __future__ import annotations

from rest_framework import serializers

from loans.models import Loan, LoanProduct, LoanRepayment, RepaymentInstalment


class LoanProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoanProduct
        fields = ["id", "name", "interest_rate", "max_amount",
                  "max_term_months", "active", "created_at"]


class LoanRepaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoanRepayment
        fields = ["id", "loan", "amount", "channel", "status", "note",
                  "created_at"]


class RepaymentClaimSerializer(serializers.ModelSerializer):
    """A member-reported transfer, with the context an officer needs to verify
    it (who, which loan, how much still owed)."""

    member_name = serializers.CharField(
        source="loan.membership.user.full_name", read_only=True)
    member_no = serializers.CharField(
        source="loan.membership.member_no", read_only=True)
    member_bank_name = serializers.CharField(
        source="loan.membership.bank_name", read_only=True)
    member_bank_account_no = serializers.CharField(
        source="loan.membership.bank_account_no", read_only=True)
    product_name = serializers.CharField(
        source="loan.product.name", read_only=True)
    outstanding = serializers.DecimalField(
        source="loan.outstanding", max_digits=14, decimal_places=2,
        read_only=True)
    channel_display = serializers.CharField(
        source="get_channel_display", read_only=True)

    class Meta:
        model = LoanRepayment
        fields = ["id", "loan", "amount", "channel", "channel_display",
                  "status", "note", "psp_reference", "member_name", "member_no",
                  "member_bank_name", "member_bank_account_no", "product_name",
                  "outstanding", "created_at"]


class RepaymentInstalmentSerializer(serializers.ModelSerializer):
    outstanding = serializers.DecimalField(max_digits=14, decimal_places=2,
                                           read_only=True)
    is_overdue = serializers.BooleanField(read_only=True)

    class Meta:
        model = RepaymentInstalment
        fields = ["id", "sequence", "due_date", "amount_due",
                  "principal_component", "interest_component", "amount_paid",
                  "outstanding", "status", "is_overdue"]


class LoanSerializer(serializers.ModelSerializer):
    member_no = serializers.CharField(source="membership.member_no",
                                      read_only=True)
    member_name = serializers.CharField(source="membership.user.full_name",
                                        read_only=True)
    # Fields an officer needs to confirm before disbursing (who + where to pay).
    member_phone = serializers.CharField(source="membership.user.phone",
                                         read_only=True)
    member_email = serializers.CharField(source="membership.user.email",
                                         read_only=True)
    member_bank_name = serializers.CharField(source="membership.bank_name",
                                             read_only=True)
    member_bank_account_no = serializers.CharField(
        source="membership.bank_account_no", read_only=True)
    member_share_capital = serializers.DecimalField(
        source="membership.share_capital", max_digits=14, decimal_places=2,
        read_only=True)
    member_photo = serializers.SerializerMethodField()
    product_name = serializers.CharField(source="product.name", read_only=True)
    status_display = serializers.CharField(source="get_status_display",
                                           read_only=True)
    interest = serializers.DecimalField(max_digits=14, decimal_places=2,
                                        read_only=True)
    total_repayable = serializers.DecimalField(max_digits=14, decimal_places=2,
                                               read_only=True)
    outstanding = serializers.DecimalField(max_digits=14, decimal_places=2,
                                           read_only=True)
    repaid_amount = serializers.DecimalField(max_digits=14, decimal_places=2,
                                             read_only=True)
    monthly_instalment = serializers.DecimalField(max_digits=14, decimal_places=2,
                                                  read_only=True)
    repayments = LoanRepaymentSerializer(many=True, read_only=True)
    instalments = RepaymentInstalmentSerializer(many=True, read_only=True)
    # The society's own account, so a member can repay by direct transfer.
    coop_bank = serializers.SerializerMethodField()

    class Meta:
        model = Loan
        fields = ["id", "membership", "member_no", "member_name",
                  "member_phone", "member_email", "member_bank_name",
                  "member_bank_account_no", "member_share_capital",
                  "member_photo", "product", "product_name", "principal",
                  "interest_rate", "term_months", "purpose", "status",
                  "status_display", "interest", "total_repayable", "outstanding",
                  "repaid_amount", "monthly_instalment", "disbursed_at",
                  "repayments", "instalments", "coop_bank", "created_at"]
        read_only_fields = ["status", "interest_rate", "disbursed_at"]

    def get_member_photo(self, obj):
        photo = getattr(obj.membership, "photo", None)
        if not photo:
            return None
        request = self.context.get("request")
        url = photo.url
        return request.build_absolute_uri(url) if request else url

    def get_coop_bank(self, obj):
        coop = obj.cooperative
        if not coop.bank_account_no:
            return None
        return {
            "bank_name": coop.bank_name,
            "account_name": coop.bank_account_name or coop.name,
            "account_no": coop.bank_account_no,
        }

    def create(self, validated_data):
        # Snapshot the product's interest rate onto the loan at application.
        validated_data.setdefault("interest_rate",
                                  validated_data["product"].interest_rate)
        return super().create(validated_data)
