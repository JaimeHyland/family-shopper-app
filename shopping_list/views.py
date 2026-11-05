
from .models import (
    Category,
    Family,
    FamilyMembership,
    ListItem,
    Product,
    Shop,
    ShoppingList,
    lock_family_for_membership_changes,
)
from .forms import (
    CategoryForm, MembershipPermissionsForm, ProductForm, ShopForm,
)
from .templatetags.custom_filters import member_display_name
from utilities.utils import is_family_admin, has_family_permission
import logging
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth.decorators import login_required
from django.db import transaction, IntegrityError
from django.db.models import Count, Q, Sum
from django.http import Http404, HttpResponseBadRequest, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from allauth.account.models import EmailAddress

logger = logging.getLogger(__name__)


def get_user_memberships(user):
    """All of a user's memberships, never assuming there is only one."""
    return FamilyMembership.objects.select_related("family").filter(user=user) # noqa


class FamilyMembershipRequiredMixin:
    """
    Resolves the family explicitly from the `family_slug` URL kwarg and
    requires an authenticated membership in it. Attaches both
    `request.family` and `request.family_membership`. Never trusts the
    slug merely because the client supplied it, and never reveals whether
    a family exists to a non-member (404 either way).
    """

    def dispatch(self, request, *args, **kwargs):
        family_slug = kwargs.get("family_slug")
        family = get_object_or_404(Family, slug=family_slug)
        membership = FamilyMembership.objects.select_related("family").filter( # noqa
            family=family, user=request.user,
        ).first()
        if membership is None:
            raise Http404("No such family for this user.")

        request.family = family
        request.family_membership = membership
        return super().dispatch(request, *args, **kwargs)


MEMBER_COLOR_COUNT = 8


def family_member_info(family):
    """{user_id: (display_name, colour_index)}. The colour is the member's
    stable join-order rank within the family, mapped onto a fixed palette
    (see .member-color-N in styles.css), so it is identical on every client
    and every render without needing a stored field."""
    memberships = FamilyMembership.objects.filter( # noqa
        family=family,
    ).order_by('date_joined', 'pk')
    return {
        m.user_id: (m.display_name, index % MEMBER_COLOR_COUNT)
        for index, m in enumerate(memberships)
    }


def annotate_item_state(items, family):
    """Sets the single semantic display state and attribution names on each
    item. Precedence: bought > cancelled > in_basket > ordinary."""
    info = family_member_info(family)

    def name_of(user_id):
        return info[user_id][0] if user_id in info else None

    for item in items:
        if item.date_bought is not None:
            item.state = 'bought'
        elif item.cancelled:
            item.state = 'cancelled'
        elif item.in_basket:
            item.state = 'in_basket'
        else:
            item.state = 'ordinary'
        owner = info.get(item.put_in_basket_by_id)
        item.owner_name = owner[0] if owner else None
        item.owner_color = owner[1] if owner else None
        item.buyer_name = name_of(item.buyer_id)
        item.cancelled_by_name = name_of(item.cancelled_by_id)
        item.restored_by_name = name_of(item.restored_by_id)
    return items


def aggregate_unit_rows(items, family, remaining_by_product=None):
    """Presentation only: persisted units remain independent records.

    Names are collected from saved attribution, never the event's actor.
    Distinct Product IDs are never combined, including variants.
    """
    from copy import copy

    items = annotate_item_state(list(items), family)
    rows = {}
    remaining = {}
    for item in items:
        if item.state == 'ordinary' and item.put_in_basket_by_id is None:
            remaining[item.product_id] = remaining.get(item.product_id, 0) + 1
        key = (item.product_id, item.state,
               item.put_in_basket_by_id if item.state == 'in_basket'
               else item.buyer_id if item.state == 'bought' else None)
        if key not in rows:
            row = copy(item)
            row.unit_count = 0
            row.unit_ids = []
            row.attribution_names = {
                'buyer_name': [], 'cancelled_by_name': [], 'restored_by_name': [],
            }
            rows[key] = row
        row = rows[key]
        row.unit_count += 1
        row.unit_ids.append(item.pk)
        for field, names in row.attribution_names.items():
            name = getattr(item, field)
            if name and name not in names:
                names.append(name)
    for row in rows.values():
        for field, names in row.attribution_names.items():
            setattr(row, field, ', '.join(names) or None)
        counts = remaining if remaining_by_product is None else remaining_by_product
        row.units_not_in_baskets = counts.get(row.product_id, 0)
    return list(rows.values())


def render_item_html(item):
    annotate_item_state([item], item.family)
    template = (
        'shopping_list/_bought_item_row.html' if item.state == 'bought'
        else 'shopping_list/_list_item_row.html'
    )
    return render_to_string(template, {'item': item})


def broadcast_list_item_update(item, user):
    # Only the saved DB state and the authenticated user are ever broadcast.
    # A channel-layer failure here must never affect the HTTP response for
    # an already-successful save, so it is logged rather than raised.
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return

    try:
        row_html = render_item_html(item)
        async_to_sync(channel_layer.group_send)(
            f"shopping_list_updates_{item.family_id}",
            {
                'type': 'list_item_update',
                'list_item_id': item.id,
                'in_basket': item.in_basket,
                'cancelled': item.cancelled,
                'bought': item.date_bought is not None,
                'product_name': item.product.product_name,
                'quantity_required': item.quantity_required,
                'quantity_bought': item.quantity_bought,
                'buyer_display_name': (
                    member_display_name(item.buyer, item.family)
                    if item.buyer_id else None
                ),
                'state': item.state,
                'owner_id': item.put_in_basket_by_id,
                'owner_display_name': item.owner_name,
                'cancelled_by_display_name': item.cancelled_by_name,
                'restored_by_display_name': item.restored_by_name,
                'row_html': row_html,
                'user': user.get_username(),
            }
        )
    except Exception:
        logger.exception(
            "Failed to broadcast list item update for item %s", item.id,
        )


