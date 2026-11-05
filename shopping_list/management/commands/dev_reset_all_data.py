"""
Development-only utility: wipe ALL application data (every User, Family,
and everything that belongs to them) to establish a clean testing
baseline. This is an APPLICATION-DATA reset, not a schema reset -- it
never touches migration history, content types, permissions, sites,
sessions, django_summernote, or socialaccount framework tables, and it
never drops or recreates the database itself.

This is a much broader, more destructive sibling of
dev_reset_one_registration (which stays unchanged and keeps its narrow,
single-account purpose). Use that command for routine single-account
cleanup during manual testing; use this one only when you actually want
to start over completely.

Usage (PowerShell):
    python manage.py dev_reset_all_data
    python manage.py dev_reset_all_data --apply
    python manage.py dev_reset_all_data --apply --yes
"""
import os

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from allauth.account.models import EmailAddress

from shopping_list.models import (
    Category,
    Family,
    FamilyMembership,
    ListItem,
    PendingFamilyAdminRegistration,
    PendingFamilyMemberInvitation,
    PendingPasswordChange,
    Product,
    Shop,
    ShoppingList,
)

# This specific superuser account is kept intact across resets -- never
# counted in the preview or included in the deletion below.
PRESERVED_SUPERUSER_USERNAME = "JaimeHyland"


class Command(BaseCommand):
    help = (
        "Dev-only: delete ALL application data (every User, Family, and "
        "everything that belongs to them) to establish a clean testing "
        "baseline. Shows a dry-run preview by default."
    )

    def add_arguments(self, parser):
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

        apply_changes = options["apply"]

        plan = self._build_plan()
        self._report_plan(plan)

        if not apply_changes:
            self.stdout.write(self.style.NOTICE(
                "Dry run only -- no changes made. Re-run with --apply to "
                "delete.",
            ))
            return

        if plan["total_rows"] == 0:
            self.stdout.write(self.style.WARNING(
                "Nothing to delete; database is already empty of "
                "application data.",
            ))
            return

        db_name = connection.settings_dict.get("NAME") or ""
        if not options["yes"]:
            confirmation = input(
                f"Type the database name ({db_name}) to confirm wiping "
                "ALL application data: ",
            )
            if confirmation.strip() != db_name:
                raise CommandError(
                    "Confirmation did not match. Aborting without making "
                    "any changes.",
                )

        deleted_summary = self._delete_everything()
        self._report_results(deleted_summary)

    def _build_plan(self):
        """Read-only counts for the dry-run preview -- mirrors exactly
        what _delete_everything() is about to remove, computed before
        any deletion happens."""
        counts = {
            "User": User.objects.exclude(
                username=PRESERVED_SUPERUSER_USERNAME,
            ).count(),
            "  of which superusers": User.objects.exclude(
                username=PRESERVED_SUPERUSER_USERNAME,
            ).filter(is_superuser=True).count(),
            "Family": Family.objects.count(),
            "FamilyMembership": FamilyMembership.objects.count(),
            "Shop": Shop.objects.count(),
            "Category": Category.objects.count(),
            "Product": Product.objects.count(),
            "ShoppingList": ShoppingList.objects.count(),
            "ListItem": ListItem.objects.count(),
            "PendingFamilyAdminRegistration":
                PendingFamilyAdminRegistration.objects.count(),
            "PendingFamilyMemberInvitation":
                PendingFamilyMemberInvitation.objects.count(),
            "PendingPasswordChange": PendingPasswordChange.objects.count(),
            "allauth EmailAddress": EmailAddress.objects.count(),
        }
        total_rows = sum(
            v for k, v in counts.items() if not k.startswith("  ")
        )
        return {"counts": counts, "total_rows": total_rows}

    def _report_plan(self, plan):
        self.stdout.write("Rows that would be deleted:")
        for label, count in plan["counts"].items():
            self.stdout.write(f"  {label}: {count}")
        self.stdout.write(
            "\nPreserved unconditionally (never touched by this "
            "command): django_migrations, content types, permissions, "
            "sites, sessions, django_summernote and socialaccount "
            "framework tables, and all file-based seed-list JSON data.",
        )

    def _delete_everything(self):
        """
        Deletion order matters only in that Family must be deleted
        before/alongside User for the cascade graph below to be
        complete in one pass; PendingFamilyAdminRegistration has no FK
        to either and can be removed independently. Family.delete()
        CASCADEs FamilyMembership, PendingFamilyMemberInvitation, Shop,
        Category, Product, ShoppingList, and ListItem (all
        on_delete=CASCADE from Family -- confirmed by inspecting every
        model field referencing Family). User.delete() CASCADEs
        PendingPasswordChange, allauth EmailAddress (and
        EmailConfirmation transitively), socialaccount.SocialAccount,
        admin.LogEntry, and FamilyMembership/ListItem rows (if any
        remained -- they won't, since the Family cascade above already
        removes every row that could reference a surviving family).
        PRESERVED_SUPERUSER_USERNAME is excluded from the User deletion
        (it has no FamilyMembership, so the Family cascade above cannot
        touch it either). Bulk QuerySet.delete() does not invoke
        FamilyMembership's overridden delete()/save() (e.g. the "last
        admin" check); that is correct and expected here, since nothing
        survives to protect.
        """
        with transaction.atomic():
            pending_count, _ = (
                PendingFamilyAdminRegistration.objects.all().delete()
            )
            _, family_cascade = Family.objects.all().delete()
            _, user_cascade = User.objects.exclude(
                username=PRESERVED_SUPERUSER_USERNAME,
            ).delete()

        return {
            "PendingFamilyAdminRegistration": pending_count,
            **family_cascade,
            **user_cascade,
        }

    def _report_results(self, deleted_summary):
        self.stdout.write(self.style.SUCCESS("Deleted:"))
        for label, count in sorted(deleted_summary.items()):
            if count:
                self.stdout.write(f"  {label}: {count}")
        self.stdout.write(self.style.SUCCESS(
            "Application data reset complete. Migration history, "
            "content types, permissions, sites, sessions, and "
            "file-based seed-list data were not touched.",
        ))

    def _ensure_dev_environment(self):
        """
        Refuses to run unless several independent signals all agree this
        is a local development environment. No single signal (e.g. just
        the command's name, or DEBUG alone) is trusted on its own.
        Deliberately duplicated from dev_reset_one_registration.py
        (rather than factored into a shared helper) so that command can
        be left completely unchanged, per the explicit requirement that
        it stay untouched.
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
