from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator

from family_shopping_list.consumers import ShoppingConsumer
from .forms import CategoryForm, ProductForm, ShopForm
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

        all_products = [
            product
            for products in response.context["grouped_products"].values()
            for product in products
        ]
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
