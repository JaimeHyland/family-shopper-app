from django.contrib.auth.models import Group

from .models import (
    AutomaticAdultGroupMembership,
    FamilyMembership,
)


ADULT_GROUP_NAME = "adult"


def user_qualifies_for_adult_group(user):
    return user.is_superuser or FamilyMembership.objects.filter(
        user=user,
        role=FamilyMembership.Role.ADMIN,
    ).exists()


def sync_adult_group_membership(user):
    if not user.pk:
        return

    automatic_grant = AutomaticAdultGroupMembership.objects.filter(
        user=user,
    ).first()

    if user_qualifies_for_adult_group(user):
        adult_group, _ = Group.objects.get_or_create(name=ADULT_GROUP_NAME)
        has_adult_group = user.groups.filter(pk=adult_group.pk).exists()
        if not has_adult_group:
            if automatic_grant is None:
                automatic_grant = AutomaticAdultGroupMembership.objects.create(
                    user=user,
                )
            user.groups.add(adult_group)
        return

    if automatic_grant is not None:
        adult_group = Group.objects.filter(name=ADULT_GROUP_NAME).first()
        if adult_group is not None:
            user.groups.remove(adult_group)
        automatic_grant.delete()