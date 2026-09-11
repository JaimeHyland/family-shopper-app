from django.db import models
from django.conf import settings
from django.contrib.auth.models import User
from django.db.models.functions import Lower
from django.utils.text import slugify
from django.core.exceptions import ValidationError
import uuid


class Family(models.Model):
    name = models.CharField(max_length=100)
    public_id = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
    )
    date_created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name_plural = "families"


class FamilyMembership(models.Model):

    class Role(models.TextChoices):
        ADMIN = "admin", "Family admin"
        MEMBER = "member", "Family member"

    family = models.ForeignKey(
        Family,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="family_memberships",
    )
    # The name shown for this user within this family; may differ per
    # family (e.g. "Hannah" in one family, "Han" in another).
    display_name = models.CharField(max_length=100)
    role = models.CharField(
        max_length=10,
        choices=Role.choices,
        default=Role.MEMBER,
    )
    date_joined = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["family", "user"],
                name="unique_family_user_membership",
            ),
            models.UniqueConstraint(
                Lower("display_name"), "family",
                name="unique_display_name_per_family_ci",
            ),
        ]

    @property
    def is_family_admin(self):
        # Not to be confused with Django's is_staff/is_superuser.
        return self.role == self.Role.ADMIN

    def clean(self):
        super().clean()

        if self.display_name:
            self.display_name = self.display_name.strip()

        if not self.display_name:
            raise ValidationError({
                "display_name": "The display name cannot be blank.",
            })

        if self.family_id:
            matching = FamilyMembership.objects.filter(
                family_id=self.family_id,
                display_name__iexact=self.display_name,
            )
            if self.pk:
                matching = matching.exclude(pk=self.pk)
            if matching.exists():
                raise ValidationError({
                    "display_name": (
                        "That display name is already used by someone "
                        "else in this family."
                    ),
                })

    def __str__(self):
        return (
            f"{self.display_name} \u2014 {self.family} "
            f"({self.get_role_display()})"
        )


