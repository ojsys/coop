from __future__ import annotations

from rest_framework import serializers

from accounts.models import MemberDocument, Membership, Role, User


class MembershipSummarySerializer(serializers.ModelSerializer):
    """A user's membership in one cooperative — used to route the frontend to
    the correct surface and to power the cooperative switcher.
    """
    cooperative_id = serializers.IntegerField(source="cooperative.id")
    cooperative_name = serializers.CharField(source="cooperative.name")
    cooperative_slug = serializers.CharField(source="cooperative.slug")
    role_slug = serializers.CharField(source="role.slug", default=None)
    role_name = serializers.CharField(source="role.name", default=None)
    is_privileged = serializers.SerializerMethodField()

    class Meta:
        model = Membership
        fields = ["id", "cooperative_id", "cooperative_name",
                  "cooperative_slug", "member_no", "role_slug", "role_name",
                  "is_privileged", "status", "share_capital"]

    def get_is_privileged(self, obj) -> bool:
        return bool(obj.role and obj.role.is_privileged)


class UserSerializer(serializers.ModelSerializer):
    memberships = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "full_name", "email", "phone", "national_id",
                  "is_platform_admin", "two_factor_enabled", "memberships"]
        read_only_fields = ["is_platform_admin"]

    def get_memberships(self, obj):
        # Unscoped: a user seeing their own memberships spans cooperatives and
        # runs before any tenant is bound (mirrors core.tenancy.resolve_cooperative).
        qs = (Membership.all_objects.filter(user=obj)
              .select_related("cooperative", "role"))
        return MembershipSummarySerializer(qs, many=True).data


class RoleSerializer(serializers.ModelSerializer):
    is_privileged = serializers.BooleanField(read_only=True)

    class Meta:
        model = Role
        fields = ["id", "slug", "name", "permissions", "is_privileged"]


class MembershipSerializer(serializers.ModelSerializer):
    # Accept nested user details on create; expose read-only summary.
    full_name = serializers.CharField(source="user.full_name")
    email = serializers.EmailField(source="user.email")
    phone = serializers.CharField(source="user.phone", required=False,
                                  allow_blank=True)
    role_name = serializers.CharField(source="role.name", read_only=True)
    savings_balance = serializers.DecimalField(
        max_digits=16, decimal_places=2, read_only=True,
    )
    document_count = serializers.IntegerField(source="documents.count",
                                              read_only=True)

    class Meta:
        model = Membership
        fields = ["id", "member_no", "full_name", "email", "phone", "role",
                  "role_name", "status", "share_capital", "joined_at",
                  "exited_at", "savings_balance",
                  # KYC / headshot
                  "photo", "date_of_birth", "gender", "address", "occupation",
                  "next_of_kin_name", "next_of_kin_phone", "bank_name",
                  "bank_account_no", "document_count"]

    def create(self, validated_data):
        user_data = validated_data.pop("user")
        user, _ = User.objects.get_or_create(
            email=user_data["email"],
            defaults={
                "full_name": user_data.get("full_name", ""),
                "phone": user_data.get("phone", ""),
            },
        )
        return Membership.objects.create(user=user, **validated_data)

    def update(self, instance, validated_data):
        # Editable: the person's name/phone (on the shared User) plus the
        # membership's role, status, share_capital and member_no. Email is the
        # login identity and is intentionally not changed here.
        user_data = validated_data.pop("user", {})
        user = instance.user
        if "full_name" in user_data:
            user.full_name = user_data["full_name"]
        if "phone" in user_data:
            user.phone = user_data["phone"]
        if user_data:
            user.save()

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance


class MemberSelfSerializer(MembershipSerializer):
    """A member editing their *own* record. They may update their name/phone
    and KYC, but never officer-controlled fields (role, status, member no,
    share capital)."""

    class Meta(MembershipSerializer.Meta):
        read_only_fields = ["member_no", "role", "status", "share_capital",
                            "joined_at", "exited_at", "savings_balance",
                            "document_count"]


class MemberDocumentSerializer(serializers.ModelSerializer):
    doc_type_display = serializers.CharField(source="get_doc_type_display",
                                             read_only=True)

    class Meta:
        model = MemberDocument
        fields = ["id", "membership", "doc_type", "doc_type_display", "label",
                  "file", "created_at"]
