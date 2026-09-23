
from .models import (
    Category,
    Family,
    FamilyMembership,
    ListItem,
    Product,
    Shop,
)
from .forms import CategoryForm, ProductForm, ShopForm
from .templatetags.custom_filters import member_display_name
from utilities.utils import is_adult
import logging
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.contrib import messages
from django.contrib.auth.mixins import UserPassesTestMixin
from django.core.exceptions import PermissionDenied

logger = logging.getLogger(__name__)


def get_user_memberships(user):
    """All of a user's memberships, never assuming there is only one."""
    return FamilyMembership.objects.select_related("family").filter(user=user) # noqa


class FamilyMembershipRequiredMixin:
    """
    Resolves the family explicitly from the `family_public_id` URL kwarg
    and requires an authenticated membership in it. Attaches both
    `request.family` and `request.family_membership`. Never trusts the
    UUID merely because the client supplied it, and never reveals whether
    a family exists to a non-member (404 either way).
    """

    def dispatch(self, request, *args, **kwargs):
        family_public_id = kwargs.get("family_public_id")
        family = get_object_or_404(Family, public_id=family_public_id)
        membership = FamilyMembership.objects.select_related("family").filter( # noqa
            family=family, user=request.user,
        ).first()
        if membership is None:
            raise Http404("No such family for this user.")

        request.family = family
        request.family_membership = membership
        return super().dispatch(request, *args, **kwargs)


def broadcast_list_item_update(item, user):
    # Only the saved DB state and the authenticated user are ever broadcast.
    # A channel-layer failure here must never affect the HTTP response for
    # an already-successful save, so it is logged rather than raised.
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return

    try:
        async_to_sync(channel_layer.group_send)(
            f"shopping_list_updates_{item.family_id}",
            {
                'type': 'list_item_update',
                'list_item_id': item.id,
                'in_basket': item.in_basket,
                'cancelled': item.cancelled,
                'bought': item.date_bought is not None,
                'product_name': item.product.product_name,
                'quantity_bought': item.quantity_bought,
                'buyer_display_name': (
                    member_display_name(item.buyer, item.family)
                    if item.buyer_id else None
                ),
                'user': user.get_username(),
            }
        )
    except Exception:
        logger.exception(
            "Failed to broadcast list item update for item %s", item.id,
        )


def broadcast_basket_bought(family, buyer, item_count):
    # One aggregate event per Buy My Basket operation, so other family
    # members get a single grouped notification instead of one per item.
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return

    try:
        async_to_sync(channel_layer.group_send)(
            f"shopping_list_updates_{family.id}",
            {
                'type': 'basket_bought',
                'buyer_username': buyer.get_username(),
                'buyer_display_name': member_display_name(buyer, family),
                'item_count': item_count,
            }
        )
    except Exception:
        logger.exception(
            "Failed to broadcast basket_bought for family %s", family.id,
        )


class AdultRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return is_adult(self.request.user)


# The two dimensions a shopping-list item can be grouped by, and the four
# supported combinations (primary, optional secondary). No new persistent
# data is needed: "category" reads product.category (defensively treating
# a missing category as "Uncategorised", even though the field is
# currently required) and "shop" reads the item's own preferred_shop
# (nullable; can genuinely be unset if that shop was later deleted).
GROUP_BY_DIMENSIONS = {
    'category': ('category', None),
    'category_shop': ('category', 'shop'),
    'shop': ('shop', None),
    'shop_category': ('shop', 'category'),
}

# 'none' is the default: the list is flat, ungrouped, unless the user
# explicitly picks one of the GROUP_BY_DIMENSIONS options below.
GROUP_BY_CHOICES = ['none'] + list(GROUP_BY_DIMENSIONS)

# Shopping-list and product-list display settings (grouping and
# filtering) are remembered for the user's session only; nothing is
# persisted to the database. The two pages have independent settings and
# different defaults (products default to grouping by category, with
# filtering off; the shopping list defaults to no grouping).
SESSION_KEY_GROUP_BY = 'shopping_list_group_by'
SESSION_KEY_FILTER = 'shopping_list_filter'
SESSION_KEY_PRODUCT_GROUP_BY = 'product_list_group_by'
SESSION_KEY_PRODUCT_FILTER = 'product_list_filter'

# 'include' (default): show only items matching a selected category/shop.
# 'exclude': hide items matching a selected category/shop.
FILTER_MODES = ('include', 'exclude')


