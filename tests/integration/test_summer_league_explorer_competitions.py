"""Integration tests for the Competition Context Explorer tab (subject=competitions).

Exercises the public route, server-rendered states, the ``?partial=1`` fragment,
CSV parity, five-section detail, definitions/coverage/stale states, membership,
trend/table parity, and empty/invalid handling — all against the deterministic
seed fixture (contract §10) rather than ambient developer data.
"""

from __future__ import annotations

import csv
import io

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tests.integration.perf._capture import count_queries

from app.services.summer_league.environment_fixtures import (
    CompetitionContextSeed,
    seed_competition_context_demo,
)
from app.services.summer_league_environment_registry import (
    metrics_for_scope,
    sortable_metric_keys,
)
from app.services.summer_league_environment_service import competition_scope_key
from app.services.summer_league_explorer_service import parse_query, run_explorer_query

pytestmark = pytest.mark.asyncio

EXPLORER = "/stats/summer-league/explorer"


async def _seed(db: AsyncSession) -> CompetitionContextSeed:
    refs = await seed_competition_context_demo(db)
    await db.commit()
    return refs


# --------------------------------------------------------------------------- #
# Navigation / tabs
# --------------------------------------------------------------------------- #


async def test_five_tabs_render_with_competitions_active(app_client: AsyncClient) -> None:
    """The Competitions tab is present beside the four existing subjects and active."""
    resp = await app_client.get(f"{EXPLORER}?subject=competitions")
    assert resp.status_code == 200
    html = resp.text
    for label in ("Players", "Game Finder", "Teams", "Matchups", "Competitions"):
        assert f">{label}</a>" in html
    # Active tab is unambiguous to screen readers.
    assert 'href="/stats/summer-league/explorer?subject=competitions" aria-current="page"' in html
    assert "Competition Context" in html


async def test_other_subjects_have_no_competition_regression(app_client: AsyncClient) -> None:
    """Existing subjects still render their own controls, not competition ones."""
    resp = await app_client.get(f"{EXPLORER}?subject=players")
    assert resp.status_code == 200
    assert "slg-scope-toggle" not in resp.text  # competition-only control
    assert "Draft class" in resp.text  # player control still present


# --------------------------------------------------------------------------- #
# Scope controls / rows
# --------------------------------------------------------------------------- #


