"""
Tests for the dev-only `reset_test_registration` management command.
"""
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings

from .management.commands.reset_test_registration import (
    Command as ResetTestRegistrationCommand,
)
from .models import (
    Category,
    Family,
    FamilyMembership,
    ListItem,
    PendingFamilyAdminRegistration,
    Product,
    Shop,
    ShoppingList,
)
from .registration import (
    complete_family_admin_registration_for_new_user,
    request_family_admin_registration,
)


def _run(**options):
    options.setdefault("stdout", StringIO())
    options.setdefault("stderr", StringIO())
    call_command("reset_test_registration", **options)


def _add_dependent_family_data(family):
    """Attach one of each kind of family-owned record, so deletion tests
    can confirm the whole dependent tree is actually gone afterwards."""
    category = Category.objects.create(
        family=family, category_name="Produce",
    )
    shop = Shop.objects.create(
        family=family, shop_name="Test Shop", type_of_shop=1,
    )
    product = Product.objects.create(
        family=family, category=category, default_shop=shop,
        product_name="Apples",
    )
    shopping_list = ShoppingList.objects.create(family=family)
    ListItem.objects.create(
        product=product, family=family, shopping_list=shopping_list,
    )


class ResetTestRegistrationPendingOnlyTests(TestCase):
    def test_removes_pending_registration_only(self):
        request_family_admin_registration(
            family_name="Pending Family", email="pending@example.com",
        )
        self.assertEqual(
            PendingFamilyAdminRegistration.objects.filter(
                email="pending@example.com",
            ).count(),
            1,
        )

        _run(email="pending@example.com", apply=True, yes=True)

        self.assertEqual(
            PendingFamilyAdminRegistration.objects.filter(
                email="pending@example.com",
            ).count(),
            0,
        )

    def test_email_can_be_reused_after_deletion(self):
        request_family_admin_registration(
            family_name="Pending Family", email="reuse@example.com",
        )
        _run(email="reuse@example.com", apply=True, yes=True)

        result = request_family_admin_registration(
            family_name="Pending Family Again", email="reuse@example.com",
        )

        self.assertIsNotNone(result)
        self.assertEqual(
            PendingFamilyAdminRegistration.objects.filter(
                email="reuse@example.com", consumed_at__isnull=True,
            ).count(),
            1,
        )


