/**
 * ============================================================================
 * FILM-ROOM.JS — Interactions for /film-room and player film-study tabs.
 * ============================================================================
 */

const VIDEO_TYPE_CLASS_BY_TAG = {
  'Think Piece': 'video-type-tag--think-piece',
  Conversation: 'video-type-tag--conversation',
  'Scouting Report': 'video-type-tag--scouting-report',
  Highlights: 'video-type-tag--highlights',
  Montage: 'video-type-tag--montage',
};

const ALL_VIDEO_TYPE_CLASSES = Object.values(VIDEO_TYPE_CLASS_BY_TAG);

function updateVideoTypeTagClass(typeTag, tagText) {
  if (!typeTag) return;
  typeTag.classList.remove(...ALL_VIDEO_TYPE_CLASSES);
  const match = VIDEO_TYPE_CLASS_BY_TAG[tagText || ''];
  if (match) typeTag.classList.add(match);
}

function initTabbedFilmPlayer(config) {
  const {
    section,
    tabsContainerSelector,
    playlistSelector,
    playlistItemsSelector,
    emptyTabSelector,
    emptyTitleSelector,
    emptySubtitleSelector,
    playlistLabelSelector,
    playlistCountSelector,
    titleSelector,
    channelSelector,
    timeSelector,
    durationSelector,
    typeTagSelector,
    getPlaylistLabel = ({ label }) => label,
    getEmptyTitle = ({ label }) => `No ${label.toLowerCase()} yet`,
    getEmptySubtitle = () => 'Check back soon or browse another category',
    onFeaturedChange = () => {},
    onThumbClick = () => {},
  } = config;

  if (!section) return;

  const tabsContainer = section.querySelector(tabsContainerSelector);
  const playlist = section.querySelector(playlistSelector);
  const playlistItems = section.querySelector(playlistItemsSelector);
  const emptyTab = section.querySelector(emptyTabSelector);
  const emptyTitle = section.querySelector(emptyTitleSelector);
  const emptySubtitle = section.querySelector(emptySubtitleSelector);
  const playlistLabel = section.querySelector(playlistLabelSelector);
  const playlistCount = section.querySelector(playlistCountSelector);
  const title = section.querySelector(titleSelector);
  const channel = section.querySelector(channelSelector);
  const time = section.querySelector(timeSelector);
  const duration = section.querySelector(durationSelector);
  const typeTag = section.querySelector(typeTagSelector);

  if (!tabsContainer || !playlist || !playlistItems) return;

  const tabs = Array.from(tabsContainer.querySelectorAll('.film-tab'));
  const thumbs = Array.from(playlistItems.querySelectorAll('.film-thumb'));
  if (!tabs.length || !thumbs.length) return;

  const thumbCountsByTag = new Map();
  thumbs.forEach((thumb) => {
    const tag = thumb.dataset.tag || '';
    thumbCountsByTag.set(tag, (thumbCountsByTag.get(tag) || 0) + 1);
  });

  const getTabVideoCount = (tab) => {
    const explicitCount = Number.parseInt(tab.dataset.videoCount || '', 10);
    if (Number.isFinite(explicitCount)) return explicitCount;
    return thumbCountsByTag.get(tab.dataset.tag || '') || 0;
  };

  tabs.forEach((tab) => {
    const count = thumbCountsByTag.get(tab.dataset.tag || '') || 0;
    tab.dataset.videoCount = String(count);
    if (count === 0) {
      tab.disabled = true;
      tab.setAttribute('aria-disabled', 'true');
    } else {
      tab.disabled = false;
      tab.removeAttribute('aria-disabled');
    }
  });

  const updateFeaturedFromThumb = (thumb) => {
    thumbs.forEach((item) => item.classList.remove('active'));
    thumb.classList.add('active');

    if (title) title.textContent = thumb.dataset.title || '';
    if (channel) channel.textContent = thumb.dataset.channel || '';
    if (time) time.textContent = thumb.dataset.time || '';
    if (duration) duration.textContent = thumb.dataset.duration || '';
    if (typeTag) typeTag.textContent = thumb.dataset.tag || '';
    updateVideoTypeTagClass(typeTag, thumb.dataset.tag || '');

    onFeaturedChange({ section, thumb, thumbs, typeTag });
  };

  const refreshPlaylistForTab = (tab) => {
    if (getTabVideoCount(tab) === 0) return;

    tabs.forEach((item) => item.classList.remove('active'));
    tab.classList.add('active');

    const tag = tab.dataset.tag || '';
    const label = tab.dataset.label || tag;

    if (playlistLabel) {
      playlistLabel.textContent = getPlaylistLabel({ tag, label, tab });
    }

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
      if (emptyTitle) emptyTitle.textContent = getEmptyTitle({ tag, label, tab });
      if (emptySubtitle) {
        emptySubtitle.textContent = getEmptySubtitle({ tag, label, tab });
      }
      return;
    }

    playlist.hidden = false;
    if (emptyTab) emptyTab.hidden = true;

    const current = visibleThumbs.find((thumb) => thumb.classList.contains('active'));
    updateFeaturedFromThumb(current || visibleThumbs[0]);
  };

  tabs.forEach((tab) => {
    tab.addEventListener('click', () => {
      if (tab.disabled || getTabVideoCount(tab) === 0) return;
      refreshPlaylistForTab(tab);
    });
  });

  thumbs.forEach((thumb) => {
    thumb.addEventListener('click', () => {
      if (thumb.hidden) return;
      updateFeaturedFromThumb(thumb);
      onThumbClick({ section, thumb });
    });
  });

  const initialTab =
    tabs.find((tab) => tab.classList.contains('active') && getTabVideoCount(tab) > 0) ||
    tabs.find((tab) => getTabVideoCount(tab) > 0);
  if (!initialTab) return;
  refreshPlaylistForTab(initialTab);
}

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

