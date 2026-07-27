"""Unit tests for the bbref bio scrape loop (`bbref_scrape.scrape_letters`).

This is the fetch/cache/assemble half of the scraper — the half the Summer
League roster cron runs every hour during an event. Every test drives it
entirely from local HTML (``from_index_dir`` / ``from_player_dir`` /
``from_player_file``) or from a fake ``httpx`` client, so nothing here touches
the network or the shared ``data/scraper-cache`` tree.

The HTML is synthetic and small on purpose: these tests are about the *loop*
(which pages get read, which slugs get emitted, what the cache does), and the
checked-in 400KB real pages cost ~15s each to parse. Parsing fidelity against
genuine bbref markup is covered by ``test_bbref_bio_parser.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.player_bio import bbref_scrape
from app.services.player_bio.bbref_scrape import _fetch_player_html, scrape_letters


def _index_html(*slugs: str) -> str:
    """Build an index page listing ``slugs``, in bbref's table shape."""
    rows = "".join(
        f"""
        <tr>
          <th data-append-csv="{slug}">
            <strong><a href="/players/{slug[0]}/{slug}.html">Player {slug}</a></strong>
          </th>
          <td data-stat="pos">PG</td>
          <td data-stat="year_min">2018</td>
          <td data-stat="year_max">2025</td>
          <td data-stat="height" csk="78">6-6</td>
          <td data-stat="weight">190</td>
          <td data-stat="birth_date" csk="19971027">October 27, 1997</td>
          <td data-stat="colleges">UCLA</td>
        </tr>
        """
        for slug in slugs
    )
    return f"<html><body><table>{rows}</table></body></html>"


def _player_html(slug: str, *, name: str = "Lonzo Ball") -> str:
    """Build a player page whose canonical link points at ``slug``."""
    return f"""
    <html><body>
      <link rel="canonical"
            href="https://www.basketball-reference.com/players/{slug[0]}/{slug}.html">
      <h1>{name}</h1>
      <div id="meta">
        <p><strong>Position:</strong> Point Guard ▪ <strong>Shoots:</strong> Right</p>
        <p>6-6, 190lb</p>
        <p><strong>College:</strong>
           <a href="/friv/colleges.fcgi?college=ucla">UCLA</a></p>
        <p><strong>Draft:</strong> Los Angeles Lakers, 1st round (2nd pick, 2nd
           overall), <a href="/draft/NBA_2017.html">2017 NBA Draft</a></p>
      </div>
    </body></html>
    """


@pytest.fixture
def index_dir(tmp_path: Path) -> Path:
    """An index directory holding a two-player ``players_b.html``."""
    directory = tmp_path / "index"
    directory.mkdir()
    (directory / "players_b.html").write_text(
        _index_html("balllo01", "bassech01"), encoding="utf-8"
    )
    return directory


@pytest.fixture
def player_dir(tmp_path: Path) -> Path:
    """A player-page directory holding HTML for ``balllo01`` only."""
    directory = tmp_path / "players"
    directory.mkdir()
    (directory / "balllo01.html").write_text(_player_html("balllo01"), encoding="utf-8")
    return directory


def test_scrape_letters_reads_local_index_and_player_pages(
    index_dir: Path, player_dir: Path, tmp_path: Path
) -> None:
    """A letter scrape emits one row per index entry, enriched from player HTML."""
    rows = scrape_letters(
        letters=["b"],
        out_dir=tmp_path,
        throttle=0.0,
        from_index_dir=index_dir,
        from_player_dir=player_dir,
    )

    by_slug = {str(row["slug"]): row for row in rows}
    assert sorted(by_slug) == ["balllo01", "bassech01"]
    # Parsed off the player page, not the index row.
    assert by_slug["balllo01"]["draft_year"] == 2017
    assert by_slug["balllo01"]["draft_pick"] == 2
    assert by_slug["balllo01"]["school"] == "UCLA"
    assert by_slug["balllo01"]["full_name"] == "Lonzo Ball"
    # Index hints are carried onto every row.
    assert by_slug["bassech01"]["is_active_nba"] is True
    assert by_slug["bassech01"]["nba_last_season"] == "2024-25"


