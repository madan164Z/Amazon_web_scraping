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
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36"
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
            "(any origin) for local development."
        ),
    )

    @property
    def cors_origins_list(self) -> list[str]:
        """Splits `cors_allowed_origins` into the list shape CORSMiddleware expects."""
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]


settings = Settings()
