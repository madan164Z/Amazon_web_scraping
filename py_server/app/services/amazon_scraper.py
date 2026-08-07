import asyncio
import random
import re
from difflib import SequenceMatcher

import httpx
from bs4 import BeautifulSoup, Tag
from loguru import logger

from app.core.config import settings
from app.core.exceptions import InvalidRegionError, UpstreamRequestError
from app.schemas.product import MatchMethod, Product, Region, ResultsInfo

_ASIN_PATTERN = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})")
_NAME_MATCH_THRESHOLD = 0.6
_RESULTS_INFO_PATTERN = re.compile(
    r"([\d,]+)\s*-\s*([\d,]+)\s+of\s+(over\s+)?([\d,]+)\s+results?\s+for",
    re.IGNORECASE,
)

AMAZON_DOMAINS: dict[str, str] = {
    "US": "https://www.amazon.com",
    "ES": "https://www.amazon.es",
    "BR": "https://www.amazon.com.br",
    "IN": "https://www.amazon.in",
    "PK": "https://www.amazon.com",
}

# Rotated per-request rather than one static User-Agent for every call.
# A single fixed UA across every request this deployment ever makes is
# itself a fingerprint-able signal distinguishing "one script" traffic
# from real browser diversity — rotating among a handful of current,
# genuinely common desktop browser UAs is a low-effort reduction in how
# uniform this traffic looks, on top of (not instead of) the retry logic
# below, which is what handles the case where a block happens anyway.
_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
    "Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)


def _build_request_headers() -> dict[str, str]:
    """Builds one request's header set with a randomly-selected User-Agent.

    Called fresh per httpx.AsyncClient construction (i.e. once per
    scrape_amazon/find_product_rank call, not once per page — all pages
    within a single rank scan share one client and therefore one UA,
    which is itself more realistic than a UA that changes page-to-page
    within what should look like one continuous browsing session).
    """
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Referer": "https://www.google.com/",
    }


def _parse_price_to_float(price_string: str | None) -> float | None:
    """Parses a price string (e.g. "$1,234.56" or "R$ 1.234,56") to float.
    Args:
        price_string: Raw text content of the price DOM element.

    Returns:
        Parsed price as float, or None if the string is empty/unparseable.
    """
    if not price_string or not isinstance(price_string, str):
        return None

    last_dot_index = price_string.rfind(".")
    last_comma_index = price_string.rfind(",")

    if last_comma_index > last_dot_index:
        cleaned = price_string.replace(".", "").replace(",", ".")
    else:
        # US format: ',' is thousands separator.
        cleaned = price_string.replace(",", "")
    final_string = re.sub(r"[^\d.]", "", cleaned)

    try:
        return float(final_string)
    except ValueError:
        return None


def _parse_rating_to_float(rating_string: str | None) -> float | None:
    """Extracts the numeric rating from a string like "4.5 out of 5 stars".

    Direct port of `parseRatingToFloat` in the original JS controller.

    Args:
        rating_string: Raw text content of the rating DOM element.

    Returns:
        Parsed rating as float, or None if no number is found.
    """
    if not rating_string or not isinstance(rating_string, str):
        return None

    standardized = rating_string.replace(",", ".")
    match = re.search(r"(\d+\.\d+)|(\d+)", standardized)

    if match:
        return float(match.group(0))

    return None


def _strip_url_query(url: str) -> str:
    """Strips query string and trailing slash from a URL for comparison."""
    return url.split("?", 1)[0].rstrip("/")


def _extract_asin(product_url: str | None, fallback_data_asin: str | None = None) -> str | None:
    """Extracts the 10-character ASIN from a product URL.
    Args:
        product_url: Absolute or relative Amazon product URL.
        fallback_data_asin: Value of the result container's `data-asin`
            attribute, used only if URL parsing fails.

    Returns:
        The ASIN, or None if it could not be determined from either source.
    """
    if product_url:
        match = _ASIN_PATTERN.search(product_url)
        if match:
            return match.group(1)

    if fallback_data_asin and re.fullmatch(r"[A-Z0-9]{10}", fallback_data_asin):
        return fallback_data_asin

    return None


def _normalize_title(title: str) -> str:
    """Lowercases and strips punctuation/extra whitespace for fuzzy matching."""
    lowered = title.lower()
    stripped = re.sub(r"[^\w\s]", " ", lowered)
    return re.sub(r"\s+", " ", stripped).strip()


