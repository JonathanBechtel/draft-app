"""AI-powered board extraction service.

Takes a ``NewsItem`` (tagged BIG_BOARD), fetches the full article page
(RSS descriptions are too short for a 30+ entry ranking list), passes
the cleaned article text to Gemini for structured extraction, resolves
player names to ``players_master`` ids, and persists a PENDING ``Board``
via ``board_service``.

This is the only path that programmatically creates boards from
unstructured analyst content. Admin-curated boards bypass this module
entirely; consensus computation runs only after a human approves
whatever lands here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Protocol

import httpx
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from urllib.parse import urlparse

from app.config import settings
from app.schemas.boards import Board, BoardKind, BoardStatus, ResolutionMethod
from app.schemas.news_items import NewsItem
from app.schemas.player_aliases import PlayerAlias
from app.schemas.players_master import PlayerMaster
from app.services import board_service
from app.services.player_search_service import find_candidate_players

logger = logging.getLogger(__name__)

_PAGE_FETCH_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)
_PAGE_FETCH_HEADERS = {
    "User-Agent": "DraftGuru/1.0 (+https://draftguru.dev; board-extraction)",
}
_GEMINI_TIMEOUT_SECONDS = 60
# Stable GA model. The earlier preview variant was both flaky on latency
# and lax on instruction-following; gemini-3.5-flash with response_schema
# is materially better at honoring the no-bare-surname constraint.
_GEMINI_MODEL = "gemini-3.5-flash"

# Cap input to Gemini at ~30k chars (~7.5k tokens). Substack articles
# rarely exceed this; if they do, we lose tail entries gracefully rather
# than blowing the model's context window.
_MAX_ARTICLE_CHARS = 30_000

# CSS selectors tried in order to find the main article body. Substack's
# markup has changed over the years; the first match wins.
_SUBSTACK_BODY_SELECTORS = (
    "div.body.markup",
    "div.single-post-container div.body",
    "div.markup",
    "article",
    "div.main-content",
    "main",
)

# Substack server-renders only chrome/teaser HTML and hydrates the
# article body client-side. Their public JSON API exposes the full
# ``body_html`` (plus a clean ``audience`` paywall signal) for any
# ``<pub>.substack.com`` post, so we route those hosts through the API.
_SUBSTACK_HOST_SUFFIX = ".substack.com"


class BoardExtractionError(Exception):
    """Raised when extraction cannot proceed.

    Examples: fetch failed, no article body, AI returned malformed payload.
    """


class PaywallDetectedError(BoardExtractionError):
    """Raised when the fetched page looks paywalled / truncated."""


# --- Public DTOs returned by the parsing helpers ---------------------------


class ExtractedBoardEntry(BaseModel):
    """One row in a Gemini-parsed big board."""

    player_name: str
    rank: int = Field(ge=1)
    tier: Optional[int] = Field(default=None, ge=1)


class ExtractedBoard(BaseModel):
    """Gemini's structured output for a single big board article."""

    draft_year: int = Field(ge=2024, le=2040)
    published_at: Optional[datetime] = None
    entries: list[ExtractedBoardEntry] = Field(default_factory=list)

    @field_validator("published_at")
    @classmethod
    def _strip_tzinfo(cls, value: Optional[datetime]) -> Optional[datetime]:
        """Coerce timezone-aware datetimes to naive UTC.

        Gemini frequently emits ISO-8601 with a trailing ``Z`` or explicit
        offset, which Pydantic parses as a timezone-aware ``datetime``. The
        DB column for ``Board.published_at`` is naive UTC, and asyncpg
        refuses to bind a tz-aware datetime to a naive ``timestamp`` column.
        Strip tzinfo at the DTO boundary so downstream code can stay
        oblivious.
        """
        if value is None or value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)


# --- Resolution result DTO ------------------------------------------------


