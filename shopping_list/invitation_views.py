"""
HTTP/form handling for inviting new members into an existing family,
accepting that invitation, and the mandatory first-login password change
for members invited without email. All business logic lives in
invitations.py; these views only translate between HTTP requests and
that service layer, mirroring registration_views.py's separation.
"""
import logging

from django.contrib import messages
from django.contrib.auth import login, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View

from utilities.utils import is_family_admin

from .emails import send_member_invitation_email
from .family_login_views import FAMILY_LOGIN_SESSION_KEY
from .invitation_forms import (
    ExistingUserInvitationConfirmationForm,
    FirstLoginPasswordChangeForm,
    InvitationCompletionForm,
    InviteByEmailForm,
    InviteWithoutEmailForm,
)
from .invitations import (
    AmbiguousEmailOwnershipError,
    InvalidInvitationTokenError,
    InvitationAuthorizationError,
    InvitationConflictError,
    PasswordPolicyError,
    complete_forced_password_change,
    complete_invitation_for_existing_user,
    complete_invitation_for_new_user,
    get_valid_pending_invitation,
    invite_member_by_email,
    invite_member_without_email,
)
from .models import FamilyMembership
from .registration import find_existing_user_for_email
from .views import FamilyMembershipRequiredMixin

logger = logging.getLogger(__name__)


@method_decorator(login_required, name="dispatch")
class InviteMemberView(FamilyMembershipRequiredMixin, View):
    """
    Admin-only: invite a new member into request.family, either by email
    or by directly setting a username + initial password. Only family
    admins may invite members; shared-family membership stays
    invitation-only (there is no self-service "join this family" path).
    """

    def get(self, request, *args, **kwargs):
        if not is_family_admin(request):
            raise PermissionDenied
        return render(request, "shopping_list/invite_member.html", {
            "email_form": InviteByEmailForm(),
            "no_email_form": InviteWithoutEmailForm(),
        })

    def post(self, request, *args, **kwargs):
        if not is_family_admin(request):
            raise PermissionDenied

        method = request.POST.get("method")
        if method == "email":
            return self._invite_by_email(request)
        if method == "no_email":
            return self._invite_without_email(request)
        return HttpResponseBadRequest("Unknown invitation method.")

    def _invite_by_email(self, request):
        form = InviteByEmailForm(request.POST)
        no_email_form = InviteWithoutEmailForm()
        if not form.is_valid():
            return render(request, "shopping_list/invite_member.html", {
                "email_form": form, "no_email_form": no_email_form,
            })

        try:
            result = invite_member_by_email(
                family=request.family, email=form.cleaned_data["email"],
                invited_by=request.user,
            )
        except InvitationConflictError as exc:
            form.add_error(None, str(exc))
            return render(request, "shopping_list/invite_member.html", {
                "email_form": form, "no_email_form": no_email_form,
            })
        except AmbiguousEmailOwnershipError:
            form.add_error(
                None,
                "This email address can't be checked automatically. "
                "Please contact support.",
            )
            return render(request, "shopping_list/invite_member.html", {
                "email_form": form, "no_email_form": no_email_form,
            })

        if result is None:
            messages.error(
                request,
                "An invitation to that address is already pending for "
                "this family. Please try again in a moment, or check "
                "with the family if they've already received one.",
            )
            return render(request, "shopping_list/invite_member.html", {
                "email_form": form, "no_email_form": no_email_form,
            })

        try:
            send_member_invitation_email(
                request, result.pending_invitation, result.raw_token,
            )
        except Exception:
            logger.exception("Failed to send member invitation email")
            result.pending_invitation.delete()
            messages.error(
                request,
                "We couldn't send the invitation email just now. "
                "Please try again.",
            )
            return render(request, "shopping_list/invite_member.html", { # noqa
                "email_form": form, "no_email_form": no_email_form,
            })

        messages.success(
            request, f"Invitation sent to {form.cleaned_data['email']}.",
        )
        return redirect("family_members", family_slug=request.family.slug)

    def _invite_without_email(self, request):
        form = InviteWithoutEmailForm(request.POST)
        email_form = InviteByEmailForm()
        if not form.is_valid():
            return render(request, "shopping_list/invite_member.html", {
                "email_form": email_form, "no_email_form": form,
            })

        try:
            membership = invite_member_without_email(
                family=request.family,
                display_name=form.cleaned_data["display_name"],
                initial_password=form.cleaned_data["initial_password"],
            )
        except InvitationConflictError as exc:
            form.add_error(None, str(exc))
            return render(request, "shopping_list/invite_member.html", {
                "email_form": email_form, "no_email_form": form,
            })
        except PasswordPolicyError as exc:
            for message in exc.messages:
                form.add_error("initial_password", message)
            return render(request, "shopping_list/invite_member.html", {
                "email_form": email_form, "no_email_form": form,
            })

        login_url = request.build_absolute_uri(
            reverse(
                "family_login", kwargs={"family_slug": request.family.slug},
            ),
        )
        messages.success(
            request,
            f"Created an account for \"{membership.display_name}\". "
            f"Share this family's login page ({login_url}) and the "
            "initial password with them directly -- they'll log in with "
            "their display name and be asked to change the password on "
            "first login.",
        )
        return redirect("family_members", family_slug=request.family.slug)