class ResetTestRegistrationCompletedAdminTests(TestCase):
    def _complete(self, email, username):
        result = request_family_admin_registration(
            family_name="", email=email,
        )
        return complete_family_admin_registration_for_new_user(
            raw_token=result.raw_token, username=username,
            password="a-very-strong-password-99",
        )

    def test_removes_completed_admin_and_exclusive_family_data(self):
        completion = self._complete("complete@example.com", "complete_admin")
        family = completion.family
        _add_dependent_family_data(family)

        _run(email="complete@example.com", apply=True, yes=True)

        self.assertFalse(
            User.objects.filter(username="complete_admin").exists(),
        )
        self.assertFalse(Family.objects.filter(pk=family.pk).exists())
        self.assertEqual(Category.objects.filter(family_id=family.pk).count(), 0) # noqa
        self.assertEqual(Shop.objects.filter(family_id=family.pk).count(), 0)
        self.assertEqual(Product.objects.filter(family_id=family.pk).count(), 0) # noqa
        self.assertEqual(ShoppingList.objects.filter(family_id=family.pk).count(), 0) # noqa
        self.assertEqual(ListItem.objects.filter(family_id=family.pk).count(), 0) # noqa

    def test_preserves_unrelated_accounts_and_families(self):
        self._complete("target@example.com", "target_admin")
        other = self._complete("other@example.com", "other_admin")

        _run(email="target@example.com", apply=True, yes=True)

        self.assertTrue(User.objects.filter(username="other_admin").exists()) # noqa
        self.assertTrue(Family.objects.filter(pk=other.family.pk).exists())

    def test_refuses_family_with_other_members(self):
        completion = self._complete("shared@example.com", "shared_admin")
        FamilyMembership.objects.create(
            family=completion.family,
            user=User.objects.create_user(
                username="shared_member", password="irrelevant-pw-1",
            ),
            display_name="Other Member",
        )

        with self.assertRaises(CommandError):
            _run(email="shared@example.com", apply=True, yes=True)

        self.assertTrue(User.objects.filter(username="shared_admin").exists()) # noqa
        self.assertTrue(Family.objects.filter(pk=completion.family.pk).exists()) # noqa

    def test_refuses_when_user_belongs_to_multiple_families(self):
        completion = self._complete("multi@example.com", "multi_admin")
        second_family = Family.objects.create(name="Second Family")
        FamilyMembership.objects.create(
            family=second_family, user=completion.user,
            display_name="Multi Admin", role=FamilyMembership.Role.ADMIN,
        )

        with self.assertRaises(CommandError):
            _run(email="multi@example.com", apply=True, yes=True)

        self.assertTrue(User.objects.filter(username="multi_admin").exists()) # noqa
        self.assertTrue(Family.objects.filter(pk=completion.family.pk).exists()) # noqa
        self.assertTrue(Family.objects.filter(pk=second_family.pk).exists())

    def test_refuses_stray_cross_family_list_item_references(self):
        completion = self._complete("stray@example.com", "stray_admin")
        other_family = Family.objects.create(name="Other Family")
        other_category = Category.objects.create(
            family=other_family, category_name="Misc",
        )
        other_product = Product.objects.create(
            family=other_family, category=other_category,
            product_name="Thing",
        )
        other_list = ShoppingList.objects.create(family=other_family)
        ListItem.objects.create(
            product=other_product, family=other_family,
            shopping_list=other_list, creator=completion.user,
        )

        with self.assertRaises(CommandError):
            _run(email="stray@example.com", apply=True, yes=True)

        self.assertTrue(User.objects.filter(username="stray_admin").exists()) # noqa

    def test_dry_run_makes_no_changes(self):
        completion = self._complete("dryrun@example.com", "dryrun_admin")

        _run(email="dryrun@example.com")

        self.assertTrue(User.objects.filter(username="dryrun_admin").exists()) # noqa
        self.assertTrue(Family.objects.filter(pk=completion.family.pk).exists()) # noqa

    def test_running_twice_is_safe(self):
        self._complete("twice@example.com", "twice_admin")

        _run(email="twice@example.com", apply=True, yes=True)
        out = StringIO()
        _run(email="twice@example.com", apply=True, yes=True, stdout=out)

        self.assertIn("nothing to do", out.getvalue())

    def test_successful_reuse_after_full_deletion(self):
        self._complete("fullreuse@example.com", "fullreuse_admin")
        _run(email="fullreuse@example.com", apply=True, yes=True)

        response = self.client.post(
            "/register/solo/",
            {"name": "Fresh Attempt", "email": "fullreuse@example.com"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            PendingFamilyAdminRegistration.objects.filter(
                email="fullreuse@example.com", consumed_at__isnull=True,
            ).count(),
            1,
        )

    def test_transaction_rolls_back_on_failure(self):
        completion = self._complete("rollback@example.com", "rollback_admin") # noqa
        family_pk = completion.family.pk

        with mock.patch.object(
            User, "delete", side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                _run(email="rollback@example.com", apply=True, yes=True)

        # The family (and its cascade) must still be rolled back together
        # with the failed user deletion -- not partially cleaned up.
        self.assertTrue(Family.objects.filter(pk=family_pk).exists())
        self.assertTrue(
            User.objects.filter(username="rollback_admin").exists(),
        )


class ResetTestRegistrationSafetyTests(TestCase):
    def test_refuses_when_debug_is_false(self):
        # RUNNING_TESTS is also overridden here: it's always True while
        # actually running under `manage.py test` (see the command's
        # comment), so simulating "not DEBUG" alone wouldn't be enough
        # to reproduce a real non-dev environment.
        with override_settings(DEBUG=False, RUNNING_TESTS=False):
            with self.assertRaises(CommandError):
                ResetTestRegistrationCommand()._ensure_dev_environment()

    def test_refuses_when_dyno_env_var_is_set(self):
        with mock.patch.dict("os.environ", {"DYNO": "web.1"}):
            with self.assertRaises(CommandError):
                ResetTestRegistrationCommand()._ensure_dev_environment()

    def test_refuses_when_database_name_does_not_look_like_dev(self):
        with mock.patch.dict(
            connection.settings_dict, {"NAME": "family_shopper_app_prod"},
        ):
            with self.assertRaises(CommandError):
                ResetTestRegistrationCommand()._ensure_dev_environment()

    def test_refuses_when_database_host_is_not_local(self):
        with mock.patch.dict(
            connection.settings_dict, {"HOST": "prod-db.example.com"},
        ):
            with self.assertRaises(CommandError):
                ResetTestRegistrationCommand()._ensure_dev_environment()

    def test_allows_the_actual_test_environment(self):
        # Sanity check: the real test-run settings must NOT be refused,
        # otherwise every other test in this file would be meaningless.
        ResetTestRegistrationCommand()._ensure_dev_environment()
