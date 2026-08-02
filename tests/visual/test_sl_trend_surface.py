"""Visual contract checks for the scope-parameterized SL trend card.

These checks deliberately mount the same server-served CSS/JS and payload
contract used by the Jinja macro.  They keep the chart reviewable even when a
developer database has no player or competition seed rows.
"""

from __future__ import annotations

import json

from playwright.sync_api import Page, expect


def _payload(*, scope_key: str = "competition:2026:42", player_id: int | None = 7, single_point: bool = False) -> dict:
    """Return a deterministic three-metric cumulative trend payload."""
    days = ["2026-07-10"] if single_point else ["2026-07-10", "2026-07-11", "2026-07-12"]
    values = {
        "gmsc": [0.42, 0.58, 0.77],
        "ts_pct": [0.51, 0.54, 0.57],
        "bpm": [1.8, 2.4, 3.1],
    }
    metrics = [
        {"key": "gmsc", "label": "Game Score"},
        {"key": "ts_pct", "label": "TS%"},
        {"key": "bpm", "label": "BPM"},
    ]
    points: list[dict] = []
    for metric in metrics:
        for index, day in enumerate(days):
            value = values[metric["key"]][index if not single_point else 0]
            points.append(
                {
                    "metric_key": metric["key"],
                    "effective_day": day,
                    "value": value,
                    "cohort_band": {"q1": value - 0.15, "q3": value + 0.15},
                }
            )
    return {
        "scope_key": scope_key,
        "scope_label": "2026 Las Vegas",
        "player_id": player_id,
        "latest_as_of": "2026-07-13T12:00:00",
        "latest_effective_day": days[-1],
        "single_point": single_point,
        "metric_keys": [metric["key"] for metric in metrics],
        "metrics": metrics,
        "points": points,
    }


def _mount(page: Page, base_url: str, payload: dict) -> None:
    """Mount a trend card with the production static assets and payload."""
    payload_json = json.dumps(payload)
    share_button = (
        '<button class="trend-card__share" type="button" data-trend-share>'
        "Share PNG</button>"
        if payload.get("player_id")
        else ""
    )
    single_note = (
        '<p class="trend-card__single-point" data-trend-single>'
        "Single-point state · one published event day</p>"
        if payload.get("single_point")
        else ""
    )
    metric_buttons = "".join(
        f'<button class="trend-card__legend-item" type="button" '
        f'data-trend-metric="{metric["key"]}">{metric["label"]}</button>'
        for metric in payload["metrics"]
    )
    html = f"""
    <!doctype html>
    <html><head><base href="{base_url}/">
      <link rel="stylesheet" href="{base_url}/static/main.css">
      <link rel="stylesheet" href="{base_url}/static/css/sl-trend.css">
    </head><body>
      <main style="max-width: 900px; margin: 1rem auto; padding: 0 .75rem">
        <section class="trend-card" data-trend-root
                 data-trend-scope="{payload['scope_key']}"
                 data-trend-player-id="{payload.get('player_id') or ''}">
          <div class="trend-card__head"><div>
            <p class="trend-card__eyebrow">{payload['scope_label']}</p>
            <h2 class="trend-card__title">Within-event trend</h2>
            <p class="trend-card__note">Cumulative through-day performance · cohort median + IQR</p>
          </div>{share_button}</div>
          <div class="trend-card__meta">
            <span>Source as of <time>{payload['latest_as_of']}</time></span>
            <span>Through <time>{payload['latest_effective_day']}</time></span>
          </div>
          <div class="trend-card__chart-wrap"><svg class="trend-card__chart"
               data-trend-chart viewBox="0 0 760 330"></svg></div>
          <div class="trend-card__legend" data-trend-legend>{metric_buttons}</div>
          {single_note}
          <p class="trend-card__tooltip" data-trend-tooltip role="status"></p>
          <script type="application/json" data-trend-payload>{payload_json}</script>
        </section>
      </main>
      <script src="{base_url}/static/js/sl-trend.js"></script>
    </body></html>
    """
    page.set_content(html)
    page.wait_for_selector("[data-trend-chart]")
    page.wait_for_function(
        "document.querySelectorAll('[data-trend-line]').length === 3"
    )


class TestSlTrendSurfaceVisuals:
    """Capture the desktop, mobile, event, share, and one-point states."""

    def test_desktop_chart_and_tap_tooltip(self, page: Page, base_url: str, screenshot) -> None:
        """Desktop chart renders all lanes and exposes point status text."""
        _mount(page, base_url, _payload())
        expect(page.locator(".trend-card__line")).to_have_count(3)
        expect(page.locator(".trend-card__band")).to_have_count(3)
        page.locator(".trend-card__point").first.click()
        expect(page.locator("[data-trend-tooltip]")).to_contain_text("GMSC")
        screenshot.capture_element(".trend-card", "sl_trend_desktop")

    def test_mobile_card_has_no_horizontal_scroll(self, mobile_page: Page, base_url: str, screenshot) -> None:
        """390px layout keeps the chart inside the viewport."""
        mobile_page.set_viewport_size({"width": 390, "height": 844})
        _mount(mobile_page, base_url, _payload())
        expect(mobile_page.locator(".trend-card")).to_be_visible()
        assert mobile_page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        screenshot.capture_element(".trend-card", "sl_trend_mobile")

    def test_event_scope_is_shareless(self, page: Page, base_url: str, screenshot) -> None:
        """Competition/cohort cards render without a player-only share action."""
        _mount(page, base_url, _payload(scope_key="competition:2026:42", player_id=None))
        expect(page.locator("[data-trend-share]")).to_have_count(0)
        expect(page.locator(".trend-card__eyebrow")).to_contain_text("2026 Las Vegas")
        screenshot.capture_element(".trend-card", "sl_trend_event_scope")

    def test_share_posts_sl_trend_component(self, page: Page, base_url: str, screenshot) -> None:
        """Player share action posts the scope and metric contract."""
        def fulfill(route) -> None:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"url": "data:image/png;base64,AA==", "filename": "trend.png"}),
            )

        page.route("**/api/export/image", fulfill)
        _mount(page, base_url, _payload())
        with page.expect_request("**/api/export/image") as request_info:
            page.locator("[data-trend-share]").click()
        request = request_info.value
        request_payload = json.loads(request.post_data)
        assert request_payload["component"] == "sl_trend"
        assert request_payload["context"]["scope_key"] == "competition:2026:42"
        screenshot.capture_element(".trend-card", "sl_trend_share_card")

    def test_single_point_state_is_explicit(self, page: Page, base_url: str, screenshot) -> None:
        """One published day remains visible and is labeled honestly."""
        _mount(page, base_url, _payload(single_point=True))
        expect(page.locator("[data-trend-single]")).to_contain_text("Single-point state")
        expect(page.locator(".trend-card__point")).to_have_count(3)
        screenshot.capture_element(".trend-card", "sl_trend_single_point")
