from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from threading import Barrier
from unittest.mock import patch

from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.core.management import call_command
from django.db import close_old_connections, connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.db.models.query import QuerySet
from django.test import Client, TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from family_shopping_list.consumers import ShoppingConsumer
from .models import ListItem, ShoppingList
from .tests import TwoFamilyTestCase, TwoFamilyFixtureMixin, ws_path
from .views import aggregate_unit_rows


class UnitControlTests(TwoFamilyTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.member_a)

    def action(self, item, action):
        return self.client.post(self.url_a('shopping_list'), {
            'item_id': item.pk, 'action': action,
        })

    def unit(self, **overrides):
        values = dict(
            family=self.family_a, product=self.product_a,
            creator=self.admin_a, request_key=self.list_item_a.request_key,
        )
        values.update(overrides)
        return ListItem.objects.create(**values)

    def controls(self, language="en"):
        class Controls(HTMLParser):
            def __init__(self):
                super().__init__()
                self.buttons = {}
                self.active = None

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'button' and 'data-basket-action' in attrs:
                    self.active = attrs
                    attrs['symbol'] = ''
                    self.buttons[attrs['data-basket-action']] = attrs

            def handle_data(self, data):
                if self.active is not None:
                    self.active['symbol'] += data

            def handle_endtag(self, tag):
                if tag == 'button':
                    self.active = None

        parser = Controls()
        parser.feed(self.client.get(self.url_a('shopping_list'), HTTP_ACCEPT_LANGUAGE=language).content.decode())
        return parser.buttons

    def test_controls_and_titles_follow_row_state_and_ownership(self):
        ordinary = self.controls()
        self.assertEqual(set(ordinary), {'add_unit', 'remove_unit', 'add_to_basket', 'remove_from_basket'})
        self.assertEqual({a for a, b in ordinary.items() if 'disabled' not in b}, {'add_unit', 'remove_unit', 'add_to_basket'})
        self.action(self.list_item_a, 'add_to_basket')
        own = self.controls()
        self.assertEqual(set(own), set(ordinary))
        self.assertEqual({a for a, b in own.items() if 'disabled' not in b}, {'add_unit', 'remove_from_basket'})
        expected = {
            'add_unit': ('+', 'Add one item to the shopping list'),
            'remove_unit': (chr(0x2212), 'Remove one item from the shopping list'),
            'add_to_basket': (chr(0x2193), 'Put one item in my basket'),
            'remove_from_basket': (chr(0x21B1), 'Return one item from my basket'),
        }
        for action, button in {**ordinary, **own}.items():
            symbol, title = expected[action]
            self.assertEqual(button['symbol'], symbol)
            self.assertEqual(button['title'], title)
            self.assertEqual(button['aria-label'], f'Product A: {title}')
            if 'disabled' in button:
                self.assertEqual(button['aria-disabled'], 'true')
        self.client.force_login(self.admin_a)
        self.assertEqual(set(self.controls()), set(ordinary))
        self.assertEqual({a for a, b in self.controls().items() if 'disabled' not in b}, {'add_unit'})
        self.client.force_login(self.member_a)
        self.action(self.list_item_a, 'remove_from_basket')
        self.action(self.list_item_a, 'cancel')
        self.assertTrue(all('disabled' in b for b in self.controls().values()))
        self.action(self.list_item_a, 'uncancel')
        self.action(self.list_item_a, 'add_to_basket')
        self.client.post(self.url_a('shopping_list'), {'action': 'buy_basket'})
        self.assertEqual(self.controls(), {})

    def test_disabled_controls_and_modal_are_accessible_and_localized(self):
        self.action(self.list_item_a, 'add_to_basket')
        self.client.force_login(self.admin_a)
        for language in ('en', 'de', 'es'):
            from .templatetags.i18n_json import _load_catalog
            from html import escape
            catalog = _load_catalog(language)
            page = self.client.get(self.url_a('shopping_list'), HTTP_ACCEPT_LANGUAGE=language)
            html = page.content.decode()
            keys = dict(add_unit='add_unit', remove_unit='remove_unit', add_to_basket='put_unit_in_basket', remove_from_basket='return_unit')
            for action, button in self.controls(language).items():
                self.assertEqual(button['title'], catalog['shopping_list.' + keys[action]])
                if 'disabled' in button:
                    self.assertIn('tabindex="0" role="button" aria-disabled="true"', html)
            self.assertIn('aria-labelledby="finalUnitModalLabel" aria-describedby="finalUnitModalDescription"', html)
            for key in ('title', 'description', 'dismiss', 'confirm'):
                self.assertIn(escape(catalog['shopping_list.final_unit.' + key]), html)
            self.assertIn('id="confirm-final-unit"', html)
            self.assertIn('data-bs-dismiss="modal"', html)

    def test_plus_creates_one_ordinary_unit_for_each_source_owner(self):
        for owner in (None, self.member_a, self.admin_a):
            with self.subTest(owner=owner):
                source = self.list_item_a
                source.in_basket = owner is not None
                source.put_in_basket_by = owner
                source.time_put_in_basket = timezone.now() if owner else None
                source.buyer = self.admin_a
                source.restored_by = self.admin_a
                source.creator_notes = 'shared request notes'
                source.save()
                before = list(ListItem.objects.order_by('pk').values())
                ids = [row['id'] for row in before]
                self.assertEqual(self.action(source, 'add_unit').status_code, 200)
                self.assertEqual(list(ListItem.objects.filter(pk__in=ids).order_by('pk').values()), before)
                new = ListItem.objects.exclude(pk__in=ids).get()
                for field in ('family_id', 'product_id', 'shopping_list_id',
                              'request_key', 'creator_id', 'preferred_shop_id',
                              'creator_notes', 'shopper_notes'):
                    self.assertEqual(getattr(new, field), getattr(source, field))
                self.assertEqual(new.quantity_required, 1)
                self.assertEqual(new.quantity_bought, 0)
                self.assertFalse(new.in_basket)
                self.assertFalse(new.cancelled)
                for field in ('put_in_basket_by_id', 'time_put_in_basket',
                              'buyer_id', 'date_bought', 'shop_bought_id',
                              'cancelled_by_id', 'date_cancelled', 'restored_by_id'):
                    self.assertIsNone(getattr(new, field), field)

    def test_minus_removes_only_ordinary_units_and_last_ordinary_row(self):
        ordinary = self.unit()
        baskets = [self.unit(in_basket=True, put_in_basket_by=owner)
                   for owner in (self.member_a, self.admin_a)]
        before = list(ListItem.objects.filter(pk__in=[u.pk for u in baskets]).order_by('pk').values())
        self.assertEqual(self.action(self.list_item_a, 'remove_unit').status_code, 200)
        self.assertFalse(ListItem.objects.filter(pk=self.list_item_a.pk).exists())
        page = self.client.get(self.url_a('shopping_list'))
        self.assertEqual({(r.state, r.unit_count) for r in page.context['shopping_list']}, {
            ('ordinary', 1), ('in_basket', 1),
        })
        self.assertEqual(self.action(ordinary, 'remove_unit').status_code, 409)
        self.assertEqual(self.action(ordinary, 'cancel_final_unit').status_code, 200)
        page = self.client.get(self.url_a('shopping_list'))
        self.assertTrue(all(r.state in ('in_basket', 'cancelled') for r in page.context['shopping_list']))
        self.assertEqual(list(ListItem.objects.filter(pk__in=[u.pk for u in baskets]).order_by('pk').values()), before)
        for unit in baskets:
            self.assertEqual(self.action(unit, 'remove_unit').status_code, 403)

    def test_final_unit_confirmation_preserves_data_until_confirmed(self):
        other = self.unit(in_basket=True, put_in_basket_by=self.admin_a)
        snapshot = list(ListItem.objects.order_by('pk').values())
        response = self.action(self.list_item_a, 'remove_unit')
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json()['confirmation_required'])
        self.assertEqual(response.json()['item_id'], self.list_item_a.pk)
        self.assertEqual(list(ListItem.objects.order_by('pk').values()), snapshot)
        self.assertEqual(self.action(self.list_item_a, 'cancel_final_unit').status_code, 200)
        self.list_item_a.refresh_from_db()
        self.assertTrue(self.list_item_a.cancelled)
        self.assertEqual(self.list_item_a.cancelled_by, self.member_a)
        self.assertIsNotNone(self.list_item_a.date_cancelled)
        self.assertIsNone(self.list_item_a.restored_by)
        self.assertEqual(ListItem.objects.filter(pk=other.pk).values().get(), snapshot[-1])

    def test_minus_counts_unbasketed_units_hidden_by_filters(self):
        from .models import Shop
        shop = Shop.objects.create(family=self.family_a, creator=self.admin_a,
                                   shop_name='Hidden shop', type_of_shop=1)
        self.unit(preferred_shop=shop)
        page = self.client.get(self.url_a('shopping_list'), {
            'filter_applied': '1', 'filter_shops': [self.shop_a.pk],
        })
        self.assertEqual(page.context['shopping_list'][0].unit_count, 1)
        self.assertEqual(self.action(self.list_item_a, 'remove_unit').status_code, 200)
        remaining = ListItem.objects.get(family=self.family_a)
        self.assertEqual(self.action(remaining, 'remove_unit').status_code, 409)

    def test_stale_confirmation_cannot_cancel_when_quantity_has_increased(self):
        self.assertEqual(self.action(self.list_item_a, 'remove_unit').status_code, 409)
        self.unit()
        snapshot = list(ListItem.objects.order_by('pk').values())
        self.assertEqual(self.action(self.list_item_a, 'cancel_final_unit').status_code, 409)
        self.assertEqual(list(ListItem.objects.order_by('pk').values()), snapshot)

    def test_down_and_return_move_existing_unit_without_creating_or_deleting(self):
        before = ListItem.objects.count()
        self.assertEqual(self.action(self.list_item_a, 'add_to_basket').status_code, 302)
        self.list_item_a.refresh_from_db()
        self.assertTrue(self.list_item_a.in_basket)
        self.assertEqual(self.list_item_a.put_in_basket_by, self.member_a)
        self.assertEqual(ListItem.objects.count(), before)
        self.assertEqual(self.action(self.list_item_a, 'remove_from_basket').status_code, 302)
        self.list_item_a.refresh_from_db()
        self.assertFalse(self.list_item_a.in_basket)
        self.assertIsNone(self.list_item_a.put_in_basket_by)
        self.assertIsNone(self.list_item_a.time_put_in_basket)
        self.assertEqual(ListItem.objects.count(), before)

    def test_return_and_purchase_cannot_affect_another_members_basket(self):
        other = self.unit(in_basket=True, put_in_basket_by=self.admin_a)
        self.action(self.list_item_a, 'add_to_basket')
        before = ListItem.objects.filter(pk=other.pk).values().get()
        self.assertEqual(self.action(other, 'remove_from_basket').status_code, 403)
        self.client.post(self.url_a('shopping_list'), {'action': 'buy_basket'})
        self.assertEqual(ListItem.objects.filter(pk=other.pk).values().get(), before)
        self.list_item_a.refresh_from_db()
        self.assertEqual(self.list_item_a.buyer, self.member_a)

    def test_four_actions_reject_other_families_and_terminal_or_closed_units(self):
        actions = ('add_unit', 'remove_unit', 'cancel_final_unit', 'add_to_basket', 'remove_from_basket')
        before = list(ListItem.objects.order_by('pk').values())
        for action in actions:
            self.assertEqual(self.action(self.list_item_b, action).status_code, 404)
        self.assertEqual(list(ListItem.objects.order_by('pk').values()), before)
        self.action(self.list_item_a, 'cancel')
        before = list(ListItem.objects.order_by('pk').values())
        for action in actions:
            self.assertIn(self.action(self.list_item_a, action).status_code, (400, 403, 409))
        self.assertEqual(list(ListItem.objects.order_by('pk').values()), before)
        self.action(self.list_item_a, 'uncancel')
        self.action(self.list_item_a, 'add_to_basket')
        self.client.post(self.url_a('shopping_list'), {'action': 'buy_basket'})
        before = list(ListItem.objects.order_by('pk').values())
        for action in actions:
            self.assertEqual(self.action(self.list_item_a, action).status_code, 409)
        self.assertEqual(list(ListItem.objects.order_by('pk').values()), before)
        self.client.post(self.url_a('close_shopping_list'), {
            'shopping_list_id': self.list_item_a.shopping_list_id,
        })
        before = list(ListItem.objects.order_by('pk').values())
        for action in actions:
            self.assertEqual(self.action(self.list_item_a, action).status_code, 400)
        self.assertEqual(list(ListItem.objects.order_by('pk').values()), before)


