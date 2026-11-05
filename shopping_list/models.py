from django.db import models, transaction
from django.conf import settings
from django.contrib.auth.models import User
from django.db.models.functions import Lower
from django.utils.text import slugify
from django.core.exceptions import ValidationError
from utilities.utils import generate_unique_slug
from allauth.account.models import EmailAddress
import uuid


class Family(models.Model):
    name = models.CharField(max_length=100)
    public_id = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
    )
    # URL-facing identifier, generated once from name and never changed
    # afterwards (even if name is later edited). public_id remains the
    # authoritative DB identity and is not used in URLs.
    slug = models.SlugField(
        max_length=250,
        unique=True,
        editable=False,
    )
    date_created = models.DateTimeField(auto_now_add=True)
    # Whether this Family was founded through solo registration (an
    # individual using the app alone) rather than family-admin
    # registration. Persisted explicitly because it cannot be inferred
    # reliably later on: a freshly-registered family admin also starts
    # out with exactly one membership, identical in that respect to a
    # solo user, until other members are invited. Used e.g. to choose
    # the right default seed-list dataset (see shopping_list.seed_lists).
    is_solo = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = generate_unique_slug(
                self.name,
                lambda candidate: Family.objects.filter(
                    slug=candidate,
                ).exists(),
            )
        super().save(*args, **kwargs)

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

    # Six granular permissions for ordinary (non-admin) members. Admins
    # always have full authority regardless of these flags (see
    # utilities.utils.has_family_permission); they only gate ordinary
    # members. can_add_list/can_remove_list default True, preserving
    # existing ordinary members' current, unrestricted access to the
    # shopping-list add/remove operations they already had. The
    # catalogue/shop flags default False: those operations were
    # admin-only before this feature existed, so a new/unconfigured
    # ordinary member must not gain them automatically - an
    # administrator has to opt a member in explicitly via the
    # membership-management UI.
    can_add_list = models.BooleanField(default=True)
    can_remove_list = models.BooleanField(default=True)
    can_add_catalogue = models.BooleanField(default=False)
    can_remove_catalogue = models.BooleanField(default=False)
    can_add_shops = models.BooleanField(default=False)
    can_remove_shops = models.BooleanField(default=False)

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

    def _would_remove_last_admin(self):
        """True if, given this instance's CURRENT (about to be persisted
        or deleted) state, no other admin membership would remain for
        its family. Callers that need to serialise this check against
        concurrent requests must first lock the family row with
        Family.objects.select_for_update(); this method only reads the
        latest committed state, it does not itself lock anything."""
        if not self.family_id:
            return False
        return not FamilyMembership.objects.filter(
            family_id=self.family_id, role=self.Role.ADMIN,
        ).exclude(pk=self.pk).exists()

    def _has_verified_email(self):
        """True if this membership's user has at least one verified
        allauth EmailAddress -- the architecture's authoritative signal
        for "this account has a real, confirmed email," independent of
        the plain User.email field (which can be stale or unverified;
        see shopping_list.tests_identity_architecture)."""
        if not self.user_id:
            return False
        return EmailAddress.objects.filter(
            user_id=self.user_id, verified=True,
        ).exists()

    def _previous_role(self):
        """The role this membership had in the database before this
        in-memory change, or None for a not-yet-saved (brand new) row.
        Several checks below need to distinguish an actual role
        TRANSITION from a row simply being re-saved unchanged, or being
        created for the first time (e.g. a family's founding admin, who
        must not be blocked by the "promotion requires a verified
        email" rule below -- there is no PREVIOUS role to transition
        from)."""
        if not self.pk:
            return None
        return FamilyMembership.objects.filter(
            pk=self.pk,
        ).values_list("role", flat=True).first()

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

        previous_role = self._previous_role()

        if (
            previous_role is not None
            and previous_role != self.Role.ADMIN
            and self.role == self.Role.ADMIN
            and not self._has_verified_email()
        ):
            raise ValidationError({
                "role": (
                    "Family administrators must have a registered, "
                    "verified email address. Add and verify an email "
                    "for this account before making them an "
                    "administrator."
                ),
            })

        if (
            previous_role == self.Role.ADMIN
            and self.role != self.Role.ADMIN
            and self._would_remove_last_admin()
        ):
            raise ValidationError({
                "role": (
                    "A family must always have at least one "
                    "administrator. Promote another member before "
                    "removing this one's administrator role."
                ),
            })

    def save(self, *args, **kwargs):
        previous_role = self._previous_role()

        # Belt-and-braces alongside clean(): these also catch role
        # changes made without going through full_clean() (e.g. a raw
        # ORM/admin save), so neither invariant can be bypassed merely
        # by skipping validation.
        if (
            previous_role is not None
            and previous_role != self.Role.ADMIN
            and self.role == self.Role.ADMIN
            and not self._has_verified_email()
        ):
            raise ValidationError(
                "Family administrators must have a registered, "
                "verified email address. Add and verify an email for "
                "this account before making them an administrator."
            )

        if (
            previous_role == self.Role.ADMIN
            and self.role != self.Role.ADMIN
            and self._would_remove_last_admin()
        ):
            raise ValidationError(
                "A family must always have at least one "
                "administrator. Promote another member before "
                "removing this one's administrator role."
            )

        # An admin's six permission flags are irrelevant while they
        # hold that role (admins always pass -- see
        # utilities.utils.has_family_permission), so whatever is
        # currently stored for them may be stale, arbitrary, or never
        # configured at all. On an actual demotion, reset to the same
        # sensible defaults a brand-new ordinary member gets, rather
        # than carrying forward a meaningless or stale value.
        if previous_role == self.Role.ADMIN and self.role != self.Role.ADMIN:
            self.can_add_list = True
            self.can_remove_list = True
            self.can_add_catalogue = False
            self.can_remove_catalogue = False
            self.can_add_shops = False
            self.can_remove_shops = False

        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        # clean() is not invoked for deletions, so the invariant is
        # enforced here too - this is what protects a direct Django-admin
        # or ORM deletion, not just the membership-management view.
        if self.is_family_admin and self._would_remove_last_admin():
            raise ValidationError(
                "A family must always have at least one administrator. "
                "Promote another member before removing this one."
            )
        super().delete(*args, **kwargs)

    def __str__(self):
        return (
            f"{self.display_name} \u2014 {self.family} "
            f"({self.get_role_display()})"
        )


