"""
Framework-neutral service layer for inviting a new ordinary member into
an EXISTING family -- either by email (a single-use verified-email link,
mirroring shopping_list.registration's Family-admin flow) or without
email (an admin picks an initial password directly; the invitee logs in
via their family's own login URL using their family-local display name
-- see family_login_views.py -- and must change that password on first
login, see PendingPasswordChange). Contains no HTTP/template concerns
(no request, response, or session access), matching the registration
service layer's separation.
"""
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (
    Family,
    FamilyMembership,
    PendingFamilyMemberInvitation,
    PendingPasswordChange,
)
from .registration import (
    INTERNAL_USERNAME_PREFIX,
    AmbiguousEmailOwnershipError,
    PasswordPolicyError,
    find_existing_user_for_email,
    generate_internal_username,
    generate_registration_token,
    hash_token,
    normalize_email,
)

# An invitee may not check their email right away, so this is deliberately
# much longer than REGISTRATION_TOKEN_LIFETIME's 30 minutes.
INVITATION_TOKEN_LIFETIME = timedelta(days=7)


# Re-exported so callers only need to import from this module.
__all__ = [
    "InvitationError",
    "InvalidInvitationTokenError",
    "InvitationConflictError",
    "InvitationAuthorizationError",
    "PasswordPolicyError",
    "AmbiguousEmailOwnershipError",
    "InvitationRequestResult",
    "invite_member_by_email",
    "invite_member_without_email",
    "get_valid_pending_invitation",
    "complete_invitation_for_new_user",
    "complete_invitation_for_existing_user",
    "complete_forced_password_change",
]


class InvitationError(Exception):
    """Base class for invitation-service errors."""


class InvalidInvitationTokenError(InvitationError):
    """Token is missing, unknown, expired, or already consumed."""


class InvitationConflictError(InvitationError):
    """
    Username/email/display name is not available, or the invitee is
    already a member of the target family.
    """


class InvitationAuthorizationError(InvitationError):
    """The authenticated user isn't authorized to consume this token."""


def _validate_password_for_candidate(password: str, *, username: str) -> None:
    try:
        validate_password(password, user=User(username=username))
    except DjangoValidationError as exc:
        raise PasswordPolicyError(exc.messages) from exc


def _default_display_name_for_existing_user(user: User) -> Optional[str]:
    """
    A sensible default display name for an ALREADY-EXISTING account
    joining another family without explicitly supplying one: reuse
    whatever display name they already go by in another family, if any
    -- keeps them recognisable as the same person across families.
    Falls back to their own Django username only if it is a real,
    user-chosen one; an opaque internal username (see
    INTERNAL_USERNAME_PREFIX) must never become a public-facing display
    name, so this returns None for those, forcing the caller to require
    an explicit display name instead.
    """
    existing_membership = (
        FamilyMembership.objects.filter(user=user).first()
    )
    if existing_membership is not None:
        return existing_membership.display_name
    if user.username.startswith(INTERNAL_USERNAME_PREFIX):
        return None
    return user.username


def _display_name_taken(family: Family, display_name: str, *, exclude_pk=None) -> bool: # noqa
    qs = FamilyMembership.objects.filter(
        family=family, display_name__iexact=display_name,
    )
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs.exists()


@dataclass
class InvitationRequestResult:
    pending_invitation: PendingFamilyMemberInvitation
    raw_token: str


def invite_member_by_email(
    *, family: Family, email: str, invited_by: Optional[User] = None,
) -> Optional[InvitationRequestResult]:
    """
    Create a pending invitation for `email` to join `family`. Returns
    None if a concurrent request already created an active invitation
    for this (family, email) pair (a rare race, not a routine outcome):
    unlike public self-registration, this is an already-authenticated
    admin inviting into their own family, so callers should tell them
    plainly that nothing was sent rather than reusing the public flow's
    never-disclose-ambiguity "invitation sent" wording.
    """
    normalized_email = normalize_email(email)
    now = timezone.now()

    existing_member = find_existing_user_for_email(normalized_email)
    if existing_member is not None and FamilyMembership.objects.filter(
        family=family, user=existing_member,
    ).exists():
        raise InvitationConflictError(
            "That person is already a member of this family.",
        )

    with transaction.atomic():
        # A fresh invitation supersedes any other still-active one for
        # the SAME (family, email) pair; a person may still have
        # separate active invitations to DIFFERENT families at once.
        PendingFamilyMemberInvitation.objects.filter(
            family=family, email=normalized_email, consumed_at__isnull=True,
        ).update(consumed_at=now)

        raw_token, digest = generate_registration_token()
        try:
            with transaction.atomic():  # savepoint: isolate a rare race
                pending = PendingFamilyMemberInvitation.objects.create(
                    family=family, email=normalized_email,
                    invited_by=invited_by, token_digest=digest,
                    expires_at=now + INVITATION_TOKEN_LIFETIME,
                )
        except IntegrityError:
            return None

    return InvitationRequestResult(
        pending_invitation=pending, raw_token=raw_token,
    )


