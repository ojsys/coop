"""
Signing up: a society joining the platform, and a person applying to join a
society.

Both are initiated by strangers, so both are written defensively. A society
goes live at once, but the platform moves no money for it until its KYC is
approved (tenants.verification). A person asking to join an existing society
gets no account until an officer approves them.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from accounts.join_models import JoinRequest
from accounts.models import Membership, Role, User
from core.reference_data import country_name

logger = logging.getLogger("api.errors")


class SignupError(Exception):
    """A signup could not be accepted (duplicate, already a member, ...)."""


def _unique_slug(name: str) -> str:
    """A URL-safe slug that doesn't collide with an existing society."""
    from tenants.models import Cooperative

    base = slugify(name)[:70] or "cooperative"
    slug, n = base, 1
    while Cooperative.objects.filter(slug=slug).exists():
        n += 1
        slug = f"{base[:70 - len(str(n)) - 1]}-{n}"
    return slug


# Recipients for a join notice are deliberately the same set as any other mail
# addressed to a cooperative: communications.cooperative_notification_emails —
# its published contact address plus every privileged officer.
#
# Privileged-only on purpose. JoinRequestViewSet is gated by IsPrivilegedOfficer,
# so a read-only officer cannot view or decide a request; mailing them "review
# it in the console" would send them to a permission wall. The contact address is what
# stops a request going unseen when a cooperative has no privileged officer,
# which was the actual hole — not the narrowness of the role check.


class LastOfficerError(Exception):
    """Refused: the change would leave a cooperative with no officer."""


_UNSET = object()


def check_officer_remains(membership, *, new_role=_UNSET, new_status=_UNSET):
    """Refuse a change that would strip a cooperative of its last officer.

    Admin is meant to be revocable — you asked for that — but a cooperative
    with nobody holding members.manage can no longer admit members, assign
    roles or be handed on. It is the exact dead end that leaves a cooperative
    unreachable, and it is reached by one careless self-demotion.

    Called by both the API serializer and the Django admin, because the admin
    edits Membership.role directly and would otherwise walk straight past a
    serializer-only check.
    """
    from accounts.models import Membership

    # A sentinel, not None: clearing the role outright is precisely the
    # destructive edit this guards, and None-means-unchanged would wave it
    # through.
    role = membership.role if new_role is _UNSET else new_role
    status = membership.status if new_status is _UNSET else new_status

    still_privileged = (
        status == Membership.Status.ACTIVE
        and role is not None
        and role.is_privileged
    )
    if still_privileged:
        return

    was_privileged = (
        membership.status == Membership.Status.ACTIVE
        and membership.role is not None
        and membership.role.is_privileged
    )
    if not was_privileged:
        return  # they were not an officer, so none is being removed

    others = (
        Membership.all_objects
        .filter(cooperative=membership.cooperative,
                status=Membership.Status.ACTIVE)
        .exclude(pk=membership.pk)
        .select_related("role")
    )
    if any(m.role and m.role.is_privileged for m in others):
        return

    raise LastOfficerError(
        f"{membership.user.full_name} is the only officer of "
        f"{membership.cooperative.name}. Make someone else an exco first — "
        f"Chairperson, Secretary or Treasurer — or the cooperative would be "
        f"left with nobody who can manage it."
    )


def check_role_keeps_officers(role, new_permissions):
    """Refuse a permission edit that would leave a cooperative with no officer.

    ``Role.is_privileged`` is derived from ``permissions``, so unticking
    "Manage members" on the Secretary role demotes every member holding it at
    once. ``check_officer_remains`` only ever inspects a Membership, so it
    never sees this edit — it is the same dead end reached by another door,
    and the role editor is the door an officer is most likely to open.
    """
    from accounts.models import Membership, Role

    if not role.is_privileged:
        return  # it granted nothing, so it can take nothing away
    if Role.PRIVILEGED_PERMISSIONS.intersection(new_permissions or ()):
        return  # still privileged after the edit

    holders = Membership.all_objects.filter(
        cooperative=role.cooperative, role=role,
        status=Membership.Status.ACTIVE,
    )
    count = holders.count()
    if count == 0:
        return  # nobody holds it, so nobody is demoted

    others = (
        Membership.all_objects
        .filter(cooperative=role.cooperative,
                status=Membership.Status.ACTIVE)
        .exclude(role=role)
        .select_related("role")
    )
    if any(m.role and m.role.is_privileged for m in others):
        return

    who = "1 member holds it" if count == 1 else f"{count} members hold it"
    raise LastOfficerError(
        f"{role.name} is the only privileged role in use at "
        f"{role.cooperative.name} and {who}. Removing those permissions would "
        f"leave the cooperative with nobody who can manage it — give someone "
        f"else a privileged role first."
    )


