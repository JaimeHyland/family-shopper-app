"""
Tests for the per-family display-name login door (family_login_views.py).
"""
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from .invitations import invite_member_without_email
from .models import Family, FamilyMembership, PendingPasswordChange


def _create_family_with_admin(family_name, admin_username):
    family = Family.objects.create(name=family_name)
    admin = User.objects.create_user(
        username=admin_username, password="a-very-strong-password-99",
    )
    FamilyMembership.objects.create(
        family=family, user=admin, role=FamilyMembership.Role.ADMIN,
        display_name=admin_username,
    )
    return family, admin


def _family_login_url(family):
    return reverse("family_login", kwargs={"family_slug": family.slug})


class FamilyLoginViewTests(TestCase):
    def setUp(self):
        # Rate-limit state lives in the cache, not the database, so it
        # survives Django's per-test transaction rollback -- and since a
        # clean DB gives deterministic family slugs across test methods,
        # a prior test's rate-limit counters could otherwise leak in.
        cache.clear()
        self.family, self.admin = _create_family_with_admin(
            "Login Family", "login_admin",
        )
        self.member = invite_member_without_email(
            family=self.family, display_name="Jim",
            initial_password="temporary-pw-99",
        )

    def test_get_shows_login_form(self):
        response = self.client.get(_family_login_url(self.family))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.family.name)
        self.assertContains(response, "Display name")

    def test_correct_display_name_and_password_logs_in(self):
        response = self.client.post(_family_login_url(self.family), {
            "display_name": "Jim", "password": "temporary-pw-99",
        })

        self.assertRedirects(
            response, reverse(
                "shopping_list", kwargs={"family_slug": self.family.slug},
            ), fetch_redirect_response=False,
        )
        self.assertIn("_auth_user_id", self.client.session)
        self.assertEqual(
            int(self.client.session["_auth_user_id"]), self.member.user.pk,
        )

    def test_display_name_match_is_case_insensitive(self):
        response = self.client.post(_family_login_url(self.family), {
            "display_name": "JIM", "password": "temporary-pw-99",
        })

        self.assertRedirects(
            response, reverse(
                "shopping_list", kwargs={"family_slug": self.family.slug},
            ), fetch_redirect_response=False,
        )

    def test_wrong_password_is_rejected_generically(self):
        response = self.client.post(_family_login_url(self.family), {
            "display_name": "Jim", "password": "totally-wrong-password",
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid display name or password.")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_unknown_display_name_in_this_family_is_rejected_generically(self): # noqa
        response = self.client.post(_family_login_url(self.family), {
            "display_name": "NoSuchPerson", "password": "whatever-99",
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid display name or password.")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_family_isolation_another_familys_matching_display_name_is_rejected(self): # noqa
        # CRITICAL: a display name that is valid in a DIFFERENT family,
        # even with its correct password for that OTHER account, must
        # never authenticate through THIS family's door.
        other_family, _other_admin = _create_family_with_admin(
            "Other Login Family", "other_login_admin",
        )
        invite_member_without_email(
            family=other_family, display_name="Jim",
            initial_password="other-familys-temp-pw-99",
        )

        response = self.client.post(_family_login_url(self.family), {
            "display_name": "Jim", "password": "other-familys-temp-pw-99",
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid display name or password.")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_successful_login_lands_directly_in_this_family_despite_other_memberships(self): # noqa
        second_family, _second_admin = _create_family_with_admin(
            "Second Membership Family", "second_membership_admin",
        )
        FamilyMembership.objects.create(
            family=second_family, user=self.member.user,
            display_name="Jimmy",
        )

        response = self.client.post(_family_login_url(self.family), {
            "display_name": "Jim", "password": "temporary-pw-99",
        })

        # Lands directly in `self.family` -- not the generic multi-family
        # chooser -- even though this account now has two memberships.
        self.assertRedirects(
            response, reverse(
                "shopping_list", kwargs={"family_slug": self.family.slug},
            ), fetch_redirect_response=False,
        )

    def test_nonexistent_family_slug_returns_404(self):
        response = self.client.get(
            reverse(
                "family_login", kwargs={"family_slug": "no-such-family"},
            ),
        )

        self.assertEqual(response.status_code, 404)

    def test_blank_submission_shows_form_errors_not_a_server_error(self):
        response = self.client.post(_family_login_url(self.family), {
            "display_name": "", "password": "",
        })

        self.assertEqual(response.status_code, 200)

    def test_first_login_password_change_still_applies_after_family_login(self): # noqa
        response = self.client.post(_family_login_url(self.family), {
            "display_name": "Jim", "password": "temporary-pw-99",
        }, follow=True)

        # Even after a successful family-door login, the existing
        # mandatory first-login password change middleware still
        # applies, unchanged -- authentication-mechanism-agnostic.
        self.assertRedirects(
            response, reverse("force_password_change"),
        )

    def test_family_login_then_password_change_lands_in_the_correct_family(self): # noqa
        self.client.post(_family_login_url(self.family), {
            "display_name": "Jim", "password": "temporary-pw-99",
        })

        response = self.client.post(reverse("force_password_change"), {
            "new_password1": "brand-new-pw-456",
            "new_password2": "brand-new-pw-456",
        })

        # Must go straight to this family's shopping list -- not via an
        # intermediate '/' hop that relies on membership-count guessing
        # (which could even 403 if the membership vanished meanwhile).
        self.assertRedirects(
            response, reverse(
                "shopping_list", kwargs={"family_slug": self.family.slug},
            ),
        )

    def test_family_login_then_password_change_falls_back_safely_if_membership_is_gone(self): # noqa
        # Defensive edge case: if the member's FamilyMembership somehow
        # disappears between the family-door login and completing the
        # forced password change (e.g. removed by an admin in the
        # meantime), the view must not trust the stale session hint --
        # it must fall back to the existing membership-count redirect
        # rather than sending them into a family they're no longer in.
        self.client.post(_family_login_url(self.family), {
            "display_name": "Jim", "password": "temporary-pw-99",
        })
        FamilyMembership.objects.filter(user=self.member.user).delete()

        response = self.client.post(reverse("force_password_change"), {
            "new_password1": "brand-new-pw-456",
            "new_password2": "brand-new-pw-456",
        })

        self.assertRedirects(
            response, reverse("logged_out_homepage"), target_status_code=403,
        )

    def test_repeated_failed_attempts_are_rate_limited(self):
        with override_settings(
            ACCOUNT_RATE_LIMITS={"family_login_failed": "2/m/key"},
        ):
            for _ in range(2):
                self.client.post(_family_login_url(self.family), {
                    "display_name": "Jim", "password": "wrong-password",
                })
            blocked = self.client.post(_family_login_url(self.family), {
                "display_name": "Jim", "password": "wrong-password",
            })

            self.assertEqual(blocked.status_code, 429)

    def test_successful_login_clears_the_rate_limit(self):
        with override_settings(
            ACCOUNT_RATE_LIMITS={"family_login_failed": "2/m/key"},
        ):
            self.client.post(_family_login_url(self.family), {
                "display_name": "Jim", "password": "wrong-password",
            })
            success = self.client.post(_family_login_url(self.family), {
                "display_name": "Jim", "password": "temporary-pw-99",
            })
            self.assertEqual(success.status_code, 302)

            self.client.logout()
            # Budget was reset by the successful login -- two more
            # failed attempts are allowed before being blocked again.
            for _ in range(2):
                response = self.client.post(_family_login_url(self.family), {
                    "display_name": "Jim", "password": "wrong-password",
                })
                self.assertEqual(response.status_code, 200)


class LoggedInIdentityDisplayTests(TestCase):
    """
    The "You are logged in as ..." nav-bar message (and anywhere else
    relying on templatetags.custom_filters.logged_in_identity) must
    never show a no-email member's internal, auto-generated username --
    only their family-local display name (+ family name).
    """

    def setUp(self):
        cache.clear()
        self.family, self.admin = _create_family_with_admin(
            "Identity Family", "identity_admin",
        )
        self.member = invite_member_without_email(
            family=self.family, display_name="Jim",
            initial_password="temporary-pw-99",
        )
        # The forced first-login password change is a separate concern
        # (covered by ForcePasswordChangeTests); clear it here so these
        # tests can exercise ordinary, already-settled pages.
        PendingPasswordChange.objects.filter(user=self.member.user).delete()

    def _login_as_member(self):
        self.client.post(_family_login_url(self.family), {
            "display_name": "Jim", "password": "temporary-pw-99",
        })

    def test_no_email_members_internal_username_is_never_rendered(self):
        self._login_as_member()

        response = self.client.get(
            reverse(
                "shopping_list", kwargs={"family_slug": self.family.slug},
            ),
        )

        self.assertNotContains(response, self.member.user.username)

    def test_no_email_member_sees_display_name_and_family_name(self):
        self._login_as_member()

        response = self.client.get(
            reverse(
                "shopping_list", kwargs={"family_slug": self.family.slug},
            ),
        )

        self.assertContains(
            response, "You are logged in as Jim (Identity Family)",
        )

    def test_no_email_member_without_a_current_family_context_still_hides_username(self): # noqa
        # Add a second membership so there's more than one to fall back
        # to, and visit a page that has no single "current family"
        # (the family chooser), to exercise the no-request.family path.
        second_family, _second_admin = _create_family_with_admin(
            "Second Identity Family", "second_identity_admin",
        )
        FamilyMembership.objects.create(
            family=second_family, user=self.member.user,
            display_name="Jimmy",
        )
        self._login_as_member()

        response = self.client.get(reverse("family_chooser"))

        self.assertNotContains(response, self.member.user.username)
        self.assertContains(response, "You are logged in as")

    def test_admin_also_sees_display_name_and_family_name(self):
        # "Option A" accounts (a real, self-chosen Django username) are
        # no longer a special case: the header always shows the
        # family-local display name, not the technical username, even
        # when they differ -- as set here deliberately, to prove this
        # isn't simply reusing the username by coincidence.
        FamilyMembership.objects.filter(
            family=self.family, user=self.admin,
        ).update(display_name="Identity Administrator")
        self.client.force_login(self.admin)

        response = self.client.get(
            reverse(
                "shopping_list", kwargs={"family_slug": self.family.slug},
            ),
        )

        self.assertContains(
            response,
            "You are logged in as Identity Administrator "
            "(Identity Family)",
        )
        self.assertNotContains(response, f"as {self.admin.username}")
