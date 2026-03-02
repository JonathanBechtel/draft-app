/**
 * ============================================================================
 * FILM-ROOM.JS — Interactions for /film-room and player film-study tabs.
 * ============================================================================
 */

const FilmRoomPageModule = {
  init() {
    const embed = document.getElementById('filmRoomEmbed');
    const cards = Array.from(document.querySelectorAll('.film-room-card'));
    if (!embed || cards.length === 0) return;

    cards.forEach((card) => {
      card.addEventListener('click', () => {
        cards.forEach((item) => item.classList.remove('film-room-card--active'));
        card.classList.add('film-room-card--active');

        const embedId = card.dataset.embedId || '';
        if (embedId) {
          embed.src = `https://www.youtube.com/embed/${encodeURIComponent(embedId)}?rel=0&modestbranding=1`;
        }

        this.updateFeaturedMeta(card);
      });
    });
  },

  updateFeaturedMeta(card) {
    const title = document.getElementById('filmRoomFeaturedTitle');
    const summary = document.getElementById('filmRoomFeaturedSummary');
    const channel = document.getElementById('filmRoomFeaturedChannel');
    const time = document.getElementById('filmRoomFeaturedTime');
    const views = document.getElementById('filmRoomFeaturedViews');

    if (title) title.textContent = card.dataset.title || '';
    if (summary) summary.textContent = card.dataset.summary || '';
    if (channel) channel.textContent = card.dataset.channel || '';
    if (time) time.textContent = card.dataset.time || '';
    if (views) views.textContent = card.dataset.views || '';
  },
};

const PlayerFilmStudyModule = {
  init() {
    const section = document.getElementById('playerFilmStudySection');
    if (!section) return;

    const tabsContainer = section.querySelector('#filmStudyTabs');
    const playlist = section.querySelector('#playerFilmPlaylist');
    const playlistItems = section.querySelector('#playerFilmPlaylistItems');
    const emptyTab = section.querySelector('#playerFilmEmptyTab');
    const emptyTitle = section.querySelector('#playerFilmEmptyTitle');
    const emptySubtitle = section.querySelector('#playerFilmEmptySubtitle');
    const playlistLabel = section.querySelector('#playerFilmPlaylistLabel');
    const playlistCount = section.querySelector('#playerFilmPlaylistCount');

    const embed = section.querySelector('#playerFilmEmbed');
    const title = section.querySelector('#playerFilmTitle');
    const channel = section.querySelector('#playerFilmChannel');
    const time = section.querySelector('#playerFilmTime');
    const duration = section.querySelector('#playerFilmDuration');
    const typeTag = section.querySelector('#playerFilmTypeTag');

    if (!tabsContainer || !playlist || !playlistItems) return;

    const tabs = Array.from(tabsContainer.querySelectorAll('.film-tab'));
    const thumbs = Array.from(playlistItems.querySelectorAll('.film-thumb'));
    if (!tabs.length || !thumbs.length) return;

    const playerName = section.dataset.playerName || 'this player';
    const videoTypeClassByTag = {
      'Think Piece': 'video-type-tag--think-piece',
      Conversation: 'video-type-tag--conversation',
      'Scouting Report': 'video-type-tag--scouting-report',
      Highlights: 'video-type-tag--highlights',
      Montage: 'video-type-tag--montage',
    };
    const allTypeClasses = Object.values(videoTypeClassByTag);

    const updateTypeTagClass = (tagText) => {
      if (!typeTag) return;
      typeTag.classList.remove(...allTypeClasses);
      const match = videoTypeClassByTag[tagText || ''];
      if (match) typeTag.classList.add(match);
    };

    const updateFeaturedFromThumb = (thumb) => {
      thumbs.forEach((item) => item.classList.remove('active'));
      thumb.classList.add('active');

      const embedId = thumb.dataset.embedId || '';
      if (embed && embedId) {
        embed.src = `https://www.youtube.com/embed/${encodeURIComponent(embedId)}?rel=0&modestbranding=1`;
      }

      if (title) title.textContent = thumb.dataset.title || '';
      if (channel) channel.textContent = thumb.dataset.channel || '';
      if (time) time.textContent = thumb.dataset.time || '';
      if (duration) duration.textContent = thumb.dataset.duration || '';
      if (typeTag) typeTag.textContent = thumb.dataset.tag || '';
      updateTypeTagClass(thumb.dataset.tag || '');
    };

    const refreshPlaylistForTab = (tab) => {
      tabs.forEach((item) => item.classList.remove('active'));
      tab.classList.add('active');

      const tag = tab.dataset.tag || '';
      const label = tab.dataset.label || tag;

      if (playlistLabel) playlistLabel.textContent = label;

      const visibleThumbs = [];
      thumbs.forEach((thumb) => {
        const isVisible = thumb.dataset.tag === tag;
        thumb.hidden = !isVisible;
        if (isVisible) visibleThumbs.push(thumb);
      });

      const countLabel = visibleThumbs.length === 1 ? 'video' : 'videos';
      if (playlistCount) {
        playlistCount.textContent = `${visibleThumbs.length} ${countLabel}`;
      }

      if (visibleThumbs.length === 0) {
        playlist.hidden = true;
        if (emptyTab) emptyTab.hidden = false;

        if (emptyTitle) {
          emptyTitle.textContent = `No ${label.toLowerCase()} yet for ${playerName}`;
        }
        if (emptySubtitle) {
          emptySubtitle.textContent = 'Check back soon or browse another category';
        }
        return;
      }

      playlist.hidden = false;
      if (emptyTab) emptyTab.hidden = true;

      const current = visibleThumbs.find((thumb) => thumb.classList.contains('active'));
      updateFeaturedFromThumb(current || visibleThumbs[0]);
    };

    tabs.forEach((tab) => {
      tab.addEventListener('click', () => refreshPlaylistForTab(tab));
    });

    thumbs.forEach((thumb) => {
      thumb.addEventListener('click', () => {
        if (thumb.hidden) return;
        updateFeaturedFromThumb(thumb);
      });
    });

    const initialTab = tabs.find((tab) => tab.classList.contains('active')) || tabs[0];
    refreshPlaylistForTab(initialTab);
  },
};

document.addEventListener('DOMContentLoaded', () => {
  FilmRoomPageModule.init();
  PlayerFilmStudyModule.init();
});