class AutomaticAdultGroupMembership(models.Model):
    """Tracks adult-group membership granted by an application rule."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="automatic_adult_group_membership",
    )


class PendingFamilyAdminRegistration(models.Model):
    """
    A not-yet-completed public self-registration request for a new Family
    admin. No User/Family/FamilyMembership exist until the emailed link is
    opened and the completion form is submitted successfully.
    """
    email = models.EmailField()
    family_name = models.CharField(max_length=100)
    token_digest = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["email"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["email"],
                condition=models.Q(consumed_at__isnull=True),
                name="unique_active_pending_registration_per_email",
            ),
        ]

    def __str__(self):
        return f"Pending registration for {self.email}"


class Shop(models.Model):

    TYPES_OF_SHOP = (
        (1, 'Supermarket'),
        (2, 'Organic shop'),
        (3, 'DIY center'),
        (4, 'Drugstore'),
        (5, 'Stationer'),
        (6, 'Specialist retailer'),
        (7, 'Flatpack furniture'),
        (8, 'Deli & fine foods'),
        (9, 'Wines and spirits'),
    )

    shop_name = models.CharField(max_length=50, null=False, blank=False)
    slug = models.SlugField(max_length=250, null=True, blank=True)
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True,
        related_name="user_who_created_shop"
    )
    date_created = models.DateTimeField(auto_now=True)
    family = models.ForeignKey(
        Family,
        on_delete=models.CASCADE,
        related_name="shops",
    )
    notes = models.TextField(null=True, blank=True)
    type_of_shop = models.IntegerField(choices=TYPES_OF_SHOP)
    current = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['family', 'slug'], name='unique_shop_slug_per_family',
            ),
        ]

    def clean(self):
        super().clean()

        if self.family_id and self.shop_name:
            matching_shops = Shop.objects.filter(
                family_id=self.family_id,
                shop_name__iexact=self.shop_name,
                current=True,
            )

            if self.pk:
                matching_shops = matching_shops.exclude(pk=self.pk)

            if matching_shops.exists():
                raise ValidationError({
                    "shop_name": (
                        "A current shop with this name already exists "
                        "for this family."
                    ),
                })

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.shop_name)
            slug = base_slug
            count = 1
            while Shop.objects.filter(
                family_id=self.family_id, slug=slug,
            ).exists():
                slug = f'{base_slug}-{count}'
                count += 1
            self.slug = slug
        super().save(*args, **kwargs)

    def __str__(self):
        return self.shop_name


class Category(models.Model):
    category_name = models.CharField(max_length=50, null=False, blank=False)
    slug = models.SlugField(max_length=250, null=True, blank=True)
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="user_who_created_category"
        )
    date_created = models.DateTimeField(auto_now=True)
    family = models.ForeignKey(
        Family,
        on_delete=models.CASCADE,
        related_name="categories",
    )
    notes = models.TextField(null=True, blank=True)
    current = models.BooleanField(default=True)

    def clean(self):
        super().clean()

        if self.family_id and self.category_name:
            matching_categories = Category.objects.filter(
                family_id=self.family_id,
                category_name__iexact=self.category_name,
                current=True,
            )

            if self.pk:
                matching_categories = matching_categories.exclude(pk=self.pk)

            if matching_categories.exists():
                raise ValidationError({
                    "category_name": (
                        "A current category with this name already exists "
                        "for this family."
                    ),
                })

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.category_name)
            slug = base_slug
            count = 1
            while Category.objects.filter(
                family_id=self.family_id, slug=slug,
            ).exists():
                slug = f'{base_slug}-{count}'
                count += 1
            self.slug = slug
        super().save(*args, **kwargs)

    def __str__(self):
        return self.category_name

    class Meta:
        verbose_name_plural = "categories"
        constraints = [
            models.UniqueConstraint(
                fields=['family', 'slug'],
                name='unique_category_slug_per_family',
            ),
        ]


class Product(models.Model):
    product_name = models.CharField(max_length=254, null=False, blank=False)
    slug = models.SlugField(max_length=250, null=True, blank=True)
    family = models.ForeignKey(
        Family,
        on_delete=models.CASCADE,
        related_name="products",
    )
    category = models.ForeignKey(
        Category, on_delete=models.CASCADE, related_name="product_category"
    )
    default_quantity = models.IntegerField(null=True, blank=True)
    default_unit = models.CharField(max_length=32, null=True, blank=True)
    default_shop = models.ForeignKey(
        Shop, on_delete=models.CASCADE, related_name="default_where_to_buy"
    )
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="user_who_created_product"
    )
    date_created = models.DateTimeField(auto_now=True)
    notes = models.TextField(null=True, blank=True)
    current = models.BooleanField(default=True)

    def save(self, *args, **kwargs):
        if not self.pk:
            # New instance
            base_slug = slugify(self.product_name)
            slug = base_slug
            count = 1
            while Product.objects.filter(
                family_id=self.family_id, slug=slug,
            ).exists():
                slug = f'{base_slug}-{count}'
                count += 1
            self.slug = slug
        else:
            # Existing instance
            old_product = Product.objects.get(pk=self.pk)
            if old_product.product_name != self.product_name:
                base_slug = slugify(self.product_name)
                slug = base_slug
                count = 1
                while Product.objects.filter(
                    family_id=self.family_id, slug=slug,
                ).exclude(pk=self.pk).exists():
                    slug = f'{base_slug}-{count}'
                    count += 1
                self.slug = slug

        super().save(*args, **kwargs)

    def clean(self):
        super().clean()

        errors = {}

        if self.family_id and self.product_name:
            matching_products = Product.objects.filter(
                family_id=self.family_id,
                product_name__iexact=self.product_name,
                current=True,
            )

            if self.pk:
                matching_products = matching_products.exclude(pk=self.pk,)

            if matching_products.exists():
                errors["product_name"] = (
                    "A current product with this name already exists "
                    "for this family."
                )

        if (
            self.family_id
            and self.category_id
            and self.category.family_id != self.family_id
        ):
            errors["category"] = (
                "The category must belong to the same family as the product."
            )

        if (
            self.family_id
            and self.default_shop_id
            and self.default_shop.family_id != self.family_id
        ):
            errors["default_shop"] = (
                "The default shop must belong to the same family as the product."
            )

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return self.product_name

    class Meta:
        ordering = ['date_created']
        constraints = [
            models.UniqueConstraint(
                fields=['family', 'slug'], name='unique_product_slug_per_family',
            ),
        ]


class ListItem(models.Model):
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="product_of_list_item"
    )
    date_created = models.DateTimeField(auto_now=True)
    family = models.ForeignKey(
        Family,
        on_delete=models.CASCADE,
        related_name="list_items",
    )
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="user_who_created_list_item"
    )
    preferred_shop = models.ForeignKey(
        Shop, on_delete=models.SET_NULL, null=True, blank=True
    )
    bought = models.BooleanField(default=False)
    date_bought = models.DateTimeField(default=None, blank=True, null=True)
    buyer = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="user_who_bought_item",
        null=True,
        blank=True
    )
    cancelled = models.BooleanField(default=False)
    date_cancelled = models.DateTimeField(default=None, blank=True, null=True)
    cancelled_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="user_who_cancelled_item",
        null=True,
        blank=True
    )
    quantity_required = models.IntegerField(default=1)
    quantity_bought = models.IntegerField(default=0)
    shop_bought = models.ForeignKey(
        Shop,
        on_delete=models.CASCADE,
        related_name="shop_where_item_bought",
        null=True,
        blank=True
    )
    creator_notes = models.TextField(null=True, blank=True)
    shopper_notes = models.TextField(null=True, blank=True)
    slug = models.SlugField(null=True, blank=True)
    current = models.BooleanField(default=True)

    class Meta:
        ordering = ['date_created']
        verbose_name = "list item"
        constraints = [
            models.UniqueConstraint(
                fields=['family', 'slug'],
                name='unique_list_item_slug_per_family',
            ),
        ]

    def __str__(self):
        return f"{self.product.product_name} \u2013 {self.product.category}"

    def clean(self):
        super().clean()

        errors = {}

        if (
            self.family_id
            and self.product_id
            and self.product.family_id != self.family_id
        ):
            errors["product"] = (
                "The product must belong to the same family as the list item."
            )

        if (
            self.family_id
            and self.preferred_shop_id
            and self.preferred_shop.family_id != self.family_id
        ):
            errors["preferred_shop"] = (
                "The preferred shop must belong to the same family as the "
                "list item."
            )

        if (
            self.family_id
            and self.shop_bought_id
            and self.shop_bought.family_id != self.family_id
        ):
            errors["shop_bought"] = (
                "The shop where the item was bought must belong to the "
                "same family as the list item."
            )

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self.preferred_shop and self.product.default_shop:
            self.preferred_shop = self.product.default_shop
        if not self.pk:
            self.clean()
        super().save(*args, **kwargs)
        if not self.slug:
            self.slug = slugify(f"{self.product.slug}-{self.id}")
            super().save(update_fields=['slug'])
