"""Unit tests for the pure helpers in ``board_extraction_service``.

No DB, no network. Anything DB-bound lives in the integration suite.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.services import board_extraction_service
from app.services.board_extraction_service import (
    BoardExtractionError,
    ExtractedBoard,
    PaywallDetectedError,
    _build_extraction_schema,
    _fetch_substack_api,
    _substack_api_url,
    extract_article_text,
    is_paywalled,
    normalize_player_name,
    parse_gemini_response,
)


# --- extract_article_text -----------------------------------------------


def test_extract_article_text_pulls_substack_body() -> None:
    """A Substack-style page yields just the article body, no nav/footer."""
    html = """
    <html>
      <head><title>2026 Big Board</title></head>
      <body>
        <nav>Site nav</nav>
        <div class="body markup">
          <h1>2026 Big Board v3</h1>
          <p>Final update before the lottery.</p>
          <p>1. Cooper Flagg, Duke (Tier 1)</p>
          <p>2. Dylan Harper, Rutgers (Tier 1)</p>
        </div>
        <footer>Footer text</footer>
      </body>
    </html>
    """
    text = extract_article_text(html)
    assert "Cooper Flagg" in text
    assert "Dylan Harper" in text
    assert "Site nav" not in text
    assert "Footer text" not in text


def test_extract_article_text_falls_back_to_article_tag() -> None:
    """When the Substack-specific selector misses, the <article> tag wins."""
    html = """
    <html><body>
      <article>
        <h1>The Top 30</h1>
        <p>1. Player A</p>
        <p>2. Player B</p>
      </article>
    </body></html>
    """
    text = extract_article_text(html)
    assert "Player A" in text
    assert "Player B" in text


def test_extract_article_text_strips_scripts_and_styles() -> None:
    """Inline JS / CSS never appears in the extracted text."""
    html = """
    <html><body>
      <article>
        <script>window.injected = 'bad'</script>
        <style>.x { color: red; }</style>
        <p>Real content.</p>
      </article>
    </body></html>
    """
    text = extract_article_text(html)
    assert "window.injected" not in text
    assert "color: red" not in text
    assert "Real content." in text


def test_extract_article_text_truncates_long_input() -> None:
    """Articles longer than the cap are truncated, not raised on."""
    big_paragraph = "Player " * 20_000
    html = f"<html><body><article><p>{big_paragraph}</p></article></body></html>"
    text = extract_article_text(html)
    assert len(text) <= 30_000


# --- is_paywalled (structural JSON-LD detection) ------------------------


def _ld_html(is_free: object) -> str:
    """Build an HTML fragment with a JSON-LD NewsArticle blob."""
    return f"""
    <html><head>
      <script type="application/ld+json">
      {{
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": "2026 Big Board",
        "isAccessibleForFree": {json_value(is_free)}
      }}
      </script>
    </head><body><article><p>Body content.</p></article></body></html>
    """


def json_value(v: object) -> str:
    """Render a Python value the way it would appear in JSON."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return f'"{v}"'
    return repr(v)


def test_is_paywalled_returns_false_for_free_post() -> None:
    assert is_paywalled(_ld_html(True)) is False


def test_is_paywalled_returns_true_for_paid_post() -> None:
    assert is_paywalled(_ld_html(False)) is True


def test_is_paywalled_handles_stringified_false() -> None:
    """Some publishers emit booleans as strings; recognize both."""
    assert is_paywalled(_ld_html("false")) is True
    assert is_paywalled(_ld_html("False")) is True


def test_is_paywalled_returns_false_when_flag_absent() -> None:
    """Default schema.org behavior: missing flag means free."""
    html = """
    <html><head>
      <script type="application/ld+json">
        {"@context":"https://schema.org","@type":"NewsArticle","headline":"X"}
      </script>
    </head><body><p>Content.</p></body></html>
    """
    assert is_paywalled(html) is False


def test_is_paywalled_returns_false_when_no_json_ld() -> None:
    """Pages with no JSON-LD aren't treated as paywalled by default."""
    html = "<html><body><article><p>Just a plain page.</p></article></body></html>"
    assert is_paywalled(html) is False


def test_is_paywalled_handles_graph_form() -> None:
    """JSON-LD can wrap multiple documents in @graph."""
    html = """
    <html><head>
      <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@graph": [
            {"@type": "WebSite", "name": "Site"},
            {"@type": "NewsArticle", "isAccessibleForFree": false}
          ]
        }
      </script>
    </head><body></body></html>
    """
    assert is_paywalled(html) is True


