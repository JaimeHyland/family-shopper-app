from django.db import migrations

from utilities.utils import generate_unique_slug


def populate_family_slugs(apps, schema_editor):
    db_alias = schema_editor.connection.alias
    Family = apps.get_model("shopping_list", "Family")

    # Ordered by id so backfilling is deterministic; whichever row is
    # processed first for a given name keeps the bare slug, and any later
    # same-named row gets a random suffix.
    for family in Family.objects.using(db_alias).filter(
        slug__isnull=True,
    ).order_by("id"):
        family.slug = generate_unique_slug(
            family.name,
            lambda candidate: Family.objects.using(db_alias).filter(
                slug=candidate,
            ).exists(),
        )
        family.save(update_fields=["slug"])


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("shopping_list", "0038_family_slug"),
    ]

    operations = [
        migrations.RunPython(
            populate_family_slugs,
            reverse_noop,
        ),
    ]
