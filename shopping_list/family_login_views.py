"""
Per-family login door: a member authenticates using their family-local
display name (not Django's globally-unique, and for no-email members
auto-generated and hidden, username) + password, scoped to one specific
family via its persistent slug. This reuses the existing identity
architecture rather than weakening it: FamilyMembership.display_name
resolves to a User, whose real password is then verified via Django's
normal authentication backend -- this is not a parallel or weaker
password check, just a different way of identifying WHICH account to
check it against.

Family isolation: resolution is always scoped by `family=family` FIRST
(from the URL's persistent slug) -- an account that only has a
membership in a DIFFERENT family can never be found here, regardless of
whether its display name happens to match.

Anti-enumeration: every failure (unknown display name in this family,
or a matching display name with the wrong password) renders the exact
same generic error. A nonexistent display name also still pays the cost
of a password hash (mirroring Django's own ModelBackend mitigation for
"no such username"), so the response time doesn't disclose whether the
display name exists in this family either.
"""
from allauth.core import ratelimit
from django.contrib.auth import authenticate, login
from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from .family_login_forms import FamilyLoginForm
from .models import Family, FamilyMembership

GENERIC_LOGIN_ERROR = "Invalid display name or password."

# Remembers which family a member authenticated through, purely so that
# shopping_list.invitation_views.ForcePasswordChangeView can send them
# straight back into THAT family afterwards instead of the generic
# membership-count-based home-page redirect (see that view for why).
FAMILY_LOGIN_SESSION_KEY = "family_login_slug"


class FamilyLoginView(View):
    """
    GET shows this family's own login form; POST authenticates by
    (family, display_name) and logs the member straight into THIS
    family -- never the generic multi-family chooser, since visiting
    this specific URL already signals which family they mean.
    """

    def get(self, request, family_slug, *args, **kwargs):
        family = get_object_or_404(Family, slug=family_slug)
        return render(request, "shopping_list/family_login.html", {
            "family": family,
            "form": FamilyLoginForm(),
        })

    def post(self, request, family_slug, *args, **kwargs):
        family = get_object_or_404(Family, slug=family_slug)
        form = FamilyLoginForm(request.POST)
        if not form.is_valid():
            return render(request, "shopping_list/family_login.html", {
                "family": family, "form": form,
            })

        display_name = form.cleaned_data["display_name"]
        password = form.cleaned_data["password"]
        rate_limit_key = f"{family.slug}:{display_name.lower()}"

        if not ratelimit.consume(
            request, action="family_login_failed", key=rate_limit_key,
        ):
            form.add_error(None, GENERIC_LOGIN_ERROR)
            return render(request, "shopping_list/family_login.html", {
                "family": family, "form": form,
            }, status=429)

        membership = FamilyMembership.objects.filter(
            family=family, display_name__iexact=display_name,
        ).select_related("user").first()

        if membership is not None:
            user = authenticate(
                request, username=membership.user.username,
                password=password,
            )
        else:
            # Mirrors Django's own ModelBackend mitigation for "no such
            # username": hash the submitted password anyway, so a
            # nonexistent display name in this family takes the same
            # time to reject as a wrong password for a real member.
            User().set_password(password)
            user = None

        if user is None:
            form.add_error(None, GENERIC_LOGIN_ERROR)
            return render(request, "shopping_list/family_login.html", {
                "family": family, "form": form,
            })

        ratelimit.clear(
            request, action="family_login_failed", key=rate_limit_key,
        )
        login(request, user)
        request.session[FAMILY_LOGIN_SESSION_KEY] = family.slug
        return redirect("shopping_list", family_slug=family.slug)