def _next_member_no(cooperative) -> str:
    """Allocate a free member number for a cooperative.

    House style is a short prefix and a sequence (IMC-0001), the prefix taken
    from the cooperative's initials so the number reads sensibly on a
    statement. ``uniq_member_no_per_coop`` is a database constraint, so this
    checks what is taken rather than assuming a count is safe — members can be
    imported or deleted, and a count would collide.
    """
    import re
    import unicodedata

    ascii_name = (unicodedata.normalize("NFKD", cooperative.name)
                  .encode("ascii", "ignore").decode())
    initials = "".join(w[0] for w in re.findall(r"[A-Za-z]+", ascii_name))
    prefix = (initials[:4] or "MEM").upper()

    taken = set(
        Membership.all_objects.filter(cooperative=cooperative)
        .values_list("member_no", flat=True)
    )
    n = 1
    while f"{prefix}-{n:04d}" in taken:
        n += 1
    return f"{prefix}-{n:04d}"


def create_founding_admin(cooperative, *, full_name, email, phone="",
                          role_slug=Role.SECRETARY, password=None):
    """Give the person who brought a cooperative on board an account and make
    them one of its officers.

    Every cooperative created through the public form used to have no members
    at all: nobody to email, nobody who could sign in, and nobody able to admit
    anyone else. This closes that.

    Self-serve signup makes the founder **Chairperson**, which carries every
    permission, so one person can set the whole society up alone before the
    rest of the exco is invited. The backfill for older cooperatives keeps the
    Secretary it always gave. Either way it is an ordinary membership, so it
    can be reassigned or revoked by any officer later.

    ``password`` is set only on an account this call creates — signup has
    already checked it against an existing account, and nothing here may
    overwrite a password somebody else chose. Passing one also means the
    caller sends its own welcome. Without one, no usable password is set and
    the welcome email invites them to choose one, exactly as
    approve_join_request does.

    Idempotent — re-running for the same person returns their membership.
    """
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        user = User.objects.create_user(
            email=email, full_name=full_name, phone=phone, password=password)
        if password is None:
            user.set_unusable_password()
            user.save(update_fields=["password"])

    existing = Membership.all_objects.filter(
        cooperative=cooperative, user=user).first()
    if existing is not None:
        return existing

    role = Role.all_objects.filter(
        cooperative=cooperative, slug=role_slug).first()
    membership = Membership.all_objects.create(
        cooperative=cooperative, user=user, role=role,
        member_no=_next_member_no(cooperative),
        status=Membership.Status.ACTIVE, joined_at=timezone.localdate(),
    )

    if password is None:
        def _welcome():
            from communications.email import send_welcome_email
            send_welcome_email(membership)

        transaction.on_commit(_welcome)
    return membership


# ── A society signing up to the platform ────────────────────────────────────
@transaction.atomic
def sign_up_cooperative(*, society_name, applicant_name, applicant_email,
                        password, applicant_phone="", country="", state="",
                        lga="", coop_type="", estimated_members=0,
                        message=""):
    """Create a cooperative that is live immediately, on its free trial.

    There is no approval step in front of the society any more: it is ACTIVE
    from this call, its trial starts today, and the founder is signed straight
    into the console. What a human still checks is *who runs it* — the KYC in
    tenants.verification — and until that is approved the platform will not
    move money for it. Bookkeeping needs no such trust; collecting money from
    the public does.

    If the address already has an account (an officer or member elsewhere),
    the password must be that account's. Signup is a public form: accepting
    any password for a known address would let a stranger attach someone's
    account to a society of their choosing — or, worse, reset it.
    """
    from platform_admin.billing import ensure_subscription
    from tenants.models import Cooperative
    from tenants.services import provision_cooperative

    existing = User.objects.filter(email__iexact=applicant_email).first()
    if existing is not None and not existing.check_password(password):
        raise SignupError(
            "That email already has a CooperativeOS account. Use its password "
            "— or reset it from the sign-in page — and try again.")

    cooperative = provision_cooperative(
        name=society_name,
        slug=_unique_slug(society_name),
        status=Cooperative.Status.ACTIVE,
        country=country,
        state=state,
        lga=lga,
        coop_type=coop_type,
        contact_email=applicant_email,
        contact_phone=applicant_phone,
    )
    # Returns None when no plan matches, which must not refuse a signup: a
    # missing price list is the platform's problem, not the society's.
    ensure_subscription(cooperative)

    founder = create_founding_admin(
        cooperative, full_name=applicant_name, email=applicant_email,
        phone=applicant_phone, role_slug=Role.CHAIRPERSON, password=password)

    from audit.services import record_action
    record_action(cooperative=cooperative, actor=founder.user,
                  action="cooperative.signup", entity=cooperative,
                  after={"status": cooperative.status})

    # Notifications must never roll back an accepted signup.
    transaction.on_commit(lambda: _notify_signup(
        membership=founder, estimated_members=estimated_members,
        message=message,
    ))
    return cooperative


def _notify_signup(*, membership, estimated_members, message):
    from communications.email import (
        platform_support_email, send_alert_email, send_signup_welcome_email,
    )

    cooperative = membership.cooperative
    user = membership.user
    send_signup_welcome_email(membership)

    rows = [
        ("Cooperative", cooperative.name),
        ("Founder", f"{user.full_name} <{user.email}>"),
        ("Phone", cooperative.contact_phone or "—"),
        ("Location", " · ".join(filter(None, (
            cooperative.lga, cooperative.state,
            country_name(cooperative.country)))) or "—"),
        ("Estimated members", str(estimated_members or "not stated")),
    ]
    if message:
        rows.append(("Message", message))
    send_alert_email(
        to=platform_support_email(),
        subject=f"New cooperative signup: {cooperative.name}",
        heading="A cooperative has signed up",
        intro="It is live on its free trial. Online payments stay closed "
              "until its exco submits KYC and we verify it.",
        rows=rows,
    )