def _clean_id_list(raw_ids, valid_ids):
    cleaned = set()
    for raw_id in raw_ids:
        if str(raw_id).isdigit() and int(raw_id) in valid_ids:
            cleaned.add(int(raw_id))
    return sorted(cleaned)


def _normalize_filter_state(
    raw_category_ids, raw_shop_ids, raw_mode, valid_category_ids, valid_shop_ids, # noqa
):
    # Stale/tampered category or shop ids (e.g. from another family, or a
    # category/shop that's since been deactivated) are silently dropped
    # rather than rejecting the whole filter.
    return {
        'categories': _clean_id_list(raw_category_ids, valid_category_ids),
        'shops': _clean_id_list(raw_shop_ids, valid_shop_ids),
        'mode': raw_mode if raw_mode in FILTER_MODES else 'include',
    }


def _apply_filter_state_generic(
    queryset, filter_state, category_lookup, shop_lookup,
):
    categories = filter_state['categories']
    shops = filter_state['shops']
    if not categories and not shops:
        return queryset

    # Matches if the item's category is one of the selected categories OR
    # its shop is one of the selected shops (i.e. "any selected category
    # or shop" as a single set of matching items).
    any_selected = Q()
    if categories:
        any_selected |= Q(**{f"{category_lookup}__in": categories})
    if shops:
        any_selected |= Q(**{f"{shop_lookup}__in": shops})

    if filter_state['mode'] == 'exclude':
        # Hide anything matching any selected category or shop.
        return queryset.exclude(any_selected)

    if categories and shops:
        # Both facets chosen: narrow to items matching both (an item's
        # category is one of the selected ones AND its shop is one of the
        # selected ones), not merely either.
        return queryset.filter(
            Q(**{f"{category_lookup}__in": categories})
            & Q(**{f"{shop_lookup}__in": shops})
        )
    return queryset.filter(any_selected)


def _apply_filter_state(shopping_list, filter_state):
    return _apply_filter_state_generic(
        shopping_list, filter_state, 'product__category_id',
        'preferred_shop_id',
    )


def _apply_product_filter_state(products, filter_state):
    return _apply_filter_state_generic(
        products, filter_state, 'category_id', 'default_shop_id',
    )


def _label_and_sort_key_for_name(name, missing_label):
    label = name or missing_label
    # The "missing" bucket always sorts first; named groups are
    # alphabetical thereafter.
    sort_key = (0, "") if name is None else (1, name.lower())
    return label, sort_key


def _group_label_and_sort_key(item, dimension):
    if dimension == 'shop':
        shop = item.preferred_shop
        name = shop.shop_name if shop else None
        return _label_and_sort_key_for_name(name, "No shop set")
    category = item.product.category
    name = category.category_name if category else None
    return _label_and_sort_key_for_name(name, "Uncategorised")


def _product_group_label_and_sort_key(product, dimension):
    if dimension == 'shop':
        shop = product.default_shop
        name = shop.shop_name if shop else None
        return _label_and_sort_key_for_name(name, "No shop set")
    category = product.category
    name = category.category_name if category else None
    return _label_and_sort_key_for_name(name, "Uncategorised")


def _bucket_items(items, dimension, label_and_sort_key_func):
    buckets = {}
    sort_keys = {}
    for item in items:
        label, sort_key = label_and_sort_key_func(item, dimension)
        buckets.setdefault(label, []).append(item)
        sort_keys[label] = sort_key
    return [
        (label, buckets[label])
        for label in sorted(buckets, key=lambda label: sort_keys[label])
    ]


def _group_items(items, group_by, label_and_sort_key_func):
    # Builds at most a two-level structure: a list of
    # {'label', 'items', 'subgroups'} dicts, where 'subgroups' is a list of
    # (label, items) tuples when a secondary dimension is requested.
    primary_dim, secondary_dim = GROUP_BY_DIMENSIONS[group_by]
    groups = []
    for label, group_items in _bucket_items(
        items, primary_dim, label_and_sort_key_func,
    ):
        subgroups = (
            _bucket_items(group_items, secondary_dim, label_and_sort_key_func)
            if secondary_dim else None
        )
        groups.append({
            'label': label,
            'items': group_items,
            'subgroups': subgroups,
        })
    return groups


def group_shopping_list_items(items, group_by):
    return _group_items(items, group_by, _group_label_and_sort_key)


def group_products(products, group_by):
    return _group_items(products, group_by, _product_group_label_and_sort_key)


