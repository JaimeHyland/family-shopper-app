"""
Framework-neutral service layer for public Family-admin self-registration.

These functions contain no HTTP/template concerns (no request, response, or
session access) so they can be called unchanged from Django template views
today and from a future JSON/Flutter-facing API without modification.
"""
import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError as DjangoValidationError
from django.contrib.auth.password_validation import validate_password
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .models import Family, FamilyMembership, PendingFamilyAdminRegistration

# Matches the duration used by the equivalent flow in Projectodo.
REGISTRATION_TOKEN_LIFETIME = timedelta(minutes=30)
# A modest cooldown to discourage rapid repeated resend requests.
REGISTRATION_RESEND_COOLDOWN = timedelta(seconds=60)


class RegistrationError(Exception):
    """Base class for registration-service errors."""


class InvalidRegistrationTokenError(RegistrationError):
    """Token is missing, unknown, expired, or already consumed."""


class RegistrationConflictError(RegistrationError):
    """Username or email is no longer available at completion time."""


class RegistrationAuthorizationError(RegistrationError):
    """The authenticated user isn't authorized to consume this token."""


class AmbiguousEmailOwnershipError(RegistrationError):
    """Legacy data has more than one candidate account for this email."""


class PasswordPolicyError(RegistrationError):
    """Structured password-validation failure, e.g. for a future API."""

    def __init__(self, messages):
        self.messages = list(messages)
        super().__init__("; ".join(self.messages))


def normalize_email(email: str) -> str:
    return User.objects.normalize_email(email).strip().lower()


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def generate_registration_token() -> tuple[str, str]:
    """Return (raw_token, token_digest). Only the digest is ever stored."""
    raw_token = secrets.token_urlsafe(48)
    return raw_token, hash_token(raw_token)


@dataclass
class RegistrationRequestResult:
    pending_registration: PendingFamilyAdminRegistration
    raw_token: str


def request_family_admin_registration(
    *, family_name: str, email: str, display_name: str = "",
) -> Optional[RegistrationRequestResult]:
    """
    Create a pending Family-admin registration for `email`.

    Every legitimate request creates a token and expects an email to be
    sent, whether or not the address belongs to an existing account --
    that branching happens at completion time instead, so requesting
    never discloses account existence. Returns None if a resend cooldown
    is active or (rarely) a concurrent request won the race; callers must
    still present the same generic outcome either way.

    `display_name` is the applicant's own name, as entered at request
    time; it is carried through to completion (see
    `complete_family_admin_registration_for_new_user`/
    `..._for_existing_user`) so it isn't lost even though completion may
    happen in a different session after the emailed link is opened.
    """
    normalized_email = normalize_email(email)
    family_name = family_name.strip()
    display_name = (display_name or "").strip()
    now = timezone.now()

    with transaction.atomic():
        active_recent = PendingFamilyAdminRegistration.objects.filter(
            email=normalized_email,
            consumed_at__isnull=True,
            created_at__gt=now - REGISTRATION_RESEND_COOLDOWN,
        ).exists()
        if active_recent:
            return None

        # A fresh request supersedes any other still-active token for the
        # same email.
        PendingFamilyAdminRegistration.objects.filter(
            email=normalized_email, consumed_at__isnull=True,
        ).update(consumed_at=now)

        raw_token, digest = generate_registration_token()
        try:
            with transaction.atomic():  # savepoint: isolate a rare race
                pending = PendingFamilyAdminRegistration.objects.create(
                    email=normalized_email,
                    family_name=family_name,
                    display_name=display_name,
                    token_digest=digest,
                    expires_at=now + REGISTRATION_TOKEN_LIFETIME,
                )
        except IntegrityError:
            # A concurrent request already created an active token for
            # this email in between; don't disclose, don't duplicate.
            return None

    return RegistrationRequestResult(
        pending_registration=pending, raw_token=raw_token,
    )


def invalidate_pending_registration(
    pending: PendingFamilyAdminRegistration,
) -> None:
    """Free the email up for an immediate retry after a failed send."""
    pending.consumed_at = timezone.now()
    pending.save(update_fields=["consumed_at"])


def get_valid_pending_registration(
    raw_token: str,
) -> PendingFamilyAdminRegistration:
    """
    Validate a token WITHOUT consuming it, so opening the emailed link
    (a GET request) is safe even if fetched ahead of time by a scanner.
    """
    if not raw_token:
        raise InvalidRegistrationTokenError("Missing token.")

    try:
        pending = PendingFamilyAdminRegistration.objects.get(
            token_digest=hash_token(raw_token),
        )
    except PendingFamilyAdminRegistration.DoesNotExist:
        raise InvalidRegistrationTokenError("Unknown token.")

    if pending.consumed_at is not None:
        raise InvalidRegistrationTokenError("Token already used.")
    if pending.expires_at <= timezone.now():
        raise InvalidRegistrationTokenError("Token expired.")

    return pending


