from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

from app.core.config import settings
from app.routes.scrape_routes import router as scrape_router

_MISSING_IDENTIFIER_MARKER = "must be provided to locate the product's rank"

app = FastAPI(
    title="Amazon Product Scraper API",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(scrape_router)

@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Remaps the "no identifier given" validator error on POST /api/rank
    from FastAPI's default 422 to a 400, matching the status-code contract
    documented on that route (400 = caller's fault / bad input shape).
    """
    for error in exc.errors():
        message = str(error.get("msg", ""))
        if _MISSING_IDENTIFIER_MARKER in message:
            details = message.split("Value error, ", 1)[-1]
            return JSONResponse(
                status_code=400,
                content={"error": "Bad Request", "details": details},
            )

    return JSONResponse(
        status_code=422,
        content={"error": "Validation Error", "details": exc.errors()},
    )


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Liveness/readiness probe endpoint — not present in the original
    JS backend, but standard for any service that will run behind a
    load balancer, Kubernetes, or uptime monitor.
    """
    return {"status": "ok"}


@app.on_event("startup")
async def on_startup() -> None:
    logger.info("Amazon Scraper API starting on port {}", settings.port)
