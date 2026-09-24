"""Tests for public Family-admin self-registration (request -> email -> completion)."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from allauth.account.models import EmailAddress

from .models import Family, FamilyMembership, PendingFamilyAdminRegistration
from .registration import (
    InvalidRegistrationTokenError,
    PasswordPolicyError,
    RegistrationConflictError,
    complete_family_admin_registration_for_existing_user,
    complete_family_admin_registration_for_new_user,
    generate_registration_token,
    get_valid_pending_registration,
    hash_token,
    request_family_admin_registration,
)


class LoginTemplateTests(TestCase):
    def test_login_page_renders_and_links_to_registration_homepage(self):
        response = self.client.get(reverse("account_login"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "account/login.html")
        registration_url = reverse("logged_out_homepage")
        self.assertContains(
            response,
            f'<a class="link" href="{registration_url}">'
            "register your family</a>",
            html=True,
        )

    def test_login_post_redirects_without_raising_noreversematch(self):
        # LOGIN_REDIRECT_URL must not be the now-parameterised 'shopping_list' # noqa
        # URL name; allauth's post-login redirect goes through the
        # homepage's membership-aware routing instead.
        family = Family.objects.create(name="Login Redirect Family")
        user = User.objects.create_user(username="login_redirect_user", password="pass1234") # noqa
        FamilyMembership.objects.create(
            family=family, user=user, display_name="LoginRedirect",
        )

        response = self.client.post(reverse("account_login"), {
            "login": "login_redirect_user",
            "password": "pass1234",
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("logged_out_homepage")) # noqa

        follow_up = self.client.get(response["Location"])
        self.assertRedirects(
            follow_up,
            reverse("shopping_list", kwargs={"family_slug": family.slug}), # noqa
        )


class HomepageRoutingTests(TestCase):
    def test_anonymous_get_root_shows_registration_homepage(self):
        response = self.client.get(reverse("logged_out_homepage"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Register your family")

    def test_authenticated_user_with_one_membership_is_redirected(self):
        family = Family.objects.create(name="Redirect Family")
        user = User.objects.create_user(username="redir_member", password="pass1234") # noqa
        FamilyMembership.objects.create(
            family=family, user=user, display_name="Redir",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("logged_out_homepage"))

        self.assertRedirects(
            response,
            reverse("shopping_list", kwargs={"family_slug": family.slug}), # noqa
        )

    def test_authenticated_user_without_family_gets_403(self):
        user = User.objects.create_user(username="no_family", password="pass1234") # noqa
        self.client.force_login(user)

        response = self.client.get(reverse("logged_out_homepage"))

        self.assertEqual(response.status_code, 403)

    def test_authenticated_user_with_multiple_memberships_sees_chooser(self):
        user = User.objects.create_user(username="multi_home", password="pass1234") # noqa
        family_one = Family.objects.create(name="Family One")
        family_two = Family.objects.create(name="Family Two")
        FamilyMembership.objects.create(
            family=family_one, user=user, display_name="One",
        )
        FamilyMembership.objects.create(
            family=family_two, user=user, display_name="Two",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("logged_out_homepage"))

        self.assertRedirects(response, reverse("family_chooser"))

    def test_family_chooser_lists_only_current_users_memberships(self):
        user = User.objects.create_user(username="chooser_user", password="pass1234") # noqa
        other_user = User.objects.create_user(username="other_chooser", password="pass1234") # noqa
        family_mine = Family.objects.create(name="Mine")
        family_other = Family.objects.create(name="NotMine")
        FamilyMembership.objects.create(
            family=family_mine, user=user, display_name="Mine display",
        )
        FamilyMembership.objects.create(
            family=family_other, user=other_user, display_name="Other display", # noqa
        )
        self.client.force_login(user)

        response = self.client.get(reverse("family_chooser"))

        self.assertContains(response, "Mine")
        self.assertNotContains(response, "NotMine")

    def test_welcome_page_redirects_to_homepage(self):
        response = self.client.get("/welcome_page/")
        self.assertRedirects(response, reverse("logged_out_homepage"))

    def test_signup_route_cannot_bypass_the_dedicated_flow(self):
        response = self.client.get(reverse("account_signup"))
        # allauth's is_open_for_signup() override closes this route; it
        # renders the "closed" page rather than the real signup form, and
        # must not create an account outside the token-verified flow.
        self.assertTemplateUsed(response, "account/signup_closed.html")
        self.assertTemplateNotUsed(response, "account/signup.html")
        self.assertEqual(User.objects.count(), 0)


class RegistrationRequestViewTests(TestCase):
    def test_blank_family_name_creates_no_pending_registration(self):
        self.client.post(reverse("logged_out_homepage"), {
            "family_name": "",
            "email": "someone@example.com",
        })
        self.assertEqual(PendingFamilyAdminRegistration.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_invalid_email_creates_no_pending_registration(self):
        self.client.post(reverse("logged_out_homepage"), {
            "family_name": "Some Family",
            "email": "not-an-email",
        })
        self.assertEqual(PendingFamilyAdminRegistration.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_valid_request_sends_one_email_and_creates_no_account_yet(self):
        response = self.client.post(reverse("logged_out_homepage"), {
            "family_name": "New Family",
            "email": "New.Admin@Example.com",
        })

        self.assertRedirects(response, reverse("registration_check_email"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("New.Admin@Example.com".lower(), mail.outbox[0].to[0].lower()) # noqa
        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(Family.objects.count(), 0)
        self.assertEqual(FamilyMembership.objects.count(), 0)

    def test_pending_registration_stores_only_the_token_digest(self):
        self.client.post(reverse("logged_out_homepage"), {
            "family_name": "Digest Family",
            "email": "digest@example.com",
        })
        pending = PendingFamilyAdminRegistration.objects.get(
            email="digest@example.com",
        )
        raw_token = mail.outbox[0].body.split("verify/")[1].split("/")[0]

        self.assertNotEqual(pending.token_digest, raw_token)
        self.assertEqual(pending.token_digest, hash_token(raw_token))
        self.assertEqual(len(pending.token_digest), 64)

    def test_request_for_existing_account_still_sends_generic_email(self):
        # Every legitimate request creates a token; branching happens at
        # completion time instead, so this must NOT be silently skipped.
        user = User.objects.create_user(
            username="existing", email="existing@example.com", password="pass1234", # noqa
        )
        family = Family.objects.create(name="Existing Family")
        FamilyMembership.objects.create(
            family=family, user=user, display_name="Existing",
        )

        response = self.client.post(reverse("logged_out_homepage"), {
            "family_name": "Whatever",
            "email": "existing@example.com",
        })

        self.assertRedirects(response, reverse("registration_check_email"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            PendingFamilyAdminRegistration.objects.filter(
                email="existing@example.com",
            ).count(),
            1,
        )

    def test_resend_cooldown_prevents_immediate_duplicate_token(self):
        request_family_admin_registration(
            family_name="Cooldown Family", email="cooldown@example.com",
        )
        second = request_family_admin_registration(
            family_name="Cooldown Family", email="cooldown@example.com",
        )
        self.assertIsNone(second)
        self.assertEqual(
            PendingFamilyAdminRegistration.objects.filter(
                email="cooldown@example.com", consumed_at__isnull=True,
            ).count(),
            1,
        )


class RegistrationCompletionViewTests(TestCase):
    def _request_registration(self, email="complete@example.com", family_name="Complete Family"): # noqa
        result = request_family_admin_registration(
            family_name=family_name, email=email,
        )
        return result.raw_token, result.pending_registration

    def _new_user_payload(self, username):
        return {
            "username": username,
            "password1": "a-very-strong-password-99",
            "password2": "a-very-strong-password-99",
            "display_name": "",
        }

    def test_get_with_valid_token_shows_form_without_consuming(self):
        raw_token, pending = self._request_registration()

        response = self.client.get(
            reverse("registration_complete", kwargs={"token": raw_token}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Complete Family")
        pending.refresh_from_db()
        self.assertIsNone(pending.consumed_at)

    def test_get_with_invalid_token_shows_invalid_token_page(self):
        response = self.client.get(
            reverse("registration_complete", kwargs={"token": "not-a-real-token"}), # noqa
        )
        self.assertEqual(response.status_code, 400)

    def test_successful_completion_creates_user_family_and_admin_membership(self): # noqa
        raw_token, _pending = self._request_registration(
            email="admin2@example.com", family_name="Successful Family",
        )

        response = self.client.post(
            reverse("registration_complete", kwargs={"token": raw_token}),
            self._new_user_payload("new_family_admin"),
        )

        family = Family.objects.get(name="Successful Family")
        self.assertRedirects(
            response,
            reverse("shopping_list", kwargs={"family_slug": family.slug}), # noqa
        )
        self.assertEqual(User.objects.filter(username="new_family_admin").count(), 1) # noqa

        user = User.objects.get(username="new_family_admin")
        membership = FamilyMembership.objects.get(user=user)
        self.assertEqual(membership.role, FamilyMembership.Role.ADMIN)
        self.assertEqual(membership.family.name, "Successful Family")

    def test_completion_creates_primary_verified_email_address(self):
        raw_token, _pending = self._request_registration(
            email="verified@example.com", family_name="Verified Family",
        )
        self.client.post(
            reverse("registration_complete", kwargs={"token": raw_token}),
            self._new_user_payload("verified_admin"),
        )

        user = User.objects.get(username="verified_admin")
        email_address = EmailAddress.objects.get(user=user)
        self.assertTrue(email_address.verified)
        self.assertTrue(email_address.primary)
        self.assertEqual(email_address.email, "verified@example.com")

    def test_duplicate_family_names_are_allowed_with_different_public_ids(self): # noqa
        raw_token_a, _ = self._request_registration(
            email="dup-a@example.com", family_name="Same Name",
        )
        raw_token_b, _ = self._request_registration(
            email="dup-b@example.com", family_name="Same Name",
        )

        self.client.post(
            reverse("registration_complete", kwargs={"token": raw_token_a}),
            self._new_user_payload("dup_admin_a"),
        )
        self.client.post(
            reverse("registration_complete", kwargs={"token": raw_token_b}),
            self._new_user_payload("dup_admin_b"),
        )

        families = Family.objects.filter(name="Same Name")
        self.assertEqual(families.count(), 2)
        public_ids = {family.public_id for family in families}
        self.assertEqual(len(public_ids), 2)

    def test_expired_token_cannot_create_account(self):
        raw_token, pending = self._request_registration(
            email="expired@example.com", family_name="Expired Family",
        )
        pending.expires_at = timezone.now() - timedelta(minutes=1)
        pending.save(update_fields=["expires_at"])

        response = self.client.post(
            reverse("registration_complete", kwargs={"token": raw_token}),
            self._new_user_payload("expired_admin"),
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(username="expired_admin").exists()) # noqa

    def test_invalid_token_post_cannot_create_account(self):
        response = self.client.post(
            reverse("registration_complete", kwargs={"token": "bogus-token"}),
            self._new_user_payload("bogus_admin"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(username="bogus_admin").exists()) # noqa

    def test_already_consumed_token_cannot_be_reused(self):
        raw_token, _pending = self._request_registration(
            email="reused@example.com", family_name="Reused Family",
        )
        self.client.post(
            reverse("registration_complete", kwargs={"token": raw_token}),
            self._new_user_payload("reused_admin_one"),
        )

        response = self.client.post(
            reverse("registration_complete", kwargs={"token": raw_token}),
            self._new_user_payload("reused_admin_two"),
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            User.objects.filter(username="reused_admin_two").exists(),
        )

    def test_username_collision_is_handled_safely(self):
        User.objects.create_user(username="taken_name", password="pass1234")
        raw_token, _pending = self._request_registration(
            email="collision@example.com", family_name="Collision Family",
        )

        response = self.client.post(
            reverse("registration_complete", kwargs={"token": raw_token}),
            self._new_user_payload("taken_name"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.filter(username="taken_name").count(), 1) # noqa
        self.assertFalse(
            Family.objects.filter(name="Collision Family").exists(),
        )

    def test_email_collision_at_completion_time_rolls_back_everything(self):
        raw_token, pending = self._request_registration(
            email="race@example.com", family_name="Race Family",
        )
        # Simulate someone else claiming this email after the request but
        # before completion.
        User.objects.create_user(
            username="already_here", email="race@example.com", password="x", # noqa
        )

        with self.assertRaises(RegistrationConflictError):
            complete_family_admin_registration_for_new_user(
                raw_token=raw_token,
                username="race_admin",
                password="strong-password-99",
            )

        self.assertFalse(User.objects.filter(username="race_admin").exists()) # noqa
        self.assertFalse(Family.objects.filter(name="Race Family").exists())
        pending.refresh_from_db()
        self.assertIsNone(pending.consumed_at)

    def test_weak_password_is_rejected_by_the_service_itself(self):
        raw_token, _pending = self._request_registration(
            email="weak@example.com", family_name="Weak Family",
        )

        with self.assertRaises(PasswordPolicyError):
            complete_family_admin_registration_for_new_user(
                raw_token=raw_token, username="weak_admin", password="12345",
            )

        self.assertFalse(User.objects.filter(username="weak_admin").exists()) # noqa
        self.assertFalse(Family.objects.filter(name="Weak Family").exists())


class ExistingEmailRegistrationTests(TestCase):
    def setUp(self):
        self.existing_user = User.objects.create_user(
            username="existing_owner", email="owner@example.com",
            password="pass1234",
        )
        self.first_family = Family.objects.create(name="First Family")
        FamilyMembership.objects.create(
            family=self.first_family, user=self.existing_user,
            display_name="Owner",
        )

    def _request(self, family_name="Second Family"):
        result = request_family_admin_registration(
            family_name=family_name, email="owner@example.com",
        )
        return result.raw_token

    def test_anonymous_get_redirects_to_login_preserving_next(self):
        raw_token = self._request()

        response = self.client.get(
            reverse("registration_complete", kwargs={"token": raw_token}),
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("next=", response["Location"])
        self.assertIn(raw_token, response["Location"])

    def test_completion_requires_login_to_the_matching_account(self):
        raw_token = self._request()
        self.client.login(username="existing_owner", password="pass1234")

        response = self.client.get(
            reverse("registration_complete", kwargs={"token": raw_token}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Second Family")

    def test_different_logged_in_account_cannot_consume_the_token(self):
        raw_token = self._request()
        User.objects.create_user(username="someone_else", password="pass1234") # noqa
        self.client.login(username="someone_else", password="pass1234")

        response = self.client.post(
            reverse("registration_complete", kwargs={"token": raw_token}),
            {"display_name": ""},
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Family.objects.filter(name="Second Family").exists()) # noqa

    def test_existing_email_completion_creates_second_admin_membership_without_new_user(self): # noqa
        raw_token = self._request()
        self.client.login(username="existing_owner", password="pass1234")

        response = self.client.post(
            reverse("registration_complete", kwargs={"token": raw_token}),
            {"display_name": "Owner Two"},
        )

        self.assertEqual(User.objects.count(), 1)
        new_family = Family.objects.get(name="Second Family")
        self.assertRedirects(
            response,
            reverse("shopping_list", kwargs={"family_slug": new_family.slug}), # noqa
        )
        memberships = FamilyMembership.objects.filter(user=self.existing_user) # noqa
        self.assertEqual(memberships.count(), 2)
        new_membership = memberships.get(family=new_family)
        self.assertEqual(new_membership.role, FamilyMembership.Role.ADMIN)
        self.assertEqual(new_membership.display_name, "Owner Two")

        # Still only one EmailAddress row for this address; not duplicated.
        self.assertEqual(
            EmailAddress.objects.filter(
                user=self.existing_user, email__iexact="owner@example.com",
            ).count(),
            1,
        )

    def test_service_function_directly_for_existing_user(self):
        raw_token = self._request(family_name="Direct Service Family")

        result = complete_family_admin_registration_for_existing_user(
            raw_token=raw_token, user=self.existing_user,
            display_name="Direct",
        )

        self.assertEqual(result.family.name, "Direct Service Family")
        self.assertEqual(result.membership.display_name, "Direct")
        self.assertEqual(User.objects.count(), 1)


class RegistrationServiceUnitTests(TestCase):
    """Exercise the framework-neutral service layer directly."""

    def test_generate_registration_token_returns_matching_digest(self):
        raw_token, digest = generate_registration_token()
        self.assertEqual(digest, hash_token(raw_token))
        self.assertEqual(len(digest), 64)

    def test_request_registration_supersedes_previous_active_token(self):
        first = request_family_admin_registration(
            family_name="First", email="super@example.com",
        )
        # Move past the cooldown so the second request is not suppressed.
        PendingFamilyAdminRegistration.objects.filter(
            email="super@example.com",
        ).update(created_at=timezone.now() - timedelta(minutes=5))
        second = request_family_admin_registration(
            family_name="Second", email="super@example.com",
        )

        with self.assertRaises(InvalidRegistrationTokenError):
            get_valid_pending_registration(first.raw_token)

        # The newer token is still valid.
        pending = get_valid_pending_registration(second.raw_token)
        self.assertEqual(pending.family_name, "Second")

    def test_complete_registration_end_to_end(self):
        result = request_family_admin_registration(
            family_name="Service Family", email="service@example.com",
        )

        completion = complete_family_admin_registration_for_new_user(
            raw_token=result.raw_token,
            username="service_admin",
            password="a-strong-password-99",
        )

        self.assertEqual(completion.user.username, "service_admin")
        self.assertEqual(completion.family.name, "Service Family")
        self.assertEqual(completion.membership.role, FamilyMembership.Role.ADMIN) # noqa
        self.assertEqual(completion.membership.display_name, "service_admin") # noqa


class ExistingMemberWithoutEmailTests(TestCase):
    def test_member_without_email_can_still_log_in(self):
        family = Family.objects.create(name="No Email Family")
        user = User.objects.create_user(
            username="no_email_member", password="pass1234",
        )
        FamilyMembership.objects.create(
            family=family, user=user, display_name="NoEmail",
        )
        self.assertEqual(user.email, "")

        logged_in = self.client.login(
            username="no_email_member", password="pass1234",
        )

        self.assertTrue(logged_in)


class FamilyPublicIdTests(TestCase):
    def test_families_get_distinct_public_ids(self):
        family_a = Family.objects.create(name="A")
        family_b = Family.objects.create(name="B")
        self.assertNotEqual(family_a.public_id, family_b.public_id)
        self.assertIsNotNone(family_a.public_id)
