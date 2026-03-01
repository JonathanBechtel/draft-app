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
    const tabsContainer = document.getElementById('filmStudyTabs');
    const list = document.getElementById('filmStudyList');
    const empty = document.getElementById('filmStudyEmpty');
    if (!tabsContainer || !list || !empty) return;

    const tabs = Array.from(tabsContainer.querySelectorAll('.film-study-tab'));
    const cards = Array.from(list.querySelectorAll('.film-study-card'));
    const applyFilter = (tag) => {
      let visibleCount = 0;
      cards.forEach((card) => {
        const matches = tag === 'all' || card.dataset.tag === tag;
        card.hidden = !matches;
        if (matches) visibleCount += 1;
      });
      empty.hidden = visibleCount !== 0;
    };

    tabs.forEach((tab) => {
      tab.addEventListener('click', () => {
        tabs.forEach((btn) => btn.classList.remove('active'));
        tab.classList.add('active');
        applyFilter(tab.dataset.tag || 'all');
      });
    });
  },
};

document.addEventListener('DOMContentLoaded', () => {
  FilmRoomPageModule.init();
  PlayerFilmStudyModule.init();
});
