from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shopping_list', '0037_alter_product_category_alter_product_default_shop'), # noqa
    ]

    operations = [
        migrations.AddField(
            model_name='family',
            name='slug',
            field=models.SlugField(blank=True, editable=False, max_length=250, null=True), # noqa
        ),
    ]
