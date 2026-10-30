import json
from functools import lru_cache
from pathlib import Path

from django import template
from django.conf import settings
from django.utils.translation import get_language

register = template.Library()

# Translation catalogs are plain "key -> string" JSON files (one per
# locale, each a complete, independent catalog - no locale is treated as
# the canonical source for the others) rather than Django's own
# gettext/.po/.mo machinery. This keeps the actual translated content in
# a format that a future React Native/Flutter frontend's own i18n system
# (e.g. react-i18next JSON namespaces, Flutter's ARB files) can reuse
# directly, or with only a trivial mechanical conversion, rather than
# being tied to a Django/gettext-specific toolchain and its
# source-language/target-language assumption.
TRANSLATIONS_DIR = Path(__file__).resolve().parent.parent / "translations"
# Derived from settings.LANGUAGES (not a second hardcoded list), so the
# two never drift apart.
SUPPORTED_LANGUAGES = tuple(code for code, _name in settings.LANGUAGES)
FALLBACK_LANGUAGE = "en"


@lru_cache(maxsize=None)
def _load_catalog(language_code):
    path = TRANSLATIONS_DIR / f"{language_code}.json"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as catalog_file:
        return json.load(catalog_file)


def _active_language():
    # get_language() reflects whatever LocaleMiddleware resolved this
    # request to (Accept-Language header, session, or the site default);
    # unsupported/unrecognised values fall back to English.
    language = (get_language() or "")[:2].lower()
    return language if language in SUPPORTED_LANGUAGES else FALLBACK_LANGUAGE


@register.simple_tag
def t(key, **kwargs):
    """Looks up `key` in the active language's JSON translation catalog,
    falling back to English and then to the raw key itself if missing -
    the same fallback chain conventionally used by key-based i18n
    frameworks. Any kwargs are interpolated with str.format(), so
    {% t "some.key" name=value %} works for simple variable
    substitution."""
    language = _active_language()
    text = _load_catalog(language).get(key)
    if text is None and language != FALLBACK_LANGUAGE:
        text = _load_catalog(FALLBACK_LANGUAGE).get(key)
    if text is None:
        text = key
    return text.format(**kwargs) if kwargs else text
