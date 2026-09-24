from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
import json

from shopping_list.models import FamilyMembership


class ShoppingConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        user = self.scope.get("user")

        if user is None or not user.is_authenticated:
            await self.close(code=4401)
            return

        family_slug = self.scope["url_route"]["kwargs"].get(
            "family_slug",
        )
        family_id = await self._get_member_family_id(user, family_slug)
        if family_id is None:
            await self.close(code=4403)
            return

        self.family_id = family_id
        # Each family gets its own group so updates never cross families
        self.group_name = f"shopping_list_updates_{family_id}"

        await self.channel_layer.group_add(
            self.group_name,
            self.channel_name
        )

        await self.accept()

    async def disconnect(self, close_code):
        group_name = getattr(self, "group_name", None)
        if group_name:
            await self.channel_layer.group_discard(
                group_name,
                self.channel_name
            )

    async def receive(self, text_data):
        # Client payloads are never trusted or rebroadcast: mutations happen
        # through the HTTP view/API, which then broadcasts the saved state.
        pass

    async def list_item_update(self, event):
        await self.send(text_data=json.dumps({
            'type': 'list_item_update',
            'list_item_id': event['list_item_id'],
            'in_basket': event['in_basket'],
            'cancelled': event['cancelled'],
            'bought': event['bought'],
            'product_name': event['product_name'],
            'quantity_bought': event['quantity_bought'],
            'buyer_display_name': event['buyer_display_name'],
            'user': event['user']
        }))

    async def basket_bought(self, event):
        await self.send(text_data=json.dumps({
            'type': 'basket_bought',
            'buyer_username': event['buyer_username'],
            'buyer_display_name': event['buyer_display_name'],
            'item_count': event['item_count'],
        }))

    @database_sync_to_async
    def _get_member_family_id(self, user, family_slug):
        # Explicitly resolves the REQUESTED family; never derives "the"
        # family via .first() now that a user may belong to several.
        if not family_slug:
            return None
        return (
            FamilyMembership.objects
            .filter(user=user, family__slug=family_slug)
            .values_list("family_id", flat=True)
            .first()
        )

