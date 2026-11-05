"""
Tests for the dev-only `dev_reset_all_data` management command.
"""
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings

from allauth.account.models import EmailAddress

from .management.commands.dev_reset_all_data import Command as DevResetAllDataCommand # noqa
from .models import (
    Category,
    Family,
    FamilyMembership,
    ListItem,
    PendingFamilyAdminRegistration,
    PendingFamilyMemberInvitation,
    PendingPasswordChange,
    Product,
    Shop,
    ShoppingList,
)


def _run(**options):
    options.setdefault("stdout", StringIO())
    options.setdefault("stderr", StringIO())
    call_command("dev_reset_all_data", **options)


def _create_full_dataset():
    """One of everything this command is responsible for deleting."""
    admin = User.objects.create_user(
        username="reset_admin", password="testpass123",
        email="reset_admin@example.com",
    )
    EmailAddress.objects.create(
        user=admin, email="reset_admin@example.com",
        verified=True, primary=True,
    )
    family = Family.objects.create(name="Reset Family")
    FamilyMembership.objects.create(
        family=family, user=admin, role=FamilyMembership.Role.ADMIN,
        display_name="Reset Admin",
    )
    category = Category.objects.create(
        family=family, category_name="Produce",
    )
    shop = Shop.objects.create(
        family=family, shop_name="Reset Shop", type_of_shop=1,
    )
    product = Product.objects.create(
        family=family, category=category, default_shop=shop,
        product_name="Apples",
    )
    shopping_list = ShoppingList.objects.create(family=family)
    ListItem.objects.create(
        product=product, family=family, shopping_list=shopping_list,
    )
    PendingFamilyAdminRegistration.objects.create(
        email="pending@example.com", token_digest="a" * 64,
        expires_at="2099-01-01T00:00:00Z",
    )
    PendingFamilyMemberInvitation.objects.create(
        family=family, email="invitee@example.com", token_digest="b" * 64,
        expires_at="2099-01-01T00:00:00Z",
    )
    no_email_member = User.objects.create_user(username="reset_member")
    FamilyMembership.objects.create(
        family=family, user=no_email_member, display_name="Reset Member",
    )
    PendingPasswordChange.objects.create(user=no_email_member)
    superuser = User.objects.create_superuser(
        username="reset_superuser", email="super@example.com",
    )
    return {
        "admin": admin, "family": family, "no_email_member": no_email_member,
        "superuser": superuser,
    }