@dataclass(frozen=True)
class ResolutionResult:
    """Result of a single player-name resolution attempt.

    Attributes:
        player_id: The resolved ``players_master`` id, or ``None`` when the
            name could not be unambiguously matched.
        method: How the match was achieved (EXACT / ALIAS / UNRESOLVED).
            VECTOR is reserved for a future threshold-based auto-resolve
            ticket; this cascade always returns UNRESOLVED when vector
            candidates are emitted rather than promoting a match on score.
        candidates: Top-K nearest-neighbour results from the vector search
            step, serialised as ``[{player_id, display_name, score}, ...]``
            for storage in ``BoardEntry.vector_candidates``.  Only populated
            when ``method`` is UNRESOLVED and the vector pass ran; ``None``
            otherwise.
    """

    player_id: Optional[int]
    method: ResolutionMethod
    candidates: Optional[list[dict]] = None  # type: ignore[type-arg]


# --- Public entry point ----------------------------------------------------


async def extract_board(
    db: AsyncSession,
    *,
    news_item_id: int,
    kind: BoardKind = BoardKind.BIG_BOARD,
    fetcher: Optional["ArticleFetcher"] = None,
    ai_client: Optional[genai.Client] = None,
) -> Optional[Board]:
    """Extract a board from a NewsItem and persist as PENDING.

    Args:
        db: Async session; caller owns commit.
        news_item_id: The article to extract from.
        kind: Provenance label for the ranking — ``BIG_BOARD`` (talent
            ranking) or ``MOCK_DRAFT`` (projected pick order). Extraction is
            identical for both: we pull the analyst's ordered prospect list
            and store ``position`` as rank / pick number. ``kind`` does not
            change the extraction or the consensus math (the engine pools
            every approved board by ``position``); it only records what the
            source published, and drives the calendar-aware presentation
            label. Mock-draft pick *ownership* (which team picks at each
            slot) is canonical public data and lives in a separate
            draft-order reference, never inferred from the article here.
        fetcher: Optional override for the article-fetch step (used in tests
            to substitute canned HTML without touching the network).
        ai_client: Optional override for the Gemini client (used in tests to
            return canned structured output).

    Returns:
        The persisted Board (PENDING), or ``None`` if extraction yielded no
        valid entries.

    Raises:
        BoardExtractionError: For non-retryable failures (no article body,
            malformed AI response, no resolvable players, etc.).
        PaywallDetectedError: When the fetched page looks paywalled.
    """
    news_item = await db.get(NewsItem, news_item_id)
    if news_item is None:
        raise BoardExtractionError(f"NewsItem {news_item_id} not found")

    # Dedup: skip if a non-PENDING board already exists for this article.
    existing = await _existing_board_for_news_item(
        db, news_item_id=news_item_id, kind=kind
    )
    if existing is not None and existing.status in (
        BoardStatus.APPROVED,
        BoardStatus.REJECTED,
    ):
        logger.info(
            "board.extract.skip news_item_id=%s reason=existing_%s",
            news_item_id,
            existing.status.value.lower(),
        )
        return existing
    if existing is not None and existing.status == BoardStatus.PENDING:
        # Replace path: the spec says we re-extract and update. For this
        # first iteration, return the existing PENDING board so a human
        # can decide. Full replace-entries-in-place can come later.
        logger.info(
            "board.extract.skip news_item_id=%s reason=existing_pending",
            news_item_id,
        )
        return existing

    # 1. Fetch article HTML and clean it to plain text.
    fetch = fetcher or _default_fetcher
    article_text = await fetch(news_item.url)

    # 2. Hand the article text to Gemini and parse the structured response.
    extracted = await _extract_via_gemini(article_text, client=ai_client)
    if not extracted.entries:
        logger.warning(
            "board.extract.no_entries news_item_id=%s url=%s",
            news_item_id,
            news_item.url,
        )
        return None

    # 3. Resolve player names via cascade (exact → alias → vector).
    #    Every entry is persisted: resolved entries carry a player_id, while
    #    unresolved entries land with player_id=None and UNRESOLVED method so
    #    an admin can review them later (T7).
    entries_in: list[board_service.EntryInput] = []
    unresolved_names: list[str] = []
    seen_player_ids: set[int] = set()
    seen_positions: set[int] = set()
    for raw in extracted.entries:
        resolution = await resolve_player(db, raw.player_name)

        # Dedup: skip a rank or player that Gemini already emitted to avoid
        # unique-constraint violations. Only resolved entries (with a real
        # player_id) participate in the player-dedup check; unresolved entries
        # each get their own slot at the extracted rank.
        if raw.rank in seen_positions:
            continue
        if resolution.player_id is not None and resolution.player_id in seen_player_ids:
            continue

        if resolution.player_id is not None:
            seen_player_ids.add(resolution.player_id)
        seen_positions.add(raw.rank)

        if resolution.player_id is None:
            unresolved_names.append(raw.player_name)

        entries_in.append(
            board_service.EntryInput(
                player_id=resolution.player_id,
                position=raw.rank,
                raw_name=raw.player_name,
                resolution_method=resolution.method,
                tier=raw.tier,
                vector_candidates=resolution.candidates,
            )
        )

    if unresolved_names:
        logger.info(
            "board.extract.unresolved_players news_item_id=%s count=%d names=%s",
            news_item_id,
            len(unresolved_names),
            unresolved_names[:10],
        )

    if not entries_in:
        logger.warning(
            "board.extract.no_entries_to_persist news_item_id=%s url=%s",
            news_item_id,
            news_item.url,
        )
        return None

    # 4. Persist via the existing CRUD service.
    #    For a MOCK_DRAFT the kind-shape constraint requires num_rounds; we
    #    derive it from the extracted pick positions since the article only
    #    gives us the ranking (canonical round/team data lives in the
    #    draft-order reference, not the article). BIG_BOARD passes None.
    num_rounds = (
        _derive_num_rounds([e.position for e in entries_in])
        if kind is BoardKind.MOCK_DRAFT
        else None
    )
    published_at = extracted.published_at or news_item.published_at or datetime.utcnow()
    board = await board_service.create_board(
        db,
        kind=kind,
        num_rounds=num_rounds,
        news_source_id=news_item.source_id,
        draft_year=extracted.draft_year,
        published_at=published_at,
        entries=entries_in,
        news_item_id=news_item_id,
    )
    logger.info(
        "board.extract.created board_id=%s news_item_id=%s entries=%d unresolved=%d",
        board.id,
        news_item_id,
        len(entries_in),
        len(unresolved_names),
    )
    return board


