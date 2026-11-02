"""
HTTP/form handling for public Family-admin registration. All business
logic lives in registration.py; these views only translate between
HTTP requests and that service layer, so the same services can later be
reused by a JSON/Flutter-facing API.
"""
import logging

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import redirect, render
from django.utils.decorators import method_decorator
from django.views import View

from .emails import send_registration_verification_email
from .registration import (
    AmbiguousEmailOwnershipError,
    InvalidRegistrationTokenError,
    PasswordPolicyError,
    RegistrationAuthorizationError,
    RegistrationConflictError,
    complete_family_admin_registration_for_existing_user,
    complete_family_admin_registration_for_new_user,
    find_existing_user_for_email,
    get_valid_pending_registration,
    invalidate_pending_registration,
    request_family_admin_registration,
)
from .registration_forms import (
    ExistingUserFamilyConfirmationForm,
    FamilyAdminRegistrationCompletionForm,
    FamilyAdminRegistrationRequestForm,
    SoloRegistrationRequestForm,
)
from .views import get_user_memberships

logger = logging.getLogger(__name__)


class AuthenticatedRedirectMixin:
    """
    Routes an already-authenticated visitor away from the anonymous-only
    welcome/registration pages, by membership count: straight into their
    one family, to a chooser if they have several, or a controlled 403 if
    they somehow have none.
    """

    def _redirect_authenticated(self, request):
        memberships = list(get_user_memberships(request.user))
        if not memberships:
            # Preserves the existing controlled no-family 403 response.
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied("You are not assigned to a family.")
        if len(memberships) == 1:
            return redirect(
                "shopping_list",
                family_slug=memberships[0].family.slug,
            )
        return redirect("family_chooser")