@method_decorator(login_required, name='dispatch')
class ShoppingListView(FamilyMembershipRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        requested_group_by = request.GET.get('group_by')
        if requested_group_by is None:
            # No explicit choice on this request: fall back to whatever
            # was last chosen this session, defending against a
            # corrupted/tampered session value.
            group_by = request.session.get(SESSION_KEY_GROUP_BY, 'none')
            if group_by not in GROUP_BY_CHOICES:
                group_by = 'none'
        else:
            group_by = (
                requested_group_by
                if requested_group_by in GROUP_BY_CHOICES else 'none'
            )
            request.session[SESSION_KEY_GROUP_BY] = group_by

        categories = list(Category.objects.filter(
            family=request.family, current=True,
        ).order_by('category_name'))
        shops = list(Shop.objects.filter(
            family=request.family,
            current=True,
            ).order_by('type_of_shop'))
        category_ids = {category.id for category in categories}
        shop_ids = {shop.id for shop in shops}

        # 'filter_applied' distinguishes an explicit (possibly empty, e.g.
        # "Remove filter") submission of the filter form from a request
        # that simply didn't mention filtering at all, so the latter can
        # fall back to the session.
        if request.GET.get('filter_applied') is None:
            stored_filter = request.session.get(SESSION_KEY_FILTER, {})
            filter_state = _normalize_filter_state(
                stored_filter.get('categories', []),
                stored_filter.get('shops', []),
                stored_filter.get('mode', 'include'),
                category_ids, shop_ids,
            )
        else:
            filter_state = _normalize_filter_state(
                request.GET.getlist('filter_categories'),
                request.GET.getlist('filter_shops'),
                request.GET.get('filter_mode', 'include'),
                category_ids, shop_ids,
            )
            request.session[SESSION_KEY_FILTER] = filter_state

        # Precompute each checkbox's checked state once, so the template
        # doesn't need fragile "is this id in that list" template syntax.
        for category in categories:
            category.is_filter_selected = (
                category.id in filter_state['categories']
            )
        for shop in shops:
            shop.is_filter_selected = shop.id in filter_state['shops']

        shopping_list = ListItem.objects.filter(
            family=request.family,
            current=True,
            date_bought__isnull=True,
        ).select_related(
            'product__category', 'preferred_shop',
        ).order_by('product__product_name')
        shopping_list = _apply_filter_state(shopping_list, filter_state)

        grouped_shopping_list = (
            group_shopping_list_items(list(shopping_list), group_by)
            if group_by != 'none' else None
        )

        # Bought items stay on the current list (current=True) but are
        # shown separately, in a collapsed section, from the unpurchased
        # items above.
        bought_items = ListItem.objects.filter(
            family=request.family,
            cancelled=False,
            current=True,
            date_bought__isnull=False,
        ).order_by('-date_bought')

        return render(request, 'shopping_list/shopping_list.html', {
            'shopping_list': shopping_list,
            'grouped_shopping_list': grouped_shopping_list,
            'group_by': group_by,
            'filter_state': filter_state,
            'categories': categories,
            'bought_items': bought_items,
            'shops': shops,
            })

    def _buy_basket(self, request):
        # Marks every item currently in the requesting user's own basket
        # as bought and empties their basket. Bought items stay attached
        # to the current shopping list (current=True) so the frontend can
        # later show them in a separate "Bought" section.
        now = timezone.now()
        basket_items = list(
            ListItem.objects.filter(
                family=request.family,
                in_basket=True,
                put_in_basket_by=request.user,
                current=True,
                cancelled=False,
            )
        )

        with transaction.atomic():
            for item in basket_items:
                item.buyer = request.user
                item.date_bought = now
                item.quantity_bought = item.quantity_required
                item.shop_bought = item.preferred_shop
                item.in_basket = False
                item.put_in_basket_by = None
                item.time_put_in_basket = None
                item.save()

        for item in basket_items:
            transaction.on_commit(
                lambda item=item: broadcast_list_item_update(
                    item, request.user,
                ),
            )

        if basket_items:
            transaction.on_commit(
                lambda: broadcast_basket_bought(
                    request.family, request.user, len(basket_items),
                ),
            )

    def post(self, request, *args, **kwargs):
        try:
            action = request.POST.get('action')

            if action == 'buy_basket':
                self._buy_basket(request)
                return redirect(
                    'shopping_list', family_public_id=request.family.public_id, # noqa
                )

            item_id = request.POST.get('item_id')
            if not item_id or not action:
                return HttpResponseBadRequest(
                    "Invalid request: Missing item_id or action"
                )

            item = get_object_or_404(
                ListItem, id=item_id, family=request.family,
            )

            if action == 'cancel':
                if item.in_basket:
                    return HttpResponseBadRequest(
                        "Cannot cancel an item that is currently in a "
                        "basket. Remove it from the basket first."
                    )
                item.cancelled = True
            elif action == 'uncancel':
                item.cancelled = False
            elif action == 'add_to_basket':
                if item.cancelled:
                    return HttpResponseBadRequest(
                        "Cannot put a cancelled item in a basket. "
                        "Un-cancel it first."
                    )
                item.in_basket = True
                item.put_in_basket_by = request.user
                item.time_put_in_basket = timezone.now()
            elif action == 'remove_from_basket':
                if item.put_in_basket_by_id != request.user.id:
                    raise PermissionDenied(
                        "Only the person who put this item in their "
                        "basket can remove it."
                    )
                item.in_basket = False
                item.put_in_basket_by = None
                item.time_put_in_basket = None
            else:
                return HttpResponseBadRequest(
                    "Invalid request: Unknown action"
                )

            item.save()
            transaction.on_commit(
                lambda: broadcast_list_item_update(item, request.user),
            )
            return redirect(
                'shopping_list', family_public_id=request.family.public_id,
            )

        except Http404:
            raise
        except PermissionDenied:
            raise
        except Exception as e:
            print(f"Error processing POST request: {e}")
            return HttpResponseBadRequest("Invalid request")


@method_decorator(login_required, name='dispatch')
class ListItemView(FamilyMembershipRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        slug = self.kwargs.get('slug')
        list_item = get_object_or_404(
            ListItem,
            slug=slug,
            family=request.family,
            current=True,
            in_basket=False
        )

        return render(
            request,
            'shopping_list/list_item.html',
            {'list_item': list_item}
        )


@method_decorator(login_required, name='dispatch')
class ProductListView(FamilyMembershipRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        requested_group_by = request.GET.get('group_by')
        if requested_group_by is None:
            group_by = request.session.get(
                SESSION_KEY_PRODUCT_GROUP_BY, 'category',
            )
            if group_by not in GROUP_BY_CHOICES:
                group_by = 'category'
        else:
            group_by = (
                requested_group_by
                if requested_group_by in GROUP_BY_CHOICES else 'category'
            )
            request.session[SESSION_KEY_PRODUCT_GROUP_BY] = group_by

        # Options for the "Add product" modal's dropdowns: unchanged,
        # every category/shop for the family regardless of active status.
        categories = Category.objects.filter(family=request.family)
        shops = Shop.objects.filter(family=request.family)

        # Options for the filter modal: only currently-active ones, each
        # annotated with whether it's presently selected.
        filter_categories = list(Category.objects.filter(
            family=request.family, current=True,
        ).order_by('category_name'))
        filter_shops = list(Shop.objects.filter(
            family=request.family, current=True,
        ).order_by('type_of_shop'))
        category_ids = {category.id for category in filter_categories}
        shop_ids = {shop.id for shop in filter_shops}

        if request.GET.get('filter_applied') is None:
            stored_filter = request.session.get(
                SESSION_KEY_PRODUCT_FILTER, {},
            )
            filter_state = _normalize_filter_state(
                stored_filter.get('categories', []),
                stored_filter.get('shops', []),
                stored_filter.get('mode', 'include'),
                category_ids, shop_ids,
            )
        else:
            filter_state = _normalize_filter_state(
                request.GET.getlist('filter_categories'),
                request.GET.getlist('filter_shops'),
                request.GET.get('filter_mode', 'include'),
                category_ids, shop_ids,
            )
            request.session[SESSION_KEY_PRODUCT_FILTER] = filter_state

        for category in filter_categories:
            category.is_filter_selected = (
                category.id in filter_state['categories']
            )
        for shop in filter_shops:
            shop.is_filter_selected = shop.id in filter_state['shops']

        product_list = Product.objects.filter(
            family=request.family, current=True,
        ).select_related(
            'category', 'default_shop',
        ).order_by('category__category_name', 'date_created')
        product_list = _apply_product_filter_state(product_list, filter_state) # noqa

        grouped_products = (
            group_products(list(product_list), group_by)
            if group_by != 'none' else None
        )

        return render(request, 'shopping_list/product_list.html', {
            'product_list': product_list,
            'grouped_products': grouped_products,
            'group_by': group_by,
            'filter_state': filter_state,
            'categories': categories,
            'shops': shops,
            'filter_categories': filter_categories,
            'filter_shops': filter_shops,
        })



@method_decorator(login_required, name='dispatch')
class ShopListView(FamilyMembershipRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        shopList = Shop.objects.filter(family=request.family, current=True)
        types_of_shop = Shop.TYPES_OF_SHOP
        return render(
            request,
            'shopping_list/shop_list.html',
            {'shop_list': shopList, 'types_of_shop': types_of_shop}
        )


@method_decorator(login_required, name='dispatch')
class CategoryListView(FamilyMembershipRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        categoryList = Category.objects.filter(
            family=request.family, current=True,
        )
        return render(
            request,
            'shopping_list/category_list.html',
            {'category_list': categoryList}
        )


@method_decorator(login_required, name='dispatch')
class ProductView(FamilyMembershipRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        slug = self.kwargs.get('slug')
        product = get_object_or_404(
            Product, slug=slug, family=request.family,
        )
        form = ProductForm(instance=product, family=request.family)
        return render(
            request,
            'shopping_list/product.html',
            {'form': form, 'product': product}
        )

    def post(self, request, *args, **kwargs):
        slug = self.kwargs.get('slug')
        product = get_object_or_404(
            Product, slug=slug, family=request.family,
        )
        if not is_adult(request.user):
            raise PermissionDenied
        form = ProductForm(
            request.POST, instance=product, family=request.family,
        )
        if form.is_valid():
            form.save()
            return redirect(
                'product_list', family_public_id=request.family.public_id,
            )
        else:
            messages.error(request, "A product with this name already exists.")
        return render(
            request,
            'shopping_list/product.html',
            {'form': form, 'product': product}
        )


@method_decorator(login_required, name='dispatch')
class ShopView(FamilyMembershipRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        slug = self.kwargs.get('slug')
        shop = get_object_or_404(Shop, slug=slug, family=request.family)
        form = ShopForm(instance=shop, family=request.family)
        return render(
            request,
            'shopping_list/shop.html',
            {'form': form, 'shop': shop}
        )

    def post(self, request, *args, **kwargs):
        slug = self.kwargs.get('slug')
        shop = get_object_or_404(Shop, slug=slug, family=request.family)
        if not is_adult(request.user):
            raise PermissionDenied
        form = ShopForm(request.POST, instance=shop, family=request.family)
        if form.is_valid():
            form.save()
            return redirect(
                'shop_list', family_public_id=request.family.public_id,
            )
        else:
            for field, errors in form.errors.items():
                print(f"{field}: {errors}")
        return render(
            request,
            'shopping_list/shop.html',
            {'form': form, 'shop': shop}
        )


@method_decorator(login_required, name='dispatch')
class CategoryView(FamilyMembershipRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        slug = self.kwargs.get('slug')
        category = get_object_or_404(
            Category, slug=slug, family=request.family,
        )
        form = CategoryForm(instance=category, family=request.family)
        return render(
            request,
            'shopping_list/category.html',
            {'form': form, 'category': category}
        )

    def post(self, request, *args, **kwargs):
        slug = self.kwargs.get('slug')
        category = get_object_or_404(
            Category, slug=slug, family=request.family,
        )
        form = CategoryForm(
            request.POST, instance=category, family=request.family,
        )
        if form.is_valid():
            form.save()
            return redirect(
                'category_list', family_public_id=request.family.public_id,
            )
        else:
            messages.error(request, "A category with this name already exists.") # noqa
        return render(
            request,
            'shopping_list/category.html',
            {'form': form, 'category': category}
        )


@method_decorator(login_required, name='dispatch')
class ProductDeleteView(FamilyMembershipRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        slug = self.kwargs.get('slug')
        product = get_object_or_404(
            Product, slug=slug, family=request.family,
        )
        return render(
            request,
            'shopping_list/product_delete.html',
            {'product': product},
        )

    def post(self, request, *args, **kwargs):
        slug = self.kwargs.get('slug')
        product = get_object_or_404(
            Product, slug=slug, family=request.family,
        )
        if not is_adult(request.user):
            raise PermissionDenied
        product.current = False
        product.save()
        return redirect(
            'product_list', family_public_id=request.family.public_id,
        )


@method_decorator(login_required, name='dispatch')
class ShopDeleteView(FamilyMembershipRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        slug = self.kwargs.get('slug')
        shop = get_object_or_404(Shop, slug=slug, family=request.family)
        affected_products = Product.objects.filter(
            family=request.family, default_shop=shop, current=True,
        ).order_by('product_name')
        other_shops = Shop.objects.filter(
            family=request.family, current=True,
        ).exclude(pk=shop.pk).order_by('type_of_shop')
        return render(
            request,
            'shopping_list/shop_delete.html',
            {
                'shop': shop,
                'affected_products': affected_products,
                'other_shops': other_shops,
            },
        )

    def post(self, request, *args, **kwargs):
        slug = self.kwargs.get('slug')
        shop = get_object_or_404(Shop, slug=slug, family=request.family)
        if not is_adult(request.user):
            raise PermissionDenied

        affected_products = Product.objects.filter(
            family=request.family, default_shop=shop, current=True,
        )
        other_shop_ids = set(Shop.objects.filter(
            family=request.family, current=True,
        ).exclude(pk=shop.pk).values_list('id', flat=True))

        for product in affected_products:
            raw_value = request.POST.get(f'reassign_shop_{product.id}', '')
            new_shop_id = int(raw_value) if raw_value.isdigit() else None
            if new_shop_id not in other_shop_ids:
                new_shop_id = None
            product.default_shop_id = new_shop_id
            product.save()

        shop.current = False
        shop.save()
        return redirect(
            'shop_list', family_public_id=request.family.public_id,
        )


@method_decorator(login_required, name='dispatch')
class CategoryDeleteView(FamilyMembershipRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        slug = self.kwargs.get('slug')
        category = get_object_or_404(
            Category, slug=slug, family=request.family,
        )
        affected_products = Product.objects.filter(
            family=request.family, category=category, current=True,
        ).order_by('product_name')
        other_categories = Category.objects.filter(
            family=request.family, current=True,
        ).exclude(pk=category.pk).order_by('category_name')
        return render(
            request,
            'shopping_list/category_delete.html',
            {
                'category': category,
                'affected_products': affected_products,
                'other_categories': other_categories,
            },
        )

    def post(self, request, *args, **kwargs):
        slug = self.kwargs.get('slug')
        category = get_object_or_404(
            Category, slug=slug, family=request.family,
        )
        affected_products = Product.objects.filter(
            family=request.family, category=category, current=True,
        )
        other_category_ids = set(Category.objects.filter(
            family=request.family, current=True,
        ).exclude(pk=category.pk).values_list('id', flat=True))

        for product in affected_products:
            raw_value = request.POST.get(
                f'reassign_category_{product.id}', '',
            )
            new_category_id = int(raw_value) if raw_value.isdigit() else None
            if new_category_id not in other_category_ids:
                new_category_id = None
            product.category_id = new_category_id
            product.save()

        category.current = False
        category.save()
        return redirect(
            'category_list', family_public_id=request.family.public_id,
        )


@method_decorator(login_required, name='dispatch')
class AddToShoppingListView(FamilyMembershipRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        selected_products = request.POST.getlist('selected-products')
        creator = request.user

        products = Product.objects.filter(
            family=request.family, id__in=selected_products,
        )

        for product in products:
            ListItem.objects.create(
                product=product,
                family=request.family,
                quantity_required=1,
                creator=creator
            )

        return redirect(
            'shopping_list', family_public_id=request.family.public_id,
        )


@method_decorator(login_required, name='dispatch')
class AddProductView(FamilyMembershipRequiredMixin, AdultRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        form = ProductForm(request.POST, family=request.family)
        if form.is_valid():
            product = form.save(commit=False)
            product.family = request.family
            product.creator = request.user
            product.save()
        else:
            messages.error(request, "A product with this name already exists.")
        return redirect(
            'product_list', family_public_id=request.family.public_id,
        )


@method_decorator(login_required, name='dispatch')
class AddShopView(FamilyMembershipRequiredMixin, AdultRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        form = ShopForm(request.POST, family=request.family)
        if form.is_valid():
            shop = form.save(commit=False)
            shop.family = request.family
            shop.creator = request.user
            shop.save()
        else:
            messages.error(request, "A shop with this name already exists.")
        return redirect(
            'shop_list', family_public_id=request.family.public_id,
        )


@method_decorator(login_required, name='dispatch')
class AddCategoryView(FamilyMembershipRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        form = CategoryForm(request.POST, family=request.family)
        if form.is_valid():
            category = form.save(commit=False)
            category.family = request.family
            category.creator = request.user
            category.save()
        else:
            messages.error(request, "A category with this name already exists.")
        return redirect(
            'category_list', family_public_id=request.family.public_id,
        )
