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
    destination_summary = serializers.CharField(source="describe_destination",
                                                read_only=True)
    destination_is_payable = serializers.BooleanField(read_only=True)

    class Meta:
        model = Loan
        fields = ["id", "membership", "member_no", "member_name",
                  "member_phone", "member_email", "member_bank_name",
                  "member_bank_account_no", "member_share_capital",
                  "member_photo", "product", "product_name", "principal",
                  "interest_rate", "term_months", "purpose", "status",
                  "status_display", "interest", "total_repayable", "outstanding",
                  "repaid_amount", "monthly_instalment", "disbursed_at",
                  "repayments", "instalments", "coop_bank",
                  # Where the payout will go, frozen at application. Shown
                  # alongside member_bank_* on purpose: if the two differ, the
                  # member changed their details after applying and the officer
                  # should see that before approving.
                  "destination_bank_name", "destination_bank_code",
                  "destination_account_no", "destination_summary",
                  "destination_is_payable", "created_at"]
        # The destination is never client-supplied: accepting one would let an
        # applicant name the account their own loan is paid into.
        read_only_fields = ["status", "interest_rate", "disbursed_at",
                            "destination_bank_name", "destination_bank_code",
                            "destination_account_no"]

    def get_member_photo(self, obj):
        """A signed, expiring URL for the member's photograph.

        ``member_photos/`` is a private prefix, and this serializer exposes the
        photograph through its own method field — so it needs the same gating as
        accounts/serializers.py. Patching only that file would have left a
        member's photograph publicly readable through /loans/, which is the kind
        of gap a prefix-wide rule is supposed to prevent.
        """
        from core.media import signed_media_url

        photo = getattr(obj.membership, "photo", None)
        if not photo:
            return None
        url = signed_media_url(photo)
        if not url:
            return None
        request = self.context.get("request")
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
        loan = super().create(validated_data)

        # And snapshot where the money will go. Done here rather than in each
        # viewset because both the member path (/me/loans/) and the officer
        # path (/loans/) come through this one method — the same reason the
        # interest rate is captured here.
        #
        # A read-through to membership.bank_account_no would let a member
        # redirect an approved payout by editing their own profile. The
        # destination fields are read-only on this serializer, so a client
        # cannot supply one either.
        loan.snapshot_destination()
        loan.save(update_fields=["destination_bank_name",
                                 "destination_bank_code",
                                 "destination_account_no", "updated_at"])
        return loan
