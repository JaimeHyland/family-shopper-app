import uuid

from django.db import migrations, models


def split_units(apps, schema_editor):
    Item = apps.get_model('shopping_list', 'ListItem')
    db = schema_editor.connection.alias
    items = Item.objects.using(db)
    # Validate before writing anything. Historical inconsistencies need
    # an explicit decision, never guessed ownership or lost quantities.
    for item in items.iterator():
        if (item.quantity_required < 1
                or item.quantity_bought != (
                    item.quantity_required if item.date_bought else 0
                )
                or (item.in_basket and item.put_in_basket_by_id is None)):
            raise RuntimeError(
                f'ListItem {item.pk} has ambiguous quantities or basket '
                'ownership. Resolve its historical data before migrating.'
            )
        # Bought rows are terminal: preserve their historical flags and
        # attribution. Unbought rows must support basket/restore actions.
        if item.date_bought is None:
            if item.cancelled and item.in_basket:
                raise RuntimeError(
                    f'ListItem {item.pk} is unbought but both cancelled '
                    'and in a basket. Reconcile its cancellation and '
                    'basket state explicitly before rerunning migration 0047.'
                )
            if not item.in_basket and item.put_in_basket_by_id is not None:
                raise RuntimeError(
                    f'ListItem {item.pk} is unbought and has a basket owner '
                    'while in_basket=False. Reconcile its basket flag and '
                    'recorded ownership explicitly before rerunning '
                    'migration 0047.'
                )

    # Restrict iteration to original rows; inserted clones have a key.
    for item in items.filter(request_key__isnull=True).iterator():
        key = uuid.uuid4()
        quantity = item.quantity_required
        bought = 1 if item.date_bought else 0
        values = {
            field.attname: getattr(item, field.attname)
            for field in Item._meta.concrete_fields
            if field.name not in (
                'id', 'slug', 'request_key', 'quantity_required',
                'quantity_bought',
            )
        }
        items.filter(pk=item.pk).update(
            request_key=key, quantity_required=1, quantity_bought=bought,
        )
        for _ in range(quantity - 1):
            clone = items.create(
                **values, request_key=key,
                quantity_required=1, quantity_bought=bought,
            )
            slug = f'unit-{clone.pk}'
            while items.filter(family_id=item.family_id, slug=slug).exists():
                slug = f'unit-{uuid.uuid4().hex}'
            items.filter(pk=clone.pk).update(slug=slug, date_created=item.date_created)
    if schema_editor.connection.vendor == 'postgresql':
        # FK checks queued by the clones must finish before AlterField.
        schema_editor.execute('SET CONSTRAINTS ALL IMMEDIATE')


def merge_units(apps, schema_editor):
    Item = apps.get_model('shopping_list', 'ListItem')
    items = Item.objects.using(schema_editor.connection.alias)
    fields = [f.attname for f in Item._meta.concrete_fields if f.name not in (
        'id', 'slug', 'request_key', 'quantity_required', 'quantity_bought',
    )]
    keys = items.values_list('request_key', flat=True).distinct()
    # Refuse reversal once units diverge: merging different owners or
    # states would destroy information. The atomic migration rolls back.
    for key in list(keys):
        units = list(items.filter(request_key=key).order_by('pk'))
        first = units[0]
        if any(any(getattr(unit, f) != getattr(first, f) for f in fields)
               for unit in units[1:]):
            raise RuntimeError(
                'Cannot merge units with different states or metadata. '
                'Keep the unit schema or restore the pre-migration backup.'
            )
        items.filter(pk=first.pk).update(
            quantity_required=len(units),
            quantity_bought=sum(unit.quantity_bought for unit in units),
        )
        items.filter(pk__in=[unit.pk for unit in units[1:]]).delete()
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute('SET CONSTRAINTS ALL IMMEDIATE')


class Migration(migrations.Migration):
    dependencies = [('shopping_list', '0046_listitem_restored_by')]

    operations = [
        migrations.AddField(
            model_name='listitem', name='request_key',
            field=models.UUIDField(null=True, db_index=True),
        ),
        migrations.RunPython(split_units, merge_units),
        migrations.AlterField(
            model_name='listitem', name='request_key',
            field=models.UUIDField(default=uuid.uuid4, db_index=True),
        ),
        migrations.AddConstraint(
            model_name='listitem',
            constraint=models.CheckConstraint(
                condition=models.Q(quantity_required=1),
                name='list_item_is_one_unit',
            ),
        ),
        migrations.AddConstraint(
            model_name='listitem',
            constraint=models.CheckConstraint(
                condition=models.Q(quantity_bought__in=[0, 1]),
                name='list_item_bought_at_most_one',
            ),
        ),
    ]
