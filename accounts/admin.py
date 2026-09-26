"""
Admin for identity, RBAC and cooperative membership.

``User`` is a global identity, so it uses Django's ``UserAdmin`` (re-pointed at
the custom model) with the standard manager — password hashing and the
read-only password hash widget come with it. ``Role``, ``Membership`` and
``MemberDocument`` are tenant-scoped and read through ``all_objects``.

``UserAdmin`` deliberately does *not* require 2FA: an operator must be able to
switch their own ``two_factor_enabled`` on, or a superuser without it could
never unlock the money-touching admins.

NDPA note: ``national_id``, date of birth, address and bank details are
regulated PII. They are kept out of changelist columns and search, and sit in a
collapsed section on the detail page, so they aren't broadcast across list views.
"""
from __future__ import annotations

from decimal import Decimal

from django import forms
from django.contrib import admin
from django.contrib.auth import forms as auth_forms
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.db.models import DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils.html import format_html

from accounts.models import (
    JoinRequest, MemberDocument, Membership, Role, User,
)
from core.admin import (TenantScopedModelAdmin, TenantScopedTabularInline,
                        TwoFactorRequiredMixin)
from ledger.models import Account

_MONEY = DecimalField(max_digits=16, decimal_places=2)
_ZERO = Value(Decimal("0.00"), output_field=_MONEY)


class UserChangeForm(auth_forms.UserChangeForm):
    class Meta(auth_forms.UserChangeForm.Meta):
        model = User


class UserCreationForm(auth_forms.AdminUserCreationForm):
    class Meta(auth_forms.AdminUserCreationForm.Meta):
        model = User
        fields = ("email", "full_name")


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    form = UserChangeForm
    add_form = UserCreationForm

    list_display = ("email", "full_name", "phone", "is_active", "is_staff",
                    "is_platform_admin", "two_factor_enabled")
    list_filter = ("is_active", "is_staff", "is_superuser", "is_platform_admin",
                   "two_factor_enabled", "groups")
    search_fields = ("email", "full_name", "phone")
    ordering = ("email",)
    filter_horizontal = ("groups", "user_permissions")
    readonly_fields = ("last_login", "created_at", "updated_at")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal info", {"fields": ("full_name", "phone")}),
        ("Sensitive (NDPA)", {
            "classes": ("collapse",),
            "description": "Regulated personal data — access is audited.",
            "fields": ("national_id",),
        }),
        ("Permissions", {
            "fields": ("is_active", "is_staff", "is_superuser",
                       "is_platform_admin", "two_factor_enabled",
                       "groups", "user_permissions"),
        }),
        ("Important dates", {
            "classes": ("collapse",),
            "fields": ("last_login", "created_at", "updated_at"),
        }),
    )

    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "full_name", "usable_password",
                       "password1", "password2"),
        }),
    )


class RoleAdminForm(forms.ModelForm):
    """Applies the last-officer rule to permission edits in the admin.

    RoleSerializer enforces it for the API; the admin edits Role.permissions
    directly and would otherwise walk straight past it.
    """

    class Meta:
        model = Role
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        if not self.instance.pk:
            return cleaned
        if "permissions" not in set(self.changed_data or ()):
            return cleaned

        from accounts.join_services import (
            LastOfficerError, check_role_keeps_officers,
        )

        previous = Role.all_objects.get(pk=self.instance.pk)
        try:
            check_role_keeps_officers(previous, cleaned.get("permissions") or [])
        except LastOfficerError as exc:
            raise forms.ValidationError(str(exc)) from exc
        return cleaned


@admin.register(Role)
class RoleAdmin(TwoFactorRequiredMixin, TenantScopedModelAdmin):
    form = RoleAdminForm

    list_display = ("name", "slug", "cooperative", "is_privileged")
    list_filter = ("cooperative", "slug")
    search_fields = ("name", "slug")
    ordering = ("cooperative", "name")
    autocomplete_fields = ("cooperative",)
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("created_at", "updated_at")
    list_select_related = ("cooperative",)

    @admin.display(boolean=True, description="Privileged (2FA required)")
    def is_privileged(self, obj):
        return obj.is_privileged


class MemberDocumentInline(TenantScopedTabularInline):
    model = MemberDocument
    extra = 0
    fields = ("doc_type", "label", "file", "created_at")
    readonly_fields = ("created_at",)