def lock_family_for_membership_changes(family_id):
    """Acquires the row lock that every supported path for changing a
    family's membership roles or removing a membership must take before
    checking-and-mutating, so concurrent operations are serialised and
    cannot both pass the "another administrator remains" check. Must be
    called inside transaction.atomic(). Used consistently by
    FamilyMembersView (the membership-management UI) and
    FamilyMembershipAdmin (Django admin's single and bulk operations) -
    see each call site for how the lock is used there."""
    return Family.objects.select_for_update().get(pk=family_id)


class PendingFamilyAdminRegistration(models.Model):
    """
    A not-yet-completed public self-registration request for a new Family
    admin. No User/Family/FamilyMembership exist until the emailed link is
    opened and the completion form is submitted successfully.
    """
    email = models.EmailField()
    # Blank for solo registrations: the family name isn't known at request
    # time there, only once the user picks a username at completion (see
    # complete_family_admin_registration_for_new_user/_for_existing_user).
    family_name = models.CharField(max_length=100, blank=True, default="")
    # The applicant's own name, captured at request time so it survives
    # through to completion (which may happen in a different browser/
    # session, e.g. after clicking the emailed link on another device).
    display_name = models.CharField(max_length=100, blank=True, default="")
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


class PendingFamilyMemberInvitation(models.Model):
    """
    A not-yet-accepted invitation for someone to join an EXISTING family
    as an ordinary member, sent by one of that family's admins. Mirrors
    PendingFamilyAdminRegistration's token/expiry/supersede pattern, but
    always resolves to a MEMBER (never ADMIN) role in an existing Family
    rather than creating a new one.
    """
    family = models.ForeignKey(
        Family,
        on_delete=models.CASCADE,
        related_name="pending_invitations",
    )
    email = models.EmailField()
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="sent_family_invitations",
    )
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
                fields=["family", "email"],
                condition=models.Q(consumed_at__isnull=True),
                name="unique_active_invitation_per_family_email",
            ),
        ]

    def __str__(self):
        return f"Invitation to {self.email} for {self.family}"


