"""Integration tests for player profile page functionality."""

from datetime import date

import pytest


@pytest.mark.asyncio
async def test_player_detail_returns_profile_data(app_client, db_session):
    """Player detail page returns real profile data from database."""
    from app.schemas.players_master import PlayerMaster
    from app.schemas.player_status import PlayerStatus
    from app.schemas.positions import Position

    # Create test position
    position = Position(code="F", description="Forward")
    db_session.add(position)
    await db_session.flush()

    # Create test player with full bio data
    player = PlayerMaster(
        display_name="Test Player",
        slug="test-player",
        birthdate=date(2005, 3, 15),
        birth_city="Portland",
        birth_state_province="OR",
        birth_country="USA",
        school="Oregon",
        high_school="Grant High School",
        shoots="R",
    )
    db_session.add(player)
    await db_session.flush()

    # Create player status with physical measurements
    status = PlayerStatus(
        player_id=player.id,
        position_id=position.id,
        height_in=81,  # 6'9"
        weight_lb=205,
    )
    db_session.add(status)
    await db_session.commit()

    # Request player detail page
    response = await app_client.get("/players/test-player")

    assert response.status_code == 200
    content = response.text

    # Verify bio fields are present
    assert "Test Player" in content
    assert "F" in content  # Position code
    assert "Oregon" in content  # College
    assert "Grant High School" in content  # High school
    assert "R" in content  # Shoots
    assert (
        "6'9\"" in content or "6&#39;9&#34;" in content
    )  # Height (may be HTML escaped as decimal entities)
    assert "205 lbs" in content  # Weight
    assert "Portland, OR" in content  # Hometown


@pytest.mark.asyncio
async def test_player_detail_shows_age_in_years_months_days(app_client, db_session):
    """Player age is displayed in 'Xy Xm Xd' format."""
    from app.schemas.players_master import PlayerMaster

    # Create player born exactly 19 years, 6 months, 10 days ago (approximately)
    # We'll just verify the format pattern is present
    player = PlayerMaster(
        display_name="Young Prospect",
        slug="young-prospect",
        birthdate=date(2005, 6, 15),
        school="Duke",
    )
    db_session.add(player)
    await db_session.commit()

    response = await app_client.get("/players/young-prospect")

    assert response.status_code == 200
    # Age should contain the pattern Xy Xm Xd (e.g., "19y 5m 16d")
    import re

    age_pattern = r"\d+y \d+m \d+d"
    assert re.search(age_pattern, response.text), "Age should be in 'Xy Xm Xd' format"


@pytest.mark.asyncio
async def test_player_detail_returns_404_for_missing_slug(app_client):
    """Player detail returns 404 for non-existent slug."""
    response = await app_client.get("/players/non-existent-player")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_player_detail_handles_missing_fields_gracefully(app_client, db_session):
    """Player detail page renders when optional fields are missing."""
    from app.schemas.players_master import PlayerMaster

    # Create minimal player with only required fields
    player = PlayerMaster(
        display_name="Minimal Player",
        slug="minimal-player",
    )
    db_session.add(player)
    await db_session.commit()

    response = await app_client.get("/players/minimal-player")

    assert response.status_code == 200
    assert "Minimal Player" in response.text


@pytest.mark.asyncio
async def test_player_detail_hides_scoreboard_when_no_metrics(app_client, db_session):
    """Draft Analytics Scoreboard is hidden when metrics are not available."""
    from app.schemas.players_master import PlayerMaster

    player = PlayerMaster(
        display_name="No Metrics Player",
        slug="no-metrics-player",
        school="UCLA",
    )
    db_session.add(player)
    await db_session.commit()

    response = await app_client.get("/players/no-metrics-player")

    assert response.status_code == 200
    # Scoreboard should not be present (it's hidden when consensusRank is None)
    assert "Draft Analytics Dashboard" not in response.text


@pytest.mark.asyncio
async def test_player_detail_shows_wingspan_from_combine(app_client, db_session):
    """Player wingspan is fetched from CombineAnthro table."""
    from app.schemas.players_master import PlayerMaster
    from app.schemas.combine_anthro import CombineAnthro
    from app.schemas.seasons import Season

    # Create season
    season = Season(code="2024-25", start_year=2024, end_year=2025)
    db_session.add(season)
    await db_session.flush()

    # Create player
    player = PlayerMaster(
        display_name="Long Arms",
        slug="long-arms",
        school="Kentucky",
    )
    db_session.add(player)
    await db_session.flush()

    # Create combine anthro data with wingspan
    anthro = CombineAnthro(
        player_id=player.id,
        season_id=season.id,
        wingspan_in=86.5,  # 7'2.5"
    )
    db_session.add(anthro)
    await db_session.commit()

    response = await app_client.get("/players/long-arms")

    assert response.status_code == 200
    # Wingspan should be displayed (86.5" = 7'2.5" with half-inch precision)
    assert "7'2.5\"" in response.text or "7&#39;2.5&#34;" in response.text


