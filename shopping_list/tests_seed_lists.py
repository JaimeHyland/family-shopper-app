from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.core.exceptions import (
    ImproperlyConfigured,
    PermissionDenied,
    ValidationError,
)
from django.core import mail
from django.db import close_old_connections
from django.test import (
    Client, SimpleTestCase, TransactionTestCase, override_settings,
)
from django.urls import reverse
from django.utils import timezone

from family_shopping_list.consumers import ShoppingConsumer
from .models import (
    Category, Family, FamilyMembership, ListItem, Product, ShoppingList,
)
from .seed_lists import import_seed_products, load_seed_lists
from .tests import TwoFamilyFixtureMixin, TwoFamilyTestCase, ws_path


SEED_ID = "irish_student_essentials"
TEST_CHANNEL_LAYERS = {
    "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
}


class SeedDataTests(SimpleTestCase):
    def test_three_named_lists_have_unique_categories_and_products(self):
        seeds = load_seed_lists()
        self.assertEqual(set(seeds), {
            SEED_ID, "irish_family_small_children", "older_irish_couple",
        })
        for seed in seeds.values():
            with self.subTest(seed=seed["name"]):
                self.assertTrue(seed["name"])
                categories = seed["categories"]
                self.assertEqual(len(categories), 10)
                names = [name for c in categories for name in c["products"]]
                self.assertEqual(len(names), len(set(names)))
                self.assertGreaterEqual(len(names), 40)
                self.assertLessEqual(len(names), 70)
                self.assertIn("Potatoes", names)
                self.assertIn("Tea bags", names)

    def test_invalid_data_and_missing_files_are_explicit_errors(self):
        with patch("shopping_list.seed_lists.SEED_DATA_DIR") as directory:
            directory.glob.return_value = []
            with self.assertRaises(ImproperlyConfigured):
                load_seed_lists()
            directory.glob.return_value = [SimpleNamespace(
                name="bad.json", read_text=lambda **kwargs: '{"name": "Bad"}',
            )]
            with self.assertRaises(ImproperlyConfigured):
                load_seed_lists()