# ── A person applying to join a society ─────────────────────────────────────
@transaction.atomic
def request_to_join(*, cooperative, full_name, email, phone="", message=""):
    """Record a membership request for officers of that society to decide."""
    email = email.strip().lower()

    already_a_member = Membership.all_objects.filter(
        cooperative=cooperative, user__email__iexact=email,
    ).exists()
    if already_a_member:
        raise SignupError(
            "That email address already belongs to a member of this "
            "cooperative. Try signing in, or use 'Forgot password'."
        )

    # Portable stand-in for the partial unique index MySQL cannot express.
    duplicate = JoinRequest.all_objects.filter(
        cooperative=cooperative, email__iexact=email,
        status=JoinRequest.Status.PENDING,
    ).exists()
    if duplicate:
        raise SignupError(
            "You already have a request waiting with this cooperative. An "
            "officer will be in touch."
        )

    request = JoinRequest.all_objects.create(
        cooperative=cooperative, full_name=full_name, email=email,
        phone=phone, message=message,
    )

    transaction.on_commit(lambda: _notify_join_request(request))
    return request


def _notify_join_request(request):
    from communications.email import (
        cooperative_notification_emails, send_alert_email,
        send_join_request_received_email,
    )

    # The applicant is told first, and unconditionally. They are the one person
    # guaranteed to be waiting, and sending this after the early return below
    # would leave exactly the people most likely to be stranded — applicants to
    # a cooperative with nobody to notify — hearing nothing at all.
    send_join_request_received_email(request)

    recipients = cooperative_notification_emails(request.cooperative)
    if not recipients:
        logger.warning(
            "Join request %s for %s has nobody to notify: no privileged "
            "officer and no contact address on the cooperative",
            request.pk, request.cooperative,
        )
        return

    send_alert_email(
        to=recipients,
        subject=f"New membership request: {request.full_name}",
        heading="Someone has asked to join your cooperative",
        intro="Review it under Members → Join requests in the console.",
        cooperative=request.cooperative,
        rows=[
            ("Name", request.full_name),
            ("Email", request.email),
            ("Phone", request.phone or "—"),
            ("Message", request.message or "—"),
        ],
    )


@transaction.atomic
def approve_join_request(join_request, *, actor, member_no, role=None):
    """Admit the applicant: create their account and membership.

    The account is created here — not when they applied — so an unapproved
    stranger never holds credentials for a society. They are emailed a
    set-password link rather than a generated password.
    """
    if join_request.status != JoinRequest.Status.PENDING:
        raise SignupError("This request has already been decided.")

    cooperative = join_request.cooperative

    if Membership.all_objects.filter(cooperative=cooperative,
                                     member_no=member_no).exists():
        raise SignupError(f"Member number {member_no} is already in use.")

    user = User.objects.filter(email__iexact=join_request.email).first()
    if user is None:
        # No usable password: the welcome email carries a set-password link.
        user = User.objects.create_user(
            email=join_request.email, full_name=join_request.full_name,
            phone=join_request.phone,
        )
        user.set_unusable_password()
        user.save(update_fields=["password"])

    if role is None:
        role = Role.all_objects.filter(cooperative=cooperative,
                                       slug=Role.MEMBER).first()

    membership = Membership.all_objects.create(
        cooperative=cooperative, user=user, role=role, member_no=member_no,
        status=Membership.Status.ACTIVE, joined_at=timezone.localdate(),
    )

    join_request.status = JoinRequest.Status.APPROVED
    join_request.decided_by = actor
    join_request.decided_at = timezone.now()
    join_request.membership = membership
    join_request.save(update_fields=["status", "decided_by", "decided_at",
                                     "membership", "updated_at"])

    from audit.services import record_action
    record_action(cooperative=cooperative, actor=actor,
                  action="membership.join_request_approved", entity=membership,
                  after={"member_no": member_no, "email": user.email})

    def _welcome():
        from communications.email import send_welcome_email
        send_welcome_email(membership)

    transaction.on_commit(_welcome)
    return membership


@transaction.atomic
def reject_join_request(join_request, *, actor, note=""):
    if join_request.status != JoinRequest.Status.PENDING:
        raise SignupError("This request has already been decided.")

    join_request.status = JoinRequest.Status.REJECTED
    join_request.decided_by = actor
    join_request.decided_at = timezone.now()
    join_request.decision_note = note
    join_request.save(update_fields=["status", "decided_by", "decided_at",
                                     "decision_note", "updated_at"])

    from audit.services import record_action
    record_action(cooperative=join_request.cooperative, actor=actor,
                  action="membership.join_request_rejected",
                  entity=join_request, after={"note": note})
    return join_request