@pytest.mark.asyncio
async def test_player_detail_hometown_formats_correctly(app_client, db_session):
    """Hometown is formatted as 'City, State' for US or 'City, Country' for international."""
    from app.schemas.players_master import PlayerMaster

    # US player
    us_player = PlayerMaster(
        display_name="US Player",
        slug="us-player",
        birth_city="Los Angeles",
        birth_state_province="CA",
        birth_country="USA",
    )
    db_session.add(us_player)

    # International player
    intl_player = PlayerMaster(
        display_name="Intl Player",
        slug="intl-player",
        birth_city="Paris",
        birth_country="France",
    )
    db_session.add(intl_player)
    await db_session.commit()

    # Check US player
    response = await app_client.get("/players/us-player")
    assert response.status_code == 200
    assert "Los Angeles, CA" in response.text

    # Check international player with city
    response = await app_client.get("/players/intl-player")
    assert response.status_code == 200
    assert "Paris, France" in response.text


@pytest.mark.asyncio
async def test_player_detail_hides_literal_null_college(app_client, db_session):
    """College rendered as None should not display the string 'null'."""
    from app.schemas.players_master import PlayerMaster
    from app.schemas.positions import Position
    from app.schemas.player_status import PlayerStatus

    # Create position and player with a literal 'null' school value
    position = Position(code="C", description="Center")
    db_session.add(position)
    await db_session.flush()

    player = PlayerMaster(
        display_name="Intl Big",
        slug="intl-big",
        school="null",  # legacy string value that should be treated as missing
        birth_country="France",
    )
    db_session.add(player)
    await db_session.flush()

    status = PlayerStatus(
        player_id=player.id,
        position_id=position.id,
        height_in=85,
        weight_lb=258,
    )
    db_session.add(status)
    await db_session.commit()

    response = await app_client.get("/players/intl-big")
    assert response.status_code == 200
    # Bio line should display position, height, weight but NOT "null" for missing college
    # Check that the primary meta shows "C • 7'1" • 258 lbs" (no null)
    assert "C • 7" in response.text or "C • 7&#39;" in response.text
    # And make sure "null" doesn't appear as visible text (not counting JSON data)
    assert "• null •" not in response.text.lower()


@pytest.mark.asyncio
async def test_player_detail_shows_country_when_no_city(app_client, db_session):
    """International players with only country show country name as hometown."""
    from app.schemas.players_master import PlayerMaster

    # International player with only country (no city)
    player = PlayerMaster(
        display_name="Mystery Intl",
        slug="mystery-intl",
        birth_country="Australia",
    )
    db_session.add(player)
    await db_session.commit()

    response = await app_client.get("/players/mystery-intl")
    assert response.status_code == 200
    assert "Australia" in response.text


@pytest.mark.asyncio
async def test_player_detail_includes_photo_url(app_client, db_session):
    """Player detail page includes photo_url in rendered output."""
    from app.schemas.players_master import PlayerMaster

    player = PlayerMaster(
        display_name="Photo Test",
        slug="photo-test",
        school="Duke",
    )
    db_session.add(player)
    await db_session.commit()

    response = await app_client.get("/players/photo-test")

    assert response.status_code == 200
    # Photo should be in an img tag with class player-photo
    assert 'class="player-photo"' in response.text
    # Since no local image exists, should use placehold.co
    assert "placehold.co" in response.text
    # Player name should be in placeholder URL
    assert "Photo+Test" in response.text


@pytest.mark.asyncio
async def test_player_detail_style_param_changes_photo_url(app_client, db_session):
    """Style query param changes which image style is used for photo URL."""
    from app.schemas.players_master import PlayerMaster

    player = PlayerMaster(
        display_name="Style Test",
        slug="style-test",
        school="Kentucky",
    )
    db_session.add(player)
    await db_session.commit()

    # Request with style param
    response = await app_client.get("/players/style-test?style=vector")

    assert response.status_code == 200
    # When no image exists, falls back to placeholder regardless of style
    assert "placehold.co" in response.text


@pytest.mark.asyncio
async def test_player_detail_photo_url_uses_player_id(app_client, db_session):
    """Photo URL comes from the current image asset for the player."""
    from app.schemas.players_master import PlayerMaster
    from app.models.fields import CohortType
    from app.schemas.image_snapshots import PlayerImageAsset, PlayerImageSnapshot

    player = PlayerMaster(
        display_name="ID Test Player",
        slug="id-test-player",
        school="UNC",
    )
    db_session.add(player)
    await db_session.commit()

    snapshot = PlayerImageSnapshot(
        run_key="test",
        version=1,
        is_current=True,
        style="default",
        cohort=CohortType.global_scope,
        draft_year=None,
        population_size=1,
        success_count=1,
        failure_count=0,
        image_size="1K",
        system_prompt="test",
        system_prompt_version="default",
    )
    db_session.add(snapshot)
    await db_session.commit()
    await db_session.refresh(snapshot)

    public_url = (
        f"https://cdn.example.com/players/{player.id}_id-test-player_default.png"
    )
    asset = PlayerImageAsset(
        snapshot_id=snapshot.id,  # type: ignore[arg-type]
        player_id=player.id,  # type: ignore[arg-type]
        s3_key=f"players/{player.id}_id-test-player_default.png",
        s3_bucket="test-bucket",
        public_url=public_url,
        user_prompt="test",
    )
    db_session.add(asset)
    await db_session.commit()

    response = await app_client.get("/players/id-test-player")

    assert response.status_code == 200
    assert public_url in response.text
