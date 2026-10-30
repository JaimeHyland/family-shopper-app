"""
Removes the Django auth Group named "adult", a leftover of the retired
adult/child distinction (see migration 0048, which removed the
AutomaticAdultGroupMembership model that used to maintain this group).

Verified before writing this migration: no code anywhere queries this
group by name, no permission is assigned to it (nor to any other group
in this project - group-based Django permissions are not used anywhere),
and every staff user currently in it is also a superuser (whose access
never depends on group permissions). It is a pure, inert leftover.

Forward-only, deliberately: this group carries no functional weight, so
reconstructing it by hand (recreate a Group named "adult", re-add
whichever users need it) is trivial if ever genuinely wanted. An earlier
version of this migration captured the group's id/permissions/members to
a JSON file next to this module so reverse() could recreate it, but that
snapshot was not portable across environments (its ids are specific to
whichever database wrote it) and was not worth the complexity for data
this low-value - see migration 0034 for the same forward-only precedent
in this app. Take a normal database backup before applying this in any
environment where recovery might matter.
"""
from django.db import migrations


GROUP_NAME = "adult"


def remove_adult_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    group = Group.objects.filter(name=GROUP_NAME).first()

    if group is None:
        # Already absent (e.g. a fresh database): nothing to delete.
        return

    # Plain ORM deletion: cascades only through this group's own
    # auth_group_permissions/auth_user_groups rows (Django-managed M2M
    # "through" tables), leaving every other group, permission, user and
    # family-membership record completely untouched.
    group.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("shopping_list", "0048_remove_automatic_adult_group_membership"),
    ]

    operations = [
        migrations.RunPython(remove_adult_group, migrations.RunPython.noop),
    ]
