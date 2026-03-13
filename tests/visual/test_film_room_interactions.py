"""Browser interaction tests for shared Film Room modules."""

import re
from pathlib import Path
from textwrap import dedent

from playwright.sync_api import Page, expect

FILM_ROOM_JS = (
    Path(__file__).resolve().parents[2] / "app/static/js/film-room.js"
).read_text(encoding="utf-8")


def _bootstrap_script(page: Page, html: str) -> None:
    """Load representative markup and execute the production Film Room script."""
    page.set_content(html)
    page.add_script_tag(content=FILM_ROOM_JS)
    page.evaluate(
        """() => {
          document.dispatchEvent(new Event('DOMContentLoaded'));
        }"""
    )


def _homepage_markup() -> str:
    """Return representative homepage Film Room markup."""
    return dedent(
        """
        <!DOCTYPE html>
        <html lang="en">
        <body>
          <section class="section" id="filmRoomHomeSection">
            <div class="film-room film-room--home sprocket-border">
              <div class="film-tabs" id="homeFilmTabs">
                <button type="button" class="film-tab active" data-tag="Think Piece" data-label="Think Pieces">
                  Think Pieces <span class="film-tab__count">(2)</span>
                </button>
                <button type="button" class="film-tab" data-tag="Conversation" data-label="Conversations">
                  Conversations <span class="film-tab__count">(1)</span>
                </button>
              </div>

              <div class="film-playlist" id="homeFilmPlaylist">
                <div class="film-player">
                  <div class="film-player__embed film-player__embed--placeholder" id="homeFilmEmbedWrap">
                    <div class="film-player__play-overlay" id="homeFilmPlayOverlay">
                      <span class="film-player__now-playing">Now Playing</span>
                    </div>
                    <iframe
                      id="homeFilmEmbed"
                      src=""
                      title="Initial title"
                      loading="lazy"
                      allowfullscreen
                      hidden
                    ></iframe>
                  </div>

                  <div class="film-player__info">
                    <h3 class="film-player__video-title" id="homeFilmTitle">Initial title</h3>
                    <div class="film-player__meta">
                      <span class="film-player__channel" id="homeFilmChannel">Initial channel</span>
                      <span id="homeFilmTime">Initial time</span>
                      <span id="homeFilmDuration">Initial duration</span>
                    </div>
                    <div class="film-player__tags" id="homeFilmTags">
                      <span class="video-type-tag" id="homeFilmTypeTag">Think Piece</span>
                      <a href="/players/cooper-flagg" class="film-player-tag">Cooper Flagg</a>
                    </div>
                  </div>
                </div>

                <div class="film-playlist-list">
                  <div class="film-playlist__header">
                    <span class="film-playlist__label" id="homeFilmPlaylistLabel">Up Next</span>
                    <span class="film-playlist__count" id="homeFilmPlaylistCount"></span>
                  </div>
                  <div class="film-playlist__items" id="homeFilmPlaylistItems">
                    <button
                      type="button"
                      class="film-thumb active"
                      data-video-id="home-think-1"
                      data-tag="Think Piece"
                      data-embed-id="think11111aa"
                      data-title="Why Cooper Flagg Fits Anywhere"
                      data-channel="The Ringer"
                      data-time="2 days ago"
                      data-duration="12:34"
                      data-mentions='[{"slug":"cooper-flagg","display_name":"Cooper Flagg"}]'
                    >
                      <span class="film-thumb__info">
                        <span class="film-thumb__title">Why Cooper Flagg Fits Anywhere</span>
                      </span>
                    </button>
                    <button
                      type="button"
                      class="film-thumb"
                      data-video-id="home-think-2"
                      data-tag="Think Piece"
                      data-embed-id="think22222bb"
                      data-title="Ace Bailey Shot Creation Deep Dive"
                      data-channel="No Ceilings"
                      data-time="1 day ago"
                      data-duration="10:01"
                      data-mentions='[{"slug":"ace-bailey","display_name":"Ace Bailey"},{"slug":"cooper-flagg","display_name":"Cooper Flagg"}]'
                    >
                      <span class="film-thumb__info">
                        <span class="film-thumb__title">Ace Bailey Shot Creation Deep Dive</span>
                      </span>
                    </button>
                    <button
                      type="button"
                      class="film-thumb"
                      data-video-id="home-conv-1"
                      data-tag="Conversation"
                      data-embed-id="conv33333cc"
                      data-title="Dylan Harper Draft Stock Check-In"
                      data-channel="Game Theory"
                      data-time="3 hours ago"
                      data-duration="45:00"
                      data-mentions='[{"slug":"dylan-harper","display_name":"Dylan Harper"}]'
                    >
                      <span class="film-thumb__info">
                        <span class="film-thumb__title">Dylan Harper Draft Stock Check-In</span>
                      </span>
                    </button>
                  </div>
                </div>
              </div>

              <div class="film-empty-tab" id="homeFilmEmptyTab" hidden>
                <div class="film-empty-tab__title" id="homeFilmEmptyTitle"></div>
                <div class="film-empty-tab__subtitle" id="homeFilmEmptySubtitle"></div>
              </div>
            </div>
          </section>
        </body>
        </html>
        """
    )


