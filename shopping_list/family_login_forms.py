"""
Form for the per-family login door (see family_login_views.py): a
member authenticates with their family-local display name, not
Django's globally-unique (and, for no-email members, auto-generated and
hidden) username.
"""
from django import forms


class FamilyLoginForm(forms.Form):
    display_name = forms.CharField(max_length=100)
    password = forms.CharField(strip=False, widget=forms.PasswordInput)

    def clean_display_name(self):
        return self.cleaned_data["display_name"].strip()
