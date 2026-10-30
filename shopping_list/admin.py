from django.contrib import admin
from django.db import transaction
from .models import (
    Category,
    Family,
    FamilyMembership,
    ListItem,
    PendingFamilyAdminRegistration,
    Product,
    Shop,
    ShoppingList,
    lock_family_for_membership_changes,
)


class FamilyAdmin(admin.ModelAdmin):
    list_display = ('name', 'public_id', 'date_created')
    readonly_fields = ('public_id', 'date_created')
    search_fields = ('name',)


class FamilyMembershipAdmin(admin.ModelAdmin):
    list_display = ('display_name', 'user', 'family', 'role', 'date_joined')
    list_filter = ('role', 'family')
    search_fields = ('display_name', 'user__username', 'family__name')
    readonly_fields = ('date_joined',)

    def save_model(self, request, obj, form, change):
        # Takes the same family-row lock as FamilyMembersView, so an
        # admin-initiated role change is serialised against concurrent
        # changes to the same family's memberships (whether made through
        # the membership-management view or here).
        with transaction.atomic():
            lock_family_for_membership_changes(obj.family_id)
            super().save_model(request, obj, form, change)

    def delete_model(self, request, obj):
        with transaction.atomic():
            lock_family_for_membership_changes(obj.family_id)
            super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        # QuerySet.delete() bypasses each instance's overridden delete(),
        # which is where the "every family keeps at least one
        # administrator" invariant is enforced, so delete one at a time.
        # Lock every distinct affected family first, in a fixed (pk)
        # order, so a bulk "Delete selected" spanning several families
        # can't deadlock against another concurrent operation locking
        # the same families in a different order.
        family_ids = sorted(set(
            queryset.values_list("family_id", flat=True),
        ))
        with transaction.atomic():
            for family_id in family_ids:
                lock_family_for_membership_changes(family_id)
            for membership in queryset:
                membership.delete()


class PendingFamilyAdminRegistrationAdmin(admin.ModelAdmin):
    # token_digest is a one-way hash, never the raw token, but it still
    # should never be hand-edited via the admin.
    list_display = (
        'email', 'family_name', 'created_at', 'expires_at', 'consumed_at',
    )
    list_filter = ('consumed_at',)
    search_fields = ('email', 'family_name')
    readonly_fields = (
        'email', 'family_name', 'token_digest', 'created_at', 'expires_at',
        'consumed_at',
    )


class ListItemAdmin(admin.ModelAdmin):
    # Display fields on admin/.../change page
    fields = (
        'product', 'shopping_list', 'category', 'preferred_shop',
        'in_basket', 'buyer', 'shop_bought', 'creator_notes',
        'shopper_notes', 'cancelled', 'cancelled_by', 'date_cancelled',
        'quantity_required', 'quantity_bought', 'current',
    )

    readonly_fields = (
        'default_shop', 'date_bought', 'category',
        'creator', 'date_created', 'creator', 'shopping_list',
    )

    def default_shop(self, obj):
        return f"{obj.product.default_shop}"

    def category(self, obj):
        return f"{obj.product.category}"

    default_shop.short_description = 'Default Shop'
    category.short_description = 'Category'

    # Group ListItems by Product category and allow filtering
    list_display = (
        'product', 'shopping_list', 'category', 'preferred_shop',
        'in_basket', 'cancelled',
    )

    # Enable filtering by category (from Product model) and by which
    # shopping list (open vs historical/closed) an item belongs to.
    list_filter = ('product__category', 'preferred_shop', 'shopping_list__status') # noqa

    # Order items by category and product
    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        return queryset.select_related('product').order_by(
            'preferred_shop', 'product__category', 'product__product_name'
            )

    # Make the category field sortable in the list
    def category(self, obj):
        return obj.product.category

    # Allow ordering by the category field
    category.admin_order_field = 'product__category'

    ordering = ('preferred_shop', 'product__category',)


class ProductAdmin(admin.ModelAdmin):
    list_display = ('product_name', 'slug', 'based_on')
    fields = (
        'product_name', 'slug', 'category', 'default_quantity', 'default_unit',
        'default_shop', 'based_on', 'creator', 'date_created', 'notes',
        'current',
        )
    readonly_fields = ('slug', 'date_created', 'creator',)


class ShopAdmin(admin.ModelAdmin):
    list_display = ('shop_name', 'slug')
    fields = (
        'shop_name', 'slug', 'family', 'type_of_shop', 'creator',
        'date_created', 'notes', 'current',
    )
    readonly_fields = ('slug', 'family', 'date_created', 'creator',)


class CategoryAdmin(admin.ModelAdmin):
    list_display = ('category_name', 'slug')
    fields = (
        'category_name', 'slug', 'family', 'creator', 'date_created',
        'notes', 'current',
    )
    readonly_fields = ('slug', 'family', 'date_created', 'creator',)


class ShoppingListAdmin(admin.ModelAdmin):
    list_display = (
        'family', 'status', 'created_at', 'closed_at', 'closed_by',
    )
    list_filter = ('status', 'family')
    readonly_fields = (
        'family', 'created_at', 'created_by', 'closed_at', 'closed_by',
    )


admin.site.register(ListItem, ListItemAdmin)
admin.site.register(Product, ProductAdmin)
admin.site.register(Category, CategoryAdmin)
admin.site.register(Shop, ShopAdmin)
admin.site.register(Family, FamilyAdmin)
admin.site.register(FamilyMembership, FamilyMembershipAdmin)
admin.site.register(ShoppingList, ShoppingListAdmin)
admin.site.register(
    PendingFamilyAdminRegistration, PendingFamilyAdminRegistrationAdmin,
)
