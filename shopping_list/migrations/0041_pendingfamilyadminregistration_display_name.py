from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shopping_list', '0040_alter_family_slug_required'),
    ]

    operations = [
        migrations.AddField(
            model_name='pendingfamilyadminregistration',
            name='display_name',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
    ]
