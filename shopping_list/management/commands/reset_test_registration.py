"""
Development-only utility: remove a single test Family-admin
registration -- pending or completed -- by email address, so the same
address can be reused for a fresh manual-testing registration.

This is NOT a general-purpose account-deletion feature. It only
recognises the narrow, unambiguous shape of a single test
administrator who is the sole member of their own exclusively-owned
family, with no other family memberships anywhere; anything broader
(shared families, multiple memberships, ambiguous email ownership) is
refused rather than guessed at.

Usage (PowerShell):
    python manage.py reset_test_registration --email test@example.com
    python manage.py reset_test_registration --email test@example.com --apply
    python manage.py reset_test_registration --email test@example.com --apply --yes
"""
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import Q

from allauth.account.models import EmailAddress

from shopping_list.models import (
    FamilyMembership,
    ListItem,
    PendingFamilyAdminRegistration,
)
from shopping_list.registration import (
    AmbiguousEmailOwnershipError,
    find_existing_user_for_email,
    normalize_email,
)


class Command(BaseCommand):
    help = (
        "Dev-only: remove a test Family admin's pending or completed "
        "registration (and exclusively-owned family data) by email, so "
        "the address can be reused. Shows a dry-run preview by default."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--email", required=True,
            help="Email address of the test registration to remove.",
        )
        parser.add_argument(
            "--apply", action="store_true",
            help=(
                "Actually delete. Without this flag, only a preview is "
                "shown and nothing is changed."
            ),
        )
        parser.add_argument(
            "--yes", action="store_true",
            help=(
                "Skip the interactive confirmation prompt (for scripted/ "
                "automated use, e.g. from tests)."
            ),
        )

    def handle(self, *args, **options):
        self._ensure_dev_environment()

        email = normalize_email(options["email"])
        apply_changes = options["apply"]

        pending_qs = PendingFamilyAdminRegistration.objects.filter(
            email=email,
        )

        try:
            user = find_existing_user_for_email(email)
        except AmbiguousEmailOwnershipError as exc:
            raise CommandError(
                f"Refusing to proceed: {exc} Aborting without making any "
                "changes.",
            )

        if user is None and not pending_qs.exists():
            self.stdout.write(self.style.WARNING(
                f"No registration record or account found for {email!r}; "
                "nothing to do.",
            ))
            return

        family = None
        if user is not None:
            family = self._resolve_exclusive_family(user)

        self._report_plan(email, user, family, pending_qs)

        if not apply_changes:
            self.stdout.write(self.style.NOTICE(
                "Dry run only -- no changes made. Re-run with --apply to "
                "delete.",
            ))
            return

        if not options["yes"]:
            confirmation = input(
                f"Type the email address ({email}) to confirm deletion: ",
            )
            if confirmation.strip().lower() != email:
                raise CommandError(
                    "Confirmation did not match. Aborting without making "
                    "any changes.",
                )

        pending_count = pending_qs.count()
        family_name = family.name if family is not None else None
        username = user.username if user is not None else None

        with transaction.atomic():
            pending_qs.delete()
            if family is not None:
                family.delete()
            if user is not None:
                user.delete()

        summary = [f"Removed {pending_count} registration record(s)"]
        if family_name is not None:
            summary.append(f"family {family_name!r} and its data")
        if username is not None:
            summary.append(f"user {username!r}")
        self.stdout.write(self.style.SUCCESS(
            ", ".join(summary) + f" for {email}.",
        ))

    def _resolve_exclusive_family(self, user):
        """
        Returns the single Family this test admin exclusively owns, or
        raises CommandError if the situation is anything other than
        that one unambiguous, safe-to-delete shape.
        """
        memberships = list(FamilyMembership.objects.filter(user=user))

        if len(memberships) == 0:
            raise CommandError(
                f"{user.username!r} has no family membership; this "
                "command only supports a single test administrator and "
                "will not guess what to do here. Aborting without making "
                "any changes.",
            )
        if len(memberships) > 1:
            raise CommandError(
                f"{user.username!r} belongs to {len(memberships)} "
                "families; this command only supports removing a test "
                "administrator who belongs to exactly one family (to "
                "avoid cascading deletes into other families' data). "
                "Aborting without making any changes.",
            )

        membership = memberships[0]
        if membership.role != FamilyMembership.Role.ADMIN:
            raise CommandError(
                f"{user.username!r} is a member (not the admin) of "
                f"{membership.family.name!r}; this command only supports "
                "removing a test admin account. Aborting without making "
                "any changes.",
            )

        family = membership.family
        other_members = family.memberships.exclude(pk=membership.pk)
        if other_members.exists():
            names = ", ".join(
                m.display_name for m in other_members
            )
            raise CommandError(
                f"Family {family.name!r} has other member(s) ({names}); "
                "this command will not delete data shared with other "
                "users. Aborting without making any changes.",
            )

        # Belt-and-braces: deleting this user CASCADEs ListItem rows
        # where they are creator/put_in_basket_by/buyer/cancelled_by/
        # restored_by (see shopping_list/models.py). The single-
        # membership check above should already make it impossible for
        # this user to be referenced from another family's list items,
        # but this is verified explicitly rather than assumed, since
        # that CASCADE would otherwise silently destroy another
        # family's data.
        stray_items = ListItem.objects.filter(
            Q(creator=user)
            | Q(put_in_basket_by=user)
            | Q(buyer=user)
            | Q(cancelled_by=user)
            | Q(restored_by=user)
        ).exclude(family=family)
        if stray_items.exists():
            raise CommandError(
                f"{user.username!r} is referenced from "
                f"{stray_items.count()} list item(s) belonging to other "
                "families; this command will not risk deleting shared "
                "data. Aborting without making any changes.",
            )

        return family

    def _report_plan(self, email, user, family, pending_qs):
        self.stdout.write(f"Target email: {email}")

        pending = list(pending_qs)
        if pending:
            self.stdout.write(f"Registration records to delete ({len(pending)}):") # noqa
            for row in pending:
                state = (
                    "consumed" if row.consumed_at else "active"
                )
                self.stdout.write(
                    f"  - id={row.pk} family_name={row.family_name!r} "
                    f"created_at={row.created_at} state={state}",
                )
        else:
            self.stdout.write("Registration records to delete: none")

        if user is None:
            self.stdout.write("Account: none (nothing completed yet)")
            return

        email_rows = EmailAddress.objects.filter(user=user).count()
        self.stdout.write(
            f"Account: username={user.username!r} (id={user.pk}), "
            f"{email_rows} allauth EmailAddress row(s)",
        )
        self.stdout.write(
            f"Family: {family.name!r} (id={family.pk}, slug={family.slug})"
            " -- exclusively owned by this test admin",
        )
        self.stdout.write(
            "Dependent records that will be deleted with the family: "
            f"{family.shops.count()} shop(s), "
            f"{family.categories.count()} categor(y/ies), "
            f"{family.products.count()} product(s), "
            f"{family.shopping_lists.count()} shopping list(s), "
            f"{family.list_items.count()} list item(s)",
        )
        self.stdout.write(
            "Everything else (other users, families, and their data) "
            "will be left untouched.",
        )

    def _ensure_dev_environment(self):
        """
        Refuses to run unless several independent signals all agree this
        is a local development environment. No single signal (e.g. just
        the command's name, or DEBUG alone) is trusted on its own.
        """
        # Django's test runner forces settings.DEBUG = False for the
        # duration of the run (regardless of the real DEBUG setting), so
        # the DEBUG check alone would also refuse to run under `manage.py
        # test` in a genuine dev environment. RUNNING_TESTS (set in
        # settings.py, already used there for the same reason) covers
        # that case without weakening the production check: a real
        # deployment never runs with "test" in sys.argv.
        running_tests = getattr(settings, "RUNNING_TESTS", False)
        if not (settings.DEBUG or running_tests):
            raise CommandError(
                "Refusing to run: DEBUG is not enabled. This command is "
                "for local development use only.",
            )

        if os.environ.get("DYNO"):
            raise CommandError(
                "Refusing to run: a Heroku DYNO environment variable is "
                "set, indicating this is a deployed dyno, not a local "
                "development environment.",
            )

        db = connection.settings_dict
        db_name = db.get("NAME") or ""
        db_host = db.get("HOST") or ""

        if not db_name.endswith("_dev"):
            raise CommandError(
                f"Refusing to run: database name {db_name!r} doesn't "
                "look like a local development database (expected a "
                "name ending in '_dev'). Aborting without making any "
                "changes.",
            )
        if db_host not in ("", "127.0.0.1", "localhost"):
            raise CommandError(
                f"Refusing to run: database host {db_host!r} doesn't "
                "look like a local development database. Aborting "
                "without making any changes.",
            )
