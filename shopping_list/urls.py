from . import views
from .seed_views import SeedListView
from django.urls import path


urlpatterns = [
    path('shopping-list/', views.ShoppingListView.as_view(), name='shopping_list'), # noqa
    path('seed-lists/', SeedListView.as_view(), name='seed_lists'),
    path('list-item-<slug:slug>/', views.ListItemView.as_view(), name='list_item'),  # noqa
    path('add-to-shopping-list/', views.AddToShoppingListView.as_view(), name='add_to_shopping_list'),  # noqa
    path('resolve-shopping-list-duplicate/', views.ResolveShoppingListDuplicateView.as_view(), name='resolve_shopping_list_duplicate'),  # noqa
    path('dismiss-shopping-list-duplicate/', views.DismissShoppingListDuplicateView.as_view(), name='dismiss_shopping_list_duplicate'),  # noqa
    path('close-shopping-list/', views.CloseShoppingListView.as_view(), name='close_shopping_list'),  # noqa
    path('dismiss-shopping-list-completion/', views.DismissShoppingListCompletionView.as_view(), name='dismiss_shopping_list_completion'),  # noqa
    path('products/', views.ProductListView.as_view(), name='product_list'),
    path('products/<slug:slug>/', views.ProductView.as_view(), name='product_detail'), # noqa
    path('products/<slug:slug>/delete/', views.ProductDeleteView.as_view(), name='product_delete'), # noqa
    path('add-product/', views.AddProductView.as_view(), name='add_product'),
    path('shops/', views.ShopListView.as_view(), name='shop_list'),
    path('shops/<slug:slug>/', views.ShopView.as_view(), name='shop_detail'),
    path('shops/<slug:slug>/delete/', views.ShopDeleteView.as_view(), name='shop_delete'), # noqa
    path('add-shop/', views.AddShopView.as_view(), name='add_shop'),
    path('categories/', views.CategoryListView.as_view(), name='category_list'), # noqa
    path('categories/<slug:slug>/', views.CategoryView.as_view(), name='category_detail'), # noqa
    path('categories/<slug:slug>/delete/', views.CategoryDeleteView.as_view(), name='category_delete'), # noqa
    path('add-category/', views.AddCategoryView.as_view(), name='add_category'), # noqa
    path('members/', views.FamilyMembersView.as_view(), name='family_members'), # noqa
]
