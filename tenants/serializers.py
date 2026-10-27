from __future__ import annotations

import re

from rest_framework import serializers

from tenants.models import Cooperative


JOIN_CODE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class CooperativeSerializer(serializers.ModelSerializer):
    # The subscription's plan — what the society is billed on and what its
    # member cap follows. ``tier`` is only a coarse size label.
    plan_name = serializers.CharField(source="subscription.plan.name",
                                      default=None, read_only=True)

    class Meta:
        model = Cooperative
        fields = ["id", "name", "slug", "registration_no", "coop_type",
                  "country", "state", "lga", "base_currency", "tier", "status",
                  "brand_color", "logo", "favicon", "member_cap", "plan_name",
                  "bank_name",
                  "bank_account_name", "bank_account_no",
                  "contact_email", "contact_phone", "contact_address",
                  "statement_footer", "join_code", "join_link_enabled",
                  "created_at"]
        # The cap follows the plan (Subscription.sync_member_cap); it is shown,
        # never set.
        read_only_fields = ["status", "member_cap"]


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
                  "favicon",
                  "contact_email", "contact_phone", "contact_address",
                  "statement_footer", "join_code", "join_link_enabled"]

    _BRANDING = ("logo", "favicon", "brand_color")
    # Set by the platform, not the society: billing follows the plan's member
    # band, and a size label a society typed itself showed officers a plan
    # billing never used. (The member cap is not writable by anyone: it
    # follows the plan.)
    _PLATFORM_ONLY = ("tier",)

    def validate_join_code(self, value):
        """The tail of the society's invite link: /join/<code>.

        Lowercase letters, digits and single hyphens, so it survives being read
        aloud at a meeting or typed from a WhatsApp message.
        """
        code = (value or "").strip().lower()
        if not 3 <= len(code) <= 50:
            raise serializers.ValidationError(
                "Use between 3 and 50 characters.")
        if not JOIN_CODE_RE.match(code):
            raise serializers.ValidationError(
                "Use only letters, numbers and single hyphens between words "
                "(e.g. imole-farmers).")
        taken = Cooperative.objects.filter(join_code__iexact=code)
        if self.instance is not None:
            taken = taken.exclude(pk=self.instance.pk)
        if taken.exists():
            raise serializers.ValidationError(
                "Another cooperative is already using that link.")
        return code

    def validate(self, attrs):
        """Changing the society's branding follows its plan.

        A platform admin may still set it — e.g. while onboarding a society
        that is moving to a plan which includes it.
        """
        from core.entitlements import CUSTOM_BRANDING, require_feature

        request = self.context.get("request")
        if request is not None and not getattr(request.user,
                                               "is_platform_admin", False):
            for field in self._PLATFORM_ONLY:
                attrs.pop(field, None)
        changing = [f for f in self._BRANDING if f in attrs and (
            self.instance is None or attrs[f] != getattr(self.instance, f))]
        if (changing and self.instance is not None and request is not None
                and not getattr(request.user, "is_platform_admin", False)):
            require_feature(self.instance, CUSTOM_BRANDING)
        return attrs
