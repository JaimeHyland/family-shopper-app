from django.apps import AppConfig


class ShoppingListConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'shopping_list'

    def ready(self):
        from . import signals  # noqa: F401
