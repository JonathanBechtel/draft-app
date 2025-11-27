/**
 * ============================================================================
 * Home Page JavaScript
 * VS Arena comparison tool and homepage-specific interactions
 * ============================================================================
 */

document.addEventListener('DOMContentLoaded', () => {
  initVSArena();
  initCategoryTabs();
});

/**
 * Initialize VS Arena comparison functionality
 */
function initVSArena() {
  const playerASelect = document.getElementById('playerA');
  const playerBSelect = document.getElementById('playerB');

  if (!playerASelect || !playerBSelect) return;

  // Handle player selection changes
  playerASelect.addEventListener('change', updateComparison);
  playerBSelect.addEventListener('change', updateComparison);
}

/**
 * Initialize category tab switching
 */
function initCategoryTabs() {
  const tabs = document.querySelectorAll('.h2h-tab');

  tabs.forEach(tab => {
    tab.addEventListener('click', () => {
      // Update active state
      tabs.forEach(t => t.classList.remove('h2h-tab--active'));
      tab.classList.add('h2h-tab--active');

      // Refresh comparison with new category
      updateComparison();
    });
  });
}

/**
 * Update the comparison display based on selected players
 */
async function updateComparison() {
  const playerASelect = document.getElementById('playerA');
  const playerBSelect = document.getElementById('playerB');
  const activeTab = document.querySelector('.h2h-tab--active');

  const playerAId = playerASelect?.value;
  const playerBId = playerBSelect?.value;
  const category = activeTab?.dataset.category || 'anthropometrics';

  // Update player names in headers
  updatePlayerHeaders(playerASelect, playerBSelect);

  // If both players selected, fetch comparison data
  if (playerAId && playerBId) {
    await fetchAndDisplayComparison(playerAId, playerBId, category);
  } else {
    showPlaceholderComparison();
  }
}

/**
 * Update player name headers
 */
function updatePlayerHeaders(selectA, selectB) {
  const headerA = document.getElementById('headerA');
  const headerB = document.getElementById('headerB');
  const nameA = document.getElementById('h2hPhotoNameA');
  const nameB = document.getElementById('h2hPhotoNameB');
  const photoA = document.getElementById('h2hPhotoA');
  const photoB = document.getElementById('h2hPhotoB');

  const optionA = selectA?.selectedOptions[0];
  const optionB = selectB?.selectedOptions[0];

  const playerAName = optionA?.value ? optionA.text.split(' (')[0] : 'Player A';
  const playerBName = optionB?.value ? optionB.text.split(' (')[0] : 'Player B';

  if (headerA) headerA.textContent = playerAName;
  if (headerB) headerB.textContent = playerBName;
  if (nameA) nameA.textContent = playerAName;
  if (nameB) nameB.textContent = playerBName;

  // Update placeholder images with player names
  if (photoA) {
    photoA.src = `https://placehold.co/150x150/edf2f7/1f2937?text=${encodeURIComponent(playerAName)}`;
    photoA.alt = playerAName;
  }
  if (photoB) {
    photoB.src = `https://placehold.co/150x150/edf2f7/1f2937?text=${encodeURIComponent(playerBName)}`;
    photoB.alt = playerBName;
  }
}

/**
 * Fetch comparison data from API and display
 */
async function fetchAndDisplayComparison(playerAId, playerBId, category) {
  const comparisonBody = document.getElementById('comparisonBody');
  const similarityBadge = document.getElementById('h2hSimilarityBadge');
  const winnerDeclaration = document.getElementById('winnerDeclaration');

  // Map category tabs to API category values
  const categoryMap = {
    'anthropometrics': 'anthropometrics',
    'combine': 'combine_performance',
    'advanced': 'advanced_stats'
  };
  const apiCategory = categoryMap[category] || category;

  // Show loading state
  if (comparisonBody) {
    comparisonBody.innerHTML = `
      <tr>
        <td colspan="3" class="text-center" style="padding: 2rem;">
          <div class="skeleton" style="height: 1rem; width: 60%; margin: 0 auto;"></div>
        </td>
      </tr>
    `;
  }

  try {
    // Fetch comparison data from API
    const url = `/api/players/compare?player_a=${playerAId}&player_b=${playerBId}` +
                (apiCategory ? `&category=${apiCategory}` : '');
    const response = await fetch(url);

    if (!response.ok) {
      // If API returns error or no data, use placeholder data
      displayPlaceholderComparisonData(playerAId, playerBId, category);
      return;
    }

    const data = await response.json();

    // If no comparison data available, use placeholder
    if (!data.comparisons || data.comparisons.length === 0) {
      displayPlaceholderComparisonData(playerAId, playerBId, category);
      return;
    }

    displayComparisonData(data);
  } catch (error) {
    console.log('API not available, using placeholder data:', error);
    displayPlaceholderComparisonData(playerAId, playerBId, category);
  }
}

/**
 * Display comparison data in the table
 */
