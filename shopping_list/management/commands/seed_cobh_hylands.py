"""
One-off data seed for the Cobh Hylands family: creates the product
categories/products listed below if they don't already (case-
insensitively, among currently-active records) exist for that family.

Intended to be removed again once it has been run against production.

Usage:
    python manage.py seed_cobh_hylands [--family-name "Cobh Hylands"] [--dry-run]
"""
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from shopping_list.models import Category, Family, Product


CATALOGUE = {
    "Fresh Fruit": [
        "Apples", "Bananas", "Oranges", "Pears", "Grapes", "Strawberries",
    ],
    "Vegetables": [
        "Potatoes", "Carrots", "Onions", "Broccoli", "Cauliflower",
        "Peppers", "Tomatoes",
    ],
    "Dairy & Eggs": [
        "Milk", "Butter", "Cheese", "Eggs", "Natural yoghurt", "Cream",
    ],
    "Bread & Bakery": [
        "Sliced bread", "Brown bread", "Bread rolls", "Croissants",
        "Soda bread",
    ],
    "Tinned & Dry Goods": [
        "Baked beans", "Tinned tomatoes", "Tinned sweetcorn", "Tinned tuna",
        "Rice", "Pasta", "Flour", "Sugar",
    ],
    "Tea, Coffee & Drinks": [
        "Tea bags", "Coffee", "Hot chocolate", "Orange juice", "Apple juice",
    ],
    "Household": [
        "Toilet paper", "Kitchen roll", "Washing-up liquid",
        "Laundry detergent", "Bin bags", "Aluminium foil", "Cling film",
    ],
    "Hardware & Household Supplies": [
        "Light bulbs", "Batteries", "Sellotape", "String", "Matches",
        "Sponges", "Clothes pegs",
    ],
    "Staples": [
        "Rum", "Oil paints", "Sensemilla",
    ],
}


class Command(BaseCommand):
    help = "Seed the Cobh Hylands family with a fixed product catalogue."

    def add_arguments(self, parser):
        parser.add_argument(
            "--family-name", default="Cobh Hylands",
            help="Exact family name to seed (default: 'Cobh Hylands').",
        )
        parser.add_argument(
            "--creator-username", default=None,
            help=(
                "Username to record as creator of any new records. If "
                "omitted, creator is left unset (the field is nullable)."
            ),
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would happen without writing anything.",
        )

    def handle(self, *args, **options):
        family_name = options["family_name"]
        dry_run = options["dry_run"]
        creator = None
        if options["creator_username"]:
            try:
                creator = get_user_model().objects.get(
                    username=options["creator_username"],
                )
            except get_user_model().DoesNotExist:
                raise CommandError(
                    f"No user named {options['creator_username']!r} "
                    "was found. Aborting without making any changes.",
                )

        matching_families = list(
            Family.objects.filter(name__iexact=family_name),
        )
        if len(matching_families) == 0:
            raise CommandError(
                f"No family named {family_name!r} was found. Aborting "
                "without making any changes.",
            )
        if len(matching_families) > 1:
            ids = ", ".join(str(f.pk) for f in matching_families)
            raise CommandError(
                f"{len(matching_families)} families named {family_name!r} "
                f"were found (ids: {ids}); this must be unambiguous. "
                "Aborting without making any changes.",
            )
        family = matching_families[0]
        self.stdout.write(
            f"Target family: {family.name!r} (id={family.pk}, "
            f"public_id={family.public_id})",
        )

        categories_created = []
        categories_existing = []
        products_created = []
        products_existing = []
        anomalies = []

        with transaction.atomic():
            for category_name, product_names in CATALOGUE.items():
                category, created = self._get_or_create_category(
                    family, category_name, creator, anomalies,
                )
                (categories_created if created else categories_existing) \
                    .append(category)

                for product_name in product_names:
                    product, created = self._get_or_create_product(
                        family, category, product_name, creator, anomalies,
                    )
                    (products_created if created else products_existing) \
                        .append(product)

            if dry_run:
                transaction.set_rollback(True)

        self._report(
            dry_run, categories_created, categories_existing,
            products_created, products_existing, anomalies,
        )

    def _get_or_create_category(
        self, family, category_name, creator, anomalies,
    ):
        existing = Category.objects.filter(
            family=family, category_name__iexact=category_name,
            current=True,
        ).first()
        if existing is not None:
            if existing.category_name != category_name:
                anomalies.append(
                    f"Category {category_name!r} matched existing "
                    f"{existing.category_name!r} (case differs); reused "
                    "the existing record rather than creating a new one.",
                )
            return existing, False

        category = Category(
            category_name=category_name, family=family, creator=creator,
        )
        # creator is null=True but not blank=True: full_clean() would
        # otherwise wrongly demand a value even though the DB allows NULL.
        exclude = [] if creator else ['creator']
        try:
            category.full_clean(exclude=exclude)
        except ValidationError as exc:
            anomalies.append(
                f"Category {category_name!r} failed validation: {exc}",
            )
            raise
        category.save()
        return category, True

    def _get_or_create_product(
        self, family, category, product_name, creator, anomalies,
    ):
        existing = Product.objects.filter(
            family=family, product_name__iexact=product_name,
            current=True,
        ).first()
        if existing is not None:
            if existing.category_id != category.id:
                anomalies.append(
                    f"Product {product_name!r} already exists for this "
                    f"family under category {existing.category!r}, not "
                    f"{category.category_name!r}; left it unchanged.",
                )
            if existing.product_name != product_name:
                anomalies.append(
                    f"Product {product_name!r} matched existing "
                    f"{existing.product_name!r} (case differs); reused "
                    "the existing record rather than creating a new one.",
                )
            return existing, False

        product = Product(
            product_name=product_name, family=family, category=category,
            creator=creator,
        )
        # creator is null=True but not blank=True: full_clean() would
        # otherwise wrongly demand a value even though the DB allows NULL.
        exclude = [] if creator else ['creator']
        try:
            product.full_clean(exclude=exclude)
        except ValidationError as exc:
            anomalies.append(
                f"Product {product_name!r} failed validation: {exc}",
            )
            raise
        product.save()
        return product, True

    def _report(
        self, dry_run, categories_created, categories_existing,
        products_created, products_existing, anomalies,
    ):
        if dry_run:
            self.stdout.write(self.style.WARNING(
                "Dry run: no changes were committed.",
            ))

        self.stdout.write(self.style.SUCCESS(
            f"Categories created: {len(categories_created)}",
        ))
        for category in categories_created:
            self.stdout.write(f"  + {category.category_name}")

        self.stdout.write(
            f"Categories already present (reused): "
            f"{len(categories_existing)}",
        )
        for category in categories_existing:
            self.stdout.write(f"  = {category.category_name}")

        self.stdout.write(self.style.SUCCESS(
            f"Products created: {len(products_created)}",
        ))
        for product in products_created:
            self.stdout.write(f"  + {product.product_name}")

        self.stdout.write(
            f"Products already present (reused): {len(products_existing)}",
        )
        for product in products_existing:
            self.stdout.write(f"  = {product.product_name}")

        if anomalies:
            self.stdout.write(self.style.WARNING(
                f"Anomalies ({len(anomalies)}):",
            ))
            for anomaly in anomalies:
                self.stdout.write(self.style.WARNING(f"  ! {anomaly}"))
        else:
            self.stdout.write(self.style.SUCCESS("No anomalies."))
