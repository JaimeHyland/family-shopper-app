import secrets

from django.utils.text import slugify


def is_adult(user):
    return user.groups.filter(name='adult').exists()


_SLUG_SUFFIX_ALPHABET = 'abcdefghijklmnopqrstuvwxyz0123456789'


def generate_unique_slug(name, exists_fn, suffix_length=4, max_attempts=100):
    """
    Slugify `name`; if that slug is already taken (per `exists_fn`), retry
    with a short random suffix appended until a free one is found. The
    suffix is random rather than sequential, so it never reveals how many
    other records share the same base slug.
    """
    base_slug = slugify(name)
    if not exists_fn(base_slug):
        return base_slug

    for _ in range(max_attempts):
        suffix = ''.join(
            secrets.choice(_SLUG_SUFFIX_ALPHABET) for _ in range(suffix_length) # noqa
        )
        candidate = f"{base_slug}-{suffix}"
        if not exists_fn(candidate):
            return candidate

    raise RuntimeError(
        f"Could not generate a unique slug for {name!r} after "
        f"{max_attempts} attempts."
    )
