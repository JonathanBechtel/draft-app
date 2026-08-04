# app/config.py
from datetime import date
from pathlib import Path
from typing import Literal, Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.schemas.boards import BoardKind

REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Draft calendar
# ---------------------------------------------------------------------------

# 2026 NBA Draft Lottery is scheduled for mid-May 2026 (exact date TBD; use
# a safe placeholder of 2026-05-13, the Tuesday of combine week).  Once the
# lottery fires, mock-draft consensus becomes the canonical hero view because
# pick slots are set.  Before the lottery, the Big Board consensus is shown.
LOTTERY_DATE: date = date(2026, 5, 13)


def get_consensus_board_kind(today: date | None = None) -> BoardKind:
    """Return the calendar-appropriate BoardKind for the consensus hero.

    Args:
        today: Override the current date (useful in tests; defaults to
            ``date.today()`` when ``None``).

    Returns:
        ``BoardKind.MOCK_DRAFT`` when today is on or after ``LOTTERY_DATE``,
        ``BoardKind.BIG_BOARD`` otherwise.
    """
    if today is None:
        today = date.today()
    return BoardKind.BIG_BOARD if today < LOTTERY_DATE else BoardKind.MOCK_DRAFT


class Settings(BaseSettings):
    database_url: str
    secret_key: str
    env: Literal["dev", "stage", "prod"] = Field(
        validation_alias=AliasChoices("ENV", "APP_ENV")
    )
    debug: bool = False
    log_level: str = "INFO"
    access_log: bool = False
    log_requests: bool = False
    sql_echo: bool = False
    auto_init_db: bool = True

    # Gemini API settings
    gemini_api_key: Optional[str] = None  # General/image generation key
    gemini_summarization_api_key: Optional[str] = (
        None  # Separate key for RSS summarization
    )
    gemini_embedding_model: str = "gemini-embedding-001"
    # Output dimensionality requested from the embedding model. Must match the
    # vector width of the player_embeddings.embedding column; changing it
    # requires a migration + re-embed.
    gemini_embedding_dim: int = 768
    # Task type for embeddings. SEMANTIC_SIMILARITY is symmetric (same for the
    # stored player vector and the query) and materially outperforms the
    # default/retrieval modes for short name-to-name matching. Changing it
    # requires re-embedding the whole table.
    gemini_embedding_task_type: str = "SEMANTIC_SIMILARITY"
    youtube_api_key: Optional[str] = None

    # Image generation settings
    image_gen_size: str = "1K"  # Options: "1K", "2K"
    image_gen_quality: str = "standard"  # Options: "draft", "standard", "high"
    default_image_style: str = Field(
        default="default",
        validation_alias=AliasChoices("DEFAULT_IMAGE_STYLE", "IMAGE_STYLE_DEFAULT"),
        description="Global default player image style for UI rendering.",
    )

    # S3 storage settings
    s3_bucket_name: Optional[str] = None
    s3_region: str = Field(
        default="us-east-1",
        validation_alias=AliasChoices("S3_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"),
    )
    s3_access_key_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID"),
    )
    s3_secret_access_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("S3_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY"),
    )
    s3_public_url_base: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("S3_PUBLIC_URL_BASE", "CDN_PUBLIC_URL_BASE"),
        description="Optional base URL (CDN) for serving S3 objects.",
    )
    s3_upload_acl: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "S3_UPLOAD_ACL",
        ),
        description=(
            "Optional S3 ACL to apply on upload (e.g., 'public-read'). "
            "If unset, objects rely on bucket policy/permissions for readability."
        ),
    )
    image_storage_local: bool = False  # True = local filesystem (dev only)

    # Email settings (for user invitations and password resets)
    resend_api_key: Optional[str] = None
    email_from_address: str = "noreply@draftguru.dev"
    app_base_url: str = "http://localhost:8000"

    # Board auto-ingest worker settings.
    # Feature flag: ships dormant (False) so the worker only runs when explicitly
    # enabled via environment variable or .env.
    board_auto_ingest_enabled: bool = False
    board_auto_ingest_lookback_days: int = 7

    # Post-lottery mock-draft team overlay.
    # When enabled (and the calendar is past LOTTERY_DATE), the consensus board
    # renders as a mock draft: each consensus row is shown with the team that
    # owns that pick slot (from the DraftPickSlot reference). Ships dormant
    # (False) so it only goes live once the draft order is seeded and reviewed.
    mock_draft_team_overlay_enabled: bool = False

    # Summer League Desk home-page takeover -- the operator override lever the
    # Event Desk framework doc calls out ("Window source = schedule-driven with
    # a config force-on/off & date override"). `app.services.sources.summer_league.
    # desk_read` is the sole reader; see that module's docstring for the exact
    # gating semantics.
    #   - "auto" (default): the calendar decides. The Desk takes over the home
    #     page only when the SL event's lifecycle is Active/Wind-down AND it is
    #     the resolved `is_home_owner` (single-owner-by-priority, per
    #     `event_desk.lifecycle.resolve_home_owner`).
    #   - "off": kill switch. The Desk never takes over, regardless of
    #     lifecycle/ownership -- for incident response or a deliberate pause.
    #   - "on": bypasses the `is_home_owner` requirement only (still requires an
    #     actual Active/Wind-down lifecycle window on the calendar -- this never
    #     fabricates a window that doesn't exist). Lets ops force SL's takeover
    #     even when a higher-priority registered event would otherwise win.
    sl_desk_force_mode: Literal["auto", "off", "on"] = Field(
        default="auto",
        validation_alias=AliasChoices("SL_DESK_FORCE_MODE"),
        description=(
            "Summer League Desk home-page takeover override: 'auto' (calendar + "
            "ownership decide), 'off' (never takes over), 'on' (bypass the "
            "is_home_owner gate; the lifecycle window itself is still required)."
        ),
    )
    # Optional calendar-date override for the Desk's window resolution (same
    # framework-doc lever as `sl_desk_force_mode`). When set, every Desk
    # request or explicitly allowed dev/stage tick resolves "today" against this date instead of the real
    # wall-clock date -- useful for demoing/QAing a specific day (e.g. a
    # Wind-down Ledger) without waiting for the actual calendar. Scheduled
    # production writes reject this override so a historical demo date can
    # never be persisted as current content. `None` (default) means "use the
    # real current date."
    sl_desk_force_date: Optional[date] = Field(
        default=None,
        validation_alias=AliasChoices("SL_DESK_FORCE_DATE"),
        description=(
            "Optional calendar-date override for the Summer League Desk's "
            "window resolution. Unset uses the real current date."
        ),
    )

    # Competition Context Explorer -- freshness threshold for the "stale"
    # badge on published environment profiles (implementation contract §8:
    # "A profile beyond the configured freshness threshold remains readable
    # as the last good version but displays a stale badge; it is never
    # silently replaced by request-time aggregation."). Compared against
    # ``SummerLeagueEnvironmentProfile.calculated_at`` by
    # ``app.services.sources.summer_league.environment_refresh.is_environment_profile_stale``,
    # the single shared source of truth other surfaces (#607/#608 reads)
    # should call rather than re-deriving the threshold. Default (48h)
    # tolerates a quiet weekend/off-cycle gap during an active event without
    # false-positiving, while still catching a genuinely stuck incremental
    # refresh. A dormant/historical profile computed long ago will always
    # read as stale under this simple age check -- that is intentional
    # honesty ("last computed on <date>"), not a bug; see the runbook.
    summer_league_environment_stale_after_hours: int = Field(
        default=48,
        validation_alias=AliasChoices("SUMMER_LEAGUE_ENVIRONMENT_STALE_AFTER_HOURS"),
        description=(
            "Hours after a Competition Context profile's calculated_at "
            "before public reads display a stale badge."
        ),
    )

    @property
    def is_dev(self) -> bool:
        return self.env == "dev" or self.debug is True

    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()  # type: ignore[call-arg]
