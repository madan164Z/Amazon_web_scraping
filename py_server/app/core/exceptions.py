from builtins import Exception, str, super


class ScraperError(Exception):
    """Base class for all scraper-related failures."""


class InvalidRegionError(ScraperError):
    """Raised when the requested region is not a supported Amazon domain."""

    def __init__(self, region: str) -> None:
        self.region = region
        super().__init__(f"Unsupported region: {region!r}")


class UpstreamRequestError(ScraperError):
    """Raised when the HTTP request to Amazon fails (timeout, network"""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class ProductNotIdentifiableError(ScraperError):
    """Raised when a rank-lookup request provides no usable identifier."""

    def __init__(self) -> None:
        super().__init__(
            "At least one of 'asin', 'product_url', or 'product_name' "
            "must be provided to locate the product's rank."
        )
