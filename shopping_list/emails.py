"""
Sends the Family-admin registration verification email and the
member-invitation email.

`build_registration_completion_url`/`build_invitation_completion_url` are
the only functions that know about these web completion URLs. A future
deployment can swap them out for a Flutter app link / universal link
builder without changing anything else here or in the registration/
invitation service layers.
"""
from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.urls import reverse


def build_registration_completion_url(request, raw_token: str) -> str:
    path = reverse("registration_complete", kwargs={"token": raw_token})
    return request.build_absolute_uri(path)


def build_invitation_completion_url(request, raw_token: str) -> str:
    path = reverse("member_invitation_accept", kwargs={"token": raw_token})
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


def send_member_invitation_email(
    request, pending_invitation, raw_token: str,
) -> None:
    completion_url = build_invitation_completion_url(request, raw_token)
    context = {
        "family_name": pending_invitation.family.name,
        "completion_url": completion_url,
    }
    subject = f"You've been invited to join {pending_invitation.family.name}" # noqa
    body = render_to_string("invitations/member_invitation_email.txt", context) # noqa

    send_mail(
        subject,
        body,
        settings.DEFAULT_FROM_EMAIL,
        [pending_invitation.email],
    )
