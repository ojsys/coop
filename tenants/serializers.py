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

    _BRANDING = ("logo", "favicon", "brand_color")

    def validate(self, attrs):
        """Changing the society's branding follows its plan.

        A platform admin may still set it — e.g. while onboarding a society
        that is moving to a plan which includes it.
        """
        from core.entitlements import CUSTOM_BRANDING, require_feature

        request = self.context.get("request")
        changing = [f for f in self._BRANDING if f in attrs and (
            self.instance is None or attrs[f] != getattr(self.instance, f))]
        if (changing and self.instance is not None and request is not None
                and not getattr(request.user, "is_platform_admin", False)):
            require_feature(self.instance, CUSTOM_BRANDING)
        return attrs