def _titles_match(candidate: str | None, target_normalized: str) -> bool:
    """Fuzzy-compares a scraped product title against the target product name.
    Args:
        candidate: Scraped product title (may be None).
        target_normalized: Already-normalized target title to compare against.

    Returns:
        True if the titles are similar enough to be considered the same product.
    """
    if not candidate:
        return False
    candidate_normalized = _normalize_title(candidate)
    ratio = SequenceMatcher(None, candidate_normalized, target_normalized).ratio()
    return ratio >= _NAME_MATCH_THRESHOLD


def _extract_product(product_el: Tag, domain: str, region: Region) -> Product:
    """Extracts a single Product from one search-result container element."""
    title_el = product_el.select_one('div[data-cy="title-recipe"] a h2 span')
    link_el = product_el.select_one('div[data-cy="title-recipe"] .a-link-normal')
    rating_el = product_el.select_one('div[data-cy="reviews-block"] .a-icon-alt')
    price_el = product_el.select_one('div[data-cy="price-recipe"] .a-price .a-offscreen')
    image_el = product_el.select_one('div[data-cy="image-container"] .s-image')
    if title_el is None:
        title_el = product_el.select_one("h2 span")
    if link_el is None:
        link_el = product_el.select_one('a.a-link-normal[href*="/dp/"]') or product_el.select_one(
            "h2 a.a-link-normal"
        )
    if price_el is None:
        price_el = product_el.select_one(".a-price .a-offscreen")
    if image_el is None:
        image_el = product_el.select_one("img.s-image")

    title = title_el.get_text(strip=True) if title_el else None

    product_url = None
    if link_el and link_el.get("href"):
        href = link_el["href"]
        product_url = href if href.startswith("http") else f"{domain}{href}"

    rating_stars = (
        _parse_rating_to_float(rating_el.get_text(strip=True)) if rating_el else None
    )
    price = _parse_price_to_float(price_el.get_text(strip=True)) if price_el else None
    image_url = image_el.get("src") if image_el else None

    asin = _extract_asin(product_url, fallback_data_asin=product_el.get("data-asin"))

    is_sponsored = _is_sponsored(product_el)

    return Product(
        title=title,
        price=price,
        ratingStars=rating_stars,
        imageUrl=image_url,
        productUrl=product_url,
        asin=asin,
        region=region,
        isSponsored=is_sponsored,
    )


def _is_sponsored(product_el: Tag) -> bool:
    """Determines whether a search-result container is a Sponsored placement.

    Args:
        product_el: The search-result container element.

    Returns:
        True if any sponsored signal is present.
    """
    if product_el.get("data-component-type") == "sp-sponsored-result":
        return True

    if product_el.select_one(".puis-label-popover, .s-sponsored-label-text, .s-widget-sponsored-label-text"):
        return True
    label_el = product_el.select_one("[data-component-type='s-label-popover-icon'] ~ span, .a-color-secondary")
    if label_el and label_el.get_text(strip=True).lower() == "sponsored":
        return True

    return False


def _extract_pagination(soup: BeautifulSoup, current_page: int) -> int:
    """Extracts total page count from Amazon's pagination strip.
    Args:
        soup: Parsed HTML document.
        current_page: The page number that was requested.

    Returns:
        Total number of result pages, defaulting to `current_page` when
        it cannot be determined from the page markup.
    """
    pagination_container = soup.select_one(".s-pagination-strip")
    if not pagination_container:
        return current_page

    total_pages = current_page

    last_page_el = pagination_container.select_one(
        ".s-pagination-item.s-pagination-disabled"
        ":not(.s-pagination-previous):not(.s-pagination-ellipsis)"
    )
    if last_page_el:
        text = last_page_el.get_text(strip=True)
        if text != "...":
            try:
                total_pages = int(text)
            except ValueError:
                total_pages = current_page

    next_button_disabled = pagination_container.select_one(
        ".s-pagination-item.s-pagination-next.s-pagination-disabled"
    )
    if next_button_disabled:
        total_pages = current_page

    return total_pages


