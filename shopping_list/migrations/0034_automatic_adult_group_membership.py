from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


ADULT_GROUP_NAME = "adult"
ADMIN_ROLE = "admin"


def sync_existing_adult_group_memberships(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split(".", 1))
    Group = apps.get_model("auth", "Group")
    FamilyMembership = apps.get_model("shopping_list", "FamilyMembership")
    AutomaticGrant = apps.get_model(
        "shopping_list", "AutomaticAdultGroupMembership",
    )

    adult_group, _ = Group.objects.get_or_create(name=ADULT_GROUP_NAME)
    qualifying_user_ids = set(
        User.objects.filter(is_superuser=True).values_list("pk", flat=True)
    )
    qualifying_user_ids.update(
        FamilyMembership.objects.filter(role=ADMIN_ROLE).values_list(
            "user_id", flat=True,
        )
    )

    for user_id in qualifying_user_ids:
        user = User.objects.get(pk=user_id)
        if not user.groups.filter(pk=adult_group.pk).exists():
            user.groups.add(adult_group)
            AutomaticGrant.objects.get_or_create(user_id=user_id)


class Migration(migrations.Migration):
    dependencies = [
        ("shopping_list", "0033_pendingfamilyadminregistration_active_email_unique"),
    ]

    operations = [
        migrations.CreateModel(
            name="AutomaticAdultGroupMembership",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="automatic_adult_group_membership",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.RunPython(
            sync_existing_adult_group_memberships,
            migrations.RunPython.noop,
        ),
    ]