def broadcast_list_item_added(item, user):
    # Sent whenever a brand new ListItem is created: an ordinary add from
    # the product catalogue, or a product variant created via the
    # duplicate-product prompt. Lets connected clients add the new row to
    # the shopping list live rather than waiting for a page reload.
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return

    try:
        async_to_sync(channel_layer.group_send)(
            f"shopping_list_updates_{item.family_id}",
            {
                'type': 'list_item_added',
                'list_item_id': item.id,
                'list_item_slug': item.slug,
                'product_name': item.product.product_name,
                'quantity_required': item.quantity_required,
                'user': user.get_username(),
            }
        )
    except Exception:
        logger.exception(
            "Failed to broadcast list item added for item %s", item.id,
        )


def broadcast_product_list_status(family, product, user):
    # Lets the product catalogue page show a live "On list" indicator
    # without a page reload: sent whenever a product's aggregate active
    # quantity on the shopping list changes (added, quantity increased, a
    # variant created for it, or an item bought out of the active list).
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return

    on_list_quantity = _get_active_quantities_for_products(
        family, [product.id],
    ).get(product.id)

    try:
        async_to_sync(channel_layer.group_send)(
            f"shopping_list_updates_{family.id}",
            {
                'type': 'product_list_status',
                'product_id': product.id,
                'on_list_quantity': on_list_quantity,
                'user': user.get_username(),
            }
        )
    except Exception:
        logger.exception(
            "Failed to broadcast product list status for product %s",
            product.id,
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
                # An opaque id, not buyer.get_username(): the client only
                # needs this to check "was it me?" (see base.html), and a
                # no-email member's Django username is an internal
                # identifier that must never reach the browser.
                'buyer_user_id': buyer.pk,
                'buyer_display_name': member_display_name(buyer, family),
                'item_count': item_count,
            }
        )
    except Exception:
        logger.exception(
            "Failed to broadcast basket_bought for family %s", family.id,
        )


def broadcast_shopping_list_complete(shopping_list):
    # Lets every connected family member see the "list complete" prompt
    # live, not just the person whose action finished the list off.
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return

    try:
        async_to_sync(channel_layer.group_send)(
            f"shopping_list_updates_{shopping_list.family_id}",
            {
                'type': 'shopping_list_complete',
                'shopping_list_id': shopping_list.id,
            }
        )
    except Exception:
        logger.exception(
            "Failed to broadcast shopping list complete for list %s",
            shopping_list.id,
        )


def broadcast_shopping_list_replaced(family, closed_list_id, new_list):
    # Tells connected clients the list they were looking at is now
    # closed/historical, and which new open list has replaced it, so they
    # can refresh to the correct, current state rather than keep acting
    # on a list that's no longer open.
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return

    try:
        async_to_sync(channel_layer.group_send)(
            f"shopping_list_updates_{family.id}",
            {
                'type': 'shopping_list_replaced',
                'closed_list_id': closed_list_id,
                'new_list_id': new_list.id,
            }
        )
    except Exception:
        logger.exception(
            "Failed to broadcast shopping list replaced for family %s",
            family.id,
        )


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

# Products a user tried to add that were already on their family's active
# shopping list, awaiting a choice (increase quantity / create a variant).
# Stored in the session (not the database) as a list of {family_id,
# product_id} dicts, so nothing changes about the list until the user
# actually resolves (or lets lapse) each one.
SESSION_KEY_PENDING_DUPLICATES = 'pending_shopping_list_duplicates'


def _active_list_items(family):
    """The "active" rule shared by every active-item lookup: belongs to
    the family's current (open) list and hasn't been bought yet.
    Cancelled items are deliberately still included, since they remain
    visible on that same list (just struck through) rather than being
    removed from it - this is distinct from "outstanding", which also
    excludes cancelled items (see _shopping_list_item_counts)."""
    return ListItem.objects.filter(
        family=family, current=True, shopping_list__status=ShoppingList.Status.OPEN,
        date_bought__isnull=True,
    )


def _get_active_list_item_for_product(family, product, for_update=False):
    queryset = _active_list_items(family).filter(product=product)
    if for_update:
        queryset = queryset.select_for_update()
    return queryset.first()


def _get_active_quantities_for_products(family, product_ids):
    """Maps product_id -> aggregate active quantity, using exactly the
    same "active" rule as _get_active_list_item_for_product. Summed
    rather than read from a single row, so a product that somehow has
    more than one active row (the application-level duplicate check
    doesn't enforce this at the database level) still gets a correct,
    non-misleading total rather than an arbitrary single row's figure.
    Products with no active rows are simply absent from the result.
    """
    if not product_ids:
        return {}
    rows = _active_list_items(family).filter(
        product_id__in=product_ids,
    ).values('product_id').annotate(total=Sum('quantity_required'))
    return {row['product_id']: row['total'] for row in rows}


