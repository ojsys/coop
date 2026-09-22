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

    class Meta:
        model = Plan
        fields = ["id", "name", "tier", "tier_display", "price_monthly",
                  "currency", "min_members", "max_members", "description",
                  "active", "subscriber_count", "created_at"]

    def get_subscriber_count(self, obj) -> int:
        # Annotated in the viewset queryset; fall back to a live count.
        return getattr(obj, "subscriber_count", obj.subscriptions.count())


class SubscriptionSerializer(serializers.ModelSerializer):
    cooperative_name = serializers.CharField(source="cooperative.name",
                                             read_only=True)
    plan_name = serializers.CharField(source="plan.name", read_only=True)
    status_display = serializers.CharField(source="get_status_display",
                                           read_only=True)

    class Meta:
        model = Subscription
        fields = ["id", "cooperative", "cooperative_name", "plan", "plan_name",
                  "status", "status_display", "started_at",
                  "current_period_start", "current_period_end", "created_at"]


class InvoiceSerializer(serializers.ModelSerializer):
    cooperative_name = serializers.CharField(source="cooperative.name",
                                             read_only=True)
    status_display = serializers.CharField(source="get_status_display",
                                           read_only=True)

    class Meta:
        model = Invoice
        fields = ["id", "cooperative", "cooperative_name", "subscription",
                  "number", "period_label", "amount", "currency", "status",
                  "status_display", "issued_at", "due_at", "paid_at",
                  "created_at"]


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

    def get_verification(self, obj) -> dict:
        """The exact DNS record a cooperative admin must add, in plain terms."""
        from django.conf import settings

        target = getattr(settings, "WHITE_LABEL_CNAME_TARGET",
                         "tenants.cooperativeos.africa")
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
    class Meta:
        model = PlatformProfile
        fields = ["name", "brand_color", "logo", "favicon", "support_email",
                  "support_phone", "default_currency", "default_timezone"]


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
