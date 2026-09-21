from django.contrib.auth import get_user_model
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import FamilyMembership
from .permissions import sync_adult_group_membership


User = get_user_model()


@receiver(post_save, sender=User)
def sync_adult_group_after_user_save(sender, instance, raw, **kwargs):
    if raw:
        return
    sync_adult_group_membership(instance)


@receiver(post_save, sender=FamilyMembership)
def sync_adult_group_after_membership_save(sender, instance, raw, **kwargs):
    if raw:
        return
    sync_adult_group_membership(instance.user)


@receiver(post_delete, sender=FamilyMembership)
def sync_adult_group_after_membership_delete(sender, instance, **kwargs):
    sync_adult_group_membership(instance.user)
    