def test_is_paywalled_ignores_non_newsarticle_types() -> None:
    """A WebPage with isAccessibleForFree=false shouldn't trigger
    the gate; only NewsArticle counts (that's the doc the article body
    lives in)."""
    html = """
    <html><head>
      <script type="application/ld+json">
        {"@type":"WebPage","isAccessibleForFree":false}
      </script>
    </head></html>
    """
    assert is_paywalled(html) is False


# --- normalize_player_name ----------------------------------------------


def test_normalize_player_name_basic_lowercase() -> None:
    assert normalize_player_name("Cooper Flagg") == "cooper flagg"


def test_normalize_player_name_strips_diacritics() -> None:
    assert normalize_player_name("Théo Maledon") == "theo maledon"


def test_normalize_player_name_strips_suffix() -> None:
    assert normalize_player_name("Bronny James Jr.") == "bronny james"
    assert normalize_player_name("Bronny James Jr") == "bronny james"
    assert normalize_player_name("Tim Hardaway Sr.") == "tim hardaway"
    assert normalize_player_name("Robert Williams III") == "robert williams"


def test_normalize_player_name_collapses_whitespace() -> None:
    assert normalize_player_name("  Cooper   Flagg  ") == "cooper flagg"


def test_normalize_player_name_empty_input() -> None:
    assert normalize_player_name("") == ""
    assert normalize_player_name("   ") == ""


def test_normalize_player_name_unchanged_for_ascii_no_suffix() -> None:
    """The common case: lowercase + trim, otherwise unchanged."""
    assert normalize_player_name("Dylan Harper") == "dylan harper"


# --- parse_gemini_response ----------------------------------------------


def test_parse_gemini_response_happy_path() -> None:
    raw = """
    {
      "draft_year": 2026,
      "published_at": "2026-03-15",
      "entries": [
        {"player_name": "Cooper Flagg", "rank": 1, "tier": 1},
        {"player_name": "Dylan Harper", "rank": 2, "tier": 1}
      ]
    }
    """
    parsed = parse_gemini_response(raw)
    assert parsed.draft_year == 2026
    assert len(parsed.entries) == 2
    assert parsed.entries[0].player_name == "Cooper Flagg"
    assert parsed.entries[0].tier == 1


def test_parse_gemini_response_strips_markdown_fences() -> None:
    """Gemini sometimes ignores the no-fences rule; we still cope."""
    raw = """```json
    {"draft_year": 2026, "entries": []}
    ```"""
    parsed = parse_gemini_response(raw)
    assert parsed.draft_year == 2026
    assert parsed.entries == []


def test_parse_gemini_response_rejects_non_json() -> None:
    with pytest.raises(BoardExtractionError):
        parse_gemini_response("definitely not json")


def test_parse_gemini_response_rejects_bad_schema() -> None:
    """A JSON payload missing required fields fails validation."""
    with pytest.raises(BoardExtractionError):
        parse_gemini_response('{"entries": []}')  # missing draft_year


def test_parse_gemini_response_rejects_zero_rank() -> None:
    """Rank must be 1-based per the schema."""
    raw = '{"draft_year": 2026, "entries": [{"player_name": "X", "rank": 0}]}'
    with pytest.raises(BoardExtractionError):
        parse_gemini_response(raw)


def test_parse_gemini_response_empty_response_raises() -> None:
    with pytest.raises(BoardExtractionError):
        parse_gemini_response("")


# --- ExtractedBoard timezone normalization ------------------------------


def test_extracted_board_strips_tzinfo_from_utc_z() -> None:
    """A Z-suffixed ISO string lands as a naive UTC datetime."""
    board = ExtractedBoard.model_validate(
        {"draft_year": 2026, "published_at": "2026-03-15T00:00:00Z", "entries": []}
    )
    assert board.published_at is not None
    assert board.published_at.tzinfo is None
    assert board.published_at == datetime(2026, 3, 15, 0, 0, 0)


def test_extracted_board_strips_tzinfo_from_explicit_offset() -> None:
    """A datetime with an explicit non-UTC offset is converted to naive UTC."""
    board = ExtractedBoard.model_validate(
        {"draft_year": 2026, "published_at": "2026-03-15T08:00:00+04:00", "entries": []}
    )
    assert board.published_at is not None
    assert board.published_at.tzinfo is None
    # 08:00 +04:00 == 04:00 UTC
    assert board.published_at == datetime(2026, 3, 15, 4, 0, 0)


