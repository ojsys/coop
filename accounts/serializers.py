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
    # The society's own branding, so each surface can wear it rather than the
    # CooperativeOS mark. Null until an officer uploads one.
    cooperative_logo = serializers.ImageField(source="cooperative.logo",
                                              read_only=True)
    cooperative_favicon = serializers.ImageField(source="cooperative.favicon",
                                                 read_only=True)
    cooperative_brand_color = serializers.CharField(
        source="cooperative.brand_color", read_only=True)
    role_slug = serializers.CharField(source="role.slug", default=None)
    role_name = serializers.CharField(source="role.name", default=None)
    is_privileged = serializers.SerializerMethodField()
    is_officer = serializers.SerializerMethodField()
    # What the society's plan includes, so each surface can offer only what
    # will work instead of letting an officer or member hit a refusal.
    plan = serializers.SerializerMethodField()

    class Meta:
        model = Membership
        fields = ["id", "cooperative_id", "cooperative_name",
                  "cooperative_slug", "cooperative_logo",
                  "cooperative_favicon", "cooperative_brand_color",
                  "member_no", "role_slug", "role_name",
                  "is_privileged", "is_officer", "status", "share_capital",
                  "plan"]

    def get_plan(self, obj) -> dict:
        from core.entitlements import features_for, in_grace, subscription_for

        sub = subscription_for(obj.cooperative)
        return {
            "name": sub.plan.name if sub else None,
            "features": sorted(features_for(obj.cooperative)),
            # Set while the society keeps every feature regardless of plan, so
            # the console can say when that ends.
            "grace_until": (sub.features_grace_until
                            if sub and in_grace(sub) else None),
        }

    def to_representation(self, instance):
        from core.entitlements import CUSTOM_BRANDING

        data = super().to_representation(instance)
        # Branding is a plan feature: without it every surface wears the
        # CooperativeOS look, whatever was uploaded while it was included.
        if CUSTOM_BRANDING not in data["plan"]["features"]:
            data["cooperative_logo"] = None
            data["cooperative_favicon"] = None
            data["cooperative_brand_color"] = "#0b4f3a"
        return data

    def get_is_privileged(self, obj) -> bool:
        return bool(obj.role and obj.role.is_privileged)

    def get_is_officer(self, obj) -> bool:
        """Holds an office of any kind — the console's own entry rule.

        Exposed so the frontend can stop inferring it from ``role_slug !==
        'member'``, which diverges from the backend the moment a cooperative
        adds a role of its own.
        """
        return bool(obj.role and obj.role.is_officer)


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

    def validate(self, attrs):
        """Do not let a permission edit strip the last officers of privilege.

        Editing a role is how permissions are tuned — the point of the screen
        — but is_privileged is computed from this list, so clearing it demotes
        every holder at once and can empty a cooperative of officers.
        """
        from accounts.join_services import (
            LastOfficerError, check_role_keeps_officers,
        )

        if self.instance is None or "permissions" not in attrs:
            return attrs
        try:
            check_role_keeps_officers(self.instance, attrs["permissions"])
        except LastOfficerError as exc:
            raise serializers.ValidationError(
                {"permissions": [str(exc)]}) from exc
        return attrs


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
                  "bank_code", "bank_account_no", "document_count"]

    def to_representation(self, instance):
        """Hand out a signed, expiring URL for the member's photograph.

        ``photo`` stays *writable* so multipart upload keeps working — only the
        output is rewritten. Every client reads whatever string the API returns
        (``<img src={p.photo}>`` in the member app and console, ``NetworkImage``
        in the Flutter app), so this needs no client change.

        MemberSelfSerializer subclasses this, so /me/profile/ is covered too.

        The request is passed through so the URL stays absolute, matching what
        DRF's own FileField returned before — the Flutter client loads this via
        NetworkImage and cannot resolve a relative path.
        """
        from core.media import sign_private_urls

        return sign_private_urls(
            super().to_representation(instance), "photo",
            request=self.context.get("request"),
        )

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

    def validate(self, attrs):
        """Do not let the last officer be demoted or deactivated.

        Role changes are how admin is granted and revoked, which is the point
        — but revoking the only one leaves a cooperative nobody can manage.
        """
        from accounts.join_services import LastOfficerError, check_officer_remains

        if self.instance is None:
            return attrs
        if "role" not in attrs and "status" not in attrs:
            return attrs

        try:
            # Pass only what is actually being changed. Using .get() with a
            # fallback would turn an explicit "clear the role" into "no
            # change" and wave the demotion through.
            changes = {}
            if "role" in attrs:
                changes["new_role"] = attrs["role"]
            if "status" in attrs:
                changes["new_status"] = attrs["status"]
            check_officer_remains(self.instance, **changes)
        except LastOfficerError as exc:
            raise serializers.ValidationError({"role": [str(exc)]}) from exc
        return attrs

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

        bank_touched = any(
            field in validated_data
            for field in ("bank_name", "bank_code", "bank_account_no"))

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        # An officer correcting bank details must refresh a pending loan's
        # destination exactly as the member's own edit does. Without this the
        # same change had different effects depending on who made it, and an
        # officer who fixed a missing bank code found the loan still unpayable.
        if bank_touched:
            from loans.services import refresh_pending_destinations

            refresh_pending_destinations(instance)
        return instance


