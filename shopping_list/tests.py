from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator

from family_shopping_list.consumers import ShoppingConsumer
from .forms import CategoryForm, ProductForm, ShopForm
from .models import Category, Family, FamilyMembership, ListItem, Product, Shop


def create_family_with_members(name, admin_username, member_usernames=()):
    family = Family.objects.create(name=name)
    admin = User.objects.create_user(username=admin_username, password="pass1234")
    FamilyMembership.objects.create(
        family=family, user=admin, role=FamilyMembership.Role.ADMIN,
    )
    members = []
    for username in member_usernames:
        user = User.objects.create_user(username=username, password="pass1234")
        FamilyMembership.objects.create(
            family=family, user=user, role=FamilyMembership.Role.MEMBER,
        )
        members.append(user)
    return family, admin, members


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


class TwoFamilyTestCase(TwoFamilyFixtureMixin, TestCase):
    pass


class FamilyIsolationOnReadsTests(TwoFamilyTestCase):
    def test_shopping_list_only_shows_own_family_items(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.get(reverse("shopping_list"))

        items = list(response.context["shopping_list"])
        self.assertIn(self.list_item_a, items)
        self.assertNotIn(self.list_item_b, items)

    def test_product_list_only_shows_own_family_products(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.get(reverse("product_list"))

        all_products = [
            product
            for products in response.context["grouped_products"].values()
            for product in products
        ]
        self.assertIn(self.product_a, all_products)
        self.assertNotIn(self.product_b, all_products)

    def test_shop_list_only_shows_own_family_shops(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.get(reverse("shop_list"))

        shops = list(response.context["shop_list"])
        self.assertIn(self.shop_a, shops)
        self.assertNotIn(self.shop_b, shops)

    def test_category_list_only_shows_own_family_categories(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.get(reverse("category_list"))

        categories = list(response.context["category_list"])
        self.assertIn(self.category_a, categories)
        self.assertNotIn(self.category_b, categories)


class CrossFamilyObjectAccessTests(TwoFamilyTestCase):
    def test_cross_family_list_item_returns_404(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.get(
            reverse("list_item", kwargs={"slug": self.list_item_b.slug}),
        )
        self.assertEqual(response.status_code, 404)

    def test_cross_family_product_returns_404(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.get(
            reverse("product_detail", kwargs={"slug": self.product_b.slug}),
        )
        self.assertEqual(response.status_code, 404)

    def test_cross_family_shop_returns_404(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.get(
            reverse("shop_detail", kwargs={"slug": self.shop_b.slug}),
        )
        self.assertEqual(response.status_code, 404)

    def test_cross_family_category_returns_404(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.get(
            reverse("category_detail", kwargs={"slug": self.category_b.slug}),
        )
        self.assertEqual(response.status_code, 404)

    def test_cross_family_list_item_action_returns_404(self):
        self.client.login(username="member_a", password="pass1234")
        response = self.client.post(reverse("shopping_list"), {
            "item_id": self.list_item_b.id,
            "action": "buy",
        })
        self.assertEqual(response.status_code, 404)


class CreateUpdateFamilyScopingTests(TwoFamilyTestCase):
    def test_cannot_create_product_with_other_familys_category_or_shop(self):
        self.client.login(username="member_a", password="pass1234")
        self.client.post(reverse("add_product"), {
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
            reverse("product_detail", kwargs={"slug": self.product_a.slug}),
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
        self.client.post(reverse("add_to_shopping_list"), {
            "selected-products": [self.product_b.id],
        })

        self.assertFalse(
            ListItem.objects.filter(product=self.product_b, family=self.family_a).exists(), # noqa
        )


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
    """Forms must set instance.family before model validation runs."""

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
    def test_user_cannot_have_multiple_family_memberships(self):
        family_one = Family.objects.create(name="Family One")
        family_two = Family.objects.create(name="Family Two")
        user = User.objects.create_user(username="dual_member", password="pass1234")

        FamilyMembership.objects.create(family=family_one, user=user)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                FamilyMembership.objects.create(family=family_two, user=user)


class SuperuserWithoutMembershipTests(TestCase):
    def test_superuser_without_membership_gets_no_family_data(self):
        User.objects.create_superuser(
            username="root", password="pass1234", email="root@example.com",
        )
        self.client.login(username="root", password="pass1234")

        response = self.client.get(reverse("shopping_list"))

        self.assertEqual(response.status_code, 403)


class WebSocketFamilyIsolationTests(TwoFamilyFixtureMixin, TransactionTestCase):
    async def test_http_action_broadcasts_only_to_own_family(self):
        communicator_a = WebsocketCommunicator(
            ShoppingConsumer.as_asgi(), "/ws/",
        )
        communicator_a.scope["user"] = self.member_a
        connected_a, _ = await communicator_a.connect()
        self.assertTrue(connected_a)

        communicator_b = WebsocketCommunicator(
            ShoppingConsumer.as_asgi(), "/ws/",
        )
        communicator_b.scope["user"] = self.member_b
        connected_b, _ = await communicator_b.connect()
        self.assertTrue(connected_b)

        try:
            logged_in = await sync_to_async(self.client.login)(
                username="admin_a", password="pass1234",
            )
            self.assertTrue(logged_in)
            await sync_to_async(self.client.post)(reverse("shopping_list"), {
                "item_id": self.list_item_a.id,
                "action": "buy",
            })

            message = await communicator_a.receive_json_from()
            self.assertEqual(message["list_item_id"], self.list_item_a.id)
            self.assertTrue(message["bought"])
            self.assertFalse(message["cancelled"])
            self.assertEqual(message["user"], "admin_a")

            self.assertTrue(await communicator_b.receive_nothing(timeout=0.2))
        finally:
            await communicator_a.disconnect()
            await communicator_b.disconnect()

    async def test_rejects_connection_without_family_membership(self):
        user = await self._create_unaffiliated_superuser()
        communicator = WebsocketCommunicator(ShoppingConsumer.as_asgi(), "/ws/")
        communicator.scope["user"] = user

        connected, close_code = await communicator.connect()

        self.assertFalse(connected)
        self.assertEqual(close_code, 4403)

    @staticmethod
    async def _create_unaffiliated_superuser():
        from asgiref.sync import sync_to_async

        return await sync_to_async(User.objects.create_superuser)(
            username="root2", password="pass1234", email="root2@example.com",
        )


class WebSocketServerAuthorityTests(TwoFamilyFixtureMixin, TransactionTestCase):
    async def test_spoofed_client_username_and_state_are_never_broadcast(self):
        communicator_a = WebsocketCommunicator(
            ShoppingConsumer.as_asgi(), "/ws/",
        )
        communicator_a.scope["user"] = self.member_a
        connected, _ = await communicator_a.connect()
        self.assertTrue(connected)

        try:
            # A malicious client claims to be someone else and asserts a
            # bought/cancelled state that does not match the database.
            await communicator_a.send_to(text_data=(
                '{"list_item_id": %d, "bought": true, '
                '"cancelled": true, "user": "admin_a"}'
            ) % self.list_item_a.id)

            self.assertTrue(
                await communicator_a.receive_nothing(timeout=0.2),
            )
            await sync_to_async(self.list_item_a.refresh_from_db)()
            self.assertFalse(self.list_item_a.bought)
            self.assertFalse(self.list_item_a.cancelled)
        finally:
            await communicator_a.disconnect()

    async def test_cross_family_item_id_is_never_broadcast(self):
        communicator_a = WebsocketCommunicator(
            ShoppingConsumer.as_asgi(), "/ws/",
        )
        communicator_a.scope["user"] = self.member_a
        connected_a, _ = await communicator_a.connect()
        self.assertTrue(connected_a)

        communicator_b = WebsocketCommunicator(
            ShoppingConsumer.as_asgi(), "/ws/",
        )
        communicator_b.scope["user"] = self.member_b
        connected_b, _ = await communicator_b.connect()
        self.assertTrue(connected_b)

        try:
            # member_a claims an item id that actually belongs to family B.
            await communicator_a.send_to(text_data=(
                '{"list_item_id": %d, "bought": true, '
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
                reverse("shopping_list"),
                {"item_id": self.list_item_b.id, "action": "buy"},
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
