from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from types import SimpleNamespace

from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator

from family_shopping_list.consumers import ShoppingConsumer
from .forms import CategoryForm, ProductForm, ShopForm
from .views import (
    GROUP_BY_DIMENSIONS,
    _group_label_and_sort_key,
    group_shopping_list_items,
)
from .models import (
    AutomaticAdultGroupMembership,
    Category,
    Family,
    FamilyMembership,
    ListItem,
    Product,
    Shop,
)


def create_family_with_members(name, admin_username, member_usernames=()):
    family = Family.objects.create(name=name)
    admin = User.objects.create_user(username=admin_username, password="pass1234")
    FamilyMembership.objects.create(
        family=family, user=admin, role=FamilyMembership.Role.ADMIN,
        display_name=admin_username,
    )
    members = []
    for username in member_usernames:
        user = User.objects.create_user(username=username, password="pass1234")
        FamilyMembership.objects.create(
            family=family, user=user, role=FamilyMembership.Role.MEMBER,
            display_name=username,
        )
        members.append(user)
    return family, admin, members


def family_url(family, name, **kwargs):
    return reverse(name, kwargs={"family_public_id": family.public_id, **kwargs}) # noqa


def ws_path(family):
    return f"/ws/families/{family.public_id}/shopping-list/"


class TwoFamilyFixtureMixin:
    """Base setup providing two fully populated, independent families."""

    def setUp(self):
        self.family_a, self.admin_a, (self.member_a,) = (
            create_family_with_members("Family A", "admin_a", ["member_a"])
        )
        self.family_b, self.admin_b, (self.member_b,) = (
            create_family_with_members("Family B", "admin_b", ["member_b"])
        )

        self.shop_a = Shop.objects.create(
            shop_name="Shop A", type_of_shop=1,
            family=self.family_a, creator=self.admin_a,
        )
        self.category_a = Category.objects.create(
            category_name="Category A",
            family=self.family_a, creator=self.admin_a,
        )
        self.product_a = Product.objects.create(
            product_name="Product A", family=self.family_a,
            category=self.category_a, default_shop=self.shop_a,
            creator=self.admin_a,
        )
        self.list_item_a = ListItem.objects.create(
            product=self.product_a, family=self.family_a,
            creator=self.admin_a,
        )

        self.shop_b = Shop.objects.create(
            shop_name="Shop B", type_of_shop=1,
            family=self.family_b, creator=self.admin_b,
        )
        self.category_b = Category.objects.create(
            category_name="Category B",
            family=self.family_b, creator=self.admin_b,
        )
        self.product_b = Product.objects.create(
            product_name="Product B", family=self.family_b,
            category=self.category_b, default_shop=self.shop_b,
            creator=self.admin_b,
        )
        self.list_item_b = ListItem.objects.create(
            product=self.product_b, family=self.family_b,
            creator=self.admin_b,
        )

    def url_a(self, name, **kwargs):
        return family_url(self.family_a, name, **kwargs)

    def url_b(self, name, **kwargs):
        return family_url(self.family_b, name, **kwargs)


class TwoFamilyTestCase(TwoFamilyFixtureMixin, TestCase):
    pass