def _annotate_products_with_list_status(products, family):
    """Attaches on_list_quantity (an int, or None if not on the active
    list) to each product in the given iterable, in place, for display in
    the product catalogue. Products are matched by id (never by name)."""
    quantities = _get_active_quantities_for_products(
        family, [product.id for product in products],
    )
    for product in products:
        product.on_list_quantity = quantities.get(product.id)
    return products


def get_pending_duplicate_conflicts(request):
    """Pending duplicate-product choices for this user, scoped to the
    current family, re-validated against the current database state so a
    stale or already-resolved entry never reappears."""
    pending = request.session.get(SESSION_KEY_PENDING_DUPLICATES, [])
    still_pending = []
    conflicts = []
    changed = False

    for entry in pending:
        if entry.get('family_id') != request.family.id:
            # Belongs to a different family (the user switched); keep it
            # as-is for whenever they're back in that family's context.
            still_pending.append(entry)
            continue

        product = Product.objects.filter(
            id=entry.get('product_id'), family=request.family,
        ).first()
        existing_item = (
            _get_active_list_item_for_product(request.family, product)
            if product else None
        )
        if product is None or existing_item is None:
            # No longer a genuine duplicate (e.g. removed/cancelled, or
            # resolved from another tab): drop it silently.
            changed = True
            continue

        still_pending.append(entry)
        # The aggregate (not just existing_item's own quantity_required)
        # is shown, so the prompt can never understate what's already
        # requested if more than one active row somehow exists.
        on_list_quantity = _get_active_quantities_for_products(
            request.family, [product.id],
        ).get(product.id)
        conflicts.append({
            'product': product,
            'existing_item': existing_item,
            'on_list_quantity': on_list_quantity,
        })

    if changed:
        request.session[SESSION_KEY_PENDING_DUPLICATES] = still_pending

    return conflicts


def _add_pending_duplicate(request, product):
    pending = request.session.get(SESSION_KEY_PENDING_DUPLICATES, [])
    already_pending = any(
        entry.get('family_id') == request.family.id
        and entry.get('product_id') == product.id
        for entry in pending
    )
    if not already_pending:
        pending.append({'family_id': request.family.id, 'product_id': product.id}) # noqa
        request.session[SESSION_KEY_PENDING_DUPLICATES] = pending


def _clear_pending_duplicate(request, product_id):
    pending = request.session.get(SESSION_KEY_PENDING_DUPLICATES, [])
    remaining = [
        entry for entry in pending
        if not (
            entry.get('family_id') == request.family.id
            and entry.get('product_id') == product_id
        )
    ]
    if len(remaining) != len(pending):
        request.session[SESSION_KEY_PENDING_DUPLICATES] = remaining


# Dismissing the "list complete, start a new one?" suggestion is
# remembered per-session, keyed by which list it was shown for and how
# many items that list had at the time - so adding further items to the
# same (still open) list later makes the suggestion eligible to reappear,
# rather than being suppressed forever once dismissed once.
SESSION_KEY_DISMISSED_COMPLETION = 'dismissed_shopping_list_completion'


def _shopping_list_item_counts(family):
    """(total, outstanding) item counts for the family's open list, using
    the same bought/cancelled semantics as the rest of the app: an item
    only counts as outstanding if it is neither bought nor cancelled."""
    items = ListItem.objects.filter(family=family, current=True)
    total = items.count()
    outstanding = items.filter(
        date_bought__isnull=True, cancelled=False,
    ).count()
    return total, outstanding


def _is_list_complete(total, outstanding):
    """A list counts as "complete" once it has at least one item and none
    are still outstanding (not bought, not cancelled). The one
    authoritative definition of "complete", shared by every caller that
    needs it."""
    return total > 0 and outstanding == 0


def is_open_list_complete(family):
    total, outstanding = _shopping_list_item_counts(family)
    return _is_list_complete(total, outstanding)


def get_completion_suggestion_context(request, open_list):
    """Whether to show the "list complete" suggestion right now, and the
    item count to remember if the user dismisses it."""
    total, outstanding = _shopping_list_item_counts(request.family)
    if not _is_list_complete(total, outstanding):
        return False, total

    dismissed = request.session.get(SESSION_KEY_DISMISSED_COMPLETION, {})
    already_dismissed = (
        dismissed.get('shopping_list_id') == open_list.id
        and dismissed.get('item_count') == total
    )
    return (not already_dismissed), total