# --- Article fetching -----------------------------------------------------


class ArticleFetcher(Protocol):
    async def __call__(self, url: str) -> str: ...


async def _default_fetcher(url: str) -> str:
    """Top-level article fetcher.

    Routes Substack URLs through the public JSON API (the rendered HTML
    contains only chrome/teaser markup) and falls through to a generic
    public-HTML scrape for non-Substack hosts.

    Raises ``PaywallDetectedError`` when the source signals the article
    is gated — for Substack via the ``audience`` API field, for other
    hosts via schema.org JSON-LD metadata.
    """
    if _substack_api_url(url) is not None:
        return await _fetch_substack_api(url)

    html = await _http_get(url)
    if is_paywalled(html):
        raise PaywallDetectedError(
            "Article is paywalled per schema.org isAccessibleForFree; "
            "refusing to extract from a teaser."
        )
    return extract_article_text(html)


def _substack_api_url(url: str) -> Optional[str]:
    """Translate a Substack post URL to its public API endpoint.

    Handles two Substack URL shapes:

    - Canonical publication URL: ``https://<pub>.substack.com/p/<slug>``
    - Share-link URL: ``https://open.substack.com/pub/<pub>/p/<slug>``
      (generated by Substack's share button — common in RSS feeds and
      social shares; resolves to the same post).

    Returns ``None`` for any URL that isn't one of those shapes — custom
    domains, non-Substack hosts, the bare ``substack.com`` apex, or
    Substack URLs that don't carry a ``/p/<slug>`` post path. The API
    URL drops any query string — the JSON endpoint doesn't accept one
    and including it returns 404.
    """
    if not url.startswith(("http://", "https://")):
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if not host.endswith(_SUBSTACK_HOST_SUFFIX):
        return None

    path_parts = parsed.path.rstrip("/").split("/")

    # ``open.substack.com`` is a share-link router, not a publication host.
    # It only serves posts under /pub/<publication>/p/<slug>; the canonical
    # /p/<slug> form is invalid on this host and would generate a 404 against
    # the API. Handle it separately and never fall through.
    if host == "open.substack.com":
        if (
            len(path_parts) >= 5
            and path_parts[1] == "pub"
            and path_parts[2]
            and path_parts[3] == "p"
            and path_parts[4]
        ):
            publication = path_parts[2]
            slug = path_parts[4]
            return (
                f"{parsed.scheme}://{publication}.substack.com" f"/api/v1/posts/{slug}"
            )
        return None

    # Canonical publication form: /p/<slug>
    # After rstrip+split that's ["", "p", "<slug>"].
    if len(path_parts) >= 3 and path_parts[1] == "p" and path_parts[2]:
        slug = path_parts[2]
        return f"{parsed.scheme}://{host}/api/v1/posts/{slug}"

    return None