def invite_member_without_email(
    *, family: Family, display_name: str, initial_password: str,
) -> FamilyMembership:
    """
    Synchronous equivalent of the email invitation: the admin picks the
    new member's family-local display name and an initial password
    directly (no email involved). Django's required globally-unique
    username is generated internally -- never chosen or seen by the
    admin or the invitee -- since display_name is what identifies this
    person both within the family and when they log in (see
    family_login_views.FamilyLoginView). The invitee must change the
    initial password on first login -- see PendingPasswordChange and
    complete_forced_password_change.
    """
    chosen_display_name = (display_name or "").strip()
    if not chosen_display_name:
        raise InvitationConflictError("A display name is required.")
    if _display_name_taken(family, chosen_display_name):
        raise InvitationConflictError(
            "That display name is already used by someone else in this "
            "family. Please choose a different display name.",
        )

    username = generate_internal_username()
    _validate_password_for_candidate(initial_password, username=username)

    with transaction.atomic():
        user = User.objects.create_user(
            username=username, password=initial_password,
        )
        membership = FamilyMembership.objects.create(
            user=user, family=family, role=FamilyMembership.Role.MEMBER,
            display_name=chosen_display_name,
        )
        PendingPasswordChange.objects.create(user=user)

    return membership


def get_valid_pending_invitation(
    raw_token: str,
) -> PendingFamilyMemberInvitation:
    """
    Validate a token WITHOUT consuming it, so opening the emailed link
    (a GET request) is safe even if fetched ahead of time by a scanner.
    """
    if not raw_token:
        raise InvalidInvitationTokenError("Missing token.")

    try:
        pending = PendingFamilyMemberInvitation.objects.select_related(
            "family",
        ).get(token_digest=hash_token(raw_token))
    except PendingFamilyMemberInvitation.DoesNotExist:
        raise InvalidInvitationTokenError("Unknown token.")

    if pending.consumed_at is not None:
        raise InvalidInvitationTokenError("Invitation already used.")
    if pending.expires_at <= timezone.now():
        raise InvalidInvitationTokenError("Invitation expired.")

    return pending


def complete_invitation_for_new_user(
    *, raw_token: str, password: str, display_name: str,
) -> FamilyMembership:
    """
    Atomically create a brand-new User, an ordinary-member
    FamilyMembership in the invitation's family, and a verified/primary
    allauth EmailAddress, then delete the now-fulfilled invitation (see
    shopping_list.registration's completion functions for why rows are
    deleted rather than retained-but-flagged). Only a CSRF-protected
    POST should call this (never a GET). The User's Django username is
    generated internally (see registration.generate_internal_username)
    -- the invitee is never asked for one. display_name is required:
    unlike the Family-admin registration paths, no name is ever
    collected earlier for an email invitation (the inviting admin only
    supplies an email address), so there is no other human-meaningful
    value left to fall back to once the username itself is opaque.
    """
    digest = hash_token(raw_token) if raw_token else None
    if digest is None:
        raise InvalidInvitationTokenError("Missing token.")

    chosen_display_name = (display_name or "").strip()
    if not chosen_display_name:
        raise InvitationConflictError("A display name is required.")

    username = generate_internal_username()
    _validate_password_for_candidate(password, username=username)

    with transaction.atomic():
        try:
            pending = (
                PendingFamilyMemberInvitation.objects
                .select_for_update()
                .select_related("family")
                .get(token_digest=digest)
            )
        except PendingFamilyMemberInvitation.DoesNotExist:
            raise InvalidInvitationTokenError("Unknown token.")

        if pending.consumed_at is not None:
            raise InvalidInvitationTokenError("Invitation already used.")
        if pending.expires_at <= timezone.now():
            raise InvalidInvitationTokenError("Invitation expired.")

        if find_existing_user_for_email(pending.email) is not None:
            raise InvitationConflictError(
                "That email address is no longer available.",
            )

        if _display_name_taken(pending.family, chosen_display_name):
            raise InvitationConflictError(
                "That display name is already used by someone else in "
                "this family. Please choose a different display name.",
            )

        user = User.objects.create_user(
            username=username, email=pending.email, password=password,
        )
        membership = FamilyMembership.objects.create(
            user=user, family=pending.family,
            role=FamilyMembership.Role.MEMBER,
            display_name=chosen_display_name,
        )
        EmailAddress.objects.create(
            user=user, email=pending.email, verified=True, primary=True,
        )

        pending.delete()

    return membership


