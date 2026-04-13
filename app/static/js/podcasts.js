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
          <div class="podcast-featured__actions">
            <a href="${esc(ep.episode_url || ep.audio_url)}" target="_blank" rel="noopener noreferrer" class="listen-cta">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width: 1rem; height: 1rem;">
                <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>
                <polyline points="15 3 21 3 21 9"></polyline>
                <line x1="10" y1="14" x2="21" y2="3"></line>
              </svg>
              ${esc(ep.listen_on_text || 'Listen to Episode')}
            </a>
          </div>
        </div>
      </div>
    `;

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
          <a href="${esc(ep.episode_url || ep.audio_url)}" target="_blank" rel="noopener noreferrer" class="episode-row__listen" aria-label="${esc(ep.listen_on_text || 'Listen to Episode')}">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>
              <polyline points="15 3 21 3 21 9"></polyline>
              <line x1="10" y1="14" x2="21" y2="3"></line>
            </svg>
          </a>
        </div>
      `;
    }).join('');

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
    const activeShow = window.ACTIVE_SHOW;

    container.innerHTML = shows.map(s => {
      const isActive = activeShow === s.id;
      const href = isActive ? '/podcasts' : `/podcasts?show=${s.id}`;
      return `
        <a href="${href}" class="show-directory-item${isActive ? ' show-directory-item--active' : ''}">
          ${s.artwork_url
            ? `<img class="show-directory-item__art" src="${esc(s.artwork_url)}" alt="${esc(s.name)}" />`
            : `<div class="show-directory-item__art" style="display:flex;align-items:center;justify-content:center;background:var(--color-slate-100);font-family:var(--font-mono);color:var(--color-slate-400);font-size:0.625rem;">DG</div>`
          }
          <div class="show-directory-item__info">
            <div class="show-directory-item__name">${esc(s.name)}</div>
          </div>
        </a>
      `;
    }).join('');
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
