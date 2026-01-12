/**
 * Tweet Share - Generates a share card and opens a tweet intent
 */
const TweetShare = {
  /**
   * Generate a tweet for a component
   * @param {string} component - Component type (vs_arena, performance, h2h, comps)
   * @param {number[]} playerIds - Array of player IDs
   * @param {Object} context - Export context options
   */
  async share(component, playerIds, context = {}) {
    try {
      const response = await fetch('/api/export/image', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          component,
          player_ids: playerIds,
          context: {
            comparison_group: context.comparisonGroup || 'current_draft',
            same_position: context.samePosition || false,
            metric_group: context.metricGroup || 'anthropometrics',
          },
        }),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || `HTTP ${response.status}`);
      }

      const data = await response.json();
      const text = this.buildTweetText(data.title);
      const url = data.url;
      this.openTweetIntent(text, url);
    } catch (err) {
      console.error('Tweet share failed:', err);
      alert('Failed to generate tweet: ' + (err.message || 'Unknown error'));
    }
  },

  /**
   * Build tweet text from export title
   * @param {string} title - Share card title
   */
  buildTweetText(title) {
    const pageUrl = window.location.href;
    if (title) {
      return `${title} — DraftGuru\n${pageUrl}`;
    }
    return `DraftGuru\n${pageUrl}`;
  },

  /**
   * Open the Twitter intent window
   * @param {string} text - Tweet text
   * @param {string} url - URL to include in the tweet
   */
  openTweetIntent(text, url) {
    const intentUrl = new URL('https://twitter.com/intent/tweet');
    if (text) intentUrl.searchParams.set('text', text);
    if (url) intentUrl.searchParams.set('url', url);

    const opened = window.open(intentUrl.toString(), '_blank', 'noopener,noreferrer');
    if (!opened) {
      window.location.href = intentUrl.toString();
    }
  }
};

// Export for global access
window.TweetShare = TweetShare;
