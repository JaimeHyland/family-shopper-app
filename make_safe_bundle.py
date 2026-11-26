#!/usr/bin/env python3

"""
Create a sanitized ZIP of a Django/Flutter/etc. project for code review.

Usage:
    python make_safe_bundle.py

Output:
    family-shopping-list-safe.zip

IMPORTANT:
Known private files are excluded. Suspicious JSON or text that cannot be
confidently redacted is withheld and listed in SAFE_BUNDLE_REPORT.txt.
The ZIP is checked before publication, but review it before sharing:
heuristics cannot reliably identify unlabelled or encoded private data.
"""

from __future__ import annotations

import ast
import json
import re
import tempfile
import zipfile
from collections import Counter
from pathlib import Path


PROJECT_ROOT = Path.cwd().resolve()
OUTPUT_ZIP = PROJECT_ROOT / "family-shopping-list-safe.zip"
REPORT_NAME = "SAFE_BUNDLE_REPORT.txt"

EXCLUDED_PATHS = {
    "data/fixtures.json": "database fixture with account and session data",
    "assets/documentation/getting to know the potential user": "private user research",
    "assets/documentation/gettting_to_know_the_user.md": "private user research",
}

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

REDACTED = "[REDACTED]"
SENSITIVE_FIELDS = (
    r"(?:[a-z0-9_-]+[_-])?(?:password|passwd|password[_-]?hash|secret|secret[_-]?key|"
    r"api[_-]?(?:key|secret)|(?:access|refresh|auth|api|session|id)[_-]?token|"
    r"client[_-]?secret|private[_-]?key|(?:secret[_-]?)?access[_-]?key(?:[_-]?id)?|"
    r"session[_-]?(?:id|key|data)|sessionid|authorization|"
    r"(?:first|last|full)[_-]?name|phone(?:[_-]?number)?|"
    r"(?:home|street|postal|mailing)[_-]?address|date[_-]?of[_-]?birth)"
)
FIELD_PREFIX = rf"""(?<![\w-])(?P<key_quote>["']?){SENSITIVE_FIELDS}(?P=key_quote)[ \t]*[:=][ \t]*"""
QUOTED_FIELD = re.compile(
    FIELD_PREFIX + r"""(?P<quote>["'])(?P<value>(?:\\[^\r\n]|(?!(?P=quote))[^\\\r\n])*)(?P=quote)(?!(?P=quote))""",
    re.IGNORECASE,
)
CONFIG_FIELD = re.compile(
    rf"(?:^[ \t]*(?:-[ \t]+)?|(?<=[{{,])[ \t]*){FIELD_PREFIX}(?P<value>[^\r\n]*)",
    re.IGNORECASE | re.MULTILINE,
)
COMPLEX_LITERAL = re.compile(
    FIELD_PREFIX + r"""(?:\([ \t\r\n]*|[ \t]*\r?\n[ \t]+)?(?:[rubf]{0,2}["']{3}|[rubf]{1,2}["']|`)""",
    re.IGNORECASE,
)
CONFIG_SUFFIXES = {".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".properties"}
JSON_SUFFIXES = {".json", ".webmanifest"}
JSON_LINES_SUFFIXES = {".jsonl", ".ndjson"}
JSON_EXTRA_FIELDS = re.compile(r"token|cookie|email(?:[_-]?address)?|username", re.IGNORECASE)
PRIVATE_MODELS = {"auth.user", "sessions.session", "admin.logentry", "account.emailaddress"}

EMAIL = re.compile(r"(?<![\w.%+-])[\w.%+-]+@((?:[a-z0-9-]+\.)+[a-z]{2,})\b", re.IGNORECASE)
EXAMPLE_DOMAINS = {"example.com", "example.net", "example.org"}
CREDENTIAL_URL = re.compile(r"""(?i)(\b[a-z][a-z0-9+.-]*://)[^:/@\s"'<>]+:[^/@\s"'<>]+@""")
AUTH_HEADER = re.compile(r"""(?i)(\bauthorization["']?\s*[:=]\s*["']?(?:bearer|basic)\s+)[a-z0-9._~+/=-]+""")
TOKEN = re.compile(
    r"\b(?:sk-[\w-]{16,}|(?:sk|rk)_(?:live|test)_[a-zA-Z0-9]{16,}|"
    r"gh[pousr]_[a-zA-Z0-9]{20,}|github_pat_[\w]{20,}|(?:AKIA|ASIA)[A-Z0-9]{16}|"
    r"xox[baprs]-[\w-]{12,}|AIza[\w-]{35}|eyJ[\w-]+\.[\w-]+\.[\w-]+)\b"
)
RESIDUAL_PATTERNS = {
    "private key material": re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    "password hash": re.compile(
        r"(?i)\b(?:pbkdf2_sha\d+|argon2(?:id|i|d)?|bcrypt(?:_sha256)?|scrypt)\$[\w$./+=-]{12,}"
        r"|\$(?:2[abxy]|argon2(?:id|i|d)?|[156])\$[\w$./+=,-]{12,}"
    ),
    "session data": re.compile(
        r"""(?i)\bsessionid\s*=\s*[a-z0-9_-]{16,}|\.?[a-z0-9_-]{16,}:[a-z0-9]{6,12}:[a-z0-9_-]{32,}"""
    ),
    "credentials in URL parameters": re.compile(
        r"(?i)[?&](?:access_token|auth_token|api_key|password|token)=[a-z0-9_%+./=-]{8,}"
    ),
    "phone number": re.compile(r"(?<![\w+])\+\d{1,3}[ .-]+(?:\(?\d{2,5}\)?[ .-]+){1,3}\d{2,5}\b"),
}


def is_example_email(match: re.Match[str]) -> bool:
    domain = match[1].lower()
    return domain in EXAMPLE_DOMAINS or domain.endswith((".test", ".example", ".invalid"))


def signature_finding(text: str) -> str:
    for reason, pattern in RESIDUAL_PATTERNS.items():
        if pattern.search(text):
            return reason
    if TOKEN.search(text) or CREDENTIAL_URL.search(text) or AUTH_HEADER.search(text):
        return "credential signature"
    if any(not is_example_email(match) for match in EMAIL.finditer(text)):
        return "non-example email address"
    return ""


def unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def json_finding(text: str) -> str:
    try:
        pending = [json.loads(text, object_pairs_hook=unique_json_object)]
    except (ValueError, RecursionError):
        return "invalid or ambiguous JSON"

    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            model = value.get("model")
            if isinstance(model, str) and model.lower() in PRIVATE_MODELS:
                return "Django account, session or audit record"
            for key, item in value.items():
                if item not in (None, "", REDACTED, [], {}):
                    if re.fullmatch(SENSITIVE_FIELDS, key, re.IGNORECASE) or JSON_EXTRA_FIELDS.fullmatch(key):
                        return "credential or personal-data field in JSON"
                pending.extend((key, item))
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, str):
            finding = signature_finding(value)
            if finding:
                return finding
    return ""


