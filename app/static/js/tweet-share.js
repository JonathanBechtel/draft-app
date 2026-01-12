/**
 * Tweet Share - open an X/Twitter share window with prefilled text
 */
const TweetShare = {
  /**
   * Open the X/Twitter intent with text and URL.
   * @param {Object} options - Share options
   * @param {string} options.text - Tweet text
   * @param {string} options.url - URL to share
   */
  open({ text, url }) {
    const base = 'https://twitter.com/intent/tweet';
    const params = new URLSearchParams();

    if (text) params.set('text', text);
    if (url) params.set('url', url);

    const shareUrl = `${base}?${params.toString()}`;
    window.open(shareUrl, '_blank', 'noopener,noreferrer');
  }
};

window.TweetShare = TweetShare;
