
from .models import (
    Category,
    FamilyMembership,
    ListItem,
    Product,
    Shop,
)
from .forms import CategoryForm, ProductForm, ShopForm
from utilities.utils import is_adult
import logging
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import render, redirect, get_object_or_404
from django.utils.decorators import method_decorator
from django.views import View
from django.contrib import messages
from django.contrib.auth.mixins import UserPassesTestMixin
from django.core.exceptions import PermissionDenied

logger = logging.getLogger(__name__)


def get_user_family(user):
    membership = FamilyMembership.objects.select_related("family").filter(
        user=user,
    ).first()

    if membership is None:
        raise PermissionDenied("You are not assigned to a family.")

    return membership.family


class FamilyRequiredMixin:
    def dispatch(self, request, *args, **kwargs):
        request.family = get_user_family(request.user)
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
                'bought': item.bought,
                'cancelled': item.cancelled,
                'user': user.get_username(),
            }
        )
    except Exception:
        logger.exception(
            "Failed to broadcast list item update for item %s", item.id,
        )


def welcome_page(request):
    if request.user.is_authenticated:
        return redirect('shopping_list')
    return render(request, 'logged_out_homepage.html')


class AdultRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return is_adult(self.request.user)


@method_decorator(login_required, name='dispatch')
class ShoppingListView(FamilyRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        # Both options are 'category' by default
        filter_chosen = request.GET.get('filter_by', 'category')
        order_chosen = request.GET.get('order_by', 'category')

        shopping_list = ListItem.objects.filter(
            family=request.family,
            bought=False,
            cancelled=False,
            current=True,
        )

        if order_chosen == 'shop':
            shopping_list = shopping_list.order_by('shop_bought__shop_name', 'product__category__category_name') # noqa
        else:
            shopping_list = shopping_list.order_by('product__category__category_name', 'shop_bought__shop_name') # noqa

        shops = Shop.objects.filter(
            family=request.family,
            current=True,
            ).order_by('type_of_shop')

        return render(request, 'shopping_list/shopping_list.html', {
            'shopping_list': shopping_list,
            'shops': shops,
            'filter_chosen': filter_chosen,
            'order_chosen': order_chosen,
            })

    def post(self, request, *args, **kwargs):
        try:
            item_id = request.POST.get('item_id')
            action = request.POST.get('action')
            if not item_id or not action:
                return HttpResponseBadRequest(
                    "Invalid request: Missing item_id or action"
                )

            item = get_object_or_404(
                ListItem, id=item_id, family=request.family,
            )

            if action == 'cancel':
                item.cancelled = True
            elif action == 'uncancel':
                item.cancelled = False
            elif action == 'buy':
                item.bought = True
            elif action == 'unbuy':
                item.bought = False
            else:
                return HttpResponseBadRequest(
                    "Invalid request: Unknown action"
                )

            item.save()
            transaction.on_commit(
                lambda: broadcast_list_item_update(item, request.user),
            )
            return redirect('shopping_list')

        except Http404:
            raise
        except Exception as e:
            print(f"Error processing POST request: {e}")
            return HttpResponseBadRequest("Invalid request")


@method_decorator(login_required, name='dispatch')
class ListItemView(FamilyRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        slug = self.kwargs.get('slug')
        list_item = get_object_or_404(
            ListItem,
            slug=slug,
            family=request.family,
            current=True,
            bought=False
        )

        return render(
            request,
            'shopping_list/list_item.html',
            {'list_item': list_item}
        )


@method_decorator(login_required, name='dispatch')
class ProductListView(FamilyRequiredMixin, View):
    def get(self, request, *args, **kwargs):

        product_list = Product.objects.filter(
            family=request.family, current=True,
        ).order_by('category__category_name', 'date_created')

        grouped_products = {}

        # Group products by category
        for product in product_list:
            category_name = product.category.category_name
            if category_name not in grouped_products:
                grouped_products[category_name] = []
            grouped_products[category_name].append(product)
        # For the AddProductModalDialog to populate the dropdowns
        categories = Category.objects.filter(family=request.family)
        shops = Shop.objects.filter(family=request.family)

        return render(request, 'shopping_list/product_list.html', {
            'grouped_products': grouped_products,
            'categories': categories,
            'shops': shops
        })


@method_decorator(login_required, name='dispatch')
class ShopListView(FamilyRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        shopList = Shop.objects.filter(family=request.family, current=True)
        types_of_shop = Shop.TYPES_OF_SHOP
        return render(
            request,
            'shopping_list/shop_list.html',
            {'shop_list': shopList, 'types_of_shop': types_of_shop}
        )


@method_decorator(login_required, name='dispatch')
class CategoryListView(FamilyRequiredMixin, View):
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
class ProductView(FamilyRequiredMixin, View):
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
        form = ProductForm(
            request.POST, instance=product, family=request.family,
        )
        if form.is_valid():
            form.save()
            return redirect('product_list')
        else:
            messages.error(request, "A product with this name already exists.")
        return render(
            request,
            'shopping_list/product.html',
            {'form': form, 'product': product}
        )


@method_decorator(login_required, name='dispatch')
class ShopView(FamilyRequiredMixin, View):
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
        form = ShopForm(request.POST, instance=shop, family=request.family)
        if form.is_valid():
            form.save()
            return redirect('shop_list')
        else:
            for field, errors in form.errors.items():
                print(f"{field}: {errors}")
        return render(
            request,
            'shopping_list/shop.html',
            {'form': form, 'shop': shop}
        )


@method_decorator(login_required, name='dispatch')
class CategoryView(FamilyRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        slug = self.kwargs.get('slug')
        category = get_object_or_404(
            Category, slug=slug, family=request.family,
        )
        return render(
            request,
            'shopping_list/category.html',
            {'category': category}
        )


@method_decorator(login_required, name='dispatch')
class AddToShoppingListView(FamilyRequiredMixin, View):
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

        return redirect('shopping_list')


@method_decorator(login_required, name='dispatch')
class AddProductView(FamilyRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        form = ProductForm(request.POST, family=request.family)
        if form.is_valid():
            product = form.save(commit=False)
            product.family = request.family
            product.creator = request.user
            product.save()
        else:
            messages.error(request, "A product with this name already exists.")
        return redirect('product_list')


@method_decorator(login_required, name='dispatch')
class AddShopView(FamilyRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        form = ShopForm(request.POST, family=request.family)
        if form.is_valid():
            shop = form.save(commit=False)
            shop.family = request.family
            shop.creator = request.user
            shop.save()
        else:
            messages.error(request, "A shop with this name already exists.")
        return redirect('shop_list')


@method_decorator(login_required, name='dispatch')
class AddCategoryView(FamilyRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        form = CategoryForm(request.POST, family=request.family)
        if form.is_valid():
            category = form.save(commit=False)
            category.family = request.family
            category.creator = request.user
            category.save()
        else:
            messages.error(request, "A category with this name already exists.")
        return redirect('category_list')