class UnitBasketTests(TwoFamilyTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.member_a)

    def add_units(self, count):
        return [ListItem.objects.create(
            family=self.family_a, product=self.product_a,
            creator=self.admin_a, request_key=self.list_item_a.request_key,
            creator_notes='original request notes',
        ) for _ in range(count)]

    def action(self, item, action):
        return self.client.post(self.url_a('shopping_list'), {
            'item_id': item.pk, 'action': action,
        })

    def rows(self):
        return aggregate_unit_rows(ListItem.objects.filter(
            family=self.family_a, current=True,
        ), self.family_a)

    def test_purchase_button_tracks_own_basket_on_page_and_live_refresh(self):
        def assert_button(enabled):
            page = self.client.get(self.url_a('shopping_list'))
            button = page.content.decode().split('id="buy-my-basket-btn"', 1)[1].split('>', 1)[0]
            self.assertEqual('disabled' not in button, enabled)
            data = self.client.get(self.url_a('shopping_list'), {'rows_only': '1'}).json()
            self.assertEqual(data['has_basket_items'], enabled)

        assert_button(False)
        self.action(self.list_item_a, 'add_to_basket')
        assert_button(True)
        self.action(self.list_item_a, 'remove_from_basket')
        assert_button(False)
        self.action(self.list_item_a, 'add_to_basket')
        self.client.post(self.url_a('shopping_list'), {'action': 'buy_basket'})
        assert_button(False)

    def test_purchase_button_ignores_other_owners_and_list_filters(self):
        self.action(self.list_item_a, 'add_to_basket')
        self.client.force_login(self.admin_a)
        page = self.client.get(self.url_a('shopping_list'))
        self.assertFalse(page.context['has_basket_items'])
        self.client.force_login(self.member_a)
        page = self.client.get(self.url_a('shopping_list'), {
            'filter_applied': '1', 'filter_mode': 'exclude',
            'filter_categories': [self.category_a.pk],
        })
        self.assertEqual(page.context['shopping_list'], [])
        self.assertTrue(page.context['has_basket_items'])

    def test_one_unit_can_be_put_in_and_returned(self):
        self.assertEqual(self.action(self.list_item_a, 'add_to_basket').status_code, 302)
        self.list_item_a.refresh_from_db()
        self.assertEqual(self.list_item_a.put_in_basket_by, self.member_a)
        self.assertEqual(self.action(self.list_item_a, 'remove_from_basket').status_code, 302)
        self.list_item_a.refresh_from_db()
        self.assertFalse(self.list_item_a.in_basket)
        self.assertFalse(self.list_item_a.cancelled)
        self.assertIsNone(self.list_item_a.put_in_basket_by)

    def test_each_down_assigns_one_unit_and_remaining_units_group(self):
        self.add_units(3)
        self.action(self.list_item_a, 'add_to_basket')
        self.action(self.list_item_a, 'add_to_basket')
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual({(r.state, r.unit_count) for r in rows}, {
            ('in_basket', 2), ('ordinary', 2),
        })
        self.assertTrue(all(r.quantity_required == 1 for r in rows))

    def test_multiple_owners_and_remaining_units_are_separate_rows(self):
        units = self.add_units(3)
        self.action(self.list_item_a, 'add_to_basket')
        self.client.force_login(self.admin_a)
        self.action(units[-1], 'add_to_basket')
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual({r.put_in_basket_by_id for r in rows if r.in_basket}, {
            self.member_a.pk, self.admin_a.pk,
        })
        self.assertEqual(next(r.unit_count for r in rows if r.state == 'ordinary'), 2)

    def test_down_rejects_exhausted_product_and_return_rejects_other_owner(self):
        self.action(self.list_item_a, 'add_to_basket')
        self.assertEqual(self.action(self.list_item_a, 'add_to_basket').status_code, 409)
        self.client.force_login(self.admin_a)
        self.assertEqual(self.action(self.list_item_a, 'remove_from_basket').status_code, 403)
        self.assertEqual(ListItem.objects.filter(product=self.product_a).count(), 1)

    def test_purchase_only_own_units_and_preserves_per_unit_buyers(self):
        second, ordinary = self.add_units(2)
        self.action(self.list_item_a, 'add_to_basket')
        self.client.force_login(self.admin_a)
        self.action(second, 'add_to_basket')
        self.client.post(self.url_a('shopping_list'), {'action': 'buy_basket'})
        second.refresh_from_db()
        self.assertEqual(second.buyer, self.admin_a)
        self.assertEqual(second.quantity_bought, 1)
        self.list_item_a.refresh_from_db()
        self.assertTrue(self.list_item_a.in_basket)
        ordinary.refresh_from_db()
        self.assertIsNone(ordinary.date_bought)

    def test_cancel_restore_do_not_silently_change_other_units(self):
        second = self.add_units(1)[0]
        self.action(self.list_item_a, 'cancel')
        self.list_item_a.refresh_from_db()
        self.assertEqual(self.list_item_a.cancelled_by, self.member_a)
        self.assertEqual(self.action(self.list_item_a, 'add_to_basket').status_code, 400)
        self.client.force_login(self.admin_a)
        self.action(self.list_item_a, 'uncancel')
        self.list_item_a.refresh_from_db()
        self.assertEqual(self.list_item_a.restored_by, self.admin_a)
        second.refresh_from_db()
        self.assertFalse(second.cancelled)
        self.assertIsNone(second.restored_by)

    def test_duplicate_increase_adds_ordinary_unit_with_request_identity(self):
        self.action(self.list_item_a, 'add_to_basket')
        self.client.post(self.url_a('resolve_shopping_list_duplicate'), {
            'product_id': self.product_a.pk, 'choice': 'increase_quantity',
        })
        units = ListItem.objects.filter(product=self.product_a)
        self.assertEqual(units.count(), 2)
        self.assertEqual(units.filter(in_basket=True).count(), 1)
        self.assertEqual(set(units.values_list('request_key', flat=True)), {
            self.list_item_a.request_key,
        })

    def test_close_carries_selected_units_and_notes_but_never_bought(self):
        cancelled, bought = self.add_units(2)
        self.action(cancelled, 'cancel')
        self.action(bought, 'add_to_basket')
        self.client.post(self.url_a('shopping_list'), {'action': 'buy_basket'})
        old_list = self.list_item_a.shopping_list
        self.client.post(self.url_a('close_shopping_list'), {
            'shopping_list_id': old_list.pk,
            'carry_forward_unpurchased': 'on', 'carry_forward_cancelled': 'on',
        })
        new_units = ListItem.objects.filter(family=self.family_a, current=True)
        self.assertEqual(new_units.count(), 2)
        self.assertFalse(new_units.filter(cancelled=True).exists())
        self.assertFalse(new_units.filter(date_bought__isnull=False).exists())
        self.assertEqual(set(new_units.values_list('request_key', flat=True)), {
            self.list_item_a.request_key,
        })
        self.assertTrue(new_units.filter(creator_notes='original request notes').exists())
        self.assertEqual(self.action(self.list_item_a, 'add_to_basket').status_code, 400)

    def test_grouped_fragments_match_initial_page_and_viewer_permissions(self):
        second, third = self.add_units(2)
        self.action(self.list_item_a, 'add_to_basket')
        self.action(second, 'add_to_basket')
        response = self.client.get(self.url_a('shopping_list'))
        self.assertEqual(len(response.context['shopping_list']), 2)
        data = self.client.get(self.url_a('shopping_list'), {'rows_only': '1'}).json()
        self.assertIn(data['rows_html'], response.content.decode())
        self.assertEqual(data['rows_html'].count('<li '), 2)  # 2 aggregated rows, no column headings # noqa
        self.assertIn('(&times;2)', data['rows_html'])
        self.assertNotIn('completed-chkbx', data['rows_html'])
        self.assertIn('Add one item to the shopping list', data['rows_html'])
        self.client.force_login(self.admin_a)
        other = self.client.get(self.url_a('shopping_list'), {'rows_only': '1'}).json()
        self.assertIn('data-basket-action="remove_from_basket"', other['rows_html'])
        self.assertIn('disabled aria-disabled="true"', other['rows_html'])
        self.assertEqual(data['shopping_list_id'], third.shopping_list_id)

    def test_fragments_use_viewers_language_and_existing_filters(self):
        self.add_units(1)
        self.client.get(self.url_a('shopping_list'), {
            'filter_applied': '1', 'filter_mode': 'exclude',
            'filter_categories': [self.category_a.pk],
        })
        filtered = self.client.get(self.url_a('shopping_list'), {'rows_only': '1'}).json()
        self.assertNotIn('Product A', filtered['rows_html'])
        self.client.get(self.url_a('shopping_list'), {'filter_applied': '1'})
        for language, label in [('de', 'zur Einkaufsliste'),
                                ('es', 'a la lista de la compra')]:
            data = self.client.get(self.url_a('shopping_list'), {'rows_only': '1'},
                                   HTTP_ACCEPT_LANGUAGE=language).json()
            self.assertIn(label, data['rows_html'])
            self.assertNotIn('Add one unit', data['rows_html'])

    def test_bought_count_counts_units_and_buyer_groups_remain_attributed(self):
        self.add_units(1)
        self.action(self.list_item_a, 'add_to_basket')
        self.action(self.list_item_a, 'add_to_basket')
        self.client.post(self.url_a('shopping_list'), {'action': 'buy_basket'})
        data = self.client.get(self.url_a('shopping_list'), {'rows_only': '1'}).json()
        self.assertEqual(data['bought_count'], 2)
        self.assertEqual(data['bought_html'].count('<li '), 1)
        self.assertIn('Bought by member_a', data['bought_html'])

    def test_stale_purchase_form_cannot_purchase_replacement_list_units(self):
        old_id = self.list_item_a.shopping_list_id
        self.client.post(self.url_a('close_shopping_list'), {
            'shopping_list_id': old_id, 'carry_forward_unpurchased': 'on',
        })
        new_unit = ListItem.objects.get(family=self.family_a, current=True)
        self.action(new_unit, 'add_to_basket')
        response = self.client.post(self.url_a('shopping_list'), {
            'action': 'buy_basket', 'shopping_list_id': old_id,
        })
        self.assertEqual(response.status_code, 409)
        new_unit.refresh_from_db()
        self.assertIsNone(new_unit.date_bought)

    def test_hidden_units_still_enable_plus_for_visible_product(self):
        from .models import Shop
        second = self.add_units(1)[0]
        other_shop = Shop.objects.create(
            family=self.family_a, creator=self.admin_a,
            shop_name='Other shop', type_of_shop=1,
        )
        second.preferred_shop = other_shop
        second.save()
        self.action(self.list_item_a, 'add_to_basket')
        response = self.client.get(self.url_a('shopping_list'), {
            'filter_applied': '1', 'filter_shops': [self.shop_a.pk],
        })
        row = response.context['shopping_list'][0]
        self.assertEqual(row.units_not_in_baskets, 1)
        self.assertEqual(self.action(self.list_item_a, 'add_to_basket').status_code, 302)

    def test_detail_quantity_matches_ordinary_group_without_changing_units(self):
        from .models import Product
        self.add_units(1)
        # Independent requests for the same Product share a visible row.
        ListItem.objects.create(
            family=self.family_a, product=self.product_a, creator=self.admin_a,
        )
        for owner in (self.member_a, self.admin_a):
            ListItem.objects.create(
                family=self.family_a, product=self.product_a,
                creator=self.admin_a, in_basket=True,
                put_in_basket_by=owner, time_put_in_basket=timezone.now(),
            )
        ListItem.objects.create(
            family=self.family_a, product=self.product_a, creator=self.admin_a,
            cancelled=True, cancelled_by=self.member_a,
        )
        ListItem.objects.create(
            family=self.family_a, product=self.product_a, creator=self.admin_a,
            date_bought=timezone.now(), buyer=self.member_a, quantity_bought=1,
        )
        closed = ShoppingList.objects.create(
            family=self.family_a, status=ShoppingList.Status.CLOSED,
        )
        ListItem.objects.create(
            family=self.family_a, product=self.product_a, creator=self.admin_a,
            shopping_list=closed, current=False,
        )
        variant = Product.objects.create(
            family=self.family_a, product_name='Product A variant',
            category=self.category_a, default_shop=self.shop_a,
            creator=self.admin_a, based_on=self.product_a,
        )
        ListItem.objects.create(
            family=self.family_a, product=variant, creator=self.admin_a,
        )
        snapshot = list(ListItem.objects.order_by('pk').values())
        response = self.client.get(self.url_a('list_item', slug=self.list_item_a.slug))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['requested_quantity'], 3)
        self.assertContains(response, '<strong>Quantity required:</strong> 3 X')
        self.assertEqual(list(ListItem.objects.order_by('pk').values()), snapshot)

    def test_detail_quantity_respects_visible_group_shop_filter(self):
        from .models import Shop
        self.add_units(1)
        other_shop = Shop.objects.create(
            family=self.family_a, creator=self.admin_a,
            shop_name='Different shop', type_of_shop=1,
        )
        ListItem.objects.create(
            family=self.family_a, product=self.product_a, creator=self.admin_a,
            preferred_shop=other_shop,
        )
        page = self.client.get(self.url_a('shopping_list'), {
            'filter_applied': '1', 'filter_shops': [self.shop_a.pk],
        })
        self.assertEqual(page.context['shopping_list'][0].unit_count, 2)
        detail = self.client.get(self.url_a('list_item', slug=self.list_item_a.slug))
        self.assertEqual(detail.context['requested_quantity'], 2)


