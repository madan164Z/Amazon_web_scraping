from typing import Literal
import re

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.region_detection import detect_region_from_url

Region = Literal["US", "ES", "BR", "IN", "PK"]

MatchMethod = Literal["asin", "url", "name", "none"]

_ASIN_FORMAT = r"^[A-Z0-9]{10}$"

class Product(BaseModel):
    """A single scraped Amazon search result."""

    title: str | None = None
    price: float | None = None
    ratingStars: float | None = Field(default=None)
    imageUrl: str | None = None
    productUrl: str | None = None
    asin: str | None = Field(
        default=None,
        description="10-character Amazon product identifier, extracted from productUrl.",
    )
    region: Region
    isSponsored: bool = Field(
        default=False,
        description=(
            "True if this result was rendered as a Sponsored placement "
            "in Amazon's search grid rather than an organic result."
        ),
    )


class ResultsInfo(BaseModel):
    """Amazon's own result-count summary for a keyword, parsed from the
    search page's info bar, e.g. "289-306 of over 90,000 results for
    'dog bed'"."""

    rangeStart: int | None = Field(
        default=None, description="First result index shown on this page, e.g. 289."
    )
    rangeEnd: int | None = Field(
        default=None, description="Last result index shown on this page, e.g. 306."
    )
    total: int | None = Field(
        default=None,
        description="Total result count for the keyword across all pages, e.g. 90000.",
    )
    totalIsEstimate: bool = Field(
        default=False,
        description="True if Amazon prefixed the total with 'over' (an estimate rather than an exact count).",
    )
    raw: str | None = Field(
        default=None,
        description="The unparsed text as Amazon rendered it, e.g. '289-306 of over 90,000 results for \"dog bed\"'.",
    )


class ScrapeResponse(BaseModel):
    """Full response body for GET /api/scrape."""

    products: list[Product]
    currentPage: int
    totalPages: int
    resultsInfo: ResultsInfo | None = Field(
        default=None,
        description="Amazon's total-results summary for this keyword, if it could be parsed from the page.",
    )


class ErrorResponse(BaseModel):
    """Error body shape, matching the original `{ error, details }` JSON."""

    error: str
    details: str | None = None


class RankRequest(BaseModel):
    """Request body for POST /api/rank — find a specific product's SERP
    position for a given search keyword.

    `region` is optional: when omitted, it's inferred from `product_url`'s
    domain (amazon.in -> IN, amazon.com.br -> BR, etc.) so a caller who
    only has a product URL never needs to separately figure out and pass
    the correct region — the URL already encodes it. `region` is only
    required as an explicit input when no `product_url` is given (i.e.
    identifying purely by `asin` or `product_name`, neither of which
    carries region information), or when targeting "PK", which has no
    dedicated Amazon domain to detect from (see `detect_region_from_url`).
    """

    keyword: str = Field(..., min_length=1, description="Search term, e.g. 'backpack'")
    region: Region | None = Field(
        default=None,
        description=(
            "Amazon marketplace region. Optional if `product_url` is given "
            "— the region is inferred from the URL's domain. Required "
            "(and defaults to 'US' if still unresolved) when identifying "
            "by `asin` or `product_name` alone, since neither carries "
            "region information the way a URL's domain does."
        ),
    )
    asin: str | None = Field(
        default=None,
        description="10-character ASIN, e.g. 'B08N5WRWNW'. Highest-priority identifier.",
    )
    product_url: str | None = Field(
        default=None,
        description=(
            "Full Amazon product URL. ASIN is extracted from this if "
            "`asin` is not given, and region is inferred from this if "
            "`region` is not given."
        ),
    )
    product_name: str | None = Field(
        default=None,
        description=(
            "Product title to fuzzy-match against, used only when neither "
            "`asin` nor `product_url` is provided or when they fail to "
            "resolve to a usable ASIN."
        ),
    )
    max_pages: int = Field(
        default=5,
        ge=1,
        le=5,
        description="Hard-capped at 5 — do not scan further even if the product isn't found.",
    )

    @field_validator("asin", mode="after")
    @classmethod
    def _normalize_asin(cls, value: str | None) -> str | None:
        """Normalizes `asin` to uppercase and rejects anything that isn't
        a real 10-character ASIN, converting it to None instead of raising. """
        if value is None:
            return None
        stripped = value.strip().upper()
        if not stripped:
            return None
        if not re.fullmatch(_ASIN_FORMAT, stripped):
            return None
        return stripped

    @model_validator(mode="after")
    def _require_one_identifier(self) -> "RankRequest":
        if not (self.asin or (self.product_url and self.product_url.strip()) or (self.product_name and self.product_name.strip())):
            raise ValueError(
                "At least one of 'asin', 'product_url', or 'product_name' "
                "must be provided to locate the product's rank. If 'asin' "
                "was provided, it must be a valid 10-character ASIN."
            )
        return self

    @model_validator(mode="after")
    def _resolve_region(self) -> "RankRequest":
        """Fills in `region` when the caller didn't supply one.

        Runs after `_require_one_identifier` (Pydantic v2 model_validators
        execute in declaration order), so by this point at least one
        identifier is guaranteed present. Resolution order:
            1. Explicit `region` — always wins if given, even if it
               disagrees with the URL's actual domain, since the caller
               may deliberately be checking a cross-region listing.
            2. Inferred from `product_url`'s domain.
            3. "US" — a reasonable default when neither is available
               (asin/product_name-only requests), matching the prior
               default value's behavior for full backward compatibility.

        Mutates and returns self rather than raising — region is always
        resolvable to *something*, so there's no failure case here.
        """
        if self.region is not None:
            return self

        detected = detect_region_from_url(self.product_url)
        object.__setattr__(self, "region", detected or "US")
        return self


class RankResponse(BaseModel):
    """Response body for POST /api/rank."""

    found: bool
    rank: int | None = Field(
        default=None,
        description="1-indexed position in the combined organic+sponsored SERP order across all scanned pages. None if not found.",
    )
    page: int | None = Field(
        default=None, description="Which page (1-indexed) the match was found on."
    )
    positionOnPage: int | None = Field(
        default=None, description="1-indexed position within that specific page."
    )
    matchMethod: MatchMethod = Field(
        default="none", description="Which identifier resolved the match: 'asin', 'url', 'name', or 'none'."
    )
    matchedProduct: Product | None = Field(
        default=None, description="The product record that matched, if found."
    )
    pagesScanned: int = Field(description="How many pages were actually fetched.")
    totalProductsScanned: int = Field(
        description="Total organic + sponsored products scanned across all pages."
    )
    allProducts: list[Product] = Field(
        default_factory=list,
        description="Every product scanned, in displayed SERP order, sponsored included — for building a full-results view alongside the rank.",
    )
    resultsInfo: ResultsInfo | None = Field(
        default=None,
        description="Amazon's total-results summary for this keyword, taken from the first page scanned.",
    )
    scanIncomplete: bool = Field(
        default=False,
        description=(
            "True if the scan stopped early because a page fetch failed "
            "after retries (e.g. Amazon's bot-check persisted across all "
            "retry attempts), rather than because max_pages was exhausted "
            "or the product was found. When True and found=False, "
            "'not found' means 'not found in the pages successfully "
            "scanned before the failure' — not a confirmed absence across "
            "the full requested page range. The client should treat this "
            "case differently from a clean not-found (e.g. show a retry "
            "prompt) rather than displaying it identically."
        ),
    )
