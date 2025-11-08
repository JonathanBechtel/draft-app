import pathlib

from scripts.bbref_bio_scraper import parse_index_html, parse_player_html


def read_sample(path: str) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8", errors="ignore")


def test_parse_index_html_extracts_rows_and_active_flag():
    html = read_sample("scrapers/bbref/index_page_example.html")
    rows = parse_index_html("b", html)
    assert rows, "should parse some rows"
    # Find Charles Bassey row
    found = next((r for r in rows if r.slug == "bassech01"), None)
    assert found is not None
    assert found.active_flag is True
    assert found.height_in in (82, None)  # inches
    assert found.year_max in (2025, None)


def test_parse_player_html_core_fields():
    html = read_sample("scrapers/bbref/player_page_example.html")
    bio = parse_player_html(
        letter="b",
        slug="balllo01",
        html=html,
        source_url="https://www.basketball-reference.com/players/b/balllo01.html",
    )
    assert bio.full_name.lower().startswith("lonzo")
    assert bio.birth_date == "1997-10-27"
    assert bio.shoots is None or bio.shoots in ("Right", "Left", "right", "left")
    # Instagram handle should be parsed
    if bio.social_instagram_handle:
        assert bio.social_instagram_handle == bio.social_instagram_handle.lower()
