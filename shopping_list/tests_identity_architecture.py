"""
Tests enforcing the application's documented User / Family /
FamilyMembership identity architecture:

- A User account has a globally unique email address (one persistent
  identity, not a growing list of addresses).
- A technical Django username is never used in place of a member's
  family-specific display name.
- A Family's identity (name/slug) never derives from an administrator's
  email address.
- Administrator email addresses never influence member display-name
  uniqueness.
- FamilyMembership enforces case-insensitive display-name uniqueness
  per family, while allowing the same person different display names
  in different families, and allowing the same display name to be
  reused independently across different families.
"""
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from allauth.account import app_settings as allauth_account_settings
from allauth.account.models import EmailAddress

from .models import Family, FamilyMembership
from .registration import (
    complete_family_admin_registration_for_existing_user,
    complete_family_admin_registration_for_new_user,
    request_family_admin_registration,
)


class UserGloballyUniqueEmailTests(TestCase):
    """
    "User account ... Has a globally unique email address" -- enforced
    two ways: allauth's own ACCOUNT_CHANGE_EMAIL setting keeps each
    account down to one address (replace, not add), and the database's
    conditional unique_verified_email constraint (allauth's own
    EmailAddress model) prevents two different accounts from both
    holding the same address verified, regardless of how the row was
    created.
    """

    def test_account_change_email_is_enabled(self):
        self.assertTrue(allauth_account_settings.CHANGE_EMAIL)

    def test_account_email_page_uses_the_single_address_template(self):
        user = User.objects.create_user(
            username="single_email_user",
            password="a-very-strong-password-99",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("account_email"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "account/email_change.html")
        self.assertTemplateNotUsed(response, "account/email.html")

    def test_database_rejects_two_accounts_both_verified_for_the_same_email(self): # noqa
        # This is the real, unconditional backstop: regardless of which
        # code path is used to create the row, the database itself will
        # never allow two different accounts to simultaneously hold the
        # same address as a verified email.
        first_user = User.objects.create_user(
            username="owner_one", password="a-very-strong-password-99",
        )
        second_user = User.objects.create_user(
            username="owner_two", password="a-very-strong-password-99",
        )
        EmailAddress.objects.create(
            user=first_user, email="shared@example.com",
            verified=True, primary=True,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                EmailAddress.objects.create(
                    user=second_user, email="shared@example.com",
                    verified=True, primary=True,
                )

    def test_registration_refuses_an_email_already_owned_by_another_account(self): # noqa
        # Application-level check, backed by the database constraint
        # above: completing a NEW admin registration for an email that
        # already verified-belongs to someone else is refused, not
        # allowed to create a second claim on it.
        existing = User.objects.create_user(
            username="prior_owner", password="a-very-strong-password-99",
        )
        EmailAddress.objects.create(
            user=existing, email="taken@example.com",
            verified=True, primary=True,
        )
        result = request_family_admin_registration(
            family_name="New Family", email="taken@example.com",
        )

        from .registration import RegistrationConflictError
        with self.assertRaises(RegistrationConflictError):
            complete_family_admin_registration_for_new_user(
                raw_token=result.raw_token, username="hopeful_admin",
                password="a-very-strong-password-99",
            )
        self.assertFalse(
            User.objects.filter(username="hopeful_admin").exists(),
        )


class UsernameNotConfusedWithDisplayNameTests(TestCase):
    """
    "Any technical Django username retained for authentication must not
    be confused with the person's family-specific display name."
    """

    def test_same_user_has_independent_display_names_in_two_families(self):
        user = User.objects.create_user(
            username="multi_family_person",
            password="a-very-strong-password-99",
        )
        family_a = Family.objects.create(name="Family A")
        family_b = Family.objects.create(name="Family B")
        FamilyMembership.objects.create(
            family=family_a, user=user, display_name="Jim",
        )
        FamilyMembership.objects.create(
            family=family_b, user=user, display_name="James",
        )

        membership_a = FamilyMembership.objects.get(family=family_a, user=user) # noqa
        membership_b = FamilyMembership.objects.get(family=family_b, user=user) # noqa
        self.assertEqual(membership_a.display_name, "Jim")
        self.assertEqual(membership_b.display_name, "James")
        # Neither display name need have anything to do with the
        # authentication username.
        self.assertNotEqual(membership_a.display_name, user.username)
        self.assertNotEqual(membership_b.display_name, user.username)

    def test_username_can_double_as_someone_elses_display_name_without_conflict(self): # noqa
        # The username namespace and the per-family display-name
        # namespace are entirely independent: nothing should treat a
        # collision between them as a problem.
        User.objects.create_user(
            username="dana", password="a-very-strong-password-99",
        )
        other_user = User.objects.create_user(
            username="someone_else", password="a-very-strong-password-99",
        )
        family = Family.objects.create(name="Independent Namespaces Family")

        membership = FamilyMembership.objects.create(
            family=family, user=other_user, display_name="dana",
        )

        self.assertEqual(membership.display_name, "dana")


class FamilyIdentityDoesNotDeriveFromEmailTests(TestCase):
    """
    "Family ... Must not derive its identity from an administrator's
    email address."
    """

    def test_solo_registration_without_a_family_name_defaults_to_username_not_email(self): # noqa
        result = request_family_admin_registration(
            family_name="", email="jordan.identity@example.com",
        )

        completion = complete_family_admin_registration_for_new_user(
            raw_token=result.raw_token, username="jordan_solo",
            password="a-very-strong-password-99",
        )

        self.assertEqual(completion.family.name, "jordan_solo")
        self.assertNotIn("jordan.identity", completion.family.name)
        self.assertNotIn("example.com", completion.family.name)
        self.assertNotIn("@", completion.family.name)
        self.assertNotIn("@", completion.family.slug)

    def test_existing_user_creating_a_second_family_without_a_name_defaults_to_username_not_email(self): # noqa
        existing_user = User.objects.create_user(
            username="second_family_person",
            password="a-very-strong-password-99",
            email="second.family@example.com",
        )
        first_family = Family.objects.create(name="First Family")
        FamilyMembership.objects.create(
            family=first_family, user=existing_user, display_name="Owner",
        )
        result = request_family_admin_registration(
            family_name="", email="second.family@example.com",
        )

        completion = complete_family_admin_registration_for_existing_user(
            raw_token=result.raw_token, user=existing_user,
        )

        self.assertEqual(completion.family.name, "second_family_person")
        self.assertNotIn("second.family", completion.family.name)
        self.assertNotIn("@", completion.family.name)

    def test_family_public_id_is_a_persistent_uuid_not_derived_from_anything(self): # noqa
        family = Family.objects.create(name="UUID Family")
        original_public_id = family.public_id

        family.name = "Renamed Family"
        family.save()
        family.refresh_from_db()

        self.assertEqual(family.public_id, original_public_id)


class DisplayNameUniquenessTests(TestCase):
    """
    "FamilyMembership ... Enforces case-insensitive uniqueness of
    display names within each family" and "Administrator email
    addresses must not influence member display-name uniqueness."
    """

    def setUp(self):
        self.family = Family.objects.create(name="Casing Family")
        self.admin_user = User.objects.create_user(
            username="casing_admin", password="a-very-strong-password-99",
            email="admin.email@example.com",
        )
        FamilyMembership.objects.create(
            family=self.family, user=self.admin_user, display_name="James",
            role=FamilyMembership.Role.ADMIN,
        )

    def test_differently_cased_duplicate_is_rejected_by_validation(self):
        other_user = User.objects.create_user(
            username="other_james", password="a-very-strong-password-99",
        )
        duplicate = FamilyMembership(
            family=self.family, user=other_user, display_name="JAMES",
        )

        with self.assertRaises(ValidationError):
            duplicate.full_clean()

    def test_differently_cased_duplicate_is_rejected_by_the_database_itself(self): # noqa
        # The real, unconditional backstop: even a raw .create() that
        # skips full_clean() is still stopped by the database
        # constraint, exactly mirroring the exact-case case already
        # relied on elsewhere in this app.
        other_user = User.objects.create_user(
            username="raw_james", password="a-very-strong-password-99",
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                FamilyMembership.objects.create(
                    family=self.family, user=other_user,
                    display_name="james",
                )

    def test_admin_email_address_does_not_affect_display_name_availability(self): # noqa
        # A member choosing a display name that happens to equal (or
        # resemble) the admin's email address is unrelated and must not
        # be blocked -- email and display name are different namespaces.
        other_user = User.objects.create_user(
            username="email_lookalike_member",
            password="a-very-strong-password-99",
        )

        membership = FamilyMembership.objects.create(
            family=self.family, user=other_user,
            display_name="admin.email@example.com",
        )

        self.assertEqual(
            membership.display_name, "admin.email@example.com",
        )

    def test_same_display_name_is_independently_available_in_a_different_family(self): # noqa
        other_family = Family.objects.create(name="Another Casing Family")
        other_user = User.objects.create_user(
            username="other_family_james",
            password="a-very-strong-password-99",
        )

        membership = FamilyMembership.objects.create(
            family=other_family, user=other_user, display_name="James",
        )

        self.assertEqual(membership.display_name, "James")
        self.assertEqual(
            FamilyMembership.objects.filter(
                display_name__iexact="james",
            ).count(),
            2,
        )
