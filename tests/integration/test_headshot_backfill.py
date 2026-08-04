"""Integration tests for the C1 NBA-CDN headshot backfill sweep.

Covers the invariants that make the sweep safe to run repeatedly:

1. A resolved player with an ``nba_stats`` external id and no reference image
   gets the CDN URL stamped; a player without that external id is untouched.
2. Players that already carry a reference image are skipped unless ``overwrite``.
3. A URL that fails validation routes the player to the fallback list and is
   *not* stamped.

Requires ``TEST_DATABASE_URL`` and ``PYTEST_ALLOW_DB=1``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_external_ids import PlayerExternalId
from app.schemas.players_master import PlayerMaster
from app.services.sources.summer_league.headshots import (
    NBA_STATS_SYSTEM,
    backfill_nba_headshots,
    nba_headshot_url,
)


def test_nba_headshot_url_builds_cdn_path() -> None:
    """The pure builder produces the 1040x760 CDN URL for a PERSON_ID."""
    assert nba_headshot_url("1641705") == (
        "https://cdn.nba.com/headshots/nba/latest/1040x760/1641705.png"
    )


async def _add_player(
    db: AsyncSession,
    display_name: str,
    *,
    person_id: str | None,
    reference_image_url: str | None = None,
) -> PlayerMaster:
    parts = display_name.split(" ", 1)
    player = PlayerMaster(
        first_name=parts[0],
        last_name=parts[1] if len(parts) > 1 else "",
        display_name=display_name,
        is_stub=False,
        bio_source="test",
        reference_image_url=reference_image_url,
    )
    db.add(player)
    await db.flush()
    assert player.id is not None
    if person_id is not None:
        db.add(
            PlayerExternalId(
                player_id=player.id,
                system=NBA_STATS_SYSTEM,
                external_id=person_id,
            )
        )
        await db.flush()
    return player


@pytest.mark.asyncio
async def test_backfill_stamps_players_with_external_id_only(
    db_session: AsyncSession,
) -> None:
    """Only players joined to an nba_stats external id are stamped."""
    with_id = await _add_player(db_session, "Has Id", person_id="1641705")
    without_id = await _add_player(db_session, "No Id", person_id=None)

    report = await backfill_nba_headshots(db_session)

    assert report.set_count == 1
    assert report.skipped_existing == 0
    assert report.fallback == []
    await db_session.refresh(with_id)
    await db_session.refresh(without_id)
    assert with_id.reference_image_url == nba_headshot_url("1641705")
    assert without_id.reference_image_url is None


@pytest.mark.asyncio
async def test_backfill_skips_existing_unless_overwrite(
    db_session: AsyncSession,
) -> None:
    """Existing reference images are preserved unless overwrite is requested."""
    existing_url = "https://example.com/custom.png"
    player = await _add_player(
        db_session,
        "Has Image",
        person_id="1641706",
        reference_image_url=existing_url,
    )

    skip_report = await backfill_nba_headshots(db_session)
    assert skip_report.set_count == 0
    assert skip_report.skipped_existing == 1
    await db_session.refresh(player)
    assert player.reference_image_url == existing_url

    overwrite_report = await backfill_nba_headshots(db_session, overwrite=True)
    assert overwrite_report.set_count == 1
    await db_session.refresh(player)
    assert player.reference_image_url == nba_headshot_url("1641706")


@pytest.mark.asyncio
async def test_backfill_routes_invalid_urls_to_fallback(
    db_session: AsyncSession,
) -> None:
    """A URL failing validation is not stamped and lands in the fallback list."""
    good = await _add_player(db_session, "Good Headshot", person_id="1641707")
    bad = await _add_player(db_session, "No Headshot", person_id="9999999")

    async def _validator(url: str) -> bool:
        return "9999999" not in url

    report = await backfill_nba_headshots(db_session, validator=_validator)

    assert report.set_count == 1
    assert report.fallback == [(bad.id, "9999999")]
    await db_session.refresh(good)
    await db_session.refresh(bad)
    assert good.reference_image_url == nba_headshot_url("1641707")
    assert bad.reference_image_url is None
