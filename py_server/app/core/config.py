from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    """Runtime configuration for the scraper API."""
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    port: int = 3000
    request_timeout_seconds: float = 15.0
    user_agent: str = Field(
        default=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36"
        ),
        description=(
            "DEPRECATED / unused by the scraper itself: "
            "app.services.amazon_scraper now rotates among several "
            "hardcoded User-Agent strings per-request rather than using "
            "one fixed value (see _USER_AGENTS in amazon_scraper.py) — a "
            "single static UA across every request is itself a "
            "fingerprint-able signal. Left here only so an existing .env "
            "with USER_AGENT set doesn't fail to load (extra='ignore' "
            "would silently drop it anyway, but keeping the field avoids "
            "a confusing 'why isn't my override doing anything' moment)."
        ),
    )
    page_min_delay_seconds: float = Field(
        default=1.5,
        description="Minimum delay between successive search-page requests during a rank scan, jittered.",
    )
    page_max_delay_seconds: float = Field(
        default=3.5,
        description="Maximum delay between successive search-page requests during a rank scan, jittered.",
    )
    cors_allowed_origins: str = Field(
        default="*",
        description=(
            "Comma-separated list of allowed frontend origins, e.g. "
            "'https://myapp.vercel.app,https://myapp.com'. Defaults to '*' "
            "(any origin) for local development. Set this explicitly in "
            "production — a wildcard is also silently incompatible with "
            "allow_credentials=True in real browsers, so it must be a "
            "concrete origin list once a frontend domain exists."
        ),
    )

    @property
    def cors_origins_list(self) -> list[str]:
        """Splits `cors_allowed_origins` into the list shape CORSMiddleware expects."""
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]


settings = Settings()