def _player_markup() -> str:
    """Return representative player-page Film Study markup."""
    return dedent(
        """
        <!DOCTYPE html>
        <html lang="en">
        <body>
          <section class="section" id="playerFilmStudySection" data-player-name="Ace Bailey">
            <div class="film-room film-room--player sprocket-border">
              <div class="film-tabs" id="filmStudyTabs">
                <button type="button" class="film-tab active" data-tag="Highlights" data-label="Highlights">
                  Highlights <span class="film-tab__count">(2)</span>
                </button>
                <button type="button" class="film-tab" data-tag="Montage" data-label="Montage">
                  Montage <span class="film-tab__count">(1)</span>
                </button>
              </div>

              <div class="film-playlist" id="playerFilmPlaylist">
                <div class="film-player">
                  <div class="film-player__embed">
                    <iframe
                      id="playerFilmEmbed"
                      src="https://www.youtube.com/embed/high11111aa?rel=0&modestbranding=1"
                      title="Ace Bailey Full Highlights"
                      loading="lazy"
                      allowfullscreen
                    ></iframe>
                  </div>

                  <div class="film-player__info">
                    <h3 class="film-player__video-title" id="playerFilmTitle">Ace Bailey Full Highlights</h3>
                    <div class="film-player__meta">
                      <span class="film-player__channel" id="playerFilmChannel">Overtime</span>
                      <span id="playerFilmTime">4 days ago</span>
                      <span id="playerFilmDuration">8:45</span>
                    </div>
                    <div class="film-player__tags">
                      <span class="video-type-tag" id="playerFilmTypeTag">Highlights</span>
                      <a href="/players/ace-bailey" class="film-player-tag">Ace Bailey</a>
                    </div>
                  </div>
                </div>

                <div class="film-playlist-list">
                  <div class="film-playlist__header">
                    <span class="film-playlist__label" id="playerFilmPlaylistLabel">Highlights</span>
                    <span class="film-playlist__count" id="playerFilmPlaylistCount"></span>
                  </div>
                  <div class="film-playlist__items" id="playerFilmPlaylistItems">
                    <button
                      type="button"
                      class="film-thumb active"
                      data-video-id="player-high-1"
                      data-tag="Highlights"
                      data-embed-id="high11111aa"
                      data-title="Ace Bailey Full Highlights"
                      data-channel="Overtime"
                      data-time="4 days ago"
                      data-duration="8:45"
                    >
                      <span class="film-thumb__info">
                        <span class="film-thumb__title">Ace Bailey Full Highlights</span>
                      </span>
                    </button>
                    <button
                      type="button"
                      class="film-thumb"
                      data-video-id="player-high-2"
                      data-tag="Highlights"
                      data-embed-id="high22222bb"
                      data-title="Ace Bailey Finishing Package"
                      data-channel="The Box and One"
                      data-time="2 days ago"
                      data-duration="6:12"
                    >
                      <span class="film-thumb__info">
                        <span class="film-thumb__title">Ace Bailey Finishing Package</span>
                      </span>
                    </button>
                    <button
                      type="button"
                      class="film-thumb"
                      data-video-id="player-montage-1"
                      data-tag="Montage"
                      data-embed-id="mont33333cc"
                      data-title="Ace Bailey Summer Montage"
                      data-channel="SLAM"
                      data-time="12 hours ago"
                      data-duration="3:30"
                    >
                      <span class="film-thumb__info">
                        <span class="film-thumb__title">Ace Bailey Summer Montage</span>
                      </span>
                    </button>
                  </div>
                </div>
              </div>

              <div class="film-empty-tab" id="playerFilmEmptyTab" hidden>
                <div class="film-empty-tab__title" id="playerFilmEmptyTitle"></div>
                <div class="film-empty-tab__subtitle" id="playerFilmEmptySubtitle"></div>
              </div>
            </div>
          </section>
        </body>
        </html>
        """
    )