def safety_finding(path: Path, text: str) -> str:
    suffix = path.suffix.lower()
    finding = signature_finding(text)
    if finding:
        return finding
    if suffix in JSON_SUFFIXES:
        return json_finding(text)
    if suffix in JSON_LINES_SUFFIXES:
        return next((finding for line in text.splitlines() if line.strip()
                     and (finding := json_finding(line))), "")
    if any(match["value"] not in ("", REDACTED) for match in QUOTED_FIELD.finditer(text)):
        return "literal credential or personal-data field"
    if COMPLEX_LITERAL.search(text):
        return "complex literal in sensitive field"
    if suffix in CONFIG_SUFFIXES:
        for match in CONFIG_FIELD.finditer(text):
            if not re.fullmatch(r"""(?:["']\[REDACTED\]["']|["']{2})?[ \t]*(?:[#;].*)?""", match["value"]):
                return "unverified value in sensitive config field"
    return ""


def is_excluded(path: Path) -> tuple[bool, str]:
    """Return (excluded?, reason)."""

    relative = path.relative_to(PROJECT_ROOT)
    relative_key = relative.as_posix().lower()
    if relative_key in EXCLUDED_PATHS:
        return True, EXCLUDED_PATHS[relative_key]

    if path.is_symlink() or not path.resolve().is_relative_to(PROJECT_ROOT):
        return True, "symlink or path outside project"

    if relative_key == REPORT_NAME.lower():
        return True, "reserved generated report name"

    if any(part.lower() in EXCLUDED_DIRS for part in relative.parts[:-1]):
        return True, "excluded directory"

    if path.name.lower() in EXCLUDED_FILENAMES:
        return True, "sensitive filename"

    # Catch .env.* generically.
    if path.name.lower().startswith(".env."):
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


def redact_text(text: str) -> str:
    def redact_literal(match: re.Match[str]) -> str:
        if match["value"] in ("", REDACTED):
            return match[0]
        start, end = match.span("value")
        return match[0][:start - match.start()] + REDACTED + match[0][end - match.start():]

    text = QUOTED_FIELD.sub(redact_literal, text)
    text = CREDENTIAL_URL.sub(r"\1", text)
    text = AUTH_HEADER.sub(r"\1" + REDACTED, text)
    text = TOKEN.sub(REDACTED, text)
    return EMAIL.sub(
        lambda match: match[0] if is_example_email(match) else "redacted@example.invalid",
        text,
    )


def verify_zip(candidate: Path) -> None:
    with zipfile.ZipFile(candidate) as archive:
        for entry in archive.infolist():
            path = Path(entry.filename)
            # Reading every member also verifies its CRC.
            if signature_finding(entry.filename) or safety_finding(
                path, archive.read(entry).decode("utf-8")
            ):
                raise SystemExit("Stopped: a ZIP member failed the final safety check. No ZIP published.")