class MemberInvitationCompletionView(View):
    """
    GET validates the token and shows the appropriate completion form;
    POST consumes it and adds the membership (+ user, for a new email).
    Mirrors registration_views.RegistrationCompletionView.
    """

    def get(self, request, token, *args, **kwargs):
        try:
            pending = get_valid_pending_invitation(token)
        except InvalidInvitationTokenError:
            return render(
                request, "registration/invalid_token.html", status=400,
            )

        try:
            existing_user = find_existing_user_for_email(pending.email)
        except AmbiguousEmailOwnershipError:
            return self._ambiguous_response(request)

        if existing_user is None:
            return render(request, "invitations/accept_new_user.html", {
                "form": InvitationCompletionForm(),
                "family_name": pending.family.name,
            })

        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())

        if request.user.pk != existing_user.pk:
            return self._wrong_account_response(request)

        return render(request, "invitations/accept_existing_user.html", {
            "form": ExistingUserInvitationConfirmationForm(),
            "family_name": pending.family.name,
        })

    def post(self, request, token, *args, **kwargs):
        try:
            pending = get_valid_pending_invitation(token)
        except InvalidInvitationTokenError:
            return render(
                request, "registration/invalid_token.html", status=400,
            )

        try:
            existing_user = find_existing_user_for_email(pending.email)
        except AmbiguousEmailOwnershipError:
            return self._ambiguous_response(request)

        if existing_user is None:
            return self._post_new_user(request, token, pending)

        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())

        if request.user.pk != existing_user.pk:
            return self._wrong_account_response(request)

        return self._post_existing_user(request, token, pending)

    def _post_new_user(self, request, token, pending):
        form = InvitationCompletionForm(request.POST)
        if not form.is_valid():
            return render(request, "invitations/accept_new_user.html", {
                "form": form, "family_name": pending.family.name,
            })

        try:
            membership = complete_invitation_for_new_user(
                raw_token=token,
                username=form.cleaned_data["username"],
                password=form.cleaned_data["password1"],
                display_name=form.cleaned_data["display_name"],
            )
        except InvalidInvitationTokenError:
            return render(
                request, "registration/invalid_token.html", status=400,
            )
        except PasswordPolicyError as exc:
            for message in exc.messages:
                form.add_error("password1", message)
            return render(request, "invitations/accept_new_user.html", {
                "form": form, "family_name": pending.family.name,
            })
        except InvitationConflictError as exc:
            form.add_error(None, str(exc))
            return render(request, "invitations/accept_new_user.html", {
                "form": form, "family_name": pending.family.name,
            })

        login(
            request, membership.user,
            backend="django.contrib.auth.backends.ModelBackend",
        )
        messages.success(
            request, f"Welcome! You've joined {membership.family.name}.",
        )
        return redirect(
            "shopping_list", family_slug=membership.family.slug,
        )

    def _post_existing_user(self, request, token, pending):
        form = ExistingUserInvitationConfirmationForm(request.POST)
        if not form.is_valid():
            return render(request, "invitations/accept_existing_user.html", { # noqa
                "form": form, "family_name": pending.family.name,
            })

        try:
            membership = complete_invitation_for_existing_user(
                raw_token=token, user=request.user,
                display_name=form.cleaned_data["display_name"],
            )
        except InvalidInvitationTokenError:
            return render(
                request, "registration/invalid_token.html", status=400,
            )
        except InvitationAuthorizationError:
            return self._wrong_account_response(request)
        except InvitationConflictError as exc:
            form.add_error(None, str(exc))
            return render(request, "invitations/accept_existing_user.html", { # noqa
                "form": form, "family_name": pending.family.name,
            })

        messages.success(
            request, f"You've joined {membership.family.name}.",
        )
        return redirect(
            "shopping_list", family_slug=membership.family.slug,
        )

    def _ambiguous_response(self, request):
        return render(
            request, "invitations/cannot_complete.html",
            {
                "message": (
                    "This invitation can't be completed automatically. "
                    "Please contact the family admin."
                ),
            },
            status=409,
        )

    def _wrong_account_response(self, request):
        return render(
            request, "invitations/cannot_complete.html",
            {
                "message": (
                    "This invitation link is for a different account. "
                    "Please log out and try again with the correct "
                    "account."
                ),
            },
            status=403,
        )


