from django import forms
from .models import Product, Shop, Category, FamilyMembership
from django.core.exceptions import ValidationError
from django_summernote.widgets import SummernoteWidget


class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = [
            'product_name',
            'category',
            'default_quantity',
            'default_unit',
            'default_shop',
            'notes',
        ]
        widgets = {
            'notes': SummernoteWidget(),
        }

    def __init__(self, *args, family=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.family = family or getattr(self.instance, "family", None)
        if self.family:
            self.instance.family = self.family

        # The model fields are nullable so a category/shop can be
        # deactivated without deleting products that reference them, but
        # this form (ordinary product creation/editing) still requires
        # both to be chosen.
        self.fields["category"].required = True
        self.fields["default_shop"].required = True

        self.fields["category"].queryset = Category.objects.none()
        self.fields["default_shop"].queryset = Shop.objects.none()

        if self.family:
            self.fields["category"].queryset = Category.objects.filter(
                family=self.family,
            )
            self.fields["default_shop"].queryset = Shop.objects.filter(
                family=self.family,
            )

    def clean_product_name(self):
        product_name = self.cleaned_data.get("product_name")

        if product_name and self.family:
            matching_products = Product.objects.filter(
                family=self.family,
                product_name__iexact=product_name,
                current=True,
            )

            if self.instance.pk:
                matching_products = matching_products.exclude(
                    pk=self.instance.pk,
                )

            if matching_products.exists():
                raise ValidationError(
                    "A current product with this name already exists "
                    "for this family."
                )

        return product_name


class ShopForm(forms.ModelForm):
    class Meta:
        model = Shop
        fields = ['shop_name', 'type_of_shop', 'notes',]
        widgets = {
            'notes': SummernoteWidget(),
        }

    def __init__(self, *args, family=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.family = family or getattr(self.instance, "family", None)
        if self.family:
            self.instance.family = self.family

    def clean_shop_name(self):
        shop_name = self.cleaned_data.get("shop_name")

        if shop_name and self.family:
            matching_shops = Shop.objects.filter(
                family=self.family,
                shop_name__iexact=shop_name,
                current=True,
            )

            if self.instance.pk:
                matching_shops = matching_shops.exclude(pk=self.instance.pk)

            if matching_shops.exists():
                raise ValidationError(
                    "A current shop with this name already exists "
                    "for this family."
                )

        return shop_name


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ['category_name', 'notes',]
        widgets = {
            'notes': SummernoteWidget(),
        }

    def __init__(self, *args, family=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.family = family or getattr(self.instance, "family", None)
        if self.family:
            self.instance.family = self.family

    def clean_category_name(self):
        category_name = self.cleaned_data.get("category_name")

        if category_name and self.family:
            matching_categories = Category.objects.filter(
                family=self.family,
                category_name__iexact=category_name,
                current=True,
            )

            if self.instance.pk:
                matching_categories = matching_categories.exclude(
                    pk=self.instance.pk,
                )

            if matching_categories.exists():
                raise ValidationError(
                    "A current category with this name already exists "
                    "for this family."
                )

        return category_name


class MembershipPermissionsForm(forms.ModelForm):
    """Lets a family administrator change one existing member's role and
    six granular permissions. Never creates or removes a membership
    itself (that is handled separately by FamilyMembersView), and never
    touches family/user/display_name."""

    class Meta:
        model = FamilyMembership
        fields = [
            'role',
            'can_add_list', 'can_remove_list',
            'can_add_catalogue', 'can_remove_catalogue',
            'can_add_shops', 'can_remove_shops',
        ]