def _extract_results_info(soup: BeautifulSoup) -> ResultsInfo | None:
    """Extracts Amazon's own result-count summary for the searched keyword.

    Amazon renders a line such as "289-306 of over 90,000 results for
    'dog bed'" in a result-info bar above the grid. The exact container
    markup shifts between layout experiments, so this tries the known
    selectors first and falls back to scanning the full page text for
    the pattern — the regex is what actually carries the parse, the
    selectors are just there to narrow the search and avoid false
    positives elsewhere on the page.

    Args:
        soup: Parsed HTML of a search-results page.

    Returns:
        A `ResultsInfo` with the parsed range/total, or None if no
        result-count text could be found (e.g. zero-results pages).
    """
    candidates = [
        soup.select_one('span[data-component-type="s-result-info-bar"]'),
        soup.select_one(".s-desktop-toolbar"),
        soup.select_one(".sg-col-inner .a-section.a-spacing-small.a-spacing-top-small"),
    ]

    text = None
    for container in candidates:
        if container:
            candidate_text = container.get_text(" ", strip=True)
            if _RESULTS_INFO_PATTERN.search(candidate_text):
                text = candidate_text
                break

    if text is None:
        full_text = soup.get_text(" ", strip=True)
        if _RESULTS_INFO_PATTERN.search(full_text):
            text = full_text

    if text is None:
        return None

    match = _RESULTS_INFO_PATTERN.search(text)
    if not match:
        return None

    range_start = int(match.group(1).replace(",", ""))
    range_end = int(match.group(2).replace(",", ""))
    is_estimate = match.group(3) is not None
    total = int(match.group(4).replace(",", ""))

    return ResultsInfo(
        rangeStart=range_start,
        rangeEnd=range_end,
        total=total,
        totalIsEstimate=is_estimate,
        raw=match.group(0),
    )


def _is_blocked_page(response_text: str, soup: BeautifulSoup) -> bool:
    """Detects Amazon's soft-block / CAPTCHA page, which returns HTTP 200
    (so `raise_for_status()` doesn't catch it) but is not a real SERP.

    This matters most on shared/datacenter egress IPs (serverless
    platforms, cloud VMs) — Amazon is far more likely to challenge those
    than a residential IP, and does so silently: same 200 status, totally
    different HTML. Without this check, a blocked response gets parsed as
    "0 products on this page", which the rank scanner then treats as an
    early, seemingly clean stop — reporting the wrong page count, wrong
    rank, or "not found" with no indication anything went wrong upstream.

    Args:
        response_text: Raw HTML body of the response.
        soup: The same body, already parsed.

    Returns:
        True if this looks like a bot-check/interstitial page rather than
        genuine search results.
    """
    lowered = response_text.lower()
    block_markers = (
        "enter the characters you see below",
        "to discuss automated access to amazon data",
        "/errors/validatecaptcha",
        "api-services-support@amazon.com",
        "sorry, we just need to make sure you're not a robot",
    )
    if any(marker in lowered for marker in block_markers):
        return True

    title = soup.select_one("title")
    if title and "robot check" in title.get_text(strip=True).lower():
        return True

    return False


