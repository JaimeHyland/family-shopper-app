"""
Sends the Family-admin registration verification email.

`build_registration_completion_url` is the one function that knows about
the web completion URL. A future deployment can swap it out for a Flutter
app link / universal link builder without changing anything else here or
in the registration service layer.
"""
from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.urls import reverse


def build_registration_completion_url(request, raw_token: str) -> str:
    path = reverse("registration_complete", kwargs={"token": raw_token})
    return request.build_absolute_uri(path)


def send_registration_verification_email(
    request, pending_registration, raw_token: str,
) -> None:
    completion_url = build_registration_completion_url(request, raw_token)
    context = {
        "family_name": pending_registration.family_name,
        "completion_url": completion_url,
    }
    subject = "Confirm your family's shopping list registration"
    body = render_to_string("registration/verification_email.txt", context)

    send_mail(
        subject,
        body,
        settings.DEFAULT_FROM_EMAIL,
        [pending_registration.email],
    )
