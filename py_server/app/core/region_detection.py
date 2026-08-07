from builtins import ValueError, dict, str
from urllib.parse import urlparse

from typing import Literal

Region = Literal["US", "ES", "BR", "IN", "PK"]

_HOSTNAME_TO_REGION: dict[str, Region] = {
    "amazon.com": "US",
    "www.amazon.com": "US",
    "smile.amazon.com": "US",
    "amazon.es": "ES",
    "www.amazon.es": "ES",
    "amazon.com.br": "BR",
    "www.amazon.com.br": "BR",
    "amazon.in": "IN",
    "www.amazon.in": "IN",
    "amazon.pk": "PK",
    "www.amazon.pk": "PK",
}


def detect_region_from_url(product_url: str | None) -> Region | None:
    """Infers the Amazon marketplace region from a product URL's hostname.

    Args:
        product_url: A full Amazon product URL, e.g.
            "https://www.amazon.in/dp/B08N5WRWNW". May be None or
            malformed; both return None rather than raising, since this
            is a best-effort inference, not a required field.

    Returns:
        The matching Region code, or None if the URL is missing, isn't a
        parseable URL, or its hostname isn't one of the supported
        marketplaces (see `_HOSTNAME_TO_REGION`) — callers should treat
        None as "could not auto-detect, fall back to another source of
        region" rather than as an error.
    """
    if not product_url:
        return None

    try:
        hostname = urlparse(product_url).hostname
    except ValueError:
        return None

    if not hostname:
        return None

    return _HOSTNAME_TO_REGION.get(hostname.lower())
