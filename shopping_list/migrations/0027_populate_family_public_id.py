import uuid

from django.db import migrations


def assign_distinct_public_ids(apps, schema_editor):
    db_alias = schema_editor.connection.alias
    Family = apps.get_model("shopping_list", "Family")

    # Assign a freshly generated UUID per row; a single evaluated default
    # would give every existing row the same value.
    for family in Family.objects.using(db_alias).filter(public_id__isnull=True):
        family.public_id = uuid.uuid4()
        family.save(update_fields=["public_id"])


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        (
            "shopping_list",
            "0026_family_public_id_and_pending_registration",
        ),
    ]

    operations = [
        migrations.RunPython(
            assign_distinct_public_ids,
            reverse_noop,
        ),
    ]
