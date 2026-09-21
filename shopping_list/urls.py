from . import views
from django.urls import path


urlpatterns = [
    path('shopping-list/', views.ShoppingListView.as_view(), name='shopping_list'), # noqa
    path('list-item-<slug:slug>/', views.ListItemView.as_view(), name='list_item'),  # noqa
    path('add-to-shopping-list/', views.AddToShoppingListView.as_view(), name='add_to_shopping_list'),  # noqa
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
]