class HomeView(AuthenticatedRedirectMixin, View):
    """
    The public welcome page at '/'.

    Anonymous: presents exactly two choices -- start a new registration,
    or log in to an existing account. No registration form lives here.
    Authenticated: routed by membership count (see
    `_redirect_authenticated`).
    """

    def get(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return self._redirect_authenticated(request)
        return render(request, "logged_out_homepage.html")


class RegistrationStartView(AuthenticatedRedirectMixin, View):
    """Reached via "I'm new here": choose solo vs. family registration."""

    def get(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return self._redirect_authenticated(request)
        return render(request, "registration/start.html")


class _RegistrationRequestView(AuthenticatedRedirectMixin, View):
    """
    Shared stage-1 logic for both solo and family registration: validate
    the form, create a pending registration, and email a verification
    link. Subclasses only supply the form, the template, and how to read
    the family/account name out of the validated form; everything else
    -- including the email-verification safeguards -- is identical.
    """

    form_class = None
    template_name = None

    def get_family_name(self, form):
        raise NotImplementedError

    def get_display_name(self, form):
        """The applicant's own name, carried through to completion."""
        raise NotImplementedError

    def get(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return self._redirect_authenticated(request)
        return render(request, self.template_name, {
            "form": self.form_class(),
        })

    def post(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return self._redirect_authenticated(request)

        form = self.form_class(request.POST)
        if form.is_valid():
            result = request_family_admin_registration(
                family_name=self.get_family_name(form),
                email=form.cleaned_data["email"],
                display_name=self.get_display_name(form),
            )
            if result is not None:
                try:
                    send_registration_verification_email(
                        request,
                        result.pending_registration,
                        result.raw_token,
                    )
                except Exception:
                    logger.exception(
                        "Failed to send registration verification email",
                    )
                    invalidate_pending_registration(
                        result.pending_registration,
                    )
                    messages.error(
                        request,
                        "We couldn't send the verification email just now. "
                        "Please try again.",
                    )
                    return render(request, self.template_name, {
                        "form": form,
                    })
            # Same generic outcome regardless of cooldown/existing-account/
            # brand-new-request -- never discloses which case applied.
            return redirect("registration_check_email")

        return render(request, self.template_name, {"form": form})


class FamilyRegistrationRequestView(_RegistrationRequestView):
    """Stage 1 of family registration: family name + email."""

    form_class = FamilyAdminRegistrationRequestForm
    template_name = "registration/family_request.html"

    def get_family_name(self, form):
        return form.cleaned_data["family_name"]

    def get_display_name(self, form):
        return form.cleaned_data["admin_name"]


class SoloRegistrationRequestView(_RegistrationRequestView):
    """
    Stage 1 of solo registration: the person's own name + email. Reuses
    the family-admin registration service unchanged -- the resulting
    Family simply isn't named yet at this stage; it defaults to whatever
    username the person picks at completion (see
    `complete_family_admin_registration_for_new_user`/`..._for_existing_
    user`), so it ends up containing just that one user without ever
    asking for a "family name" at all.
    """

    form_class = SoloRegistrationRequestForm
    template_name = "registration/solo_request.html"

    def get_family_name(self, form):
        return ""

    def get_display_name(self, form):
        return form.cleaned_data["name"]


def registration_check_email(request):
    return render(request, "registration/check_email.html")


@method_decorator(login_required, name='dispatch')
class FamilyChooserView(View):
    """Lists only the current user's own memberships."""

    def get(self, request, *args, **kwargs):
        memberships = list(get_user_memberships(request.user))
        return render(request, "family_chooser.html", {
            "memberships": memberships,
        })


class RegistrationCompletionView(View):
    """
    GET validates the token and shows the appropriate completion form;
    POST consumes it and creates the family (+ user, for a new email).
    """

    def get(self, request, token, *args, **kwargs):
        try:
            pending = get_valid_pending_registration(token)
        except InvalidRegistrationTokenError:
            return render(
                request, "registration/invalid_token.html", status=400,
            )

        try:
            existing_user = find_existing_user_for_email(pending.email)
        except AmbiguousEmailOwnershipError:
            return self._ambiguous_response(request)

        if existing_user is None:
            return render(request, "registration/complete.html", {
                "form": FamilyAdminRegistrationCompletionForm(
                    initial={"display_name": pending.display_name},
                ),
                "family_name": pending.family_name,
            })

        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())

        if request.user.pk != existing_user.pk:
            return self._wrong_account_response(request)

        return render(request, "registration/confirm_existing.html", {
            "form": ExistingUserFamilyConfirmationForm(
                initial={"display_name": pending.display_name},
            ),
            "family_name": pending.family_name,
        })

    def post(self, request, token, *args, **kwargs):
        try:
            pending = get_valid_pending_registration(token)
        except InvalidRegistrationTokenError:
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
        form = FamilyAdminRegistrationCompletionForm(request.POST)
        if not form.is_valid():
            return render(request, "registration/complete.html", {
                "form": form,
                "family_name": pending.family_name,
            })

        try:
            result = complete_family_admin_registration_for_new_user(
                raw_token=token,
                username=form.cleaned_data["username"],
                password=form.cleaned_data["password1"],
                display_name=form.cleaned_data["display_name"],
            )
        except InvalidRegistrationTokenError:
            return render(
                request, "registration/invalid_token.html", status=400,
            )
        except PasswordPolicyError as exc:
            for message in exc.messages:
                form.add_error("password1", message)
            return render(request, "registration/complete.html", {
                "form": form,
                "family_name": pending.family_name,
            })
        except RegistrationConflictError as exc:
            form.add_error(None, str(exc))
            return render(request, "registration/complete.html", {
                "form": form,
                "family_name": pending.family_name,
            })

        login(
            request, result.user,
            backend="django.contrib.auth.backends.ModelBackend",
        )
        messages.success(request, "Welcome! Your family has been created.")
        return redirect(
            "seed_lists", family_slug=result.family.slug,
        )

    def _post_existing_user(self, request, token, pending):
        form = ExistingUserFamilyConfirmationForm(request.POST)
        if not form.is_valid():
            return render(request, "registration/confirm_existing.html", {
                "form": form,
                "family_name": pending.family_name,
            })

        try:
            result = complete_family_admin_registration_for_existing_user(
                raw_token=token,
                user=request.user,
                display_name=form.cleaned_data["display_name"],
            )
        except InvalidRegistrationTokenError:
            return render(
                request, "registration/invalid_token.html", status=400,
            )
        except RegistrationAuthorizationError:
            return self._wrong_account_response(request)

        messages.success(
            request, "Your new family has been created.",
        )
        return redirect(
            "seed_lists", family_slug=result.family.slug,
        )

    def _ambiguous_response(self, request):
        return render(
            request, "registration/cannot_complete.html",
            {
                "message": (
                    "This registration can't be completed automatically. "
                    "Please contact support."
                ),
            },
            status=409,
        )

    def _wrong_account_response(self, request):
        return render(
            request, "registration/cannot_complete.html",
            {
                "message": (
                    "This registration link is for a different account. "
                    "Please log out and try again with the correct account."
                ),
            },
            status=403,
        )
