import json
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings
from django.core.exceptions import (
    ImproperlyConfigured,
    PermissionDenied,
    ValidationError,
)
from django.db import transaction

from .models import Category, Family, FamilyMembership, Product


SEED_DATA_DIR = Path(__file__).resolve().parent / "seed_data"


@dataclass
class SeedImportResult:
    """Outcome of importing a seed-list selection into the family's
    product catalogue. Both lists are Product instances: `created` are
    brand-new catalogue rows, `reused` are existing catalogue products
    whose name exactly matched a selected seed product (so nothing was
    created for them). Neither list touches the shopping list."""
    created: list = field(default_factory=list)
    reused: list = field(default_factory=list)


def load_seed_lists():
    seeds = {}
    for path in sorted(SEED_DATA_DIR.glob("*.json")):
        try:
            seed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ImproperlyConfigured(
                f"Cannot load seed list {path.name}.",
            ) from exc
        valid = (
            isinstance(seed, dict)
            and isinstance(seed.get("name"), str)
            and bool(seed["name"].strip())
            and isinstance(seed.get("categories"), list)
            and bool(seed["categories"])
        )
        names = set()
        category_names = set()
        if valid:
            for category in seed["categories"]:
                if not (
                    isinstance(category, dict)
                    and isinstance(category.get("name"), str)
                    and 0 < len(category["name"].strip()) <= 50
                    and category["name"] not in category_names
                    and isinstance(category.get("products"), list)
                    and bool(category["products"])
                ):
                    valid = False
                    break
                category_names.add(category["name"])
                for name in category["products"]:
                    if not (
                        isinstance(name, str)
                        and 0 < len(name.strip()) <= 254
                        and name == name.strip()
                        and name not in names
                    ):
                        valid = False
                        break
                    names.add(name)
        if not valid:
            raise ImproperlyConfigured(
                f"Invalid seed list structure in {path.name}.",
            )
        seeds[path.stem] = seed
    if not seeds:
        raise ImproperlyConfigured("No shopping-list seed files were found.")
    return seeds


def get_default_seed_id(seeds, family):
    """
    The seed_id (a key into `seeds`, i.e. a seed_data/*.json filename
    stem) to pre-select the first time a member visits the seed-list
    page, chosen according to the kind of family they belong to --
    configured via settings.DEFAULT_SEED_LIST_BY_USER_TYPE, so either
    default can be changed without touching this function, and the JSON
    dataset files themselves carry no notion of who defaults to them.

    "User type" comes from Family.is_solo, set once at registration
    time (see shopping_list.registration), not from the family's
    current membership count: a freshly-registered family admin also
    starts out with exactly one membership, indistinguishable from a
    solo user on membership count alone until other members are invited.

    Only the "solo"/"family" dimension is implemented now; the
    configuration dict's keys could later be extended to also vary by
    locale/geography (e.g. a (user_type, locale) tuple) without
    changing this function's signature or callers.
    """
    user_type = "solo" if family.is_solo else "family"
    seed_id = settings.DEFAULT_SEED_LIST_BY_USER_TYPE.get(user_type)
    if seed_id in seeds:
        return seed_id
    # Configured default missing or its file no longer exists -- fall
    # back to the previous behaviour (the first available seed list)
    # rather than erroring out on what is otherwise a working page.
    return next(iter(seeds))


def import_seed_products(request, seed_id, selected_names):
    """Creates or reuses catalogue Product/Category records for the
    selected seed products. This is a catalogue-only operation: it never
    creates, modifies or removes ListItems, never touches anyone's
    basket, and never broadcasts shopping-list WebSocket events (there is
    nothing shopping-list-related to report). Users subsequently add
    individual products to their shopping list through the existing
    product-catalogue workflow (AddToShoppingListView)."""
    seed = load_seed_lists().get(seed_id)
    if seed is None:
        raise ValidationError("Choose a valid seed list.")
    selected_names = set(selected_names)
    allowed_names = {
        name for category in seed["categories"]
        for name in category["products"]
    }
    if not selected_names <= allowed_names:
        raise ValidationError("Choose products from the selected seed list.")

    result = SeedImportResult()
    with transaction.atomic():
        family = Family.objects.select_for_update().get(pk=request.family.pk)
        if not FamilyMembership.objects.filter(
            family=family, user=request.user,
        ).exists():
            raise PermissionDenied("You are not a member of this family.")
        if not selected_names:
            return result

        # Exact, case-sensitive name match against the family's existing
        # catalogue: the established seed-list duplicate rule (distinct
        # from forms' case-insensitive validation). Several matching
        # catalogue rows resolve deterministically: an active one first,
        # then the lowest id.
        existing_products = {}
        for product in Product.objects.filter(
            family=family, product_name__in=selected_names,
        ).order_by("-current", "pk"):
            existing_products.setdefault(product.product_name, product)

        for category_data in seed["categories"]:
            category = None
            for name in category_data["products"]:
                if name not in selected_names:
                    continue
                product = existing_products.get(name)
                if product is not None:
                    result.reused.append(product)
                    continue
                if category is None:
                    category = Category.objects.filter(
                        family=family,
                        category_name__iexact=category_data["name"],
                    ).order_by("-current", "pk").first()
                    if category is None:
                        category = Category.objects.create(
                            family=family,
                            category_name=category_data["name"],
                            creator=request.user,
                        )
                # Only server-owned seed names may create catalogue rows.
                # Exact matching here deliberately differs from form-level
                # case-insensitive duplicate validation.
                product = Product.objects.create(
                    family=family, product_name=name, category=category,
                    creator=request.user, default_quantity=1,
                )
                result.created.append(product)
    return result
