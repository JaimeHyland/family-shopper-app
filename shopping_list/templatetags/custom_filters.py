from django import template
from utilities.utils import is_adult
from shopping_list.models import FamilyMembership


register = template.Library()


@register.filter
def is_adult_filter(user):
    return is_adult(user)


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
