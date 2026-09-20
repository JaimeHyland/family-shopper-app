from allauth.account.adapter import DefaultAccountAdapter


class NoPublicSignupAccountAdapter(DefaultAccountAdapter):
    """
    Closes allauth's ordinary /accounts/signup/ flow. Public registration
    only happens through the dedicated Family-admin verified-email flow
    starting at '/'.
    """

    def is_open_for_signup(self, request):
        return False