async def _fetch_substack_api(url: str) -> str:
    """Fetch a Substack post via its public JSON API.

    Returns the cleaned body text extracted from ``body_html``. Raises
    ``PaywallDetectedError`` when ``audience`` is anything but
    ``"everyone"`` or when ``free_unlock_required`` is set. Raises
    ``BoardExtractionError`` for non-Substack URLs, malformed payloads,
    or empty bodies.
    """
    api_url = _substack_api_url(url)
    if api_url is None:
        raise BoardExtractionError(f"Not a Substack post URL: {url}")

    raw = await _http_get(api_url)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BoardExtractionError(
            f"Substack API returned non-JSON for {api_url}: {exc.msg}"
        ) from exc
    if not isinstance(payload, dict):
        raise BoardExtractionError(
            f"Substack API returned non-object payload for {api_url}"
        )

    audience = payload.get("audience")
    if audience is not None and audience != "everyone":
        raise PaywallDetectedError(
            f"Substack post audience={audience!r}; refusing to extract from a "
            "gated article."
        )
    if payload.get("free_unlock_required") is True:
        raise PaywallDetectedError(
            "Substack post requires a free unlock; treating as paywalled."
        )

    body_html = payload.get("body_html") or ""
    if not body_html:
        raise BoardExtractionError(
            f"Substack API returned empty body_html for {api_url}"
        )
    return extract_article_text(body_html)


