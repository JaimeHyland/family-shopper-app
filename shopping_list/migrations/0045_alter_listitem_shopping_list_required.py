import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shopping_list', '0044_backfill_shopping_lists'),
    ]

    operations = [
        migrations.AlterField(
            model_name='listitem',
            name='shopping_list',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='items', to='shopping_list.shoppinglist'), # noqa
        ),
    ]