async def test_season_scope_one_row_per_year(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Season scope renders one row per calendar year across all competitions."""
    await _seed(db_session)
    resp = await app_client.get(f"{EXPLORER}?subject=competitions&profile_scope=season")
    assert resp.status_code == 200
    assert "Summer League seasons" in resp.text
    assert "2024 Summer League (all competitions)" in resp.text
    assert "2025 Summer League (all competitions)" in resp.text


async def test_competition_scope_one_row_per_edition(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Competition scope renders one row per competition edition with venue."""
    await _seed(db_session)
    resp = await app_client.get(
        f"{EXPLORER}?subject=competitions&profile_scope=competition"
    )
    assert resp.status_code == 200
    assert "Individual competitions" in resp.text
    assert "2024 Las Vegas" in resp.text
    assert "2024 California Classic" in resp.text


async def test_season_scope_clears_venue(
    db_session: AsyncSession,
) -> None:
    """A season scope never applies a stale venue (contract §6 canonicalization)."""
    q = parse_query(
        {"subject": "competitions", "profile_scope": "season", "venue": "las_vegas"}
    )
    assert q.venue is None
    assert q.competition_id is None


async def test_min_games_filters_final_only(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Min final games includes a row only at/below its final-game count."""
    await _seed(db_session)
    # cc2024 has 6 final games; a floor of 10 must drop it.
    q = parse_query(
        {"subject": "competitions", "profile_scope": "competition", "min_gp": "10"}
    )
    result = await run_explorer_query(db_session, q)
    labels = {r.label for r in result.rows}
    assert "2024 California Classic" not in labels
    assert "2024 Las Vegas" in labels  # 18 finals >= 10


async def test_coverage_filter_shot_complete(
    db_session: AsyncSession,
) -> None:
    """shot_complete keeps only profiles whose shot coverage is complete."""
    await _seed(db_session)
    q = parse_query(
        {
            "subject": "competitions",
            "profile_scope": "competition",
            "coverage": "shot_complete",
        }
    )
    result = await run_explorer_query(db_session, q)
    labels = {r.label for r in result.rows}
    assert "2024 Las Vegas" in labels  # shot complete
    assert "2024 California Classic" not in labels  # box-only, shot missing


async def test_metric_threshold_filter_composes(
    db_session: AsyncSession,
) -> None:
    """A registry metric threshold never fires on a null/partial metric."""
    await _seed(db_session)
    # pace_per_48 >= 200 excludes everything (values ~99.5); no 500, empty result.
    q = parse_query(
        {
            "subject": "competitions",
            "profile_scope": "season",
            "fcol0": "pace_per_48",
            "fop0": "gte",
            "fval0": "200",
        }
    )
    result = await run_explorer_query(db_session, q)
    assert result.total == 0


async def test_invalid_metric_filter_degrades_safely(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A bogus metric key/value degrades off rather than 500-ing."""
    await _seed(db_session)
    resp = await app_client.get(
        f"{EXPLORER}?subject=competitions&fcol0=not_a_metric&fop0=gte&fval0=abc"
    )
    assert resp.status_code == 200
    assert "2024 Summer League" in resp.text  # unfiltered result still shows


# --------------------------------------------------------------------------- #
# Detail — identity, five sections, membership, definitions, coverage
# --------------------------------------------------------------------------- #


async def test_season_detail_names_members(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Season detail names every included competition, including a 0-final one."""
    await _seed(db_session)
    resp = await app_client.get(f"{EXPLORER}?subject=competitions&detail_year=2024")
    html = resp.text
    assert "Included competitions" in html
    assert "salt_lake_city" in html or "Salt Lake" in html.lower() or "Salt Lake" in html
    # All five sections present.
    for section in (
        "Identity &amp; format",
        "Field composition",
        "How it played",
        "Performance landscape",
        "Data confidence",
    ):
        assert section in html


async def test_competition_detail_resolves_by_id(
    db_session: AsyncSession,
) -> None:
    """Competition detail resolves exactly one canonical competition by id."""
    refs = await _seed(db_session)
    cid = refs.competition_ids["lv2024"]
    q = parse_query(
        {
            "subject": "competitions",
            "profile_scope": "competition",
            "competition_id": str(cid),
        }
    )
    result = await run_explorer_query(db_session, q)
    assert result.competition_detail is not None
    assert result.competition_detail.competition_id == cid
    assert result.competition_detail.scope_key == competition_scope_key(cid)


async def test_competition_id_authoritative_over_stale_year(
    db_session: AsyncSession,
) -> None:
    """A stale detail_year never coexists with an authoritative competition_id."""
    refs = await _seed(db_session)
    cid = refs.competition_ids["lv2025"]
    q = parse_query(
        {
            "subject": "competitions",
            "profile_scope": "competition",
            "competition_id": str(cid),
            "detail_year": "2024",
        }
    )
    assert q.detail_year is None
    result = await run_explorer_query(db_session, q)
    assert result.competition_detail is not None
    assert result.competition_detail.year == 2025


async def test_every_metric_exposes_definition(
    db_session: AsyncSession,
) -> None:
    """Every registry metric in the detail carries formula/denominator/interpretation."""
    await _seed(db_session)
    q = parse_query({"subject": "competitions", "detail_year": "2024"})
    result = await run_explorer_query(db_session, q)
    detail = result.competition_detail
    assert detail is not None
    seen = {m.key for section in detail.sections for m in section.metrics}
    expected = {d.key for d in metrics_for_scope("season_all_competitions")}
    assert seen == expected
    for section in detail.sections:
        for m in section.metrics:
            assert m.formula and m.denominator and m.interpretation
            assert m.unit


async def test_box_only_competition_null_shot_metrics(
    db_session: AsyncSession,
) -> None:
    """A box-complete/shot-missing profile shows null shot metrics, not zeros."""
    refs = await _seed(db_session)
    cid = refs.competition_ids["cc2024"]
    q = parse_query(
        {
            "subject": "competitions",
            "profile_scope": "competition",
            "competition_id": str(cid),
        }
    )
    result = await run_explorer_query(db_session, q)
    detail = result.competition_detail
    assert detail is not None
    assert detail.values["rim_fg_pct"] is None
    assert detail.coverage["rim_fg_pct"].coverage == "unavailable"
    # Box-derived metric still renders.
    assert detail.values["pace_per_48"] is not None
    assert detail.coverage["pace_per_48"].coverage == "complete"


async def test_pbp_informational_not_gating(
    db_session: AsyncSession,
) -> None:
    """PBP coverage is informational and never blanks box-derived assisted-FG rate."""
    refs = await _seed(db_session)
    cid = refs.competition_ids["cc2024"]  # pbp_covered=0
    q = parse_query(
        {
            "subject": "competitions",
            "profile_scope": "competition",
            "competition_id": str(cid),
        }
    )
    result = await run_explorer_query(db_session, q)
    detail = result.competition_detail
    assert detail is not None
    assert detail.values["assisted_fg_rate"] is not None
    pbp = next(s for s in detail.source_coverage if s.source == "pbp")
    assert pbp.informational is True


async def test_stale_profile_flagged_but_served(
    db_session: AsyncSession,
) -> None:
    """A profile past the freshness threshold serves as last-good with a stale flag."""
    refs = await _seed(db_session)
    cid = refs.competition_ids["lv2023"]
    q = parse_query(
        {
            "subject": "competitions",
            "profile_scope": "competition",
            "competition_id": str(cid),
        }
    )
    result = await run_explorer_query(db_session, q)
    assert result.competition_detail is not None
    assert result.competition_detail.is_stale is True


async def test_field_composition_known_unknown(
    db_session: AsyncSession,
) -> None:
    """Field composition discloses known/unknown/total per attribute."""
    await _seed(db_session)
    q = parse_query({"subject": "competitions", "detail_year": "2024"})
    result = await run_explorer_query(db_session, q)
    detail = result.competition_detail
    assert detail is not None
    attrs = {a.attribute_key: a for a in detail.field_composition}
    assert set(attrs) == {"draft", "age", "position", "origin"}
    for a in attrs.values():
        assert a.total == a.known + a.unknown


# --------------------------------------------------------------------------- #
# Trend
# --------------------------------------------------------------------------- #


async def test_season_trend_one_point_per_year_with_gap(
    db_session: AsyncSession,
) -> None:
    """Season trend has one point per surviving year; a partial year is a gap."""
    await _seed(db_session)
    q = parse_query(
        {"subject": "competitions", "profile_scope": "season", "trend_metric": "pace_per_48"}
    )
    result = await run_explorer_query(db_session, q)
    trend = result.competition_trend
    assert trend is not None
    by_year = {p.year: p for p in trend.points}
    assert by_year[2023].value is None  # box-partial -> gap, never zero
    assert by_year[2024].value is not None
    assert by_year[2025].value is not None


async def test_competition_trend_requires_venue(
    db_session: AsyncSession,
) -> None:
    """The unfiltered competition table renders no trend (never blends venues)."""
    await _seed(db_session)
    q = parse_query({"subject": "competitions", "profile_scope": "competition"})
    result = await run_explorer_query(db_session, q)
    assert result.competition_trend is None


async def test_competition_trend_single_venue_series(
    db_session: AsyncSession,
) -> None:
    """A venue-scoped competition trend renders one series across its years."""
    await _seed(db_session)
    q = parse_query(
        {
            "subject": "competitions",
            "profile_scope": "competition",
            "venue": "las_vegas",
            "trend_metric": "pace_per_48",
        }
    )
    result = await run_explorer_query(db_session, q)
    trend = result.competition_trend
    assert trend is not None
    assert trend.venue_slug == "las_vegas"
    years = {p.year for p in trend.points}
    assert years == {2023, 2024, 2025}


async def test_trend_and_table_agree(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Rendered trend table values match the table cell values for the same metric."""
    await _seed(db_session)
    resp = await app_client.get(
        f"{EXPLORER}?subject=competitions&profile_scope=season&trend_metric=offensive_rating"
    )
    assert resp.status_code == 200
    # The offensive_rating value (104.9) must appear in BOTH the results-table
    # cell and the trend data table — i.e. at least twice. A single occurrence
    # would mean the trend failed to render the selected metric, which this
    # test exists to catch (a bare substring check would pass on the table
    # alone and silently miss a missing trend).
    assert resp.text.count("104.9") >= 2


# --------------------------------------------------------------------------- #
# Partial / CSV parity
# --------------------------------------------------------------------------- #


async def test_partial_renders_fragment_only(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """?partial=1 returns just the results fragment (no full page chrome)."""
    await _seed(db_session)
    resp = await app_client.get(f"{EXPLORER}?subject=competitions&partial=1")
    assert resp.status_code == 200
    assert '<div class="slg-comp" id="explorer-results">' in resp.text
    assert "<html" not in resp.text.lower()


async def test_csv_structure_scope_ids_values_and_definitions(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """CSV includes stable scope ids, values, coverage, freshness, version, defs.

    Validates the CSV export contract's structure and self-describing trailer
    (contract §6). CSV-vs-computed-value parity is proven separately by
    ``test_summer_league_explorer.py::test_competitions_csv_and_table_values_agree``.
    """
    await _seed(db_session)
    resp = await app_client.get(f"{EXPLORER}?subject=competitions&format=csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    text = resp.text
    reader = list(csv.reader(io.StringIO(text)))
    header = reader[0]
    assert header[0] == "Scope"
    assert "Scope Key" in header
    assert "Version" in header
    assert "Box Coverage" in header
    # A season row carries its stable scope key and a value.
    season_rows = [r for r in reader if r and r[0].startswith("2024 Summer League")]
    assert season_rows
    assert "season:2024" in season_rows[0]
    # Definitions dictionary is appended.
    assert any(r and r[0] == "# Metric definitions" for r in reader)
    assert any(r and r[0] == "points_per_team_game" for r in reader)


# --------------------------------------------------------------------------- #
# Empty / invalid
# --------------------------------------------------------------------------- #


async def test_empty_scope_no_rows_no_error(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A valid filter yielding zero rows shows an explanatory empty state, not 500."""
    await _seed(db_session)
    resp = await app_client.get(f"{EXPLORER}?subject=competitions&year_min=2099")
    assert resp.status_code == 200
    assert "No competition profiles match" in resp.text


async def test_unknown_competition_id_no_detail(
    db_session: AsyncSession,
) -> None:
    """An unknown competition_id resolves no detail rather than erroring."""
    await _seed(db_session)
    q = parse_query(
        {
            "subject": "competitions",
            "profile_scope": "competition",
            "competition_id": "999999",
        }
    )
    result = await run_explorer_query(db_session, q)
    assert result.competition_detail is None


async def test_html_render_within_query_budget(
    db_session: AsyncSession, app_client: AsyncClient, async_engine: AsyncEngine
) -> None:
    """HTML list + detail + trend, and its partial, stay within the 10-query
    ceiling (contract §9) — the HTML render costs the same as the CSV path."""
    await _seed(db_session)
    full_url = (
        f"{EXPLORER}?subject=competitions&profile_scope=season"
        "&detail_year=2024&trend_metric=pace_per_48"
    )
    partial_url = full_url + "&partial=1"
    # Warm up caches so the measured render reflects steady state.
    assert (await app_client.get(full_url)).status_code == 200
    assert (await app_client.get(partial_url)).status_code == 200

    with count_queries(async_engine) as full_captured:
        assert (await app_client.get(full_url)).status_code == 200
    with count_queries(async_engine) as partial_captured:
        assert (await app_client.get(partial_url)).status_code == 200

    assert len(full_captured) <= 10, (
        f"full render issued {len(full_captured)} queries (budget 10): {full_captured}"
    )
    assert len(partial_captured) <= len(full_captured), (
        "partial render should cost no more than the full render"
    )


async def test_default_sort_is_year(db_session: AsyncSession) -> None:
    """Competitions default to sorting by year and accept registry metric sorts."""
    q = parse_query({"subject": "competitions"})
    assert q.sort == "year"
    q2 = parse_query({"subject": "competitions", "sort": "pace_per_48"})
    assert q2.sort == "pace_per_48"
    assert "pace_per_48" in set(sortable_metric_keys())
