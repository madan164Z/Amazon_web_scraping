from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from loguru import logger

from app.core.exceptions import (
    InvalidRegionError,
    ProductNotIdentifiableError,
    UpstreamRequestError,
)
from app.schemas.product import (
    ErrorResponse,
    RankRequest,
    RankResponse,
    Region,
    ScrapeResponse,
)
from app.services.amazon_scraper import find_product_rank, scrape_amazon

router = APIRouter(prefix="/api", tags=["scrape"])


@router.get(
    "/scrape",
    response_model=ScrapeResponse,
    responses={
        400: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
)
async def scrape(
    keyword: str = Query(..., min_length=1, description="Search term"),
    region: Region = Query(default="US", description="Amazon marketplace region"),
    page: int = Query(default=1, ge=1, description="1-indexed result page"),
) -> ScrapeResponse:
    """Scrapes Amazon search results for the given keyword/region/page."""
    try:
        products, current_page, total_pages, results_info = await scrape_amazon(
            keyword=keyword, region=region, page=page
        )
    except InvalidRegionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UpstreamRequestError as exc:
        logger.error("Upstream scrape failure: {}", exc)
        raise HTTPException(
            status_code=502,
            detail="An error occurred while scraping data.",
        ) from exc

    return ScrapeResponse(
        products=products,
        currentPage=current_page,
        totalPages=total_pages,
        resultsInfo=results_info,
    )


@router.post(
    "/rank",
    response_model=RankResponse,
    responses={
        400: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
)
async def rank(request: RankRequest) -> RankResponse:
    """Finds a specific product's position in Amazon search results."""
    try:
        (
            found,
            rank_value,
            page,
            position_on_page,
            match_method,
            matched_product,
            pages_scanned,
            all_products,
            results_info,
            scan_incomplete,
        ) = await find_product_rank(
            keyword=request.keyword,
            region=request.region,
            asin=request.asin,
            product_url=request.product_url,
            product_name=request.product_name,
            max_pages=min(request.max_pages, 5),
        )
    except InvalidRegionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UpstreamRequestError as exc:
        logger.error("Upstream rank-lookup failure: {}", exc)
        raise HTTPException(
            status_code=502,
            detail="An error occurred while scraping data.",
        ) from exc

    return RankResponse(
        found=found,
        rank=rank_value,
        page=page,
        positionOnPage=position_on_page,
        matchMethod=match_method,
        matchedProduct=matched_product,
        pagesScanned=pages_scanned,
        totalProductsScanned=len(all_products),
        allProducts=all_products,
        resultsInfo=results_info,
        scanIncomplete=scan_incomplete,
    )