function displayComparisonData(data) {
  const comparisonBody = document.getElementById('comparisonBody');
  const similarityBadge = document.getElementById('h2hSimilarityBadge');
  const winnerDeclaration = document.getElementById('winnerDeclaration');

  if (!comparisonBody) return;

  // Update similarity badge
  if (similarityBadge && data.similarity_score !== undefined) {
    similarityBadge.textContent = `${Math.round(data.similarity_score)}% Similar`;
  }

  // Build comparison rows
  if (data.comparisons && data.comparisons.length > 0) {
    let html = '';
    data.comparisons.forEach(metric => {
      const valueA = formatMetricValue(metric.value_a, metric.unit);
      const valueB = formatMetricValue(metric.value_b, metric.unit);

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
    comparisonBody.innerHTML = html;
  }

  // Show winner declaration
  if (winnerDeclaration) {
    if (data.wins_a > data.wins_b) {
      winnerDeclaration.innerHTML = `<div class="h2h-winner-banner h2h-winner-banner--a">Player A wins ${data.wins_a} - ${data.wins_b}</div>`;
    } else if (data.wins_b > data.wins_a) {
      winnerDeclaration.innerHTML = `<div class="h2h-winner-banner h2h-winner-banner--b">Player B wins ${data.wins_b} - ${data.wins_a}</div>`;
    } else {
      winnerDeclaration.innerHTML = `<div class="h2h-winner-banner h2h-winner-banner--tie">It's a tie! ${data.wins_a} - ${data.wins_b}</div>`;
    }
  }
}

/**
 * Display placeholder comparison data when API is not available
 */
function displayPlaceholderComparisonData(playerAId, playerBId, category) {
  const comparisonBody = document.getElementById('comparisonBody');
  const similarityBadge = document.getElementById('h2hSimilarityBadge');
  const winnerDeclaration = document.getElementById('winnerDeclaration');

  // Placeholder metrics based on category
  let metrics = [];

  if (category === 'anthropometrics') {
    metrics = [
      { name: 'Height (no shoes)', a: '6\'8"', b: '6\'7"', winner: 'a' },
      { name: 'Wingspan', a: '7\'2"', b: '7\'0"', winner: 'a' },
      { name: 'Standing Reach', a: '9\'0"', b: '8\'10"', winner: 'a' },
      { name: 'Weight', a: '215 lbs', b: '220 lbs', winner: 'b' },
      { name: 'Hand Length', a: '9.5"', b: '9.0"', winner: 'a' },
      { name: 'Hand Width', a: '10.0"', b: '10.5"', winner: 'b' },
    ];
  } else if (category === 'combine') {
    metrics = [
      { name: 'Lane Agility', a: '10.8s', b: '11.1s', winner: 'a' },
      { name: '3/4 Sprint', a: '3.15s', b: '3.22s', winner: 'a' },
      { name: 'Standing Vertical', a: '32"', b: '30"', winner: 'a' },
      { name: 'Max Vertical', a: '38"', b: '37"', winner: 'a' },
      { name: 'Bench Press', a: '12 reps', b: '15 reps', winner: 'b' },
    ];
  } else {
    metrics = [
      { name: 'PER', a: '24.5', b: '22.1', winner: 'a' },
      { name: 'TS%', a: '62.1%', b: '58.5%', winner: 'a' },
      { name: 'USG%', a: '28.4%', b: '31.2%', winner: 'b' },
      { name: 'BPM', a: '+6.2', b: '+5.8', winner: 'a' },
      { name: 'Win Shares', a: '4.2', b: '3.9', winner: 'a' },
    ];
  }

  // Update similarity badge
  if (similarityBadge) {
    similarityBadge.textContent = '75% Similar';
  }

  // Build comparison rows
  if (comparisonBody) {
    let html = '';
    let winsA = 0;
    let winsB = 0;

    metrics.forEach(metric => {
      const classA = metric.winner === 'a' ? 'h2h-value h2h-value--winner' : 'h2h-value h2h-value--loser';
      const classB = metric.winner === 'b' ? 'h2h-value h2h-value--winner' : 'h2h-value h2h-value--loser';

      if (metric.winner === 'a') winsA++;
      else if (metric.winner === 'b') winsB++;

      html += `
        <tr>
          <td class="text-right"><span class="${classA}">${metric.a}</span></td>
          <td class="text-center mono-font uppercase" style="font-size: 0.75rem; color: var(--color-slate-600);">${metric.name}</td>
          <td class="text-left"><span class="${classB}">${metric.b}</span></td>
        </tr>
      `;
    });
    comparisonBody.innerHTML = html;

    // Show winner declaration
    if (winnerDeclaration) {
      if (winsA > winsB) {
        winnerDeclaration.innerHTML = `<div class="h2h-winner-banner h2h-winner-banner--a">Player A wins ${winsA} - ${winsB}</div>`;
      } else if (winsB > winsA) {
        winnerDeclaration.innerHTML = `<div class="h2h-winner-banner h2h-winner-banner--b">Player B wins ${winsB} - ${winsA}</div>`;
      } else {
        winnerDeclaration.innerHTML = `<div class="h2h-winner-banner h2h-winner-banner--tie">It's a tie! ${winsA} - ${winsB}</div>`;
      }
    }
  }
}

/**
 * Show placeholder when no players selected
 */
function showPlaceholderComparison() {
  const comparisonBody = document.getElementById('comparisonBody');
  const similarityBadge = document.getElementById('h2hSimilarityBadge');
  const winnerDeclaration = document.getElementById('winnerDeclaration');

  if (similarityBadge) {
    similarityBadge.textContent = 'Select Players';
  }

  if (comparisonBody) {
    comparisonBody.innerHTML = `
      <tr>
        <td colspan="3" class="text-center" style="padding: 2rem; color: var(--color-slate-500);">
          Select two players to compare
        </td>
      </tr>
    `;
  }

  if (winnerDeclaration) {
    winnerDeclaration.innerHTML = '';
  }
}

/**
 * Format metric value with unit
 */
function formatMetricValue(value, unit) {
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
