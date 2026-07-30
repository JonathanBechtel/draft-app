"""Integration tests for the bbref bio ingest (`player_bio._ingest_rows`).

This is the write half of the bio pipeline the Summer League roster cron runs:
a scraped CSV row is resolved to a canonical ``players_master`` record and then
fans out into external ids, an alias, immutable master fields, an ephemeral
status row, and a raw-meta snapshot.

The tests stage against the real schema through ``db_session`` and assert on
rows, not on call counts. ``_ingest_rows`` deliberately does not commit — the
caller owns the transaction — so each test flushes and reads back within the
fixture's own transaction.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app.schemas.player_aliases import PlayerAlias
from app.schemas.player_bio_snapshots import PlayerBioSnapshot
from app.schemas.player_external_ids import PlayerExternalId
from app.schemas.player_status import PlayerStatus
from app.schemas.players_master import PlayerMaster
from app.services.player_bio.ingest import IngestOptions, _ingest_rows
from app.services.player_bio.rows import BioRow


def _bio_row(**overrides: object) -> BioRow:
    """Build a ``BioRow`` with realistic defaults, overriding named fields."""
    defaults: dict[str, object] = {
        "slug": "balllo01",
        "url": "https://www.basketball-reference.com/players/b/balllo01.html",
        "full_name": "Lonzo Ball",
        "birth_date": "1997-10-27",
        "birth_city": "Anaheim",
        "birth_state_province": "California",
        "birth_country": "US",
        "shoots": "Right",
        "school": "UCLA",
        "high_school": "Chino Hills in Chino Hills, California",
        "draft_year": 2017,
        "draft_round": 1,
        "draft_pick": 2,
        "draft_team": "Los Angeles Lakers",
        "nba_debut_date": "2017-10-19",
        "nba_debut_season": "2017-18",
        "is_active_nba": True,
        "current_team": "Cleveland Cavaliers",
        "nba_last_season": "2024-25",
        "position": "Point Guard",
        "height_in": 78,
        "weight_lb": 190,
        "social_x_handle": None,
        "social_x_url": None,
        "social_instagram_handle": None,
        "social_instagram_url": None,
        "source_url": ("https://www.basketball-reference.com/players/b/balllo01.html"),
        "scraped_at": "2026-07-27T00:00:00+00:00",
    }
    defaults.update(overrides)
    return BioRow(**defaults)  # type: ignore[arg-type]


def _options(tmp_path: Path, **overrides: object) -> IngestOptions:
    """Build ``IngestOptions`` pointing at an empty snapshot cache."""
    defaults: dict[str, object] = {
        "cache_dir": tmp_path,
        "overwrite_master": False,
        "create_missing": False,
        "verbose": False,
    }
    defaults.update(overrides)
    return IngestOptions(**defaults)  # type: ignore[arg-type]


async def _add_player(db, **fields: object) -> PlayerMaster:
    """Insert a ``players_master`` row and flush so it has an id."""
    player = PlayerMaster(**fields)  # type: ignore[arg-type]
    db.add(player)
    await db.flush()
    return player


@pytest.mark.asyncio
async def test_matches_by_display_name_and_writes_the_full_fan_out(
    db_session, tmp_path: Path
) -> None:
    """An exact name match drives every downstream write for that row.

    ``display_name`` is deliberately not ``"Lonzo Ball"`` here: ``_load_lookup``
    indexes both the display name and the derived ``"first last"``, so a player
    whose display name is exactly first+last lands twice under the same key. See
    ``test_a_player_whose_display_name_is_first_last_reads_as_ambiguous``.
    """
    player = await _add_player(
        db_session, first_name="Lonzo", last_name="Ball", display_name="Zo Ball"
    )

    report = await _ingest_rows(db_session, [_bio_row()], _options(tmp_path))
    await db_session.flush()

    assert report.unmatched == []
    assert report.ambiguous == []

    ext = (
        (
            await db_session.execute(
                select(PlayerExternalId).where(PlayerExternalId.player_id == player.id)
            )
        )
        .scalars()
        .all()
    )
    assert {(e.system, e.external_id) for e in ext} == {("bbr", "balllo01")}

    alias = (
        (
            await db_session.execute(
                select(PlayerAlias).where(PlayerAlias.player_id == player.id)
            )
        )
        .scalars()
        .first()
    )
    assert alias is not None
    assert alias.full_name == "Lonzo Ball"
    assert alias.context == "bbr"

    await db_session.refresh(player)
    assert player.birthdate is not None and player.birthdate.isoformat() == "1997-10-27"
    assert player.nba_debut_date is not None
    assert player.birth_city == "Anaheim"
    assert player.birth_country == "United States"  # canonicalized from "US"
    assert player.draft_year == 2017
    assert player.draft_pick == 2
    assert player.school == "UCLA"

    status = (
        (
            await db_session.execute(
                select(PlayerStatus).where(PlayerStatus.player_id == player.id)
            )
        )
        .scalars()
        .first()
    )
    assert status is not None
    assert status.is_active_nba is True
    assert status.current_team == "Cleveland Cavaliers"
    assert status.height_in == 78
    assert status.weight_lb == 190
    assert status.source == "bbr"
    assert status.position_id is not None


@pytest.mark.asyncio
async def test_matches_by_existing_bbr_external_id(db_session, tmp_path: Path) -> None:
    """The bbr external id wins even when the scraped name matches nobody."""
    player = await _add_player(
        db_session, first_name="Alonzo", last_name="Ball", display_name="Alonzo Ball"
    )
    db_session.add(
        PlayerExternalId(player_id=player.id, system="bbr", external_id="balllo01")
    )
    await db_session.flush()

    report = await _ingest_rows(
        db_session, [_bio_row(full_name="Totally Different Name")], _options(tmp_path)
    )
    await db_session.flush()

    assert report.unmatched == []
    await db_session.refresh(player)
    assert player.draft_year == 2017


@pytest.mark.asyncio
async def test_unmatched_row_is_reported_and_writes_nothing(
    db_session, tmp_path: Path
) -> None:
    """With ``create_missing`` off, an unresolvable row is reported, not invented."""
    report = await _ingest_rows(
        db_session, [_bio_row(full_name="Nobody Here")], _options(tmp_path)
    )
    await db_session.flush()

    assert report.unmatched == ["balllo01"]
    players = (await db_session.execute(select(PlayerMaster))).scalars().all()
    assert players == []


@pytest.mark.asyncio
async def test_create_missing_inserts_a_canonical_player(
    db_session, tmp_path: Path
) -> None:
    """``create_missing`` mints a ``players_master`` row from the scraped name."""
    report = await _ingest_rows(
        db_session,
        [_bio_row(full_name="Ausar Jabari Thompson")],
        _options(tmp_path, create_missing=True),
    )
    await db_session.flush()

    assert report.unmatched == []
    player = (await db_session.execute(select(PlayerMaster))).scalars().one()
    assert player.first_name == "Ausar"
    assert player.middle_name == "Jabari"
    assert player.last_name == "Thompson"
    assert player.display_name == "Ausar Jabari Thompson"


@pytest.mark.asyncio
async def test_create_missing_reuses_diacritic_variant(
    db_session, tmp_path: Path
) -> None:
    """A diacritic-only bio name reuses the existing canonical player."""
    player = await _add_player(
        db_session,
        first_name="José",
        last_name="García",
        display_name="José García",
    )

    report = await _ingest_rows(
        db_session,
        [_bio_row(full_name="Jose Garcia")],
        _options(tmp_path, create_missing=True),
    )
    await db_session.flush()

    assert report.ambiguous == []
    assert report.unmatched == []
    players = (await db_session.execute(select(PlayerMaster))).scalars().all()
    assert len(players) == 1
    assert players[0].id == player.id


@pytest.mark.asyncio
async def test_create_missing_routes_suffix_variant_to_review(
    db_session, tmp_path: Path
) -> None:
    """A suffix-differing bio name is reported instead of linked or minted."""
    await _add_player(
        db_session,
        first_name="Gary",
        last_name="Payton",
        suffix="II",
        display_name="Gary Payton II",
    )

    report = await _ingest_rows(
        db_session,
        [_bio_row(full_name="Gary Payton")],
        _options(tmp_path, create_missing=True),
    )
    await db_session.flush()

    assert report.ambiguous == ["balllo01"]
    players = (await db_session.execute(select(PlayerMaster))).scalars().all()
    assert len(players) == 1


@pytest.mark.asyncio
async def test_two_rows_for_one_new_player_reuse_the_created_record(
    db_session, tmp_path: Path
) -> None:
    """The in-run lookup update keeps a second row from creating a duplicate."""
    rows = [
        _bio_row(slug="thompau01", full_name="Ausar Thompson"),
        _bio_row(slug="thompau02", full_name="Ausar Thompson"),
    ]

    await _ingest_rows(db_session, rows, _options(tmp_path, create_missing=True))
    await db_session.flush()

    players = (await db_session.execute(select(PlayerMaster))).scalars().all()
    assert len(players) == 1


@pytest.mark.asyncio
async def test_ambiguous_name_is_reported_and_left_unresolved(
    db_session, tmp_path: Path
) -> None:
    """Two players sharing a name resolve to nobody — the namesake guard."""
    first = await _add_player(
        db_session, first_name="Derek", last_name="Harper", display_name="D. Harper"
    )
    second = await _add_player(
        db_session, first_name="Dylan", last_name="Harper", display_name="D. Harper"
    )
    db_session.add_all(
        [
            PlayerAlias(player_id=first.id, full_name="D. Harper", context="test"),
            PlayerAlias(player_id=second.id, full_name="D. Harper", context="test"),
        ]
    )
    await db_session.flush()

    report = await _ingest_rows(
        db_session,
        [_bio_row(slug="harpede01", full_name="D. Harper")],
        _options(tmp_path),
    )

    assert report.ambiguous == ["harpede01"]
    assert report.unmatched == []


@pytest.mark.asyncio
async def test_fixed_ambiguity_mapping_overrides_every_automatic_match(
    db_session, tmp_path: Path
) -> None:
    """A manual ``slug -> player_id`` resolution beats name and external-id matching."""
    named = await _add_player(
        db_session, first_name="Lonzo", last_name="Ball", display_name="Lonzo Ball"
    )
    manual = await _add_player(
        db_session,
        first_name="LiAngelo",
        last_name="Ball",
        display_name="LiAngelo Ball",
    )
    assert manual.id is not None

    await _ingest_rows(
        db_session, [_bio_row()], _options(tmp_path, fixed_map={"balllo01": manual.id})
    )
    await db_session.flush()

    ext = (
        (
            await db_session.execute(
                select(PlayerExternalId).where(PlayerExternalId.system == "bbr")
            )
        )
        .scalars()
        .all()
    )
    assert [e.player_id for e in ext] == [manual.id]
    await db_session.refresh(named)
    assert named.draft_year is None


@pytest.mark.asyncio
async def test_social_handles_become_external_ids(db_session, tmp_path: Path) -> None:
    """X and Instagram handles are stored alongside the bbr id, with their urls."""
    player = await _add_player(
        db_session, first_name="Lonzo", last_name="Ball", display_name="Lonzo Ball"
    )

    await _ingest_rows(
        db_session,
        [
            _bio_row(
                social_x_handle="zo",
                social_x_url="https://x.com/zo",
                social_instagram_handle="zo",
                social_instagram_url="https://instagram.com/zo",
            )
        ],
        _options(tmp_path),
    )
    await db_session.flush()

    ext = (
        (
            await db_session.execute(
                select(PlayerExternalId).where(PlayerExternalId.player_id == player.id)
            )
        )
        .scalars()
        .all()
    )
    assert {(e.system, e.external_id) for e in ext} == {
        ("bbr", "balllo01"),
        ("x", "zo"),
        ("instagram", "zo"),
    }


@pytest.mark.asyncio
async def test_existing_master_fields_are_not_overwritten_by_default(
    db_session, tmp_path: Path
) -> None:
    """Master fields are immutable unless ``overwrite_master`` is set."""
    player = await _add_player(
        db_session,
        first_name="Lonzo",
        last_name="Ball",
        display_name="Lonzo Ball",
        birth_city="Chino Hills",
        draft_pick=99,
    )

    await _ingest_rows(db_session, [_bio_row()], _options(tmp_path))
    await db_session.flush()
    await db_session.refresh(player)

    assert player.birth_city == "Chino Hills"
    assert player.draft_pick == 99

    await _ingest_rows(
        db_session, [_bio_row()], _options(tmp_path, overwrite_master=True)
    )
    await db_session.flush()
    await db_session.refresh(player)

    assert player.birth_city == "Anaheim"
    assert player.draft_pick == 2


@pytest.mark.asyncio
async def test_cached_player_html_is_snapshotted(db_session, tmp_path: Path) -> None:
    """A cached page contributes its ``div#meta`` as a raw bio snapshot."""
    player = await _add_player(
        db_session, first_name="Lonzo", last_name="Ball", display_name="Lonzo Ball"
    )
    (tmp_path / "balllo01.html").write_text(
        "<html><div id='meta'><p>Born: 1997</p></div></html>", encoding="utf-8"
    )

    await _ingest_rows(db_session, [_bio_row()], _options(tmp_path))
    await db_session.flush()

    snapshot = (
        (
            await db_session.execute(
                select(PlayerBioSnapshot).where(
                    PlayerBioSnapshot.player_id == player.id
                )
            )
        )
        .scalars()
        .one()
    )
    assert snapshot.source == "bbr"
    assert "Born: 1997" in snapshot.raw_meta_html


