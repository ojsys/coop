from __future__ import annotations

from rest_framework import serializers

from tenants.models import Cooperative


class CooperativeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Cooperative
        fields = ["id", "name", "slug", "registration_no", "coop_type",
                  "state", "lga", "base_currency", "tier", "status",
                  "brand_color", "logo", "favicon", "member_cap", "bank_name",
                  "bank_account_name", "bank_account_no",
                  "contact_email", "contact_phone", "contact_address",
                  "statement_footer", "created_at"]
        read_only_fields = ["status"]


class CooperativeUpdateSerializer(serializers.ModelSerializer):
    """Society-profile editing by a coop admin.

    Excludes identity/lifecycle fields (``slug``, ``status``) that must not
    change from the console.
    """

    class Meta:
        model = Cooperative
        fields = ["id", "name", "registration_no", "coop_type", "state",
                  "lga", "base_currency", "tier", "brand_color", "logo",
                  "favicon", "member_cap",
                  "bank_name", "bank_account_name", "bank_account_no",
                  "contact_email", "contact_phone", "contact_address",
                  "statement_footer"]
