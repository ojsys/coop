from __future__ import annotations

from rest_framework import serializers

from tenants.models import Cooperative


class CooperativeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Cooperative
        fields = ["id", "name", "slug", "registration_no", "coop_type",
                  "country", "state", "lga", "base_currency", "tier", "status",
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
        # The three bank fields are deliberately absent. They are the one place
        # a single officer could redirect every future payment, so they are not
        # writable here at all — they go through
        # POST /cooperatives/{id}/propose-bank-details/ and are applied only
        # when a different privileged officer approves. Leaving them writable
        # here would make that control bypassable with a plain profile PATCH.
        # They remain readable on CooperativeSerializer.
        fields = ["id", "name", "registration_no", "coop_type", "country",
                  "state", "lga", "base_currency", "tier", "brand_color",
                  "logo",
                  "favicon", "member_cap",
                  "contact_email", "contact_phone", "contact_address",
                  "statement_footer"]
