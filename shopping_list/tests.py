from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connections, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from types import SimpleNamespace
import threading

from allauth.account.models import EmailAddress
from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator

from family_shopping_list.consumers import ShoppingConsumer
from utilities.utils import is_family_admin
from .forms import CategoryForm, ProductForm, ShopForm
from .views import (
    GROUP_BY_DIMENSIONS,
    _group_label_and_sort_key,
    group_shopping_list_items,
)
from .models import (
    Category,
    Family,
    FamilyMembership,
    ListItem,
    Product,
    Shop,
    ShoppingList,
    lock_family_for_membership_changes,
)


def create_family_with_members(
    name, admin_username, member_usernames=(), is_solo=False,
):
    family = Family.objects.create(name=name, is_solo=is_solo)
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


def add_test_units(item, count):
    values = {
        field.attname: getattr(item, field.attname)
        for field in ListItem._meta.concrete_fields
        if field.name not in ('id', 'slug', 'date_created')
    }
    return [ListItem.objects.create(**values) for _ in range(count)]


def family_url(family, name, **kwargs):
    return reverse(name, kwargs={"family_slug": family.slug, **kwargs}) # noqa


def ws_path(family):
    return f"/ws/families/{family.slug}/shopping-list/"


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

    # --- Page-load unit controls ---

    def test_cancelled_item_has_disabled_unit_controls(self):
        self.list_item_a.cancelled = True
        self.list_item_a.save()
        self.client.force_login(self.member_a)
        html = self.client.get(self.url_a("shopping_list")).content.decode()
        row = html.split('data-state="cancelled"', 1)[1].split('</li>', 1)[0]
        self.assertEqual(row.count('data-basket-action='), 4)
        self.assertEqual(row.count('disabled aria-disabled="true"'), 4)
        self.assertNotIn('name="cancelled_item"', html)
        self.assertNotIn('cancel-chkbx', html)

    def test_in_basket_item_renders_without_cancelled_checkbox(self):
        self.list_item_a.in_basket = True
        self.list_item_a.put_in_basket_by = self.member_a
        self.list_item_a.save()
        self.client.force_login(self.member_a)
        html = self.client.get(self.url_a("shopping_list")).content.decode()
        self.assertNotIn('name="cancelled_item"', html)
        self.assertNotIn('cancel-chkbx', html)
        row = html.split('data-state="in_basket"', 1)[1].split('</li>', 1)[0]
        self.assertEqual(row.count('data-basket-action='), 4)
        self.assertEqual(row.count('disabled aria-disabled="true"'), 2)

    def test_normal_item_renders_without_cancelled_checkbox_and_plus_enabled(self):
        self.client.force_login(self.member_a)
        html = self.client.get(self.url_a("shopping_list")).content.decode()
        self.assertNotIn('name="cancelled_item"', html)
        self.assertNotIn('cancel-chkbx', html)
        self.assertNotIn('toggleCancelUncancel', html)
        row = html.split('data-state="ordinary"', 1)[1].split('</li>', 1)[0]
        self.assertEqual(row.count('data-basket-action='), 4)
        plus = row.split('data-basket-action="add_unit"', 1)[1].split('>', 1)[0]
        self.assertNotIn('disabled', plus)
        fragments = self.client.get(
            self.url_a("shopping_list"), {"rows_only": "1"},
        ).json()
        self.assertNotIn('name="cancelled_item"', fragments["rows_html"])
        self.assertNotIn('cancel-chkbx', fragments["rows_html"])

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

        # Cancel/uncancel are AJAX-only actions: they return JSON, not a
        # redirect (the live UI update comes over the WebSocket instead).
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
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

        self.assertEqual(response.status_code, 200)
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
        self._put_in_basket(self.list_item_a, self.member_a)
        add_test_units(self.list_item_a, 2)

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
        self.assertEqual(self.list_item_a.quantity_bought, 1)
        self.assertEqual(ListItem.objects.filter(product=self.product_a, buyer=self.member_a).count(), 3)
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


class EmptyCategoryHeadingTests(TwoFamilyTestCase):
    """Category/shop headings in the grouped shopping-list view. Empty
    groups are already excluded server-side (group_shopping_list_items
    only buckets labels that have at least one passed-in item); these
    tests guard that, and the related "last item cancelled" case. A
    cancelled item stays on the active list (see _active_list_items), so
    its heading is still rendered here - just marked with
    data-group-heading/cancelled-list-item so the client can hide it
    while "Show cancelled items" is off (see the client-side test in
    shopping_list/tests_unit_controls.js)."""

    def setUp(self):
        super().setUp()
        self.empty_category = Category.objects.create(
            category_name="Empty Category", family=self.family_a,
            creator=self.admin_a,
        )
        # A catalogue product exists in this category, but nothing from
        # it is on the shopping list - the category itself is untouched.
        Product.objects.create(
            product_name="Unlisted Product", family=self.family_a,
            category=self.empty_category, creator=self.admin_a,
        )

    def test_category_with_items_is_displayed(self):
        self.client.force_login(self.admin_a)
        response = self.client.get(
            self.url_a("shopping_list"), {"group_by": "category"},
        )

        labels = [g['label'] for g in response.context["grouped_shopping_list"]] # noqa
        self.assertIn("Category A", labels)
        self.assertContains(response, "Category A")

    def test_category_with_no_displayed_items_has_no_heading(self):
        self.client.force_login(self.admin_a)
        response = self.client.get(
            self.url_a("shopping_list"), {"group_by": "category"},
        )

        labels = [g['label'] for g in response.context["grouped_shopping_list"]] # noqa
        self.assertNotIn("Empty Category", labels)
        # "Empty Category" legitimately still appears in the filter
        # sidebar (any current category can be filtered by); only its
        # heading in the grouped list itself must be absent.
        self.assertNotContains(
            response,
            '<li class="list-group-item bg-light fw-bold" '
            'data-group-heading="primary">Empty Category</li>',
        )
        # Display-only: the category and its catalogue product remain.
        self.empty_category.refresh_from_db()
        self.assertTrue(self.empty_category.current)
        self.assertTrue(Product.objects.filter(
            family=self.family_a, category=self.empty_category, current=True,
        ).exists())

    def test_cancelling_the_last_displayed_item_marks_its_heading_for_client_side_hiding(self): # noqa
        self.client.force_login(self.member_a)
        category = Category.objects.create(
            category_name="Disappearing", family=self.family_a,
            creator=self.admin_a,
        )
        product = Product.objects.create(
            product_name="Vanishing Product", family=self.family_a,
            category=category, creator=self.admin_a,
        )
        item = ListItem.objects.create(
            product=product, family=self.family_a, creator=self.member_a,
        )

        response = self.client.get(
            self.url_a("shopping_list"), {"group_by": "category"},
        )
        self.assertContains(response, "Disappearing")

        # The normal "remove" action on a product's only remaining unit:
        # confirmation is required, then confirmed (soft cancel, not a
        # hard delete - see ListItemView's final-unit handling).
        conflict = self.client.post(self.url_a("shopping_list"), {
            "item_id": item.pk, "action": "remove_unit",
        })
        self.assertEqual(conflict.status_code, 409)
        confirmed = self.client.post(self.url_a("shopping_list"), {
            "item_id": item.pk, "action": "cancel_final_unit",
        })
        self.assertEqual(confirmed.status_code, 200)
        item.refresh_from_db()
        self.assertTrue(item.cancelled)

        heading_markup = (
            '<li class="list-group-item bg-light fw-bold" '
            'data-group-heading="primary">Disappearing</li>'
        )
        full_page = self.client.get(
            self.url_a("shopping_list"), {"group_by": "category"},
        )
        self.assertContains(full_page, heading_markup)
        self.assertContains(full_page, "cancelled-list-item")

        # Realtime/WebSocket-driven refreshes use this same endpoint, so
        # the client's syncGroupHeadingVisibility() sees the same markup.
        live_refresh = self.client.get(self.url_a("shopping_list"), {
            "group_by": "category", "rows_only": "1",
        })
        rows_html = live_refresh.json()["rows_html"]
        self.assertIn(heading_markup, rows_html)
        self.assertIn("cancelled-list-item", rows_html)

        # The underlying category and product are untouched.
        category.refresh_from_db()
        product.refresh_from_db()
        self.assertTrue(category.current)
        self.assertTrue(product.current)


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


class FamilyAdminPrivilegeTests(TestCase):
    """Replaces the retired AdultGroupLifecycleTests. Family-administrator
    authority is now determined solely by FamilyMembership.role for the
    family being acted on (see utilities.utils.is_family_admin) - there is
    no global adult/child Django Group or signal-driven membership
    involved any more. Cross-family isolation and the superuser-as-
    ordinary-member case for a SECOND family are covered by
    CrossFamilyAdminPrivilegeTests below."""

    def setUp(self):
        self.family = Family.objects.create(name="Family Admin Family")

    def test_admin_role_grants_catalogue_privileges_immediately(self):
        user = User.objects.create_user(username="role_admin")
        category = Category.objects.create(
            category_name="Role Category", family=self.family, creator=user,
        )
        shop = Shop.objects.create(
            shop_name="Role Shop", family=self.family, creator=user,
            type_of_shop=1,
        )
        membership = FamilyMembership.objects.create(
            family=self.family, user=user,
            role=FamilyMembership.Role.ADMIN, display_name="Role Admin",
        )
        self.client.force_login(user)

        response = self.client.post(family_url(self.family, "add_product"), { # noqa
            "product_name": "Admin Added Product", "category": category.pk,
            "default_shop": shop.pk, "default_quantity": 1,
            "default_unit": "item",
        })

        self.assertRedirects(response, family_url(self.family, "product_list")) # noqa
        self.assertTrue(Product.objects.filter(
            family=self.family, product_name="Admin Added Product",
        ).exists())

        # Demoting takes effect on the very next request - no signal and
        # no cached group membership to synchronise. A second admin must
        # exist first: the last-administrator invariant would otherwise
        # reject this demotion.
        FamilyMembership.objects.create(
            family=self.family, user=User.objects.create_user(
                username="role_admin_backup",
            ), role=FamilyMembership.Role.ADMIN,
            display_name="Backup Admin",
        )
        membership.role = FamilyMembership.Role.MEMBER
        membership.save(update_fields=["role"])

        self.assertEqual(self.client.post(
            family_url(self.family, "add_product"), {
                "product_name": "Should Be Blocked", "category": category.pk,
                "default_shop": shop.pk, "default_quantity": 1,
                "default_unit": "item",
            },
        ).status_code, 403)
        self.assertFalse(Product.objects.filter(
            family=self.family, product_name="Should Be Blocked",
        ).exists())

    def test_ordinary_member_cannot_add_or_edit_products_or_shops(self):
        user = User.objects.create_user(username="ordinary_member")
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

        # Ordinary members still use the application normally - no
        # adult/child role gates this, only membership itself.
        self.assertEqual(self.client.get(
            family_url(self.family, "shopping_list"),
        ).status_code, 200)

    def test_superuser_is_not_automatically_a_family_admin(self):
        superuser = User.objects.create_superuser(
            username="root_in_family", email="root@example.com",
        )
        FamilyMembership.objects.create(
            family=self.family, user=superuser, display_name="Root",
        )
        self.client.force_login(superuser)

        response = self.client.post(family_url(self.family, "add_product"), { # noqa
            "product_name": "Should Be Blocked",
            "category": "", "default_shop": "",
            "default_quantity": 1, "default_unit": "item",
        })

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Product.objects.filter(
            family=self.family, product_name="Should Be Blocked",
        ).exists())

    def test_password_management_and_logout_remain_available(self):
        user = User.objects.create_user(username="ordinary_password_user")
        FamilyMembership.objects.create(
            family=self.family, user=user, display_name="Ordinary",
        )
        self.client.force_login(user)

        change_password = self.client.get(
            reverse("account_change_password"),
        )
        self.assertEqual(change_password.status_code, 200)

        page = self.client.get(family_url(self.family, "shopping_list"))
        self.assertContains(page, "Logout")
        self.assertContains(page, reverse("account_change_password"))

    def test_family_admin_wording_replaces_the_retired_parental_wording(self): # noqa
        user = User.objects.create_user(username="wording_member")
        FamilyMembership.objects.create(
            family=self.family, user=user, display_name="Member",
        )
        self.client.force_login(user)

        page = self.client.get(family_url(self.family, "product_list"))

        self.assertContains(page, "Ask a family admin to add a new product") # noqa
        self.assertNotContains(page, "Ask your parents")

    def test_removing_the_legacy_signal_machinery_does_not_break_membership_lifecycle(self): # noqa
        # Creating/updating/deleting Users and FamilyMemberships must not
        # raise: there is no dangling post_save/post_delete receiver left
        # importing the removed AutomaticAdultGroupMembership model. A
        # second admin is required so the last-administrator invariant
        # (unrelated to the retired signal machinery) doesn't reject the
        # demotion/removal being exercised here.
        FamilyMembership.objects.create(
            family=self.family, user=User.objects.create_user(
                username="lifecycle_backup_admin",
            ), role=FamilyMembership.Role.ADMIN,
            display_name="Lifecycle Backup Admin",
        )
        user = User.objects.create_user(username="lifecycle_user")
        membership = FamilyMembership.objects.create(
            family=self.family, user=user, display_name="Lifecycle",
            role=FamilyMembership.Role.ADMIN,
        )
        membership.role = FamilyMembership.Role.MEMBER
        membership.save(update_fields=["role"])
        membership.delete()
        user.delete()

        self.assertFalse(FamilyMembership.objects.filter(
            display_name="Lifecycle",
        ).exists())