def find_existing_user_for_email(email: str) -> Optional[User]:
    """
    Return the single unambiguous account owning `email`, or None if no
    account owns it. Raises AmbiguousEmailOwnershipError rather than
    guessing if legacy data has more than one candidate.
    """
    normalized_email = normalize_email(email)
    candidate_ids = set(
        User.objects.filter(
            Q(email__iexact=normalized_email)
            | Q(emailaddress__email__iexact=normalized_email)
        ).values_list("id", flat=True)
    )
    if len(candidate_ids) > 1:
        raise AmbiguousEmailOwnershipError(
            "More than one existing account is associated with this email.", # noqa
        )
    if not candidate_ids:
        return None
    return User.objects.get(id=next(iter(candidate_ids)))


def _validate_password_for_candidate(password: str, *, username: str) -> None: # noqa
    try:
        validate_password(password, user=User(username=username))
    except DjangoValidationError as exc:
        raise PasswordPolicyError(exc.messages) from exc


@dataclass
class RegistrationCompletionResult:
    user: User
    family: Family
    membership: FamilyMembership


def complete_family_admin_registration_for_new_user(
    *, raw_token: str, username: str, password: str,
    display_name: Optional[str] = None,
) -> RegistrationCompletionResult:
    """
    Atomically create a brand-new User, Family, admin FamilyMembership and
    a verified/primary allauth EmailAddress, then delete the now-fulfilled
    pending registration -- the User/Family created here are the durable
    record of a successful registration, so there is nothing left for
    the pending row to track. (Superseded/invalidated pending
    registrations are still retained elsewhere, unaffected by this.)
    Only a CSRF-protected POST should call this (never a GET).
    """
    digest = hash_token(raw_token) if raw_token else None
    if digest is None:
        raise InvalidRegistrationTokenError("Missing token.")

    _validate_password_for_candidate(password, username=username)

    with transaction.atomic():
        try:
            pending = (
                PendingFamilyAdminRegistration.objects
                .select_for_update()
                .get(token_digest=digest)
            )
        except PendingFamilyAdminRegistration.DoesNotExist:
            raise InvalidRegistrationTokenError("Unknown token.")

        if pending.consumed_at is not None:
            raise InvalidRegistrationTokenError("Token already used.")
        if pending.expires_at <= timezone.now():
            raise InvalidRegistrationTokenError("Token expired.")

        if User.objects.filter(username__iexact=username).exists():
            raise RegistrationConflictError("That username is not available.") # noqa
        if find_existing_user_for_email(pending.email) is not None:
            raise RegistrationConflictError(
                "That email address is no longer available.",
            )

        user = User.objects.create_user(
            username=username, email=pending.email, password=password,
        )
        # Solo registrations never collect a family name (see
        # SoloRegistrationRequestView.get_family_name): the family is
        # simply named after the username just chosen here.
        family = Family.objects.create(name=pending.family_name or username) # noqa
        chosen_display_name = (
            (display_name or "").strip()
            or pending.display_name
            or username
        )
        membership = FamilyMembership.objects.create(
            user=user, family=family, role=FamilyMembership.Role.ADMIN,
            display_name=chosen_display_name,
        )
        EmailAddress.objects.create(
            user=user, email=pending.email, verified=True, primary=True,
        )

        pending.delete()

    return RegistrationCompletionResult(
        user=user, family=family, membership=membership,
    )


def complete_family_admin_registration_for_existing_user(
    *, raw_token: str, user: User, display_name: Optional[str] = None,
) -> RegistrationCompletionResult:
    """
    Atomically create a new Family and an admin FamilyMembership for an
    ALREADY-AUTHENTICATED existing `user`, without creating another User.
    The caller must have already confirmed `user` is logged in and that
    they, specifically, own the pending email (login/reauthentication
    proves which identity is requesting the new family; the token alone
    only proves control of the mailbox). The pending registration is
    deleted once fulfilled -- see complete_family_admin_registration_for_
    new_user for why.
    """
    digest = hash_token(raw_token) if raw_token else None
    if digest is None:
        raise InvalidRegistrationTokenError("Missing token.")

    with transaction.atomic():
        try:
            pending = (
                PendingFamilyAdminRegistration.objects
                .select_for_update()
                .get(token_digest=digest)
            )
        except PendingFamilyAdminRegistration.DoesNotExist:
            raise InvalidRegistrationTokenError("Unknown token.")

        if pending.consumed_at is not None:
            raise InvalidRegistrationTokenError("Token already used.")
        if pending.expires_at <= timezone.now():
            raise InvalidRegistrationTokenError("Token expired.")

        if normalize_email(user.email or "") != pending.email:
            raise RegistrationAuthorizationError(
                "This registration link is not for your account.",
            )

        family = Family.objects.create(
            # Mirrors the new-user path: a solo request never collected a
            # family name, so it defaults to the already-existing user's
            # own username instead.
            name=pending.family_name or user.username,
        )
        chosen_display_name = (
            (display_name or "").strip()
            or pending.display_name
            or user.username
        )
        membership = FamilyMembership.objects.create(
            user=user, family=family, role=FamilyMembership.Role.ADMIN,
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

    return RegistrationCompletionResult(
        user=user, family=family, membership=membership,
    )
