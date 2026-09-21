# Generated manually: renames the misleadingly-named "bought" boolean to
# "in_basket", which is what it has always actually represented (the
# active shopping-list state). The future terminal "purchased" concept
# (date_bought/buyer/quantity_bought/shop_bought) is untouched.
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("shopping_list", "0034_automatic_adult_group_membership"),
    ]

    operations = [
        migrations.RenameField(
            model_name="listitem",
            old_name="bought",
            new_name="in_basket",
        ),
    ]