class PendingPasswordChange(models.Model):
    """
    Exists only while a User created via the no-email member-invitation
    path (an admin picks a username + initial password directly -- see
    invite_member_without_email) still needs to change that password
    before using the app. Deleted the moment they do; its mere existence
    IS the "must change password" flag -- mirroring how
    PendingFamilyAdminRegistration/PendingFamilyMemberInvitation rows are
    deleted once fulfilled rather than retained-but-flagged.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="pending_password_change",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Pending password change for {self.user}"


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
        Category, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="product_category",
    )
    default_quantity = models.IntegerField(null=True, blank=True)
    default_unit = models.CharField(max_length=32, null=True, blank=True)
    default_shop = models.ForeignKey(
        Shop, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="default_where_to_buy",
    )
    # Set when this product was created as a variant of another product
    # (e.g. "Milk (organic)" based on "Milk"), via the shopping-list
    # duplicate-product prompt. Left null for ordinary, non-variant
    # products. SET_NULL on delete: losing the original must not take the
    # variant down with it.
    based_on = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="variants",
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

        if (
            self.family_id
            and self.based_on_id
            and self.based_on.family_id != self.family_id
        ):
            errors["based_on"] = (
                "The original product must belong to the same family as "
                "the variant."
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


class ShoppingList(models.Model):
    """
    The lifecycle entity behind "a family's shopping list". A family has
    at most one OPEN list at a time (enforced below with a database
    constraint, not just application logic); closing it preserves its
    ListItems as a historical record rather than deleting or mutating
    them, and a fresh OPEN list is created to replace it.
    """

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"

    family = models.ForeignKey(
        Family, on_delete=models.CASCADE, related_name="shopping_lists",
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.OPEN,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        blank=True, related_name="shopping_lists_created",
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        blank=True, related_name="shopping_lists_closed",
    )

    class Meta:
        ordering = ['-created_at']
        constraints = [
            # The actual guarantee that a family can never have two open
            # lists at once, even under concurrent requests: enforced by
            # the database, not merely checked in application code.
            models.UniqueConstraint(
                fields=['family'],
                condition=models.Q(status='open'),
                name='unique_open_shopping_list_per_family',
            ),
        ]

    def __str__(self):
        return (
            f"{self.family} \u2014 {self.get_status_display()} list "
            f"({self.created_at:%Y-%m-%d})"
        )

    @classmethod
    def get_or_create_open(cls, family, user=None):
        """
        Returns the family's open list, creating one if none currently
        exists (e.g. a brand-new family, or just after closing one
        without yet starting the next). The common case (one already
        exists) never takes a lock; only the rare "none exists yet" path
        does, with the unique-open-list database constraint as the final
        backstop against a genuine race between two such lookups.
        """
        open_list = cls.objects.filter(
            family=family, status=cls.Status.OPEN,
        ).first()
        if open_list is not None:
            return open_list

        with transaction.atomic():
            locked_family = Family.objects.select_for_update().get(
                pk=family.pk,
            )
            open_list = cls.objects.filter(
                family=locked_family, status=cls.Status.OPEN,
            ).first()
            if open_list is not None:
                return open_list
            return cls.objects.create(family=locked_family, created_by=user)


class ListItem(models.Model):
    # Units of the same original request retain a common identity even
    # when their owners/states differ or they are carried to a new list.
    request_key = models.UUIDField(default=uuid.uuid4, db_index=True)
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="product_of_list_item"
    )
    date_created = models.DateTimeField(auto_now=True)
    family = models.ForeignKey(
        Family,
        on_delete=models.CASCADE,
        related_name="list_items",
    )
    # The lifecycle list this item belongs to (open or closed). "current"
    # below remains the field every existing query actually filters on
    # (unchanged, for minimal disruption); it is kept in lockstep with
    # shopping_list.status by the one place that closes a list, so
    # "current=True" and "shopping_list.status == OPEN" never disagree.
    shopping_list = models.ForeignKey(
        ShoppingList, on_delete=models.CASCADE, related_name="items",
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
    # "in_basket" is the existing active shopping-list state (an item
    # someone currently has in their basket). It is distinct from the
    # future terminal "actually purchased" concept represented below by
    # date_bought/buyer/quantity_bought/shop_bought, which remain named
    # with "bought" deliberately and are not affected by this rename.
    in_basket = models.BooleanField(default=False)
    # Describe only the CURRENT in-basket episode; both are cleared to
    # NULL when the item is taken back out. No history of past episodes
    # is kept.
    put_in_basket_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="user_who_put_item_in_basket",
        null=True,
        blank=True
    )
    time_put_in_basket = models.DateTimeField(
        default=None, blank=True, null=True,
    )
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
    # Who most recently un-cancelled this item; cleared on cancel/buy.
    restored_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name="user_who_restored_item",
        null=True,
        blank=True,
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
            models.CheckConstraint(
                condition=models.Q(quantity_required=1),
                name='list_item_is_one_unit',
            ),
            models.CheckConstraint(
                condition=models.Q(quantity_bought__in=[0, 1]),
                name='list_item_bought_at_most_one',
            ),
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

        if (
            self.family_id
            and self.shopping_list_id
            and self.shopping_list.family_id != self.family_id
        ):
            errors["shopping_list"] = (
                "The shopping list must belong to the same family as the "
                "list item."
            )

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self.shopping_list_id:
            # Every ListItem belongs to some list; callers that don't
            # care which one (the vast majority) get the family's open
            # list, auto-created if none exists yet.
            self.shopping_list = ShoppingList.get_or_create_open(
                self.family, self.creator,
            )
        if not self.preferred_shop and self.product.default_shop:
            self.preferred_shop = self.product.default_shop
        if not self.pk:
            self.clean()
        super().save(*args, **kwargs)
        if not self.slug:
            self.slug = slugify(f"{self.product.slug}-{self.id}")
            super().save(update_fields=['slug'])
