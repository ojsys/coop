"""
Identity, RBAC, and cooperative membership.

A ``User`` is a single global identity (a person). A ``Membership`` binds that
person to a specific cooperative with a ``Role`` — RBAC is therefore *scoped per
tenant*: the same person can be a Treasurer in one coop and an ordinary Member
in another, and permissions never cross cooperative boundaries.
"""
from __future__ import annotations

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.contrib.auth.base_user import BaseUserManager
from django.db import models

from core.models import TenantScopedModel, TimeStampedModel


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create(self, email, password, **extra):
        if not email:
            raise ValueError("Users must have an email address")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("is_platform_admin", True)
        return self._create(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin, TimeStampedModel):
    """Global identity. Cooperative-specific data lives on ``Membership``."""

    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=20, blank=True, db_index=True)
    full_name = models.CharField(max_length=200)
    national_id = models.CharField(
        max_length=30, blank=True,
        help_text="e.g. Nigerian NIN (regulated PII under NDPA 2023).",
    )

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    # Startup Ripple internal super-admin (can provision/target any tenant).
    is_platform_admin = models.BooleanField(default=False)
    # 2FA is required for finance/admin roles before handling live money.
    two_factor_enabled = models.BooleanField(default=False)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["full_name"]

    def __str__(self) -> str:
        return f"{self.full_name} <{self.email}>"


class Role(TenantScopedModel, TimeStampedModel):
    """A named bundle of permissions, scoped to one cooperative."""

    # Canonical roles from the PRD personas; coops may add custom roles.
    CHAIRPERSON = "chairperson"
    SECRETARY = "secretary"
    TREASURER = "treasurer"
    MEMBER = "member"

    DEFAULT_ROLES = {
        CHAIRPERSON: ("Chairperson", ["governance.approve", "governance.vote",
                                      "reports.view"]),
        SECRETARY: ("Secretary", ["members.manage", "governance.manage",
                                   "comms.broadcast", "reports.view"]),
        TREASURER: ("Treasurer", ["ledger.post", "ledger.reconcile",
                                  "reports.view"]),
        MEMBER: ("Member", ["self.view", "governance.vote"]),
    }
    # Permissions that touch money or admin and therefore require 2FA.
    PRIVILEGED_PERMISSIONS = {"ledger.post", "ledger.reconcile",
                              "members.manage", "governance.manage"}

    slug = models.SlugField(max_length=50)
    name = models.CharField(max_length=80)
    permissions = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["cooperative", "slug"],
                name="uniq_role_per_coop",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} @ {self.cooperative_id}"

    @property
    def is_privileged(self) -> bool:
        return bool(self.PRIVILEGED_PERMISSIONS.intersection(self.permissions))

    @classmethod
    def seed_defaults(cls, cooperative) -> dict[str, "Role"]:
        """Create the canonical roles for a cooperative. Idempotent."""
        created = {}
        for slug, (name, perms) in cls.DEFAULT_ROLES.items():
            role, _ = cls.all_objects.get_or_create(
                cooperative=cooperative,
                slug=slug,
                defaults={"name": name, "permissions": perms},
            )
            created[slug] = role
        return created


class Membership(TenantScopedModel, TimeStampedModel):
    """A person's relationship with one cooperative."""

    class Status(models.TextChoices):
        PROSPECTIVE = "prospective", "Prospective"
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"
        EXITED = "exited", "Exited"

    user = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="memberships",
    )
    role = models.ForeignKey(
        Role, on_delete=models.PROTECT, related_name="memberships",
        null=True, blank=True,
    )
    member_no = models.CharField(max_length=40)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.ACTIVE,
    )
    share_capital = models.DecimalField(
        max_digits=14, decimal_places=2, default=0,
    )
    joined_at = models.DateField(null=True, blank=True)
    exited_at = models.DateField(null=True, blank=True)

    # Headshot + KYC (NDPA-regulated PII; stored per-membership since a person
    # may belong to several cooperatives).
    class Gender(models.TextChoices):
        FEMALE = "female", "Female"
        MALE = "male", "Male"
        OTHER = "other", "Other"

    photo = models.ImageField(upload_to="member_photos/", null=True, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=10, choices=Gender.choices, blank=True)
    address = models.CharField(max_length=255, blank=True)
    occupation = models.CharField(max_length=120, blank=True)
    next_of_kin_name = models.CharField(max_length=200, blank=True)
    next_of_kin_phone = models.CharField(max_length=20, blank=True)
    bank_name = models.CharField(max_length=120, blank=True)
    bank_account_no = models.CharField(max_length=20, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["cooperative", "member_no"],
                name="uniq_member_no_per_coop",
            ),
            models.UniqueConstraint(
                fields=["cooperative", "user"],
                name="uniq_membership_per_coop_user",
            ),
        ]
        ordering = ["member_no"]

    def __str__(self) -> str:
        return f"{self.member_no} ({self.user.full_name})"

    @property
    def savings_balance(self):
        """Derived member balance — total member funds held for this member.

        Never stored; always summed from the append-only ledger.
        """
        from ledger.services import member_balance

        return member_balance(self)


class MemberDocument(TenantScopedModel, TimeStampedModel):
    """A KYC / supporting document uploaded for a member (ID, passport, etc.)."""

    class DocType(models.TextChoices):
        ID = "id", "Means of ID"
        PASSPORT = "passport", "Passport photo"
        PROOF_OF_ADDRESS = "proof_of_address", "Proof of address"
        FORM = "form", "Membership form"
        OTHER = "other", "Other"

    membership = models.ForeignKey(
        Membership, on_delete=models.CASCADE, related_name="documents",
    )
    doc_type = models.CharField(max_length=20, choices=DocType.choices,
                                default=DocType.OTHER)
    label = models.CharField(max_length=120, blank=True)
    file = models.FileField(upload_to="member_documents/")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.get_doc_type_display()} — {self.membership.member_no}"


# Join requests live in their own module for readability. Imported here because
# Django's app registry only discovers models reachable from <app>/models.py —
# without this line the model exists but no migration is ever generated for it.
from accounts.join_models import JoinRequest  # noqa: E402,F401