def test_extracted_board_keeps_already_naive_datetime() -> None:
    """A naive datetime passes through untouched."""
    board = ExtractedBoard.model_validate(
        {"draft_year": 2026, "published_at": "2026-03-15T12:30:00", "entries": []}
    )
    assert board.published_at == datetime(2026, 3, 15, 12, 30, 0)
    assert board.published_at.tzinfo is None  # type: ignore[union-attr]


def test_extracted_board_published_at_none_is_preserved() -> None:
    board = ExtractedBoard.model_validate({"draft_year": 2026, "entries": []})
    assert board.published_at is None


# --- is_paywalled with @type as a list ----------------------------------


def test_is_paywalled_accepts_type_as_list_with_newsarticle() -> None:
    """JSON-LD validly emits ``@type`` as a list; recognize NewsArticle inside."""
    html = """
    <html><head>
      <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": ["NewsArticle", "Article"],
          "isAccessibleForFree": false
        }
      </script>
    </head><body></body></html>
    """
    assert is_paywalled(html) is True


def test_is_paywalled_type_list_without_newsarticle_is_ignored() -> None:
    """A list of types that doesn't include NewsArticle isn't gated."""
    html = """
    <html><head>
      <script type="application/ld+json">
        {
          "@type": ["BlogPosting", "Article"],
          "isAccessibleForFree": false
        }
      </script>
    </head></html>
    """
    assert is_paywalled(html) is False


# --- _substack_api_url --------------------------------------------------


def test_substack_api_url_translates_basic_post_url() -> None:
    """A canonical Substack post URL maps to the JSON API endpoint."""
    assert (
        _substack_api_url("https://edemirnba.substack.com/p/2026-big-board")
        == "https://edemirnba.substack.com/api/v1/posts/2026-big-board"
    )


def test_substack_api_url_drops_query_string() -> None:
    """Query strings are dropped — the API endpoint rejects them."""
    assert (
        _substack_api_url(
            "https://edemirnba.substack.com/p/2026-big-board?utm_source=share"
        )
        == "https://edemirnba.substack.com/api/v1/posts/2026-big-board"
    )


def test_substack_api_url_handles_trailing_slash() -> None:
    """Trailing slashes on the post URL are stripped before slug extraction."""
    assert (
        _substack_api_url("https://edemirnba.substack.com/p/2026-big-board/")
        == "https://edemirnba.substack.com/api/v1/posts/2026-big-board"
    )


def test_substack_api_url_preserves_http_scheme() -> None:
    """Scheme is preserved (http stays http, https stays https)."""
    assert (
        _substack_api_url("http://edemirnba.substack.com/p/big-board")
        == "http://edemirnba.substack.com/api/v1/posts/big-board"
    )


def test_substack_api_url_returns_none_for_non_substack_host() -> None:
    """Non-Substack hosts (including custom domains) return None."""
    assert _substack_api_url("https://example.com/p/2026-big-board") is None
    assert _substack_api_url("https://dunksandthrees.com/p/big-board") is None


def test_substack_api_url_returns_none_for_substack_apex() -> None:
    """The bare ``substack.com`` apex isn't a publication; ignore it."""
    assert _substack_api_url("https://substack.com/p/big-board") is None


def test_substack_api_url_returns_none_for_non_post_path() -> None:
    """Substack home pages and archive paths don't have ``/p/<slug>``."""
    assert _substack_api_url("https://edemirnba.substack.com/") is None
    assert _substack_api_url("https://edemirnba.substack.com/archive") is None


def test_substack_api_url_returns_none_for_non_http_scheme() -> None:
    """Refuse non-http(s) schemes outright."""
    assert _substack_api_url("ftp://edemirnba.substack.com/p/big-board") is None
    assert _substack_api_url("file:///etc/passwd") is None


def test_substack_api_url_case_insensitive_host() -> None:
    """Host comparison is case-insensitive (URLs from RSS sometimes upper-case)."""
    assert (
        _substack_api_url("https://Edemirnba.Substack.com/p/big-board")
        == "https://edemirnba.substack.com/api/v1/posts/big-board"
    )


