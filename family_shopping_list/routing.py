from django.urls import path
from . import consumers

websocket_urlpatterns = [
    path(
        'ws/families/<uuid:family_public_id>/shopping-list/',
        consumers.ShoppingConsumer.as_asgi(),
    ),
]
