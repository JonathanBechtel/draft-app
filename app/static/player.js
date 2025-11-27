/**
 * ============================================================================
 * Player Detail Page JavaScript
 * Performance percentiles, comparisons, and head-to-head functionality
 * ============================================================================
 */

document.addEventListener('DOMContentLoaded', () => {
  // Initialize modules if their containers exist
  if (document.getElementById('perfBarsContainer')) {
    PerformanceModule.init();
  }
  if (document.getElementById('compResultsGrid')) {
    ComparisonsModule.init();
  }
  if (document.getElementById('playerFeedContainer')) {
    PlayerNewsModule.init();
  }
});

/**
 * ============================================================================
 * PERFORMANCE MODULE
 * Handles percentile bar display and category switching
 * ============================================================================
 */
const PerformanceModule = {
  currentCategory: 'anthropometrics',
  playerId: null,

  init() {
    // Get player ID from data attribute
    const container = document.getElementById('perfBarsContainer');
    if (container) {
      this.playerId = container.dataset.playerId;
    }

    this.setupEventListeners();
    this.renderBars();
  },

  setupEventListeners() {
    // Category tabs
    const tabs = document.querySelectorAll('.perf-tab');
    tabs.forEach(tab => {
      tab.addEventListener('click', () => {
        tabs.forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        this.currentCategory = tab.dataset.category;
        this.renderBars();
      });
    });

    // Cohort selector
    const cohortSelect = document.getElementById('perfCohort');
    if (cohortSelect) {
      cohortSelect.addEventListener('change', () => this.renderBars());
    }

    // Position filter
    const positionFilter = document.getElementById('perfPositionFilter');
    if (positionFilter) {
      positionFilter.addEventListener('change', () => this.renderBars());
    }
  },

  async renderBars() {
    const container = document.getElementById('perfBarsContainer');
    if (!container || !this.playerId) return;

    // Show loading state
    container.innerHTML = `
      <div class="perf-bar-row">
        <div class="skeleton" style="height: 1rem; width: 100px;"></div>
        <div class="perf-bar-track">
          <div class="skeleton" style="height: 100%; width: 60%;"></div>
        </div>
        <div class="skeleton" style="height: 1rem; width: 80px;"></div>
      </div>
    `;

    try {
      // Map category to API category value
      const categoryMap = {
        'anthropometrics': 'anthropometrics',
        'combinePerformance': 'combine_performance',
        'advancedStats': 'advanced_stats'
      };
      const apiCategory = categoryMap[this.currentCategory] || this.currentCategory;

      // Fetch metrics from API
      const response = await fetch(`/api/players/${this.playerId}/metrics?category=${apiCategory}`);

      if (!response.ok) {
        this.renderPlaceholderBars(container);
        return;
      }

      const data = await response.json();

      if (!data.metrics || data.metrics.length === 0) {
        this.renderPlaceholderBars(container);
        return;
      }

      // Render bars
      let html = '';
      data.metrics.forEach(metric => {
        const percentile = metric.percentile || 50;
        const tierClass = this.getPercentileTierClass(percentile);
        const displayValue = this.formatMetricValue(metric.value, metric.unit);

        html += `
          <div class="perf-bar-row">
            <div class="perf-metric-label">${metric.display_name}</div>
            <div class="perf-bar-track">
              <div class="perf-bar-fill ${tierClass}" style="width: ${percentile}%;"></div>
            </div>
            <div class="perf-values">
              <span class="perf-actual-value">${displayValue}</span>
              <span class="perf-percentile-value ${tierClass}">${percentile}th</span>
            </div>
          </div>
        `;
      });

      container.innerHTML = html;

    } catch (error) {
      console.log('API not available, using placeholder data:', error);
      this.renderPlaceholderBars(container);
    }
  },

  renderPlaceholderBars(container) {
    // Placeholder data based on category
    const placeholders = {
      anthropometrics: [
        { metric: "Height", value: '6\'9"', percentile: 92 },
        { metric: "Weight", value: "205 lbs", percentile: 78 },
        { metric: "Wingspan", value: '7\'2"', percentile: 95 },
        { metric: "Standing Reach", value: '9\'2"', percentile: 94 },
        { metric: "Hand Length", value: '9.5"', percentile: 88 },
        { metric: "Hand Width", value: '10.25"', percentile: 90 },
      ],
      combinePerformance: [
        { metric: "Lane Agility", value: "10.84s", percentile: 89 },
        { metric: "3/4 Sprint", value: "3.15s", percentile: 91 },
        { metric: "Max Vertical", value: '36.0"', percentile: 87 },
        { metric: "Standing Vertical", value: '30.5"', percentile: 83 },
        { metric: "Bench Press", value: "12 reps", percentile: 68 },
      ],
      advancedStats: [
        { metric: "PER", value: "28.6", percentile: 97 },
        { metric: "True Shooting %", value: "61.2%", percentile: 94 },
        { metric: "Usage Rate", value: "28.4%", percentile: 91 },
        { metric: "Win Shares", value: "6.8", percentile: 95 },
        { metric: "Box Plus/Minus", value: "+8.9", percentile: 96 },
      ]
    };

    const metrics = placeholders[this.currentCategory] || placeholders.anthropometrics;

    let html = '';
    metrics.forEach(metric => {
      const tierClass = this.getPercentileTierClass(metric.percentile);

      html += `
        <div class="perf-bar-row">
          <div class="perf-metric-label">${metric.metric}</div>
          <div class="perf-bar-track">
            <div class="perf-bar-fill ${tierClass}" style="width: ${metric.percentile}%;"></div>
          </div>
          <div class="perf-values">
            <span class="perf-actual-value">${metric.value}</span>
            <span class="perf-percentile-value ${tierClass}">${metric.percentile}th</span>
          </div>
        </div>
      `;
    });

    container.innerHTML = html;
  },

  getPercentileTierClass(percentile) {
    if (percentile >= 90) return 'elite';
    if (percentile >= 75) return 'good';
    if (percentile >= 50) return 'average';
    return 'below-average';
  },

  formatMetricValue(value, unit) {
    if (value === null || value === undefined) return 'N/A';

    if (unit === 'inches') {
      return `${value.toFixed(1)}"`;
    } else if (unit === 'pounds') {
      return `${value.toFixed(0)} lbs`;
    } else if (unit === 'seconds') {
      return `${value.toFixed(2)}s`;
    } else if (unit === 'percent') {
      return `${value.toFixed(1)}%`;
    } else if (unit === 'reps') {
      return `${value.toFixed(0)} reps`;
    }

    return typeof value === 'number' ? value.toFixed(1) : value;
  }
};

