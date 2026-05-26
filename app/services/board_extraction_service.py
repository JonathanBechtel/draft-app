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
from datetime import datetime, timezone
from typing import Optional, Protocol

import httpx
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.schemas.boards import Board, BoardKind, BoardStatus
from app.schemas.news_items import NewsItem
from app.schemas.player_aliases import PlayerAlias
from app.schemas.players_master import PlayerMaster
from app.services import board_service

logger = logging.getLogger(__name__)

_PAGE_FETCH_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)
_PAGE_FETCH_HEADERS = {
    "User-Agent": "DraftGuru/1.0 (+https://draftguru.dev; board-extraction)",
}
_GEMINI_TIMEOUT_SECONDS = 60
_GEMINI_MODEL = "gemini-3-flash-preview"

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
        kind: Currently only ``BoardKind.BIG_BOARD`` is supported; mock-draft
            extraction is a follow-up ticket.
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
    if kind != BoardKind.BIG_BOARD:
        raise NotImplementedError(
            "MOCK_DRAFT extraction is a follow-up ticket; only BIG_BOARD "
            "extraction is supported in this iteration."
        )

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

    # 3. Resolve player names → players_master ids (drop unmatched, log them).
    entries_in: list[board_service.EntryInput] = []
    unmatched: list[str] = []
    seen_player_ids: set[int] = set()
    seen_positions: set[int] = set()
    for raw in extracted.entries:
        player_id = await _resolve_player_id(db, raw.player_name)
        if player_id is None:
            unmatched.append(raw.player_name)
            continue
        if player_id in seen_player_ids or raw.rank in seen_positions:
            # Gemini occasionally repeats a player or a rank; skip silently.
            continue
        seen_player_ids.add(player_id)
        seen_positions.add(raw.rank)
        entries_in.append(
            board_service.EntryInput(
                player_id=player_id, position=raw.rank, tier=raw.tier
            )
        )

    if unmatched:
        logger.info(
            "board.extract.unmatched_players news_item_id=%s count=%d names=%s",
            news_item_id,
            len(unmatched),
            unmatched[:10],
        )

    if not entries_in:
        logger.warning(
            "board.extract.no_resolvable_entries news_item_id=%s url=%s",
            news_item_id,
            news_item.url,
        )
        return None

    # 4. Persist via the existing CRUD service.
    published_at = extracted.published_at or news_item.published_at or datetime.utcnow()
    board = await board_service.create_board(
        db,
        news_source_id=news_item.source_id,
        draft_year=extracted.draft_year,
        published_at=published_at,
        entries=entries_in,
        news_item_id=news_item_id,
    )
    logger.info(
        "board.extract.created board_id=%s news_item_id=%s entries=%d unmatched=%d",
        board.id,
        news_item_id,
        len(entries_in),
        len(unmatched),
    )
    return board


# --- Article fetching -----------------------------------------------------


class ArticleFetcher(Protocol):
    async def __call__(self, url: str) -> str: ...


async def _default_fetcher(url: str) -> str:
    """Fetch a Substack article URL and return its cleaned body text.

    Raises ``PaywallDetectedError`` if the page's schema.org JSON-LD
    metadata signals that the article is gated.
    """
    html = await _http_get(url)
    if is_paywalled(html):
        raise PaywallDetectedError(
            "Article is paywalled per schema.org isAccessibleForFree; "
            "refusing to extract from a teaser."
        )
    return extract_article_text(html)


async def _http_get(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        raise BoardExtractionError(f"Refusing to fetch non-http(s) URL: {url}")
    async with httpx.AsyncClient(
        timeout=_PAGE_FETCH_TIMEOUT,
        headers=_PAGE_FETCH_HEADERS,
        follow_redirects=True,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
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


_BIG_BOARD_EXTRACTION_PROMPT = """You are a structured-data extractor for DraftGuru, an NBA Draft analytics site.

You will be given the cleaned body text of an NBA Draft analyst's "big board" article — a published, ordered ranking of college / international prospects.

Your job is to extract the ranking into a structured JSON payload. Be careful: the article often contains commentary, headers, and unrelated player references. Only include players that appear in the analyst's RANKED LIST, in the order the analyst presents them.

Output strict JSON matching this schema (do NOT wrap in markdown fences):

{
  "draft_year": <integer, e.g. 2026>,
  "published_at": <ISO 8601 date string or null>,
  "entries": [
    {"player_name": "<full name as written>", "rank": <integer, 1-based>, "tier": <integer or null>}
  ]
}

Rules:
- "rank" is the analyst's position in the ranking (1, 2, 3, ...), not the player's pick projection.
- "tier" is only present when the analyst explicitly groups players into tiers (Tier 1, Tier 2, ...). Otherwise use null.
- Use the player's full name exactly as it appears in the article (e.g., "Cooper Flagg", not "Flagg").
- Skip honorable-mention sections, "watch list" addenda, NBA veterans referenced for comparison, and analyst self-references.
- If you cannot identify a coherent ordered list of prospects, return entries: [].
- "draft_year" is the draft year the article is ranking for. Infer it from headers, context, or default to the current calendar year if ambiguous.
- "published_at" should be the article's publication date if discoverable; otherwise null.
"""


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
                        types.Part.from_text(text=_BIG_BOARD_EXTRACTION_PROMPT)
                    ],
                    temperature=0.1,
                    response_mime_type="application/json",
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


async def _resolve_player_id(db: AsyncSession, raw_name: str) -> Optional[int]:
    """Look up a PlayerMaster id by name; fall back to aliases.

    Strategy:
    1. Normalize the extracted name (``normalize_player_name``).
    2. Load candidate rows and compute the same normalization Python-side
       to find an exact match. We avoid the ``unaccent`` Postgres
       extension and don't apply a prefix prefilter (diacritic-leading
       names would be missed by a SQL ``LIKE`` on the normalized first
       letter).
    3. If exactly one candidate matches, return its id.
    4. If zero, try the same on ``player_aliases.full_name``.
    5. If multiple candidates match (e.g., two real prospects share a
       name), treat as unresolved — never guess. The follow-up ticket
       persists these for admin review.

    Returns ``None`` for empty input, no match, or ambiguous match.
    """
    needle = normalize_player_name(raw_name)
    if not needle:
        return None

    candidate = await _find_unique_normalized_match(
        db,
        column=PlayerMaster.display_name,  # type: ignore[arg-type]
        id_column=PlayerMaster.id,  # type: ignore[arg-type]
        needle=needle,
    )
    if candidate is not None:
        return candidate

    return await _find_unique_normalized_match(
        db,
        column=PlayerAlias.full_name,  # type: ignore[arg-type]
        id_column=PlayerAlias.player_id,  # type: ignore[arg-type]
        needle=needle,
    )


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