def test_substack_api_url_translates_open_substack_share_link() -> None:
    """Share-link URLs on ``open.substack.com`` resolve to the publication's API.

    Substack's share button generates ``open.substack.com/pub/<pub>/p/<slug>``
    URLs that RSS feeds and social posts commonly carry. Without this branch
    they fall through to the HTML scraper and silently lose the article body.
    """
    assert (
        _substack_api_url(
            "https://open.substack.com/pub/edemirnba/p/2026-big-board"
        )
        == "https://edemirnba.substack.com/api/v1/posts/2026-big-board"
    )


def test_substack_api_url_open_share_link_drops_query_and_trailing_slash() -> None:
    """Share links arrive from clients with utm tags and trailing slashes."""
    assert (
        _substack_api_url(
            "https://open.substack.com/pub/edemirnba/p/2026-big-board/?r=abc"
        )
        == "https://edemirnba.substack.com/api/v1/posts/2026-big-board"
    )


def test_substack_api_url_rejects_open_host_without_pub_path() -> None:
    """``open.substack.com`` paths that don't carry /pub/<pub>/p/<slug> are not posts."""
    assert _substack_api_url("https://open.substack.com/") is None
    assert _substack_api_url("https://open.substack.com/pub/edemirnba") is None
    assert (
        _substack_api_url("https://open.substack.com/pub/edemirnba/p/") is None
    )
    assert _substack_api_url("https://open.substack.com/p/2026-big-board") is None


# --- _fetch_substack_api ------------------------------------------------


def _fake_http_get(payload: dict | str):
    """Return an async stub that yields ``payload`` serialised as JSON.

    If a string is passed it's returned verbatim — useful for malformed
    JSON tests.
    """

    async def _stub(url: str) -> str:
        if isinstance(payload, str):
            return payload
        return json.dumps(payload)

    return _stub


@pytest.mark.asyncio
async def test_fetch_substack_api_returns_body_text_for_free_post(
    monkeypatch,
) -> None:
    """An ``audience=everyone`` payload yields the cleaned body text."""
    payload = {
        "title": "2026 Big Board",
        "audience": "everyone",
        "free_unlock_required": False,
        "body_html": (
            "<div><p>1. Cooper Flagg, Duke</p>"
            "<p>2. Dylan Harper, Rutgers</p></div>"
        ),
    }
    monkeypatch.setattr(
        board_extraction_service, "_http_get", _fake_http_get(payload)
    )

    text = await _fetch_substack_api(
        "https://edemirnba.substack.com/p/2026-big-board"
    )
    assert "Cooper Flagg" in text
    assert "Dylan Harper" in text


@pytest.mark.asyncio
async def test_fetch_substack_api_raises_on_paid_audience(monkeypatch) -> None:
    """``audience=only_paid`` triggers the paywall path."""
    payload = {"audience": "only_paid", "body_html": "<p>teaser</p>"}
    monkeypatch.setattr(
        board_extraction_service, "_http_get", _fake_http_get(payload)
    )

    with pytest.raises(PaywallDetectedError):
        await _fetch_substack_api(
            "https://edemirnba.substack.com/p/2026-big-board"
        )


@pytest.mark.asyncio
async def test_fetch_substack_api_raises_on_only_free_audience(
    monkeypatch,
) -> None:
    """``audience=only_free`` is also gated (requires a free-tier login)."""
    payload = {"audience": "only_free", "body_html": "<p>teaser</p>"}
    monkeypatch.setattr(
        board_extraction_service, "_http_get", _fake_http_get(payload)
    )

    with pytest.raises(PaywallDetectedError):
        await _fetch_substack_api(
            "https://edemirnba.substack.com/p/2026-big-board"
        )


@pytest.mark.asyncio
async def test_fetch_substack_api_raises_on_free_unlock_required(
    monkeypatch,
) -> None:
    """``free_unlock_required`` is the secondary paywall signal."""
    payload = {
        "audience": "everyone",
        "free_unlock_required": True,
        "body_html": "<p>teaser</p>",
    }
    monkeypatch.setattr(
        board_extraction_service, "_http_get", _fake_http_get(payload)
    )

    with pytest.raises(PaywallDetectedError):
        await _fetch_substack_api(
            "https://edemirnba.substack.com/p/2026-big-board"
        )


@pytest.mark.asyncio
async def test_fetch_substack_api_rejects_non_substack_url() -> None:
    """The helper refuses to run on a URL it can't translate."""
    with pytest.raises(BoardExtractionError):
        await _fetch_substack_api("https://example.com/p/some-post")