async def _fetch_search_page(
    client: httpx.AsyncClient, domain: str, keyword: str, page: int, region: Region
) -> tuple[BeautifulSoup, list[Product], ResultsInfo | None]:
    """Fetches and parses one Amazon search-results page into Products.
    Args:
        client: Shared httpx client for connection reuse across pages.
        domain: Base Amazon domain URL for this region, e.g. "https://www.amazon.com".
        keyword: Search term, will be URL-encoded by httpx's params handling.
        page: 1-indexed result page to fetch.
        region: Region tag to stamp onto each extracted Product.

    Returns:
        A tuple of (parsed soup, extracted products in DOM/displayed order,
        parsed results-count info or None). Cards with no title and no
        resolvable identifier (ASIN/URL) are dropped as layout-only
        elements; everything else is kept, including listings with no
        price (e.g. not deliverable to the request's inferred location).

    Raises:
        UpstreamRequestError: Request failed, timed out, returned a
            non-2xx status, or came back as a bot-check/CAPTCHA page
            (see `_is_blocked_page`) — this is treated as a hard failure
            rather than "zero results", since silently continuing on a
            blocked page is exactly what produces wrong ranks in
            production without ever raising an error.
    """
    url = f"{domain}/s"
    params = {"k": keyword, "page": str(page)}

    try:
        response = await client.get(url, params=params)
        response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise UpstreamRequestError(f"Request to {domain} timed out") from exc
    except httpx.HTTPStatusError as exc:
        raise UpstreamRequestError(
            f"Amazon responded with status {exc.response.status_code} "
            "(likely rate-limited or blocking the request)"
        ) from exc
    except httpx.HTTPError as exc:
        raise UpstreamRequestError(f"Failed to reach {domain}: {exc}") from exc

    soup = BeautifulSoup(response.text, "lxml")

    if _is_blocked_page(response.text, soup):
        raise UpstreamRequestError(
            f"Amazon served a bot-check/CAPTCHA page for {domain} instead of "
            "search results (page=" + str(page) + ", keyword=" + keyword + "). "
            "This is common on shared/datacenter IPs (serverless platforms, "
            "cloud VMs) and does not mean zero results — it means the request "
            "was challenged before any real data was returned."
        )

    organic_elements = soup.select('div[data-component-type="s-search-result"]')
    sponsored_elements = soup.select('div[data-component-type="sp-sponsored-result"]')

    seen_ids: set[int] = set()
    product_elements: list[Tag] = []
    for el in organic_elements + sponsored_elements:
        if id(el) not in seen_ids:
            seen_ids.add(id(el))
            product_elements.append(el)

    all_tags_in_order = list(soup.find_all("div"))
    order_index = {id(tag): i for i, tag in enumerate(all_tags_in_order)}
    product_elements.sort(key=lambda el: order_index.get(id(el), 0))

    products = [_extract_product(el, domain, region) for el in product_elements]
    # Keep any real listing — title plus a resolvable identifier (ASIN or
    # product URL). Do NOT require `price is not None`: Amazon omits the
    # price for products it won't deliver to the request's IP-inferred
    # location (or that are otherwise unbuyable there), but the listing
    # still occupies a real rank in the SERP. Filtering on price would
    # silently drop these from `all_products` and shift/undercount every
    # rank after them. Only drop cards with no title and no identifier —
    # those are layout-only elements (e.g. carousel bleed-through), not
    # real listings, regardless of which country the seller ships from.
    products = [p for p in products if p.title and (p.asin or p.productUrl)]

    results_info = _extract_results_info(soup)

    return soup, products, results_info


# Number of attempts (including the first) made for a single search-page
# fetch before giving up and propagating UpstreamRequestError. Amazon's
# bot-check is frequently transient on shared/datacenter IPs — the same
# request retried a few seconds later, with a fresh set of headers, often
# succeeds where the first attempt was challenged. This is the single
# biggest lever for reducing the "An error occurred while scraping data"
# failure rate seen in production without changing IP/proxy strategy.
_MAX_FETCH_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 2.0


