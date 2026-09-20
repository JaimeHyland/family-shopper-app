from django.conf import settings
from django.db import migrations


FAMILY_NAME = "Hyland family"
MEMBER_USERNAMES = ["Lily", "James", "Annett", "Jamesie"]
ADMIN_USERNAME = "JaimeHy"


def assign_existing_data(apps, schema_editor):
    db_alias = schema_editor.connection.alias

    Family = apps.get_model("shopping_list", "Family")
    FamilyMembership = apps.get_model(
        "shopping_list",
        "FamilyMembership",
    )
    Shop = apps.get_model("shopping_list", "Shop")
    Category = apps.get_model("shopping_list", "Category")
    Product = apps.get_model("shopping_list", "Product")
    ListItem = apps.get_model("shopping_list", "ListItem")

    user_app, user_model = settings.AUTH_USER_MODEL.split(".")
    User = apps.get_model(user_app, user_model)

    has_legacy_records = any(
        model.objects.using(db_alias).filter(family__isnull=True).exists()
        for model in (Shop, Category, Product, ListItem)
    )
    if not has_legacy_records:
        # Nothing to migrate on a fresh database: don't create a family.
        return

    family, _ = Family.objects.using(db_alias).get_or_create(
        name=FAMILY_NAME,
    )

    for model in (Shop, Category, Product, ListItem):
        model.objects.using(db_alias).filter(
            family__isnull=True,
        ).update(family_id=family.pk)

    members = User.objects.using(db_alias).filter(
        username__in=MEMBER_USERNAMES,
    )
    for user in members:
        FamilyMembership.objects.using(db_alias).update_or_create(
            user_id=user.pk,
            defaults={
                "family_id": family.pk,
                "role": "member",
            },
        )

    admin = User.objects.using(db_alias).filter(
        username=ADMIN_USERNAME,
    ).first()
    if admin:
        FamilyMembership.objects.using(db_alias).update_or_create(
            user_id=admin.pk,
            defaults={
                "family_id": family.pk,
                "role": "admin",
            },
        )


def reverse_assignment(apps, schema_editor):
    db_alias = schema_editor.connection.alias

    Family = apps.get_model("shopping_list", "Family")
    FamilyMembership = apps.get_model(
        "shopping_list",
        "FamilyMembership",
    )
    Shop = apps.get_model("shopping_list", "Shop")
    Category = apps.get_model("shopping_list", "Category")
    Product = apps.get_model("shopping_list", "Product")
    ListItem = apps.get_model("shopping_list", "ListItem")

    family = Family.objects.using(db_alias).filter(
        name=FAMILY_NAME,
    ).first()
    if not family:
        return

    FamilyMembership.objects.using(db_alias).filter(
        family_id=family.pk,
    ).delete()

    for model in (Shop, Category, Product, ListItem):
        model.objects.using(db_alias).filter(
            family_id=family.pk,
        ).update(family=None)

    family.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("shopping_list", "0022_category_family_listitem_family_product_family_and_more"),
    ]

    operations = [
        migrations.RunPython(
            assign_existing_data,
            reverse_assignment,
        ),
    ]
