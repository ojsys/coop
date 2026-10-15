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

    # What an ordinary member holds. Anything beyond this is an office.
    MEMBER_PERMISSIONS = {"self.view", "governance.vote"}

    @property
    def is_privileged(self) -> bool:
        return bool(self.PRIVILEGED_PERMISSIONS.intersection(self.permissions))

    @property
    def is_officer(self) -> bool:
        """Holds an office — any permission beyond an ordinary member's.

        Broader than ``is_privileged`` and deliberately so. A Chairperson holds
        governance.approve and reports.view but none of PRIVILEGED_PERMISSIONS,
        so they cannot post to the ledger or manage members — yet they sit on the
        console to approve things and need to *see* what they are approving.
        This is the line between "may look" and "may act".

        Defined by permissions rather than by slug, because a cooperative can add
        its own roles: a society that creates an "Auditor" role with
        reports.view gets read access without anyone editing this code, and a
        slug comparison would have missed it.
        """
        return bool(set(self.permissions) - self.MEMBER_PERMISSIONS)

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
    """A person's relationship with one cooperative.

    Usually a member. Some societies also serve **non-members** — people who
    borrow, save and use the app but have not joined: they hold no shares, so
    they cannot vote, hold office or share in dividends. They sit on the same
    register (``kind``) so loans, repayments, statements and the ledger work
    for them unchanged, and they count towards the society's pricing band like
    anyone else it serves. An officer can convert one to a member.
    """

    class Status(models.TextChoices):
        PROSPECTIVE = "prospective", "Prospective"
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"
        EXITED = "exited", "Exited"

    class Kind(models.TextChoices):
        MEMBER = "member", "Member"
        NON_MEMBER = "non_member", "Non-member"

    user = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="memberships",
    )
    role = models.ForeignKey(
        Role, on_delete=models.PROTECT, related_name="memberships",
        null=True, blank=True,
    )
    member_no = models.CharField(max_length=40)
    kind = models.CharField(
        max_length=12, choices=Kind.choices, default=Kind.MEMBER,
    )
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
    # Paystack's code for the bank above. A transfer recipient cannot be
    # created from a bank *name* — the API takes a code — so without this no
    # loan can be disbursed and no withdrawal paid out electronically.
    # Blank on every row written before bank selection existed: the member or
    # an officer picks the bank from the list once and it fills in. Kept
    # separate from bank_name rather than replacing it, so an existing record
    # stays readable and a half-migrated row is obvious rather than silently
    # wrong.
    bank_code = models.CharField(max_length=10, blank=True)
    bank_account_no = models.CharField(max_length=20, blank=True)

    # The verified badge. Only an officer gives it. Completing the profile
    # (see ``verification_status``) puts the member in the officers' review
    # queue; that part is computed on read, so it can never drift from the
    # profile and documents it describes.
    class Verification(models.TextChoices):
        AUTOMATIC = "", "Not decided"
        OFFICER = "officer", "Verified by an officer"
        # An officer declined or withdrew it. The member sees the officer's
        # note; updating their profile puts them back in the review queue.
        REVOKED = "revoked", "Declined by an officer"

    verification = models.CharField(
        max_length=10, choices=Verification.choices, blank=True, default="")
    verified_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")
    verified_at = models.DateTimeField(null=True, blank=True)
    # The officer's reason when declining or withdrawing — shown to the
    # member, so "the ID photo is unreadable" tells them what to fix.
    verification_note = models.CharField(max_length=255, blank=True)

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
    def is_member(self) -> bool:
        """A full member: may vote, hold office and receive dividends."""
        return self.kind == self.Kind.MEMBER

    # A complete profile, as steps the member works through. Each requirement
    # is (label, check). Bank details are left out on purpose: they are about
    # paying the member, not about who the member is.
    PROFILE_STEPS = (
        ("details", "Personal details", (
            ("Full name", lambda m: m.user.full_name),
            ("Phone number", lambda m: m.user.phone),
            ("Date of birth", lambda m: m.date_of_birth),
            ("Gender", lambda m: m.gender),
            ("Occupation", lambda m: m.occupation),
        )),
        ("contact", "Address & next of kin", (
            ("Address", lambda m: m.address),
            ("Next of kin", lambda m: m.next_of_kin_name),
            ("Next of kin's phone", lambda m: m.next_of_kin_phone),
        )),
        # The profile picture, or a passport photograph uploaded as a document:
        # members reasonably read "photo" as either, and both show their face.
        ("photo", "Photo", (
            ("Photo", lambda m: m.photo or m.has_passport_photo),
        )),
        ("id", "Means of ID", (
            ("Means of ID document", lambda m: m.has_id_document),
        )),
    )

    def _has_document(self, doc_type, listed_attr) -> bool:
        listed = getattr(self, listed_attr, None)
        if listed is not None:
            return listed
        return self.documents.filter(doc_type=doc_type).exists()

    @property
    def has_id_document(self) -> bool:
        return self._has_document(MemberDocument.DocType.ID,
                                  "listed_has_id_document")

    @property
    def has_passport_photo(self) -> bool:
        return self._has_document(MemberDocument.DocType.PASSPORT,
                                  "listed_has_passport_photo")

    @property
    def profile_steps(self) -> list[dict]:
        steps = []
        for key, label, requirements in self.PROFILE_STEPS:
            missing = [name for name, present in requirements
                       if not present(self)]
            steps.append({"key": key, "label": label, "done": not missing,
                          "missing": missing})
        return steps

    @property
    def profile_percent(self) -> int:
        total = sum(len(reqs) for _, _, reqs in self.PROFILE_STEPS)
        missing = len(self.verification_missing)
        return round(100 * (total - missing) / total)

    @property
    def verification_missing(self) -> list[str]:
        """Everything still needed before an officer can review the member."""
        return [name for step in self.profile_steps for name in step["missing"]]

    @property
    def verification_status(self) -> str:
        """``verified`` | ``withdrawn`` | ``awaiting`` | ``incomplete``.

        Completing the profile does not verify anyone: software never looks
        at the uploaded ID. It puts the member in the officers' queue
        (``awaiting``), and only an officer's decision gives the badge.
        """
        if self.verification == self.Verification.OFFICER:
            return "verified"
        if self.verification == self.Verification.REVOKED:
            return "withdrawn"
        return "incomplete" if self.verification_missing else "awaiting"

    @property
    def verified_via(self) -> str | None:
        return "officer" if self.is_verified else None

    @property
    def is_verified(self) -> bool:
        return self.verification == self.Verification.OFFICER

    @property
    def savings_balance(self):
        """Derived member balance — total member funds held for this member.

        Never stored; always summed from the append-only ledger.
        """
        listed = getattr(self, "listed_balance", None)
        if listed is not None:
            return listed
        from ledger.services import member_balance

        return member_balance(self)

    @classmethod
    def with_list_figures(cls, qs):
        """Annotate each row's savings balance, document count and whether it
        has a means of ID (for the verified badge).

        A list of members otherwise costs two queries per member — one ledger
        sum for the balance, one count of documents — which is thousands for a
        large society. The balance is the same sum ``member_balance`` takes:
        credits less debits on liability accounts, from the ledger.
        """
        from django.db.models import (Count, DecimalField, Exists, F,
                                      OuterRef, Subquery, Sum, Value)
        from django.db.models.functions import Coalesce

        from ledger.models import Account, LedgerEntry

        money = DecimalField(max_digits=16, decimal_places=2)
        balance = (LedgerEntry.all_objects
                   .filter(membership=OuterRef("pk"),
                           account__kind=Account.Kind.LIABILITY)
                   .values("membership")
                   .annotate(b=Sum(F("credit") - F("debit")))
                   .values("b"))
        documents = (MemberDocument.all_objects
                     .filter(membership=OuterRef("pk"))
                     .values("membership")
                     .annotate(n=Count("id"))
                     .values("n"))
        id_documents = MemberDocument.all_objects.filter(
            membership=OuterRef("pk"), doc_type=MemberDocument.DocType.ID)
        passport_photos = MemberDocument.all_objects.filter(
            membership=OuterRef("pk"),
            doc_type=MemberDocument.DocType.PASSPORT)
        return qs.annotate(
            listed_balance=Coalesce(Subquery(balance, output_field=money),
                                    Value(0), output_field=money),
            listed_document_count=Coalesce(
                Subquery(documents, output_field=models.IntegerField()),
                Value(0)),
            # So the verified badge costs no query per row either.
            listed_has_id_document=Exists(id_documents),
            listed_has_passport_photo=Exists(passport_photos),
        )


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