def test_scrape_letters_falls_back_to_index_hints_when_player_page_is_missing(
    index_dir: Path, tmp_path: Path
) -> None:
    """With no player HTML and no cached page, index height/weight still land.

    ``_fetch_player_html`` returns an empty string when the cache misses and
    the fetch fails, so the player-page parse yields nothing and the index row
    is the only source for these fields.
    """
    empty_player_dir = tmp_path / "no-players"
    empty_player_dir.mkdir()
    (empty_player_dir / "unrelated.html").write_text("<html></html>", encoding="utf-8")

    rows = scrape_letters(
        letters=["b"],
        out_dir=tmp_path,
        throttle=0.0,
        from_index_dir=index_dir,
        from_player_dir=empty_player_dir,
    )

    bassey = next(row for row in rows if row["slug"] == "bassech01")
    assert bassey["height_in"] == 78
    assert bassey["weight_lb"] == 190
    assert bassey["birth_date"] == "1997-10-27"
    assert bassey["position"] == "PG"
    assert bassey["full_name"] == "bassech01"  # no <h1>; falls back to the slug


def test_scrape_letters_falls_back_to_the_example_index_page(tmp_path: Path) -> None:
    """A missing ``players_{letter}.html`` falls back to ``index_page_example.html``."""
    directory = tmp_path / "index"
    directory.mkdir()
    (directory / "index_page_example.html").write_text(
        _index_html("balllo01"), encoding="utf-8"
    )

    rows = scrape_letters(
        letters=["z"],  # no players_z.html exists
        out_dir=tmp_path,
        throttle=0.0,
        from_index_dir=directory,
        from_player_dir=tmp_path / "missing",
    )

    assert [row["slug"] for row in rows] == ["balllo01"]


def test_scrape_letters_restricts_to_the_sample_slug_from_a_player_file(
    index_dir: Path, tmp_path: Path
) -> None:
    """``from_player_file`` scopes the run to the one player that file describes."""
    sample = tmp_path / "player_page_example.html"
    sample.write_text(_player_html("balllo01"), encoding="utf-8")

    rows = scrape_letters(
        letters=["b"],
        out_dir=tmp_path,
        throttle=0.0,
        from_index_dir=index_dir,
        from_player_file=sample,
    )

    assert [row["slug"] for row in rows] == ["balllo01"]


def test_scrape_letters_synthesizes_an_index_row_for_an_unlisted_sample(
    tmp_path: Path,
) -> None:
    """A sample player absent from the index still yields a row, built from the page."""
    directory = tmp_path / "index"
    directory.mkdir()
    (directory / "players_b.html").write_text(_index_html("someoneelse01"), "utf-8")
    sample = tmp_path / "player_page_example.html"
    sample.write_text(_player_html("balllo01"), encoding="utf-8")

    rows = scrape_letters(
        letters=["b"],
        out_dir=tmp_path,
        throttle=0.0,
        from_index_dir=directory,
        from_player_file=sample,
    )

    assert [row["slug"] for row in rows] == ["balllo01"]
    assert rows[0]["full_name"] == "Lonzo Ball"


def test_scrape_letters_scrapes_extra_slugs_not_in_any_index(
    player_dir: Path, tmp_path: Path
) -> None:
    """`extra_slugs` is the seam the SL cron uses: no letters, just cohort slugs."""
    rows = scrape_letters(
        letters=[],
        out_dir=tmp_path,
        throttle=0.0,
        from_player_dir=player_dir,
        extra_slugs=["balllo01", "BALLLO01", "  "],
    )

    # Case-folded and deduplicated; blank entries dropped.
    assert [row["slug"] for row in rows] == ["balllo01"]
    assert rows[0]["draft_pick"] == 2


def test_scrape_letters_does_not_rescrape_a_slug_already_seen_via_the_index(
    index_dir: Path, player_dir: Path, tmp_path: Path
) -> None:
    """A cohort slug the index already produced is not emitted twice."""
    rows = scrape_letters(
        letters=["b"],
        out_dir=tmp_path,
        throttle=0.0,
        from_index_dir=index_dir,
        from_player_dir=player_dir,
        extra_slugs=["balllo01"],
    )

    assert [row["slug"] for row in rows].count("balllo01") == 1


def test_scrape_letters_skips_extra_slugs_with_no_html(tmp_path: Path) -> None:
    """A slug whose page cannot be read is skipped, not emitted as an empty row."""
    empty_dir = tmp_path / "players"
    empty_dir.mkdir()
    # An existing-but-unrelated file keeps from_player_dir truthy without
    # providing HTML for the requested slug.
    (empty_dir / "someone_else.html").write_text("<html></html>", encoding="utf-8")

    rows = scrape_letters(
        letters=[],
        out_dir=tmp_path,
        throttle=0.0,
        from_player_dir=empty_dir,
        extra_slugs=["nobodyxx01"],
    )

    assert rows == []