@method_decorator(login_required, name="dispatch")
class ForcePasswordChangeView(View):
    """
    First-login mandatory password change for members invited without
    email (see shopping_list.middleware.ForcePasswordChangeMiddleware,
    which is what actually routes every other request here).
    """

    def get(self, request, *args, **kwargs):
        if not hasattr(request.user, "pending_password_change"):
            return redirect("logged_out_homepage")
        membership = FamilyMembership.objects.filter(
            user=request.user,
        ).first()
        initial = {}
        if membership is not None:
            initial["display_name"] = membership.display_name
        return render(
            request, "shopping_list/first_login_password_change.html",
            {"form": FirstLoginPasswordChangeForm(initial=initial)},
        )

    def post(self, request, *args, **kwargs):
        if not hasattr(request.user, "pending_password_change"):
            return redirect("logged_out_homepage")

        form = FirstLoginPasswordChangeForm(request.POST)
        if not form.is_valid():
            return render(
                request, "shopping_list/first_login_password_change.html",
                {"form": form},
            )

        try:
            complete_forced_password_change(
                user=request.user,
                new_password=form.cleaned_data["new_password1"],
                display_name=form.cleaned_data["display_name"],
            )
        except PasswordPolicyError as exc:
            for message in exc.messages:
                form.add_error("new_password1", message)
            return render(
                request, "shopping_list/first_login_password_change.html",
                {"form": form},
            )
        except InvitationConflictError as exc:
            form.add_error("display_name", str(exc))
            return render(
                request, "shopping_list/first_login_password_change.html",
                {"form": form},
            )

        # The user's row changed under them mid-session; without this
        # they'd be logged out by Django's session-auth-hash check on
        # the very next request.
        update_session_auth_hash(request, request.user)
        messages.success(request, "Your password has been updated.")

        # Go straight back into the family they authenticated through
        # (see family_login_views.FamilyLoginView), rather than the
        # generic membership-count-based home-page redirect -- which
        # would otherwise take an unnecessary extra hop, or even 403
        # if, say, their membership was removed in the meantime.
        family_slug = request.session.pop(FAMILY_LOGIN_SESSION_KEY, None)
        if family_slug:
            membership = FamilyMembership.objects.filter(
                user=request.user, family__slug=family_slug,
            ).first()
            if membership is not None:
                return redirect(
                    "shopping_list", family_slug=family_slug,
                )

        return redirect("logged_out_homepage")
