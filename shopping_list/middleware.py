"""
Gates every authenticated request behind a mandatory password change for
accounts created via the no-email member-invitation path (see
shopping_list.invitations.invite_member_without_email). The existence of
a PendingPasswordChange row for request.user IS the "must change
password" flag -- see that model's docstring in models.py.
"""
from django.shortcuts import redirect
from django.urls import Resolver404, resolve

# URL names a user with a pending password change may still reach --
# the page that actually clears the flag, and a deliberate escape hatch
# to log out instead of dealing with it right now.
EXEMPT_URL_NAMES = {
    "force_password_change",
    "account_logout",
}


class ForcePasswordChangeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and not request.path.startswith("/admin/")
            and hasattr(user, "pending_password_change")
        ):
            try:
                url_name = resolve(request.path_info).url_name
            except Resolver404:
                url_name = None
            if url_name not in EXEMPT_URL_NAMES:
                return redirect("force_password_change")

        return self.get_response(request)
