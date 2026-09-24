from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shopping_list', '0039_populate_family_slug'),
    ]

    operations = [
        migrations.AlterField(
            model_name='family',
            name='slug',
            field=models.SlugField(editable=False, max_length=250, unique=True), # noqa
        ),
    ]