const HomeFilmRoomModule = {
  init() {
    const section = document.getElementById('filmRoomHomeSection');
    if (!section) return;

    const embedWrap = section.querySelector('#homeFilmEmbedWrap');
    const embed = section.querySelector('#homeFilmEmbed');
    const playOverlay = section.querySelector('#homeFilmPlayOverlay');
    const tagsContainer = section.querySelector('#homeFilmTags');

    const buildPlayerTags = (mentions) => {
      if (!tagsContainer) return;

      const existing = tagsContainer.querySelectorAll('.film-player-tag');
      existing.forEach((el) => el.remove());

      (mentions || []).forEach((p) => {
        const a = document.createElement('a');
        a.href = `/players/${p.slug}`;
        a.className = 'film-player-tag';
        a.textContent = p.display_name;
        tagsContainer.appendChild(a);
      });
    };

    const startPlayback = () => {
      const embedId = embedWrap ? embedWrap.dataset.pendingEmbedId : '';
      if (embed && embedId) {
        embed.src = `https://www.youtube.com/embed/${encodeURIComponent(embedId)}?rel=0&modestbranding=1&autoplay=1`;
        embed.hidden = false;
      }
      if (playOverlay) playOverlay.hidden = true;
      if (embedWrap) embedWrap.classList.remove('film-player__embed--placeholder');
    };

    if (playOverlay) {
      playOverlay.addEventListener('click', startPlayback);
    }

    initTabbedFilmPlayer({
      section,
      tabsContainerSelector: '#homeFilmTabs',
      playlistSelector: '#homeFilmPlaylist',
      playlistItemsSelector: '#homeFilmPlaylistItems',
      emptyTabSelector: '#homeFilmEmptyTab',
      emptyTitleSelector: '#homeFilmEmptyTitle',
      emptySubtitleSelector: '#homeFilmEmptySubtitle',
      playlistLabelSelector: '#homeFilmPlaylistLabel',
      playlistCountSelector: '#homeFilmPlaylistCount',
      titleSelector: '#homeFilmTitle',
      channelSelector: '#homeFilmChannel',
      timeSelector: '#homeFilmTime',
      durationSelector: '#homeFilmDuration',
      typeTagSelector: '#homeFilmTypeTag',
      getPlaylistLabel: () => 'Up Next',
      onFeaturedChange: ({ thumb }) => {
        if (embed) {
          embed.src = '';
          embed.hidden = true;
        }
        if (embedWrap) {
          embedWrap.classList.add('film-player__embed--placeholder');
          embedWrap.dataset.pendingEmbedId = thumb.dataset.embedId || '';
        }
        if (playOverlay) playOverlay.hidden = false;

        let mentions = [];
        try {
          mentions = JSON.parse(thumb.dataset.mentions || '[]');
        } catch (_e) {
          /* ignore malformed data attributes */
        }
        buildPlayerTags(mentions);
      },
      onThumbClick: () => {
        startPlayback();
      },
    });
  },
};

const PlayerFilmStudyModule = {
  init() {
    const section = document.getElementById('playerFilmStudySection');
    if (!section) return;

    const embed = section.querySelector('#playerFilmEmbed');
    const playerName = section.dataset.playerName || 'this player';

    initTabbedFilmPlayer({
      section,
      tabsContainerSelector: '#filmStudyTabs',
      playlistSelector: '#playerFilmPlaylist',
      playlistItemsSelector: '#playerFilmPlaylistItems',
      emptyTabSelector: '#playerFilmEmptyTab',
      emptyTitleSelector: '#playerFilmEmptyTitle',
      emptySubtitleSelector: '#playerFilmEmptySubtitle',
      playlistLabelSelector: '#playerFilmPlaylistLabel',
      playlistCountSelector: '#playerFilmPlaylistCount',
      titleSelector: '#playerFilmTitle',
      channelSelector: '#playerFilmChannel',
      timeSelector: '#playerFilmTime',
      durationSelector: '#playerFilmDuration',
      typeTagSelector: '#playerFilmTypeTag',
      getEmptyTitle: ({ label }) => `No ${label.toLowerCase()} yet for ${playerName}`,
      onFeaturedChange: ({ thumb }) => {
        const embedId = thumb.dataset.embedId || '';
        if (embed && embedId) {
          embed.src = `https://www.youtube.com/embed/${encodeURIComponent(embedId)}?rel=0&modestbranding=1`;
        }
      },
    });
  },
};

document.addEventListener('DOMContentLoaded', () => {
  FilmRoomPageModule.init();
  HomeFilmRoomModule.init();
  PlayerFilmStudyModule.init();
});
