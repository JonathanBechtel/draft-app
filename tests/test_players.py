import pytest


@pytest.mark.asyncio
async def test_list_players_returns_200(app_client):
    response = await app_client.get("/players")
    assert response.status_code == 200