class SeedImportTests(TwoFamilyTestCase):
    def setUp(self):
        super().setUp()
        self.request = SimpleNamespace(
            family=self.family_a, user=self.member_a,
        )

    def seed(self, names, seed_id=SEED_ID):
        return import_seed_products(self.request, seed_id, names)

    def test_selected_products_are_created_in_the_catalogue_only(self):
        before_items = list(ListItem.objects.order_by("pk").values())
        result = self.seed(["Potatoes", "Milk"])
        self.assertEqual({p.product_name for p in result.created}, {
            "Potatoes", "Milk",
        })
        self.assertEqual(result.reused, [])
        for product in result.created:
            self.assertEqual(product.family, self.family_a)
            self.assertEqual(product.creator, self.member_a)
            self.assertTrue(product.current)
        self.assertTrue(Category.objects.filter(
            family=self.family_a, category_name="Vegetables",
        ).exists())
        self.assertTrue(Category.objects.filter(
            family=self.family_a, category_name="Dairy and Eggs",
        ).exists())
        # Strictly catalogue-only: no shopping-list side effects.
        self.assertEqual(list(ListItem.objects.order_by("pk").values()), before_items) # noqa
        self.assertFalse(Product.objects.filter(
            family=self.family_a, product_name="Apples",
        ).exists())

    def test_empty_selection_writes_nothing(self):
        family = Family.objects.create(name="Empty family")
        FamilyMembership.objects.create(
            family=family, user=self.member_a, display_name="Member",
        )
        self.request.family = family
        result = self.seed([])
        self.assertEqual(result.created, [])
        self.assertEqual(result.reused, [])
        self.assertFalse(Product.objects.filter(family=family).exists())
        self.assertFalse(Category.objects.filter(family=family).exists())

    def test_import_never_creates_or_requires_a_shopping_list(self):
        family = Family.objects.create(name="No list family")
        FamilyMembership.objects.create(
            family=family, user=self.member_a, display_name="Member",
        )
        self.request.family = family
        self.assertFalse(ShoppingList.objects.filter(family=family).exists())

        result = self.seed(["Potatoes"])

        self.assertEqual(len(result.created), 1)
        self.assertFalse(ShoppingList.objects.filter(family=family).exists())

    def test_exact_name_duplicate_is_reused_not_recreated(self):
        self.product_a.product_name = "Potatoes"
        self.product_a.save()

        result = self.seed(["Potatoes", "Potatoes"])

        self.assertEqual(result.created, [])
        self.assertEqual([p.pk for p in result.reused], [self.product_a.pk])
        self.assertEqual(Product.objects.filter(
            family=self.family_a, product_name="Potatoes",
        ).count(), 1)

    def test_different_names_and_case_remain_distinct(self):
        self.product_a.product_name = "Potatoes, floury"
        self.product_a.save()

        result = self.seed(["Potatoes"])

        self.assertEqual({p.product_name for p in result.created}, {
            "Potatoes",
        })
        self.assertEqual(Product.objects.filter(
            family=self.family_a,
        ).values_list("product_name", flat=True).count(), 2)

        existing_milk = Product.objects.create(
            family=self.family_a, product_name="milk", creator=self.admin_a,
        )
        result = self.seed(["Milk"])
        self.assertEqual([p.product_name for p in result.created], ["Milk"])
        self.assertEqual(result.reused, [])
        self.assertEqual(Product.objects.filter(
            family=self.family_a, product_name__iexact="milk",
        ).count(), 2)
        existing_milk.refresh_from_db()
        self.assertEqual(existing_milk.product_name, "milk")

    def test_reuse_preserves_existing_shopping_list_items_unchanged(self):
        # These items simulate products already added to the shopping
        # list through the normal workflow, in varied states. Importing
        # the same product names again must not touch any of them.
        potatoes = Product.objects.create(
            family=self.family_a, product_name="Potatoes", creator=self.admin_a, # noqa
        )
        milk = Product.objects.create(
            family=self.family_a, product_name="Milk", creator=self.admin_a,
        )
        now = timezone.now()
        ListItem.objects.create(
            family=self.family_a, product=potatoes, creator=self.admin_a,
            in_basket=True, put_in_basket_by=self.admin_a,
            time_put_in_basket=now,
        )
        ListItem.objects.create(
            family=self.family_a, product=milk, creator=self.admin_a,
            buyer=self.member_a, date_bought=now, quantity_bought=1,
        )
        ListItem.objects.filter(pk=self.list_item_a.pk).update(
            cancelled=True, cancelled_by=self.admin_a, date_cancelled=now,
        )
        before = list(ListItem.objects.order_by("pk").values())

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            result = self.seed(["Potatoes", "Milk"])

        self.assertEqual(callbacks, [])
        self.assertEqual(list(ListItem.objects.order_by("pk").values()), before) # noqa
        self.assertEqual(result.created, [])
        self.assertCountEqual(
            [p.pk for p in result.reused], [potatoes.pk, milk.pk],
        )

    def test_catalogue_resolution_is_family_scoped_and_deterministic(self):
        foreign = Product.objects.create(
            family=self.family_b, product_name="Potatoes", creator=self.admin_b, # noqa
        )
        archived = Product.objects.create(
            family=self.family_a, product_name="Milk", current=False,
            creator=self.admin_a,
        )
        active = Product.objects.create(
            family=self.family_a, product_name="Milk", creator=self.admin_a,
            category=self.category_a, default_shop=self.shop_a,
        )
        Product.objects.create(
            family=self.family_a, product_name="Milk", creator=self.admin_a,
        )

        result = self.seed(["Potatoes", "Milk"])

        self.assertEqual([p.product_name for p in result.created], [
            "Potatoes",
        ])
        self.assertNotEqual(result.created[0].pk, foreign.pk)
        reused_by_name = {p.product_name: p for p in result.reused}
        self.assertEqual(reused_by_name["Milk"].pk, active.pk)
        active.refresh_from_db()
        archived.refresh_from_db()
        self.assertEqual(active.category, self.category_a)
        self.assertFalse(archived.current)

    def test_inactive_catalogue_rows_are_reused_without_reactivation(self):
        category = Category.objects.create(
            family=self.family_a, category_name="vegetables", current=False,
            creator=self.admin_a,
        )
        product = Product.objects.create(
            family=self.family_a, product_name="Milk", current=False,
            creator=self.admin_a,
        )

        result = self.seed(["Potatoes", "Milk"])

        reused_by_name = {p.product_name: p for p in result.reused}
        created_by_name = {p.product_name: p for p in result.created}
        self.assertEqual(reused_by_name["Milk"].pk, product.pk)
        self.assertEqual(created_by_name["Potatoes"].category_id, category.pk)
        category.refresh_from_db()
        product.refresh_from_db()
        self.assertFalse(category.current)
        self.assertFalse(product.current)

    def test_only_trusted_seed_names_and_members_can_import(self):
        before_products = Product.objects.count()
        for seed_id, names in [
            ("../secrets", ["Milk"]), (SEED_ID, ["Arbitrary new product"]),
        ]:
            with self.assertRaises(ValidationError):
                self.seed(names, seed_id)
        self.request.family = self.family_b
        with self.assertRaises(PermissionDenied):
            self.seed(["Milk"])
        self.assertEqual(Product.objects.count(), before_products)

    def test_failure_rolls_back_the_whole_catalogue_import(self):
        with patch(
            "shopping_list.seed_lists.Product.objects.create",
            side_effect=ValidationError("Cannot create product"),
        ):
            with self.assertRaises(ValidationError):
                self.seed(["Potatoes"])
        self.assertFalse(Product.objects.filter(
            family=self.family_a, product_name="Potatoes",
        ).exists())
        self.assertFalse(Category.objects.filter(
            family=self.family_a, category_name="Vegetables",
        ).exists())

    def test_import_sends_no_shopping_list_websocket_events(self):
        with patch("shopping_list.views.broadcast_list_item_added") as added, \
             patch("shopping_list.views.broadcast_list_item_update") as updated, \
             patch("shopping_list.views.broadcast_product_list_status") as status: # noqa
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                result = self.seed(["Potatoes", "Milk"])
            self.assertEqual(callbacks, [])
        self.assertEqual(len(result.created), 2)
        added.assert_not_called()
        updated.assert_not_called()
        status.assert_not_called()