class MemberSelfSerializer(MembershipSerializer):
    """A member editing their *own* record. They may update their name/phone
    and KYC, but never officer-controlled fields (role, status, member no,
    share capital)."""

    # Statuses in which a payout is pending or has been decided, so the
    # destination must not move under it.
    LIVE_LOAN_STATUSES = ("pending", "approved", "disbursed")
    BANK_FIELDS = ("bank_name", "bank_code", "bank_account_no")

    class Meta(MembershipSerializer.Meta):
        read_only_fields = ["member_no", "role", "status", "share_capital",
                            "joined_at", "exited_at", "savings_balance",
                            "document_count"]

    def validate(self, attrs):
        """Refuse a bank-detail *change* while the member has a live loan.

        The threat is narrow and real: approve a loan, then edit the account
        number, and the payout goes somewhere the officer never vetted. The
        snapshot on the loan already blocks that for approved loans, and this
        closes the same door one step earlier.

        **Filling a blank is allowed**, deliberately. Locking outright would
        trap a member who applied before adding their details: snapshot blank,
        edits refused, loan unpayable for ever. So an empty field can be
        completed, and an existing value cannot be replaced.

        Officers are not restricted here — a mistyped account number has to be
        correctable, and writes through /members/ are privileged and audited.
        """
        attrs = super().validate(attrs)
        if self.instance is None:
            return attrs

        changing = [
            field for field in self.BANK_FIELDS
            if field in attrs
            and (attrs[field] or "") != (getattr(self.instance, field) or "")
            # Filling a blank is a completion, not a redirect.
            and (getattr(self.instance, field) or "")
        ]
        if not changing:
            return attrs

        from loans.models import Loan

        live = (Loan.all_objects
                .filter(membership=self.instance,
                        status__in=self.LIVE_LOAN_STATUSES)
                .exists())
        if live:
            raise serializers.ValidationError({
                changing[0]: [
                    "Your bank details cannot be changed while you have a loan "
                    "application or an active loan, because a loan is paid to "
                    "the account recorded when you applied. Ask an officer of "
                    "your cooperative to correct them."
                ]
            })
        return attrs

    def update(self, instance, validated_data):
        """Keep a PENDING loan's destination tracking the member.

        Only reachable for a blank being filled (see ``validate``). Without
        this, a member who applied before supplying an account would be left
        with a permanently blank destination and an unpayable loan.
        """
        membership = super().update(instance, validated_data)

        if not any(field in validated_data for field in self.BANK_FIELDS):
            return membership

        from loans.services import refresh_pending_destinations

        refresh_pending_destinations(membership)
        return membership


class MemberDocumentSerializer(serializers.ModelSerializer):
    doc_type_display = serializers.CharField(source="get_doc_type_display",
                                             read_only=True)

    class Meta:
        model = MemberDocument
        fields = ["id", "membership", "doc_type", "doc_type_display", "label",
                  "file", "created_at"]

    def to_representation(self, instance):
        """Signed URL for the document, which is KYC material.

        As with the photograph, ``file`` remains writable for upload and only
        the representation changes — the console and member app both render
        ``<a href={d.file}>`` and keep working unmodified. The request keeps the
        URL absolute, as DRF's FileField had it.
        """
        from core.media import sign_private_urls

        return sign_private_urls(
            super().to_representation(instance), "file",
            request=self.context.get("request"),
        )
