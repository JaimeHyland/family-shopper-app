from django.db import migrations


def populate_display_names(apps, schema_editor):
    db_alias = schema_editor.connection.alias
    FamilyMembership = apps.get_model("shopping_list", "FamilyMembership")

    for membership in (
        FamilyMembership.objects.using(db_alias)
        .filter(display_name__isnull=True)
        .select_related("user")
    ):
        membership.display_name = membership.user.username.strip()
        membership.save(update_fields=["display_name"])


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("shopping_list", "0029_familymembership_display_name"),
    ]

    operations = [
        migrations.RunPython(
            populate_display_names,
            reverse_noop,
        ),
    ]
