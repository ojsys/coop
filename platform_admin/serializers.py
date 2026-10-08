from __future__ import annotations

from django.contrib.auth import get_user_model
from rest_framework import serializers

from platform_admin.models import (
    Domain, Incident, Invoice, NotificationTemplate, OnboardingItem, Plan,
    PlatformProfile, PlatformTeamMember, ProviderStatus, Subscription,
    SupportTicket,
)

User = get_user_model()


class PlanSerializer(serializers.ModelSerializer):
    tier_display = serializers.CharField(source="get_tier_display",
                                         read_only=True)
    subscriber_count = serializers.SerializerMethodField()
    band_label = serializers.CharField(read_only=True)

    class Meta:
        model = Plan
        fields = ["id", "name", "tier", "tier_display", "price_monthly",
                  "price_annual", "price_is_from", "currency", "min_members",
                  "max_members", "band_label", "description", "features",
                  "extras", "active", "subscriber_count", "created_at"]

    def validate_features(self, value):
        from core.entitlements import CATALOGUE

        unknown = sorted(set(value or []) - set(CATALOGUE))
        if unknown:
            raise serializers.ValidationError(
                f"Unknown feature(s): {', '.join(unknown)}.")
        return sorted(set(value or []))

    def get_subscriber_count(self, obj) -> int:
        # Annotated in the viewset queryset; fall back to a live count.
        return getattr(obj, "subscriber_count", obj.subscriptions.count())


class SubscriptionSerializer(serializers.ModelSerializer):
    cooperative_name = serializers.CharField(source="cooperative.name",
                                             read_only=True)
    plan_name = serializers.CharField(source="plan.name", read_only=True)
    status_display = serializers.CharField(source="get_status_display",
                                           read_only=True)
    cycle_amount = serializers.DecimalField(max_digits=12, decimal_places=2,
                                            read_only=True)

    class Meta:
        model = Subscription
        fields = ["id", "cooperative", "cooperative_name", "plan", "plan_name",
                  "status", "status_display", "billing_cycle",
                  "price_override", "cycle_amount", "monthly_value",
                  "features_grace_until", "started_at",
                  "current_period_start", "current_period_end",
                  "created_at"]
        read_only_fields = ["monthly_value"]

    def validate(self, attrs):
        """An annual subscription needs a price to invoice.

        Institutional is quoted per society, so annual billing on it must carry
        the negotiated figure — otherwise the renewal would have nothing to
        charge and billing would skip it.
        """
        def current(field):
            if field in attrs:
                return attrs[field]
            return getattr(self.instance, field, None)

        plan = current("plan")
        cycle = current("billing_cycle") or Subscription.Cycle.MONTHLY
        if (cycle == Subscription.Cycle.ANNUAL and plan is not None
                and plan.price_annual is None
                and current("price_override") is None):
            raise serializers.ValidationError({
                "price_override": f"{plan.name} has no list annual price — "
                                  f"enter the price quoted to this society."})
        return attrs


class InvoiceSerializer(serializers.ModelSerializer):
    cooperative_name = serializers.CharField(source="cooperative.name",
                                             read_only=True)
    status_display = serializers.CharField(source="get_status_display",
                                           read_only=True)

    class Meta:
        model = Invoice
        # pay_token is deliberately absent: it authorises payment for anyone
        # holding it, so it belongs in the emailed link and nowhere else.
        fields = ["id", "cooperative", "cooperative_name", "subscription",
                  "number", "period_label", "amount", "currency", "status",
                  "status_display", "issued_at", "due_at", "paid_at",
                  "psp_reference", "last_reminder_at", "reminder_count",
                  "created_at"]
        read_only_fields = ["psp_reference", "last_reminder_at",
                            "reminder_count"]


class OnboardingItemSerializer(serializers.ModelSerializer):
    display_name = serializers.CharField(read_only=True)
    stage_display = serializers.CharField(source="get_stage_display",
                                          read_only=True)
    owner_name = serializers.CharField(source="owner.full_name",
                                       read_only=True, default=None)

    class Meta:
        model = OnboardingItem
        fields = ["id", "cooperative", "prospect_name", "display_name",
                  "stage", "stage_display", "owner", "owner_name",
                  "target_go_live", "notes", "created_at", "updated_at"]