class MembershipAdminForm(forms.ModelForm):
    """Applies the last-officer rule to admin edits.

    MembershipSerializer enforces it for the API, but the admin writes
    Membership.role directly and would otherwise walk straight past it. It
    belongs in the form rather than save_model: Django renders a form error,
    whereas raising from save_model is an uncaught 500.
    """

    class Meta:
        model = Membership
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        if not self.instance.pk:
            return cleaned
        if not ({"role", "status"} & set(self.changed_data or ())):
            return cleaned

        from accounts.join_services import (
            LastOfficerError, check_officer_remains,
        )

        previous = Membership.all_objects.get(pk=self.instance.pk)
        try:
            check_officer_remains(
                previous,
                new_role=cleaned.get("role"),
                new_status=cleaned.get("status", previous.status),
            )
        except LastOfficerError as exc:
            raise forms.ValidationError(str(exc)) from exc
        return cleaned


@admin.register(Membership)
class MembershipAdmin(TwoFactorRequiredMixin, TenantScopedModelAdmin):
    form = MembershipAdminForm

    list_display = ("member_no", "member_name", "cooperative", "role", "status",
                    "share_capital", "savings_balance", "joined_at")
    list_filter = ("status", "cooperative", "role", "gender", "joined_at")
    search_fields = ("member_no", "user__full_name", "user__email",
                     "user__phone")
    ordering = ("cooperative", "member_no")
    date_hierarchy = "joined_at"
    autocomplete_fields = ("cooperative", "user", "role")
    list_select_related = ("user", "role", "cooperative")
    readonly_fields = ("savings_balance", "photo_preview", "created_at",
                       "updated_at")
    inlines = (MemberDocumentInline,)

    fieldsets = (
        (None, {
            "fields": ("cooperative", "user", "member_no", "role", "status"),
        }),
        ("Membership", {
            "fields": ("share_capital", "savings_balance", "joined_at",
                       "exited_at"),
        }),
        ("Profile", {
            "fields": ("photo", "photo_preview", "occupation"),
        }),
        ("Sensitive (NDPA)", {
            "classes": ("collapse",),
            "description": "Regulated personal data — access is audited.",
            "fields": ("date_of_birth", "gender", "address",
                       "next_of_kin_name", "next_of_kin_phone",
                       "bank_name", "bank_account_no"),
        }),
        ("Timestamps", {
            "classes": ("collapse",),
            "fields": ("created_at", "updated_at"),
        }),
    )

    def get_queryset(self, request):
        # Member funds in one aggregate; Membership.savings_balance queries the
        # ledger per call, which would be an N+1 down the changelist.
        liability = Q(ledger_entries__account__kind=Account.Kind.LIABILITY)
        return super().get_queryset(request).annotate(
            _funds_credit=Coalesce(
                Sum("ledger_entries__credit", filter=liability), _ZERO,
                output_field=_MONEY),
            _funds_debit=Coalesce(
                Sum("ledger_entries__debit", filter=liability), _ZERO,
                output_field=_MONEY),
        )

    @admin.display(description="Name", ordering="user__full_name")
    def member_name(self, obj):
        return obj.user.full_name

    @admin.display(description="Savings balance")
    def savings_balance(self, obj):
        """Derived from the append-only ledger — never stored."""
        if obj.pk is None:
            return "—"
        credit = getattr(obj, "_funds_credit", None)
        if credit is None:         # detail page: no annotation, fall back
            return obj.savings_balance
        return credit - obj._funds_debit

    @admin.display(description="Photo")
    def photo_preview(self, obj):
        if not obj.photo:
            return "—"
        return format_html(
            '<img src="{}" alt="" style="max-height:160px;border-radius:4px">',
            obj.photo.url,
        )


@admin.register(JoinRequest)
class JoinRequestAdmin(TenantScopedModelAdmin):
    """A viewer, not an editor.

    Approving a request creates a User and a Membership and emails a
    set-password link — all of that lives in accounts.join_services. Flipping
    `status` here would mark someone admitted without admitting them, so the
    decision belongs in the console, and this stays read-only.
    """

    list_display = ("full_name", "email", "cooperative", "status",
                    "created_at", "decided_by", "decided_at")
    list_filter = ("status", "cooperative", "created_at")
    search_fields = ("full_name", "email", "phone", "message")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_select_related = ("cooperative", "decided_by", "membership")

    def get_readonly_fields(self, request, obj=None):
        return tuple(f.name for f in self.model._meta.fields) + ("membership",)

    def has_add_permission(self, request):
        # Requests arrive from the public join form.
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(MemberDocument)
class MemberDocumentAdmin(TenantScopedModelAdmin):
    list_display = ("membership", "doc_type", "label", "download",
                    "cooperative", "created_at")
    list_filter = ("doc_type", "cooperative", "created_at")
    search_fields = ("label", "membership__member_no",
                     "membership__user__full_name")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    autocomplete_fields = ("cooperative", "membership")
    list_select_related = ("membership", "cooperative")
    readonly_fields = ("download", "created_at", "updated_at")

    @admin.display(description="File")
    def download(self, obj):
        if not obj.file:
            return "—"
        return format_html('<a href="{}" target="_blank">Download</a>',
                           obj.file.url)
