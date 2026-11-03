from django import template
from shopping_list.invitations import INTERNAL_USERNAME_PREFIX
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
    CURRENTLY logged-in user's own identity is surfaced). A no-email
    member's Django username is an opaque, internal identifier (see
    shopping_list.invitations.invite_member_without_email /
    INTERNAL_USERNAME_PREFIX) and must never be shown -- this always
    resolves to their family-local display name instead, with the
    family name alongside it whenever one can be determined. Accounts
    with a real, self-chosen username (e.g. admins, or members invited
    by email) are shown as before -- unaffected.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    if not user.username.startswith(INTERNAL_USERNAME_PREFIX):
        return str(user)

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
    # No membership at all is not expected to happen in practice for an
    # internal-username account, but this must still never fall back to
    # the raw username.
    return "your account"