class CrossFamilyAdminPrivilegeTests(TwoFamilyTestCase):
    """Regression coverage for the cross-family administrator privilege
    leak: `is_adult()`/the global "adult" Django Group used to grant
    catalogue-management privileges based on ANY family admin membership,
    or on `is_superuser`, regardless of the family actually being acted
    on. The fix (`utilities.utils.is_family_admin`) checks only
    `request.family_membership.is_family_admin` for the specific family
    resolved by `FamilyMembershipRequiredMixin`."""

    def setUp(self):
        super().setUp()
        # admin_a administers family_a (from the base fixture) and is
        # ALSO an ordinary member of family_b - the scenario this fix
        # targets.
        FamilyMembership.objects.create(
            family=self.family_b, user=self.admin_a,
            display_name="Admin A in Family B",
        )
        # A second user who is an ordinary member of BOTH families. No
        # password needed; only ever authenticated via force_login.
        self.both_member = User.objects.create_user(username="both_member")
        FamilyMembership.objects.create(
            family=self.family_a, user=self.both_member,
            display_name="Both A",
        )
        FamilyMembership.objects.create(
            family=self.family_b, user=self.both_member,
            display_name="Both B",
        )

    def _attempt_admin_only_operations(self, family, product, shop):
        """POSTs every admin-only product/shop/variant operation against
        `family` with the currently logged-in client and returns the list
        of response status codes, in a fixed order."""
        add_product = self.client.post(
            family_url(family, "add_product"), {
                "product_name": "Should Be Blocked",
                "category": "", "default_shop": "",
                "default_quantity": 1, "default_unit": "item",
            },
        )
        edit_product = self.client.post(
            family_url(family, "product_detail", slug=product.slug), {
                "product_name": "Renamed By Intruder",
                "category": product.category_id or "",
                "default_shop": product.default_shop_id or "",
                "default_quantity": 1, "default_unit": "item",
            },
        )
        delete_product = self.client.post(
            family_url(family, "product_delete", slug=product.slug),
        )
        add_shop = self.client.post(
            family_url(family, "add_shop"),
            {"shop_name": "Should Be Blocked", "type_of_shop": 1},
        )
        edit_shop = self.client.post(
            family_url(family, "shop_detail", slug=shop.slug),
            {"shop_name": "Renamed By Intruder", "type_of_shop": 1},
        )
        delete_shop = self.client.post(
            family_url(family, "shop_delete", slug=shop.slug),
        )
        self.client.post(family_url(family, "add_to_shopping_list"), {
            "selected-products": [product.id],
        })
        create_variant = self.client.post(
            family_url(family, "resolve_shopping_list_duplicate"), {
                "product_id": product.id, "choice": "create_variant",
                "variant_name": "Intruder Variant",
            },
        )
        return [
            r.status_code for r in (
                add_product, edit_product, delete_product,
                add_shop, edit_shop, create_variant,
            )
        ]

    def test_admin_retains_their_own_family_privileges(self):
        self.client.force_login(self.admin_a)

        response = self.client.post(family_url(self.family_a, "add_product"), { # noqa
            "product_name": "Legitimate New Product",
            "category": self.category_a.pk, "default_shop": self.shop_a.pk,
            "default_quantity": 1, "default_unit": "item",
        })

        self.assertRedirects(response, self.url_a("product_list"))
        self.assertTrue(Product.objects.filter(
            family=self.family_a, product_name="Legitimate New Product",
        ).exists())

    def test_admin_of_one_family_gets_no_admin_privileges_in_another(self):
        self.client.force_login(self.admin_a)

        statuses = self._attempt_admin_only_operations(
            self.family_b, self.product_b, self.shop_b,
        )

        self.assertEqual(statuses, [403] * len(statuses))
        self.product_b.refresh_from_db()
        self.shop_b.refresh_from_db()
        self.assertTrue(self.product_b.current)
        self.assertEqual(self.product_b.product_name, "Product B")
        self.assertTrue(self.shop_b.current)
        self.assertEqual(self.shop_b.shop_name, "Shop B")
        self.assertFalse(Product.objects.filter(
            family=self.family_b, product_name__in=[
                "Should Be Blocked", "Intruder Variant",
            ],
        ).exists())
        self.assertFalse(Shop.objects.filter(
            family=self.family_b, shop_name="Should Be Blocked",
        ).exists())

    def test_admin_of_one_family_keeps_ordinary_member_rights_in_another(self): # noqa
        self.client.force_login(self.admin_a)

        response = self.client.get(self.url_b("shopping_list"))
        self.assertEqual(response.status_code, 200)

        # self.product_b is already on the list (base fixture); use a
        # fresh product so the ordinary "add to list" action succeeds
        # directly rather than hitting the separate duplicate-conflict
        # prompt (a different, already-covered code path).
        fresh_product = Product.objects.create(
            product_name="Fresh Product B", family=self.family_b,
            category=self.category_b, default_shop=self.shop_b,
            creator=self.admin_b,
        )
        add_response = self.client.post(
            family_url(self.family_b, "add_to_shopping_list"),
            {"selected-products": [fresh_product.id]},
        )
        self.assertRedirects(add_response, self.url_b("shopping_list"))
        self.assertTrue(ListItem.objects.filter(
            family=self.family_b, product=fresh_product,
        ).exists())

    def test_member_of_both_families_gets_no_admin_privileges_in_either(self): # noqa
        self.client.force_login(self.both_member)

        statuses_a = self._attempt_admin_only_operations(
            self.family_a, self.product_a, self.shop_a,
        )
        statuses_b = self._attempt_admin_only_operations(
            self.family_b, self.product_b, self.shop_b,
        )

        self.assertEqual(statuses_a, [403] * len(statuses_a))
        self.assertEqual(statuses_b, [403] * len(statuses_b))

    def test_single_family_admin_behaviour_is_unaffected(self):
        # admin_b has no membership at all in family_a: the ordinary,
        # most common case, confirming nothing regressed for it.
        self.client.force_login(self.admin_b)

        response = self.client.post(family_url(self.family_b, "add_product"), { # noqa
            "product_name": "Ordinary Admin Product",
            "category": self.category_b.pk, "default_shop": self.shop_b.pk,
            "default_quantity": 1, "default_unit": "item",
        })

        self.assertRedirects(response, self.url_b("product_list"))
        self.assertTrue(Product.objects.filter(
            family=self.family_b, product_name="Ordinary Admin Product",
        ).exists())

    def test_superuser_status_grants_no_admin_privilege_as_an_ordinary_member(self): # noqa
        # The removed `is_adult()` path also treated is_superuser as an
        # automatic "adult" grant, independent of family role; closing
        # the cross-family leak closes this adjacent leak too, since both
        # went through the same call sites.
        superuser = User.objects.create_superuser(
            username="root_member", email="root@example.com",
        )
        FamilyMembership.objects.create(
            family=self.family_a, user=superuser, display_name="Root",
        )
        self.client.force_login(superuser)

        statuses = self._attempt_admin_only_operations(
            self.family_a, self.product_a, self.shop_a,
        )

        self.assertEqual(statuses, [403] * len(statuses))

    def test_missing_family_context_is_denied_not_granted_globally(self):
        # Direct unit coverage of the fail-safe helper itself: a request
        # reached without the expected family-scoped context (no
        # `family_membership` attribute at all) must be denied, never
        # fall back to a broader/global privilege check.
        bare_request = SimpleNamespace()
        self.assertFalse(is_family_admin(bare_request))

        admin_request = SimpleNamespace(family_membership=FamilyMembership(
            role=FamilyMembership.Role.ADMIN,
        ))
        self.assertTrue(is_family_admin(admin_request))

        member_request = SimpleNamespace(family_membership=FamilyMembership(
            role=FamilyMembership.Role.MEMBER,
        ))
        self.assertFalse(is_family_admin(member_request))

        none_request = SimpleNamespace(family_membership=None)
        self.assertFalse(is_family_admin(none_request))