/**
 * ============================================================================
 * COMPARISONS MODULE
 * Handles similar player cards and comparison functionality
 * ============================================================================
 */
const ComparisonsModule = {
  currentCategory: 'anthropometrics',
  playerId: null,

  init() {
    const container = document.getElementById('compResultsGrid');
    if (container) {
      this.playerId = container.dataset.playerId;
    }

    this.setupEventListeners();
    this.renderComparisons();
  },

  setupEventListeners() {
    // Category tabs
    const tabs = document.querySelectorAll('.comp-tab');
    tabs.forEach(tab => {
      tab.addEventListener('click', () => {
        tabs.forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        this.currentCategory = tab.dataset.category;
        this.renderComparisons();
      });
    });

    // Pool selector
    const poolSelect = document.getElementById('compPool');
    if (poolSelect) {
      poolSelect.addEventListener('change', () => this.renderComparisons());
    }

    // Position filter
    const positionFilter = document.getElementById('compPositionFilter');
    if (positionFilter) {
      positionFilter.addEventListener('change', () => this.renderComparisons());
    }
  },

  async renderComparisons() {
    const container = document.getElementById('compResultsGrid');
    if (!container || !this.playerId) return;

    // Show loading state
    container.innerHTML = Array(4).fill(`
      <div class="prospect-card">
        <div class="skeleton" style="height: 192px;"></div>
        <div class="prospect-info">
          <div class="skeleton" style="height: 1rem; width: 80%; margin-bottom: 0.5rem;"></div>
          <div class="skeleton" style="height: 0.75rem; width: 60%;"></div>
        </div>
      </div>
    `).join('');

    try {
      // Map category to API dimension value
      const dimensionMap = {
        'anthropometrics': 'anthro',
        'combinePerformance': 'combine',
        'advancedStats': 'composite'
      };
      const dimension = dimensionMap[this.currentCategory] || 'composite';

      // Fetch similar players from API
      const response = await fetch(`/api/players/${this.playerId}/similar?dimension=${dimension}&limit=8`);

      if (!response.ok) {
        this.renderPlaceholderComparisons(container);
        return;
      }

      const data = await response.json();

      if (!data.similar_players || data.similar_players.length === 0) {
        this.renderPlaceholderComparisons(container);
        return;
      }

      // Render comparison cards
      let html = '';
      data.similar_players.forEach(player => {
        const similarityScore = Math.round(player.score * 100);
        const badgeClass = this.getSimilarityBadgeClass(similarityScore);
        const imgUrl = player.image_url ||
          `https://placehold.co/320x192/edf2f7/1f2937?text=${encodeURIComponent(player.display_name)}`;

        html += `
          <div class="prospect-card">
            <span class="similarity-badge ${badgeClass}">${similarityScore}%</span>
            <div class="prospect-image-wrapper">
              <img class="prospect-image" src="${imgUrl}" alt="${player.display_name}" />
            </div>
            <div class="prospect-info">
              <div class="prospect-name">${player.display_name}</div>
              <div class="prospect-meta">${player.position || ''} ${player.school ? '• ' + player.school : ''}</div>
              <button class="comp-compare-btn" data-player-id="${player.id}" onclick="ComparisonsModule.openComparison(${player.id}, '${player.display_name}')">
                <svg class="icon" viewBox="0 0 24 24" style="width: 1rem; height: 1rem;">
                  <path d="M16 3h5v5M4 20L21 3M21 16v5h-5M15 15l6 6M4 4l5 5"></path>
                </svg>
                Compare
              </button>
            </div>
          </div>
        `;
      });

      container.innerHTML = html;

    } catch (error) {
      console.log('API not available, using placeholder data:', error);
      this.renderPlaceholderComparisons(container);
    }
  },

  renderPlaceholderComparisons(container) {
    const placeholders = [
      { name: "Chet Holmgren", position: "F/C", school: "Gonzaga (2022)", similarity: 92 },
      { name: "Jaren Jackson Jr.", position: "F", school: "Michigan State (2018)", similarity: 87 },
      { name: "Evan Mobley", position: "F/C", school: "USC (2021)", similarity: 85 },
      { name: "Paolo Banchero", position: "F", school: "Duke (2022)", similarity: 78 },
      { name: "Franz Wagner", position: "F", school: "Michigan (2021)", similarity: 74 },
      { name: "Jonathan Kuminga", position: "F", school: "G League (2021)", similarity: 68 },
      { name: "Jabari Smith Jr.", position: "F", school: "Auburn (2022)", similarity: 72 },
      { name: "Scottie Barnes", position: "F", school: "Florida State (2021)", similarity: 70 },
    ];

    let html = '';
    placeholders.forEach(player => {
      const badgeClass = this.getSimilarityBadgeClass(player.similarity);
      const imgUrl = `https://placehold.co/320x192/edf2f7/1f2937?text=${encodeURIComponent(player.name)}`;

      html += `
        <div class="prospect-card">
          <span class="similarity-badge ${badgeClass}">${player.similarity}%</span>
          <div class="prospect-image-wrapper">
            <img class="prospect-image" src="${imgUrl}" alt="${player.name}" />
          </div>
          <div class="prospect-info">
            <div class="prospect-name">${player.name}</div>
            <div class="prospect-meta">${player.position} • ${player.school}</div>
            <button class="comp-compare-btn">
              <svg class="icon" viewBox="0 0 24 24" style="width: 1rem; height: 1rem;">
                <path d="M16 3h5v5M4 20L21 3M21 16v5h-5M15 15l6 6M4 4l5 5"></path>
              </svg>
              Compare
            </button>
          </div>
        </div>
      `;
    });

    container.innerHTML = html;
  },

  getSimilarityBadgeClass(score) {
    if (score >= 85) return 'similarity-badge--high';
    if (score >= 70) return 'similarity-badge--good';
    if (score >= 55) return 'similarity-badge--moderate';
    return 'similarity-badge--weak';
  },

  openComparison(playerId, playerName) {
    // Navigate to compare page with both players
    const currentPlayerId = this.playerId;
    window.location.href = `/compare?player_a=${currentPlayerId}&player_b=${playerId}`;
  }
};

