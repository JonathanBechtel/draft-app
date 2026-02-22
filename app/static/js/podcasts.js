/**
 * ============================================================================
 * PODCASTS.JS — JavaScript for the dedicated /podcasts page
 * Audio player, hero, episode list, filters, sidebar, pagination
 * ============================================================================
 */

const TAG_CLASSES = {
  'Draft Analysis': 'draft-analysis',
  'Mock Draft': 'mock-draft',
  'Game Breakdown': 'game-breakdown',
  'Interview': 'interview',
  'Trade & Intel': 'trade-intel',
  'Prospect Debate': 'prospect-debate',
  'Mailbag': 'mailbag',
  'Event Preview': 'event-preview',
};

/**
 * ============================================================================
 * PODCAST AUDIO — Shared Audio() manager
 * ============================================================================
 */
const PodcastAudio = {
  audio: null,
  activeBtn: null,
  activeProgress: null,
  activeTime: null,

  init() {
    this.audio = new Audio();
    this.audio.addEventListener('timeupdate', () => this._onTimeUpdate());
    this.audio.addEventListener('ended', () => this._onEnded());
  },

  play(audioUrl, btn) {
    if (!this.audio) this.init();

    // If same button clicked, toggle pause/play
    if (this.activeBtn === btn && !this.audio.paused) {
      this.pause();
      return;
    }

    // If different source, load new
    if (this.audio.src !== audioUrl) {
      this.audio.src = audioUrl;
    }

    // Reset previous active button
    if (this.activeBtn && this.activeBtn !== btn) {
      this._resetBtn(this.activeBtn);
    }

    this.activeBtn = btn;
    // Find the progress bar and time display near this button
    const row = btn.closest('.podcast-featured__player, .episode-row--page, .episode-row__inner');
    if (row) {
      this.activeProgress = row.querySelector('.progress-bar');
      this.activeTime = row.querySelector('.progress-time');
    }

    btn.innerHTML = '<svg viewBox="0 0 24 24"><rect x="6" y="4" width="4" height="16"></rect><rect x="14" y="4" width="4" height="16"></rect></svg>';
    this.audio.play();
  },

  pause() {
    if (this.audio) this.audio.pause();
    if (this.activeBtn) {
      this._resetBtn(this.activeBtn);
    }
  },

  seek(value) {
    if (this.audio && this.audio.duration) {
      this.audio.currentTime = (value / 100) * this.audio.duration;
    }
  },

  _onTimeUpdate() {
    if (!this.audio || !this.audio.duration) return;
    const pct = (this.audio.currentTime / this.audio.duration) * 100;
    if (this.activeProgress) {
      this.activeProgress.value = pct;
    }
    if (this.activeTime) {
      this.activeTime.textContent = this._formatTime(this.audio.currentTime);
    }
  },

  _onEnded() {
    if (this.activeBtn) this._resetBtn(this.activeBtn);
    if (this.activeProgress) this.activeProgress.value = 0;
    if (this.activeTime) this.activeTime.textContent = '0:00';
    this.activeBtn = null;
  },

  _resetBtn(btn) {
    btn.innerHTML = '<svg viewBox="0 0 24 24"><polygon points="5,3 19,12 5,21"></polygon></svg>';
  },

  _formatTime(seconds) {
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${m}:${s.toString().padStart(2, '0')}`;
  }
};

/**
 * ============================================================================
 * HERO MODULE — Featured episode at the top
 * ============================================================================
 */
const PodcastHeroModule = {
  init(episodes) {
    const container = document.getElementById('podcastHero');
    if (!container || !episodes || episodes.length === 0) return;

    const ep = episodes[0];
    const esc = DraftGuru.escapeHtml.bind(DraftGuru);
    const tagClass = TAG_CLASSES[ep.tag] || '';
    const episodeTag = tagClass
      ? `<span class="episode-tag episode-tag--lg episode-tag--${tagClass}" style="align-self: flex-start; margin-bottom: 0.375rem;">${esc(ep.tag)}</span>`
      : '';

    container.innerHTML = `
      <div class="podcast-featured" style="margin-bottom: 2rem;">
        <div class="podcast-featured__artwork">
          ${(ep.artwork_url || ep.show_artwork_url)
            ? `<img src="${esc(ep.artwork_url || ep.show_artwork_url)}" alt="${esc(ep.show_name)}" />`
            : `<div style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;background:var(--color-slate-200);font-family:var(--font-mono);color:var(--color-slate-500);font-size:2rem;">DG</div>`
          }
          <div class="podcast-featured__badge">
            <span class="pulse-dot"></span>
            Latest Episode
          </div>
        </div>
        <div class="podcast-featured__body">
          ${episodeTag}
          <div class="podcast-featured__show">${esc(ep.show_name)}</div>
          <div class="podcast-featured__title">${esc(ep.title)}</div>
          ${ep.summary ? `<div class="podcast-featured__summary">${esc(ep.summary)}</div>` : ''}
          <div class="podcast-featured__meta">
            <span>${esc(ep.duration)}</span>
            <span class="meta-dot"></span>
            <span>${esc(ep.time)}</span>
          </div>
          <div class="podcast-featured__player">
            <button class="play-btn" aria-label="Play episode" data-audio="${esc(ep.audio_url)}">
              <svg viewBox="0 0 24 24"><polygon points="5,3 19,12 5,21"></polygon></svg>
            </button>
            <div class="progress-track">
              <input type="range" class="progress-bar" min="0" max="100" value="0" step="0.1" />
              <span class="progress-time">0:00</span>
            </div>
          </div>
        </div>
      </div>
    `;

    // Bind play button
    const playBtn = container.querySelector('.play-btn');
    if (playBtn) {
      playBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        PodcastAudio.play(playBtn.dataset.audio, playBtn);
      });
    }

    // Bind progress bar seek
    const progressBar = container.querySelector('.progress-bar');
    if (progressBar) {
      progressBar.addEventListener('input', (e) => {
        PodcastAudio.seek(parseFloat(e.target.value));
      });
    }
  }
};

/**
 * ============================================================================
 * LIST MODULE — Episode list rendering
 * ============================================================================
 */
const PodcastListModule = {
  renderPageList(episodes, container) {
    if (!container || !episodes) return;

    if (episodes.length === 0) {
      container.innerHTML = '<div style="padding: 2rem; text-align: center; color: var(--color-slate-400); font-family: var(--font-mono); font-size: 0.8125rem;">No episodes match your filter</div>';
      return;
    }

    const esc = DraftGuru.escapeHtml.bind(DraftGuru);
    container.innerHTML = episodes.map(ep => {
      const tagClass = TAG_CLASSES[ep.tag] || '';
      const episodeTag = tagClass ? `<span class="episode-tag episode-tag--${tagClass}">${esc(ep.tag)}</span>` : '';

      return `
        <div class="episode-row--page" data-tag="${esc(ep.tag)}">
          ${(ep.artwork_url || ep.show_artwork_url)
            ? `<img class="episode-row__art" src="${esc(ep.artwork_url || ep.show_artwork_url)}" alt="${esc(ep.show_name)}" loading="lazy" />`
            : `<div class="episode-row__art" style="display:flex;align-items:center;justify-content:center;background:var(--color-slate-100);font-family:var(--font-mono);color:var(--color-slate-400);font-size:0.75rem;">DG</div>`
          }
          <div class="episode-row__info">
            <div class="episode-row__show-line">
              <span class="episode-row__show">${esc(ep.show_name)}</span>
              ${episodeTag}
            </div>
            <div class="episode-row__title">${esc(ep.title)}</div>
            ${ep.summary ? `<div class="episode-row__summary">${esc(ep.summary)}</div>` : ''}
            <div class="episode-row__meta">
              <span>${esc(ep.duration)}</span>
              <span class="meta-dot"></span>
              <span>${esc(ep.time)}</span>
            </div>
          </div>
          <button class="episode-row__play" aria-label="Play episode" data-audio="${esc(ep.audio_url)}">
            <svg viewBox="0 0 24 24"><polygon points="6,3 20,12 6,21"></polygon></svg>
          </button>
        </div>
      `;
    }).join('');

    // Bind play buttons
    container.querySelectorAll('.episode-row__play').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        PodcastAudio.play(btn.dataset.audio, btn);
      });
    });

    // Bind progress bars
    container.querySelectorAll('.progress-bar').forEach(bar => {
      bar.addEventListener('input', (e) => {
        PodcastAudio.seek(parseFloat(e.target.value));
      });
    });
  }
};

/**
 * ============================================================================
 * FILTERS MODULE — No-op; filtering is now server-side via URL params.
 * Kept as a namespace for potential future client-side enhancements.
 * ============================================================================
 */
const PodcastFiltersModule = {
  init() {}
};

/**
 * ============================================================================
 * SIDEBAR MODULE — Show directory + trending mentions
 * ============================================================================
 */
const PodcastSidebarModule = {
  renderShowDirectory(shows) {
    const container = document.getElementById('showDirectory');
    if (!container || !shows) return;

    if (shows.length === 0) {
      container.innerHTML = '<div style="color: var(--color-slate-400); font-size: 0.8125rem;">No shows available</div>';
      return;
    }

    const esc = DraftGuru.escapeHtml.bind(DraftGuru);
    container.innerHTML = shows.map(s => `
      <div class="show-directory-item">
        ${s.artwork_url
          ? `<img class="show-directory-item__art" src="${esc(s.artwork_url)}" alt="${esc(s.name)}" />`
          : `<div class="show-directory-item__art" style="display:flex;align-items:center;justify-content:center;background:var(--color-slate-100);font-family:var(--font-mono);color:var(--color-slate-400);font-size:0.625rem;">DG</div>`
        }
        <div class="show-directory-item__info">
          <div class="show-directory-item__name">${esc(s.name)}</div>
        </div>
      </div>
    `).join('');
  },

  renderTrendingMentions(trending) {
    const container = document.getElementById('trendingMentions');
    if (!container || !trending) return;

    if (trending.length === 0) {
      container.innerHTML = '<div style="color: var(--color-slate-400); font-size: 0.8125rem;">No trending players</div>';
      return;
    }

    const esc = DraftGuru.escapeHtml.bind(DraftGuru);
    const maxMentions = trending[0].mention_count || 1;

    container.innerHTML = trending.map((p, i) => {
      const pct = (p.mention_count / maxMentions) * 100;
      const href = p.slug ? `/players/${esc(p.slug)}` : '#';
      return `
        <a href="${href}" class="trending-mention">
          <span class="trending-mention__rank">${i + 1}</span>
          <span class="trending-mention__name">${esc(p.display_name)}</span>
          <span class="trending-mention__count">${p.mention_count}</span>
          <div class="trending-mention__bar">
            <div class="trending-mention__bar-fill" style="width: ${pct}%"></div>
          </div>
        </a>
      `;
    }).join('');
  }
};

/**
 * ============================================================================
 * PAGINATION MODULE — No-op; pagination is now server-side via URL params.
 * ============================================================================
 */
const PodcastPaginationModule = {};

/**
 * ============================================================================
 * INITIALIZATION
 * ============================================================================
 */
document.addEventListener('DOMContentLoaded', () => {
  const episodes = window.PODCAST_EPISODES || [];
  const shows = window.PODCAST_SHOWS || [];
  const trending = window.PODCAST_TRENDING || [];

  PodcastAudio.init();

  // Hero is only present on the first page with no tag filter
  const heroEl = document.getElementById('podcastHero');
  const hasHero = !!heroEl;
  if (hasHero) {
    PodcastHeroModule.init(episodes);
  }

  // Render episode list (skip first if hero is showing it)
  const listContainer = document.getElementById('pageList');
  const listEpisodes = hasHero ? episodes.slice(1) : episodes;
  PodcastListModule.renderPageList(listEpisodes, listContainer);

  PodcastSidebarModule.renderShowDirectory(shows);
  PodcastSidebarModule.renderTrendingMentions(trending);
});