class WebSocketFamilyIsolationTests(TwoFamilyFixtureMixin, TransactionTestCase):
    async def test_http_action_broadcasts_only_to_own_family(self):
        communicator_a = WebsocketCommunicator(
            ShoppingConsumer.as_asgi(), ws_path(self.family_a),
        )
        communicator_a.scope["user"] = self.member_a
        communicator_a.scope["url_route"] = {
            "kwargs": {"family_slug": self.family_a.slug},
        }
        connected_a, _ = await communicator_a.connect()
        self.assertTrue(connected_a)

        communicator_b = WebsocketCommunicator(
            ShoppingConsumer.as_asgi(), ws_path(self.family_b),
        )
        communicator_b.scope["user"] = self.member_b
        communicator_b.scope["url_route"] = {
            "kwargs": {"family_slug": self.family_b.slug},
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
            "kwargs": {"family_slug": self.family_a.slug},
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
            "kwargs": {"family_slug": self.family_a.slug},
        }
        connected_first, _ = await communicator_first.connect()
        self.assertTrue(connected_first)

        communicator_second = WebsocketCommunicator(
            ShoppingConsumer.as_asgi(), ws_path(second_family),
        )
        communicator_second.scope["user"] = self.member_a
        communicator_second.scope["url_route"] = {
            "kwargs": {"family_slug": second_family.slug},
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
            "kwargs": {"family_slug": self.family_a.slug},
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
            "kwargs": {"family_slug": self.family_a.slug},
        }
        connected_a, _ = await communicator_a.connect()
        self.assertTrue(connected_a)

        communicator_b = WebsocketCommunicator(
            ShoppingConsumer.as_asgi(), ws_path(self.family_b),
        )
        communicator_b.scope["user"] = self.member_b
        communicator_b.scope["url_route"] = {
            "kwargs": {"family_slug": self.family_b.slug},
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
            "kwargs": {"family_slug": self.family_a.slug},
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

            # Two items were bought: each sends a list_item_update and a
            # product_list_status (both for the same product here, since
            # both basket items share product_a), plus one grouped
            # basket_bought notification, plus one shopping_list_complete
            # since buying both items (the list's only two) finishes it.
            messages = [
                await communicator.receive_json_from() for _ in range(6)
            ]
            item_messages = [
                m for m in messages if m["type"] == "list_item_update"
            ]
            status_messages = [
                m for m in messages if m["type"] == "product_list_status"
            ]
            basket_messages = [
                m for m in messages if m["type"] == "basket_bought"
            ]
            complete_messages = [
                m for m in messages if m["type"] == "shopping_list_complete"
            ]

            self.assertEqual(len(item_messages), 2)
            for message in item_messages:
                self.assertTrue(message["bought"])
                self.assertEqual(message["quantity_bought"], 1)
                self.assertTrue(message["product_name"])

            # Both bought items were the only active rows for product_a,
            # so it drops out of the active list entirely.
            self.assertEqual(len(status_messages), 2)
            for message in status_messages:
                self.assertEqual(message["product_id"], self.product_a.id)
                self.assertIsNone(message["on_list_quantity"])

            self.assertEqual(len(basket_messages), 1)
            self.assertEqual(basket_messages[0]["item_count"], 2)
            self.assertEqual(
                basket_messages[0]["buyer_user_id"], self.member_a.pk,
            )

            self.assertEqual(len(complete_messages), 1)

            self.assertTrue(
                await communicator.receive_nothing(timeout=0.2),
            )
        finally:
            await communicator.disconnect()


class DuplicateProductPromptTests(TwoFamilyTestCase):
    """Covers the "already on your shopping list" prompt: detecting a
    product already on the active list, and the two resolution choices
    (increase quantity / create a variant)."""

    def _resolve(self, family, **data):
        return self.client.post(
            family_url(family, "resolve_shopping_list_duplicate"), data,
        )

    def test_adding_a_product_not_on_the_list_adds_it_normally(self):
        new_product = Product.objects.create(
            product_name="New Product", family=self.family_a,
            category=self.category_a, default_shop=self.shop_a,
            creator=self.admin_a,
        )
        self.client.force_login(self.member_a)
        response = self.client.post(self.url_a("add_to_shopping_list"), {
            "selected-products": [new_product.id],
        })

        self.assertRedirects(response, self.url_a("shopping_list"))
        self.assertTrue(
            ListItem.objects.filter(
                product=new_product, family=self.family_a,
                current=True, date_bought__isnull=True,
            ).exists(),
        )

    def test_adding_a_product_already_on_the_list_offers_a_choice(self):
        # self.product_a is already on the list via self.list_item_a.
        self.client.force_login(self.member_a)
        response = self.client.post(self.url_a("add_to_shopping_list"), {
            "selected-products": [self.product_a.id],
        })

        self.assertRedirects(response, self.url_a("product_list"))
        # No second ListItem was silently created.
        self.assertEqual(
            ListItem.objects.filter(
                product=self.product_a, family=self.family_a,
            ).count(),
            1,
        )

        list_response = self.client.get(self.url_a("product_list"))
        conflicts = list_response.context["duplicate_conflicts"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["product"], self.product_a)
        self.assertEqual(conflicts[0]["existing_item"], self.list_item_a)

    def test_increasing_quantity_creates_one_additional_unit(self):
        self.client.force_login(self.member_a)
        self.client.post(self.url_a("add_to_shopping_list"), {
            "selected-products": [self.product_a.id],
        })

        response = self._resolve(
            self.family_a,
            product_id=self.product_a.id, choice="increase_quantity",
        )

        self.assertRedirects(response, self.url_a("shopping_list"))
        self.list_item_a.refresh_from_db()
        self.assertEqual(self.list_item_a.quantity_required, 1)
        self.assertEqual(
            ListItem.objects.filter(
                product=self.product_a, family=self.family_a,
            ).count(),
            2,
        )

    def test_creating_a_variant_preserves_original_and_adds_separate_item(self): # noqa
        self.client.force_login(self.admin_a)
        self.client.post(self.url_a("add_to_shopping_list"), {
            "selected-products": [self.product_a.id],
        })

        response = self._resolve(
            self.family_a,
            product_id=self.product_a.id, choice="create_variant",
            variant_name="Product A (organic)",
        )

        self.assertRedirects(response, self.url_a("shopping_list"))
        self.list_item_a.refresh_from_db()
        self.assertEqual(self.list_item_a.quantity_required, 1)  # unchanged

        variant = Product.objects.get(
            product_name="Product A (organic)", family=self.family_a,
        )
        self.assertEqual(variant.based_on, self.product_a)
        self.assertEqual(variant.category, self.product_a.category)
        self.assertEqual(variant.default_shop, self.product_a.default_shop)

        variant_item = ListItem.objects.get(
            product=variant, family=self.family_a,
        )
        self.assertEqual(variant_item.quantity_required, 1)
        # The original item is untouched, and both now coexist.
        self.assertEqual(
            ListItem.objects.filter(family=self.family_a, product__in=[
                self.product_a, variant,
            ]).count(),
            2,
        )

    def test_non_adult_cannot_create_a_variant(self):
        self.client.force_login(self.member_a)
        self.client.post(self.url_a("add_to_shopping_list"), {
            "selected-products": [self.product_a.id],
        })

        response = self._resolve(
            self.family_a,
            product_id=self.product_a.id, choice="create_variant",
            variant_name="Product A (organic)",
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            Product.objects.filter(
                product_name="Product A (organic)",
            ).exists(),
        )

    def test_dismissing_the_prompt_leaves_the_list_unchanged(self):
        self.client.force_login(self.member_a)
        self.client.post(self.url_a("add_to_shopping_list"), {
            "selected-products": [self.product_a.id],
        })

        response = self.client.post(
            self.url_a("dismiss_shopping_list_duplicate"),
            {"product_id": self.product_a.id},
        )

        self.assertRedirects(response, self.url_a("product_list"))
        self.list_item_a.refresh_from_db()
        self.assertEqual(self.list_item_a.quantity_required, 1)
        self.assertEqual(
            ListItem.objects.filter(
                product=self.product_a, family=self.family_a,
            ).count(),
            1,
        )
        # The prompt does not reappear once dismissed.
        list_response = self.client.get(self.url_a("product_list"))
        self.assertEqual(list_response.context["duplicate_conflicts"], [])

    def test_repeated_selection_of_the_same_product_is_consistent(self):
        self.client.force_login(self.member_a)
        for _ in range(3):
            self.client.post(self.url_a("add_to_shopping_list"), {
                "selected-products": [self.product_a.id],
            })

        # Still just the one pending conflict and the one original item.
        list_response = self.client.get(self.url_a("product_list"))
        self.assertEqual(
            len(list_response.context["duplicate_conflicts"]), 1,
        )
        self.assertEqual(
            ListItem.objects.filter(
                product=self.product_a, family=self.family_a,
            ).count(),
            1,
        )

    def test_product_identity_is_by_id_not_by_name(self):
        # A variant has its own identity: selecting it is never treated as
        # a duplicate of the product it was based on, even though the two
        # are related.
        variant = Product.objects.create(
            product_name="Product A Variant", family=self.family_a,
            category=self.category_a, default_shop=self.shop_a,
            creator=self.admin_a, based_on=self.product_a,
        )

        self.client.force_login(self.member_a)
        response = self.client.post(self.url_a("add_to_shopping_list"), {
            "selected-products": [variant.id],
        })

        self.assertRedirects(response, self.url_a("shopping_list"))
        self.assertTrue(
            ListItem.objects.filter(
                product=variant, family=self.family_a,
            ).exists(),
        )

    def test_cancelled_item_still_counts_as_on_the_active_list(self):
        # Cancelled items remain on the active shopping list view (just
        # struck through), so re-selecting the product must still prompt
        # rather than silently adding a second row.
        self.list_item_a.cancelled = True
        self.list_item_a.save()

        self.client.force_login(self.member_a)
        self.client.post(self.url_a("add_to_shopping_list"), {
            "selected-products": [self.product_a.id],
        })

        list_response = self.client.get(self.url_a("product_list"))
        self.assertEqual(
            len(list_response.context["duplicate_conflicts"]), 1,
        )

    def test_bought_item_does_not_count_as_a_duplicate(self):
        # A product whose only ListItem has already been bought is no
        # longer "on the active list": re-adding it is a normal add.
        self.list_item_a.date_bought = timezone.now()
        self.list_item_a.quantity_bought = 1
        self.list_item_a.save()

        self.client.force_login(self.member_a)
        response = self.client.post(self.url_a("add_to_shopping_list"), {
            "selected-products": [self.product_a.id],
        })

        self.assertRedirects(response, self.url_a("shopping_list"))
        self.assertEqual(
            ListItem.objects.filter(
                product=self.product_a, family=self.family_a,
                date_bought__isnull=True,
            ).count(),
            1,
        )

    def test_cross_family_product_id_in_resolve_returns_404(self):
        self.client.force_login(self.member_a)
        response = self._resolve(
            self.family_a,
            product_id=self.product_b.id, choice="increase_quantity",
        )

        self.assertEqual(response.status_code, 404)

    def test_repeated_increase_quantity_submissions_are_not_lost(self):
        # Simulates a double-click / repeated submission: each resolution
        # must count, with no lost update and no duplicate row.
        self.client.force_login(self.member_a)
        self.client.post(self.url_a("add_to_shopping_list"), {
            "selected-products": [self.product_a.id],
        })

        self._resolve(
            self.family_a,
            product_id=self.product_a.id, choice="increase_quantity",
        )
        # Simulate the pending state still showing (e.g. a second
        # browser tab that hadn't refreshed) and the user clicking again.
        self._add_pending_again()
        self._resolve(
            self.family_a,
            product_id=self.product_a.id, choice="increase_quantity",
        )

        self.list_item_a.refresh_from_db()
        self.assertEqual(self.list_item_a.quantity_required, 1)
        self.assertEqual(
            ListItem.objects.filter(
                product=self.product_a, family=self.family_a,
            ).count(),
            3,
        )

    def _add_pending_again(self):
        self.client.post(self.url_a("add_to_shopping_list"), {
            "selected-products": [self.product_a.id],
        })

    def test_double_submitting_the_same_variant_name_does_not_duplicate(self):
        self.client.force_login(self.admin_a)
        self.client.post(self.url_a("add_to_shopping_list"), {
            "selected-products": [self.product_a.id],
        })

        first = self._resolve(
            self.family_a,
            product_id=self.product_a.id, choice="create_variant",
            variant_name="Product A (organic)",
        )
        self._add_pending_again()
        second = self._resolve(
            self.family_a,
            product_id=self.product_a.id, choice="create_variant",
            variant_name="Product A (organic)",
        )

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(
            Product.objects.filter(
                product_name__iexact="Product A (organic)",
            ).count(),
            1,
        )


class DuplicateProductPromptWebSocketTests(
    TwoFamilyFixtureMixin, TransactionTestCase,
):
    async def test_increase_quantity_broadcasts_updated_quantity(self):
        communicator = WebsocketCommunicator(
            ShoppingConsumer.as_asgi(), ws_path(self.family_a),
        )
        communicator.scope["user"] = self.member_a
        communicator.scope["url_route"] = {
            "kwargs": {"family_slug": self.family_a.slug},
        }
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        try:
            await sync_to_async(self.client.force_login)(self.member_a)
            await sync_to_async(self.client.post)(
                self.url_a("add_to_shopping_list"),
                {"selected-products": [self.product_a.id]},
            )
            await sync_to_async(self.client.post)(
                self.url_a("resolve_shopping_list_duplicate"),
                {
                    "product_id": self.product_a.id,
                    "choice": "increase_quantity",
                },
            )

            message = await communicator.receive_json_from()
            self.assertEqual(message["type"], "list_item_added")
            self.assertNotEqual(message["list_item_id"], self.list_item_a.id)
            self.assertEqual(message["quantity_required"], 1)
        finally:
            await communicator.disconnect()

    async def test_create_variant_broadcasts_a_new_list_item(self):
        communicator = WebsocketCommunicator(
            ShoppingConsumer.as_asgi(), ws_path(self.family_a),
        )
        communicator.scope["user"] = self.member_a
        communicator.scope["url_route"] = {
            "kwargs": {"family_slug": self.family_a.slug},
        }
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        try:
            await sync_to_async(self.client.force_login)(self.admin_a)
            await sync_to_async(self.client.post)(
                self.url_a("add_to_shopping_list"),
                {"selected-products": [self.product_a.id]},
            )
            await sync_to_async(self.client.post)(
                self.url_a("resolve_shopping_list_duplicate"),
                {
                    "product_id": self.product_a.id,
                    "choice": "create_variant",
                    "variant_name": "Product A (organic)",
                },
            )

            message = await communicator.receive_json_from()
            self.assertEqual(message["type"], "list_item_added")
            self.assertEqual(message["product_name"], "Product A (organic)")
            self.assertEqual(message["quantity_required"], 1)
        finally:
            await communicator.disconnect()


class ProductCatalogueAddFlowTests(TwoFamilyTestCase):
    """Covers clicking a product row to add it, the separate
    product-details info icon, and the "On list" status indicator."""

    @staticmethod
    def _extract_row_html(html, product_id):
        marker = f'data-product-id="{product_id}"'
        start = html.index(marker)
        row_start = html.rindex('<li', 0, start)
        row_end = html.index('</li>', start) + len('</li>')
        return html[row_start:row_end]

    def test_product_row_click_target_is_a_real_submit_button(self):
        # A native <button type="submit"> is focusable and keyboard
        # operable (Enter/Space) without any extra JS or ARIA wiring.
        self.client.force_login(self.member_a)
        response = self.client.get(self.url_a("product_list"))
        row_html = self._extract_row_html(
            response.content.decode(), self.product_a.id,
        )

        self.assertIn('<form', row_html)
        self.assertIn('action="' + self.url_a("add_to_shopping_list") + '"', row_html) # noqa
        self.assertIn('<button type="submit"', row_html)
        self.assertIn(
            f'<span class="product-name-text">{self.product_a.product_name}</span>', # noqa
            row_html,
        )

    def test_product_name_is_no_longer_a_details_link(self):
        self.client.force_login(self.member_a)
        response = self.client.get(self.url_a("product_list"))
        row_html = self._extract_row_html(
            response.content.decode(), self.product_a.id,
        )

        # The old pattern directly linking the name to product_detail.
        detail_url = self.url_a(
            "product_detail", slug=self.product_a.slug,
        )
        self.assertNotIn(
            f'href="{detail_url}">{self.product_a.product_name}</a>',
            row_html,
        )

    def test_info_icon_links_to_product_details_outside_the_add_form(self):
        self.client.force_login(self.member_a)
        response = self.client.get(self.url_a("product_list"))
        row_html = self._extract_row_html(
            response.content.decode(), self.product_a.id,
        )

        detail_url = self.url_a(
            "product_detail", slug=self.product_a.slug,
        )
        self.assertIn(f'href="{detail_url}"', row_html)
        # The info link comes after the add-form's closing tag, i.e. it's
        # a sibling, never nested inside the add button/form.
        form_close_index = row_html.index('</form>')
        info_link_index = row_html.index(f'href="{detail_url}"')
        self.assertGreater(info_link_index, form_close_index)

    def test_visiting_product_details_does_not_add_to_the_list(self):
        new_product = Product.objects.create(
            product_name="Untouched Product", family=self.family_a,
            category=self.category_a, default_shop=self.shop_a,
            creator=self.admin_a,
        )
        self.client.force_login(self.member_a)
        response = self.client.get(
            self.url_a("product_detail", slug=new_product.slug),
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            ListItem.objects.filter(
                product=new_product, family=self.family_a,
            ).exists(),
        )

    def test_product_not_on_list_shows_no_badge(self):
        new_product = Product.objects.create(
            product_name="Fresh Product", family=self.family_a,
            category=self.category_a, default_shop=self.shop_a,
            creator=self.admin_a,
        )
        self.client.force_login(self.member_a)
        response = self.client.get(self.url_a("product_list"))

        products_by_id = {
            product.id: product
            for product in response.context["product_list"]
        }
        self.assertIsNone(products_by_id[new_product.id].on_list_quantity)

        row_html = self._extract_row_html(
            response.content.decode(), new_product.id,
        )
        self.assertIn('on-list-badge', row_html)
        self.assertIn('d-none', row_html)

    def test_product_on_list_shows_quantity_badge(self):
        self.client.force_login(self.member_a)
        response = self.client.get(self.url_a("product_list"))

        products_by_id = {
            product.id: product
            for product in response.context["product_list"]
        }
        self.assertEqual(products_by_id[self.product_a.id].on_list_quantity, 1) # noqa

        row_html = self._extract_row_html(
            response.content.decode(), self.product_a.id,
        )
        self.assertIn('On list', row_html)
        badge_start = row_html.index('on-list-badge')
        badge_section = row_html[badge_start:badge_start + 200]
        self.assertNotIn('d-none', badge_section.split('"', 1)[0])

    def test_quantity_increase_updates_the_catalogue_indicator(self):
        self.client.force_login(self.member_a)
        self.client.post(self.url_a("add_to_shopping_list"), {
            "selected-products": [self.product_a.id],
        })
        self.client.post(self.url_a("resolve_shopping_list_duplicate"), {
            "product_id": self.product_a.id, "choice": "increase_quantity",
        })

        response = self.client.get(self.url_a("product_list"))
        products_by_id = {
            product.id: product
            for product in response.context["product_list"]
        }
        self.assertEqual(products_by_id[self.product_a.id].on_list_quantity, 2) # noqa

    def test_cancelled_item_still_counts_toward_the_indicator(self):
        self.list_item_a.cancelled = True
        self.list_item_a.save()

        self.client.force_login(self.member_a)
        response = self.client.get(self.url_a("product_list"))

        products_by_id = {
            product.id: product
            for product in response.context["product_list"]
        }
        self.assertEqual(products_by_id[self.product_a.id].on_list_quantity, 1) # noqa

    def test_bought_item_no_longer_counts_toward_the_indicator(self):
        self.list_item_a.date_bought = timezone.now()
        self.list_item_a.quantity_bought = 1
        self.list_item_a.save()

        self.client.force_login(self.member_a)
        response = self.client.get(self.url_a("product_list"))

        products_by_id = {
            product.id: product
            for product in response.context["product_list"]
        }
        self.assertIsNone(products_by_id[self.product_a.id].on_list_quantity)

    def test_variant_has_its_own_independent_indicator(self):
        self.client.force_login(self.admin_a)
        self.client.post(self.url_a("add_to_shopping_list"), {
            "selected-products": [self.product_a.id],
        })
        self.client.post(self.url_a("resolve_shopping_list_duplicate"), {
            "product_id": self.product_a.id, "choice": "create_variant",
            "variant_name": "Product A (organic)",
        })
        variant = Product.objects.get(
            product_name="Product A (organic)", family=self.family_a,
        )

        response = self.client.get(self.url_a("product_list"))
        products_by_id = {
            product.id: product
            for product in response.context["product_list"]
        }
        # The original is untouched; the variant has its own quantity.
        self.assertEqual(products_by_id[self.product_a.id].on_list_quantity, 1) # noqa
        self.assertEqual(products_by_id[variant.id].on_list_quantity, 1)

    def test_family_isolation_of_catalogue_status(self):
        # family_b's product/list-item data must never leak into
        # family_a's rendered catalogue context.
        self.client.force_login(self.member_a)
        response = self.client.get(self.url_a("product_list"))

        product_ids = {
            product.id for product in response.context["product_list"]
        }
        self.assertNotIn(self.product_b.id, product_ids)

    def test_clicking_a_product_not_on_the_list_adds_it_via_the_row_form(self): # noqa
        new_product = Product.objects.create(
            product_name="Row Click Product", family=self.family_a,
            category=self.category_a, default_shop=self.shop_a,
            creator=self.admin_a,
        )
        self.client.force_login(self.member_a)
        response = self.client.post(self.url_a("add_to_shopping_list"), {
            "selected-products": [new_product.id],
        })

        self.assertRedirects(response, self.url_a("shopping_list"))
        self.assertTrue(
            ListItem.objects.filter(
                product=new_product, family=self.family_a,
            ).exists(),
        )

    def test_clicking_a_product_already_on_the_list_redirects_to_the_prompt(self): # noqa
        self.client.force_login(self.member_a)
        response = self.client.post(self.url_a("add_to_shopping_list"), {
            "selected-products": [self.product_a.id],
        })

        self.assertRedirects(response, self.url_a("product_list"))
        self.assertEqual(
            ListItem.objects.filter(
                product=self.product_a, family=self.family_a,
            ).count(),
            1,
        )


class ProductCatalogueStatusWebSocketTests(
    TwoFamilyFixtureMixin, TransactionTestCase,
):
    async def _connect(self, family, user):
        communicator = WebsocketCommunicator(
            ShoppingConsumer.as_asgi(), ws_path(family),
        )
        communicator.scope["user"] = user
        communicator.scope["url_route"] = {
            "kwargs": {"family_slug": family.slug},
        }
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        return communicator

    async def test_adding_a_new_product_broadcasts_its_catalogue_status(self):
        new_product = await sync_to_async(Product.objects.create)(
            product_name="Broadcast Product", family=self.family_a,
            category=self.category_a, default_shop=self.shop_a,
            creator=self.admin_a,
        )
        communicator = await self._connect(self.family_a, self.member_a)

        try:
            await sync_to_async(self.client.force_login)(self.member_a)
            await sync_to_async(self.client.post)(
                self.url_a("add_to_shopping_list"),
                {"selected-products": [new_product.id]},
            )

            messages = [
                await communicator.receive_json_from() for _ in range(2)
            ]
            status_messages = [
                m for m in messages if m["type"] == "product_list_status"
            ]
            self.assertEqual(len(status_messages), 1)
            self.assertEqual(status_messages[0]["product_id"], new_product.id) # noqa
            self.assertEqual(status_messages[0]["on_list_quantity"], 1)
        finally:
            await communicator.disconnect()

    async def test_increasing_quantity_broadcasts_updated_catalogue_status(self): # noqa
        communicator = await self._connect(self.family_a, self.member_a)

        try:
            await sync_to_async(self.client.force_login)(self.member_a)
            await sync_to_async(self.client.post)(
                self.url_a("resolve_shopping_list_duplicate"),
                {
                    "product_id": self.product_a.id,
                    "choice": "increase_quantity",
                },
            )

            messages = [
                await communicator.receive_json_from() for _ in range(2)
            ]
            status_messages = [
                m for m in messages if m["type"] == "product_list_status"
            ]
            self.assertEqual(len(status_messages), 1)
            self.assertEqual(
                status_messages[0]["product_id"], self.product_a.id,
            )
            self.assertEqual(status_messages[0]["on_list_quantity"], 2)
        finally:
            await communicator.disconnect()

    async def test_creating_a_variant_broadcasts_status_for_the_variant_only(self): # noqa
        communicator = await self._connect(self.family_a, self.member_a)

        try:
            await sync_to_async(self.client.force_login)(self.admin_a)
            await sync_to_async(self.client.post)(
                self.url_a("resolve_shopping_list_duplicate"),
                {
                    "product_id": self.product_a.id,
                    "choice": "create_variant",
                    "variant_name": "Product A (organic)",
                },
            )
            variant = await sync_to_async(Product.objects.get)(
                product_name="Product A (organic)", family=self.family_a,
            )

            messages = [
                await communicator.receive_json_from() for _ in range(2)
            ]
            status_messages = [
                m for m in messages if m["type"] == "product_list_status"
            ]
            self.assertEqual(len(status_messages), 1)
            self.assertEqual(status_messages[0]["product_id"], variant.id)
            self.assertEqual(status_messages[0]["on_list_quantity"], 1)
        finally:
            await communicator.disconnect()

    async def test_buying_a_basket_item_broadcasts_its_removal_from_status(self): # noqa
        def put_in_basket():
            self.list_item_a.in_basket = True
            self.list_item_a.put_in_basket_by = self.member_a
            self.list_item_a.time_put_in_basket = timezone.now()
            self.list_item_a.save()

        await sync_to_async(put_in_basket)()
        communicator = await self._connect(self.family_a, self.member_a)

        try:
            await sync_to_async(self.client.force_login)(self.member_a)
            await sync_to_async(self.client.post)(
                self.url_a("shopping_list"), {"action": "buy_basket"},
            )

            messages = [
                await communicator.receive_json_from() for _ in range(2)
            ]
            status_messages = [
                m for m in messages if m["type"] == "product_list_status"
            ]
            self.assertEqual(len(status_messages), 1)
            self.assertEqual(
                status_messages[0]["product_id"], self.product_a.id,
            )
            self.assertIsNone(status_messages[0]["on_list_quantity"])
        finally:
            await communicator.disconnect()

    async def test_family_isolation_of_catalogue_status_broadcasts(self):
        communicator_a = await self._connect(self.family_a, self.member_a)
        communicator_b = await self._connect(self.family_b, self.member_b)

        try:
            await sync_to_async(self.client.force_login)(self.member_b)
            await sync_to_async(self.client.post)(
                self.url_b("resolve_shopping_list_duplicate"),
                {
                    "product_id": self.product_b.id,
                    "choice": "increase_quantity",
                },
            )

            self.assertTrue(
                await communicator_a.receive_nothing(timeout=0.2),
            )
            message = await communicator_b.receive_json_from()
            self.assertEqual(message["type"], "list_item_added")
        finally:
            await communicator_a.disconnect()
            await communicator_b.disconnect()


class ShoppingListLifecycleTests(TwoFamilyTestCase):
    """Covers the open/closed list lifecycle: at most one open list per
    family, the close-out suggestion, and closing with carry-forward."""

    def _open_list(self, family):
        return ShoppingList.objects.get(
            family=family, status=ShoppingList.Status.OPEN,
        )

    def _close(
        self, family, shopping_list_id,
        carry_forward_unpurchased=False, carry_forward_cancelled=False,
    ):
        data = {"shopping_list_id": shopping_list_id}
        if carry_forward_unpurchased:
            data["carry_forward_unpurchased"] = "on"
        if carry_forward_cancelled:
            data["carry_forward_cancelled"] = "on"
        return self.client.post(
            family_url(family, "close_shopping_list"), data,
        )

    # --- Basic invariants ---

    def test_family_has_at_most_one_open_list(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ShoppingList.objects.create(family=self.family_a)

    def test_new_family_can_create_an_open_list_on_demand(self):
        new_family = Family.objects.create(name="Brand New Family")
        self.assertFalse(
            ShoppingList.objects.filter(family=new_family).exists(),
        )

        created = ShoppingList.get_or_create_open(new_family)

        self.assertEqual(created.status, ShoppingList.Status.OPEN)
        self.assertEqual(
            ShoppingList.get_or_create_open(new_family), created,
        )
        self.assertEqual(
            ShoppingList.objects.filter(
                family=new_family, status=ShoppingList.Status.OPEN,
            ).count(),
            1,
        )

    def test_existing_family_items_were_backfilled_onto_one_open_list(self):
        # TwoFamilyFixtureMixin's items go through ListItem.save(), which
        # auto-attaches the family's open list (creating it on first use,
        # exactly like the real backfill migration did for pre-existing
        # families).
        self.list_item_a.refresh_from_db()
        self.assertIsNotNone(self.list_item_a.shopping_list_id)
        self.assertEqual(
            self.list_item_a.shopping_list.status, ShoppingList.Status.OPEN,
        )

    # --- Completion detection ---

    def test_list_with_outstanding_items_is_not_complete(self):
        from .views import is_open_list_complete
        self.assertFalse(is_open_list_complete(self.family_a))

    def test_list_with_all_items_resolved_is_complete(self):
        from .views import is_open_list_complete
        self.list_item_a.cancelled = True
        self.list_item_a.save()
        self.assertTrue(is_open_list_complete(self.family_a))

    def test_empty_list_does_not_count_as_complete(self):
        from .views import is_open_list_complete
        new_family = Family.objects.create(name="Empty Family")
        ShoppingList.get_or_create_open(new_family)
        self.assertFalse(is_open_list_complete(new_family))

    def test_shopping_list_page_shows_completion_suggestion_when_complete(self): # noqa
        self.client.force_login(self.member_a)
        response = self.client.get(self.url_a("shopping_list"))
        self.assertFalse(response.context["show_completion_suggestion"])

        self.list_item_a.cancelled = True
        self.list_item_a.save()
        response = self.client.get(self.url_a("shopping_list"))
        self.assertTrue(response.context["show_completion_suggestion"])
        self.assertEqual(
            response.context["open_list"], self._open_list(self.family_a),
        )

    def test_deferring_suggestion_leaves_list_open_and_unchanged(self):
        self.list_item_a.cancelled = True
        self.list_item_a.save()
        self.client.force_login(self.member_a)
        open_list = self._open_list(self.family_a)

        response = self.client.post(
            self.url_a("dismiss_shopping_list_completion"),
            {"shopping_list_id": open_list.id, "item_count": 1},
        )
        self.assertRedirects(response, self.url_a("shopping_list"))

        follow_up = self.client.get(self.url_a("shopping_list"))
        self.assertFalse(follow_up.context["show_completion_suggestion"])

        open_list.refresh_from_db()
        self.assertEqual(open_list.status, ShoppingList.Status.OPEN)
        self.list_item_a.refresh_from_db()
        self.assertTrue(self.list_item_a.cancelled)
        self.assertTrue(self.list_item_a.current)

    def test_adding_another_item_makes_the_suggestion_reappear(self):
        # A dismissal is tied to the item count at the time; once that
        # count changes (a new item was added to the still-open list),
        # the suggestion becomes eligible again next time it's complete.
        self.list_item_a.cancelled = True
        self.list_item_a.save()
        self.client.force_login(self.member_a)
        open_list = self._open_list(self.family_a)
        self.client.post(
            self.url_a("dismiss_shopping_list_completion"),
            {"shopping_list_id": open_list.id, "item_count": 1},
        )

        second_item = ListItem.objects.create(
            product=self.product_a, family=self.family_a,
            creator=self.admin_a, cancelled=True,
        )
        self.assertTrue(
            self.client.get(
                self.url_a("shopping_list"),
            ).context["show_completion_suggestion"],
        )
        del second_item  # only needed to create the second row

    # --- Closing and carrying forward ---

    def test_closing_list_preserves_historical_contents_and_statuses(self):
        self.list_item_a.cancelled = True
        self.list_item_a.save()
        add_test_units(self.list_item_a, 1)
        old_list = self._open_list(self.family_a)
        self.client.force_login(self.member_a)

        response = self._close(self.family_a, old_list.id)

        self.assertRedirects(response, self.url_a("shopping_list"))
        old_list.refresh_from_db()
        self.assertEqual(old_list.status, ShoppingList.Status.CLOSED)
        self.assertIsNotNone(old_list.closed_at)
        self.assertEqual(old_list.closed_by, self.member_a)

        self.list_item_a.refresh_from_db()
        self.assertEqual(self.list_item_a.shopping_list_id, old_list.id)
        self.assertTrue(self.list_item_a.cancelled)
        self.assertEqual(self.list_item_a.quantity_required, 1)
        self.assertFalse(self.list_item_a.current)

    def test_new_open_list_is_created_on_close(self):
        self.list_item_a.cancelled = True
        self.list_item_a.save()
        old_list = self._open_list(self.family_a)
        self.client.force_login(self.member_a)

        self._close(self.family_a, old_list.id)

        new_list = self._open_list(self.family_a)
        self.assertNotEqual(new_list.id, old_list.id)
        self.assertEqual(
            ShoppingList.objects.filter(
                family=self.family_a, status=ShoppingList.Status.OPEN,
            ).count(),
            1,
        )

    def test_closing_without_carrying_forward_cancelled_items(self):
        self.list_item_a.cancelled = True
        self.list_item_a.save()
        old_list = self._open_list(self.family_a)
        self.client.force_login(self.member_a)

        self._close(self.family_a, old_list.id, carry_forward_cancelled=False) # noqa

        new_list = self._open_list(self.family_a)
        self.assertEqual(ListItem.objects.filter(shopping_list=new_list).count(), 0) # noqa

    def test_closing_with_cancelled_items_carried_forward(self):
        self.list_item_a.cancelled = True
        self.list_item_a.creator_notes = "the big box"
        self.list_item_a.save()
        add_test_units(self.list_item_a, 2)
        old_list = self._open_list(self.family_a)
        self.client.force_login(self.member_a)

        self._close(self.family_a, old_list.id, carry_forward_cancelled=True)

        new_list = self._open_list(self.family_a)
        new_item = ListItem.objects.filter(shopping_list=new_list).first()
        self.assertEqual(new_item.product_id, self.product_a.id)
        self.assertEqual(new_item.quantity_required, 1)
        self.assertEqual(ListItem.objects.filter(shopping_list=new_list).count(), 3)
        self.assertEqual(new_item.creator_notes, "the big box")
        # Fresh status: not copied blindly from the historical item.
        self.assertFalse(new_item.cancelled)
        self.assertIsNone(new_item.date_bought)
        self.assertFalse(new_item.in_basket)
        self.assertTrue(new_item.current)

    def test_bought_items_are_never_carried_forward(self):
        self.list_item_a.date_bought = timezone.now()
        self.list_item_a.quantity_bought = 1
        self.list_item_a.save()
        second_item = ListItem.objects.create(
            product=Product.objects.create(
                product_name="Cancelled Product", family=self.family_a,
                category=self.category_a, default_shop=self.shop_a,
                creator=self.admin_a,
            ),
            family=self.family_a, creator=self.admin_a, cancelled=True,
        )
        old_list = self._open_list(self.family_a)
        self.client.force_login(self.member_a)

        self._close(self.family_a, old_list.id, carry_forward_cancelled=True)

        new_list = self._open_list(self.family_a)
        carried_product_ids = set(
            ListItem.objects.filter(
                shopping_list=new_list,
            ).values_list('product_id', flat=True),
        )
        self.assertNotIn(self.product_a.id, carried_product_ids)
        self.assertIn(second_item.product_id, carried_product_ids)

    def test_variant_product_is_preserved_as_a_distinct_product(self):
        variant = Product.objects.create(
            product_name="Product A Variant", family=self.family_a,
            category=self.category_a, default_shop=self.shop_a,
            creator=self.admin_a, based_on=self.product_a,
        )
        self.list_item_a.cancelled = True
        self.list_item_a.save()
        variant_item = ListItem.objects.create(
            product=variant, family=self.family_a,
            creator=self.admin_a, cancelled=True,
        )
        old_list = self._open_list(self.family_a)
        self.client.force_login(self.member_a)

        self._close(self.family_a, old_list.id, carry_forward_cancelled=True)

        new_list = self._open_list(self.family_a)
        new_product_ids = set(
            ListItem.objects.filter(
                shopping_list=new_list,
            ).values_list('product_id', flat=True),
        )
        self.assertEqual(new_product_ids, {self.product_a.id, variant.id})
        del variant_item  # only needed to create the second row

    def test_carry_forward_preserves_multiple_units_for_one_product(self):
        # Not achievable through the normal UI (which prevents this), but
        # the data model doesn't forbid it, so carry-forward must cope.
        extra_item = ListItem.objects.create(
            product=self.product_a, family=self.family_a,
            creator=self.admin_a, cancelled=True,
        )
        add_test_units(extra_item, 1)
        self.list_item_a.cancelled = True
        self.list_item_a.quantity_required = 1
        self.list_item_a.save()
        old_list = self._open_list(self.family_a)
        self.client.force_login(self.member_a)

        self._close(self.family_a, old_list.id, carry_forward_cancelled=True)

        new_list = self._open_list(self.family_a)
        new_items = ListItem.objects.filter(
            shopping_list=new_list, product=self.product_a,
        )
        self.assertEqual(new_items.count(), 3)
        self.assertTrue(all(item.quantity_required == 1 for item in new_items))
        del extra_item

    def test_repeated_close_requests_are_idempotent(self):
        self.list_item_a.cancelled = True
        self.list_item_a.save()
        old_list = self._open_list(self.family_a)
        self.client.force_login(self.member_a)

        first = self._close(
            self.family_a, old_list.id, carry_forward_cancelled=True,
        )
        second = self._close(
            self.family_a, old_list.id, carry_forward_cancelled=True,
        )

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(
            ShoppingList.objects.filter(family=self.family_a).count(), 2,
        )
        self.assertEqual(
            ShoppingList.objects.filter(
                family=self.family_a, status=ShoppingList.Status.OPEN,
            ).count(),
            1,
        )
        new_list = self._open_list(self.family_a)
        self.assertEqual(
            ListItem.objects.filter(
                shopping_list=new_list, product=self.product_a,
            ).count(),
            1,
        )

    def test_closing_a_list_with_outstanding_unpurchased_items_is_allowed(self): # noqa
        # self.list_item_a is left in its default state: not bought, not
        # cancelled. The user should not be forced to cancel it just to
        # be allowed to close the list.
        open_list = self._open_list(self.family_a)
        self.client.force_login(self.member_a)

        response = self._close(self.family_a, open_list.id)

        self.assertRedirects(response, self.url_a("shopping_list"))
        open_list.refresh_from_db()
        self.assertEqual(open_list.status, ShoppingList.Status.CLOSED)
        self.list_item_a.refresh_from_db()
        self.assertEqual(self.list_item_a.shopping_list_id, open_list.id)
        self.assertFalse(self.list_item_a.current)
        self.assertFalse(self.list_item_a.cancelled)
        self.assertIsNone(self.list_item_a.date_bought)

        new_list = self._open_list(self.family_a)
        self.assertEqual(
            ListItem.objects.filter(shopping_list=new_list).count(), 0,
        )

    def test_closing_an_empty_list_is_allowed(self):
        self.list_item_a.delete()
        open_list = self._open_list(self.family_a)
        self.assertEqual(
            ListItem.objects.filter(shopping_list=open_list).count(), 0,
        )
        self.client.force_login(self.member_a)

        response = self._close(self.family_a, open_list.id)

        self.assertRedirects(response, self.url_a("shopping_list"))
        open_list.refresh_from_db()
        self.assertEqual(open_list.status, ShoppingList.Status.CLOSED)
        new_list = self._open_list(self.family_a)
        self.assertNotEqual(new_list.id, open_list.id)
        self.assertEqual(
            ListItem.objects.filter(shopping_list=new_list).count(), 0,
        )

    def test_closing_with_unpurchased_items_carried_forward(self):
        self.list_item_a.creator_notes = "the small box"
        self.list_item_a.save()
        add_test_units(self.list_item_a, 3)
        old_list = self._open_list(self.family_a)
        self.client.force_login(self.member_a)

        self._close(
            self.family_a, old_list.id, carry_forward_unpurchased=True,
        )

        new_list = self._open_list(self.family_a)
        new_item = ListItem.objects.filter(shopping_list=new_list).first()
        self.assertEqual(new_item.product_id, self.product_a.id)
        self.assertEqual(new_item.quantity_required, 1)
        self.assertEqual(ListItem.objects.filter(shopping_list=new_list).count(), 4)
        self.assertEqual(new_item.creator_notes, "the small box")
        # Fresh status: not copied blindly from the historical item.
        self.assertFalse(new_item.cancelled)
        self.assertIsNone(new_item.date_bought)
        self.assertFalse(new_item.in_basket)
        self.assertTrue(new_item.current)

    # --- Independent carry-forward combinations ---

    def _make_mixed_list(self):
        """Gives family_a's current open list one unpurchased
        (uncancelled) item and one separate cancelled item, as distinct
        products, and returns (unpurchased_item, cancelled_item)."""
        self.list_item_a.quantity_required = 1
        self.list_item_a.save()
        cancelled_product = Product.objects.create(
            product_name="Cancelled Product", family=self.family_a,
            category=self.category_a, default_shop=self.shop_a,
            creator=self.admin_a,
        )
        cancelled_item = ListItem.objects.create(
            product=cancelled_product, family=self.family_a,
            creator=self.admin_a, cancelled=True,
        )
        add_test_units(cancelled_item, 1)
        return self.list_item_a, cancelled_item

    def test_closing_with_neither_carry_forward_option_carries_nothing(self): # noqa
        self._make_mixed_list()
        old_list = self._open_list(self.family_a)
        self.client.force_login(self.member_a)

        self._close(self.family_a, old_list.id)

        new_list = self._open_list(self.family_a)
        self.assertEqual(
            ListItem.objects.filter(shopping_list=new_list).count(), 0,
        )

    def test_closing_with_only_unpurchased_carried_forward(self):
        unpurchased_item, cancelled_item = self._make_mixed_list()
        old_list = self._open_list(self.family_a)
        self.client.force_login(self.member_a)

        self._close(
            self.family_a, old_list.id, carry_forward_unpurchased=True,
        )

        new_list = self._open_list(self.family_a)
        carried_product_ids = set(
            ListItem.objects.filter(
                shopping_list=new_list,
            ).values_list('product_id', flat=True),
        )
        self.assertEqual(carried_product_ids, {unpurchased_item.product_id})
        self.assertNotIn(cancelled_item.product_id, carried_product_ids)

    def test_closing_with_only_cancelled_carried_forward(self):
        unpurchased_item, cancelled_item = self._make_mixed_list()
        old_list = self._open_list(self.family_a)
        self.client.force_login(self.member_a)

        self._close(
            self.family_a, old_list.id, carry_forward_cancelled=True,
        )

        new_list = self._open_list(self.family_a)
        carried_product_ids = set(
            ListItem.objects.filter(
                shopping_list=new_list,
            ).values_list('product_id', flat=True),
        )
        self.assertEqual(carried_product_ids, {cancelled_item.product_id})
        self.assertNotIn(unpurchased_item.product_id, carried_product_ids)

    def test_closing_with_both_carried_forward(self):
        unpurchased_item, cancelled_item = self._make_mixed_list()
        old_list = self._open_list(self.family_a)
        self.client.force_login(self.member_a)

        self._close(
            self.family_a, old_list.id,
            carry_forward_unpurchased=True, carry_forward_cancelled=True,
        )

        new_list = self._open_list(self.family_a)
        carried_product_ids = set(
            ListItem.objects.filter(
                shopping_list=new_list,
            ).values_list('product_id', flat=True),
        )
        self.assertEqual(
            carried_product_ids,
            {unpurchased_item.product_id, cancelled_item.product_id},
        )
        # Both carried items get fresh, active status regardless of
        # which bucket they came from.
        for item in ListItem.objects.filter(shopping_list=new_list):
            self.assertFalse(item.cancelled)
            self.assertIsNone(item.date_bought)
            self.assertFalse(item.in_basket)
            self.assertTrue(item.current)

    def test_bought_items_are_never_carried_forward_even_with_both_options(self): # noqa
        self.list_item_a.date_bought = timezone.now()
        self.list_item_a.quantity_bought = 1
        self.list_item_a.save()
        unpurchased_item = ListItem.objects.create(
            product=Product.objects.create(
                product_name="Unpurchased Product", family=self.family_a,
                category=self.category_a, default_shop=self.shop_a,
                creator=self.admin_a,
            ),
            family=self.family_a, creator=self.admin_a,
        )
        cancelled_item = ListItem.objects.create(
            product=Product.objects.create(
                product_name="Cancelled Product", family=self.family_a,
                category=self.category_a, default_shop=self.shop_a,
                creator=self.admin_a,
            ),
            family=self.family_a, creator=self.admin_a, cancelled=True,
        )
        old_list = self._open_list(self.family_a)
        self.client.force_login(self.member_a)

        self._close(
            self.family_a, old_list.id,
            carry_forward_unpurchased=True, carry_forward_cancelled=True,
        )

        new_list = self._open_list(self.family_a)
        carried_product_ids = set(
            ListItem.objects.filter(
                shopping_list=new_list,
            ).values_list('product_id', flat=True),
        )
        self.assertNotIn(self.product_a.id, carried_product_ids)
        self.assertIn(unpurchased_item.product_id, carried_product_ids)
        self.assertIn(cancelled_item.product_id, carried_product_ids)

    def test_closing_someone_elses_family_list_id_is_a_safe_no_op(self):
        self.list_item_a.cancelled = True
        self.list_item_a.save()
        open_list_a = self._open_list(self.family_a)
        open_list_b = self._open_list(self.family_b)
        self.client.force_login(self.member_a)

        # member_a submits family B's list id while acting within family
        # A's own URL; the view only ever operates on family A's actual
        # open list, so this can't touch or close family B's list.
        response = self._close(self.family_a, open_list_b.id)

        self.assertRedirects(response, self.url_a("shopping_list"))
        open_list_a.refresh_from_db()
        open_list_b.refresh_from_db()
        self.assertEqual(open_list_a.status, ShoppingList.Status.OPEN)
        self.assertEqual(open_list_b.status, ShoppingList.Status.OPEN)

    def test_cross_family_close_request_is_rejected(self):
        self.client.force_login(self.member_a)
        response = self.client.post(
            self.url_b("close_shopping_list"),
            {"shopping_list_id": self._open_list(self.family_b).id},
        )
        self.assertEqual(response.status_code, 404)

    # --- Closed lists can't be modified through normal endpoints ---

    def test_closed_list_item_cannot_be_cancelled(self):
        self.list_item_a.cancelled = True
        self.list_item_a.save()
        old_list = self._open_list(self.family_a)
        self.client.force_login(self.member_a)
        self._close(self.family_a, old_list.id)

        response = self.client.post(self.url_a("shopping_list"), {
            "item_id": self.list_item_a.id, "action": "uncancel",
        })

        self.assertEqual(response.status_code, 400)
        self.list_item_a.refresh_from_db()
        self.assertTrue(self.list_item_a.cancelled)

    def test_closed_list_item_cannot_be_added_to_basket(self):
        # Buy the item first so the list qualifies as complete, then
        # close it, then try to act on the now-historical item.
        self.list_item_a.date_bought = timezone.now()
        self.list_item_a.quantity_bought = 1
        self.list_item_a.save()
        old_list = self._open_list(self.family_a)
        self.client.force_login(self.member_a)
        self._close(self.family_a, old_list.id)

        response = self.client.post(self.url_a("shopping_list"), {
            "item_id": self.list_item_a.id, "action": "add_to_basket",
        })

        self.assertEqual(response.status_code, 400)


class FamilyMembershipPermissionsTests(TwoFamilyTestCase):
    """Covers the six granular FamilyMembership permissions and the
    always-full admin authority that overrides them. Direct client.post
    calls throughout (not UI interaction), so every "denied" assertion
    here also demonstrates that a direct bypass attempt is rejected, not
    merely hidden in the UI."""

    def _set_member_a_flags(self, **flags):
        FamilyMembership.objects.filter(
            family=self.family_a, user=self.member_a,
        ).update(**flags)

    def test_six_permission_fields_have_correct_defaults(self):
        user = User.objects.create_user(username="fresh_member")
        membership = FamilyMembership.objects.create(
            family=self.family_a, user=user, display_name="Fresh Member",
        )
        # can_add_list/can_remove_list preserve ordinary members'
        # pre-existing, unrestricted shopping-list access.
        for field in ("can_add_list", "can_remove_list"):
            self.assertTrue(getattr(membership, field), field)
        # The catalogue/shop flags default False: those operations were
        # admin-only before this feature existed, so a new member must
        # not gain them automatically.
        for field in (
            "can_add_catalogue", "can_remove_catalogue",
            "can_add_shops", "can_remove_shops",
        ):
            self.assertFalse(getattr(membership, field), field)

    def test_existing_membership_retains_shopping_list_access(self):
        # member_a comes from the base fixture with no explicit flags:
        # the migration's default=True must mean they can still add to
        # and remove from the shopping list, exactly as before this
        # feature existed.
        self.client.force_login(self.member_a)
        fresh_product = Product.objects.create(
            product_name="Fresh Product A", family=self.family_a,
            category=self.category_a, default_shop=self.shop_a,
            creator=self.admin_a,
        )

        add_response = self.client.post(self.url_a("add_to_shopping_list"), {
            "selected-products": [fresh_product.id],
        })
        self.assertRedirects(add_response, self.url_a("shopping_list"))

        cancel_response = self.client.post(self.url_a("shopping_list"), {
            "item_id": self.list_item_a.id, "action": "cancel",
        })
        self.assertEqual(cancel_response.status_code, 200)
        self.list_item_a.refresh_from_db()
        self.assertTrue(self.list_item_a.cancelled)

    def test_can_add_list_allows_and_denies_adding_to_shopping_list(self):
        fresh_product = Product.objects.create(
            product_name="Gate Add List Product", family=self.family_a,
            category=self.category_a, default_shop=self.shop_a,
            creator=self.admin_a,
        )
        self._set_member_a_flags(can_add_list=False)
        self.client.force_login(self.member_a)

        denied = self.client.post(self.url_a("add_to_shopping_list"), {
            "selected-products": [fresh_product.id],
        })
        self.assertEqual(denied.status_code, 403)
        self.assertFalse(ListItem.objects.filter(
            family=self.family_a, product=fresh_product,
        ).exists())

        self._set_member_a_flags(can_add_list=True)
        allowed = self.client.post(self.url_a("add_to_shopping_list"), {
            "selected-products": [fresh_product.id],
        })
        self.assertRedirects(allowed, self.url_a("shopping_list"))
        self.assertTrue(ListItem.objects.filter(
            family=self.family_a, product=fresh_product,
        ).exists())

    def test_can_remove_list_allows_and_denies_removing_from_shopping_list(self): # noqa
        self._set_member_a_flags(can_remove_list=False)
        self.client.force_login(self.member_a)

        denied = self.client.post(self.url_a("shopping_list"), {
            "item_id": self.list_item_a.id, "action": "cancel",
        })
        self.assertEqual(denied.status_code, 403)
        self.list_item_a.refresh_from_db()
        self.assertFalse(self.list_item_a.cancelled)

        self._set_member_a_flags(can_remove_list=True)
        allowed = self.client.post(self.url_a("shopping_list"), {
            "item_id": self.list_item_a.id, "action": "cancel",
        })
        self.assertEqual(allowed.status_code, 200)
        self.list_item_a.refresh_from_db()
        self.assertTrue(self.list_item_a.cancelled)

    def test_can_add_catalogue_allows_and_denies_adding_product(self):
        self._set_member_a_flags(can_add_catalogue=False)
        self.client.force_login(self.member_a)

        denied = self.client.post(self.url_a("add_product"), {
            "product_name": "Gated Product", "category": self.category_a.pk,
            "default_shop": self.shop_a.pk, "default_quantity": 1,
            "default_unit": "item",
        })
        self.assertEqual(denied.status_code, 403)
        self.assertFalse(Product.objects.filter(
            family=self.family_a, product_name="Gated Product",
        ).exists())

        self._set_member_a_flags(can_add_catalogue=True)
        allowed = self.client.post(self.url_a("add_product"), {
            "product_name": "Gated Product", "category": self.category_a.pk,
            "default_shop": self.shop_a.pk, "default_quantity": 1,
            "default_unit": "item",
        })
        self.assertRedirects(allowed, self.url_a("product_list"))
        self.assertTrue(Product.objects.filter(
            family=self.family_a, product_name="Gated Product",
        ).exists())

    def test_can_remove_catalogue_allows_and_denies_deactivating_product(self): # noqa
        self._set_member_a_flags(can_remove_catalogue=False)
        self.client.force_login(self.member_a)

        denied = self.client.post(
            self.url_a("product_delete", slug=self.product_a.slug),
        )
        self.assertEqual(denied.status_code, 403)
        self.product_a.refresh_from_db()
        self.assertTrue(self.product_a.current)

        self._set_member_a_flags(can_remove_catalogue=True)
        allowed = self.client.post(
            self.url_a("product_delete", slug=self.product_a.slug),
        )
        self.assertRedirects(allowed, self.url_a("product_list"))
        self.product_a.refresh_from_db()
        self.assertFalse(self.product_a.current)

    def test_can_add_shops_allows_and_denies_adding_shop(self):
        self._set_member_a_flags(can_add_shops=False)
        self.client.force_login(self.member_a)

        denied = self.client.post(self.url_a("add_shop"), {
            "shop_name": "Gated Shop", "type_of_shop": 1,
        })
        self.assertEqual(denied.status_code, 403)
        self.assertFalse(Shop.objects.filter(
            family=self.family_a, shop_name="Gated Shop",
        ).exists())

        self._set_member_a_flags(can_add_shops=True)
        allowed = self.client.post(self.url_a("add_shop"), {
            "shop_name": "Gated Shop", "type_of_shop": 1,
        })
        self.assertRedirects(allowed, self.url_a("shop_list"))
        self.assertTrue(Shop.objects.filter(
            family=self.family_a, shop_name="Gated Shop",
        ).exists())

    def test_can_remove_shops_allows_and_denies_deleting_shop(self):
        other_shop = Shop.objects.create(
            shop_name="Gated Removable Shop", type_of_shop=1,
            family=self.family_a, creator=self.admin_a,
        )
        self._set_member_a_flags(can_remove_shops=False)
        self.client.force_login(self.member_a)

        denied = self.client.post(
            self.url_a("shop_delete", slug=other_shop.slug),
        )
        self.assertEqual(denied.status_code, 403)
        other_shop.refresh_from_db()
        self.assertTrue(other_shop.current)

        self._set_member_a_flags(can_remove_shops=True)
        allowed = self.client.post(
            self.url_a("shop_delete", slug=other_shop.slug),
        )
        self.assertRedirects(allowed, self.url_a("shop_list"))
        other_shop.refresh_from_db()
        self.assertFalse(other_shop.current)

    def test_admin_retains_full_access_with_all_six_flags_false(self):
        FamilyMembership.objects.filter(
            family=self.family_a, user=self.admin_a,
        ).update(
            can_add_list=False, can_remove_list=False,
            can_add_catalogue=False, can_remove_catalogue=False,
            can_add_shops=False, can_remove_shops=False,
        )
        self.client.force_login(self.admin_a)

        add_product = self.client.post(self.url_a("add_product"), {
            "product_name": "Admin Override Product",
            "category": self.category_a.pk, "default_shop": self.shop_a.pk,
            "default_quantity": 1, "default_unit": "item",
        })
        self.assertRedirects(add_product, self.url_a("product_list"))

        add_shop = self.client.post(self.url_a("add_shop"), {
            "shop_name": "Admin Override Shop", "type_of_shop": 1,
        })
        self.assertRedirects(add_shop, self.url_a("shop_list"))

        cancel = self.client.post(self.url_a("shopping_list"), {
            "item_id": self.list_item_a.id, "action": "cancel",
        })
        self.assertEqual(cancel.status_code, 200)

    def test_catalogue_add_and_list_add_permissions_are_independent(self):
        # can_add_catalogue=True must not grant can_add_list, and
        # vice versa: creating a catalogue product and adding an
        # existing product to the shopping list are governed separately.
        fresh_product = Product.objects.create(
            product_name="Independence Product", family=self.family_a,
            category=self.category_a, default_shop=self.shop_a,
            creator=self.admin_a,
        )
        self._set_member_a_flags(can_add_catalogue=True, can_add_list=False)
        self.client.force_login(self.member_a)

        list_add_denied = self.client.post(
            self.url_a("add_to_shopping_list"),
            {"selected-products": [fresh_product.id]},
        )
        self.assertEqual(list_add_denied.status_code, 403)

        catalogue_add_allowed = self.client.post(self.url_a("add_product"), {
            "product_name": "Independently Added Product",
            "category": self.category_a.pk, "default_shop": self.shop_a.pk,
            "default_quantity": 1, "default_unit": "item",
        })
        self.assertRedirects(
            catalogue_add_allowed, self.url_a("product_list"),
        )

    def test_reading_and_authentication_available_with_all_flags_false(self):
        self._set_member_a_flags(
            can_add_list=False, can_remove_list=False,
            can_add_catalogue=False, can_remove_catalogue=False,
            can_add_shops=False, can_remove_shops=False,
        )
        self.client.force_login(self.member_a)

        for url_name in (
            "shopping_list", "product_list", "shop_list", "category_list",
        ):
            self.assertEqual(
                self.client.get(self.url_a(url_name)).status_code, 200,
                url_name,
            )
        self.assertEqual(
            self.client.get(reverse("account_change_password")).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(reverse("account_logout")).status_code, 302,
        )

    def test_ordinary_member_cannot_alter_other_members_basket_ownership(self): # noqa
        # can_remove_list must not extend to purchasing, returning, or
        # otherwise taking ownership of another member's basket unit -
        # that remains governed purely by basket-ownership rules.
        self._set_member_a_flags(can_remove_list=True)
        self.client.force_login(self.admin_a)
        self.client.post(self.url_a("shopping_list"), {
            "item_id": self.list_item_a.id, "action": "add_to_basket",
        })
        self.list_item_a.refresh_from_db()
        self.assertEqual(self.list_item_a.put_in_basket_by_id, self.admin_a.id) # noqa

        self.client.force_login(self.member_a)
        remove_from_basket = self.client.post(self.url_a("shopping_list"), {
            "item_id": self.list_item_a.id, "action": "remove_from_basket",
        })
        self.assertEqual(remove_from_basket.status_code, 403)
        self.list_item_a.refresh_from_db()
        self.assertTrue(self.list_item_a.in_basket)
        self.assertEqual(self.list_item_a.put_in_basket_by_id, self.admin_a.id) # noqa

        # cancelling a basket item is already blocked for everyone,
        # admins included (see the final report for this known gap).
        cancel_basket_item = self.client.post(self.url_a("shopping_list"), {
            "item_id": self.list_item_a.id, "action": "cancel",
        })
        self.assertEqual(cancel_basket_item.status_code, 400)
        self.list_item_a.refresh_from_db()
        self.assertFalse(self.list_item_a.cancelled)

    def test_demoting_one_of_two_admins_is_allowed(self):
        User.objects.create_user(username="second_admin_demote")
        FamilyMembership.objects.create(
            family=self.family_a, user=User.objects.get(
                username="second_admin_demote",
            ), role=FamilyMembership.Role.ADMIN,
            display_name="Second Admin",
        )

        original_membership = FamilyMembership.objects.get(
            family=self.family_a, user=self.admin_a,
        )
        original_membership.role = FamilyMembership.Role.MEMBER
        original_membership.save(update_fields=["role"])

        original_membership.refresh_from_db()
        self.assertEqual(original_membership.role, FamilyMembership.Role.MEMBER) # noqa

    def test_removing_one_of_two_admins_is_allowed(self):
        second_admin = User.objects.create_user(username="second_admin_remove") # noqa
        second_membership = FamilyMembership.objects.create(
            family=self.family_a, user=second_admin,
            role=FamilyMembership.Role.ADMIN, display_name="Second Admin",
        )

        second_membership.delete()

        self.assertFalse(FamilyMembership.objects.filter(
            pk=second_membership.pk,
        ).exists())
        # The original admin, untouched, is still there to keep the
        # family administered.
        self.assertTrue(FamilyMembership.objects.filter(
            family=self.family_a, user=self.admin_a,
            role=FamilyMembership.Role.ADMIN,
        ).exists())

    def test_demoting_or_removing_the_last_admin_is_rejected(self):
        membership = FamilyMembership.objects.get(
            family=self.family_a, user=self.admin_a,
        )

        membership.role = FamilyMembership.Role.MEMBER
        with self.assertRaises(ValidationError):
            membership.save(update_fields=["role"])
        membership.refresh_from_db()
        self.assertEqual(membership.role, FamilyMembership.Role.ADMIN)

        with self.assertRaises(ValidationError):
            membership.delete()
        self.assertTrue(FamilyMembership.objects.filter(
            pk=membership.pk,
        ).exists())

    def test_permission_checks_are_scoped_to_the_current_family(self):
        # The SAME user has independent flags per family: a restriction
        # in family_a must not bleed into family_b.
        FamilyMembership.objects.create(
            family=self.family_b, user=self.member_a,
            display_name="Member A in Family B",
            can_add_catalogue=True,
        )
        self._set_member_a_flags(can_add_catalogue=False)
        self.client.force_login(self.member_a)

        denied_in_a = self.client.post(self.url_a("add_product"), {
            "product_name": "Scoped Product", "category": self.category_a.pk,
            "default_shop": self.shop_a.pk, "default_quantity": 1,
            "default_unit": "item",
        })
        self.assertEqual(denied_in_a.status_code, 403)

        allowed_in_b = self.client.post(self.url_b("add_product"), {
            "product_name": "Scoped Product", "category": self.category_b.pk,
            "default_shop": self.shop_b.pk, "default_quantity": 1,
            "default_unit": "item",
        })
        self.assertRedirects(allowed_in_b, self.url_b("product_list"))

    def test_seed_import_requires_can_add_catalogue(self):
        from .seed_lists import load_seed_lists

        seed_id = next(iter(load_seed_lists()))
        seed = load_seed_lists()[seed_id]
        product_name = seed["categories"][0]["products"][0]

        self._set_member_a_flags(can_add_catalogue=False)
        self.client.force_login(self.member_a)

        denied = self.client.post(self.url_a("seed_lists"), {
            "seed": seed_id, "products": [product_name], "action": "import",
        })
        self.assertEqual(denied.status_code, 403)
        self.assertFalse(Product.objects.filter(
            family=self.family_a, product_name=product_name,
        ).exists())


class WebSocketMutationRejectionTests(
    TwoFamilyFixtureMixin, TransactionTestCase,
):
    """WebSocket item #13 of the permissions test plan: the consumer
    never acts on client payloads (see consumers.py's no-op receive()),
    so there is no mutation entry point to bypass these permissions
    through."""

    async def test_websocket_cannot_perform_protected_mutations(self):
        communicator = WebsocketCommunicator(
            ShoppingConsumer.as_asgi(), ws_path(self.family_a),
        )
        communicator.scope["user"] = self.member_a
        communicator.scope["url_route"] = {
            "kwargs": {"family_slug": self.family_a.slug},
        }
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        # A crafted "mutation" payload sent over the socket must have no
        # effect, since all mutations go through the HTTP views.
        await communicator.send_json_to({
            "action": "cancel", "item_id": self.list_item_a.id,
        })
        self.assertTrue(await communicator.receive_nothing(timeout=0.2))

        item = await sync_to_async(ListItem.objects.get)(
            pk=self.list_item_a.id,
        )
        self.assertFalse(item.cancelled)

        await communicator.disconnect()


class FamilyMembersViewTests(TwoFamilyTestCase):
    """The minimal permission-management UI: FamilyMembersView."""

    def test_admin_can_view_and_update_a_members_permissions(self):
        self.client.force_login(self.admin_a)

        page = self.client.get(self.url_a("family_members"))
        self.assertEqual(page.status_code, 200)

        member_membership = FamilyMembership.objects.get(
            family=self.family_a, user=self.member_a,
        )
        response = self.client.post(self.url_a("family_members"), {
            "membership_id": member_membership.pk, "action": "update",
            "role": FamilyMembership.Role.MEMBER,
            "can_add_catalogue": "on",
        })
        self.assertRedirects(response, self.url_a("family_members"))
        member_membership.refresh_from_db()
        self.assertTrue(member_membership.can_add_catalogue)
        self.assertFalse(member_membership.can_add_list)

    def test_admin_can_remove_a_member_via_the_view(self):
        self.client.force_login(self.admin_a)
        member_membership = FamilyMembership.objects.get(
            family=self.family_a, user=self.member_a,
        )

        response = self.client.post(self.url_a("family_members"), {
            "membership_id": member_membership.pk, "action": "remove",
        })

        self.assertRedirects(response, self.url_a("family_members"))
        self.assertFalse(FamilyMembership.objects.filter(
            pk=member_membership.pk,
        ).exists())

    def test_non_admin_cannot_access_the_members_view(self):
        self.client.force_login(self.member_a)

        self.assertEqual(
            self.client.get(self.url_a("family_members")).status_code, 403,
        )
        admin_membership = FamilyMembership.objects.get(
            family=self.family_a, user=self.admin_a,
        )
        post_response = self.client.post(self.url_a("family_members"), {
            "membership_id": admin_membership.pk, "action": "remove",
        })
        self.assertEqual(post_response.status_code, 403)
        self.assertTrue(FamilyMembership.objects.filter(
            pk=admin_membership.pk,
        ).exists())

    def test_view_rejects_demoting_or_removing_the_last_admin(self):
        self.client.force_login(self.admin_a)
        admin_membership = FamilyMembership.objects.get(
            family=self.family_a, user=self.admin_a,
        )

        demote_response = self.client.post(self.url_a("family_members"), {
            "membership_id": admin_membership.pk, "action": "update",
            "role": FamilyMembership.Role.MEMBER,
        })
        self.assertRedirects(demote_response, self.url_a("family_members"))
        admin_membership.refresh_from_db()
        self.assertEqual(
            admin_membership.role, FamilyMembership.Role.ADMIN,
        )

        remove_response = self.client.post(self.url_a("family_members"), {
            "membership_id": admin_membership.pk, "action": "remove",
        })
        self.assertRedirects(remove_response, self.url_a("family_members"))
        self.assertTrue(FamilyMembership.objects.filter(
            pk=admin_membership.pk,
        ).exists())

    def test_view_rejects_a_membership_id_from_another_family(self):
        self.client.force_login(self.admin_a)
        other_family_membership = FamilyMembership.objects.get(
            family=self.family_b, user=self.admin_b,
        )

        response = self.client.post(self.url_a("family_members"), {
            "membership_id": other_family_membership.pk, "action": "remove",
        })

        self.assertEqual(response.status_code, 404)
        self.assertTrue(FamilyMembership.objects.filter(
            pk=other_family_membership.pk,
        ).exists())

    def test_admin_rows_show_permissions_checked_and_disabled(self):
        self.client.force_login(self.admin_a)
        admin_membership = FamilyMembership.objects.get(
            family=self.family_a, user=self.admin_a,
        )
        member_membership = FamilyMembership.objects.get(
            family=self.family_a, user=self.member_a,
        )
        # Fixture default for an admin membership: can_add_list and
        # can_remove_list True, the other four False -- a deliberate mix
        # so the hidden-input checks below can't pass by accident if the
        # value were hardcoded to always be "on".
        true_fields = ["can_add_list", "can_remove_list"]
        false_fields = [
            "can_add_catalogue", "can_remove_catalogue",
            "can_add_shops", "can_remove_shops",
        ]

        page = self.client.get(self.url_a("family_members"))

        # Admin row: all six of the SIX permission fields are shown
        # checked AND disabled (editing them has no effect while this
        # member remains an administrator) ...
        for field in true_fields + false_fields:
            self.assertContains(
                page, f'id="{field}-{admin_membership.pk}" checked disabled', # noqa
            )
            # ... while the matching hidden input carries the
            # membership's ACTUAL stored value, not an assumed True, so
            # that value survives unchanged if this admin is demoted.
            expected_hidden_value = "on" if field in true_fields else ""
            self.assertContains(
                page,
                f'<input type="hidden" name="{field}" '
                f'value="{expected_hidden_value}">',
            )

        # Ordinary member row: unaffected -- every field still reflects
        # its real stored value and remains editable (not disabled).
        for field in true_fields + false_fields:
            self.assertContains(
                page, f'id="{field}-{member_membership.pk}"',
            )
            self.assertNotContains(
                page, f'id="{field}-{member_membership.pk}" checked disabled', # noqa
            )

    def test_demoting_an_admin_resets_their_permission_flags_to_defaults(self): # noqa
        self.client.force_login(self.admin_a)
        second_admin = User.objects.create_user(
            username="second_admin_a", password="testpass123",
        )
        second_admin_membership = FamilyMembership.objects.create(
            family=self.family_a, user=second_admin,
            role=FamilyMembership.Role.ADMIN, display_name="Second Admin",
        )
        # Deliberately NON-default stored flags -- anything an admin's
        # flags happen to hold is irrelevant while they're an admin
        # (see has_family_permission), so this proves the demotion
        # RESETS to the standard ordinary-member defaults rather than
        # merely preserving whatever was stored before.
        second_admin_membership.can_add_list = False
        second_admin_membership.can_remove_list = False
        second_admin_membership.can_add_catalogue = True
        second_admin_membership.can_remove_catalogue = True
        second_admin_membership.can_add_shops = True
        second_admin_membership.can_remove_shops = True
        second_admin_membership.save()

        # Mirrors what the admin-row template actually submits for the
        # disabled-but-hidden-mirrored checkboxes: the same (non-default)
        # stored values set above. The model must override these on an
        # actual demotion, not merely pass them through.
        response = self.client.post(self.url_a("family_members"), {
            "membership_id": second_admin_membership.pk, "action": "update",
            "role": FamilyMembership.Role.MEMBER,
            "can_add_list": "", "can_remove_list": "",
            "can_add_catalogue": "on", "can_remove_catalogue": "on",
            "can_add_shops": "on", "can_remove_shops": "on",
        })

        self.assertRedirects(response, self.url_a("family_members"))
        second_admin_membership.refresh_from_db()
        self.assertEqual(
            second_admin_membership.role, FamilyMembership.Role.MEMBER,
        )
        # Reset to the standard ordinary-member defaults -- NOT the
        # stale admin-era values submitted above.
        self.assertTrue(second_admin_membership.can_add_list)
        self.assertTrue(second_admin_membership.can_remove_list)
        self.assertFalse(second_admin_membership.can_add_catalogue)
        self.assertFalse(second_admin_membership.can_remove_catalogue)
        self.assertFalse(second_admin_membership.can_add_shops)
        self.assertFalse(second_admin_membership.can_remove_shops)

    def test_promoting_a_member_without_a_verified_email_is_rejected(self): # noqa
        self.client.force_login(self.admin_a)
        # member_a (from TwoFamilyFixtureMixin) has no email at all.
        response = self.client.post(self.url_a("family_members"), {
            "membership_id": FamilyMembership.objects.get(
                family=self.family_a, user=self.member_a,
            ).pk,
            "action": "update",
            "role": FamilyMembership.Role.ADMIN,
            "can_add_list": "on", "can_remove_list": "on",
        })

        self.assertRedirects(response, self.url_a("family_members"))
        membership = FamilyMembership.objects.get(
            family=self.family_a, user=self.member_a,
        )
        self.assertEqual(membership.role, FamilyMembership.Role.MEMBER)

    def test_promoting_a_member_with_a_verified_email_succeeds(self): # noqa
        self.client.force_login(self.admin_a)
        EmailAddress.objects.create(
            user=self.member_a, email="member_a@example.com",
            verified=True, primary=True,
        )

        response = self.client.post(self.url_a("family_members"), {
            "membership_id": FamilyMembership.objects.get(
                family=self.family_a, user=self.member_a,
            ).pk,
            "action": "update",
            "role": FamilyMembership.Role.ADMIN,
            "can_add_list": "on", "can_remove_list": "on",
        })

        self.assertRedirects(response, self.url_a("family_members"))
        membership = FamilyMembership.objects.get(
            family=self.family_a, user=self.member_a,
        )
        self.assertEqual(membership.role, FamilyMembership.Role.ADMIN)

    def test_role_dropdown_disables_admin_option_without_a_verified_email(self): # noqa
        self.client.force_login(self.admin_a)
        member_membership = FamilyMembership.objects.get(
            family=self.family_a, user=self.member_a,
        )

        page = self.client.get(self.url_a("family_members"))

        self.assertContains(
            page,
            f'value="admin"  disabled>Family admin',
        )
        self.assertContains(
            page, f'id="id_role_{member_membership.pk}"',
        )

    def test_role_dropdown_enables_admin_option_with_a_verified_email(self): # noqa
        EmailAddress.objects.create(
            user=self.member_a, email="member_a2@example.com",
            verified=True, primary=True,
        )
        self.client.force_login(self.admin_a)

        page = self.client.get(self.url_a("family_members"))

        self.assertNotContains(page, 'value="admin"  disabled>Family admin') # noqa

    def test_sole_admins_role_control_is_disabled_in_the_template(self): # noqa
        self.client.force_login(self.admin_a)
        admin_membership = FamilyMembership.objects.get(
            family=self.family_a, user=self.admin_a,
        )

        page = self.client.get(self.url_a("family_members"))

        self.assertContains(
            page, f'id="id_role_{admin_membership.pk}" class="form-select" disabled', # noqa
        )
        self.assertContains(
            page,
            f'<input type="hidden" name="role" value="{admin_membership.role}">', # noqa
        )


class FamilyMembershipAdminLastAdminProtectionTests(TwoFamilyTestCase):
    """Functional coverage, via the real Django admin HTTP views (not
    just direct model calls), that the last-administrator invariant
    reaches Django admin's single-object change/delete views and the
    bulk "Delete selected" action - not just the custom
    FamilyMembersView."""

    def setUp(self):
        super().setUp()
        self.staff_user = User.objects.create_superuser(
            username="admin_site_staff", email="staff@example.com",
        )
        self.client.force_login(self.staff_user)

    def test_admin_change_form_rejects_demoting_the_last_admin(self):
        membership = FamilyMembership.objects.get(
            family=self.family_a, user=self.admin_a,
        )
        url = reverse(
            "admin:shopping_list_familymembership_change",
            args=[membership.pk],
        )

        response = self.client.post(url, {
            "family": self.family_a.pk, "user": self.admin_a.pk,
            "display_name": membership.display_name,
            "role": FamilyMembership.Role.MEMBER,
        })

        # Django admin re-shows the form (200) rather than redirecting
        # when the save is rejected by validation.
        self.assertEqual(response.status_code, 200)
        membership.refresh_from_db()
        self.assertEqual(membership.role, FamilyMembership.Role.ADMIN)

    def test_admin_delete_view_rejects_removing_the_last_admin(self):
        membership = FamilyMembership.objects.get(
            family=self.family_a, user=self.admin_a,
        )
        url = reverse(
            "admin:shopping_list_familymembership_delete",
            args=[membership.pk],
        )

        # Django admin's delete confirmation view has no built-in
        # handling for a ValidationError raised during delete (unlike
        # form validation, which is caught and redisplayed) - it
        # propagates, which Django's test client re-raises here. The
        # membership is still correctly protected; the known UX
        # limitation (no friendly admin-site message) is documented in
        # the final report rather than worked around with new scope.
        with self.assertRaises(ValidationError):
            self.client.post(url, {"post": "yes"})
        self.assertTrue(FamilyMembership.objects.filter(
            pk=membership.pk,
        ).exists())

    def test_admin_bulk_delete_action_rejects_removing_the_last_admin(self):
        membership = FamilyMembership.objects.get(
            family=self.family_a, user=self.admin_a,
        )
        url = reverse("admin:shopping_list_familymembership_changelist")

        with self.assertRaises(ValidationError):
            self.client.post(url, {
                "action": "delete_selected",
                "_selected_action": [membership.pk],
                "post": "yes",
            })
        self.assertTrue(FamilyMembership.objects.filter(
            pk=membership.pk,
        ).exists())

    def test_admin_change_form_allows_demoting_when_another_admin_remains(self): # noqa
        second_admin = User.objects.create_user(
            username="admin_site_second_admin",
        )
        FamilyMembership.objects.create(
            family=self.family_a, user=second_admin,
            role=FamilyMembership.Role.ADMIN, display_name="Second Admin",
        )
        membership = FamilyMembership.objects.get(
            family=self.family_a, user=self.admin_a,
        )
        url = reverse(
            "admin:shopping_list_familymembership_change",
            args=[membership.pk],
        )

        response = self.client.post(url, {
            "family": self.family_a.pk, "user": self.admin_a.pk,
            "display_name": membership.display_name,
            "role": FamilyMembership.Role.MEMBER,
        })

        self.assertRedirects(
            response,
            reverse("admin:shopping_list_familymembership_changelist"),
        )
        membership.refresh_from_db()
        self.assertEqual(membership.role, FamilyMembership.Role.MEMBER)


class UserAdminDisplayNameTests(TwoFamilyTestCase):
    """shopping_list.admin.UserAdmin: the User changelist shows each
    account's family-local display name(s) alongside its (technical,
    globally-unique) username, for admin/superuser maintenance only."""

    def setUp(self):
        super().setUp()
        self.staff_user = User.objects.create_superuser(
            username="admin_site_staff_display_names",
            email="staff_dn@example.com",
        )
        self.client.force_login(self.staff_user)

    def test_changelist_shows_username_and_display_name(self):
        response = self.client.get(reverse("admin:auth_user_changelist"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Display name(s)")
        # member_a's technical username AND their family-local display
        # name (set to the same string by create_family_with_members,
        # but these are two distinct columns -- not a coincidence being
        # masked here) must both appear.
        self.assertContains(response, self.member_a.username)
        membership = FamilyMembership.objects.get(
            family=self.family_a, user=self.member_a,
        )
        self.assertContains(
            response,
            f"{membership.display_name} ({self.family_a.name})",
        )

    def test_changelist_shows_multiple_display_names_for_one_account(self):
        # The SAME account can go by a different display name in each
        # family it belongs to -- confirm both show up, distinctly.
        FamilyMembership.objects.create(
            family=self.family_b, user=self.member_a, display_name="Other Name", # noqa
        )

        response = self.client.get(reverse("admin:auth_user_changelist"))

        original_membership = FamilyMembership.objects.get(
            family=self.family_a, user=self.member_a,
        )
        self.assertContains(
            response,
            f"{original_membership.display_name} ({self.family_a.name})",
        )
        self.assertContains(
            response, f"Other Name ({self.family_b.name})",
        )

    def test_changelist_shows_a_dash_for_an_account_with_no_membership(self):
        orphan = User.objects.create_user(username="orphan_display_name_test") # noqa

        response = self.client.get(reverse("admin:auth_user_changelist"))

        self.assertContains(response, orphan.username)
        self.assertContains(response, "—")


class LastAdministratorConcurrencyTests(TwoFamilyFixtureMixin, TransactionTestCase): # noqa
    """Proves the family-row lock actually serialises two concurrent
    attempts to change admin status for the SAME family, using two real
    threads with independent DB connections (TransactionTestCase commits
    for real, unlike TestCase's savepoint-wrapped tests, so this is the
    only way to exercise genuine cross-connection blocking)."""

    def test_concurrent_demotions_cannot_both_leave_the_family_adminless(self): # noqa
        second_admin = User.objects.create_user(
            username="concurrent_second_admin",
        )
        FamilyMembership.objects.create(
            family=self.family_a, user=second_admin,
            role=FamilyMembership.Role.ADMIN, display_name="Second Admin",
        )
        first_membership_id = FamilyMembership.objects.get(
            family=self.family_a, user=self.admin_a,
        ).pk
        second_membership_id = FamilyMembership.objects.get(
            family=self.family_a, user=second_admin,
        ).pk

        # Synchronises the two threads so the ordering below is
        # deterministic rather than a timing gamble: thread two only
        # attempts its own lock once thread one confirms it is already
        # holding the family row, and thread one only commits (releasing
        # the lock) once thread two confirms it has started waiting.
        first_thread_has_lock = threading.Event()
        second_thread_is_waiting = threading.Event()
        release_first_thread = threading.Event()
        second_thread_outcome = {}

        def demote_first_admin():
            connections.close_all()
            try:
                with transaction.atomic():
                    lock_family_for_membership_changes(self.family_a.pk)
                    first_thread_has_lock.set()
                    # Give the second thread a bounded window to actually
                    # start blocking on the same row lock before this
                    # thread proceeds to commit.
                    second_thread_is_waiting.wait(timeout=5)
                    release_first_thread.wait(timeout=5)
                    membership = FamilyMembership.objects.get(
                        pk=first_membership_id,
                    )
                    membership.role = FamilyMembership.Role.MEMBER
                    membership.save(update_fields=["role"])
            finally:
                connections.close_all()

        def demote_second_admin():
            connections.close_all()
            try:
                first_thread_has_lock.wait(timeout=5)
                second_thread_is_waiting.set()
                # This blocks on Postgres's row lock until thread one's
                # transaction commits - the actual behaviour under test.
                with transaction.atomic():
                    lock_family_for_membership_changes(self.family_a.pk)
                    membership = FamilyMembership.objects.get(
                        pk=second_membership_id,
                    )
                    membership.role = FamilyMembership.Role.MEMBER
                    membership.save(update_fields=["role"])
                second_thread_outcome["succeeded"] = True
            except ValidationError:
                second_thread_outcome["succeeded"] = False
            finally:
                connections.close_all()

        first_thread = threading.Thread(target=demote_first_admin)
        second_thread = threading.Thread(target=demote_second_admin)

        first_thread.start()
        second_thread.start()
        first_thread_has_lock.wait(timeout=5)
        second_thread_is_waiting.wait(timeout=5)
        release_first_thread.set()

        first_thread.join(timeout=10)
        second_thread.join(timeout=10)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        # Thread one's demotion (while second_admin was still an admin)
        # must have succeeded; thread two's demotion (by then the only
        # remaining admin) must have been rejected by the invariant -
        # exactly the outcome the lock exists to guarantee.
        self.assertIn("succeeded", second_thread_outcome)
        self.assertFalse(second_thread_outcome["succeeded"])
        self.assertEqual(FamilyMembership.objects.filter(
            family=self.family_a, role=FamilyMembership.Role.ADMIN,
        ).count(), 1)
        self.assertTrue(FamilyMembership.objects.filter(
            family=self.family_a, user=second_admin,
            role=FamilyMembership.Role.ADMIN,
        ).exists())


class ShoppingListLifecycleWebSocketTests(
    TwoFamilyFixtureMixin, TransactionTestCase,
):
    async def _connect(self, family, user):
        communicator = WebsocketCommunicator(
            ShoppingConsumer.as_asgi(), ws_path(family),
        )
        communicator.scope["user"] = user
        communicator.scope["url_route"] = {
            "kwargs": {"family_slug": family.slug},
        }
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        return communicator

    async def test_cancelling_the_last_item_broadcasts_list_complete(self):
        communicator = await self._connect(self.family_a, self.member_a)

        try:
            await sync_to_async(self.client.force_login)(self.member_a)
            await sync_to_async(self.client.post)(
                self.url_a("shopping_list"),
                {"item_id": self.list_item_a.id, "action": "cancel"},
            )

            messages = [
                await communicator.receive_json_from() for _ in range(2)
            ]
            complete_messages = [
                m for m in messages if m["type"] == "shopping_list_complete"
            ]
            self.assertEqual(len(complete_messages), 1)

            open_list = await sync_to_async(ShoppingList.objects.get)(
                family=self.family_a, status=ShoppingList.Status.OPEN,
            )
            self.assertEqual(
                complete_messages[0]["shopping_list_id"], open_list.id,
            )
        finally:
            await communicator.disconnect()

    async def test_closing_the_list_broadcasts_replaced_and_new_item(self):
        def prepare():
            self.list_item_a.cancelled = True
            self.list_item_a.save()
            return ShoppingList.objects.get(
                family=self.family_a, status=ShoppingList.Status.OPEN,
            )

        old_list = await sync_to_async(prepare)()
        communicator = await self._connect(self.family_a, self.member_a)

        try:
            await sync_to_async(self.client.force_login)(self.member_a)
            await sync_to_async(self.client.post)(
                self.url_a("close_shopping_list"),
                {
                    "shopping_list_id": old_list.id,
                    "carry_forward_cancelled": "on",
                },
            )

            # One product_list_status (product_a), one list_item_added
            # (the carried-forward item), one shopping_list_replaced.
            messages = [
                await communicator.receive_json_from() for _ in range(3)
            ]
            types = {m["type"] for m in messages}
            self.assertEqual(
                types,
                {
                    'product_list_status', 'list_item_added',
                    'shopping_list_replaced',
                },
            )

            replaced = next(
                m for m in messages if m["type"] == 'shopping_list_replaced'
            )
            self.assertEqual(replaced["closed_list_id"], old_list.id)

            new_list = await sync_to_async(
                lambda: ShoppingList.objects.get(
                    family=self.family_a, status=ShoppingList.Status.OPEN,
                ),
            )()
            self.assertEqual(replaced["new_list_id"], new_list.id)

            added = next(
                m for m in messages if m["type"] == 'list_item_added'
            )
            self.assertEqual(added["product_name"], "Product A")
        finally:
            await communicator.disconnect()

    async def test_closing_a_list_with_outstanding_items_broadcasts_replaced(self): # noqa
        # self.list_item_a is left unpurchased and uncancelled; closing
        # with neither carry-forward option checked should still close
        # the list (no "list still has outstanding items" error) and
        # broadcast the replacement, just without any carried-forward
        # item.
        old_list = await sync_to_async(
            lambda: ShoppingList.objects.get(
                family=self.family_a, status=ShoppingList.Status.OPEN,
            ),
        )()
        communicator = await self._connect(self.family_a, self.member_a)

        try:
            await sync_to_async(self.client.force_login)(self.member_a)
            response = await sync_to_async(self.client.post)(
                self.url_a("close_shopping_list"),
                {"shopping_list_id": old_list.id},
            )
            self.assertEqual(response.status_code, 302)

            # One product_list_status (product_a's on-list quantity
            # changed), one shopping_list_replaced - no list_item_added,
            # since nothing was carried forward.
            messages = [
                await communicator.receive_json_from() for _ in range(2)
            ]
            types = {m["type"] for m in messages}
            self.assertEqual(
                types, {'product_list_status', 'shopping_list_replaced'},
            )

            replaced = next(
                m for m in messages if m["type"] == 'shopping_list_replaced'
            )
            self.assertEqual(replaced["closed_list_id"], old_list.id)
        finally:
            await communicator.disconnect()




class ItemAttributionTests(TwoFamilyTestCase):
    def setUp(self):
        super().setUp()
        self.item2 = ListItem.objects.create(
            product=self.product_a, family=self.family_a,
            creator=self.admin_a, slug="second-item",
        )

    def act(self, user, item, action, family_url_name="shopping_list"):
        self.client.force_login(user)
        return self.client.post(self.url_a(family_url_name), {
            "item_id": item.id, "action": action,
        })

    def page(self, user):
        self.client.force_login(user)
        return self.client.get(self.url_a("shopping_list")).content.decode()

    def row(self, html, item):
        start = html.rindex("<li", 0, html.index(f'data-item-id="{item.id}"'))
        return html[start:html.index("</li>", start)]

    # --- Basket ownership, colour and identity ---

    def test_each_member_gets_consistent_colour_and_identity(self):
        self.act(self.member_a, self.list_item_a, "add_to_basket")
        self.act(self.admin_a, self.item2, "add_to_basket")

        # Same rendering regardless of who is viewing.
        for viewer in (self.member_a, self.admin_a):
            html = self.page(viewer)
            row_1 = self.row(html, self.list_item_a)
            row_2 = self.row(html, self.item2)
            self.assertIn("member-color-1", row_1)
            self.assertIn("In basket: member_a", row_1)
            self.assertIn("member-color-0", row_2)
            self.assertIn("In basket: admin_a", row_2)
            self.assertIn('data-state="in_basket"', row_1)

    def test_ordinary_item_has_no_highlight_or_attribution(self):
        row = self.row(self.page(self.member_a), self.list_item_a)
        self.assertIn('data-state="ordinary"', row)
        self.assertNotIn("member-color", row)
        self.assertNotIn("item-attribution", row)

    def test_other_member_cannot_claim_item_in_someone_elses_basket(self):
        self.act(self.member_a, self.list_item_a, "add_to_basket")
        self.list_item_a.refresh_from_db()
        stamp = self.list_item_a.time_put_in_basket
        self.item2.cancelled = True
        self.item2.save()
        count = ListItem.objects.count()

        response = self.act(self.admin_a, self.list_item_a, "add_to_basket")

        self.assertEqual(response.status_code, 409)
        self.list_item_a.refresh_from_db()
        self.assertEqual(self.list_item_a.put_in_basket_by, self.member_a)
        self.assertEqual(self.list_item_a.time_put_in_basket, stamp)
        self.assertEqual(self.list_item_a.quantity_required, 1)
        self.assertEqual(ListItem.objects.count(), count)

    def test_owner_adding_again_does_not_reassign_the_first_unit(self):
        self.act(self.member_a, self.list_item_a, "add_to_basket")
        self.list_item_a.refresh_from_db()
        stamp = self.list_item_a.time_put_in_basket

        self.act(self.member_a, self.list_item_a, "add_to_basket")

        self.list_item_a.refresh_from_db()
        self.assertEqual(self.list_item_a.time_put_in_basket, stamp)
        self.assertEqual(self.list_item_a.quantity_required, 1)

    def test_other_member_cannot_remove_and_ownership_survives(self):
        self.act(self.member_a, self.list_item_a, "add_to_basket")
        response = self.act(
            self.admin_a, self.list_item_a, "remove_from_basket",
        )
        self.assertEqual(response.status_code, 403)
        self.list_item_a.refresh_from_db()
        self.assertEqual(self.list_item_a.put_in_basket_by, self.member_a)

    def test_removal_clears_owner_and_next_adder_becomes_owner(self):
        self.act(self.member_a, self.list_item_a, "add_to_basket")
        self.act(self.member_a, self.list_item_a, "remove_from_basket")
        self.list_item_a.refresh_from_db()
        self.assertFalse(self.list_item_a.in_basket)
        self.assertIsNone(self.list_item_a.put_in_basket_by)

        self.act(self.admin_a, self.list_item_a, "add_to_basket")

        self.list_item_a.refresh_from_db()
        self.assertEqual(self.list_item_a.put_in_basket_by, self.admin_a)
        row = self.row(self.page(self.member_a), self.list_item_a)
        self.assertIn("In basket: admin_a", row)
        self.assertNotIn("member_a", row)

    def test_non_owner_sees_disabled_return_and_remove_buttons(self):
        self.act(self.member_a, self.list_item_a, "add_to_basket")
        row = self.row(self.page(self.admin_a), self.list_item_a)
        for action in ('remove_from_basket', 'remove_unit'):
            button = row.split(f'data-basket-action="{action}"', 1)[1].split('>', 1)[0]
            self.assertIn('disabled aria-disabled="true"', button)
        row = self.row(self.page(self.member_a), self.list_item_a)
        button = row.split('data-basket-action="remove_from_basket"', 1)[1].split('>', 1)[0]
        self.assertNotIn('disabled', button)

    # --- Buying ---

    def test_buyer_is_shown_and_may_differ_from_basket_owner(self):
        self.act(self.member_a, self.list_item_a, "add_to_basket")
        self.client.force_login(self.member_a)
        self.client.post(self.url_a("shopping_list"), {"action": "buy_basket"}) # noqa
        self.list_item_a.refresh_from_db()
        self.assertEqual(self.list_item_a.buyer, self.member_a)
        self.assertIsNone(self.list_item_a.put_in_basket_by)

        # Buyer attribution is read from `buyer`, never from the basket
        # owner: a different recorded buyer is displayed as-is.
        self.item2.buyer = self.admin_a
        self.item2.date_bought = timezone.now()
        self.item2.quantity_bought = 1
        self.item2.put_in_basket_by = self.member_a
        self.item2.save()

        html = self.page(self.admin_a)
        bought = html[html.index('id="bought-items-list"'):]
        row_1 = self.row(bought, self.list_item_a)
        row_2 = self.row(bought, self.item2)
        self.assertIn("Bought by member_a", row_1)
        self.assertIn("Bought by admin_a", row_2)
        self.assertNotIn("member-color", row_2)
        self.assertIn("&#10003;", row_1)

    def test_bought_item_cannot_be_basketed_or_cancelled(self):
        self.list_item_a.date_bought = timezone.now()
        self.list_item_a.buyer = self.member_a
        self.list_item_a.save()
        for action in ("add_to_basket", "cancel"):
            response = self.act(self.admin_a, self.list_item_a, action)
            self.assertEqual(response.status_code, 409)
        self.list_item_a.refresh_from_db()
        self.assertFalse(self.list_item_a.in_basket)
        self.assertFalse(self.list_item_a.cancelled)

    # --- Cancel / uncancel ---

    def test_cancel_records_who_and_displays_it(self):
        self.act(self.member_a, self.list_item_a, "cancel")
        self.list_item_a.refresh_from_db()
        self.assertTrue(self.list_item_a.cancelled)
        self.assertEqual(self.list_item_a.cancelled_by, self.member_a)
        self.assertIsNotNone(self.list_item_a.date_cancelled)
        self.assertFalse(self.list_item_a.in_basket)
        self.assertIsNone(self.list_item_a.put_in_basket_by)

        row = self.row(self.page(self.admin_a), self.list_item_a)
        self.assertIn('data-state="cancelled"', row)
        self.assertIn("Cancelled by member_a", row)
        self.assertNotIn("member-color", row)
        self.assertNotIn("Bought by", row)

    def test_item_in_basket_cannot_be_cancelled_and_keeps_owner(self):
        self.act(self.member_a, self.list_item_a, "add_to_basket")
        response = self.act(self.admin_a, self.list_item_a, "cancel")
        self.assertEqual(response.status_code, 400)
        self.list_item_a.refresh_from_db()
        self.assertFalse(self.list_item_a.cancelled)
        self.assertEqual(self.list_item_a.put_in_basket_by, self.member_a)

    def test_uncancel_by_another_member_is_attributed_and_basketable(self):
        self.act(self.member_a, self.list_item_a, "cancel")
        self.act(self.admin_a, self.list_item_a, "uncancel")
        self.list_item_a.refresh_from_db()
        self.assertFalse(self.list_item_a.cancelled)
        self.assertEqual(self.list_item_a.restored_by, self.admin_a)
        self.assertIsNone(self.list_item_a.cancelled_by)

        row = self.row(self.page(self.member_a), self.list_item_a)
        self.assertIn('data-state="ordinary"', row)
        self.assertIn("Restored by admin_a", row)
        self.assertNotIn("Cancelled by", row)

        self.act(self.member_a, self.list_item_a, "add_to_basket")
        self.list_item_a.refresh_from_db()
        self.assertEqual(self.list_item_a.put_in_basket_by, self.member_a)
        row = self.row(self.page(self.member_a), self.list_item_a)
        self.assertNotIn("Restored by", row)

        # Cancelling again clears the previous restoration attribution.
        self.act(self.member_a, self.list_item_a, "remove_from_basket")
        self.act(self.admin_a, self.list_item_a, "cancel")
        self.list_item_a.refresh_from_db()
        self.assertIsNone(self.list_item_a.restored_by)
        self.assertEqual(self.list_item_a.cancelled_by, self.admin_a)

    # --- Integrity ---

    def test_cross_family_user_cannot_cancel_or_basket(self):
        self.client.force_login(self.member_b)
        for action in ("cancel", "add_to_basket"):
            response = self.client.post(self.url_b("shopping_list"), {
                "item_id": self.list_item_a.id, "action": action,
            })
            self.assertEqual(response.status_code, 404)
        self.list_item_a.refresh_from_db()
        self.assertFalse(self.list_item_a.cancelled)
        self.assertFalse(self.list_item_a.in_basket)

    def test_closed_list_items_cannot_be_changed(self):
        ListItem.objects.filter(pk=self.list_item_a.pk).update(current=False)
        for action in ("add_to_basket", "cancel", "uncancel"):
            response = self.act(self.member_a, self.list_item_a, action)
            self.assertEqual(response.status_code, 400)
        self.list_item_a.refresh_from_db()
        self.assertIsNone(self.list_item_a.cancelled_by)
        self.assertFalse(self.list_item_a.in_basket)

    def test_stale_repeat_actions_do_not_change_attribution(self):
        self.act(self.member_a, self.list_item_a, "cancel")
        self.act(self.admin_a, self.list_item_a, "cancel")
        self.list_item_a.refresh_from_db()
        self.assertEqual(self.list_item_a.cancelled_by, self.member_a)

        self.act(self.admin_a, self.list_item_a, "uncancel")
        self.act(self.member_a, self.list_item_a, "uncancel")
        self.list_item_a.refresh_from_db()
        self.assertEqual(self.list_item_a.restored_by, self.admin_a)


class ItemAttributionWebSocketTests(TwoFamilyFixtureMixin, TransactionTestCase):
    async def _connect(self):
        communicator = WebsocketCommunicator(
            ShoppingConsumer.as_asgi(), ws_path(self.family_a),
        )
        communicator.scope["user"] = self.member_a
        communicator.scope["url_route"] = {
            "kwargs": {"family_slug": self.family_a.slug},
        }
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        return communicator

    async def _post(self, user, action):
        await sync_to_async(self.client.force_login)(user)
        return await sync_to_async(self.client.post)(
            self.url_a("shopping_list"),
            {"item_id": self.list_item_a.id, "action": action},
        )

    async def test_updates_carry_persisted_state_and_matching_row_html(self):
        communicator = await self._connect()
        try:
            await self._post(self.member_a, "add_to_basket")
            msg = await communicator.receive_json_from()
            self.assertEqual(msg["state"], "in_basket")
            self.assertEqual(msg["owner_id"], self.member_a.id)
            self.assertEqual(msg["owner_display_name"], "member_a")
            self.assertIn("member-color-1", msg["row_html"])
            self.assertIn("In basket: member_a", msg["row_html"])

            # Rejected claim by someone else: nothing changes, and the
            # persisted state is re-sent so a stale client corrects itself.
            response = await self._post(self.admin_a, "add_to_basket")
            self.assertEqual(response.status_code, 409)
            msg = await communicator.receive_json_from()
            self.assertEqual(msg["owner_id"], self.member_a.id)

            await self._post(self.member_a, "remove_from_basket")
            msg = await communicator.receive_json_from()
            self.assertEqual(msg["state"], "ordinary")
            self.assertIsNone(msg["owner_id"])
            self.assertNotIn("member-color", msg["row_html"])

            await self._post(self.member_a, "cancel")
            msg = await communicator.receive_json_from()
            self.assertEqual(msg["state"], "cancelled")
            self.assertEqual(msg["cancelled_by_display_name"], "member_a")
            self.assertIn("Cancelled by member_a", msg["row_html"])
            # Cancelling may also send a list-complete notice; drain it.
            while not await communicator.receive_nothing(timeout=0.2):
                await communicator.receive_json_from()

            await self._post(self.admin_a, "uncancel")
            msg = await communicator.receive_json_from()
            self.assertEqual(msg["state"], "ordinary")
            self.assertEqual(msg["restored_by_display_name"], "admin_a")
            self.assertIn("Restored by admin_a", msg["row_html"])
        finally:
            await communicator.disconnect()