async def _fetch_search_page_with_retry(
    client: httpx.AsyncClient, domain: str, keyword: str, page: int, region: Region
) -> tuple[BeautifulSoup, list[Product], ResultsInfo | None]:
    """Retries `_fetch_search_page` with exponential backoff + jitter on
    transient upstream failures (timeouts, 5xx, and Amazon bot-check
    pages), instead of surfacing a hard failure on the first hiccup.

    This is what most directly reduces the "An error occurred while
    scraping data" failure rate: on datacenter/serverless egress IPs,
    Amazon's soft-block is often a per-request coin flip rather than a
    durable ban — a retry a couple seconds later, from the same IP, with
    the same headers, frequently returns a genuine results page. Retrying
    at this layer (once per page) rather than only at the top-level route
    also means a transient block on, say, page 3 of a 5-page rank scan
    doesn't have to restart the whole scan from page 1.

    Args:
        client, domain, keyword, page, region: Passed through unchanged
            to `_fetch_search_page`.

    Returns:
        Same shape as `_fetch_search_page`.

    Raises:
        UpstreamRequestError: If every attempt (see `_MAX_FETCH_ATTEMPTS`)
            fails. The exception raised is from the *last* attempt, so
            the caller sees the most recent failure reason rather than
            the first.
    """
    last_error: UpstreamRequestError | None = None

    for attempt in range(1, _MAX_FETCH_ATTEMPTS + 1):
        try:
            return await _fetch_search_page(client, domain, keyword, page, region)
        except UpstreamRequestError as exc:
            last_error = exc
            if attempt < _MAX_FETCH_ATTEMPTS:
                backoff = _RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                jitter = random.uniform(0, backoff * 0.5)
                delay = backoff + jitter
                logger.warning(
                    "Fetch attempt {}/{} failed for page {} ({}), retrying in {:.1f}s",
                    attempt,
                    _MAX_FETCH_ATTEMPTS,
                    page,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

    # Loop only exits without returning if every attempt raised — re-raise
    # the last failure rather than swallowing it.
    assert last_error is not None
    raise last_error


async def scrape_amazon(
    keyword: str, region: Region, page: int
) -> tuple[list[Product], int, int, ResultsInfo | None]:
    """Fetches and parses an Amazon search-results page (organic + sponsored).

    Args:
        keyword: Search term, will be URL-encoded.
        region: One of "US", "ES", "BR", "IN", "PK" — selects the Amazon
            domain (see AMAZON_DOMAINS).
        page: 1-indexed result page to fetch.

    Returns:
        A tuple of (products, current_page, total_pages, results_info).
        `products` includes both organic and sponsored results, each
        flagged via `Product.isSponsored`, in the order Amazon displayed
        them — including listings with no price (e.g. not deliverable to
        the request's inferred location); those still occupy a real rank
        and are not filtered out. `results_info` is Amazon's own
        "X-Y of over Z results for '<keyword>'" summary, or None if it
        couldn't be parsed off the page.

    Raises:
        InvalidRegionError: If `region` is not a supported domain key.
        UpstreamRequestError: If the request to Amazon fails, times out,
            or returns a non-2xx status (including likely bot-blocking
            responses like 503/CAPTCHA pages).
    """
    domain = AMAZON_DOMAINS.get(region)
    if domain is None:
        raise InvalidRegionError(region)

    logger.info("Scraping Amazon", region=region, keyword=keyword, page=page)

    async with httpx.AsyncClient(
        headers=_build_request_headers(),
        timeout=settings.request_timeout_seconds,
        follow_redirects=True,
    ) as client:
        soup, products, results_info = await _fetch_search_page_with_retry(
            client, domain, keyword, page, region
        )

    total_pages = _extract_pagination(soup, current_page=page)

    sponsored_count = sum(1 for p in products if p.isSponsored)
    logger.info(
        "Scrape complete",
        region=region,
        results=len(products),
        sponsored=sponsored_count,
        total_pages=total_pages,
    )

    return products, page, total_pages, results_info


async def find_product_rank(
    keyword: str,
    region: Region,
    asin: str | None = None,
    product_url: str | None = None,
    product_name: str | None = None,
    max_pages: int = 5,
) -> tuple[
    bool,
    int | None,
    int | None,
    int | None,
    MatchMethod,
    Product | None,
    int,
    list[Product],
    ResultsInfo | None,
    bool,
]:
    """Scans up to `max_pages` of Amazon search results to locate one product's rank.

    Rank is the 1-indexed position in the *combined* organic+sponsored
    display order across all scanned pages — matching what a real user
    scrolling the search results would count, not two separate organic/
    sponsored counters. Pages are fetched sequentially (not concurrently)
    and stop as soon as a match is found, both to minimize request volume
    against Amazon (rate-limit risk) and because later pages become
    irrelevant once the target's position is known.

    Matching priority (first resolvable method wins, per product, in order):
        1. ASIN exact match — `asin` if given, else extracted from `product_url`.
        2. product_url exact match — only reached if neither yields a usable
           ASIN (e.g. a malformed URL); compares normalized URLs directly.
        3. product_name fuzzy match — only used if no ASIN could be resolved
           from either `asin` or `product_url`, since name matching is
           inherently less reliable than an exact identifier.

    Args:
        keyword: Search term to scan results for, e.g. "backpack".
        region: Amazon marketplace region.
        asin: Target product's ASIN, if known. Highest-priority identifier.
        product_url: Target product's full Amazon URL. ASIN is extracted
            from this if `asin` is not given.
        product_name: Target product's title, used only as a fallback when
            no ASIN could be resolved from `asin` or `product_url`.
        max_pages: Upper bound on pages scanned, hard-capped by the caller
            (route layer) at 5 regardless of what's passed here.

    Returns:
        Tuple of:
            found: Whether a match was located.
            rank: 1-indexed combined position, or None.
            page: 1-indexed page the match was on, or None.
            position_on_page: 1-indexed position within that page, or None.
            match_method: Which identifier resolved the match.
            matched_product: The matching Product, or None.
            pages_scanned: How many pages were actually fetched.
            all_products: Every product scanned, in displayed order.
            results_info: Amazon's "X-Y of over Z results for '<keyword>'"
                summary, taken from the first page scanned, or None.
            scan_incomplete: True if the scan stopped early because a page
                fetch failed after retries, rather than because max_pages
                was exhausted or the product was found — see
                `RankResponse.scanIncomplete` for how this should be
                surfaced to the client.

    Raises:
        InvalidRegionError: If `region` is not a supported domain key.
        UpstreamRequestError: Only if the *first* page fails after
            retries — a failed first page means zero data was gathered,
            which is a materially different (and materially less useful)
            result than a scan that gathered several pages of real data
            before hitting a transient block partway through. Failures on
            page 2+ are handled gracefully instead (see `scan_incomplete`
            in the Returns section) rather than discarding everything
            already scanned.
    """
    domain = AMAZON_DOMAINS.get(region)
    if domain is None:
        raise InvalidRegionError(region)

    target_asin = asin or _extract_asin(product_url)
    target_name_normalized = _normalize_title(product_name) if product_name else None

    logger.info(
        "Rank lookup starting",
        region=region,
        keyword=keyword,
        target_asin=target_asin,
        has_name_fallback=target_name_normalized is not None,
        max_pages=max_pages,
    )

    all_products: list[Product] = []
    combined_index = 0
    pages_scanned = 0
    results_info: ResultsInfo | None = None
    scan_incomplete = False

    async with httpx.AsyncClient(
        headers=_build_request_headers(),
        timeout=settings.request_timeout_seconds,
        follow_redirects=True,
    ) as client:
        for page_num in range(1, max_pages + 1):
            try:
                _, page_products, page_results_info = await _fetch_search_page_with_retry(
                    client, domain, keyword, page_num, region
                )
            except UpstreamRequestError:
                if page_num == 1:
                    # Zero data gathered — nothing useful to return, so
                    # this is a genuine hard failure. Let it propagate to
                    # the route layer's existing 502 handling.
                    raise
                # Pages 1..page_num-1 already succeeded and are in
                # all_products — return what was gathered instead of
                # discarding a multi-page scan over one late failure.
                logger.warning(
                    "Rank lookup: page {} failed after retries, stopping "
                    "with partial results ({} products from {} pages)",
                    page_num,
                    len(all_products),
                    pages_scanned,
                )
                scan_incomplete = True
                break

            pages_scanned = page_num
            if page_num == 1:
                results_info = page_results_info

            if not page_products:
                logger.info("Rank lookup: page {} empty, stopping scan", page_num)
                break

            for position_on_page, product in enumerate(page_products, start=1):
                combined_index += 1
                all_products.append(product)

                match_method: MatchMethod = "none"
                if target_asin and product.asin and product.asin == target_asin:
                    match_method = "asin"
                elif (
                    target_asin is None
                    and product_url
                    and product.productUrl
                    and _strip_url_query(product.productUrl) == _strip_url_query(product_url)
                ):
                    match_method = "url"
                elif (
                    target_asin is None
                    and target_name_normalized
                    and _titles_match(product.title, target_name_normalized)
                ):
                    match_method = "name"

                if match_method != "none":
                    logger.info(
                        "Rank lookup: match found",
                        rank=combined_index,
                        page=page_num,
                        position_on_page=position_on_page,
                        match_method=match_method,
                    )
                    return (
                        True,
                        combined_index,
                        page_num,
                        position_on_page,
                        match_method,
                        product,
                        pages_scanned,
                        all_products,
                        results_info,
                        False,
                    )
            if page_num < max_pages:
                delay = random.uniform(
                    settings.page_min_delay_seconds, settings.page_max_delay_seconds
                )
                await asyncio.sleep(delay)

    logger.info(
        "Rank lookup: not found within {} pages ({} products scanned, incomplete={})",
        pages_scanned,
        len(all_products),
        scan_incomplete,
    )
    return False, None, None, None, "none", None, pages_scanned, all_products, results_info, scan_incomplete