def _check_and_broadcast_list_completion(family):
    # Checked after any action that could have just finished off the open
    # list (buying or cancelling an item). Broadcasting again when it was
    # already complete is harmless: clients treat the event idempotently.
    if is_open_list_complete(family):
        open_list = ShoppingList.objects.filter(
            family=family, status=ShoppingList.Status.OPEN,
        ).first()
        if open_list is not None:
            broadcast_shopping_list_complete(open_list)


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

        open_list = ShoppingList.get_or_create_open(request.family, request.user)
        # Filters affect presentation, not which units can enter a basket.
        remaining_by_product = dict(_active_list_items(request.family).filter(
            shopping_list=open_list, cancelled=False, in_basket=False,
            put_in_basket_by__isnull=True,
        ).values('product_id').annotate(total=Count('pk')).values_list(
            'product_id', 'total',
        ))
        shopping_list = _active_list_items(request.family).filter(
            shopping_list=open_list,
        ).select_related(
            'product__category', 'preferred_shop', 'family',
        ).order_by('product__product_name', 'pk')
        shopping_list = aggregate_unit_rows(
            list(_apply_filter_state(shopping_list, filter_state)),
            request.family,
            remaining_by_product,
        )

        grouped_shopping_list = (
            group_shopping_list_items(shopping_list, group_by)
            if group_by != 'none' else None
        )

        # Bought items stay on the current list (current=True) but are
        # shown separately, in a collapsed section, from the unpurchased
        # items above.
        bought_items = aggregate_unit_rows(list(ListItem.objects.filter(
            family=request.family,
            shopping_list=open_list,
            current=True,
            date_bought__isnull=False,
        ).select_related('product', 'family').order_by('-date_bought')),
            request.family,
        )

        show_completion_suggestion, list_item_count = (
            get_completion_suggestion_context(request, open_list)
        )
        has_basket_items = ListItem.objects.filter(
            family=request.family, shopping_list=open_list, current=True,
            in_basket=True, put_in_basket_by=request.user,
            cancelled=False, date_bought__isnull=True,
        ).exists()

        context = {
            'shopping_list': shopping_list,
            'grouped_shopping_list': grouped_shopping_list,
            'group_by': group_by,
            'filter_state': filter_state,
            'categories': categories,
            'bought_items': bought_items,
            'bought_unit_count': sum(row.unit_count for row in bought_items),
            'shops': shops,
            'open_list': open_list,
            'show_completion_suggestion': show_completion_suggestion,
            'list_item_count': list_item_count,
            'has_basket_items': has_basket_items,
            }
        if request.GET.get('rows_only') == '1':
            return JsonResponse({
                'shopping_list_id': open_list.pk,
                'rows_html': render_to_string(
                    'shopping_list/_shopping_list_rows.html', context, request=request,
                ),
                'bought_html': render_to_string(
                    'shopping_list/_bought_rows.html', context, request=request,
                ),
                'bought_count': context['bought_unit_count'],
                'has_basket_items': has_basket_items,
                'show_completion_suggestion': show_completion_suggestion,
            })
        return render(request, 'shopping_list/shopping_list.html', context)

    def _buy_basket(self, request):
        # Marks every item currently in the requesting user's own basket
        # as bought and empties their basket. Bought items stay attached
        # to the current shopping list (current=True) so the frontend can
        # later show them in a separate "Bought" section.
        now = timezone.now()

        with transaction.atomic():
            Family.objects.select_for_update().get(pk=request.family.pk)
            expected_list = request.POST.get('shopping_list_id')
            if expected_list and not ShoppingList.objects.filter(
                pk=expected_list, family=request.family,
                status=ShoppingList.Status.OPEN,
            ).exists():
                return False
            basket_items = list(
                ListItem.objects.select_for_update(of=('self',)).filter(
                    family=request.family,
                    in_basket=True,
                    put_in_basket_by=request.user,
                    current=True,
                    shopping_list__status=ShoppingList.Status.OPEN,
                    cancelled=False,
                    date_bought__isnull=True,
                ).select_related('product', 'family')
            )
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
            # A bought item drops out of the active list, so the product
            # catalogue's "On list" indicator for it needs updating too.
            transaction.on_commit(
                lambda item=item: broadcast_product_list_status(
                    request.family, item.product, request.user,
                ),
            )

        if basket_items:
            transaction.on_commit(
                lambda: broadcast_basket_bought(
                    request.family, request.user, len(basket_items),
                ),
            )
            transaction.on_commit(
                lambda: _check_and_broadcast_list_completion(request.family),
            )

    def _apply_item_action(self, request, item_id, action):
        # Runs inside transaction.atomic(); the row lock serialises
        # concurrent actions on the same item, so ownership checks below
        # are made against the latest persisted state.
        item = get_object_or_404(
            ListItem.objects.select_for_update(of=('self',)).select_related(
                'product', 'family',
            ),
            id=item_id, family=request.family,
        )
        if not item.current or item.shopping_list.status != ShoppingList.Status.OPEN:
            return HttpResponseBadRequest(
                "This item belongs to a closed shopping list and can "
                "no longer be changed."
            )
        if item.date_bought is not None:
            return self._conflict(
                item, request, "This item has already been bought.",
            )

        # Basket actions (add_to_basket/remove_from_basket) are governed
        # purely by basket ownership, not by these six permissions, and
        # are deliberately absent from this mapping.
        required_permission = {
            'add_unit': 'can_add_list',
            'remove_unit': 'can_remove_list',
            'cancel_final_unit': 'can_remove_list',
            'cancel': 'can_remove_list',
            'uncancel': 'can_remove_list',
        }.get(action)
        if required_permission and not has_family_permission(
            request, required_permission,
        ):
            raise PermissionDenied

        if action == 'add_unit':
            if item.cancelled:
                return HttpResponseBadRequest("Restore the cancelled unit first.")
            new_item = _add_requested_unit(request, item)
            transaction.on_commit(
                lambda: broadcast_list_item_added(new_item, request.user),
            )
            transaction.on_commit(
                lambda: broadcast_product_list_status(
                    request.family, new_item.product, request.user,
                ),
            )
            return JsonResponse({'ok': True})

        if action in ('remove_unit', 'cancel_final_unit'):
            if item.in_basket or item.put_in_basket_by_id is not None:
                raise PermissionDenied("Units in baskets cannot be removed from the list.")
            if item.cancelled:
                return HttpResponseBadRequest("Restore the cancelled unit first.")
            remaining = ListItem.objects.filter(
                family=request.family, shopping_list=item.shopping_list,
                product_id=item.product_id, current=True,
                date_bought__isnull=True, cancelled=False, in_basket=False,
                put_in_basket_by__isnull=True,
            ).count()
            if remaining == 1:
                if action == 'remove_unit':
                    return JsonResponse({
                        'ok': False, 'confirmation_required': True,
                        'item_id': item.pk,
                    }, status=409)
                # Preserve the final unit using the normal cancellation
                # lifecycle and attribution below, rather than deleting it.
                action = 'cancel'
            elif action == 'cancel_final_unit':
                return self._conflict(item, request, "The unbasketed quantity has changed.")
            else:
                product = item.product
                item.delete()
                transaction.on_commit(
                    lambda: broadcast_product_list_status(
                        request.family, product, request.user,
                    ),
                )
                transaction.on_commit(
                    lambda: _check_and_broadcast_list_completion(request.family),
                )
                return JsonResponse({'ok': True})

        if action == 'add_to_basket':
            # The displayed row is an anchor, not a quantity counter.
            # Under the family lock, choose exactly one eligible unit.
            if item.cancelled:
                return HttpResponseBadRequest("Cannot put a cancelled unit in a basket.")
            eligible = ListItem.objects.select_for_update().filter(
                family=request.family, shopping_list=item.shopping_list,
                product_id=item.product_id, current=True,
                date_bought__isnull=True, cancelled=False, in_basket=False,
                put_in_basket_by__isnull=True,
            ).order_by('pk').first()
            if eligible is None:
                return self._conflict(item, request, "No units remain outside baskets.")
            item = eligible

        if action == 'cancel':
            if item.in_basket:
                return HttpResponseBadRequest(
                    "Cannot cancel an item that is currently in a "
                    "basket. Remove it from the basket first."
                )
            if not item.cancelled:
                item.cancelled = True
                item.cancelled_by = request.user
                item.date_cancelled = timezone.now()
                item.restored_by = None
        elif action == 'uncancel':
            if item.cancelled:
                item.cancelled = False
                item.cancelled_by = None
                item.date_cancelled = None
                item.restored_by = request.user
        elif action == 'add_to_basket':
            item.in_basket = True
            item.put_in_basket_by = request.user
            item.time_put_in_basket = timezone.now()
        elif action == 'remove_from_basket':
            if not item.in_basket or item.put_in_basket_by_id != request.user.id:
                raise PermissionDenied(
                    "Only the person who put this item in their "
                    "basket can remove it."
                )
            if item.cancelled:
                return HttpResponseBadRequest("Restore the cancelled unit first.")
            item.in_basket = False
            item.put_in_basket_by = None
            item.time_put_in_basket = None
        else:
            return HttpResponseBadRequest("Invalid request: Unknown action")

        item.save()
        transaction.on_commit(
            lambda: broadcast_list_item_update(item, request.user),
        )
        if action in ('cancel', 'uncancel'):
            transaction.on_commit(
                lambda: _check_and_broadcast_list_completion(
                    request.family,
                ),
            )
            # This path is only ever submitted via fetch(); the
            # response body is never used, so skip the redirect's
            # wasted extra GET (the live UI update comes over the
            # WebSocket broadcast above).
            return JsonResponse({"ok": True})
        return redirect('shopping_list', family_slug=request.family.slug) # noqa

    def _conflict(self, item, request, message):
        # The requester's page was stale. Nothing is changed; re-broadcast
        # the persisted state so their row corrects itself too.
        transaction.on_commit(
            lambda: broadcast_list_item_update(item, request.user),
        )
        return JsonResponse({"ok": False, "error": message}, status=409)

    def post(self, request, *args, **kwargs):
        try:
            action = request.POST.get('action')

            if action == 'buy_basket':
                if self._buy_basket(request) is False:
                    return JsonResponse({'ok': False}, status=409)
                return redirect(
                    'shopping_list', family_slug=request.family.slug, # noqa
                )

            item_id = request.POST.get('item_id')
            if not item_id or not action:
                return HttpResponseBadRequest(
                    "Invalid request: Missing item_id or action"
                )

            with transaction.atomic():
                # Same lock order as close/purchase/add: list replacement
                # cannot race a mutation into the historical list.
                Family.objects.select_for_update().get(pk=request.family.pk)
                return self._apply_item_action(request, item_id, action)

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

        # The URL identifies one unit, but the link comes from a grouped
        # row. Reuse that row's state/product grouping and saved filters.
        units = ListItem.objects.filter(
            family=request.family, shopping_list=list_item.shopping_list,
            product_id=list_item.product_id, current=True,
        )
        filter_state = request.session.get(SESSION_KEY_FILTER, {
            'categories': [], 'shops': [], 'mode': 'include',
        })
        rows = aggregate_unit_rows(
            _apply_filter_state(units, filter_state), request.family,
        )
        requested_quantity = next((
            row.unit_count for row in rows if list_item.pk in row.unit_ids
        ), 1)

        return render(
            request,
            'shopping_list/list_item.html',
            {'list_item': list_item, 'requested_quantity': requested_quantity}
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
        product_list = list(
            _apply_product_filter_state(product_list, filter_state),
        )
        # Realised into a concrete list (rather than left as a lazy
        # queryset) so both the grouped and flat rendering paths below
        # see the exact same annotated objects/values.
        _annotate_products_with_list_status(product_list, request.family)

        grouped_products = (
            group_products(product_list, group_by)
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
            'duplicate_conflicts': get_pending_duplicate_conflicts(request),
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
        if not is_family_admin(request):
            raise PermissionDenied
        form = ProductForm(
            request.POST, instance=product, family=request.family,
        )
        if form.is_valid():
            form.save()
            return redirect(
                'product_list', family_slug=request.family.slug,
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
        if not is_family_admin(request):
            raise PermissionDenied
        form = ShopForm(request.POST, instance=shop, family=request.family)
        if form.is_valid():
            form.save()
            return redirect(
                'shop_list', family_slug=request.family.slug,
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
                'category_list', family_slug=request.family.slug,
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
        if not has_family_permission(request, 'can_remove_catalogue'):
            raise PermissionDenied
        product.current = False
        product.save()
        return redirect(
            'product_list', family_slug=request.family.slug,
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
        if not has_family_permission(request, 'can_remove_shops'):
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
            'shop_list', family_slug=request.family.slug,
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
            'category_list', family_slug=request.family.slug,
        )


def _add_requested_unit(request, source):
    """Extend a request without copying a unit's ownership or terminal state."""
    return ListItem.objects.create(
        product=source.product, family=request.family,
        shopping_list=source.shopping_list, request_key=source.request_key,
        creator=source.creator, preferred_shop=source.preferred_shop,
        creator_notes=source.creator_notes, shopper_notes=source.shopper_notes,
    )


def _add_fresh_list_item(request, product):
    """Creates a brand-new, ordinary (quantity 1, not cancelled, not
    bought) ListItem for this product on the family's open list. Shared
    by every "add this product to the list" path: a plain catalogue add,
    the "no longer a duplicate" fallback when resolving a stale duplicate
    prompt, and adding a freshly-created product variant."""
    return ListItem.objects.create(
        product=product, family=request.family,
        shopping_list=ShoppingList.get_or_create_open(
            request.family, request.user,
        ),
        quantity_required=1, creator=request.user,
    )


@method_decorator(login_required, name='dispatch')
class AddToShoppingListView(FamilyMembershipRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        if not has_family_permission(request, 'can_add_list'):
            raise PermissionDenied
        selected_products = request.POST.getlist('selected-products')

        products = Product.objects.filter(
            family=request.family, id__in=selected_products,
        )

        for product in products:
            # Locking the product row serialises concurrent attempts to
            # add the same product at once, so two near-simultaneous
            # requests can't both see "no existing item" and both create
            # one: the second sees the first's new row and is offered the
            # duplicate choice instead.
            with transaction.atomic():
                Family.objects.select_for_update().get(pk=request.family.pk)
                product = Product.objects.select_for_update().get(
                    pk=product.pk,
                )
                existing_item = _get_active_list_item_for_product(
                    request.family, product,
                )
                if existing_item is not None:
                    _add_pending_duplicate(request, product)
                    continue

                new_item = _add_fresh_list_item(request, product)

            transaction.on_commit(
                lambda item=new_item: broadcast_list_item_added(
                    item, request.user,
                ),
            )
            transaction.on_commit(
                lambda product=product: broadcast_product_list_status(
                    request.family, product, request.user,
                ),
            )

        return redirect(
            'product_list' if get_pending_duplicate_conflicts(request)
            else 'shopping_list',
            family_slug=request.family.slug,
        )


@method_decorator(login_required, name='dispatch')
class ResolveShoppingListDuplicateView(FamilyMembershipRequiredMixin, View):
    """Handles the user's choice for one product already on their active
    shopping list: either increase the existing item's quantity, or
    create a new, separately-tracked product variant and add that."""

    def post(self, request, *args, **kwargs):
        raw_product_id = request.POST.get('product_id', '')
        choice = request.POST.get('choice')

        if not raw_product_id.isdigit() or choice not in (
            'increase_quantity', 'create_variant',
        ):
            return HttpResponseBadRequest("Invalid request")

        product_id = int(raw_product_id)

        try:
            with transaction.atomic():
                Family.objects.select_for_update().get(pk=request.family.pk)
                product = get_object_or_404(
                    Product, pk=product_id, family=request.family,
                )
                existing_item = _get_active_list_item_for_product(
                    request.family, product, for_update=True,
                )

                if choice == 'increase_quantity':
                    if not has_family_permission(request, 'can_add_list'):
                        raise PermissionDenied(
                            "You do not have permission to add to the "
                            "shopping list."
                        )
                    new_item, updated_item = self._increase_quantity(
                        request, product, existing_item,
                    )
                else:
                    if not has_family_permission(request, 'can_add_catalogue'): # noqa
                        raise PermissionDenied(
                            "Ask a family admin to create a new product "
                            "variant."
                        )
                    variant_name = request.POST.get(
                        'variant_name', '',
                    ).strip()
                    new_item, updated_item = self._create_variant(
                        request, product, variant_name,
                    )
        except ValidationError as error:
            messages.error(request, "; ".join(error.messages))
            return redirect(
                'product_list', family_slug=request.family.slug,
            )
        except IntegrityError:
            messages.error(
                request,
                "That product name was taken by someone else just now. "
                "Please try again.",
            )
            return redirect(
                'product_list', family_slug=request.family.slug,
            )

        _clear_pending_duplicate(request, product_id)

        if updated_item is not None:
            transaction.on_commit(
                lambda: broadcast_list_item_update(updated_item, request.user), # noqa
            )
        if new_item is not None:
            transaction.on_commit(
                lambda: broadcast_list_item_added(new_item, request.user),
            )

        # increase_quantity always affects the original product (whether
        # it incremented the existing row or, falling back, created a
        # fresh one for it); create_variant only ever affects the new
        # variant product, never the original.
        affected_product = (
            product if choice == 'increase_quantity' else new_item.product
        )
        transaction.on_commit(
            lambda: broadcast_product_list_status(
                request.family, affected_product, request.user,
            ),
        )

        return redirect(
            'product_list' if get_pending_duplicate_conflicts(request)
            else 'shopping_list',
            family_slug=request.family.slug,
        )

    def _increase_quantity(self, request, product, existing_item):
        if existing_item is None:
            # No longer a duplicate (e.g. resolved/removed concurrently
            # or from another tab): fall back to adding it normally.
            new_item = _add_fresh_list_item(request, product)
            return new_item, None

        # Increasing a request adds a new ordinary unit; an existing
        # owner's basket (or a cancelled unit) is never enlarged silently.
        new_item = _add_requested_unit(request, existing_item)
        return new_item, None

    def _create_variant(self, request, original_product, variant_name):
        if not variant_name:
            raise ValidationError(
                "Enter a name for the new product variant.",
            )

        variant = Product(
            product_name=variant_name,
            family=request.family,
            category=original_product.category,
            default_quantity=original_product.default_quantity,
            default_unit=original_product.default_unit,
            default_shop=original_product.default_shop,
            notes=original_product.notes,
            creator=request.user,
            based_on=original_product,
        )
        variant.full_clean()
        variant.save()

        new_item = _add_fresh_list_item(request, variant)
        return new_item, None


@method_decorator(login_required, name='dispatch')
class DismissShoppingListDuplicateView(FamilyMembershipRequiredMixin, View):
    """Lets the user dismiss a pending duplicate-product prompt without
    making any change to the shopping list."""

    def post(self, request, *args, **kwargs):
        raw_product_id = request.POST.get('product_id', '')
        if raw_product_id.isdigit():
            _clear_pending_duplicate(request, int(raw_product_id))
        return redirect(
            'product_list', family_slug=request.family.slug,
        )


@method_decorator(login_required, name='dispatch')
class DismissShoppingListCompletionView(FamilyMembershipRequiredMixin, View):
    """Lets the user defer the "list complete, start a new one?"
    suggestion. Makes no change to the shopping list itself; the
    dismissal is remembered only for this browser session, and only
    until the list's item count changes again."""

    def post(self, request, *args, **kwargs):
        raw_list_id = request.POST.get('shopping_list_id', '')
        raw_item_count = request.POST.get('item_count', '')
        if raw_list_id.isdigit() and raw_item_count.isdigit():
            request.session[SESSION_KEY_DISMISSED_COMPLETION] = {
                'shopping_list_id': int(raw_list_id),
                'item_count': int(raw_item_count),
            }
        return redirect(
            'shopping_list', family_slug=request.family.slug,
        )


@method_decorator(login_required, name='dispatch')
class CloseShoppingListView(FamilyMembershipRequiredMixin, View):
    """
    Closes out the family's open list - whether or not it still has
    unpurchased and/or cancelled items - and starts a fresh one,
    independently optionally carrying forward unpurchased (never bought,
    never cancelled) and/or cancelled (never bought) products. The whole
    operation - closing, creating the replacement, and any carry-forward
    - happens in one atomic transaction, serialised per family by
    locking the Family row, so concurrent or retried requests can never
    produce two open lists or duplicate carry-forward items.
    """

    def post(self, request, *args, **kwargs):
        raw_list_id = request.POST.get('shopping_list_id', '')
        if not raw_list_id.isdigit():
            return HttpResponseBadRequest("Invalid request")
        requested_id = int(raw_list_id)
        carry_forward_unpurchased = (
            request.POST.get('carry_forward_unpurchased') == 'on'
        )
        carry_forward_cancelled = (
            request.POST.get('carry_forward_cancelled') == 'on'
        )

        with transaction.atomic():
            locked_family = Family.objects.select_for_update().get(
                pk=request.family.pk,
            )
            open_list = ShoppingList.objects.filter(
                family=locked_family, status=ShoppingList.Status.OPEN,
            ).first()

            if open_list is None or open_list.id != requested_id:
                # Already closed/replaced by a concurrent or retried
                # request (or the page was simply stale): nothing left to
                # do here. Idempotent no-op, not an error - the user just
                # sees the list that resulted.
                return redirect(
                    'shopping_list', family_slug=request.family.slug,
                )

            old_items = list(
                ListItem.objects.filter(shopping_list=open_list)
                .select_related('product')
            )

            now = timezone.now()
            open_list.status = ShoppingList.Status.CLOSED
            open_list.closed_at = now
            open_list.closed_by = request.user
            open_list.save(update_fields=['status', 'closed_at', 'closed_by']) # noqa
            ListItem.objects.filter(shopping_list=open_list).update(
                current=False,
            )

            new_list = ShoppingList.objects.create(
                family=locked_family, created_by=request.user,
            )

            # Carry forward whichever of the two independent categories
            # were asked for - unpurchased (not bought, not cancelled)
            # and/or cancelled (not bought) - grouped by product so the
            # new list never ends up with the same product on two
            # separate rows. Already-bought items are never carried
            # forward, regardless of either choice.
            carry_candidates = [
                item for item in old_items
                if item.date_bought is None
                and (
                    (carry_forward_unpurchased and not item.cancelled)
                    or (carry_forward_cancelled and item.cancelled)
                )
            ]
            # Carry each selected unit independently, keeping request
            # identity and notes even when products have mixed states.
            new_items = []
            for item in carry_candidates:
                new_item = ListItem.objects.create(
                    product_id=item.product_id, family=locked_family,
                    shopping_list=new_list, request_key=item.request_key,
                    quantity_required=1, creator=item.creator,
                    preferred_shop=item.preferred_shop,
                    creator_notes=item.creator_notes,
                    shopper_notes=item.shopper_notes,
                )
                new_items.append(new_item)

        products_by_id = {
            item.product_id: item.product for item in old_items
        }
        for product in products_by_id.values():
            transaction.on_commit(
                lambda product=product: broadcast_product_list_status(
                    request.family, product, request.user,
                ),
            )
        for new_item in new_items:
            transaction.on_commit(
                lambda item=new_item: broadcast_list_item_added(
                    item, request.user,
                ),
            )
        transaction.on_commit(
            lambda: broadcast_shopping_list_replaced(
                request.family, open_list.id, new_list,
            ),
        )

        return redirect('shopping_list', family_slug=request.family.slug)


@method_decorator(login_required, name='dispatch')
class AddProductView(FamilyMembershipRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        if not has_family_permission(request, 'can_add_catalogue'):
            raise PermissionDenied
        form = ProductForm(request.POST, family=request.family)
        if form.is_valid():
            product = form.save(commit=False)
            product.family = request.family
            product.creator = request.user
            product.save()
        else:
            messages.error(request, "A product with this name already exists.")
        return redirect(
            'product_list', family_slug=request.family.slug,
        )


@method_decorator(login_required, name='dispatch')
class AddShopView(FamilyMembershipRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        if not has_family_permission(request, 'can_add_shops'):
            raise PermissionDenied
        form = ShopForm(request.POST, family=request.family)
        if form.is_valid():
            shop = form.save(commit=False)
            shop.family = request.family
            shop.creator = request.user
            shop.save()
        else:
            messages.error(request, "A shop with this name already exists.")
        return redirect(
            'shop_list', family_slug=request.family.slug,
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
            'category_list', family_slug=request.family.slug,
        )


@method_decorator(login_required, name='dispatch')
class FamilyMembersView(FamilyMembershipRequiredMixin, View):
    """The smallest coherent interface for a family administrator to see
    every member of the current family and change an ordinary member's
    role and six permissions, or remove a membership. Administrators
    always have full authority regardless of the six flags - the
    checkboxes only matter for non-admin rows. The "every family keeps at
    least one administrator" invariant is enforced by
    FamilyMembership.save()/delete() itself (see models.py), so it
    applies here, to any other caller of those methods, and even to a
    direct Django-admin edit/delete of a membership; this view only adds
    the surrounding family-row lock for proper concurrency protection and
    a friendly error message. Restricted entirely to family admins: there
    is no read-only variant for ordinary members."""

    def get(self, request, *args, **kwargs):
        if not is_family_admin(request):
            raise PermissionDenied
        memberships = list(FamilyMembership.objects.filter(
            family=request.family,
        ).select_related('user').order_by('date_joined', 'pk'))
        admin_count = sum(1 for m in memberships if m.is_family_admin)
        # One query for all rows, rather than one EmailAddress lookup
        # per membership, to avoid N+1.
        verified_user_ids = set(EmailAddress.objects.filter(
            user_id__in=[m.user_id for m in memberships], verified=True,
        ).values_list('user_id', flat=True))
        rows = [
            {
                'membership': membership,
                'form': MembershipPermissionsForm(
                    instance=membership,
                    auto_id=f'id_%s_{membership.pk}',
                ),
                'is_sole_admin': (
                    membership.is_family_admin and admin_count == 1
                ),
                'has_verified_email': membership.user_id in verified_user_ids, # noqa
            }
            for membership in memberships
        ]
        return render(request, 'shopping_list/family_members.html', {
            'rows': rows,
        })

    def post(self, request, *args, **kwargs):
        if not is_family_admin(request):
            raise PermissionDenied

        action = request.POST.get('action')
        membership_id = request.POST.get('membership_id')

        with transaction.atomic():
            # Locking the family row serialises this against any other
            # concurrent role-change/removal request for the same
            # family - including a Django-admin edit/delete, which takes
            # the same lock (see FamilyMembershipAdmin) - so two
            # operations can't both pass the "another admin remains"
            # check and leave zero.
            lock_family_for_membership_changes(request.family.pk)
            membership = get_object_or_404(
                FamilyMembership.objects.select_for_update(),
                pk=membership_id, family=request.family,
            )

            if action == 'remove':
                display_name = membership.display_name
                try:
                    membership.delete()
                    messages.success(
                        request, f"Removed {display_name} from the family.",
                    )
                except ValidationError as error:
                    messages.error(request, "; ".join(error.messages))
            elif action == 'update':
                form = MembershipPermissionsForm(
                    request.POST, instance=membership,
                )
                if form.is_valid():
                    try:
                        form.save()
                        messages.success(
                            request,
                            f"Updated settings for "
                            f"{membership.display_name}.",
                        )
                    except ValidationError as error:
                        messages.error(request, "; ".join(error.messages))
                else:
                    error_text = "; ".join(
                        msg for msgs in form.errors.values() for msg in msgs
                    )
                    messages.error(
                        request,
                        error_text or "Could not update that member's "
                        "settings.",
                    )
            else:
                return HttpResponseBadRequest("Unknown action.")

        return redirect('family_members', family_slug=request.family.slug)