def test_scrape_letters_fetches_and_caches_pages_when_nothing_is_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no local pages the loop fetches over HTTP and writes the cache."""
    monkeypatch.chdir(tmp_path)
    requested: list[str] = []

    class _FakeResponse:
        def __init__(self, text: str) -> None:
            self.text = text

        def raise_for_status(self) -> None:
            return None

    class _FakeClient:
        def get(self, url: str) -> _FakeResponse:
            requested.append(url)
            if url.endswith("/players/b/"):
                return _FakeResponse(_index_html("balllo01"))
            return _FakeResponse(_player_html("balllo01"))

    monkeypatch.setattr(bbref_scrape, "_client", lambda timeout=30.0: _FakeClient())

    rows = scrape_letters(letters=["b"], out_dir=tmp_path, throttle=0.0)

    assert [row["slug"] for row in rows] == ["balllo01"]
    assert requested == [
        "https://www.basketball-reference.com/players/b/",
        "https://www.basketball-reference.com/players/b/balllo01.html",
    ]
    cache = tmp_path / "data" / "scraper-cache"
    assert (cache / "players_b.html").exists()
    assert (cache / "players" / "balllo01.html").exists()


def test_scrape_letters_reuses_the_cached_index_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached index page is read from disk instead of re-fetched."""
    monkeypatch.chdir(tmp_path)
    cache = tmp_path / "data" / "scraper-cache"
    (cache / "players").mkdir(parents=True)
    (cache / "players_b.html").write_text(_index_html("balllo01"), encoding="utf-8")
    (cache / "players" / "balllo01.html").write_text(
        _player_html("balllo01"), encoding="utf-8"
    )

    class _ExplodingClient:
        def get(self, url: str) -> object:  # pragma: no cover - must not run
            raise AssertionError(f"cache hit must not fetch {url}")

    monkeypatch.setattr(
        bbref_scrape, "_client", lambda timeout=30.0: _ExplodingClient()
    )

    rows = scrape_letters(letters=["b"], out_dir=tmp_path, throttle=0.0)

    assert [row["slug"] for row in rows] == ["balllo01"]


def test_fetch_player_html_prefers_the_cache_over_the_network(tmp_path: Path) -> None:
    """A cached page short-circuits the fetch entirely — no client call."""

    class _ExplodingClient:
        def get(self, url: str) -> object:  # pragma: no cover - must not run
            raise AssertionError("cache hit must not hit the network")

    (tmp_path / "balllo01.html").write_text("<html>cached</html>", encoding="utf-8")

    html = _fetch_player_html(
        slug="balllo01",
        source_url="https://example.invalid/balllo01.html",
        cache_dir=tmp_path,
        client=_ExplodingClient(),  # type: ignore[arg-type]
        refresh=False,
        throttle=0.0,
        verbose=False,
    )

    assert html == "<html>cached</html>"


def test_fetch_player_html_falls_back_to_cache_when_the_fetch_fails(
    tmp_path: Path,
) -> None:
    """A failed refresh returns the stale cached copy rather than an empty page."""

    class _FailingClient:
        def get(self, url: str) -> object:
            raise RuntimeError("bbref down")

    (tmp_path / "balllo01.html").write_text("<html>stale</html>", encoding="utf-8")

    html = _fetch_player_html(
        slug="balllo01",
        source_url="https://example.invalid/balllo01.html",
        cache_dir=tmp_path,
        client=_FailingClient(),  # type: ignore[arg-type]
        refresh=True,
        throttle=0.0,
        verbose=False,
    )

    assert html == "<html>stale</html>"


def test_fetch_player_html_returns_empty_when_there_is_no_cache_and_no_client(
    tmp_path: Path,
) -> None:
    """Nothing to read and nothing to fetch yields an empty string, not a raise."""
    assert (
        _fetch_player_html(
            slug="nobodyxx01",
            source_url="https://example.invalid/nobodyxx01.html",
            cache_dir=tmp_path,
            client=None,
            refresh=False,
            throttle=0.0,
            verbose=False,
        )
        == ""
    )
