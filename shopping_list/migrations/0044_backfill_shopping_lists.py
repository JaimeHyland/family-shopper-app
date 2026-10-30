from django.db import migrations


def backfill_shopping_lists(apps, schema_editor):
    """
    Every family's existing ListItems represent its one de-facto current
    list (there was never a "close" operation before this migration), so
    each family gets exactly one open ShoppingList, with all of its
    existing ListItems attached to it.
    """
    Family = apps.get_model('shopping_list', 'Family')
    ShoppingList = apps.get_model('shopping_list', 'ShoppingList')
    ListItem = apps.get_model('shopping_list', 'ListItem')

    for family in Family.objects.all():
        if not ListItem.objects.filter(family=family).exists():
            # No items yet for this family: no list needed until one is
            # actually used (ShoppingList.get_or_create_open handles that
            # on demand).
            continue

        shopping_list, _ = ShoppingList.objects.get_or_create(
            family=family, status='open',
        )
        ListItem.objects.filter(
            family=family, shopping_list__isnull=True,
        ).update(shopping_list=shopping_list)


def noop_reverse(apps, schema_editor):
    # Reversing would require deciding what to do with ShoppingList rows
    # that may by then have items from more than one historical list;
    # not a meaningful operation to support automatically.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('shopping_list', '0043_shoppinglist_listitem_shopping_list_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_shopping_lists, noop_reverse),
    ]
