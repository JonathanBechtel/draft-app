/**
 * ============================================================================
 * NEWS.JS — JavaScript for the dedicated /news page
 * Hero, article grid, sidebar trending, initialization
 * ============================================================================
 */

/**
 * ============================================================================
 * HERO MODULE — Featured article at the top (reuses home.js HeroModule pattern)
 * ============================================================================
 */
const NewsHeroModule = {
  article: null,
  currentMode: 'gradient',

  categoryIcons: {
    'Scouting Report': '<path d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/><path d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/>',
    'Big Board': '<path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01"/>',
    'Mock Draft': '<path d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"/>',
    'Tier Update': '<path d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6"/>',
    'Game Recap': '<path d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"/><path d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>',
    'Film Study': '<path d="M7 4v16M17 4v16M3 8h4m10 0h4M3 12h18M3 16h4m10 0h4M4 20h16a1 1 0 001-1V5a1 1 0 00-1-1H4a1 1 0 00-1 1v14a1 1 0 001 1z"/>',
    'Skill Theme': '<path d="M13 10V3L4 14h7v7l9-11h-7z"/>',
    'Team Fit': '<path d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z"/>',
    'Draft Intel': '<path d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z"/>',
    'Statistical Analysis': '<path d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/>'
  },

  init() {
    this.article = window.NEWS_HERO;
    const section = document.getElementById('newsHeroSection');
    if (!this.article || !section) return;

    this.detectAndRender();
  },

  async detectAndRender() {
    const mode = await this.detectDisplayMode();
    this.currentMode = mode;
    this.render(mode);
  },

  detectDisplayMode() {
    return new Promise((resolve) => {
      if (!this.article?.image_url) {
        resolve('gradient');
        return;
      }
      const img = new Image();
      img.onload = () => {
        if (img.naturalWidth >= 800) resolve('full');
        else if (img.naturalWidth >= 400) resolve('split');
        else resolve('blurred');
      };
      img.onerror = () => resolve('gradient');
      img.src = this.article.image_url;
    });
  },

  render(mode) {
    const hero = document.getElementById('heroArticle');
    if (!hero) return;
    const article = this.article;
    const imageUrl = article.image_url;
    const tagClass = DraftGuru.getTagClass(article.tag);
    const esc = DraftGuru.escapeHtml.bind(DraftGuru);
    const safeImageUrl = esc(imageUrl || '');
    const safeTitle = esc(article.title);

    hero.className = 'news-hero';
    hero.onclick = () => window.open(article.url, '_blank');

    if (mode === 'full' && imageUrl) {
      hero.innerHTML = `
        <img class="news-hero__image" src="${safeImageUrl}" alt="${safeTitle}" />
        ${this.renderOverlay(article, tagClass)}
      `;
    } else if (mode === 'split' && imageUrl) {
      hero.classList.add('news-hero--split');
      hero.innerHTML = `
        <div class="news-hero__text-area">
          <span class="news-hero__tag tag--${tagClass}">${esc(article.tag)}</span>
          <h2 class="news-hero__title">${safeTitle}</h2>
          <p class="news-hero__summary">${esc(article.summary || '')}</p>
          <div class="news-hero__meta">
            <span class="news-hero__source">${esc(article.source)}</span>
            ${article.author ? `<span class="news-hero__author">by ${esc(article.author)}</span>` : ''}
            <span class="news-hero__time">${esc(article.time)}</span>
          </div>
        </div>
        <div class="news-hero__image-area">
          <img class="news-hero__image" src="${safeImageUrl}" alt="${safeTitle}" />
        </div>
      `;
    } else if (mode === 'blurred' && imageUrl) {
      hero.classList.add('news-hero--blurred');
      hero.innerHTML = `
        <div class="news-hero__background" style="background-image: url('${safeImageUrl}');"></div>
        <div class="news-hero__image-container">
          <img class="news-hero__image" src="${safeImageUrl}" alt="${safeTitle}" />
        </div>
        ${this.renderOverlay(article, tagClass)}
      `;
    } else {
      hero.classList.add('news-hero--gradient', `news-hero--${tagClass}`);
      const iconPath = this.categoryIcons[article.tag] || this.categoryIcons['Scouting Report'];
      hero.innerHTML = `
        <div class="news-hero__pattern"></div>
        <svg class="news-hero__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
          ${iconPath}
        </svg>
        <div class="news-hero__spacer"></div>
        ${this.renderOverlay(article, tagClass)}
      `;
    }
  },

  renderOverlay(article, tagClass) {
    const esc = DraftGuru.escapeHtml.bind(DraftGuru);
    return `
      <div class="news-hero__overlay">
        <span class="news-hero__tag tag--${tagClass}">${esc(article.tag)}</span>
        <h2 class="news-hero__title">${esc(article.title)}</h2>
        <p class="news-hero__summary">${esc(article.summary || '')}</p>
        <div class="news-hero__meta">
          <span class="news-hero__source">${esc(article.source)}</span>
          ${article.author ? `<span class="news-hero__author">by ${esc(article.author)}</span>` : ''}
          <span class="news-hero__time">${esc(article.time)}</span>
        </div>
      </div>
    `;
  }
};

