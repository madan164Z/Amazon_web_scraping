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
    """Infers the Amazon marketplace region from a product URL's hostname."""
    if not product_url:
        return None

    try:
        hostname = urlparse(product_url).hostname
    except ValueError:
        return None

    if not hostname:
        return None

    return _HOSTNAME_TO_REGION.get(hostname.lower())