/**
 * ============================================================================
 * PLAYER NEWS MODULE
 * Handles player-specific news feed
 * ============================================================================
 */
const PlayerNewsModule = {
  playerId: null,

  init() {
    const container = document.getElementById('playerFeedContainer');
    if (container) {
      this.playerId = container.dataset.playerId;
    }

    this.renderNews();
  },

  async renderNews() {
    const container = document.getElementById('playerFeedContainer');
    if (!container) return;

    // Show loading state
    container.innerHTML = Array(3).fill(`
      <div class="feed-item">
        <div class="skeleton" style="height: 1rem; width: 90%; margin-bottom: 0.5rem;"></div>
        <div class="skeleton" style="height: 0.75rem; width: 60%;"></div>
      </div>
    `).join('');

    // Use placeholder news data for now
    // In production, this would fetch from /api/news?player_id={playerId}
    this.renderPlaceholderNews(container);
  },

  renderPlaceholderNews(container) {
    const playerName = document.querySelector('.section-header')?.textContent?.split('—')[0]?.trim() || 'Player';

    const newsItems = [
      {
        title: `${playerName} Dominates in Recent Showcase Performance`,
        source: "Draft Insider",
        time_ago: "2h",
        tag: "riser"
      },
      {
        title: `Scout's Take: ${playerName}'s Path to the Top Pick`,
        source: "Hoops Report",
        time_ago: "5h",
        tag: "analysis"
      },
      {
        title: `Workout Report: ${playerName} Impresses NBA Teams`,
        source: "Draft Central",
        time_ago: "1d",
        tag: "highlight"
      },
      {
        title: `${playerName}'s College Season Breakdown`,
        source: "Sports Wire",
        time_ago: "2d",
        tag: "analysis"
      },
      {
        title: `Mock Draft Update: ${playerName} Stock Rising`,
        source: "Draft Experts",
        time_ago: "3d",
        tag: "riser"
      }
    ];

    let html = '';
    newsItems.forEach(item => {
      html += `
        <a href="#" class="feed-item player-feed-item">
          <div class="feed-title">${item.title}</div>
          <div class="feed-meta">
            <span>${item.source}</span>
            <span>•</span>
            <span>${item.time_ago}</span>
            <span class="feed-tag ${item.tag}">${item.tag}</span>
          </div>
        </a>
      `;
    });

    container.innerHTML = html;
  }
};

