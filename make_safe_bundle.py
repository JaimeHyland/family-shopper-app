#!/usr/bin/env python3

"""
Create a sanitized ZIP of a Django/Flutter/etc. project for code review.

Usage:
    python make_safe_bundle.py

Output:
    family-shopping-list-safe.zip

IMPORTANT:
This script is deliberately conservative, but no automated secret scanner
can guarantee that it will catch every possible secret. Review the generated
SAFE_BUNDLE_REPORT.txt before sharing the ZIP.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path


PROJECT_ROOT = Path.cwd().resolve()
OUTPUT_ZIP = PROJECT_ROOT / "family-shopping-list-safe.zip"

# Entire directories that should never be included.
EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    "node_modules",
    "build",
    "dist",
    ".dart_tool",
    ".flutter-plugins",
    ".flutter-plugins-dependencies",
    "coverage",
    "htmlcov",
    "media",
    "uploads",
    "staticfiles",
}

# Exact filenames that are normally sensitive or unnecessary.
EXCLUDED_FILENAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    ".env.test",
    ".DS_Store",
    "db.sqlite3",
    "credentials.json",
    "service-account.json",
    "service_account.json",
    "google-services.json",
    "GoogleService-Info.plist",
    "firebase-adminsdk.json",
    "id_rsa",
    "id_ed25519",
}

# Extensions that commonly contain secrets, databases, certificates,
# private keys, or large generated data.
EXCLUDED_SUFFIXES = {
    ".sqlite",
    ".sqlite3",
    ".db",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".crt",
    ".cer",
    ".der",
    ".dump",
    ".bak",
    ".backup",
    ".log",
    ".pyc",
    ".pyo",
}

# Binary / large asset types that usually aren't necessary for code review.
# Add/remove entries if your project genuinely needs particular assets.
BINARY_ASSET_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".ico",
    ".mp3",
    ".mp4",
    ".mov",
    ".avi",
    ".zip",
    ".tar",
    ".gz",
    ".7z",
    ".pdf",
}

# Individual files larger than this will be skipped.
MAX_FILE_SIZE = 2 * 1024 * 1024  # 2 MB


# ---------------------------------------------------------------------------
# Redaction rules
# ---------------------------------------------------------------------------

REDACTION_PATTERNS = [
    # Django/Python assignments such as:
    # SECRET_KEY = "..."
    # DATABASE_PASSWORD = '...'
    (
        re.compile(
            r"""(?im)^(\s*
            (?:
                SECRET_KEY|
                API_KEY|
                API_SECRET|
                ACCESS_TOKEN|
                REFRESH_TOKEN|
                AUTH_TOKEN|
                PASSWORD|
                PASSWD|
                DATABASE_PASSWORD|
                DB_PASSWORD|
                EMAIL_HOST_PASSWORD|
                AWS_SECRET_ACCESS_KEY|
                AWS_ACCESS_KEY_ID|
                STRIPE_SECRET_KEY|
                OPENAI_API_KEY|
                PRIVATE_KEY
            )
            \s*=\s*)
            (["']).*?\2
            """,
            re.VERBOSE,
        ),
        r'\1"[REDACTED]"',
    ),

    # JSON/YAML-ish values:
    # "password": "secret"
    # token: secret
    (
        re.compile(
            r"""(?im)^(\s*["']?
            (?:
                secret_key|
                api_key|
                api_secret|
                access_token|
                refresh_token|
                auth_token|
                password|
                passwd|
                db_password|
                database_password|
                private_key|
                client_secret
            )
            ["']?\s*[:=]\s*)
            (["']?)[^\s"',}\]]+\2
            """,
            re.VERBOSE,
        ),
        r'\1"[REDACTED]"',
    ),

    # Authorization: Bearer ey....
    (
        re.compile(
            r"(?i)(Authorization\s*[:=]\s*[\"']?Bearer\s+)[A-Za-z0-9._~+/=-]+"
        ),
        r"\1[REDACTED]",
    ),

    # Database URLs containing username/password.
    # postgres://user:password@host/db
    (
        re.compile(
            r"(?i)\b(postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?):"
            r"//([^:\s/@]+):([^@\s/]+)@"
        ),
        r"\1://\2:[REDACTED]@",
    ),

    # Common API-token forms.
    (
        re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
        "[REDACTED-API-KEY]",
    ),

    (
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
        "[REDACTED-GITHUB-TOKEN]",
    ),

    # AWS Access Key IDs
    (
        re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
        "[REDACTED-AWS-KEY]",
    ),

    # PEM private keys.
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
            r"-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[REDACTED PRIVATE KEY]",
    ),
]


def is_excluded(path: Path) -> tuple[bool, str]:
    """Return (excluded?, reason)."""

    relative = path.relative_to(PROJECT_ROOT)

    if any(part in EXCLUDED_DIRS for part in relative.parts[:-1]):
        return True, "excluded directory"

    if path.name in EXCLUDED_FILENAMES:
        return True, "sensitive filename"

    # Catch .env.* generically.
    if path.name == ".env" or path.name.startswith(".env."):
        return True, "environment file"

    suffix = path.suffix.lower()

    if suffix in EXCLUDED_SUFFIXES:
        return True, f"sensitive/generated extension {suffix}"

    if suffix in BINARY_ASSET_SUFFIXES:
        return True, f"binary asset {suffix}"

    try:
        if path.stat().st_size > MAX_FILE_SIZE:
            return True, "file larger than 2 MB"
    except OSError:
        return True, "could not stat file"

    return False, ""


def looks_binary(data: bytes) -> bool:
    """A small binary-file heuristic."""
    if b"\x00" in data:
        return True

    sample = data[:8192]

    if not sample:
        return False

    # Treat mostly printable UTF-8-ish files as text.
    try:
        sample.decode("utf-8")
        return False
    except UnicodeDecodeError:
        return True


def redact_text(text: str) -> tuple[str, int]:
    """Apply secret-redaction patterns."""
    changes = 0

    for pattern, replacement in REDACTION_PATTERNS:
        text, count = pattern.subn(replacement, text)
        changes += count

    return text, changes


def main() -> None:
    if OUTPUT_ZIP.exists():
        OUTPUT_ZIP.unlink()

    included: list[str] = []
    excluded: list[tuple[str, str]] = []
    redacted: list[tuple[str, int]] = []

    with tempfile.TemporaryDirectory(prefix="safe-project-bundle-") as temp:
        safe_root = Path(temp) / PROJECT_ROOT.name
        safe_root.mkdir(parents=True)

        for path in PROJECT_ROOT.rglob("*"):
            if not path.is_file():
                continue

            if path.resolve() == OUTPUT_ZIP.resolve():
                continue

            relative = path.relative_to(PROJECT_ROOT)

            skip, reason = is_excluded(path)
            if skip:
                excluded.append((str(relative), reason))
                continue

            try:
                raw = path.read_bytes()
            except (OSError, PermissionError) as exc:
                excluded.append((str(relative), f"unreadable: {exc}"))
                continue

            if looks_binary(raw):
                excluded.append((str(relative), "binary file"))
                continue

            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                excluded.append((str(relative), "non-UTF-8 file"))
                continue

            sanitized, count = redact_text(text)

            destination = safe_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(sanitized, encoding="utf-8")

            included.append(str(relative))

            if count:
                redacted.append((str(relative), count))

        report = safe_root / "SAFE_BUNDLE_REPORT.txt"

        report_lines = [
            "SAFE PROJECT BUNDLE REPORT",
            "=" * 70,
            "",
            f"Original project directory: {PROJECT_ROOT}",
            "",
            "IMPORTANT:",
            "This bundle was automatically sanitized. Automated detection",
            "cannot guarantee that every possible credential or private value",
            "was found. Review this report and, ideally, inspect the ZIP before",
            "sharing it.",
            "",
            f"Files included: {len(included)}",
            f"Files containing automatic redactions: {len(redacted)}",
            f"Files excluded: {len(excluded)}",
            "",
            "FILES WITH REDACTIONS",
            "-" * 70,
        ]

        if redacted:
            for filename, count in sorted(redacted):
                report_lines.append(f"{filename}  ({count} replacement(s))")
        else:
            report_lines.append("(none detected)")

        report_lines += [
            "",
            "EXCLUDED FILES",
            "-" * 70,
        ]

        if excluded:
            for filename, reason in sorted(excluded):
                report_lines.append(f"{filename}  -- {reason}")
        else:
            report_lines.append("(none)")

        report_lines += [
            "",
            "INCLUDED FILES",
            "-" * 70,
        ]

        report_lines.extend(sorted(included))

        report.write_text("\n".join(report_lines) + "\n", encoding="utf-8")

        with zipfile.ZipFile(
            OUTPUT_ZIP,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for path in safe_root.rglob("*"):
                if path.is_file():
                    archive.write(
                        path,
                        arcname=path.relative_to(safe_root.parent),
                    )

    print()
    print("Safe project bundle created:")
    print(f"  {OUTPUT_ZIP}")
    print()
    print(f"Included files: {len(included)}")
    print(f"Files redacted: {len(redacted)}")
    print(f"Excluded files: {len(excluded)}")
    print()
    print("Before sharing it:")
    print("  1. Open the ZIP.")
    print("  2. Read SAFE_BUNDLE_REPORT.txt.")
    print("  3. Check settings/configuration files for anything private.")
    print("  4. Upload the ZIP here when you're satisfied.")
    print()


if __name__ == "__main__":
    main()