class UnitWebSocketTests(TwoFamilyFixtureMixin, TransactionTestCase):
    async def test_four_controls_broadcast_and_refresh_grouped_quantities(self):
        communicator = WebsocketCommunicator(ShoppingConsumer.as_asgi(), ws_path(self.family_a))
        communicator.scope['user'] = self.member_a
        communicator.scope['url_route'] = {'kwargs': {'family_slug': self.family_a.slug}}
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        await sync_to_async(self.client.force_login)(self.member_a)
        try:
            expected = [
                ('add_unit', ['list_item_added', 'product_list_status'], 1, False),
                ('add_to_basket', ['list_item_update'], 2, True),
                ('remove_from_basket', ['list_item_update'], 1, False),
                ('remove_unit', ['product_list_status'], 1, False),
            ]
            for action, types, groups, has_basket in expected:
                await sync_to_async(self.client.post)(self.url_a('shopping_list'), {
                    'item_id': self.list_item_a.pk, 'action': action,
                })
                events = [await communicator.receive_json_from() for _ in types]
                self.assertEqual([event['type'] for event in events], types)
                response = await sync_to_async(self.client.get)(self.url_a('shopping_list'), {'rows_only': '1'})
                self.assertEqual(response.json()['rows_html'].count('<li '), groups) # noqa
                self.assertEqual(response.json()['has_basket_items'], has_basket)
            remaining = await sync_to_async(ListItem.objects.get)(family=self.family_a)
            await sync_to_async(self.client.post)(self.url_a('shopping_list'), {
                'item_id': remaining.pk, 'action': 'cancel_final_unit',
            })
            event = await communicator.receive_json_from()
            self.assertEqual(event['type'], 'list_item_update')
            self.assertTrue(event['cancelled'])
            completion = await communicator.receive_json_from()
            self.assertEqual(completion['type'], 'shopping_list_complete')
            response = await sync_to_async(self.client.get)(self.url_a('shopping_list'), {'rows_only': '1'})
            self.assertIn('data-state="cancelled"', response.json()['rows_html'])
            self.assertTrue(response.json()['show_completion_suggestion'])
        finally:
            await communicator.disconnect()

    async def test_unit_events_refresh_persisted_grouped_quantities(self):
        second = await sync_to_async(ListItem.objects.create)(
            product=self.product_a, family=self.family_a, creator=self.admin_a,
            request_key=self.list_item_a.request_key,
        )
        communicator = WebsocketCommunicator(ShoppingConsumer.as_asgi(), ws_path(self.family_a))
        communicator.scope['user'] = self.member_a
        communicator.scope['url_route'] = {'kwargs': {'family_slug': self.family_a.slug}}
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        await sync_to_async(self.client.force_login)(self.member_a)
        try:
            for item, action, expected in [
                (self.list_item_a, 'add_to_basket', 2),
                (second, 'add_to_basket', 1),
                (self.list_item_a, 'remove_from_basket', 2),
                (self.list_item_a, 'cancel', 2),
                (self.list_item_a, 'uncancel', 2),
            ]:
                await sync_to_async(self.client.post)(self.url_a('shopping_list'), {
                    'item_id': item.pk, 'action': action,
                })
                event = await communicator.receive_json_from()
                self.assertEqual(event['type'], 'list_item_update')
                response = await sync_to_async(self.client.get)(self.url_a('shopping_list'), {
                    'rows_only': '1',
                })
                self.assertEqual(response.json()['rows_html'].count('<li '), expected) # noqa
            # Forged WebSocket mutations remain ignored.
            await communicator.send_json_to({'list_item_id': second.pk, 'action': 'remove_from_basket'})
            self.assertTrue(await communicator.receive_nothing(timeout=0.2))
            await sync_to_async(second.refresh_from_db)()
            self.assertTrue(second.in_basket)
        finally:
            await communicator.disconnect()


