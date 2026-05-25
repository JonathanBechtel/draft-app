"""Unit tests for the pure helpers in ``board_extraction_service``.

No DB, no network. Anything DB-bound lives in the integration suite.
"""

from __future__ import annotations

import pytest

from app.services.board_extraction_service import (
    BoardExtractionError,
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