@pytest.mark.asyncio
async def test_fetch_substack_api_raises_on_malformed_json(monkeypatch) -> None:
    """A non-JSON response from the API is a hard error, not silently swallowed."""
    monkeypatch.setattr(
        board_extraction_service,
        "_http_get",
        _fake_http_get("definitely not json"),
    )

    with pytest.raises(BoardExtractionError):
        await _fetch_substack_api(
            "https://edemirnba.substack.com/p/2026-big-board"
        )


@pytest.mark.asyncio
async def test_fetch_substack_api_raises_on_empty_body_html(monkeypatch) -> None:
    """An empty body is also a hard error — we can't extract from nothing."""
    payload = {"audience": "everyone", "body_html": ""}
    monkeypatch.setattr(
        board_extraction_service, "_http_get", _fake_http_get(payload)
    )

    with pytest.raises(BoardExtractionError):
        await _fetch_substack_api(
            "https://edemirnba.substack.com/p/2026-big-board"
        )


# --- _default_fetcher routing ------------------------------------------


@pytest.mark.asyncio
async def test_default_fetcher_routes_substack_url_through_api(monkeypatch) -> None:
    """Substack URLs hit ``_fetch_substack_api``, not the HTML scraper."""
    called = {"api": False, "html": False}

    async def _api_stub(url: str) -> str:
        called["api"] = True
        return "Cooper Flagg etc"

    async def _html_stub(url: str) -> str:
        called["html"] = True
        return "<html></html>"

    monkeypatch.setattr(board_extraction_service, "_fetch_substack_api", _api_stub)
    monkeypatch.setattr(board_extraction_service, "_http_get", _html_stub)

    text = await board_extraction_service._default_fetcher(
        "https://edemirnba.substack.com/p/2026-big-board"
    )
    assert text == "Cooper Flagg etc"
    assert called["api"] is True
    assert called["html"] is False


@pytest.mark.asyncio
async def test_default_fetcher_falls_back_to_html_for_non_substack(
    monkeypatch,
) -> None:
    """Non-Substack URLs go through the legacy HTML-scrape path."""
    called = {"api": False, "html": False}

    async def _api_stub(url: str) -> str:
        called["api"] = True
        return "should not be reached"

    async def _html_stub(url: str) -> str:
        called["html"] = True
        return (
            "<html><body><article><p>Real content here.</p>"
            "<p>Some prospect.</p></article></body></html>"
        )

    monkeypatch.setattr(board_extraction_service, "_fetch_substack_api", _api_stub)
    monkeypatch.setattr(board_extraction_service, "_http_get", _html_stub)

    text = await board_extraction_service._default_fetcher(
        "https://example.com/2026-big-board"
    )
    assert called["api"] is False
    assert called["html"] is True
    assert "Real content here" in text


# --- _build_extraction_schema -------------------------------------------


def test_build_extraction_schema_player_name_allows_partial() -> None:
    """The schema's player_name description must allow partial/surname-only names.

    This is the load-bearing piece of guidance sent to Gemini's structured-output
    decoder. The contract is recall-first: emit every ranked position using
    whatever name the analyst writes there; downstream resolution handles
    precision. Losing this (e.g., reverting to anti-surname language) silently
    reduces recall on prose-heavy boards.
    """
    schema = _build_extraction_schema()
    entries_items = schema.properties["entries"].items
    description = entries_items.properties["player_name"].description or ""
    # Must explicitly allow partial/surname-only names.
    assert "partial" in description.lower() or "surname" in description.lower()
    # Must NOT tell Gemini to omit entries or refuse partial names.
    assert "omit the entry" not in description.lower()
    assert "never emit" not in description.lower()
    assert "full first" not in description.lower()


def test_build_extraction_schema_requires_player_name_and_rank() -> None:
    """Both fields must be marked required so the decoder can't omit them."""
    schema = _build_extraction_schema()
    entries_items = schema.properties["entries"].items
    assert set(entries_items.required) >= {"player_name", "rank"}


def test_build_extraction_schema_tier_is_nullable() -> None:
    """``tier`` is opt-in — only set when analyst explicitly groups players."""
    schema = _build_extraction_schema()
    tier_field = schema.properties["entries"].items.properties["tier"]
    assert tier_field.nullable is True