async def _http_get(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        raise BoardExtractionError(f"Refusing to fetch non-http(s) URL: {url}")
    async with httpx.AsyncClient(
        timeout=_PAGE_FETCH_TIMEOUT,
        headers=_PAGE_FETCH_HEADERS,
        follow_redirects=True,
    ) as client:
        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # DNS/timeout/connect failures and 4xx/5xx responses surface as
            # httpx.HTTPError; wrap them in the documented BoardExtractionError
            # contract so callers don't see a raw transport error (or a 500).
            raise BoardExtractionError(f"Failed to fetch {url}: {exc}") from exc
        return response.text


def extract_article_text(html: str) -> str:
    """Pull the main article body out of a Substack page's HTML.

    Strips scripts, styles, nav, footer, and aside content; collapses
    whitespace; truncates to ``_MAX_ARTICLE_CHARS``.

    Pure function — no IO. Paywall detection lives in ``is_paywalled``;
    this function only does body extraction so it can run safely on any
    HTML (including partial / paywalled responses for inspection).
    """
    soup = BeautifulSoup(html, "html.parser")

    for tag_name in ("script", "style", "nav", "footer", "aside", "noscript"):
        for tag in soup.find_all(tag_name):
            tag.decompose()

    body = None
    for selector in _SUBSTACK_BODY_SELECTORS:
        body = soup.select_one(selector)
        if body is not None:
            break

    if body is None:
        # As a last resort, use the whole document. This is usually wrong
        # for Substack but keeps the function honest for non-Substack pages.
        body = soup.body or soup

    text_parts: list[str] = []
    for node in body.find_all(["h1", "h2", "h3", "p", "li"]):
        line = node.get_text(" ", strip=True)
        if line:
            text_parts.append(line)

    text = "\n".join(text_parts)
    return text[:_MAX_ARTICLE_CHARS]


def is_paywalled(html: str) -> bool:
    """Return True if the page's schema.org JSON-LD declares the article gated.

    Substack (like most modern publishers) emits a
    ``<script type="application/ld+json">`` block with a ``NewsArticle``
    document. The schema.org-standard ``isAccessibleForFree: false``
    flag is the canonical "this is paywalled" signal — far more reliable
    than CSS class names or copy keywords, which change with theme tweaks.

    Returns False when no JSON-LD block exists or no NewsArticle blob
    sets the flag to False (i.e., the page is free or the marker is
    absent — both are "do not block" cases).

    Pure function — no IO. Unit-tested against fixture HTML.
    """
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        # JSON-LD can be a single object or a list (or a @graph collection).
        for blob in _iter_json_ld_blobs(payload):
            if not isinstance(blob, dict):
                continue
            # JSON-LD allows ``@type`` to be a single string OR a list of
            # strings (e.g. ``["NewsArticle", "Article"]``). Handle both.
            type_value = blob.get("@type")
            type_strings = type_value if isinstance(type_value, list) else [type_value]
            if "NewsArticle" not in type_strings:
                continue
            # schema.org default is "true" when omitted, so only treat
            # explicit "false" (or stringified equivalents) as gated.
            value = blob.get("isAccessibleForFree")
            if value is False or (isinstance(value, str) and value.lower() == "false"):
                return True
    return False


def _iter_json_ld_blobs(payload: object):
    """Yield every JSON-LD blob in a payload (handles single, list, @graph)."""
    if isinstance(payload, list):
        for item in payload:
            yield from _iter_json_ld_blobs(item)
        return
    if isinstance(payload, dict):
        if "@graph" in payload and isinstance(payload["@graph"], list):
            yield from _iter_json_ld_blobs(payload["@graph"])
        yield payload


# --- Gemini extraction ----------------------------------------------------


_RANKING_EXTRACTION_PROMPT = """You are a structured-data extractor for DraftGuru, an NBA Draft analytics site.

You will be given the cleaned body text of an NBA Draft analyst's article that ranks college / international prospects. The article may be framed either as a "big board" (a pure talent ranking) or as a "mock draft" (a projected pick order, often phrased as "Team X selects Player Y" or "1. Team — Player"). Treat both identically: extract the analyst's ORDERED LIST OF PLAYERS into the structured response schema you have been given.

Rules:
- "rank" is the player's position in the ordered list (1, 2, 3, ...). For a big board this is the talent rank; for a mock draft this is the pick number. Either way it is just the 1-based order in which the analyst presents the players.
- Extract ONLY the player at each position. Ignore the team making the pick, trade notes, and round labels — DraftGuru sources team/pick ownership separately and does not need them from this article.
- "tier" is only present when the analyst explicitly groups players into tiers (Tier 1, Tier 2, ...). Otherwise leave it null. Do NOT use a mock draft's round (Round 1 / Round 2) as a tier.
- Extract EVERY ranked position in the list. Use whatever name the analyst writes at that position — partial names like "Mara" or "Bronny James Jr." are acceptable; downstream resolution handles them.
- Skip honorable-mention sections, "watch list" addenda, NBA veterans referenced for comparison, and analyst self-references.
- Only include players from the analyst's RANKED/PICK LIST, in the order the analyst presents them. Do not include incidental player mentions from the surrounding commentary.
- If you cannot identify a coherent ordered list of prospects, return an empty entries list.
- "draft_year" is the draft year the article is ranking for. Infer it from headers, context, or default to the current calendar year if ambiguous.
- "published_at" should be the article's publication date if discoverable; otherwise null.
"""


def _build_extraction_schema() -> types.Schema:
    """OpenAPI-ish schema sent to Gemini's structured-output decoder.

    Built by hand rather than introspected from ``ExtractedBoard`` so we
    can attach strong field ``description`` text — the description on
    ``player_name`` is the single most important load-bearing piece of
    instruction tuning for prose-heavy big boards, where analysts often
    structure the ranking as bare-surname headers and rely on context
    introduced earlier in the article to disambiguate.
    """
    return types.Schema(
        type=types.Type.OBJECT,
        required=["draft_year", "entries"],
        properties={
            "draft_year": types.Schema(
                type=types.Type.INTEGER,
                description=(
                    "The draft year this board is ranking prospects for. "
                    "Infer from headers/context; if ambiguous, use the "
                    "current calendar year."
                ),
            ),
            "published_at": types.Schema(
                type=types.Type.STRING,
                nullable=True,
                description=(
                    "ISO 8601 publication date of the article if "
                    "discoverable from the text; null otherwise."
                ),
            ),
            "entries": types.Schema(
                type=types.Type.ARRAY,
                description=(
                    "The analyst's ranked list, in order, one entry per "
                    "ranked position. Exclude honorable mentions, watch "
                    "lists, and any player mentioned only as a comp."
                ),
                items=types.Schema(
                    type=types.Type.OBJECT,
                    required=["player_name", "rank"],
                    properties={
                        "player_name": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "The player's name exactly as it appears "
                                "at the ranked position in the article. "
                                "Partial or surname-only forms are "
                                "acceptable (e.g., 'Mara', 'Bronny James Jr.'); "
                                "downstream resolution handles them. "
                                "Never skip a ranked position — always "
                                "emit whatever name the analyst uses there."
                            ),
                        ),
                        "rank": types.Schema(
                            type=types.Type.INTEGER,
                            description=(
                                "The analyst's 1-based position in the "
                                "ranking, not a pick projection."
                            ),
                        ),
                        "tier": types.Schema(
                            type=types.Type.INTEGER,
                            nullable=True,
                            description=(
                                "Only set when the analyst explicitly "
                                "groups players into tiers (Tier 1, "
                                "Tier 2, ...). Never inferred."
                            ),
                        ),
                    },
                ),
            ),
        },
    )