def complete_invitation_for_existing_user(
    *, raw_token: str, user: User, display_name: Optional[str] = None,
) -> FamilyMembership:
    """
    Atomically add an ordinary-member FamilyMembership for an ALREADY-
    AUTHENTICATED existing `user` to the invitation's family, without
    creating another User. The caller must have already confirmed `user`
    is logged in and that they, specifically, own the invited email
    (mirrors complete_family_admin_registration_for_existing_user).
    """
    digest = hash_token(raw_token) if raw_token else None
    if digest is None:
        raise InvalidInvitationTokenError("Missing token.")

    with transaction.atomic():
        try:
            pending = (
                PendingFamilyMemberInvitation.objects
                .select_for_update()
                .select_related("family")
                .get(token_digest=digest)
            )
        except PendingFamilyMemberInvitation.DoesNotExist:
            raise InvalidInvitationTokenError("Unknown token.")

        if pending.consumed_at is not None:
            raise InvalidInvitationTokenError("Invitation already used.")
        if pending.expires_at <= timezone.now():
            raise InvalidInvitationTokenError("Invitation expired.")

        if normalize_email(user.email or "") != pending.email:
            raise InvitationAuthorizationError(
                "This invitation link is not for your account.",
            )

        if FamilyMembership.objects.filter(
            family=pending.family, user=user,
        ).exists():
            raise InvitationConflictError(
                "You are already a member of this family.",
            )

        chosen_display_name = (
            (display_name or "").strip()
            or _default_display_name_for_existing_user(user)
        )
        if not chosen_display_name:
            raise InvitationConflictError(
                "A display name is required to join this family.",
            )
        if _display_name_taken(pending.family, chosen_display_name):
            raise InvitationConflictError(
                "That display name is already used by someone else in "
                "this family. Please choose a different display name.",
            )

        membership = FamilyMembership.objects.create(
            user=user, family=pending.family,
            role=FamilyMembership.Role.MEMBER,
            display_name=chosen_display_name,
        )

        # Ensure a verified EmailAddress exists without duplicating rows
        # or blindly replacing another primary email.
        email_address = EmailAddress.objects.filter(
            user=user, email__iexact=pending.email,
        ).first()
        if email_address is None:
            has_primary = EmailAddress.objects.filter(
                user=user, primary=True,
            ).exists()
            EmailAddress.objects.create(
                user=user, email=pending.email, verified=True,
                primary=not has_primary,
            )
        elif not email_address.verified:
            email_address.verified = True
            email_address.save(update_fields=["verified"])

        pending.delete()

    return membership


def complete_forced_password_change(
    *, user: User, new_password: str, display_name: Optional[str] = None,
) -> None:
    """
    Changes `user`'s password and clears their PendingPasswordChange
    flag. If `display_name` is given, updates their (sole) family
    membership's display name too -- mirrors the "may also change their
    display name" first-login allowance. Raises PasswordPolicyError if
    the new password fails validation, or InvitationConflictError if the
    requested display name collides with another member's.
    """
    try:
        validate_password(new_password, user=user)
    except DjangoValidationError as exc:
        raise PasswordPolicyError(exc.messages) from exc

    display_name = (display_name or "").strip()

    with transaction.atomic():
        user.set_password(new_password)
        user.save(update_fields=["password"])

        if display_name:
            membership = (
                FamilyMembership.objects
                .select_for_update()
                .filter(user=user)
                .first()
            )
            if (
                membership is not None
                and membership.display_name != display_name
            ):
                if _display_name_taken(
                    membership.family, display_name,
                    exclude_pk=membership.pk,
                ):
                    raise InvitationConflictError(
                        "That display name is already used by someone "
                        "else in this family.",
                    )
                membership.display_name = display_name
                membership.save(update_fields=["display_name"])

        PendingPasswordChange.objects.filter(user=user).delete()
