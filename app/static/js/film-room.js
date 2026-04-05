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
    this.initPlaylist();
    this.initLoadMore();
    this.initCardClicks();
  },

  initPlaylist() {
    const embed = document.getElementById('filmRoomEmbed');
    const playlistItems = document.getElementById('filmRoomPlaylistItems');
    if (!embed || !playlistItems) return;

    const thumbs = Array.from(playlistItems.querySelectorAll('.film-thumb'));
    if (thumbs.length === 0) return;

    const titleEl = document.getElementById('filmRoomTitle');
    const channelEl = document.getElementById('filmRoomChannel');
    const timeEl = document.getElementById('filmRoomTime');
    const durationEl = document.getElementById('filmRoomDuration');
    const viewsEl = document.getElementById('filmRoomViews');
    const typeTagEl = document.getElementById('filmRoomTypeTag');
    const tagsContainer = document.getElementById('filmRoomTags');

    const buildPlayerTags = (mentions) => {
      if (!tagsContainer) return;
      const existing = tagsContainer.querySelectorAll('.film-player-tag');
      existing.forEach((el) => el.remove());

      (mentions || []).slice(0, 10).forEach((p) => {
        const a = document.createElement('a');
        a.href = `/players/${p.slug}`;
        a.className = 'film-player-tag';
        a.textContent = p.display_name;
        tagsContainer.appendChild(a);
      });
    };

    thumbs.forEach((thumb) => {
      thumb.addEventListener('click', () => {
        thumbs.forEach((item) => item.classList.remove('active'));
        thumb.classList.add('active');

        const embedId = thumb.dataset.embedId || '';
        if (embedId) {
          embed.src = `https://www.youtube.com/embed/${encodeURIComponent(embedId)}?rel=0&modestbranding=1`;
        }

        if (titleEl) titleEl.textContent = thumb.dataset.title || '';
        if (channelEl) {
          const channelUrl = thumb.dataset.channelUrl || '';
          const channelName = thumb.dataset.channel || '';
          if (channelUrl && channelEl.tagName === 'A') {
            channelEl.href = channelUrl;
            channelEl.textContent = '';
            channelEl.insertAdjacentText('afterbegin', channelName);
            if (!channelEl.querySelector('.film-player__channel-icon')) {
              const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
              svg.classList.add('film-player__channel-icon');
              svg.setAttribute('viewBox', '0 0 24 24');
              svg.setAttribute('fill', 'currentColor');
              svg.setAttribute('aria-hidden', 'true');
              svg.innerHTML = '<path d="M23.5 6.2a3 3 0 0 0-2.1-2.1C19.5 3.5 12 3.5 12 3.5s-7.5 0-9.4.6A3 3 0 0 0 .5 6.2 31.9 31.9 0 0 0 0 12a31.9 31.9 0 0 0 .5 5.8 3 3 0 0 0 2.1 2.1c1.9.6 9.4.6 9.4.6s7.5 0 9.4-.6a3 3 0 0 0 2.1-2.1c.4-1.9.5-5.8.5-5.8s0-3.9-.5-5.8zM9.6 15.5V8.5l6.3 3.5-6.3 3.5z"/>';
              channelEl.appendChild(svg);
            }
            channelEl.title = `Visit ${channelName} on YouTube`;
          } else {
            channelEl.textContent = channelName;
          }
        }
        if (timeEl) timeEl.textContent = thumb.dataset.time || '';
        if (durationEl) durationEl.textContent = thumb.dataset.duration || '';
        if (viewsEl) viewsEl.textContent = thumb.dataset.views || '';

        if (typeTagEl) {
          typeTagEl.textContent = thumb.dataset.tag || '';
          updateVideoTypeTagClass(typeTagEl, thumb.dataset.tag || '');
        }

        let mentions = [];
        try {
          mentions = JSON.parse(thumb.dataset.mentions || '[]');
        } catch (_e) {
          /* ignore malformed data */
        }
        buildPlayerTags(mentions);
      });
    });
  },

  initLoadMore() {
    const btn = document.getElementById('filmLoadMoreBtn');
    const grid = document.getElementById('filmGrid');
    const loadMoreWrap = document.getElementById('filmLoadMore');
    if (!btn || !grid || !loadMoreWrap) return;

    const state = window.__filmRoomState;
    if (!state) return;

    let currentOffset = state.offset + state.limit;

    btn.addEventListener('click', async () => {
      btn.disabled = true;
      btn.textContent = 'Loading...';

      const params = new URLSearchParams();
      params.set('format', 'json');
      params.set('offset', String(currentOffset));
      if (state.tag) params.set('tag', state.tag);
      if (state.channel) params.set('channel', String(state.channel));
      if (state.player) params.set('player', String(state.player));
      if (state.search) params.set('search', state.search);

      try {
        const resp = await fetch(`/film-room?${params.toString()}`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();

        data.videos.forEach((video) => {
          const card = document.createElement('div');
          card.className = 'film-card';
          card.dataset.embedId = video.youtube_embed_id || '';

          const tagSlug = (video.tag || '').toLowerCase().replace(/\s+/g, '-');
          const typeTagClass = VIDEO_TYPE_CLASS_BY_TAG[video.tag || ''] || '';

          // Build image section
          const imageDiv = document.createElement('div');
          imageDiv.className = 'film-card__image';

          if (video.thumbnail_url) {
            const img = document.createElement('img');
            img.src = video.thumbnail_url;
            img.alt = video.title || '';
            img.loading = 'lazy';
            imageDiv.appendChild(img);
          } else {
            const placeholder = document.createElement('div');
            placeholder.className = 'film-card__image-placeholder';
            placeholder.innerHTML =
              '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>';
            imageDiv.appendChild(placeholder);
          }

          const durationBadge = document.createElement('span');
          durationBadge.className = 'film-card__duration';
          durationBadge.textContent = video.duration || '';
          imageDiv.appendChild(durationBadge);

          const typeBadge = document.createElement('div');
          typeBadge.className = 'film-card__type-badge';
          const typeSpan = document.createElement('span');
          typeSpan.className = `video-type-tag video-type-tag--${tagSlug}`;
          typeSpan.textContent = video.tag || '';
          typeBadge.appendChild(typeSpan);
          imageDiv.appendChild(typeBadge);

          card.appendChild(imageDiv);

          // Build body section
          const bodyDiv = document.createElement('div');
          bodyDiv.className = 'film-card__body';

          const titleDiv = document.createElement('div');
          titleDiv.className = 'film-card__title';
          titleDiv.textContent = video.title || '';
          bodyDiv.appendChild(titleDiv);

          const channelDiv = document.createElement('div');
          channelDiv.className = 'film-card__channel';
          channelDiv.textContent = video.channel_name || '';
          bodyDiv.appendChild(channelDiv);

          const metaDiv = document.createElement('div');
          metaDiv.className = 'film-card__meta';
          const timeSpan = document.createElement('span');
          timeSpan.textContent = video.time || '';
          const dotSpan = document.createElement('span');
          dotSpan.className = 'meta-dot';
          const viewsSpan = document.createElement('span');
          viewsSpan.textContent = video.view_count_display || '';
          metaDiv.appendChild(timeSpan);
          metaDiv.appendChild(dotSpan);
          metaDiv.appendChild(viewsSpan);
          bodyDiv.appendChild(metaDiv);

          // Player tags
          const mentions = video.mentioned_players || [];
          if (mentions.length > 0) {
            const tagsDiv = document.createElement('div');
            tagsDiv.className = 'film-card__tags';
            mentions.slice(0, 10).forEach((p) => {
              const a = document.createElement('a');
              a.href = `/players/${p.slug}`;
              a.className = 'film-player-tag';
              a.textContent = p.display_name;
              tagsDiv.appendChild(a);
            });
            bodyDiv.appendChild(tagsDiv);
          }

          card.appendChild(bodyDiv);
          grid.appendChild(card);
        });

        currentOffset += state.limit;
        if (!data.has_more) {
          loadMoreWrap.remove();
        } else {
          btn.disabled = false;
          btn.textContent = 'Load More Videos';
        }
      } catch (_err) {
        btn.disabled = false;
        btn.textContent = 'Load More Videos';
      }
    });
  },

  initCardClicks() {
    const grid = document.getElementById('filmGrid');
    const playlistItems = document.getElementById('filmRoomPlaylistItems');
    const featured = document.querySelector('.film-page__featured');
    if (!grid || !playlistItems || !featured) return;

    grid.addEventListener('click', (e) => {
      const card = e.target.closest('.film-card');
      if (!card) return;
      // Don't intercept clicks on player tag links
      if (e.target.closest('a')) return;

      const embedId = card.dataset.embedId;
      if (!embedId) return;

      // Find the matching thumb in the playlist and click it
      const thumb = playlistItems.querySelector(
        `.film-thumb[data-embed-id="${CSS.escape(embedId)}"]`,
      );
      if (thumb) {
        thumb.click();
        featured.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    });
  },
};

const HomeFilmRoomModule = {
  init() {
    const section = document.getElementById('filmRoomHomeSection');
    if (!section) return;

    const embedWrap = section.querySelector('#homeFilmEmbedWrap');
    const embed = section.querySelector('#homeFilmEmbed');
    const poster = section.querySelector('#homeFilmPoster');
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
      if (poster) poster.hidden = true;
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
        if (poster) {
          poster.src = thumb.dataset.thumbnail || '';
          poster.alt = thumb.dataset.title || '';
          poster.hidden = false;
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

const FilmSearchModule = {
  DEBOUNCE_MS: 300,
  MIN_QUERY: 2,
  input: null,
  results: null,
  form: null,
  controller: null,
  debounceTimer: null,
  items: [],
  selectedIndex: -1,
  blurTimer: null,

  init() {
    this.input = document.getElementById('filmSearchInput');
    this.results = document.getElementById('filmSearchResults');
    if (!this.input || !this.results) return;
    this.form = this.input.closest('form');

    this.input.addEventListener('input', () => this.onInput());
    this.input.addEventListener('keydown', (e) => this.onKeydown(e));
    this.input.addEventListener('focus', () => this.onFocus());
    this.input.addEventListener('blur', () => this.onBlur());
    if (this.form) {
      this.form.addEventListener('submit', (e) => this.onSubmit(e));
    }
  },

  onInput() {
    clearTimeout(this.debounceTimer);
    const q = (this.input.value || '').trim();
    if (q.length < this.MIN_QUERY) {
      this.close();
      return;
    }
    this.debounceTimer = setTimeout(() => this.fetchSuggestions(q), this.DEBOUNCE_MS);
  },

  async fetchSuggestions(query) {
    if (this.controller) this.controller.abort();
    this.controller = new AbortController();

    try {
      const resp = await fetch(
        `/api/videos/search-suggestions?q=${encodeURIComponent(query)}`,
        { signal: this.controller.signal },
      );
      if (!resp.ok) return;
      const data = await resp.json();
      this.render(data.suggestions || []);
    } catch (_e) {
      /* aborted or network error */
    }
  },

  escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  },

  render(suggestions) {
    if (suggestions.length === 0) {
      this.results.innerHTML =
        '<div class="film-search__empty">No matches found</div>';
      this.open();
      this.items = [];
      this.selectedIndex = -1;
      return;
    }

    const groups = {};
    const order = [];
    suggestions.forEach((s) => {
      if (!groups[s.category]) {
        groups[s.category] = [];
        order.push(s.category);
      }
      groups[s.category].push(s);
    });

    const categoryLabels = {
      player: 'Players',
      channel: 'Channels',
      video: 'Videos',
    };
    let html = '';
    let itemIndex = 0;

    order.forEach((cat) => {
      html += `<div class="film-search__category">${this.escapeHtml(categoryLabels[cat] || cat)}</div>`;
      groups[cat].forEach((s) => {
        const sublabelHtml = s.sublabel
          ? `<span class="film-search__item-sublabel">${this.escapeHtml(s.sublabel)}</span>`
          : '';
        html += `<div class="film-search__item" role="option" data-index="${itemIndex}"
                      data-category="${this.escapeHtml(s.category)}"
                      data-player-id="${s.player_id || ''}"
                      data-channel-id="${s.channel_id || ''}"
                      data-search-term="${this.escapeHtml(s.search_term || '')}">
                   <span class="film-search__item-label">${this.escapeHtml(s.label)}</span>
                   ${sublabelHtml}
                 </div>`;
        itemIndex++;
      });
    });

    this.results.innerHTML = html;
    this.items = Array.from(this.results.querySelectorAll('.film-search__item'));
    this.selectedIndex = -1;

    this.items.forEach((item) => {
      item.addEventListener('mousedown', (e) => {
        e.preventDefault();
        this.applyFilter(item);
      });
    });

    this.open();
  },

  open() {
    this.results.classList.add('open');
    this.input.setAttribute('aria-expanded', 'true');
  },

  close() {
    this.results.classList.remove('open');
    this.input.setAttribute('aria-expanded', 'false');
    this.selectedIndex = -1;
    this.items.forEach((item) => item.classList.remove('selected'));
  },

  onFocus() {
    clearTimeout(this.blurTimer);
    if (this.items.length > 0 || this.results.querySelector('.film-search__empty')) {
      this.open();
    }
  },

  onBlur() {
    this.blurTimer = setTimeout(() => this.close(), 200);
  },

  onKeydown(e) {
    if (!this.results.classList.contains('open') || this.items.length === 0)
      return;

    if (e.key === 'ArrowDown') {
      e.preventDefault();
      this.selectedIndex = Math.min(
        this.selectedIndex + 1,
        this.items.length - 1,
      );
      this.highlightItem();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      this.selectedIndex = Math.max(this.selectedIndex - 1, 0);
      this.highlightItem();
    } else if (e.key === 'Enter' && this.selectedIndex >= 0) {
      e.preventDefault();
      this.applyFilter(this.items[this.selectedIndex]);
    } else if (e.key === 'Escape') {
      this.close();
    }
  },

  highlightItem() {
    this.items.forEach((item, i) => {
      item.classList.toggle('selected', i === this.selectedIndex);
    });
  },

  onSubmit(e) {
    if (this.selectedIndex >= 0 && this.items[this.selectedIndex]) {
      e.preventDefault();
      this.applyFilter(this.items[this.selectedIndex]);
    }
  },

  applyFilter(item) {
    const category = item.dataset.category;
    const state = window.__filmRoomState || {};
    const params = new URLSearchParams();

    if (state.tag) params.set('tag', state.tag);

    if (category === 'player') {
      params.set('player', item.dataset.playerId);
    } else if (category === 'channel') {
      params.set('channel', item.dataset.channelId);
    } else if (category === 'video') {
      params.set('search', item.dataset.searchTerm);
    }

    const qs = params.toString();
    window.location.href = `/film-room${qs ? '?' + qs : ''}`;
  },
};

document.addEventListener('DOMContentLoaded', () => {
  FilmRoomPageModule.init();
  HomeFilmRoomModule.init();
  PlayerFilmStudyModule.init();
  FilmSearchModule.init();
});