def main() -> None:
    # Do not leave an older, potentially unsafe bundle behind if this run fails.
    OUTPUT_ZIP.unlink(missing_ok=True)

    included: list[str] = []
    excluded: Counter[str] = Counter()
    withheld: list[tuple[str, str]] = []
    redacted: list[str] = []

    with tempfile.TemporaryDirectory(prefix="safe-project-bundle-") as temp:
        safe_root = Path(temp) / PROJECT_ROOT.name
        safe_root.mkdir(parents=True)

        for path in PROJECT_ROOT.rglob("*"):
            if not path.is_file():
                continue

            if path.resolve() == OUTPUT_ZIP.resolve():
                continue

            relative = path.relative_to(PROJECT_ROOT)
            name = relative.as_posix()

            skip, reason = is_excluded(path)
            if skip:
                excluded[reason] += 1
                continue

            if signature_finding(name):
                excluded["suspicious filename (not recorded)"] += 1
                continue

            try:
                raw = path.read_bytes()
            except OSError:
                excluded["unreadable file"] += 1
                continue

            if looks_binary(raw):
                excluded["binary file"] += 1
                continue

            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                excluded["non-UTF-8 file"] += 1
                continue

            finding = signature_finding(text)
            if finding in RESIDUAL_PATTERNS:
                sanitized = text
            else:
                is_json = path.suffix.lower() in JSON_SUFFIXES | JSON_LINES_SUFFIXES
                sanitized = text if is_json else redact_text(text)
                finding = safety_finding(relative, sanitized)

            if not finding and sanitized != text and path.suffix.lower() == ".py":
                try:
                    ast.parse(sanitized)
                except SyntaxError:
                    finding = "Python syntax could not be preserved during redaction"

            if finding:
                excluded["withheld by safety scan"] += 1
                withheld.append((name, finding))
                continue

            destination = safe_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(sanitized.encode("utf-8"))

            included.append(name)

            if sanitized != text:
                redacted.append(name)

        report = safe_root / REPORT_NAME

        report_lines = [
            "SAFE PROJECT BUNDLE REPORT",
            "=" * 70,
            "",
            "IMPORTANT:",
            "This bundle was automatically sanitized. Automated detection",
            "cannot guarantee that every possible credential or private value",
            "was found. Review this report and, ideally, inspect the ZIP before",
            "sharing it.",
            "",
            f"Files included: {len(included)}",
            f"Files containing automatic redactions: {len(redacted)}",
            f"Files excluded: {sum(excluded.values())}",
            "",
            "FILES WITH REDACTIONS",
            "-" * 70,
        ]

        report_lines.extend(sorted(redacted) or ["(none detected)"])
        report_lines += [
            "",
            "EXCLUSION COUNTS BY REASON",
            "-" * 70,
        ]
        report_lines.extend(f"{count}: {reason}" for reason, count in sorted(excluded.items()))
        report_lines += ["", "EXPLICIT PROJECT EXCLUSIONS (IF PRESENT)", "-" * 70]
        report_lines.extend(f"{name} -- {reason}" for name, reason in sorted(EXCLUDED_PATHS.items()))
        report_lines += ["", "FILES WITHHELD BY THE SAFETY SCAN", "-" * 70]
        report_lines.extend([f"{name} -- {reason}" for name, reason in sorted(withheld)] or ["(none)"])

        report_lines += [
            "",
            "INCLUDED FILES",
            "-" * 70,
        ]

        report_lines.extend(sorted(included))

        report.write_text("\n".join(report_lines) + "\n", encoding="utf-8")

        with tempfile.TemporaryDirectory(prefix=".safe-bundle-", dir=PROJECT_ROOT) as staging:
            candidate = Path(staging) / OUTPUT_ZIP.name
            with zipfile.ZipFile(candidate, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                for path in sorted(safe_root.rglob("*")):
                    if path.is_file():
                        archive.write(path, arcname=path.relative_to(safe_root.parent))
            verify_zip(candidate)
            candidate.replace(OUTPUT_ZIP)

    print()
    print("Safe project bundle created:")
    print(f"  {OUTPUT_ZIP}")
    print()
    print(f"Included files: {len(included)}")
    print(f"Files redacted: {len(redacted)}")
    print(f"Excluded files: {sum(excluded.values())} ({len(withheld)} withheld by the safety scan)")
    print()
    print("Before sharing it:")
    print("  1. Open the ZIP.")
    print("  2. Read SAFE_BUNDLE_REPORT.txt.")
    print("  3. Check settings/configuration files for anything private.")
    print("  4. Upload the ZIP here when you're satisfied.")
    print()


if __name__ == "__main__":
    main()