@override_settings(CHANNEL_LAYERS=TEST_CHANNEL_LAYERS)
class SeedImportTransactionTests(TwoFamilyFixtureMixin, TransactionTestCase):
    def test_concurrent_imports_create_only_one_catalogue_product(self):
        barrier = Barrier(2)

        def import_once(_):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                request = SimpleNamespace(
                    family=self.family_a, user=self.member_a,
                )
                result = import_seed_products(request, SEED_ID, ["Milk"])
                return len(result.created)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(import_once, range(2)))
        self.assertCountEqual(results, [1, 0])
        self.assertEqual(Product.objects.filter(
            family=self.family_a, product_name="Milk",
        ).count(), 1)
        self.assertFalse(ListItem.objects.filter(
            family=self.family_a, product__product_name="Milk",
        ).exists())

    async def test_connected_family_receives_no_events_from_catalogue_import(self): # noqa
        socket = WebsocketCommunicator(
            ShoppingConsumer.as_asgi(), ws_path(self.family_a),
        )
        socket.scope["user"] = self.admin_a
        socket.scope["url_route"] = {
            "kwargs": {"family_slug": self.family_a.slug},
        }
        self.assertTrue((await socket.connect())[0])
        try:
            request = SimpleNamespace(family=self.family_a, user=self.member_a) # noqa
            result = await sync_to_async(import_seed_products)(
                request, SEED_ID, ["Milk"],
            )
            self.assertEqual(len(result.created), 1)
            self.assertTrue(await socket.receive_nothing())
        finally:
            await socket.disconnect()


