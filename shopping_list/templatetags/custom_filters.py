from django import template
from shopping_list.models import FamilyMembership


register = template.Library()


@register.filter
def member_display_name(user, family):
    """
    The name this user goes by in this family, with a safe fallback for
    historical users whose membership may no longer exist.
    """
    if user is None or family is None:
        return None
    membership = FamilyMembership.objects.filter(
        user=user, family=family,
    ).first()
    if membership is not None:
        return membership.display_name
    return getattr(user, "username", None)


@register.filter
def logged_in_identity(user, request=None):
    """
    What to show for "You are logged in as ..." (and anywhere else the
    CURRENTLY logged-in user's own identity is surfaced): always
    "<display_name> (<family name>)", resolved fresh from this user's
    FamilyMembership and the corresponding Family record -- never a
    separately stored/cached combined string. This applies uniformly to
    EVERY account, including "Option A" (email-based) accounts with a
    real, self-chosen Django username: that technical username remains
    the authentication identifier, but is never the human-facing
    identity shown to the person themselves, exactly as for any other
    member.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return None

    family = getattr(request, "family", None) if request else None
    membership = None
    if family is not None:
        membership = FamilyMembership.objects.filter(
            user=user, family=family,
        ).select_related("family").first()
    if membership is None:
        membership = FamilyMembership.objects.filter(
            user=user,
        ).select_related("family").first()

    if membership is not None:
        return f"{membership.display_name} ({membership.family.name})"
    # No membership at all (e.g. a Django-only superuser, or an
    # orphaned account) -- never fall back to the raw username.
    return "your account"