class TestFilmRoomInteractions:
    """Behavioral browser coverage for shared film-player modules."""

    def test_homepage_tabs_overlay_and_thumbs_work(
        self, page: Page, screenshots_dir: Path
    ) -> None:
        """Homepage module switches tabs, updates mentions, and plays videos."""
        _bootstrap_script(page, _homepage_markup())

        expect(page.locator("#homeFilmTitle")).to_have_text(
            "Why Cooper Flagg Fits Anywhere"
        )
        expect(page.locator("#homeFilmPlaylistLabel")).to_have_text("Up Next")
        expect(page.locator("#homeFilmPlaylistCount")).to_have_text("2 videos")
        expect(page.locator("#homeFilmTags .film-player-tag")).to_have_text(
            ["Cooper Flagg"]
        )

        page.locator('#homeFilmTabs .film-tab[data-tag="Conversation"]').click()
        expect(page.locator("#homeFilmTitle")).to_have_text(
            "Dylan Harper Draft Stock Check-In"
        )
        expect(page.locator("#homeFilmPlaylistCount")).to_have_text("1 video")
        expect(page.locator('[data-video-id="home-think-1"]')).to_be_hidden()
        expect(page.locator('[data-video-id="home-conv-1"]')).to_be_visible()
        expect(page.locator("#homeFilmTags .film-player-tag")).to_have_text(
            ["Dylan Harper"]
        )

        page.locator("#homeFilmPlayOverlay").click()
        expect(page.locator("#homeFilmEmbed")).to_have_attribute(
            "src", re.compile(r"conv33333cc.*autoplay=1")
        )
        expect(page.locator("#homeFilmPlayOverlay")).to_be_hidden()

        page.locator('#homeFilmTabs .film-tab[data-tag="Think Piece"]').click()
        page.locator('[data-video-id="home-think-2"]').click()
        expect(page.locator("#homeFilmTitle")).to_have_text(
            "Ace Bailey Shot Creation Deep Dive"
        )
        expect(page.locator("#homeFilmEmbed")).to_have_attribute(
            "src", re.compile(r"think22222bb.*autoplay=1")
        )
        expect(page.locator("#homeFilmTags .film-player-tag")).to_have_text(
            ["Ace Bailey", "Cooper Flagg"]
        )
        page.locator("#filmRoomHomeSection").screenshot(
            path=str(screenshots_dir / "film_room_home_interaction.png")
        )

    def test_player_page_tabs_and_thumbs_work(
        self, page: Page, screenshots_dir: Path
    ) -> None:
        """Player module switches tabs and updates the embedded video."""
        _bootstrap_script(page, _player_markup())

        expect(page.locator("#playerFilmTitle")).to_have_text(
            "Ace Bailey Full Highlights"
        )
        expect(page.locator("#playerFilmPlaylistLabel")).to_have_text("Highlights")
        expect(page.locator("#playerFilmPlaylistCount")).to_have_text("2 videos")

        page.locator('#filmStudyTabs .film-tab[data-tag="Montage"]').click()
        expect(page.locator("#playerFilmTitle")).to_have_text(
            "Ace Bailey Summer Montage"
        )
        expect(page.locator("#playerFilmPlaylistLabel")).to_have_text("Montage")
        expect(page.locator("#playerFilmPlaylistCount")).to_have_text("1 video")
        expect(page.locator('[data-video-id="player-high-1"]')).to_be_hidden()
        expect(page.locator("#playerFilmEmbed")).to_have_attribute(
            "src", re.compile(r"mont33333cc")
        )

        page.locator('#filmStudyTabs .film-tab[data-tag="Highlights"]').click()
        page.locator('[data-video-id="player-high-2"]').click()
        expect(page.locator("#playerFilmTitle")).to_have_text(
            "Ace Bailey Finishing Package"
        )
        expect(page.locator("#playerFilmEmbed")).to_have_attribute(
            "src", re.compile(r"high22222bb")
        )
        expect(page.locator(".film-player__tags .film-player-tag")).to_have_text(
            "Ace Bailey"
        )
        page.locator("#playerFilmStudySection").screenshot(
            path=str(screenshots_dir / "film_room_player_interaction.png")
        )
