"""
Forms for inviting a new member into an existing family and for the
subsequent first-login mandatory password change. Mirrors
registration_forms.py's conventions.
"""
from django import forms
from django.contrib.auth.forms import UsernameField
from django.contrib.auth.models import User
from django.contrib.auth.validators import UnicodeUsernameValidator


class InviteByEmailForm(forms.Form):
    """Admin-side: invite a new member by email."""

    email = forms.EmailField()

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()


class InviteWithoutEmailForm(forms.Form):
    """
    Admin-side: invite a new member by picking their family-local
    display name and an initial password -- no email, and no technical
    username, involved. Django's required globally-unique username is
    generated internally (see invitations._generate_internal_username)
    -- neither the admin nor the invitee ever needs to choose or see it.
    """

    display_name = forms.CharField(max_length=100)
    initial_password = forms.CharField(
        strip=False, widget=forms.PasswordInput,
    )
    initial_password2 = forms.CharField(
        strip=False, widget=forms.PasswordInput,
        label="Confirm initial password",
    )

    def clean_display_name(self):
        return self.cleaned_data["display_name"].strip()

    def clean(self):
        cleaned_data = super().clean()
        initial_password = cleaned_data.get("initial_password")
        initial_password2 = cleaned_data.get("initial_password2")

        if (
            initial_password
            and initial_password2
            and initial_password != initial_password2
        ):
            self.add_error(
                "initial_password2",
                "The two password fields didn't match.",
            )

        return cleaned_data


class InvitationCompletionForm(forms.Form):
    """Stage 2 (new email -> new account): choose username + password."""

    username = UsernameField(
        max_length=150,
        validators=[UnicodeUsernameValidator()],
    )
    password1 = forms.CharField(strip=False, widget=forms.PasswordInput)
    password2 = forms.CharField(strip=False, widget=forms.PasswordInput)
    display_name = forms.CharField(max_length=100, required=False)

    def clean_username(self):
        username = self.cleaned_data["username"]
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("That username is already taken.")
        return username

    def clean_display_name(self):
        return self.cleaned_data["display_name"].strip()

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get("password1")
        password2 = cleaned_data.get("password2")

        if password1 and password2 and password1 != password2:
            self.add_error("password2", "The two password fields didn't match.") # noqa

        return cleaned_data


class ExistingUserInvitationConfirmationForm(forms.Form):
    """Stage 2 (email already belongs to a logged-in user): just a display name.""" # noqa

    display_name = forms.CharField(max_length=100, required=False)

    def clean_display_name(self):
        return self.cleaned_data["display_name"].strip()


class FirstLoginPasswordChangeForm(forms.Form):
    """
    First-login mandatory password change for members invited without
    email. The display name is optional; the password change is not.
    """

    new_password1 = forms.CharField(
        strip=False, widget=forms.PasswordInput, label="New password",
    )
    new_password2 = forms.CharField(
        strip=False, widget=forms.PasswordInput,
        label="Confirm new password",
    )
    display_name = forms.CharField(max_length=100, required=False)

    def clean_display_name(self):
        return self.cleaned_data["display_name"].strip()

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get("new_password1")
        password2 = cleaned_data.get("new_password2")

        if password1 and password2 and password1 != password2:
            self.add_error("new_password2", "The two password fields didn't match.") # noqa

        return cleaned_data