/**
 * ============================================================================
 * ARTICLE GRID MODULE — Renders article cards from server data
 * ============================================================================
 */
const NewsArticleGridModule = {
  init(items) {
    const grid = document.getElementById('articlesGrid');
    if (!grid) return;

    if (!items || items.length === 0) {
      grid.innerHTML = '<div class="articles-grid--empty">No articles match your filters</div>';
      return;
    }

    grid.innerHTML = items.map(item => this.renderCard(item)).join('');
  },

  renderCard(item) {
    const tagClass = DraftGuru.getTagClass(item.tag);
    const hasImage = item.image_url && item.image_url.trim() !== '';
    const esc = DraftGuru.escapeHtml.bind(DraftGuru);

    return `
      <article class="article-card" onclick="window.open('${esc(item.url)}', '_blank')">
        <div class="article-card__image-wrapper">
          ${hasImage
            ? `<img src="${esc(item.image_url)}" class="article-card__image" alt="" loading="lazy" />`
            : `<div class="article-card__image-placeholder">DG</div>`
          }
          <span class="article-card__tag tag--${tagClass}">${esc(item.tag)}</span>
        </div>
        <div class="article-card__content">
          <h3 class="article-card__title">${esc(item.title)}</h3>
          ${item.summary ? `<p class="article-card__summary">${esc(item.summary)}</p>` : ''}
          <div class="article-card__meta">
            <span class="article-card__source">${esc(item.source)}</span>
            <span class="article-card__time">${esc(item.time)}</span>
          </div>
        </div>
      </article>
    `;
  }
};

/**
 * ============================================================================
 * TRENDING MODULE — Renders trending player mentions in the sidebar
 * ============================================================================
 */
const NewsTrendingModule = {
  init(trending) {
    const container = document.getElementById('trendingMentions');
    if (!container) return;

    if (!trending || trending.length === 0) {
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
 * SIDEBAR TOGGLE — Show more / less for collapsible sidebar lists
 * ============================================================================
 */
function initSidebarToggles() {
  const toggles = [
    { btnId: 'authorShowMore', overflowId: 'authorOverflow' },
  ];

  toggles.forEach(({ btnId, overflowId }) => {
    const btn = document.getElementById(btnId);
    const overflow = document.getElementById(overflowId);
    if (!btn || !overflow) return;

    const originalText = btn.textContent;
    btn.addEventListener('click', () => {
      const hidden = overflow.style.display === 'none';
      overflow.style.display = hidden ? '' : 'none';
      btn.textContent = hidden ? 'Show less' : originalText;
    });
  });
}

/**
 * ============================================================================
 * INITIALIZATION
 * ============================================================================
 */
document.addEventListener('DOMContentLoaded', () => {
  const items = window.NEWS_ITEMS || [];
  const trending = window.NEWS_TRENDING || [];

  NewsHeroModule.init();
  NewsArticleGridModule.init(items);
  NewsTrendingModule.init(trending);
  initSidebarToggles();
});
