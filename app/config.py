# app/config.py
from typing import Literal, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    secret_key: str
    env: Literal["dev", "stage", "prod"]
    debug: bool = False
    log_level: str = "INFO"
    access_log: bool = True
    sql_echo: bool = True
    auto_init_db: bool = True

    # ══════════════════════════════════════════════════════════════════════════
    # FEATURE VISIBILITY FLAGS
    # Set to False to hide entire sections from the UI
    # ══════════════════════════════════════════════════════════════════════════

    # Homepage sections
    FEATURE_MARKET_TICKER: bool = True  # Market Moves ticker
    FEATURE_CONSENSUS_MOCK: bool = True  # Consensus Mock Draft table
    FEATURE_TOP_PROSPECTS: bool = True  # Top Prospects grid
    FEATURE_VS_ARENA: bool = True  # VS Arena comparison tool
    FEATURE_NEWS_FEED: bool = True  # Live Draft Buzz feed
    FEATURE_AFFILIATE_SPECIALS: bool = False  # Draft Position Specials (affiliate)

    # Player page sections
    FEATURE_PLAYER_SCOREBOARD: bool = True  # Analytics Dashboard
    FEATURE_PLAYER_PERCENTILES: bool = True  # Performance percentile bars
    FEATURE_PLAYER_COMPARISONS: bool = True  # Similar players grid
    FEATURE_PLAYER_H2H: bool = True  # Head-to-head comparison
    FEATURE_PLAYER_NEWS: bool = True  # Player-specific news

    # Global features
    FEATURE_SEARCH: bool = True  # Search functionality
    FEATURE_SHARE_CARDS: bool = False  # PNG share card generation

    # ══════════════════════════════════════════════════════════════════════════
    # EXTERNAL INTEGRATIONS
    # API keys and URLs for external services
    # ══════════════════════════════════════════════════════════════════════════

    SPORTSBOOK_API_KEY: Optional[str] = None
    BUZZ_SERVICE_URL: Optional[str] = None
    NEWS_RSS_FEEDS: list[str] = []
    NEWS_INGEST_INTERVAL_MINUTES: int = 30
    IMAGE_CDN_BASE_URL: str = "https://placehold.co"

    # Placeholder mode (for development/demo)
    USE_PLACEHOLDER_DATA: bool = True

    @property
    def is_dev(self) -> bool:
        return self.env == "dev" or self.debug is True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()  # type: ignore[call-arg]