class DomainSerializer(serializers.ModelSerializer):
    cooperative_name = serializers.CharField(source="cooperative.name",
                                             read_only=True)
    verification = serializers.SerializerMethodField()

    class Meta:
        model = Domain
        fields = ["id", "cooperative", "cooperative_name", "domain",
                  "is_primary", "dns_status", "ssl_status", "verified_at",
                  "verification", "created_at"]
        read_only_fields = ["verified_at"]

    def validate(self, attrs):
        """A new custom domain needs a plan that includes one."""
        from core.entitlements import (CUSTOM_DOMAIN, has_feature,
                                       not_included_message)

        coop = attrs.get("cooperative")
        if self.instance is None and coop and not has_feature(coop,
                                                              CUSTOM_DOMAIN):
            raise serializers.ValidationError(
                {"cooperative": not_included_message(coop, CUSTOM_DOMAIN)})
        return attrs

    def get_verification(self, obj) -> dict:
        """The exact DNS record a cooperative admin must add, in plain terms."""
        from django.conf import settings

        # No fallback: settings always defines this, and a second copy of the
        # value here is how it drifted out of step in the first place.
        target = settings.WHITE_LABEL_CNAME_TARGET
        return {
            "record_type": "CNAME",
            "host": obj.domain,
            "target": target,
            "instructions": (
                f"In your domain registrar's DNS settings, add a CNAME record "
                f"for '{obj.domain}' pointing to '{target}'. DNS changes can "
                f"take up to a few hours to take effect, then click Verify."
            ),
        }


class ProviderStatusSerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source="get_kind_display",
                                         read_only=True)
    status_display = serializers.CharField(source="get_status_display",
                                           read_only=True)

    class Meta:
        model = ProviderStatus
        fields = ["id", "name", "kind", "kind_display", "status",
                  "status_display", "latency_ms", "checked_at", "created_at"]


class PlatformProfileSerializer(serializers.ModelSerializer):
    # Declared without max_length on purpose. Inherited from the model it would
    # be a MaxLengthValidator, and run_validators fires *before*
    # validate_ga_measurement_id — so a pasted Google tag (a few hundred
    # characters) was rejected for length before the ID could be lifted out of
    # it. The stored value is still short; only the input is allowed to be long.
    ga_measurement_id = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=False,
    )

    class Meta:
        model = PlatformProfile
        fields = ["name", "brand_color", "logo", "favicon", "support_email",
                  "support_phone", "default_currency", "default_timezone",
                  "ga_measurement_id"]

    def validate_ga_measurement_id(self, value):
        """Accept a bare ID or the whole pasted snippet; store just the ID."""
        from platform_admin.analytics import extract_measurement_id

        try:
            return extract_measurement_id(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc


class NotificationTemplateSerializer(serializers.ModelSerializer):
    channel_display = serializers.CharField(source="get_channel_display",
                                            read_only=True)

    class Meta:
        model = NotificationTemplate
        fields = ["id", "key", "name", "channel", "channel_display", "subject",
                  "body", "active", "created_at"]


class IncidentSerializer(serializers.ModelSerializer):
    severity_display = serializers.CharField(source="get_severity_display",
                                             read_only=True)
    status_display = serializers.CharField(source="get_status_display",
                                           read_only=True)

    class Meta:
        model = Incident
        fields = ["id", "title", "severity", "severity_display", "status",
                  "status_display", "note", "started_at", "resolved_at",
                  "created_at"]


class SupportTicketSerializer(serializers.ModelSerializer):
    cooperative_name = serializers.CharField(source="cooperative.name",
                                             read_only=True)
    created_by_name = serializers.CharField(source="created_by.full_name",
                                            read_only=True, default=None)
    priority_display = serializers.CharField(source="get_priority_display",
                                             read_only=True)
    status_display = serializers.CharField(source="get_status_display",
                                           read_only=True)

    class Meta:
        model = SupportTicket
        fields = ["id", "cooperative", "cooperative_name", "subject", "body",
                  "priority", "priority_display", "status", "status_display",
                  "created_by", "created_by_name", "closed_at", "created_at"]
        read_only_fields = ["status", "closed_at", "created_by"]


class PlatformTeamMemberSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="user.full_name", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)
    role_display = serializers.CharField(source="get_role_display",
                                         read_only=True)
    # Write-only fields used when inviting a brand-new team member.
    invite_email = serializers.EmailField(write_only=True, required=False)
    invite_name = serializers.CharField(write_only=True, required=False)

    class Meta:
        model = PlatformTeamMember
        fields = ["id", "user", "name", "email", "role", "role_display",
                  "active", "invite_email", "invite_name", "created_at"]
        extra_kwargs = {"user": {"required": False}}

    def validate(self, attrs):
        # On create you must either point at an existing user or supply an
        # email to invite a new one.
        if self.instance is None and not attrs.get("user") \
                and not attrs.get("invite_email"):
            raise serializers.ValidationError(
                "Provide either 'user' or 'invite_email'."
            )
        return attrs

    def create(self, validated_data):
        invite_email = validated_data.pop("invite_email", None)
        invite_name = validated_data.pop("invite_name", "")
        if invite_email and not validated_data.get("user"):
            user, _ = User.objects.get_or_create(
                email=invite_email,
                defaults={"full_name": invite_name or invite_email,
                          "is_platform_admin": True, "is_staff": True},
            )
            # Ensure the invited user can actually access the platform.
            if not user.is_platform_admin:
                user.is_platform_admin = True
                user.is_staff = True
                user.save(update_fields=["is_platform_admin", "is_staff"])
            validated_data["user"] = user
        return super().create(validated_data)