async def _extract_via_gemini(
    article_text: str,
    *,
    client: Optional[genai.Client] = None,
) -> ExtractedBoard:
    """Send the article body to Gemini, parse the structured response."""
    if not article_text.strip():
        raise BoardExtractionError("Empty article text — nothing to extract.")

    gemini_client = client or _build_gemini_client()

    try:
        response = await asyncio.wait_for(
            gemini_client.aio.models.generate_content(
                model=_GEMINI_MODEL,
                contents=types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=article_text)],
                ),
                config=types.GenerateContentConfig(
                    system_instruction=[
                        types.Part.from_text(text=_RANKING_EXTRACTION_PROMPT)
                    ],
                    temperature=0.1,
                    response_mime_type="application/json",
                    response_schema=_build_extraction_schema(),
                ),
            ),
            timeout=_GEMINI_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise BoardExtractionError(
            f"Gemini call timed out after {_GEMINI_TIMEOUT_SECONDS}s"
        ) from exc

    raw_text = response.text or ""
    return parse_gemini_response(raw_text)


def parse_gemini_response(raw_text: str) -> ExtractedBoard:
    """Parse Gemini's JSON output into the typed DTO. Pure function."""
    cleaned = _strip_markdown_fences(raw_text).strip()
    if not cleaned:
        raise BoardExtractionError("Gemini returned an empty response.")

    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise BoardExtractionError(
            f"Gemini response was not valid JSON: {exc.msg}"
        ) from exc

    try:
        return ExtractedBoard.model_validate(payload)
    except ValidationError as exc:
        raise BoardExtractionError(
            f"Gemini response did not match expected schema: {exc}"
        ) from exc


_MARKDOWN_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _strip_markdown_fences(text: str) -> str:
    """Strip ```json ... ``` fences in case the model ignored the instruction to emit raw JSON."""
    return _MARKDOWN_FENCE_RE.sub("", text)


def _build_gemini_client() -> genai.Client:
    api_key = settings.gemini_summarization_api_key or settings.gemini_api_key
    if not api_key:
        raise BoardExtractionError(
            "GEMINI_SUMMARIZATION_API_KEY or GEMINI_API_KEY must be configured"
        )
    return genai.Client(api_key=api_key)


