import secrets

from django.utils.text import slugify


def is_family_admin(request):
    """True only if `request` carries a specific family membership (set
    by FamilyMembershipRequiredMixin) and that membership is an admin of
    THAT family. Deliberately never falls back to any global/cross-family
    privilege check: a user who administers a different family, or a
    request reached without the expected family-scoped context, is
    treated as "not an admin" here rather than as an opportunity to fall
    back to a broader check."""
    membership = getattr(request, "family_membership", None)
    return membership is not None and membership.is_family_admin


def has_family_permission(request, flag_name):
    """True if `request` carries a family membership and that membership
    is authorised for the operation gated by `flag_name` (one of
    FamilyMembership's six `can_*` boolean fields). Admins always pass,
    regardless of the flags: an admin's authority never depends on these
    ordinary-member permission checkboxes. Fails safe (False) if there is
    no family membership on the request, exactly like is_family_admin."""
    membership = getattr(request, "family_membership", None)
    if membership is None:
        return False
    return membership.is_family_admin or bool(
        getattr(membership, flag_name, False),
    )


_SLUG_SUFFIX_ALPHABET = 'abcdefghijklmnopqrstuvwxyz0123456789'


def generate_unique_slug(name, exists_fn, suffix_length=4, max_attempts=100):
    """
    Slugify `name`; if that slug is already taken (per `exists_fn`), retry
    with a short random suffix appended until a free one is found. The
    suffix is random rather than sequential, so it never reveals how many
    other records share the same base slug.
    """
    base_slug = slugify(name)
    if not exists_fn(base_slug):
        return base_slug

    for _ in range(max_attempts):
        suffix = ''.join(
            secrets.choice(_SLUG_SUFFIX_ALPHABET) for _ in range(suffix_length) # noqa
        )
        candidate = f"{base_slug}-{suffix}"
        if not exists_fn(candidate):
            return candidate

    raise RuntimeError(
        f"Could not generate a unique slug for {name!r} after "
        f"{max_attempts} attempts."
    )