/**
 * ============================================================================
 * HEAD-TO-HEAD MODULE
 * Handles direct player comparison
 * ============================================================================
 */
const HeadToHeadModule = {
  playerAId: null,
  playerBId: null,
  currentCategory: 'anthropometrics',

  init(playerAId) {
    this.playerAId = playerAId;
    this.setupEventListeners();
    this.populatePlayerSelect();
  },

  setupEventListeners() {
    // Category tabs
    const tabs = document.querySelectorAll('.h2h-tab');
    tabs.forEach(tab => {
      tab.addEventListener('click', () => {
        tabs.forEach(t => t.classList.remove('h2h-tab--active'));
        tab.classList.add('h2h-tab--active');
        this.currentCategory = tab.dataset.category;
        this.updateComparison();
      });
    });

    // Player selector
    const playerSelect = document.getElementById('h2hPlayerSelect');
    if (playerSelect) {
      playerSelect.addEventListener('change', (e) => {
        this.playerBId = e.target.value;
        this.updateComparison();
      });
    }

    // Swap button
    const swapBtn = document.getElementById('h2hSwapBtn');
    if (swapBtn) {
      swapBtn.addEventListener('click', () => this.swapPlayers());
    }
  },

  async populatePlayerSelect() {
    const select = document.getElementById('h2hPlayerSelect');
    if (!select) return;

    try {
      // Fetch similar players for comparison options
      const response = await fetch(`/api/players/${this.playerAId}/similar?limit=10`);
      if (response.ok) {
        const data = await response.json();
        let options = '<option value="">Select a player...</option>';
        data.similar_players.forEach(player => {
          options += `<option value="${player.id}">${player.display_name} (${player.position || 'N/A'})</option>`;
        });
        select.innerHTML = options;
      }
    } catch (error) {
      console.log('Could not load comparison players:', error);
    }
  },

  async updateComparison() {
    if (!this.playerAId || !this.playerBId) return;

    const tableBody = document.getElementById('h2hTableBody');
    const similarityBadge = document.getElementById('h2hSimilarityBadge');

    if (!tableBody) return;

    // Show loading
    tableBody.innerHTML = `
      <tr>
        <td colspan="3" class="text-center" style="padding: 2rem;">
          <div class="skeleton" style="height: 1rem; width: 60%; margin: 0 auto;"></div>
        </td>
      </tr>
    `;

    try {
      const categoryMap = {
        'anthropometrics': 'anthropometrics',
        'combinePerformance': 'combine_performance',
        'advancedStats': 'advanced_stats'
      };
      const category = categoryMap[this.currentCategory] || this.currentCategory;

      const response = await fetch(
        `/api/players/compare?player_a=${this.playerAId}&player_b=${this.playerBId}&category=${category}`
      );

      if (response.ok) {
        const data = await response.json();
        this.renderComparisonTable(data);
      } else {
        this.renderPlaceholderComparison();
      }
    } catch (error) {
      console.log('Comparison API not available:', error);
      this.renderPlaceholderComparison();
    }
  },

  renderComparisonTable(data) {
    const tableBody = document.getElementById('h2hTableBody');
    const similarityBadge = document.getElementById('h2hSimilarityBadge');
    const winnerDeclaration = document.getElementById('h2hWinnerDeclaration');

    if (similarityBadge && data.similarity_score !== undefined) {
      similarityBadge.textContent = `${Math.round(data.similarity_score)}% Match`;
    }

    if (data.comparisons && data.comparisons.length > 0) {
      let html = '';
      data.comparisons.forEach(metric => {
        const valueA = this.formatValue(metric.value_a, metric.unit);
        const valueB = this.formatValue(metric.value_b, metric.unit);
        const classA = metric.winner === 'a' ? 'h2h-value h2h-value--winner' : 'h2h-value h2h-value--loser';
        const classB = metric.winner === 'b' ? 'h2h-value h2h-value--winner' : 'h2h-value h2h-value--loser';

        html += `
          <tr>
            <td class="text-right"><span class="${classA}">${valueA}</span></td>
            <td class="text-center mono-font uppercase" style="font-size: 0.75rem; color: var(--color-slate-600);">${metric.display_name}</td>
            <td class="text-left"><span class="${classB}">${valueB}</span></td>
          </tr>
        `;
      });
      tableBody.innerHTML = html;
    }

    if (winnerDeclaration && data.wins_a !== undefined && data.wins_b !== undefined) {
      if (data.wins_a > data.wins_b) {
        winnerDeclaration.innerHTML = `<div class="h2h-winner-banner h2h-winner-banner--a">Player A wins ${data.wins_a} - ${data.wins_b}</div>`;
      } else if (data.wins_b > data.wins_a) {
        winnerDeclaration.innerHTML = `<div class="h2h-winner-banner h2h-winner-banner--b">Player B wins ${data.wins_b} - ${data.wins_a}</div>`;
      } else {
        winnerDeclaration.innerHTML = `<div class="h2h-winner-banner h2h-winner-banner--tie">It's a tie! ${data.wins_a} - ${data.wins_b}</div>`;
      }
    }
  },

  renderPlaceholderComparison() {
    const tableBody = document.getElementById('h2hTableBody');
    if (!tableBody) return;

    const metrics = [
      { name: 'Height', a: '6\'9"', b: '6\'8"', winner: 'a' },
      { name: 'Wingspan', a: '7\'2"', b: '7\'0"', winner: 'a' },
      { name: 'Weight', a: '205 lbs', b: '210 lbs', winner: 'b' },
      { name: 'Standing Reach', a: '9\'2"', b: '9\'0"', winner: 'a' },
    ];

    let html = '';
    metrics.forEach(metric => {
      const classA = metric.winner === 'a' ? 'h2h-value h2h-value--winner' : 'h2h-value h2h-value--loser';
      const classB = metric.winner === 'b' ? 'h2h-value h2h-value--winner' : 'h2h-value h2h-value--loser';

      html += `
        <tr>
          <td class="text-right"><span class="${classA}">${metric.a}</span></td>
          <td class="text-center mono-font uppercase" style="font-size: 0.75rem; color: var(--color-slate-600);">${metric.name}</td>
          <td class="text-left"><span class="${classB}">${metric.b}</span></td>
        </tr>
      `;
    });
    tableBody.innerHTML = html;
  },

  formatValue(value, unit) {
    if (value === null || value === undefined) return 'N/A';
    if (unit === 'inches') return `${value.toFixed(1)}"`;
    if (unit === 'pounds') return `${value.toFixed(0)} lbs`;
    if (unit === 'seconds') return `${value.toFixed(2)}s`;
    if (unit === 'percent') return `${value.toFixed(1)}%`;
    return typeof value === 'number' ? value.toFixed(1) : value;
  },

  swapPlayers() {
    const temp = this.playerAId;
    this.playerAId = this.playerBId;
    this.playerBId = temp;
    this.updateComparison();
  }
};
