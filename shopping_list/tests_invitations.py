"""
Tests for family-member invitations (by email and without email) and the
resulting mandatory first-login password change.
"""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from allauth.account.models import EmailAddress

from .models import (
    Family,
    FamilyMembership,
    PendingFamilyMemberInvitation,
    PendingPasswordChange,
)
from .invitations import (
    INTERNAL_USERNAME_PREFIX,
    InvalidInvitationTokenError,
    InvitationConflictError,
    complete_forced_password_change,
    complete_invitation_for_existing_user,
    complete_invitation_for_new_user,
    get_valid_pending_invitation,
    invite_member_by_email,
    invite_member_without_email,
)


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


def _family_url(family, name, **kwargs):
    return reverse(name, kwargs={"family_slug": family.slug, **kwargs})


class InviteMemberViewTests(TestCase):
    def setUp(self):
        self.family, self.admin = _create_family_with_admin(
            "Invite Family", "invite_admin",
        )

    def test_get_shows_both_invitation_forms(self):
        self.client.force_login(self.admin)

        response = self.client.get(_family_url(self.family, "invite_member")) # noqa

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invite by email")
        self.assertContains(response, "Invite without email")
        # The no-email form must require the initial password twice, to
        # catch admin typos (matching every other password form in the
        # app).
        self.assertContains(response, "Confirm initial password")
        self.assertContains(
            response, 'name="initial_password2"',
        )
        # No technical username is ever exposed here -- the admin only
        # ever picks a display name, never a username, for a no-email
        # member.
        self.assertNotContains(response, 'name="username"')
        self.assertContains(
            response, _family_url(self.family, "family_login"),
        )

    def test_non_admin_cannot_access_the_invite_view(self):
        member = User.objects.create_user(
            username="ordinary_member", password="a-very-strong-password-99", # noqa
        )
        FamilyMembership.objects.create(
            family=self.family, user=member, display_name="Ordinary",
        )
        self.client.force_login(member)

        get_response = self.client.get(
            _family_url(self.family, "invite_member"),
        )
        self.assertEqual(get_response.status_code, 403)

        post_response = self.client.post(
            _family_url(self.family, "invite_member"),
            {"method": "email", "email": "sneaky@example.com"},
        )
        self.assertEqual(post_response.status_code, 403)
        self.assertEqual(
            PendingFamilyMemberInvitation.objects.count(), 0,
        )

    def test_anonymous_visitor_is_redirected_to_login(self):
        response = self.client.get(
            _family_url(self.family, "invite_member"),
        )
        self.assertEqual(response.status_code, 302)

    def test_invite_by_email_sends_one_email_and_creates_no_account_yet(self): # noqa
        self.client.force_login(self.admin)

        response = self.client.post(
            _family_url(self.family, "invite_member"),
            {"method": "email", "email": "New.Invitee@Example.com"},
        )

        self.assertRedirects(
            response, _family_url(self.family, "family_members"),
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(
            "new.invitee@example.com", mail.outbox[0].to[0].lower(),
        )
        self.assertEqual(User.objects.filter(
            username__icontains="invitee",
        ).count(), 0)
        pending = PendingFamilyMemberInvitation.objects.get(
            email="new.invitee@example.com",
        )
        self.assertEqual(pending.family, self.family)
        self.assertEqual(pending.invited_by, self.admin)

    def test_invite_by_email_already_a_member_is_rejected(self):
        member = User.objects.create_user(
            username="already_here", password="a-very-strong-password-99", # noqa
            email="already@example.com",
        )
        FamilyMembership.objects.create(
            family=self.family, user=member, display_name="Already Here",
        )
        self.client.force_login(self.admin)

        response = self.client.post(
            _family_url(self.family, "invite_member"),
            {"method": "email", "email": "already@example.com"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already a member")
        self.assertEqual(PendingFamilyMemberInvitation.objects.count(), 0)

    def test_invite_by_email_with_ambiguous_ownership_is_rejected_safely(self): # noqa
        User.objects.create_user(
            username="ambiguous_one", password="a-very-strong-password-99", # noqa
            email="ambiguous@example.com",
        )
        second_user = User.objects.create_user(
            username="ambiguous_two", password="a-very-strong-password-99", # noqa
        )
        EmailAddress.objects.create(
            user=second_user, email="ambiguous@example.com",
            verified=False, primary=False,
        )
        self.client.force_login(self.admin)

        response = self.client.post(
            _family_url(self.family, "invite_member"),
            {"method": "email", "email": "ambiguous@example.com"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "contact support")
        self.assertEqual(PendingFamilyMemberInvitation.objects.count(), 0)

    def test_invite_by_email_race_does_not_falsely_claim_success(self):
        self.client.force_login(self.admin)

        with patch(
            "shopping_list.invitation_views.invite_member_by_email",
            return_value=None,
        ):
            response = self.client.post(
                _family_url(self.family, "invite_member"),
                {"method": "email", "email": "racer@example.com"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Invitation sent")
        self.assertContains(response, "already pending")
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(PendingFamilyMemberInvitation.objects.count(), 0)

    def test_invite_without_email_creates_account_and_pending_password_change(self): # noqa
        self.client.force_login(self.admin)

        response = self.client.post(
            _family_url(self.family, "invite_member"),
            {
                "method": "no_email", "display_name": "no_email_invitee",
                "initial_password": "a-very-strong-password-99",
                "initial_password2": "a-very-strong-password-99",
            },
        )

        self.assertRedirects(
            response, _family_url(self.family, "family_members"),
        )
        self.assertEqual(len(mail.outbox), 0)
        membership = FamilyMembership.objects.get(
            family=self.family, display_name="no_email_invitee",
        )
        user = membership.user
        self.assertEqual(membership.role, FamilyMembership.Role.MEMBER)
        # The internal username is auto-generated, opaque, and never
        # equal to (or derived from) the display name the admin chose.
        self.assertTrue(user.username.startswith("member_"))
        self.assertNotEqual(user.username, "no_email_invitee")
        self.assertTrue(
            PendingPasswordChange.objects.filter(user=user).exists(),
        )

    def test_invite_without_email_does_not_expose_the_generated_username(self): # noqa
        self.client.force_login(self.admin)

        response = self.client.post(
            _family_url(self.family, "invite_member"),
            {
                "method": "no_email", "display_name": "private_username_invitee", # noqa
                "initial_password": "a-very-strong-password-99",
                "initial_password2": "a-very-strong-password-99",
            },
            follow=True,
        )

        user = FamilyMembership.objects.get(
            family=self.family, display_name="private_username_invitee",
        ).user
        self.assertNotContains(response, user.username)
        self.assertContains(response, "private_username_invitee")

    def test_invite_without_email_requires_a_display_name(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            _family_url(self.family, "invite_member"),
            {
                "method": "no_email", "display_name": "",
                "initial_password": "a-very-strong-password-99",
                "initial_password2": "a-very-strong-password-99",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            FamilyMembership.objects.filter(family=self.family).count(), 1,
        )  # just the admin -- no new membership was created

    def test_invite_without_email_weak_password_is_rejected(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            _family_url(self.family, "invite_member"),
            {
                "method": "no_email", "display_name": "weak_pw_invitee",
                "initial_password": "password",
                "initial_password2": "password",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            FamilyMembership.objects.filter(
                family=self.family, display_name="weak_pw_invitee",
            ).exists(),
        )

    def test_invite_without_email_display_name_collision_is_rejected(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            _family_url(self.family, "invite_member"),
            {
                "method": "no_email", "display_name": "invite_admin",
                "initial_password": "a-very-strong-password-99",
                "initial_password2": "a-very-strong-password-99",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already used")
        self.assertEqual(
            FamilyMembership.objects.filter(family=self.family).count(), 1,
        )

    def test_invite_without_email_mismatched_passwords_are_rejected(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            _family_url(self.family, "invite_member"),
            {
                "method": "no_email", "display_name": "typo_invitee",
                "initial_password": "a-very-strong-password-99",
                "initial_password2": "a-very-strong-password-00",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "didn&#x27;t match")
        self.assertFalse(
            FamilyMembership.objects.filter(
                family=self.family, display_name="typo_invitee",
            ).exists(),
        )


class MemberInvitationServiceTests(TestCase):
    """Exercise the framework-neutral invitations service layer directly."""

    def setUp(self):
        self.family, self.admin = _create_family_with_admin(
            "Service Family", "service_admin",
        )

    def test_no_email_invites_generate_distinct_internal_usernames(self):
        first = invite_member_without_email(
            family=self.family, display_name="First One",
            initial_password="a-very-strong-password-99",
        )
        second = invite_member_without_email(
            family=self.family, display_name="Second One",
            initial_password="a-very-strong-password-99",
        )

        self.assertTrue(first.user.username.startswith("member_"))
        self.assertTrue(second.user.username.startswith("member_"))
        self.assertNotEqual(first.user.username, second.user.username)

    def test_invite_supersedes_previous_active_invitation_for_same_family(self): # noqa
        first = invite_member_by_email(
            family=self.family, email="super@example.com",
        )
        second = invite_member_by_email(
            family=self.family, email="super@example.com",
        )

        with self.assertRaises(InvalidInvitationTokenError):
            get_valid_pending_invitation(first.raw_token)

        pending = get_valid_pending_invitation(second.raw_token)
        self.assertEqual(pending.email, "super@example.com")

    def test_same_email_can_have_separate_active_invitations_to_different_families(self): # noqa
        other_family, _other_admin = _create_family_with_admin(
            "Other Family", "other_admin",
        )

        result_a = invite_member_by_email(
            family=self.family, email="multi@example.com",
        )
        result_b = invite_member_by_email(
            family=other_family, email="multi@example.com",
        )

        # Neither is superseded -- both tokens remain valid.
        get_valid_pending_invitation(result_a.raw_token)
        get_valid_pending_invitation(result_b.raw_token)

    def test_complete_invitation_for_new_user_creates_member_and_verified_email(self): # noqa
        result = invite_member_by_email(
            family=self.family, email="joiner@example.com",
        )

        membership = complete_invitation_for_new_user(
            raw_token=result.raw_token, username="joiner_user",
            password="a-very-strong-password-99", display_name="Joiner",
        )

        self.assertEqual(membership.family, self.family)
        self.assertEqual(membership.role, FamilyMembership.Role.MEMBER)
        self.assertEqual(membership.display_name, "Joiner")
        email_address = EmailAddress.objects.get(user=membership.user)
        self.assertTrue(email_address.verified)
        self.assertTrue(email_address.primary)
        self.assertFalse(
            PendingFamilyMemberInvitation.objects.filter(
                email="joiner@example.com",
            ).exists(),
        )

    def test_complete_invitation_for_existing_user_adds_membership_without_new_user(self): # noqa
        existing_user = User.objects.create_user(
            username="existing_joiner",
            password="a-very-strong-password-99",
            email="existing_joiner@example.com",
        )
        result = invite_member_by_email(
            family=self.family, email="existing_joiner@example.com",
        )

        membership = complete_invitation_for_existing_user(
            raw_token=result.raw_token, user=existing_user,
            display_name="Existing Joiner",
        )

        self.assertEqual(User.objects.count(), 2)  # admin + existing_joiner
        self.assertEqual(membership.user, existing_user)
        self.assertEqual(membership.family, self.family)
        self.assertEqual(membership.role, FamilyMembership.Role.MEMBER)

    def test_complete_invitation_for_existing_user_defaults_to_prior_display_name(self): # noqa
        existing_user = User.objects.create_user(
            username="repeat_joiner",
            password="testpass123",
            email="repeat_joiner@example.com",
        )
        other_family = Family.objects.create(name="Other Family")
        FamilyMembership.objects.create(
            family=other_family, user=existing_user,
            display_name="Joiner Nickname",
        )
        result = invite_member_by_email(
            family=self.family, email="repeat_joiner@example.com",
        )

        membership = complete_invitation_for_existing_user(
            raw_token=result.raw_token, user=existing_user,
        )

        self.assertEqual(membership.display_name, "Joiner Nickname")

    def test_complete_invitation_for_existing_user_internal_username_requires_explicit_display_name(self): # noqa
        internal_user = User.objects.create_user(
            username=f"{INTERNAL_USERNAME_PREFIX}deadbeef",
            password="testpass123",
            email="internal_joiner@example.com",
        )
        result = invite_member_by_email(
            family=self.family, email="internal_joiner@example.com",
        )

        with self.assertRaises(InvitationConflictError):
            complete_invitation_for_existing_user(
                raw_token=result.raw_token, user=internal_user,
            )
        self.assertFalse(
            FamilyMembership.objects.filter(user=internal_user).exists(),
        )

    def test_complete_invitation_for_existing_user_internal_username_accepts_explicit_display_name(self): # noqa
        internal_user = User.objects.create_user(
            username=f"{INTERNAL_USERNAME_PREFIX}deadbeef2",
            password="testpass123",
            email="internal_joiner2@example.com",
        )
        result = invite_member_by_email(
            family=self.family, email="internal_joiner2@example.com",
        )

        membership = complete_invitation_for_existing_user(
            raw_token=result.raw_token, user=internal_user,
            display_name="Chosen Name",
        )

        self.assertEqual(membership.display_name, "Chosen Name")

    def test_expired_invitation_cannot_be_completed(self):
        result = invite_member_by_email(
            family=self.family, email="expired@example.com",
        )
        PendingFamilyMemberInvitation.objects.filter(
            email="expired@example.com",
        ).update(expires_at=timezone.now() - timedelta(seconds=1))

        with self.assertRaises(InvalidInvitationTokenError):
            complete_invitation_for_new_user(
                raw_token=result.raw_token, username="too_late",
                password="a-very-strong-password-99",
            )
        self.assertFalse(User.objects.filter(username="too_late").exists())

    def test_already_consumed_invitation_cannot_be_reused(self):
        result = invite_member_by_email(
            family=self.family, email="onceonly@example.com",
        )
        complete_invitation_for_new_user(
            raw_token=result.raw_token, username="once_user",
            password="a-very-strong-password-99",
        )

        with self.assertRaises(InvalidInvitationTokenError):
            complete_invitation_for_new_user(
                raw_token=result.raw_token, username="twice_user",
                password="a-very-strong-password-99",
            )
        self.assertFalse(
            User.objects.filter(username="twice_user").exists(),
        )

    def test_display_name_conflict_on_completion_is_rejected(self):
        result = invite_member_by_email(
            family=self.family, email="clashing@example.com",
        )

        with self.assertRaises(InvitationConflictError):
            complete_invitation_for_new_user(
                raw_token=result.raw_token, username="clashing_user",
                password="a-very-strong-password-99",
                display_name="service_admin",
            )
        self.assertFalse(
            User.objects.filter(username="clashing_user").exists(),
        )


class MemberInvitationCompletionViewTests(TestCase):
    def setUp(self):
        self.family, self.admin = _create_family_with_admin(
            "Completion Family", "completion_admin",
        )

    def _invite(self, email="newmember@example.com"):
        return invite_member_by_email(family=self.family, email=email)

    def test_get_with_valid_token_shows_new_user_form(self):
        result = self._invite()

        response = self.client.get(
            reverse(
                "member_invitation_accept",
                kwargs={"token": result.raw_token},
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Completion Family")

    def test_get_with_invalid_token_shows_invalid_token_page(self):
        response = self.client.get(
            reverse(
                "member_invitation_accept",
                kwargs={"token": "not-a-real-token"},
            ),
        )
        self.assertEqual(response.status_code, 400)

    def test_post_new_user_joins_family_and_logs_in(self):
        result = self._invite()

        response = self.client.post(
            reverse(
                "member_invitation_accept",
                kwargs={"token": result.raw_token},
            ),
            {
                "username": "brand_new_member",
                "password1": "a-very-strong-password-99",
                "password2": "a-very-strong-password-99",
                "display_name": "",
            },
        )

        self.assertRedirects(
            response,
            reverse(
                "shopping_list", kwargs={"family_slug": self.family.slug},
            ),
        )
        membership = FamilyMembership.objects.get(
            user__username="brand_new_member",
        )
        self.assertEqual(membership.role, FamilyMembership.Role.MEMBER)
        self.assertEqual(membership.family, self.family)

    def test_existing_user_must_log_in_as_the_invited_account(self):
        existing_user = User.objects.create_user(
            username="owner_of_invite",
            password="a-very-strong-password-99",
            email="owner_of_invite@example.com",
        )
        result = self._invite(email="owner_of_invite@example.com")

        anon_response = self.client.get(
            reverse(
                "member_invitation_accept",
                kwargs={"token": result.raw_token},
            ),
        )
        self.assertEqual(anon_response.status_code, 302)
        self.assertIn("next=", anon_response["Location"])

        other_user = User.objects.create_user(
            username="someone_else", password="a-very-strong-password-99",
        )
        self.client.force_login(other_user)
        wrong_account_response = self.client.get(
            reverse(
                "member_invitation_accept",
                kwargs={"token": result.raw_token},
            ),
        )
        self.assertEqual(wrong_account_response.status_code, 403)

        self.client.force_login(existing_user)
        correct_response = self.client.post(
            reverse(
                "member_invitation_accept",
                kwargs={"token": result.raw_token},
            ),
            {"display_name": "Owner"},
        )
        self.assertRedirects(
            correct_response,
            reverse(
                "shopping_list", kwargs={"family_slug": self.family.slug},
            ),
        )
        self.assertEqual(User.objects.count(), 3)  # admin + existing_user + other_user # noqa
        membership = FamilyMembership.objects.get(user=existing_user)
        self.assertEqual(membership.display_name, "Owner")


class ForcePasswordChangeTests(TestCase):
    def setUp(self):
        self.family, self.admin = _create_family_with_admin(
            "Flagged Family", "flagged_admin",
        )
        self.member = invite_member_without_email(
            family=self.family, display_name="flagged_member",
            initial_password="temporary-password-123",
        )

    def test_middleware_redirects_every_page_until_password_is_changed(self):
        self.client.force_login(self.member.user)

        response = self.client.get(
            reverse(
                "shopping_list", kwargs={"family_slug": self.family.slug},
            ),
        )

        self.assertRedirects(
            response, reverse("force_password_change"),
            fetch_redirect_response=False,
        )

    def test_middleware_does_not_affect_users_without_the_flag(self):
        self.client.force_login(self.admin)

        response = self.client.get(
            reverse(
                "shopping_list", kwargs={"family_slug": self.family.slug},
            ),
        )

        self.assertEqual(response.status_code, 200)

    def test_middleware_lets_logout_through(self):
        self.client.force_login(self.member.user)

        response = self.client.post(reverse("account_logout"))

        # Logging out redirects somewhere (not looped back to the
        # password-change page); the key property is that it isn't
        # blocked.
        self.assertIn(response.status_code, (200, 302))

    def test_get_shows_form_prefilled_with_current_display_name(self):
        self.client.force_login(self.member.user)

        response = self.client.get(reverse("force_password_change"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="flagged_member"')

    def test_post_changes_password_clears_flag_and_unlocks_the_app(self):
        self.client.force_login(self.member.user)

        response = self.client.post(reverse("force_password_change"), {
            "new_password1": "a-very-strong-password-99",
            "new_password2": "a-very-strong-password-99",
            "display_name": "",
        })

        self.assertRedirects(
            response, reverse("logged_out_homepage"),
            fetch_redirect_response=False,
        )
        self.assertFalse(
            PendingPasswordChange.objects.filter(
                user=self.member.user,
            ).exists(),
        )

        # Session survives the password change (update_session_auth_hash).
        normal_response = self.client.get(
            reverse(
                "shopping_list", kwargs={"family_slug": self.family.slug},
            ),
        )
        self.assertEqual(normal_response.status_code, 200)

        # The new password actually works for a fresh login too.
        self.client.logout()
        self.assertTrue(self.client.login(
            username=self.member.user.username,password="a-very-strong-password-99",
        ))

    def test_post_can_also_change_display_name(self):
        self.client.force_login(self.member.user)

        self.client.post(reverse("force_password_change"), {
            "new_password1": "a-very-strong-password-99",
            "new_password2": "a-very-strong-password-99",
            "display_name": "New Display Name",
        })

        membership = FamilyMembership.objects.get(user=self.member.user)
        self.assertEqual(membership.display_name, "New Display Name")

    def test_post_rejects_mismatched_passwords_and_keeps_the_flag(self):
        self.client.force_login(self.member.user)

        response = self.client.post(reverse("force_password_change"), {
            "new_password1": "a-very-strong-password-99",
            "new_password2": "a-different-password-00",
            "display_name": "",
        })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            PendingPasswordChange.objects.filter(
                user=self.member.user,
            ).exists(),
        )

    def test_post_rejects_weak_password_and_keeps_the_flag(self):
        self.client.force_login(self.member.user)

        response = self.client.post(reverse("force_password_change"), {
            "new_password1": "password",
            "new_password2": "password",
            "display_name": "",
        })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            PendingPasswordChange.objects.filter(
                user=self.member.user,
            ).exists(),
        )

    def test_service_function_rejects_display_name_collision(self):
        with self.assertRaises(InvitationConflictError):
            complete_forced_password_change(
                user=self.member.user,
                new_password="a-very-strong-password-99",
                display_name="flagged_admin",
            )
        self.assertTrue(
            PendingPasswordChange.objects.filter(
                user=self.member.user,
            ).exists(),
        )