class DevResetAllDataFunctionalTests(TestCase):
    def test_dry_run_makes_no_changes(self):
        _create_full_dataset()

        _run()

        self.assertEqual(User.objects.count(), 3)
        self.assertEqual(Family.objects.count(), 1)

    def test_dry_run_reports_accurate_counts(self):
        _create_full_dataset()

        out = StringIO()
        _run(stdout=out)
        output = out.getvalue()

        self.assertIn("User: 3", output)
        self.assertIn("of which superusers: 1", output)
        self.assertIn("Family: 1", output)
        self.assertIn("Dry run only", output)

    def test_apply_with_yes_deletes_everything(self):
        _create_full_dataset()

        _run(apply=True, yes=True)

        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(Family.objects.count(), 0)
        self.assertEqual(FamilyMembership.objects.count(), 0)
        self.assertEqual(Shop.objects.count(), 0)
        self.assertEqual(Category.objects.count(), 0)
        self.assertEqual(Product.objects.count(), 0)
        self.assertEqual(ShoppingList.objects.count(), 0)
        self.assertEqual(ListItem.objects.count(), 0)
        self.assertEqual(PendingFamilyAdminRegistration.objects.count(), 0)
        self.assertEqual(PendingFamilyMemberInvitation.objects.count(), 0)
        self.assertEqual(PendingPasswordChange.objects.count(), 0)
        self.assertEqual(EmailAddress.objects.count(), 0)

    def test_preserved_superuser_is_never_counted_or_deleted(self):
        _create_full_dataset()
        preserved = User.objects.create_superuser(
            username="JaimeHyland", email="preserved@example.com",
        )

        out = StringIO()
        _run(stdout=out)
        self.assertIn("User: 3", out.getvalue())  # excludes the preserved account # noqa

        _run(apply=True, yes=True)

        self.assertTrue(
            User.objects.filter(pk=preserved.pk).exists(),
        )
        self.assertEqual(User.objects.count(), 1)

    def test_apply_reports_what_was_deleted(self):
        _create_full_dataset()

        out = StringIO()
        _run(apply=True, yes=True, stdout=out)
        output = out.getvalue()

        self.assertIn("Deleted:", output)
        self.assertIn("reset complete", output)

    def test_apply_without_yes_requires_typed_confirmation(self):
        _create_full_dataset()
        db_name = connection.settings_dict.get("NAME")

        with mock.patch("builtins.input", return_value=db_name):
            _run(apply=True)

        self.assertEqual(User.objects.count(), 0)

    def test_mismatched_confirmation_aborts_without_changes(self):
        _create_full_dataset()

        with mock.patch("builtins.input", return_value="wrong-name"):
            with self.assertRaises(CommandError):
                _run(apply=True)

        self.assertEqual(User.objects.count(), 3)
        self.assertEqual(Family.objects.count(), 1)

    def test_running_on_an_already_empty_database_is_safe(self):
        out = StringIO()
        _run(apply=True, yes=True, stdout=out)

        self.assertIn("already empty", out.getvalue())

    def test_transaction_rolls_back_on_failure(self):
        from django.db.models.signals import post_delete

        _create_full_dataset()

        def _boom(sender, **kwargs):
            raise RuntimeError("boom")

        # QuerySet.delete() bypasses Model.delete() entirely (it's a
        # bulk SQL operation), so simulating a mid-deletion failure
        # requires hooking the post_delete signal, which Django's
        # deletion Collector does still send per row even for a bulk
        # delete.
        post_delete.connect(_boom, sender=User)
        try:
            with self.assertRaises(RuntimeError):
                _run(apply=True, yes=True)
        finally:
            post_delete.disconnect(_boom, sender=User)

        # Family (and its full cascade) must be rolled back together
        # with the failed user deletion, not partially cleaned up.
        self.assertEqual(Family.objects.count(), 1)
        self.assertEqual(User.objects.count(), 3)


class DevResetAllDataSafetyTests(TestCase):
    def test_refuses_when_debug_is_false(self):
        # RUNNING_TESTS is also overridden here: it's always True while
        # actually running under `manage.py test` (see the command's
        # comment), so simulating "not DEBUG" alone wouldn't be enough
        # to reproduce a real non-dev environment.
        with override_settings(DEBUG=False, RUNNING_TESTS=False):
            with self.assertRaises(CommandError):
                DevResetAllDataCommand()._ensure_dev_environment()

    def test_refuses_when_dyno_env_var_is_set(self):
        with mock.patch.dict("os.environ", {"DYNO": "web.1"}):
            with self.assertRaises(CommandError):
                DevResetAllDataCommand()._ensure_dev_environment()

    def test_refuses_when_database_name_does_not_look_like_dev(self):
        with mock.patch.dict(
            connection.settings_dict, {"NAME": "family_shopper_app_prod"},
        ):
            with self.assertRaises(CommandError):
                DevResetAllDataCommand()._ensure_dev_environment()

    def test_refuses_when_database_host_is_not_local(self):
        with mock.patch.dict(
            connection.settings_dict, {"HOST": "prod-db.example.com"},
        ):
            with self.assertRaises(CommandError):
                DevResetAllDataCommand()._ensure_dev_environment()

    def test_allows_the_actual_test_environment(self):
        # Sanity check: the real test-run settings must NOT be refused,
        # otherwise every other test in this file would be meaningless.
        DevResetAllDataCommand()._ensure_dev_environment()
