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
    """The whole of a member's profile, on one page.

    Two problems this solves.

    **A profile spans two tables.** A person's name and phone live on ``User``
    (one identity, possibly several cooperatives) while everything else lives on
    ``Membership``. Staff asked to "change this member's phone number" therefore
    could not do it from the member's own page — they had to know to go to Users
    instead. ``full_name`` and ``phone`` below are mirrored onto the User record
    on save, so the member's page is the one place to work.

    Email is shown but not editable here on purpose: it is the login identity,
    and changing it changes how someone signs in. ``MembershipSerializer`` makes
    the same choice for the API. The link beside it goes to the User record,
    where the change is a deliberate act rather than a side effect of correcting
    an address.

    **A bank code cannot be typed.** Paystack identifies a bank by code, and a
    wrong one does not fail visibly — it points a payout at the wrong bank. So
    when the catalogue has been loaded this is a dropdown; when it has not, it
    falls back to a text field rather than offering an empty list, exactly as the
    web console's BankSelect does.

    Also applies the last-officer rule, which MembershipSerializer enforces for
    the API: the admin writes Membership.role directly and would otherwise walk
    straight past it. It belongs in the form rather than save_model, because
    Django renders a form error whereas raising from save_model is a 500.
    """

    full_name = forms.CharField(
        max_length=200, required=False,
        help_text="The person's name, stored on their user record.")
    phone = forms.CharField(
        max_length=20, required=False,
        help_text="Stored on their user record, so it is the same in every "
                  "cooperative they belong to.")

    class Meta:
        model = Membership
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        user = getattr(self.instance, "user", None)
        if user is not None and self.instance.pk:
            self.fields["full_name"].initial = user.full_name
            self.fields["phone"].initial = user.phone
        else:
            # Creating a membership: the user is chosen from the picker above,
            # and their name comes with them. Offering to type one here would
            # imply it creates a person, which it does not.
            for name in ("full_name", "phone"):
                self.fields[name].disabled = True
                self.fields[name].help_text = (
                    "Available once the membership has been saved.")

        self._configure_bank_code()

    def _configure_bank_code(self):
        """A dropdown where the catalogue is loaded, free text where it is not."""
        if "bank_code" not in self.fields:
            return
        try:
            from payments.models import Bank

            banks = list(Bank.objects.filter(active=True)
                         .values_list("code", "name"))
        except Exception:                            # noqa: BLE001
            # A missing table (mid-migration) must not break the member page.
            banks = []

        if not banks:
            self.fields["bank_code"].help_text = (
                "The bank catalogue has not been loaded on this server, so a "
                "code cannot be chosen from a list. Load it from Payments → "
                "Banks → “Refresh the catalogue from the provider”. Without a "
                "code this member cannot receive an electronic payout."
            )
            return

        current = self.instance.bank_code if self.instance.pk else ""
        choices = [("", "— no bank —")] + [
            (code, f"{name} ({code})") for code, name in banks
        ]
        # A stored code the provider has since dropped would vanish from the
        # dropdown and be silently cleared on the next save.
        if current and current not in dict(banks):
            choices.insert(1, (current, f"{current} — no longer in the catalogue"))

        self.fields["bank_code"] = forms.ChoiceField(
            choices=choices, required=False, label="Bank",
            help_text="Paystack's code for this bank. Payouts are created from "
                      "the code, not the name.",
        )

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

    def save(self, commit=True):
        """Persist the mirrored User fields alongside the membership.

        Only touched when they actually changed, so saving a membership whose
        person is unchanged does not write to the User table — and two officers
        editing different memberships of the same person cannot clobber each
        other's unrelated edits.
        """
        membership = super().save(commit=commit)

        user = membership.user if membership.pk else None
        if user is None or not commit:
            return membership

        changed = []
        for field in ("full_name", "phone"):
            if field not in self.fields or self.fields[field].disabled:
                continue
            value = self.cleaned_data.get(field, "")
            if value != getattr(user, field):
                setattr(user, field, value)
                changed.append(field)
        if changed:
            user.save(update_fields=changed + ["updated_at"])

        # Same rule as the API: staff fixing a bank code here must refresh a
        # pending loan's destination, or the loan stays unpayable and nothing
        # says why.
        if {"bank_name", "bank_code", "bank_account_no"} & set(
                self.changed_data or ()):
            from loans.services import refresh_pending_destinations

            refresh_pending_destinations(membership)
        return membership


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
    readonly_fields = ("savings_balance", "photo_preview", "email_link",
                       "created_at", "updated_at")
    inlines = (MemberDocumentInline,)

    fieldsets = (
        (None, {
            "fields": ("cooperative", "user", "member_no", "role", "status"),
        }),
        ("Person", {
            "description": "Name and phone are stored on the user record, so a "
                           "change here applies in every cooperative this "
                           "person belongs to. Email is the login identity and "
                           "is changed on the user record itself.",
            "fields": ("full_name", "phone", "email_link"),
        }),
        ("Membership", {
            "fields": ("share_capital", "savings_balance", "joined_at",
                       "exited_at"),
        }),
        ("Profile", {
            "fields": ("photo", "photo_preview", "occupation"),
        }),
        ("Payout destination", {
            "description": "A transfer recipient is created from the bank "
                           "<em>code</em> and the account number. Without a "
                           "code this member cannot be paid electronically and "
                           "their approved loans will sit in the console's "
                           "awaiting-payment queue.",
            "fields": ("bank_code", "bank_name", "bank_account_no"),
        }),
        ("Sensitive (NDPA)", {
            "classes": ("collapse",),
            "description": "Regulated personal data — access is audited.",
            "fields": ("date_of_birth", "gender", "address",
                       "next_of_kin_name", "next_of_kin_phone"),
        }),
        ("Timestamps", {
            "classes": ("collapse",),
            "fields": ("created_at", "updated_at"),
        }),
    )

    @admin.display(description="Email")
    def email_link(self, obj):
        """The login address, with a way to reach the record that owns it."""
        from django.urls import reverse
        from django.utils.html import format_html

        if obj.pk is None or obj.user_id is None:
            return "—"
        return format_html(
            '{} · <a href="{}">edit this person\u2019s user record</a>',
            obj.user.email,
            reverse("admin:accounts_user_change", args=[obj.user_id]),
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