# --- Persistence helpers --------------------------------------------------


# NBA drafts run two rounds of ~30 picks. A mock that projects past the first
# round is a two-round mock; anything that stops at or before pick 30 is a
# single-round mock. This value is metadata only — the consensus engine never
# reads it, and canonical round/pick data lives in the draft-order reference —
# so an approximate derivation from the extracted pick count is sufficient to
# satisfy the kind-shape constraint.
_FIRST_ROUND_PICKS = 30


def _derive_num_rounds(positions: list[int]) -> int:
    """Infer a mock draft's round count from its extracted pick positions.

    Returns 2 when any pick projects past the first round, else 1. Defaults
    to 1 for an empty list (defensive — ``extract_board`` returns before
    persisting when there are no entries).

    Pure function — unit-tested.
    """
    if not positions:
        return 1
    return 2 if max(positions) > _FIRST_ROUND_PICKS else 1


async def _existing_board_for_news_item(
    db: AsyncSession,
    *,
    news_item_id: int,
    kind: BoardKind,
) -> Optional[Board]:
    """Return the most recent Board for this news item + kind, if any."""
    stmt = (
        select(Board)
        .where(Board.news_item_id == news_item_id)  # type: ignore[arg-type]
        .where(Board.kind == kind)  # type: ignore[arg-type]
        .order_by(Board.created_at.desc())  # type: ignore[attr-defined]
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


_NAME_SUFFIX_RE = re.compile(
    r"\s+(jr|sr|ii|iii|iv|v)\.?\s*$",
    re.IGNORECASE,
)


def normalize_player_name(name: str) -> str:
    """Return a canonical comparison form of a player name.

    Folds Unicode diacritics (NFKD then ASCII-strip), strips trailing
    suffix tokens (Jr., Sr., II, III, IV, V), collapses internal
    whitespace, and lowercases. Used on both sides of the lookup so a
    "Théo Maledon" DB row matches a "Theo Maledon" extracted name.

    Pure function — unit-tested.
    """
    if not name:
        return ""
    folded = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(ch for ch in folded if not unicodedata.combining(ch))
    without_suffix = _NAME_SUFFIX_RE.sub("", ascii_only)
    collapsed = re.sub(r"\s+", " ", without_suffix).strip()
    return collapsed.lower()


async def resolve_player(db: AsyncSession, raw_name: str) -> ResolutionResult:
    """Resolve a raw player name to a ``players_master`` id via a three-step cascade.

    Cascade order:
    1. **Exact** — normalized match on ``players_master.display_name``.
    2. **Alias** — normalized match on ``player_aliases.full_name``.
    3. **Vector** — embed ``raw_name`` and run a k-NN search against
       ``player_embeddings``; return the top-5 candidates for admin review,
       but **do not auto-resolve** — that is a deferred future ticket.

    The normalization applied in steps 1 and 2 (``normalize_player_name``)
    folds Unicode diacritics, strips trailing suffixes (Jr./Sr./II/III/IV/V),
    and lowercases so that "Théo Maledon" in the DB matches "Theo Maledon"
    from the extraction, and "Bronny James" matches "Bronny James Jr.".

    For ambiguous exact/alias matches (two players with the same name) the
    rule is: never guess.  Ambiguity falls through to the vector step so an
    admin can choose from the top-K candidates.

    Args:
        db: Async session; caller owns any open transaction.
        raw_name: The player name exactly as emitted by the extraction AI.

    Returns:
        A :class:`ResolutionResult` with:
        - ``player_id`` set and ``method`` EXACT/ALIAS when a unique match
          is found in the first two steps.
        - ``player_id=None``, ``method=UNRESOLVED``, and ``candidates``
          populated when the vector step runs (even if it returns 0 rows).
    """
    needle = normalize_player_name(raw_name)
    if not needle:
        return ResolutionResult(player_id=None, method=ResolutionMethod.UNRESOLVED)

    # Step 1: exact match on display_name.
    candidate = await _find_unique_normalized_match(
        db,
        column=PlayerMaster.display_name,  # type: ignore[arg-type]
        id_column=PlayerMaster.id,  # type: ignore[arg-type]
        needle=needle,
    )
    if candidate is not None:
        return ResolutionResult(player_id=candidate, method=ResolutionMethod.EXACT)

    # Step 2: alias match.
    alias_match = await _find_unique_normalized_match(
        db,
        column=PlayerAlias.full_name,  # type: ignore[arg-type]
        id_column=PlayerAlias.player_id,  # type: ignore[arg-type]
        needle=needle,
    )
    if alias_match is not None:
        return ResolutionResult(player_id=alias_match, method=ResolutionMethod.ALIAS)

    # Step 3: hybrid search (trigram lexical + cosine-distance vector).
    # Blending both signals surfaces bare/short-surname queries that pure vector
    # search misses.  Persist candidates regardless of score so an admin can
    # review them.  Do NOT auto-resolve — that is a follow-up ticket.
    try:
        hybrid_hits = await find_candidate_players(db, raw_name, k=5)
    except Exception:
        logger.exception(
            "board.resolve.hybrid_search_error raw_name=%r — returning UNRESOLVED",
            raw_name,
        )
        hybrid_hits = []

    candidates: Optional[list[dict]] = (  # type: ignore[type-arg]
        [
            {
                "player_id": hit.player_id,
                "display_name": hit.display_name,
                "score": round(hit.score, 6),
            }
            for hit in hybrid_hits
        ]
        if hybrid_hits
        else None
    )
    return ResolutionResult(
        player_id=None,
        method=ResolutionMethod.UNRESOLVED,
        candidates=candidates,
    )


async def _resolve_player_id(
    db: AsyncSession, raw_name: str
) -> tuple[Optional[int], board_service.ResolutionMethod]:
    """Thin compatibility wrapper around :func:`resolve_player`.

    Existing callers that expect a ``(player_id, ResolutionMethod)`` tuple
    can continue using this function unchanged.  The vector step is NOT
    invoked here to avoid unexpected embedding calls from callers that are
    not prepared to handle candidates.

    .. deprecated::
        Prefer :func:`resolve_player` for new call sites.
    """
    needle = normalize_player_name(raw_name)
    if not needle:
        return None, board_service.ResolutionMethod.UNRESOLVED

    candidate = await _find_unique_normalized_match(
        db,
        column=PlayerMaster.display_name,  # type: ignore[arg-type]
        id_column=PlayerMaster.id,  # type: ignore[arg-type]
        needle=needle,
    )
    if candidate is not None:
        return candidate, board_service.ResolutionMethod.EXACT

    alias_match = await _find_unique_normalized_match(
        db,
        column=PlayerAlias.full_name,  # type: ignore[arg-type]
        id_column=PlayerAlias.player_id,  # type: ignore[arg-type]
        needle=needle,
    )
    if alias_match is not None:
        return alias_match, board_service.ResolutionMethod.ALIAS

    return None, board_service.ResolutionMethod.UNRESOLVED


async def _find_unique_normalized_match(
    db: AsyncSession,
    *,
    column,
    id_column,
    needle: str,
) -> Optional[int]:
    """Return the id whose normalized ``column`` value equals ``needle``.

    Returns ``None`` if zero or multiple candidates match.

    No SQL-side prefix filter: a prefix like ``LIKE 'e%'`` would miss
    rows whose first character is a diacritic (e.g. "Éric" → lower form
    "éric" → does not satisfy ``LIKE 'e%'``), even though the normalized
    needle starts with ``e``. The candidate tables (``players_master``,
    ``player_aliases``) are small enough that scanning Python-side is
    cheap and unambiguously correct.
    """
    if not needle:
        return None

    stmt = select(id_column, column)
    result = await db.execute(stmt)

    matches: list[int] = []
    for row_id, name in result.all():
        if normalize_player_name(name) == needle:
            matches.append(int(row_id))
        if len(matches) > 1:
            # Ambiguous — bail out without guessing.
            return None

    return matches[0] if len(matches) == 1 else None
