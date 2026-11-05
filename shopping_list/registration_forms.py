from django import forms


class FamilyAdminRegistrationRequestForm(forms.Form):
    """Stage 1: request registration with a family name and an email."""

    admin_name = forms.CharField(max_length=100, label="Your name")
    family_name = forms.CharField(max_length=100)
    email = forms.EmailField()

    def clean_admin_name(self):
        return self.cleaned_data["admin_name"].strip()

    def clean_family_name(self):
        return self.cleaned_data["family_name"].strip()

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()


class SoloRegistrationRequestForm(forms.Form):
    """Stage 1 of solo registration: the person's own name and an email."""

    name = forms.CharField(max_length=100, label="Your name")
    email = forms.EmailField()

    def clean_name(self):
        return self.cleaned_data["name"].strip()

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()


class FamilyAdminRegistrationCompletionForm(forms.Form):
    """
    Stage 2 (new email): choose the account's password. The Django
    username is generated internally and never shown or chosen here --
    see shopping_list.registration.generate_internal_username.
    """

    password1 = forms.CharField(strip=False, widget=forms.PasswordInput)
    password2 = forms.CharField(strip=False, widget=forms.PasswordInput)
    display_name = forms.CharField(max_length=100, required=False)

    def clean_display_name(self):
        return self.cleaned_data["display_name"].strip()

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get("password1")
        password2 = cleaned_data.get("password2")

        if password1 and password2 and password1 != password2:
            self.add_error("password2", "The two password fields didn't match.") # noqa

        return cleaned_data


class ExistingUserFamilyConfirmationForm(forms.Form):
    """Stage 2 (email already belongs to a logged-in user): just a display name.""" # noqa

    display_name = forms.CharField(max_length=100, required=False)

    def clean_display_name(self):
        return self.cleaned_data["display_name"].strip()