class FamilyIsolationOnReadsTests(TwoFamilyTestCase):
    def test_shopping_list_only_shows_own_family_items(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.get(self.url_a("shopping_list"))

        items = list(response.context["shopping_list"])
        self.assertIn(self.list_item_a, items)
        self.assertNotIn(self.list_item_b, items)

    def test_product_list_only_shows_own_family_products(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.get(self.url_a("product_list"))

        all_products = list(response.context["product_list"])
        self.assertIn(self.product_a, all_products)
        self.assertNotIn(self.product_b, all_products)

    def test_shop_list_only_shows_own_family_shops(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.get(self.url_a("shop_list"))

        shops = list(response.context["shop_list"])
        self.assertIn(self.shop_a, shops)
        self.assertNotIn(self.shop_b, shops)

    def test_category_list_only_shows_own_family_categories(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.get(self.url_a("category_list"))

        categories = list(response.context["category_list"])
        self.assertIn(self.category_a, categories)
        self.assertNotIn(self.category_b, categories)


class ShoppingListVisibilityTests(TwoFamilyTestCase):
    """
    Covers the fix for items disappearing from the shopping list: the
    server must always include cancelled and in-basket items in the
    "shopping_list" context (current, not yet bought); hiding cancelled
    items is a client-side-only display concern with no backend
    involvement, so there's nothing server-side to toggle.
    """

    def test_cancelled_item_still_appears_in_shopping_list_context(self):
        self.list_item_a.cancelled = True
        self.list_item_a.save()

        self.client.login(username="member_a", password="pass1234")
        response = self.client.get(self.url_a("shopping_list"))

        self.assertIn(self.list_item_a, response.context["shopping_list"])

    def test_item_in_basket_still_appears_in_shopping_list_context(self):
        self.list_item_a.in_basket = True
        self.list_item_a.put_in_basket_by = self.member_a
        self.list_item_a.save()

        self.client.login(username="member_a", password="pass1234")
        response = self.client.get(self.url_a("shopping_list"))

        self.assertIn(self.list_item_a, response.context["shopping_list"])

    def test_cancel_action_leaves_item_visible_in_shopping_list_context(self):
        self.client.login(username="member_a", password="pass1234")
        self.client.post(self.url_a("shopping_list"), {
            "item_id": self.list_item_a.id, "action": "cancel",
        })

        response = self.client.get(self.url_a("shopping_list"))

        self.list_item_a.refresh_from_db()
        self.assertTrue(self.list_item_a.cancelled)
        self.assertIn(self.list_item_a, response.context["shopping_list"])

    def test_repeated_page_loads_do_not_change_list_item_fields(self):
        self.list_item_a.cancelled = True
        self.list_item_a.in_basket = True
        self.list_item_a.put_in_basket_by = self.member_a
        self.list_item_a.save()
        before = (
            self.list_item_a.cancelled,
            self.list_item_a.in_basket,
            self.list_item_a.current,
        )

        self.client.login(username="member_a", password="pass1234")
        self.client.get(self.url_a("shopping_list"))
        self.client.get(self.url_a("shopping_list"))

        self.list_item_a.refresh_from_db()
        after = (
            self.list_item_a.cancelled,
            self.list_item_a.in_basket,
            self.list_item_a.current,
        )
        self.assertEqual(before, after)


class CrossFamilyObjectAccessTests(TwoFamilyTestCase):
    def test_cross_family_list_item_returns_404(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.get(
            self.url_a("list_item", slug=self.list_item_b.slug),
        )
        self.assertEqual(response.status_code, 404)

    def test_cross_family_product_returns_404(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.get(
            self.url_a("product_detail", slug=self.product_b.slug),
        )
        self.assertEqual(response.status_code, 404)

    def test_cross_family_shop_returns_404(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.get(
            self.url_a("shop_detail", slug=self.shop_b.slug),
        )
        self.assertEqual(response.status_code, 404)

    def test_cross_family_category_returns_404(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.get(
            self.url_a("category_detail", slug=self.category_b.slug),
        )
        self.assertEqual(response.status_code, 404)

    def test_cross_family_list_item_action_returns_404(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.post(self.url_a("shopping_list"), {
            "item_id": self.list_item_b.id,
            "action": "add_to_basket",
        })
        self.assertEqual(response.status_code, 404)

    def test_supplying_a_nonmember_family_uuid_fails_safely(self):
        # member_a has no membership in family_b at all.
        self.client.login(username="member_a", password="pass1234")
        response = self.client.get(self.url_b("shopping_list"))
        self.assertEqual(response.status_code, 404)


class CreateUpdateFamilyScopingTests(TwoFamilyTestCase):
    def test_cannot_create_product_with_other_familys_category_or_shop(self):
        self.client.login(username="member_a", password="pass1234")
        self.client.post(self.url_a("add_product"), {
            "product_name": "Sneaky Product",
            "category": self.category_b.id,
            "default_shop": self.shop_b.id,
            "default_unit": "pcs",
            "default_quantity": 1,
        })

        self.assertFalse(
            Product.objects.filter(product_name="Sneaky Product").exists(),
        )

    def test_cannot_update_product_to_reference_other_familys_category(self):
        self.client.login(username="admin_a", password="pass1234")
        self.client.post(
            self.url_a("product_detail", slug=self.product_a.slug),
            {
                "product_name": self.product_a.product_name,
                "category": self.category_b.id,
                "default_shop": self.shop_a.id,
                "default_unit": "pcs",
                "default_quantity": 1,
            },
        )

        self.product_a.refresh_from_db()
        self.assertEqual(self.product_a.category, self.category_a)

    def test_cannot_add_other_familys_product_to_own_shopping_list(self):
        self.client.login(username="member_a", password="pass1234")
        self.client.post(self.url_a("add_to_shopping_list"), {
            "selected-products": [self.product_b.id],
        })

        self.assertFalse(
            ListItem.objects.filter(product=self.product_b, family=self.family_a).exists(), # noqa
        )


class CategoryEditTests(TwoFamilyTestCase):
    def test_category_detail_get_renders_editable_form_with_current_values(self): # noqa
        self.client.login(username="member_a", password="pass1234")
        response = self.client.get(
            self.url_a("category_detail", slug=self.category_a.slug),
        )

        self.assertEqual(
            response.context["form"].initial["category_name"],
            "Category A",
        )

    def test_category_edit_saves_changes_and_redirects_to_category_list(self): # noqa
        self.client.login(username="member_a", password="pass1234")
        response = self.client.post(
            self.url_a("category_detail", slug=self.category_a.slug),
            {"category_name": "Renamed Category", "notes": "Some notes"},
        )

        self.assertRedirects(response, self.url_a("category_list"))
        self.category_a.refresh_from_db()
        self.assertEqual(self.category_a.category_name, "Renamed Category")
        self.assertEqual(self.category_a.notes, "Some notes")

    def test_category_edit_rejects_duplicate_name_and_keeps_original(self):
        Category.objects.create(
            category_name="Other Category", family=self.family_a,
            creator=self.admin_a,
        )
        self.client.login(username="member_a", password="pass1234")
        response = self.client.post(
            self.url_a("category_detail", slug=self.category_a.slug),
            {"category_name": "Other Category", "notes": ""},
        )

        self.assertEqual(response.status_code, 200)
        self.category_a.refresh_from_db()
        self.assertEqual(self.category_a.category_name, "Category A")


class InBasketOwnershipTests(TwoFamilyTestCase):
    def test_add_to_basket_records_user_and_timestamp(self):
        self.client.login(username="member_a", password="pass1234")
        before = timezone.now()
        self.client.post(self.url_a("shopping_list"), {
            "item_id": self.list_item_a.id, "action": "add_to_basket",
        })

        self.list_item_a.refresh_from_db()
        self.assertTrue(self.list_item_a.in_basket)
        self.assertEqual(self.list_item_a.put_in_basket_by, self.member_a)
        self.assertIsNotNone(self.list_item_a.time_put_in_basket)
        self.assertGreaterEqual(self.list_item_a.time_put_in_basket, before)

    def test_owner_can_remove_from_basket_and_fields_are_cleared(self):
        self.list_item_a.in_basket = True
        self.list_item_a.put_in_basket_by = self.member_a
        self.list_item_a.time_put_in_basket = timezone.now()
        self.list_item_a.save()

        self.client.login(username="member_a", password="pass1234")
        response = self.client.post(self.url_a("shopping_list"), {
            "item_id": self.list_item_a.id, "action": "remove_from_basket",
        })

        self.assertEqual(response.status_code, 302)
        self.list_item_a.refresh_from_db()
        self.assertFalse(self.list_item_a.in_basket)
        self.assertIsNone(self.list_item_a.put_in_basket_by)
        self.assertIsNone(self.list_item_a.time_put_in_basket)

    def test_another_family_member_cannot_remove_from_basket(self):
        self.list_item_a.in_basket = True
        self.list_item_a.put_in_basket_by = self.admin_a
        self.list_item_a.time_put_in_basket = timezone.now()
        self.list_item_a.save()

        self.client.login(username="member_a", password="pass1234")
        response = self.client.post(self.url_a("shopping_list"), {
            "item_id": self.list_item_a.id, "action": "remove_from_basket",
        })

        self.assertEqual(response.status_code, 403)
        self.list_item_a.refresh_from_db()
        self.assertTrue(self.list_item_a.in_basket)
        self.assertEqual(self.list_item_a.put_in_basket_by, self.admin_a)

    def test_removing_item_that_is_not_in_basket_is_rejected(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.post(self.url_a("shopping_list"), {
            "item_id": self.list_item_a.id, "action": "remove_from_basket",
        })

        self.assertEqual(response.status_code, 403)
        self.list_item_a.refresh_from_db()
        self.assertFalse(self.list_item_a.in_basket)

    def test_unauthenticated_user_cannot_remove_from_basket(self):
        self.list_item_a.in_basket = True
        self.list_item_a.put_in_basket_by = self.member_a
        self.list_item_a.time_put_in_basket = timezone.now()
        self.list_item_a.save()

        response = self.client.post(self.url_a("shopping_list"), {
            "item_id": self.list_item_a.id, "action": "remove_from_basket",
        })

        self.assertEqual(response.status_code, 302)
        self.list_item_a.refresh_from_db()
        self.assertTrue(self.list_item_a.in_basket)
        self.assertEqual(self.list_item_a.put_in_basket_by, self.member_a)

    def test_user_from_another_family_cannot_affect_item(self):
        self.list_item_a.in_basket = True
        self.list_item_a.put_in_basket_by = self.member_a
        self.list_item_a.time_put_in_basket = timezone.now()
        self.list_item_a.save()

        self.client.login(username="member_b", password="pass1234")
        response = self.client.post(self.url_b("shopping_list"), {
            "item_id": self.list_item_a.id, "action": "remove_from_basket",
        })

        self.assertEqual(response.status_code, 404)
        self.list_item_a.refresh_from_db()
        self.assertTrue(self.list_item_a.in_basket)
        self.assertEqual(self.list_item_a.put_in_basket_by, self.member_a)

    def test_buyer_field_is_independent_of_put_in_basket_by(self):
        self.list_item_a.buyer = self.admin_a
        self.list_item_a.save()

        self.client.login(username="member_a", password="pass1234")
        self.client.post(self.url_a("shopping_list"), {
            "item_id": self.list_item_a.id, "action": "add_to_basket",
        })

        self.list_item_a.refresh_from_db()
        self.assertEqual(self.list_item_a.put_in_basket_by, self.member_a)
        # The unrelated future-purchase field is untouched by this action.
        self.assertEqual(self.list_item_a.buyer, self.admin_a)


class CancelledInBasketExclusivityTests(TwoFamilyTestCase):
    """Cancelled and in_basket are mutually exclusive: neither state can
    be entered while the other already applies, and this must hold both
    for the initial page render and for the POST action handlers."""

    # --- Page-load checkbox reconstruction ---

    def test_cancelled_item_renders_with_in_basket_checkbox_disabled(self):
        self.list_item_a.cancelled = True
        self.list_item_a.save()

        self.client.login(username="member_a", password="pass1234")
        response = self.client.get(self.url_a("shopping_list"))
        html = response.content.decode()

        completed_input_start = html.index(
            f'name="completed_items" value="{self.list_item_a.id}"',
        )
        row_end = html.index('</li>', completed_input_start)
        completed_input_markup = html[completed_input_start:row_end]
        self.assertIn('disabled', completed_input_markup)

    def test_in_basket_item_renders_with_cancelled_checkbox_disabled(self):
        self.list_item_a.in_basket = True
        self.list_item_a.put_in_basket_by = self.member_a
        self.list_item_a.save()

        self.client.login(username="member_a", password="pass1234")
        response = self.client.get(self.url_a("shopping_list"))
        html = response.content.decode()

        # The cancel checkbox for this item must be disabled.
        cancel_input_start = html.index(
            f'name="cancelled_item" value="{self.list_item_a.id}"',
        )
        completed_input_start = html.index(
            'name="completed_items"', cancel_input_start,
        )
        cancel_input_markup = html[cancel_input_start:completed_input_start]
        self.assertIn('disabled', cancel_input_markup)

    def test_normal_item_renders_with_both_checkboxes_enabled(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.get(self.url_a("shopping_list"))
        html = response.content.decode()

        cancel_input_start = html.index(
            f'name="cancelled_item" value="{self.list_item_a.id}"',
        )
        completed_input_start = html.index(
            'name="completed_items"', cancel_input_start,
        )
        row_markup = html[cancel_input_start:completed_input_start + 200]
        self.assertNotIn('disabled', row_markup)

    # --- Backend state-transition enforcement ---

    def test_cannot_cancel_an_item_currently_in_a_basket(self):
        self.list_item_a.in_basket = True
        self.list_item_a.put_in_basket_by = self.member_a
        self.list_item_a.save()

        self.client.login(username="member_a", password="pass1234")
        response = self.client.post(self.url_a("shopping_list"), {
            "item_id": self.list_item_a.id, "action": "cancel",
        })

        self.assertEqual(response.status_code, 400)
        self.list_item_a.refresh_from_db()
        self.assertFalse(self.list_item_a.cancelled)
        self.assertTrue(self.list_item_a.in_basket)

    def test_cannot_add_a_cancelled_item_to_the_basket(self):
        self.list_item_a.cancelled = True
        self.list_item_a.save()

        self.client.login(username="member_a", password="pass1234")
        response = self.client.post(self.url_a("shopping_list"), {
            "item_id": self.list_item_a.id, "action": "add_to_basket",
        })

        self.assertEqual(response.status_code, 400)
        self.list_item_a.refresh_from_db()
        self.assertFalse(self.list_item_a.in_basket)
        self.assertTrue(self.list_item_a.cancelled)

    def test_cancelling_a_normal_item_still_works(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.post(self.url_a("shopping_list"), {
            "item_id": self.list_item_a.id, "action": "cancel",
        })

        self.assertEqual(response.status_code, 302)
        self.list_item_a.refresh_from_db()
        self.assertTrue(self.list_item_a.cancelled)

    def test_adding_a_normal_item_to_the_basket_still_works(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.post(self.url_a("shopping_list"), {
            "item_id": self.list_item_a.id, "action": "add_to_basket",
        })

        self.assertEqual(response.status_code, 302)
        self.list_item_a.refresh_from_db()
        self.assertTrue(self.list_item_a.in_basket)

    def test_uncancelling_then_adding_to_basket_is_allowed(self):
        self.list_item_a.cancelled = True
        self.list_item_a.save()

        self.client.login(username="member_a", password="pass1234")
        self.client.post(self.url_a("shopping_list"), {
            "item_id": self.list_item_a.id, "action": "uncancel",
        })
        response = self.client.post(self.url_a("shopping_list"), {
            "item_id": self.list_item_a.id, "action": "add_to_basket",
        })

        self.assertEqual(response.status_code, 302)
        self.list_item_a.refresh_from_db()
        self.assertTrue(self.list_item_a.in_basket)
        self.assertFalse(self.list_item_a.cancelled)

    def test_removing_from_basket_then_cancelling_is_allowed(self):
        self.list_item_a.in_basket = True
        self.list_item_a.put_in_basket_by = self.member_a
        self.list_item_a.save()

        self.client.login(username="member_a", password="pass1234")
        self.client.post(self.url_a("shopping_list"), {
            "item_id": self.list_item_a.id, "action": "remove_from_basket",
        })
        response = self.client.post(self.url_a("shopping_list"), {
            "item_id": self.list_item_a.id, "action": "cancel",
        })

        self.assertEqual(response.status_code, 302)
        self.list_item_a.refresh_from_db()
        self.assertTrue(self.list_item_a.cancelled)
        self.assertFalse(self.list_item_a.in_basket)


class BuyBasketTests(TwoFamilyTestCase):
    def _put_in_basket(self, item, user):
        item.in_basket = True
        item.put_in_basket_by = user
        item.time_put_in_basket = timezone.now()
        item.save()

    def test_buy_basket_marks_own_basket_items_as_bought_and_empties_basket(self): # noqa
        self.list_item_a.quantity_required = 3
        self._put_in_basket(self.list_item_a, self.member_a)

        self.client.login(username="member_a", password="pass1234")
        before = timezone.now()
        response = self.client.post(
            self.url_a("shopping_list"), {"action": "buy_basket"},
        )

        self.assertEqual(response.status_code, 302)
        self.list_item_a.refresh_from_db()
        self.assertEqual(self.list_item_a.buyer, self.member_a)
        self.assertIsNotNone(self.list_item_a.date_bought)
        self.assertGreaterEqual(self.list_item_a.date_bought, before)
        self.assertEqual(self.list_item_a.quantity_bought, 3)
        self.assertEqual(self.list_item_a.shop_bought, self.list_item_a.preferred_shop) # noqa
        # The basket is emptied, but the item remains on the current list.
        self.assertFalse(self.list_item_a.in_basket)
        self.assertIsNone(self.list_item_a.put_in_basket_by)
        self.assertIsNone(self.list_item_a.time_put_in_basket)
        self.assertTrue(self.list_item_a.current)

    def test_buy_basket_does_not_affect_other_users_basket_items(self):
        self._put_in_basket(self.list_item_a, self.admin_a)

        self.client.login(username="member_a", password="pass1234")
        self.client.post(self.url_a("shopping_list"), {"action": "buy_basket"}) # noqa

        self.list_item_a.refresh_from_db()
        self.assertTrue(self.list_item_a.in_basket)
        self.assertEqual(self.list_item_a.put_in_basket_by, self.admin_a)
        self.assertIsNone(self.list_item_a.buyer)
        self.assertIsNone(self.list_item_a.date_bought)

    def test_buy_basket_does_not_affect_items_not_in_basket(self):
        self.client.login(username="member_a", password="pass1234")
        self.client.post(self.url_a("shopping_list"), {"action": "buy_basket"}) # noqa

        self.list_item_a.refresh_from_db()
        self.assertIsNone(self.list_item_a.buyer)
        self.assertIsNone(self.list_item_a.date_bought)

    def test_buy_basket_is_scoped_to_requesting_users_family(self):
        self._put_in_basket(self.list_item_b, self.member_b)

        self.client.login(username="member_a", password="pass1234")
        self.client.post(self.url_a("shopping_list"), {"action": "buy_basket"}) # noqa

        self.list_item_b.refresh_from_db()
        self.assertTrue(self.list_item_b.in_basket)
        self.assertIsNone(self.list_item_b.buyer)


class BoughtSectionDisplayTests(TwoFamilyTestCase):
    def test_bought_item_moves_from_shopping_list_to_bought_items_context(self): # noqa
        self.list_item_a.in_basket = True
        self.list_item_a.put_in_basket_by = self.member_a
        self.list_item_a.save()
        self.client.login(username="member_a", password="pass1234")
        self.client.post(self.url_a("shopping_list"), {"action": "buy_basket"}) # noqa

        response = self.client.get(self.url_a("shopping_list"))

        self.list_item_a.refresh_from_db()
        self.assertNotIn(self.list_item_a, response.context["shopping_list"])
        self.assertIn(self.list_item_a, response.context["bought_items"])

    def test_unpurchased_item_is_not_in_bought_items_context(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.get(self.url_a("shopping_list"))

        self.assertIn(self.list_item_a, response.context["shopping_list"])
        self.assertNotIn(self.list_item_a, response.context["bought_items"])


class GroupingHelperTests(TestCase):
    """Unit tests for the pure grouping helpers, using lightweight stand-ins
    instead of real ListItems, since Product.category and preferred_shop
    can't both be exercised as missing through the ORM alone (category is a
    required field; only preferred_shop can genuinely be null)."""

    @staticmethod
    def _item(category_name=None, shop_name=None):
        category = SimpleNamespace(category_name=category_name) if category_name else None # noqa
        shop = SimpleNamespace(shop_name=shop_name) if shop_name else None
        return SimpleNamespace(
            product=SimpleNamespace(category=category),
            preferred_shop=shop,
        )

    def test_missing_category_is_labelled_uncategorised_and_sorts_first(self): # noqa
        label, sort_key = _group_label_and_sort_key(self._item(), 'category')
        self.assertEqual(label, "Uncategorised")

        _, named_sort_key = _group_label_and_sort_key(
            self._item(category_name="Dairy"), 'category',
        )
        self.assertLess(sort_key, named_sort_key)

    def test_missing_shop_is_labelled_and_sorts_first(self):
        label, sort_key = _group_label_and_sort_key(self._item(), 'shop')
        self.assertEqual(label, "No shop set")

        _, named_sort_key = _group_label_and_sort_key(
            self._item(shop_name="Aldi"), 'shop',
        )
        self.assertLess(sort_key, named_sort_key)

    def test_group_shopping_list_items_builds_subgroups_for_combined_dimensions(self): # noqa
        items = [
            self._item(category_name="Dairy", shop_name="Aldi"),
            self._item(category_name="Dairy", shop_name="Rewe"),
        ]
        groups = group_shopping_list_items(items, 'category_shop')

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['label'], "Dairy")
        subgroup_labels = [label for label, _ in groups[0]['subgroups']]
        self.assertEqual(subgroup_labels, ["Aldi", "Rewe"])


class ShoppingListGroupingTests(TwoFamilyTestCase):
    def setUp(self):
        super().setUp()
        self.dairy = Category.objects.create(
            category_name="Dairy", family=self.family_a, creator=self.admin_a, # noqa
        )
        self.fruit_veg = Category.objects.create(
            category_name="Fruit & Vegetables", family=self.family_a,
            creator=self.admin_a,
        )
        self.aldi = Shop.objects.create(
            shop_name="Aldi", type_of_shop=1,
            family=self.family_a, creator=self.admin_a,
        )

        self.butter = Product.objects.create(
            product_name="Butter", family=self.family_a,
            category=self.dairy, default_shop=self.shop_a,
            creator=self.admin_a,
        )
        self.apples = Product.objects.create(
            product_name="Apples", family=self.family_a,
            category=self.fruit_veg, default_shop=self.aldi,
            creator=self.admin_a,
        )
        self.butter_item = ListItem.objects.create(
            product=self.butter, family=self.family_a, creator=self.admin_a,
        )
        self.apples_item = ListItem.objects.create(
            product=self.apples, family=self.family_a, creator=self.admin_a,
        )
        # self.list_item_a (Product A / Category A / Shop A) also exists
        # from the base fixture.

    def test_default_is_no_grouping(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.get(self.url_a("shopping_list"))

        self.assertEqual(response.context["group_by"], "none")
        self.assertIsNone(response.context["grouped_shopping_list"])
        self.assertIn(self.butter_item, response.context["shopping_list"])
        self.assertIn(self.apples_item, response.context["shopping_list"])

    def test_grouping_by_category(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.get(
            self.url_a("shopping_list"), {"group_by": "category"},
        )

        labels = [g['label'] for g in response.context["grouped_shopping_list"]] # noqa
        self.assertEqual(
            labels, ["Category A", "Dairy", "Fruit & Vegetables"],
        )
        for group in response.context["grouped_shopping_list"]:
            self.assertIsNone(group['subgroups'])

    def test_grouping_by_shop(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.get(
            self.url_a("shopping_list"), {"group_by": "shop"},
        )

        labels = [g['label'] for g in response.context["grouped_shopping_list"]] # noqa
        self.assertEqual(labels, ["Aldi", "Shop A"])

    def test_grouping_by_category_then_shop_has_subgroups(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.get(
            self.url_a("shopping_list"), {"group_by": "category_shop"},
        )

        groups = {
            g['label']: g for g in response.context["grouped_shopping_list"]
        }
        fruit_veg_subgroups = dict(groups["Fruit & Vegetables"]['subgroups'])
        self.assertIn(self.apples_item, fruit_veg_subgroups["Aldi"])

    def test_grouping_by_shop_then_category_has_subgroups(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.get(
            self.url_a("shopping_list"), {"group_by": "shop_category"},
        )

        groups = {
            g['label']: g for g in response.context["grouped_shopping_list"]
        }
        aldi_subgroups = dict(groups["Aldi"]['subgroups'])
        self.assertIn(self.apples_item, aldi_subgroups["Fruit & Vegetables"])

    def test_invalid_group_by_falls_back_to_no_grouping(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.get(
            self.url_a("shopping_list"), {"group_by": "nonsense"},
        )

        self.assertEqual(response.context["group_by"], "none")
        self.assertIsNone(response.context["grouped_shopping_list"])

    def test_group_by_choice_persists_in_session_across_requests(self):
        self.client.login(username="admin_a", password="pass1234")
        self.client.get(self.url_a("shopping_list"), {"group_by": "shop"})

        # No group_by in this request: should still come from the session.
        response = self.client.get(self.url_a("shopping_list"))

        self.assertEqual(response.context["group_by"], "shop")
        self.assertIsNotNone(response.context["grouped_shopping_list"])

    def test_choosing_no_grouping_again_overwrites_session_choice(self):
        self.client.login(username="admin_a", password="pass1234")
        self.client.get(self.url_a("shopping_list"), {"group_by": "category"}) # noqa
        self.client.get(self.url_a("shopping_list"), {"group_by": "none"})

        response = self.client.get(self.url_a("shopping_list"))

        self.assertEqual(response.context["group_by"], "none")
        self.assertIsNone(response.context["grouped_shopping_list"])

    def test_item_with_no_preferred_shop_appears_in_no_shop_group_first(self): # noqa
        # ListItem.save() auto-fills preferred_shop from the product's
        # default shop, so simulate the real-world way it can end up
        # null: a bare DB update, as Shop.delete() (SET_NULL) would do.
        ListItem.objects.filter(pk=self.butter_item.pk).update(
            preferred_shop=None,
        )

        self.client.login(username="admin_a", password="pass1234")
        response = self.client.get(
            self.url_a("shopping_list"), {"group_by": "shop"},
        )

        groups = response.context["grouped_shopping_list"]
        self.assertEqual(groups[0]['label'], "No shop set")
        self.assertIn(self.butter_item, groups[0]['items'])


class ShoppingListFilterTests(TwoFamilyTestCase):
    def setUp(self):
        super().setUp()
        self.dairy = Category.objects.create(
            category_name="Dairy", family=self.family_a, creator=self.admin_a, # noqa
        )
        self.fruit_veg = Category.objects.create(
            category_name="Fruit & Vegetables", family=self.family_a,
            creator=self.admin_a,
        )
        self.aldi = Shop.objects.create(
            shop_name="Aldi", type_of_shop=1,
            family=self.family_a, creator=self.admin_a,
        )
        self.rewe = Shop.objects.create(
            shop_name="Rewe", type_of_shop=1,
            family=self.family_a, creator=self.admin_a,
        )
        self.butter = Product.objects.create(
            product_name="Butter", family=self.family_a,
            category=self.dairy, default_shop=self.aldi,
            creator=self.admin_a,
        )
        self.apples = Product.objects.create(
            product_name="Apples", family=self.family_a,
            category=self.fruit_veg, default_shop=self.rewe,
            creator=self.admin_a,
        )
        self.carrots = Product.objects.create(
            product_name="Carrots", family=self.family_a,
            category=self.fruit_veg, default_shop=self.aldi,
            creator=self.admin_a,
        )
        self.butter_item = ListItem.objects.create(
            product=self.butter, family=self.family_a, creator=self.admin_a,
        )
        self.apples_item = ListItem.objects.create(
            product=self.apples, family=self.family_a, creator=self.admin_a,
        )
        self.carrots_item = ListItem.objects.create(
            product=self.carrots, family=self.family_a, creator=self.admin_a,
        )
        # self.list_item_a (Product A / Category A / Shop A) also exists
        # from the base fixture, and matches none of the above.

    def test_default_is_no_filter(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.get(self.url_a("shopping_list"))

        self.assertEqual(
            response.context["filter_state"],
            {"categories": [], "shops": [], "mode": "include"},
        )
        items = response.context["shopping_list"]
        self.assertIn(self.list_item_a, items)
        self.assertIn(self.butter_item, items)
        self.assertIn(self.apples_item, items)
        self.assertIn(self.carrots_item, items)

    def test_filter_by_single_category(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.get(self.url_a("shopping_list"), {
            "filter_applied": "1", "filter_categories": [self.dairy.id],
        })

        self.assertEqual(
            list(response.context["shopping_list"]), [self.butter_item],
        )

    def test_filter_by_multiple_categories_is_or_within_facet(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.get(self.url_a("shopping_list"), {
            "filter_applied": "1",
            "filter_categories": [self.dairy.id, self.fruit_veg.id],
        })

        items = set(response.context["shopping_list"])
        self.assertEqual(
            items, {self.butter_item, self.apples_item, self.carrots_item},
        )

    def test_filter_by_shop(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.get(self.url_a("shopping_list"), {
            "filter_applied": "1", "filter_shops": [self.aldi.id],
        })

        items = set(response.context["shopping_list"])
        self.assertEqual(items, {self.butter_item, self.carrots_item})

    def test_filter_by_category_and_shop_is_and_across_facets(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.get(self.url_a("shopping_list"), {
            "filter_applied": "1",
            "filter_categories": [self.fruit_veg.id],
            "filter_shops": [self.aldi.id],
        })

        # Fruit & Vegetables AND Aldi: only carrots qualifies; apples is
        # Fruit & Veg but at Rewe, butter is at Aldi but Dairy.
        self.assertEqual(
            list(response.context["shopping_list"]), [self.carrots_item],
        )

    def test_exclude_mode_hides_selected_category(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.get(self.url_a("shopping_list"), {
            "filter_applied": "1", "filter_categories": [self.dairy.id],
            "filter_mode": "exclude",
        })

        items = set(response.context["shopping_list"])
        self.assertEqual(
            items, {self.list_item_a, self.apples_item, self.carrots_item},
        )

    def test_exclude_mode_combines_category_and_shop_as_union(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.get(self.url_a("shopping_list"), {
            "filter_applied": "1",
            "filter_categories": [self.dairy.id],
            "filter_shops": [self.rewe.id],
            "filter_mode": "exclude",
        })

        # Hides butter (Dairy) and apples (Rewe); carrots and list_item_a
        # match neither and remain.
        items = set(response.context["shopping_list"])
        self.assertEqual(items, {self.list_item_a, self.carrots_item})

    def test_invalid_mode_falls_back_to_include(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.get(self.url_a("shopping_list"), {
            "filter_applied": "1", "filter_mode": "nonsense",
        })

        self.assertEqual(response.context["filter_state"]["mode"], "include")

    def test_another_familys_ids_are_ignored(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.get(self.url_a("shopping_list"), {
            "filter_applied": "1",
            "filter_categories": [self.category_b.id],
            "filter_shops": [self.shop_b.id],
        })

        self.assertEqual(
            response.context["filter_state"],
            {"categories": [], "shops": [], "mode": "include"},
        )
        self.assertIn(self.list_item_a, response.context["shopping_list"])

    def test_filter_choice_persists_in_session_across_requests(self):
        self.client.login(username="admin_a", password="pass1234")
        self.client.get(self.url_a("shopping_list"), {
            "filter_applied": "1", "filter_categories": [self.dairy.id],
        })

        response = self.client.get(self.url_a("shopping_list"))

        self.assertEqual(
            response.context["filter_state"]["categories"], [self.dairy.id],
        )
        self.assertEqual(
            list(response.context["shopping_list"]), [self.butter_item],
        )

    def test_removing_filter_resets_to_no_filter(self):
        self.client.login(username="admin_a", password="pass1234")
        self.client.get(self.url_a("shopping_list"), {
            "filter_applied": "1", "filter_categories": [self.dairy.id],
        })
        self.client.get(self.url_a("shopping_list"), {"filter_applied": "1"})

        response = self.client.get(self.url_a("shopping_list"))

        self.assertEqual(
            response.context["filter_state"],
            {"categories": [], "shops": [], "mode": "include"},
        )
        self.assertIn(self.list_item_a, response.context["shopping_list"])

    def test_filter_combines_with_grouping(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.get(self.url_a("shopping_list"), {
            "filter_applied": "1",
            "filter_categories": [self.dairy.id],
            "group_by": "shop",
        })

        groups = response.context["grouped_shopping_list"]
        labels = [g['label'] for g in groups]
        self.assertEqual(labels, ["Aldi"])
        self.assertIn(self.butter_item, groups[0]['items'])


class ProductListGroupingAndFilterTests(TwoFamilyTestCase):
    def setUp(self):
        super().setUp()
        self.dairy = Category.objects.create(
            category_name="Dairy", family=self.family_a, creator=self.admin_a, # noqa
        )
        self.aldi = Shop.objects.create(
            shop_name="Aldi", type_of_shop=1,
            family=self.family_a, creator=self.admin_a,
        )
        self.butter = Product.objects.create(
            product_name="Butter", family=self.family_a,
            category=self.dairy, default_shop=self.aldi,
            creator=self.admin_a,
        )
        # self.product_a (Category A / Shop A) also exists from the base
        # fixture.

    def test_default_group_by_is_category(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.get(self.url_a("product_list"))

        self.assertEqual(response.context["group_by"], "category")
        labels = [g['label'] for g in response.context["grouped_products"]]
        self.assertEqual(labels, ["Category A", "Dairy"])

    def test_group_by_none_gives_a_flat_list(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.get(
            self.url_a("product_list"), {"group_by": "none"},
        )

        self.assertIsNone(response.context["grouped_products"])
        self.assertIn(self.butter, response.context["product_list"])
        self.assertIn(self.product_a, response.context["product_list"])

    def test_default_filter_is_off(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.get(self.url_a("product_list"))

        self.assertEqual(
            response.context["filter_state"],
            {"categories": [], "shops": [], "mode": "include"},
        )
        self.assertIn(self.butter, response.context["product_list"])
        self.assertIn(self.product_a, response.context["product_list"])

    def test_filter_by_category(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.get(self.url_a("product_list"), {
            "filter_applied": "1", "filter_categories": [self.dairy.id],
        })

        self.assertEqual(
            list(response.context["product_list"]), [self.butter],
        )

    def test_filter_by_shop_exclude_mode(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.get(self.url_a("product_list"), {
            "filter_applied": "1", "filter_shops": [self.aldi.id],
            "filter_mode": "exclude",
        })

        products = list(response.context["product_list"])
        self.assertNotIn(self.butter, products)
        self.assertIn(self.product_a, products)

    def test_filter_and_group_by_persist_independently_in_session(self):
        self.client.login(username="admin_a", password="pass1234")
        self.client.get(self.url_a("product_list"), {
            "group_by": "shop",
            "filter_applied": "1", "filter_categories": [self.dairy.id],
        })

        response = self.client.get(self.url_a("product_list"))

        self.assertEqual(response.context["group_by"], "shop")
        self.assertEqual(
            response.context["filter_state"]["categories"], [self.dairy.id],
        )

    def test_another_familys_category_id_is_ignored(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.get(self.url_a("product_list"), {
            "filter_applied": "1",
            "filter_categories": [self.category_b.id],
        })

        self.assertEqual(
            response.context["filter_state"],
            {"categories": [], "shops": [], "mode": "include"},
        )

    def test_product_list_grouping_and_filtering_are_independent_of_shopping_list(self): # noqa
        self.client.login(username="admin_a", password="pass1234")
        self.client.get(
            self.url_a("shopping_list"), {"group_by": "shop"},
        )

        response = self.client.get(self.url_a("product_list"))

        # The shopping list's own group_by choice must not bleed into the
        # (independently-defaulted) product list.
        self.assertEqual(response.context["group_by"], "category")


class ProductDeleteTests(TwoFamilyTestCase):
    def test_get_shows_confirmation_page(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.get(
            self.url_a("product_delete", slug=self.product_a.slug),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["product"], self.product_a)

    def test_adult_can_deactivate_product(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.post(
            self.url_a("product_delete", slug=self.product_a.slug),
        )

        self.assertRedirects(response, self.url_a("product_list"))
        self.product_a.refresh_from_db()
        self.assertFalse(self.product_a.current)

    def test_non_adult_cannot_deactivate_product(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.post(
            self.url_a("product_delete", slug=self.product_a.slug),
        )

        self.assertEqual(response.status_code, 403)
        self.product_a.refresh_from_db()
        self.assertTrue(self.product_a.current)

    def test_cross_family_product_delete_returns_404(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.post(
            self.url_a("product_delete", slug=self.product_b.slug),
        )

        self.assertEqual(response.status_code, 404)


class ShopDeleteTests(TwoFamilyTestCase):
    def setUp(self):
        super().setUp()
        self.other_shop_a = Shop.objects.create(
            shop_name="Other Shop A", type_of_shop=1,
            family=self.family_a, creator=self.admin_a,
        )

    def test_get_with_no_dependent_products_shows_plain_confirmation(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.get(
            self.url_a("shop_delete", slug=self.other_shop_a.slug),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["affected_products"]), [])

    def test_get_lists_products_using_this_as_default_shop(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.get(
            self.url_a("shop_delete", slug=self.shop_a.slug),
        )

        self.assertIn(self.product_a, response.context["affected_products"])

    def test_deleting_shop_with_no_dependents_just_deactivates_it(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.post(
            self.url_a("shop_delete", slug=self.other_shop_a.slug),
        )

        self.assertRedirects(response, self.url_a("shop_list"))
        self.other_shop_a.refresh_from_db()
        self.assertFalse(self.other_shop_a.current)

    def test_reassigning_dependent_products_to_another_shop(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.post(
            self.url_a("shop_delete", slug=self.shop_a.slug),
            {f"reassign_shop_{self.product_a.id}": self.other_shop_a.id},
        )

        self.assertRedirects(response, self.url_a("shop_list"))
        self.product_a.refresh_from_db()
        self.assertEqual(self.product_a.default_shop, self.other_shop_a)
        self.shop_a.refresh_from_db()
        self.assertFalse(self.shop_a.current)

    def test_leaving_dependent_products_with_no_default_shop(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.post(
            self.url_a("shop_delete", slug=self.shop_a.slug),
            {f"reassign_shop_{self.product_a.id}": ""},
        )

        self.assertRedirects(response, self.url_a("shop_list"))
        self.product_a.refresh_from_db()
        self.assertIsNone(self.product_a.default_shop)

    def test_cannot_reassign_dependent_product_to_another_familys_shop(self):
        self.client.login(username="admin_a", password="pass1234")
        self.client.post(
            self.url_a("shop_delete", slug=self.shop_a.slug),
            {f"reassign_shop_{self.product_a.id}": self.shop_b.id},
        )

        self.product_a.refresh_from_db()
        self.assertIsNone(self.product_a.default_shop)

    def test_non_adult_cannot_delete_shop(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.post(
            self.url_a("shop_delete", slug=self.other_shop_a.slug),
        )

        self.assertEqual(response.status_code, 403)
        self.other_shop_a.refresh_from_db()
        self.assertTrue(self.other_shop_a.current)

    def test_cross_family_shop_delete_returns_404(self):
        self.client.login(username="admin_a", password="pass1234")
        response = self.client.get(
            self.url_a("shop_delete", slug=self.shop_b.slug),
        )

        self.assertEqual(response.status_code, 404)


class CategoryDeleteTests(TwoFamilyTestCase):
    def setUp(self):
        super().setUp()
        self.other_category_a = Category.objects.create(
            category_name="Other Category A", family=self.family_a,
            creator=self.admin_a,
        )

    def test_get_with_no_dependent_products_shows_plain_confirmation(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.get(
            self.url_a("category_delete", slug=self.other_category_a.slug),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["affected_products"]), [])

    def test_get_lists_products_using_this_category(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.get(
            self.url_a("category_delete", slug=self.category_a.slug),
        )

        self.assertIn(self.product_a, response.context["affected_products"])

    def test_deleting_category_with_no_dependents_just_deactivates_it(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.post(
            self.url_a("category_delete", slug=self.other_category_a.slug),
        )

        self.assertRedirects(response, self.url_a("category_list"))
        self.other_category_a.refresh_from_db()
        self.assertFalse(self.other_category_a.current)

    def test_reassigning_dependent_products_to_another_category(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.post(
            self.url_a("category_delete", slug=self.category_a.slug),
            {
                f"reassign_category_{self.product_a.id}":
                    self.other_category_a.id,
            },
        )

        self.assertRedirects(response, self.url_a("category_list"))
        self.product_a.refresh_from_db()
        self.assertEqual(self.product_a.category, self.other_category_a)
        self.category_a.refresh_from_db()
        self.assertFalse(self.category_a.current)

    def test_leaving_dependent_products_with_no_category(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.post(
            self.url_a("category_delete", slug=self.category_a.slug),
            {f"reassign_category_{self.product_a.id}": ""},
        )

        self.assertRedirects(response, self.url_a("category_list"))
        self.product_a.refresh_from_db()
        self.assertIsNone(self.product_a.category)

    def test_cannot_reassign_dependent_product_to_another_familys_category(self): # noqa
        self.client.login(username="member_a", password="pass1234")
        self.client.post(
            self.url_a("category_delete", slug=self.category_a.slug),
            {f"reassign_category_{self.product_a.id}": self.category_b.id},
        )

        self.product_a.refresh_from_db()
        self.assertIsNone(self.product_a.category)

    def test_cross_family_category_delete_returns_404(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.get(
            self.url_a("category_delete", slug=self.category_b.slug),
        )

        self.assertEqual(response.status_code, 404)


class ListItemFamilyValidationTests(TwoFamilyTestCase):
    def test_cannot_create_list_item_with_product_from_another_family(self):
        with self.assertRaises(ValidationError):
            ListItem.objects.create(
                product=self.product_b, family=self.family_a,
                creator=self.admin_a,
            )

    def test_cannot_create_list_item_with_preferred_shop_from_another_family(self): # noqa
        with self.assertRaises(ValidationError):
            ListItem.objects.create(
                product=self.product_a, family=self.family_a,
                creator=self.admin_a, preferred_shop=self.shop_b,
            )

    def test_cannot_create_list_item_with_shop_bought_from_another_family(self):
        with self.assertRaises(ValidationError):
            ListItem.objects.create(
                product=self.product_a, family=self.family_a,
                creator=self.admin_a, shop_bought=self.shop_b,
            )


class FormFamilyAssignmentTests(TwoFamilyTestCase):
    def test_product_form_sets_instance_family_before_validation(self):
        form = ProductForm(family=self.family_a)
        self.assertEqual(form.instance.family, self.family_a)

    def test_shop_form_sets_instance_family_before_validation(self):
        form = ShopForm(family=self.family_a)
        self.assertEqual(form.instance.family, self.family_a)

    def test_category_form_sets_instance_family_before_validation(self):
        form = CategoryForm(family=self.family_a)
        self.assertEqual(form.instance.family, self.family_a)


class FamilyMembershipUniquenessTests(TestCase):
    def test_user_can_have_memberships_in_two_families(self):
        family_one = Family.objects.create(name="Family One")
        family_two = Family.objects.create(name="Family Two")
        user = User.objects.create_user(username="multi_member", password="pass1234") # noqa

        FamilyMembership.objects.create(
            family=family_one, user=user, display_name="Multi",
        )
        FamilyMembership.objects.create(
            family=family_two, user=user, display_name="Multi Two",
        )

        self.assertEqual(
            FamilyMembership.objects.filter(user=user).count(), 2,
        )

    def test_duplicate_membership_in_same_family_is_rejected(self):
        family = Family.objects.create(name="Family One")
        user = User.objects.create_user(username="dup_member", password="pass1234") # noqa
        FamilyMembership.objects.create(
            family=family, user=user, display_name="Dup",
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                FamilyMembership.objects.create(
                    family=family, user=user, display_name="Dup Again",
                )

    def test_same_display_name_allowed_in_different_families(self):
        family_one = Family.objects.create(name="Family One")
        family_two = Family.objects.create(name="Family Two")
        user_one = User.objects.create_user(username="name_user_one", password="pass1234") # noqa
        user_two = User.objects.create_user(username="name_user_two", password="pass1234") # noqa

        FamilyMembership.objects.create(
            family=family_one, user=user_one, display_name="Sam",
        )
        FamilyMembership.objects.create(
            family=family_two, user=user_two, display_name="Sam",
        )

        self.assertEqual(
            FamilyMembership.objects.filter(display_name="Sam").count(), 2,
        )

    def test_case_insensitive_duplicate_display_name_in_one_family_is_rejected(self): # noqa
        family = Family.objects.create(name="Family One")
        user_one = User.objects.create_user(username="ci_user_one", password="pass1234") # noqa
        user_two = User.objects.create_user(username="ci_user_two", password="pass1234") # noqa
        FamilyMembership.objects.create(
            family=family, user=user_one, display_name="Hannah",
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                FamilyMembership.objects.create(
                    family=family, user=user_two, display_name="HANNAH",
                )

    def test_is_family_admin_property(self):
        family = Family.objects.create(name="Family One")
        user = User.objects.create_user(username="role_user", password="pass1234") # noqa
        admin_membership = FamilyMembership.objects.create(
            family=family, user=user, display_name="Admin",
            role=FamilyMembership.Role.ADMIN,
        )
        self.assertTrue(admin_membership.is_family_admin)

        member_family = Family.objects.create(name="Family Two")
        member_membership = FamilyMembership.objects.create(
            family=member_family, user=user, display_name="Member",
            role=FamilyMembership.Role.MEMBER,
        )
        self.assertFalse(member_membership.is_family_admin)


class DisplayNameMigrationBackfillTests(TestCase):
    def test_existing_memberships_can_be_created_with_username_based_display_name(self): # noqa
        # Exercises the same shape the 0030 data migration relies on:
        # display_name populated from user.username for pre-existing rows.
        family = Family.objects.create(name="Legacy Family")
        user = User.objects.create_user(username="legacy_user", password="pass1234") # noqa
        membership = FamilyMembership.objects.create(
            family=family, user=user, display_name=user.username,
        )
        self.assertEqual(membership.display_name, "legacy_user")


class SuperuserWithoutMembershipTests(TestCase):
    def test_superuser_without_membership_gets_no_family_data(self):
        User.objects.create_superuser(
            username="root", password="pass1234", email="root@example.com",
        )
        self.client.login(username="root", password="pass1234")

        response = self.client.get(reverse("logged_out_homepage"))

        self.assertEqual(response.status_code, 403)


class AdultGroupLifecycleTests(TestCase):
    def setUp(self):
        self.adult_group, _ = Group.objects.get_or_create(name="adult")
        self.family = Family.objects.create(name="Adult Group Family")

    def test_new_superuser_is_added_to_adult_group(self):
        user = User.objects.create_superuser(
            username="new_root", password="pass1234",
        )

        self.assertTrue(user.groups.filter(pk=self.adult_group.pk).exists())
        self.assertTrue(
            AutomaticAdultGroupMembership.objects.filter(user=user).exists(),
        )

    def test_promoted_superuser_is_added_to_adult_group(self):
        user = User.objects.create_user(
            username="promoted_root", password="pass1234",
        )
        user.is_superuser = True
        user.save(update_fields=["is_superuser"])

        self.assertTrue(user.groups.filter(pk=self.adult_group.pk).exists())

    def test_family_admin_is_added_to_adult_group(self):
        user = User.objects.create_user(
            username="family_admin", password="pass1234",
        )
        FamilyMembership.objects.create(
            family=self.family,
            user=user,
            role=FamilyMembership.Role.ADMIN,
            display_name="Family Admin",
        )

        self.assertTrue(user.groups.filter(pk=self.adult_group.pk).exists())

    def test_family_admin_promotion_and_demotion_are_synchronised(self):
        user = User.objects.create_user(
            username="role_change", password="pass1234",
        )
        membership = FamilyMembership.objects.create(
            family=self.family,
            user=user,
            display_name="Role Change",
        )
        self.assertFalse(user.groups.filter(pk=self.adult_group.pk).exists())

        membership.role = FamilyMembership.Role.ADMIN
        membership.save(update_fields=["role"])
        self.assertTrue(user.groups.filter(pk=self.adult_group.pk).exists())

        membership.role = FamilyMembership.Role.MEMBER
        membership.save(update_fields=["role"])
        self.assertFalse(user.groups.filter(pk=self.adult_group.pk).exists())

    def test_demoting_admin_preserves_preexisting_adult_membership(self):
        user = User.objects.create_user(
            username="existing_adult", password="pass1234",
        )
        user.groups.add(self.adult_group)
        membership = FamilyMembership.objects.create(
            family=self.family,
            user=user,
            role=FamilyMembership.Role.ADMIN,
            display_name="Existing Adult",
        )

        membership.role = FamilyMembership.Role.MEMBER
        membership.save(update_fields=["role"])

        self.assertTrue(user.groups.filter(pk=self.adult_group.pk).exists())
        self.assertFalse(
            AutomaticAdultGroupMembership.objects.filter(user=user).exists(),
        )

    def test_superuser_remains_adult_after_admin_membership_is_removed(self):
        user = User.objects.create_superuser(
            username="super_admin", password="pass1234",
        )
        membership = FamilyMembership.objects.create(
            family=self.family,
            user=user,
            role=FamilyMembership.Role.ADMIN,
            display_name="Super Admin",
        )

        membership.delete()

        self.assertTrue(user.groups.filter(pk=self.adult_group.pk).exists())

    def test_non_adult_cannot_add_or_edit_products_or_shops(self):
        user = User.objects.create_user(
            username="ordinary_member", password="pass1234",
        )
        category = Category.objects.create(
            category_name="Existing Category",
            family=self.family,
            creator=user,
        )
        shop = Shop.objects.create(
            shop_name="Existing Shop",
            family=self.family,
            creator=user,
            type_of_shop=1,
        )
        product = Product.objects.create(
            product_name="Existing Product",
            family=self.family,
            category=category,
            default_shop=shop,
            creator=user,
        )
        FamilyMembership.objects.create(
            family=self.family,
            user=user,
            display_name="Ordinary Member",
        )
        self.client.force_login(user)

        self.assertEqual(
            self.client.post(
                family_url(self.family, "add_product"),
                {
                    "product_name": "Blocked Product",
                    "category": category.pk,
                    "default_shop": shop.pk,
                    "default_quantity": 1,
                    "default_unit": "item",
                },
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                family_url(
                    self.family,
                    "product_detail",
                    slug=product.slug,
                ),
                {
                    "product_name": "Changed Product",
                    "category": category.pk,
                    "default_shop": shop.pk,
                    "default_quantity": 1,
                    "default_unit": "item",
                },
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                family_url(self.family, "add_shop"),
                {
                    "shop_name": "Blocked Shop",
                    "type_of_shop": 1,
                },
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                family_url(self.family, "shop_detail", slug=shop.slug),
                {"shop_name": "Changed Shop", "type_of_shop": 1},
            ).status_code,
            403,
        )


class WebSocketFamilyIsolationTests(TwoFamilyFixtureMixin, TransactionTestCase):
    async def test_http_action_broadcasts_only_to_own_family(self):
        communicator_a = WebsocketCommunicator(
            ShoppingConsumer.as_asgi(), ws_path(self.family_a),
        )
        communicator_a.scope["user"] = self.member_a
        communicator_a.scope["url_route"] = {
            "kwargs": {"family_public_id": self.family_a.public_id},
        }
        connected_a, _ = await communicator_a.connect()
        self.assertTrue(connected_a)

        communicator_b = WebsocketCommunicator(
            ShoppingConsumer.as_asgi(), ws_path(self.family_b),
        )
        communicator_b.scope["user"] = self.member_b
        communicator_b.scope["url_route"] = {
            "kwargs": {"family_public_id": self.family_b.public_id},
        }
        connected_b, _ = await communicator_b.connect()
        self.assertTrue(connected_b)

        try:
            logged_in = await sync_to_async(self.client.login)(
                username="admin_a", password="pass1234",
            )
            self.assertTrue(logged_in)
            await sync_to_async(self.client.post)(
                self.url_a("shopping_list"),
                {"item_id": self.list_item_a.id, "action": "add_to_basket"},
            )

            message = await communicator_a.receive_json_from()
            self.assertEqual(message["list_item_id"], self.list_item_a.id)
            self.assertTrue(message["in_basket"])
            self.assertFalse(message["cancelled"])
            self.assertEqual(message["user"], "admin_a")

            self.assertTrue(await communicator_b.receive_nothing(timeout=0.2))
        finally:
            await communicator_a.disconnect()
            await communicator_b.disconnect()

    async def test_rejects_connection_without_family_membership(self):
        user = await self._create_unaffiliated_superuser()
        communicator = WebsocketCommunicator(
            ShoppingConsumer.as_asgi(), ws_path(self.family_a),
        )
        communicator.scope["user"] = user
        communicator.scope["url_route"] = {
            "kwargs": {"family_public_id": self.family_a.public_id},
        }

        connected, close_code = await communicator.connect()

        self.assertFalse(connected)
        self.assertEqual(close_code, 4403)

    async def test_same_user_can_connect_to_two_family_groups_separately(self): # noqa
        second_family, _admin, _members = await sync_to_async(
            create_family_with_members,
        )("Family C", "admin_c")
        await sync_to_async(FamilyMembership.objects.create)(
            family=second_family, user=self.member_a, display_name="MemberA2", # noqa
        )

        communicator_first = WebsocketCommunicator(
            ShoppingConsumer.as_asgi(), ws_path(self.family_a),
        )
        communicator_first.scope["user"] = self.member_a
        communicator_first.scope["url_route"] = {
            "kwargs": {"family_public_id": self.family_a.public_id},
        }
        connected_first, _ = await communicator_first.connect()
        self.assertTrue(connected_first)

        communicator_second = WebsocketCommunicator(
            ShoppingConsumer.as_asgi(), ws_path(second_family),
        )
        communicator_second.scope["user"] = self.member_a
        communicator_second.scope["url_route"] = {
            "kwargs": {"family_public_id": second_family.public_id},
        }
        connected_second, _ = await communicator_second.connect()
        self.assertTrue(connected_second)

        try:
            logged_in = await sync_to_async(self.client.login)(
                username="member_a", password="pass1234",
            )
            self.assertTrue(logged_in)
            await sync_to_async(self.client.post)(
                self.url_a("shopping_list"),
                {"item_id": self.list_item_a.id, "action": "add_to_basket"},
            )

            message = await communicator_first.receive_json_from()
            self.assertEqual(message["list_item_id"], self.list_item_a.id)
            self.assertTrue(
                await communicator_second.receive_nothing(timeout=0.2),
            )
        finally:
            await communicator_first.disconnect()
            await communicator_second.disconnect()

    @staticmethod
    async def _create_unaffiliated_superuser():
        return await sync_to_async(User.objects.create_superuser)(
            username="root2", password="pass1234", email="root2@example.com",
        )


class WebSocketServerAuthorityTests(TwoFamilyFixtureMixin, TransactionTestCase):
    async def test_spoofed_client_username_and_state_are_never_broadcast(self):
        communicator_a = WebsocketCommunicator(
            ShoppingConsumer.as_asgi(), ws_path(self.family_a),
        )
        communicator_a.scope["user"] = self.member_a
        communicator_a.scope["url_route"] = {
            "kwargs": {"family_public_id": self.family_a.public_id},
        }
        connected, _ = await communicator_a.connect()
        self.assertTrue(connected)

        try:
            # A malicious client claims to be someone else and asserts an
            # in_basket/cancelled state that does not match the database.
            await communicator_a.send_to(text_data=(
                '{"list_item_id": %d, "in_basket": true, '
                '"cancelled": true, "user": "admin_a"}'
            ) % self.list_item_a.id)

            self.assertTrue(
                await communicator_a.receive_nothing(timeout=0.2),
            )
            await sync_to_async(self.list_item_a.refresh_from_db)()
            self.assertFalse(self.list_item_a.in_basket)
            self.assertFalse(self.list_item_a.cancelled)
        finally:
            await communicator_a.disconnect()

    async def test_cross_family_item_id_is_never_broadcast(self):
        communicator_a = WebsocketCommunicator(
            ShoppingConsumer.as_asgi(), ws_path(self.family_a),
        )
        communicator_a.scope["user"] = self.member_a
        communicator_a.scope["url_route"] = {
            "kwargs": {"family_public_id": self.family_a.public_id},
        }
        connected_a, _ = await communicator_a.connect()
        self.assertTrue(connected_a)

        communicator_b = WebsocketCommunicator(
            ShoppingConsumer.as_asgi(), ws_path(self.family_b),
        )
        communicator_b.scope["user"] = self.member_b
        communicator_b.scope["url_route"] = {
            "kwargs": {"family_public_id": self.family_b.public_id},
        }
        connected_b, _ = await communicator_b.connect()
        self.assertTrue(connected_b)

        try:
            # member_a claims an item id that actually belongs to family B.
            await communicator_a.send_to(text_data=(
                '{"list_item_id": %d, "in_basket": true, '
                '"cancelled": false, "user": "member_a"}'
            ) % self.list_item_b.id)

            self.assertTrue(
                await communicator_a.receive_nothing(timeout=0.2),
            )
            self.assertTrue(
                await communicator_b.receive_nothing(timeout=0.2),
            )

            logged_in = await sync_to_async(self.client.login)(
                username="member_a", password="pass1234",
            )
            self.assertTrue(logged_in)
            response = await sync_to_async(self.client.post)(
                self.url_a("shopping_list"),
                {"item_id": self.list_item_b.id, "action": "add_to_basket"},
            )
            self.assertEqual(response.status_code, 404)

            self.assertTrue(
                await communicator_a.receive_nothing(timeout=0.2),
            )
            self.assertTrue(
                await communicator_b.receive_nothing(timeout=0.2),
            )
        finally:
            await communicator_a.disconnect()
            await communicator_b.disconnect()


class BuyBasketWebSocketTests(TwoFamilyFixtureMixin, TransactionTestCase):
    async def test_buy_basket_sends_bought_updates_and_one_grouped_notification(self): # noqa
        second_item = await sync_to_async(ListItem.objects.create)(
            product=self.product_a, family=self.family_a,
            creator=self.admin_a,
        )

        def put_both_in_basket():
            for item in (self.list_item_a, second_item):
                item.in_basket = True
                item.put_in_basket_by = self.member_a
                item.time_put_in_basket = timezone.now()
                item.save()

        await sync_to_async(put_both_in_basket)()

        communicator = WebsocketCommunicator(
            ShoppingConsumer.as_asgi(), ws_path(self.family_a),
        )
        communicator.scope["user"] = self.member_a
        communicator.scope["url_route"] = {
            "kwargs": {"family_public_id": self.family_a.public_id},
        }
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        try:
            logged_in = await sync_to_async(self.client.login)(
                username="member_a", password="pass1234",
            )
            self.assertTrue(logged_in)
            await sync_to_async(self.client.post)(
                self.url_a("shopping_list"), {"action": "buy_basket"},
            )

            messages = [
                await communicator.receive_json_from() for _ in range(3)
            ]
            item_messages = [
                m for m in messages if m["type"] == "list_item_update"
            ]
            basket_messages = [
                m for m in messages if m["type"] == "basket_bought"
            ]

            # Two items were bought, but only one grouped notification is
            # sent for the whole Buy My Basket operation.
            self.assertEqual(len(item_messages), 2)
            for message in item_messages:
                self.assertTrue(message["bought"])
                self.assertEqual(message["quantity_bought"], 1)
                self.assertTrue(message["product_name"])

            self.assertEqual(len(basket_messages), 1)
            self.assertEqual(basket_messages[0]["item_count"], 2)
            self.assertEqual(basket_messages[0]["buyer_username"], "member_a")

            self.assertTrue(
                await communicator.receive_nothing(timeout=0.2),
            )
        finally:
            await communicator.disconnect()
