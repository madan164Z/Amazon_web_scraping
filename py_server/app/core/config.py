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
    request_timeout_seconds: float = 50.0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36"
    )
    bsr_max_concurrent_requests: int = Field(
        default=3,
        description=(
            "Max simultaneous product-detail-page requests when fetching "
            "Best Seller Rank. Higher = faster but much more likely to "
            "trigger Amazon's rate-limiting/blocking."
        ),
    )
    bsr_min_delay_seconds: float = Field(
        default=1.5,
        description="Minimum delay before each detail-page request, jittered.",
    )
    bsr_max_delay_seconds: float = Field(
        default=3.5,
        description="Maximum delay before each detail-page request, jittered.",
    )


settings = Settings()