class SeedSelectionViewTests(TwoFamilyTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.member_a)
        self.url = self.url_a("seed_lists")
        # Seed-list import creates catalogue products, so it is governed
        # by can_add_catalogue like any other catalogue-add operation
        # (see AddProductView). That flag defaults to False for ordinary
        # members; these tests exercise an ordinary member who has been
        # explicitly granted it, since that is the only way an ordinary
        # member can use this feature at all.
        FamilyMembership.objects.filter(
            family=self.family_a, user=self.member_a,
        ).update(can_add_catalogue=True)

    def submit(self, names, action="review", **kwargs):
        return self.client.post(self.url, {
            "seed": SEED_ID, "products": names, "action": action,
        }, **kwargs)

    @staticmethod
    def product_inputs(response):
        class Inputs(HTMLParser):
            def __init__(self):
                super().__init__()
                self.products = {}

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "input" and "data-seed-product" in attrs:
                    self.products[attrs["value"]] = "checked" in attrs

        inputs = Inputs()
        inputs.feed(response.content.decode())
        return inputs.products

    def test_existing_users_can_choose_all_three_lists_and_see_every_product(self):
        before = list(ListItem.objects.order_by("pk").values())
        for seed_id, seed in load_seed_lists().items():
            response = self.client.get(self.url, {"seed": seed_id})
            self.assertEqual(response.status_code, 200)
            for option in load_seed_lists().values():
                self.assertContains(response, option["name"])
            for category in seed["categories"]:
                self.assertContains(response, category["name"])
                for name in category["products"]:
                    self.assertContains(response, name)
            self.assertTrue(all(self.product_inputs(response).values()))
            self.assertContains(response, "Select all products")
            self.assertContains(response, "Review selection")
            self.assertContains(response, 'data-seed-category-group', count=10)
            self.assertContains(response, 'id="seed-selection-count"')
            self.assertContains(response, "seed_selection.js")
        self.assertEqual(list(ListItem.objects.order_by("pk").values()), before)

    def test_seed_list_dropdown_auto_submits_on_change(self):
        # Changing the dropdown must refresh the page to the chosen
        # seed list without requiring a separate button click.
        response = self.client.get(self.url)

        self.assertContains(
            response,
            '<select id="seed-list" name="seed" '
            'class="form-select w-auto mw-100" '
            'onchange="this.form.submit()">',
        )

    def test_seed_list_entry_point_is_on_products_not_shopping_list(self):
        shopping_list_page = self.client.get(self.url_a("shopping_list"))
        self.assertNotContains(shopping_list_page, f'href="{self.url}"')
        self.assertNotContains(shopping_list_page, "Use a seed list")

        products_page = self.client.get(self.url_a("product_list"))
        self.assertContains(products_page, f'href="{self.url}"')
        self.assertContains(products_page, "Use a seed list")

    def test_review_and_edit_keep_only_chosen_products_and_never_write(self):
        before = list(ListItem.objects.order_by("pk").values())
        response = self.submit(["Milk", "Potatoes"])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Review your selection")
        self.assertContains(response, 'name="products" value="Milk"')
        self.assertContains(response, 'name="products" value="Potatoes"')
        self.assertNotContains(response, 'name="products" value="Apples"')
        self.assertContains(response, "Edit selection")
        self.assertContains(response, "Add to product catalogue")
        self.assertEqual(list(ListItem.objects.order_by("pk").values()), before)
        response = self.submit(["Milk", "Potatoes"], "edit")
        checked = self.product_inputs(response)
        self.assertEqual({name for name, value in checked.items() if value}, {
            "Milk", "Potatoes",
        })
        self.assertFalse(checked["Apples"])
        self.assertEqual(list(ListItem.objects.order_by("pk").values()), before)
        self.assertFalse(Product.objects.filter(
            family=self.family_a, product_name="Milk",
        ).exists())

    def test_import_creates_and_reuses_catalogue_products_without_touching_the_list(self): # noqa
        before = list(ListItem.objects.order_by("pk").values())
        self.submit(["Potatoes"], "import")
        response = self.submit(["Potatoes", "Milk"], "import", follow=True)
        self.assertEqual(
            response.redirect_chain[-1][0], self.url_a("product_list"),
        )
        self.assertContains(response, "Added to your product catalogue: 1.")
        self.assertContains(response, "Already in your catalogue: 1.")
        self.assertContains(response, "Nothing was added to your shopping list.") # noqa
        self.assertEqual(Product.objects.filter(
            family=self.family_a, product_name="Potatoes",
        ).count(), 1)
        self.assertTrue(Product.objects.filter(
            family=self.family_a, product_name="Milk",
        ).exists())
        self.assertEqual(list(ListItem.objects.order_by("pk").values()), before) # noqa

    def test_empty_selection_can_be_reviewed_edited_and_imported(self):
        before = list(ListItem.objects.order_by("pk").values())
        response = self.submit([])
        self.assertContains(response, "No products selected.")
        response = self.submit([], "edit")
        self.assertFalse(any(self.product_inputs(response).values()))
        response = self.submit([], "import", follow=True)
        self.assertContains(response, "No products selected; nothing was added.")
        self.assertEqual(list(ListItem.objects.order_by("pk").values()), before)
        self.assertFalse(Product.objects.filter(family=self.family_a).exclude(
            pk=self.product_a.pk,
        ).exists())

    def test_unknown_seed_names_products_and_actions_are_rejected(self):
        before = list(ListItem.objects.order_by("pk").values())
        for seed_id in ("../secrets", "missing"):
            self.assertEqual(self.client.get(
                self.url, {"seed": seed_id},
            ).status_code, 404)
            self.assertEqual(self.client.post(self.url, {
                "seed": seed_id, "products": ["Milk"], "action": "import",
            }).status_code, 404)
        response = self.submit(["Milk", "Arbitrary product"], "import")
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Select a valid choice.", status_code=400)
        self.assertTrue(self.product_inputs(response)["Milk"])
        self.assertEqual(self.submit(["Milk"], "delete").status_code, 400)
        self.assertEqual(list(ListItem.objects.order_by("pk").values()), before)

    def test_anonymous_and_other_family_requests_cannot_import(self):
        before = list(ListItem.objects.order_by("pk").values())
        other_url = self.url_b("seed_lists")
        self.assertEqual(self.client.get(other_url).status_code, 404)
        self.assertEqual(self.client.post(other_url, {
            "seed": SEED_ID, "products": ["Milk"], "action": "import",
        }).status_code, 404)
        self.client.logout()
        for response in (self.client.get(self.url),
                         self.submit(["Milk"], "import")):
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response["Location"].startswith(
                reverse("logged_out_homepage") + "?next=",
            ))
        self.assertEqual(list(ListItem.objects.order_by("pk").values()), before)

    def test_csrf_is_required_and_general_catalogue_permissions_are_unchanged(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.member_a)
        self.assertEqual(csrf_client.post(self.url, {
            "seed": SEED_ID, "products": ["Milk"], "action": "import",
        }).status_code, 403)
        self.assertEqual(self.submit(["Milk"], "import").status_code, 302)
        self.assertFalse(self.member_a.family_memberships.filter(
            role=FamilyMembership.Role.ADMIN,
        ).exists())
        # Seed import only ever grants can_add_catalogue (explicitly set
        # for this test class's member_a in setUp); editing an existing
        # product remains admin-only regardless, unaffected by that or
        # any of the six permissions.
        self.assertEqual(self.client.post(
            self.url_a("product_detail", slug=self.product_a.slug), {
                "product_name": "Renamed By Seed Import Member",
                "category": self.category_a.pk,
                "default_shop": self.shop_a.pk,
                "default_quantity": 1, "default_unit": "item",
            },
        ).status_code, 403)
        self.product_a.refresh_from_db()
        self.assertNotEqual(
            self.product_a.product_name, "Renamed By Seed Import Member",
        )

    def test_validation_failure_is_displayed_without_a_success_redirect(self):
        with patch("shopping_list.seed_views.import_seed_products",
                   side_effect=ValidationError("Cannot add these products.")):
            response = self.submit(["Milk"], "import")
        self.assertContains(
            response, "Cannot add these products.", status_code=400,
        )
        self.assertTrue(self.product_inputs(response)["Milk"])

    def test_verified_solo_and_family_setup_redirect_to_the_seed_interface(self):
        from allauth.account.models import EmailAddress
        from django.contrib.auth.models import User

        for route, payload, username in [
            ("solo_registration_request",
             {"name": "New solo", "email": "newsolo@example.com"}, "seed_solo"),
            ("family_registration_request",
             {"admin_name": "New admin", "family_name": "New family",
              "email": "newfamily@example.com"}, "seed_family"),
        ]:
            with self.subTest(route=route):
                self.client.logout()
                response = self.client.post(reverse(route), payload)
                self.assertRedirects(
                    response, reverse("registration_check_email"),
                )
                self.assertFalse(User.objects.filter(username=username).exists())
                token = mail.outbox[-1].body.split("verify/")[1].split("/")[0]
                response = self.client.post(reverse(
                    "registration_complete", kwargs={"token": token},
                ), {
                    "username": username,
                    "password1": "a-very-strong-password-99",
                    "password2": "a-very-strong-password-99",
                    "display_name": "New user",
                })
                user = User.objects.get(username=username)
                membership = FamilyMembership.objects.get(user=user)
                self.assertEqual(membership.role, FamilyMembership.Role.ADMIN)
                self.assertTrue(EmailAddress.objects.get(user=user).verified)
                self.assertRedirects(response, reverse("seed_lists", kwargs={
                    "family_slug": membership.family.slug,
                }))
                self.assertFalse(ListItem.objects.filter(
                    family=membership.family,
                ).exists())
                page = self.client.get(response["Location"])
                self.assertContains(page, "Product catalogue starters")
                self.assertContains(page, "Back to products")
