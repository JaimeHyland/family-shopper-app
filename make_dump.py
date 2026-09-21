import os

os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    "family_shopping_list.settings",
)

import django

django.setup()

import contextlib
from django.core.management import call_command

with open("db_dump.json", "w", encoding="utf-8", newline="") as f:
    with contextlib.redirect_stdout(f):
        call_command(
            "dumpdata",
            natural_foreign=True,
            natural_primary=True,
            exclude=[
                "contenttypes",
                "auth.permission",
                "sessions",
                "admin.logentry",
            ],
            indent=2,
        )
        