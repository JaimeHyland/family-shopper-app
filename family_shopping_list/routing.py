from django.urls import path
from . import consumers

websocket_urlpatterns = [
    path(
        'ws/families/<slug:family_slug>/shopping-list/',
        consumers.ShoppingConsumer.as_asgi(),
    ),
]