class UnitConcurrencyTests(TwoFamilyFixtureMixin, TransactionTestCase):
    @skipUnlessDBFeature('has_select_for_update')
    def test_remove_racing_basket_assignment_never_deletes_a_basketed_unit(self):
        barrier = Barrier(2)
        clients = [Client(), Client()]
        for client in clients:
            client.force_login(self.member_a)

        def operate(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return clients[index].post(self.url_a('shopping_list'), {
                    'item_id': self.list_item_a.pk,
                    'action': ('remove_unit', 'add_to_basket')[index],
                }).status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            removed, assigned = list(pool.map(operate, [0, 1]))
        self.assertIn((removed, assigned), [(409, 302), (403, 302)])
        unit = ListItem.objects.filter(pk=self.list_item_a.pk).first()
        if assigned == 302:
            self.assertIsNotNone(unit)
            self.assertTrue(unit.in_basket)
            self.assertEqual(unit.put_in_basket_by, self.member_a)
        else:
            self.assertIsNone(unit)

    @skipUnlessDBFeature('has_select_for_update')
    def test_two_users_cannot_claim_the_same_unit(self):
        barrier = Barrier(2)
        clients = [Client(), Client()]
        clients[0].force_login(self.member_a)
        clients[1].force_login(self.admin_a)

        def claim(client):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return client.post(self.url_a('shopping_list'), {
                    'item_id': self.list_item_a.pk, 'action': 'add_to_basket',
                }).status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, clients))
        self.assertCountEqual(results, [302, 409])
        self.list_item_a.refresh_from_db()
        self.assertTrue(self.list_item_a.in_basket)
        self.assertIn(self.list_item_a.put_in_basket_by_id, [self.member_a.pk, self.admin_a.pk])

    @skipUnlessDBFeature('has_select_for_update')
    def test_claim_racing_close_cannot_mutate_carried_forward_unit(self):
        barrier = Barrier(2)
        clients = [Client(), Client()]
        for client in clients:
            client.force_login(self.member_a)
        old_list = self.list_item_a.shopping_list

        def operate(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                if index == 0:
                    return clients[index].post(self.url_a('shopping_list'), {
                        'item_id': self.list_item_a.pk, 'action': 'add_to_basket',
                    }).status_code
                return clients[index].post(self.url_a('close_shopping_list'), {
                    'shopping_list_id': old_list.pk, 'carry_forward_unpurchased': 'on',
                }).status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            claim_status, close_status = list(pool.map(operate, [0, 1]))
        self.assertIn(claim_status, [302, 400])
        self.assertEqual(close_status, 302)
        old_list.refresh_from_db()
        self.assertEqual(old_list.status, ShoppingList.Status.CLOSED)
        new_unit = ListItem.objects.get(family=self.family_a, current=True)
        self.assertFalse(new_unit.in_basket)
        self.assertEqual(new_unit.request_key, self.list_item_a.request_key)


class UnitMigrationTests(TransactionTestCase):
    before = [('shopping_list', '0046_listitem_restored_by')]
    after = [('shopping_list', '0047_individual_units')]

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.before)
        self.apps = executor.loader.project_state(self.before).apps
        self.Item = self.apps.get_model('shopping_list', 'ListItem')

    def tearDown(self):
        MigrationExecutor(connection).migrate(self.after)
        # Restores the schema fully, including any migrations added after
        # 0047 (e.g. 0048's AutomaticAdultGroupMembership removal) - this
        # class only ever exercises 0046/0047 directly, but leaving a
        # later migration's reversal undone here would strand an orphaned
        # table that Django's per-test flush no longer knows about.
        call_command('migrate', 'shopping_list', verbosity=0)
        super().tearDown()

    def legacy_item(self, **overrides):
        Family = self.apps.get_model('shopping_list', 'Family')
        Product = self.apps.get_model('shopping_list', 'Product')
        ShoppingList = self.apps.get_model('shopping_list', 'ShoppingList')
        User = self.apps.get_model('auth', 'User')
        family, _ = Family.objects.get_or_create(
            slug='migration', defaults={'name': 'Migration'},
        )
        user, _ = User.objects.get_or_create(username='migration-owner')
        product, _ = Product.objects.get_or_create(
            family=family, slug='milk', defaults={'product_name': 'Milk'},
        )
        shopping_list, _ = ShoppingList.objects.get_or_create(family=family)
        values = dict(
            family=family, product=product, shopping_list=shopping_list,
            creator_id=user.pk, quantity_required=3, quantity_bought=0,
            creator_notes='request notes', shopper_notes='shopper notes',
            in_basket=True, put_in_basket_by_id=user.pk,
            time_put_in_basket=timezone.now(), slug='milk-original',
        )
        values.update(overrides)
        return self.Item.objects.create(**values)

    def snapshot(self):
        return list(self.Item.objects.order_by('pk').values())

    def assert_failed_migration_restored_before_state(self, snapshot):
        self.assertEqual(self.snapshot(), snapshot)
        columns = {
            column.name for column in connection.introspection.get_table_description(
                connection.cursor(), self.Item._meta.db_table,
            )
        }
        self.assertNotIn('request_key', columns)
        self.assertNotIn(self.after[0], MigrationRecorder(connection).applied_migrations())
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, self.Item._meta.db_table)
        self.assertNotIn('list_item_is_one_unit', constraints)
        self.assertNotIn('list_item_bought_at_most_one', constraints)

    def assert_units_preserve(self, original, units):
        self.assertEqual(len(units), original.quantity_required)
        self.assertEqual(len({unit.request_key for unit in units}), 1)
        self.assertIsNotNone(units[0].request_key)
        preserved = [
            field.attname for field in self.Item._meta.concrete_fields
            if field.name not in ('id', 'slug', 'quantity_required', 'quantity_bought')
        ]
        for unit in units:
            self.assertEqual(unit.quantity_required, 1)
            self.assertEqual(unit.quantity_bought, 1 if original.date_bought else 0)
            for field in preserved:
                self.assertEqual(getattr(unit, field), getattr(original, field), field)
        original_unit = next(unit for unit in units if unit.pk == original.pk)
        self.assertEqual(original_unit.slug, original.slug)
        self.assertEqual(len({unit.slug for unit in units}), len(units))

    def test_split_and_reverse_preserve_original_data_and_attribution(self):
        original = self.legacy_item()
        second = self.legacy_item(
            slug='second-request', quantity_required=2,
            in_basket=False, put_in_basket_by_id=None, time_put_in_basket=None,
            creator_notes='independent request',
        )
        snapshot = self.snapshot()
        executor = MigrationExecutor(connection)
        executor.migrate(self.after)
        Item = executor.loader.project_state(self.after).apps.get_model('shopping_list', 'ListItem')
        self.assertEqual(Item.objects.count(), 5)
        keys = []
        for source in (original, second):
            key = Item.objects.get(pk=source.pk).request_key
            keys.append(key)
            self.assert_units_preserve(source, list(Item.objects.filter(request_key=key)))
        self.assertNotEqual(*keys)
        MigrationExecutor(connection).migrate(self.before)
        self.assert_failed_migration_restored_before_state(snapshot)
        self.assertEqual(self.Item.objects.count(), 2)

    def test_bought_and_cancelled_history_is_preserved(self):
        original = self.legacy_item(
            date_bought=timezone.now(), quantity_bought=3,
            buyer_id=None, cancelled=True, date_cancelled=timezone.now(),
            in_basket=False, put_in_basket_by_id=None,
        )
        snapshot = self.snapshot()
        MigrationExecutor(connection).migrate(self.after)
        self.assert_units_preserve(original, list(ListItem.objects.all()))
        MigrationExecutor(connection).migrate(self.before)
        self.assert_failed_migration_restored_before_state(snapshot)

    def test_known_owner_buyer_canceller_and_restorer_survive(self):
        basket = self.legacy_item()
        User = self.apps.get_model('auth', 'User')
        buyer = User.objects.create(username='recorded-buyer')
        canceller = User.objects.create(username='recorded-canceller')
        restorer = User.objects.create(username='recorded-restorer')
        Shop = self.apps.get_model('shopping_list', 'Shop')
        shop = Shop.objects.create(
            family_id=basket.family_id, shop_name='Recorded shop', type_of_shop=1,
        )
        bought = self.legacy_item(
            slug='bought-history', date_bought=timezone.now(), quantity_bought=3,
            buyer_id=buyer.pk, cancelled=True, cancelled_by_id=canceller.pk,
            date_cancelled=timezone.now(), restored_by_id=restorer.pk,
            in_basket=False, put_in_basket_by_id=basket.creator_id,
            preferred_shop_id=shop.pk, shop_bought_id=shop.pk, current=False,
        )
        cancelled = self.legacy_item(
            slug='cancelled-request', cancelled=True, cancelled_by_id=canceller.pk,
            date_cancelled=timezone.now(), in_basket=False,
            put_in_basket_by_id=None, time_put_in_basket=None,
        )
        restored = self.legacy_item(
            slug='restored-request', restored_by_id=restorer.pk,
            in_basket=False, put_in_basket_by_id=None, time_put_in_basket=None,
        )
        snapshot = self.snapshot()
        MigrationExecutor(connection).migrate(self.after)
        for original in (basket, bought, cancelled, restored):
            key = ListItem.objects.get(pk=original.pk).request_key
            self.assert_units_preserve(original, list(ListItem.objects.filter(request_key=key)))
        MigrationExecutor(connection).migrate(self.before)
        self.assert_failed_migration_restored_before_state(snapshot)

    def test_reverse_refuses_to_merge_different_owners(self):
        original = self.legacy_item()
        MigrationExecutor(connection).migrate(self.after)
        other = self.apps.get_model('auth', 'User').objects.create(username='other-owner')
        clone = ListItem.objects.exclude(pk=original.pk).first()
        ListItem.objects.filter(pk=clone.pk).update(put_in_basket_by_id=other.pk)
        snapshot = list(ListItem.objects.order_by('pk').values())
        with self.assertRaisesRegex(RuntimeError, 'different states'):
            MigrationExecutor(connection).migrate(self.before)
        self.assertEqual(list(ListItem.objects.order_by('pk').values()), snapshot)
        self.assertIn(self.after[0], MigrationRecorder(connection).applied_migrations())

    def assert_preflight_rejects_without_conversion(self, overrides, error):
        self.legacy_item()
        invalid = self.legacy_item(slug='later-invalid-record', **overrides)
        snapshot = self.snapshot()
        writes = []

        def observe(execute, sql, params, many, context):
            statement = sql.lstrip().upper()
            if (statement.startswith(('INSERT INTO', 'UPDATE', 'DELETE FROM'))
                    and '"SHOPPING_LIST_LISTITEM"' in statement):
                writes.append(sql)
            return execute(sql, params, many, context)

        try:
            with connection.execute_wrapper(observe):
                with self.assertRaisesRegex(RuntimeError, f'ListItem {invalid.pk}.*{error}'):
                    MigrationExecutor(connection).migrate(self.after)
            self.assertEqual(writes, [], 'Preflight must finish before any unit data changes')
            self.assert_failed_migration_restored_before_state(snapshot)
        finally:
            # Repair only the deliberately invalid test fixture, after
            # asserting that the failed migration left it unchanged.
            self.Item.objects.filter(pk=invalid.pk).update(
                quantity_bought=0, cancelled=False, in_basket=True,
                put_in_basket_by_id=invalid.creator_id,
            )

    def test_ambiguous_quantity_fails_before_data_conversion(self):
        self.assert_preflight_rejects_without_conversion(
            {'quantity_bought': 1}, 'ambiguous quantities',
        )

    def test_cancelled_basket_is_rejected_before_any_conversion(self):
        self.assert_preflight_rejects_without_conversion(
            {'cancelled': True},
            'both cancelled and in a basket.*Reconcile.*migration 0047',
        )

    def test_owner_without_basket_is_rejected_before_any_conversion(self):
        self.assert_preflight_rejects_without_conversion(
            {'in_basket': False},
            'basket owner while in_basket=False.*Reconcile.*migration 0047',
        )

    @skipUnlessDBFeature('can_rollback_ddl')
    def test_late_failure_automatically_rolls_back_entire_migration(self):
        first = self.legacy_item()
        self.legacy_item(slug='second-valid-request', quantity_required=2)
        snapshot = self.snapshot()
        original_create = QuerySet.create
        converted = []

        def fail_after_conversion(queryset, **kwargs):
            result = original_create(queryset, **kwargs)
            if (queryset.model._meta.label_lower == 'shopping_list.listitem'
                    and kwargs.get('request_key') is not None):
                converted.append(result.pk)
                if len(converted) == 3:
                    # First request is fully split and the second has
                    # begun. Prove changes exist inside the transaction.
                    saved = queryset.get(pk=first.pk)
                    self.assertEqual(saved.quantity_required, 1)
                    self.assertIsNotNone(saved.request_key)
                    self.assertEqual(queryset.count(), 5)
                    raise RuntimeError('Injected failure after unit insertion')
            return result

        with patch.object(QuerySet, 'create', new=fail_after_conversion):
            with self.assertRaisesRegex(RuntimeError, 'Injected failure'):
                MigrationExecutor(connection).migrate(self.after)
        self.assertEqual(len(converted), 3)
        # No explicit atomic block or forced rollback: the executor must
        # restore rows, cloned records, schema and migration bookkeeping.
        self.assert_failed_migration_restored_before_state(snapshot)