@pytest.mark.asyncio
async def test_an_external_id_owned_by_another_player_is_not_reassigned(
    db_session, tmp_path: Path
) -> None:
    """A bbr id already pointing elsewhere stays put rather than being stolen."""
    owner = await _add_player(
        db_session, first_name="Someone", last_name="Else", display_name="Someone Else"
    )
    claimant = await _add_player(
        db_session, first_name="Lonzo", last_name="Ball", display_name="Lonzo Ball"
    )
    db_session.add(
        PlayerExternalId(player_id=owner.id, system="bbr", external_id="balllo01")
    )
    await db_session.flush()

    # Resolve by the manual map so the run reaches _upsert_external as `claimant`
    # despite the external id already belonging to `owner`.
    assert claimant.id is not None
    await _ingest_rows(
        db_session,
        [_bio_row()],
        _options(tmp_path, fixed_map={"balllo01": claimant.id}),
    )
    await db_session.flush()

    ext = (
        (
            await db_session.execute(
                select(PlayerExternalId).where(
                    PlayerExternalId.external_id == "balllo01"
                )
            )
        )
        .scalars()
        .all()
    )
    assert [e.player_id for e in ext] == [owner.id]


@pytest.mark.asyncio
async def test_a_player_whose_display_name_is_first_last_reads_as_ambiguous(
    db_session, tmp_path: Path
) -> None:
    """A unique normalized display match is no longer confused by lookup buckets.

    The shared identity guard sees one canonical player and reuses it without
    marking the row ambiguous.
    """
    player = await _add_player(
        db_session, first_name="Lonzo", last_name="Ball", display_name="Lonzo Ball"
    )

    report = await _ingest_rows(db_session, [_bio_row()], _options(tmp_path))
    await db_session.flush()

    assert report.ambiguous == []
    assert report.unmatched == []
    # Reported, but still correctly resolved and written.
    ext = (
        (
            await db_session.execute(
                select(PlayerExternalId).where(PlayerExternalId.system == "bbr")
            )
        )
        .scalars()
        .all()
    )
    assert [e.player_id for e in ext] == [player.id]
