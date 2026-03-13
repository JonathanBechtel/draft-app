/**
 * ============================================================================
 * PODCAST-AUDIO.JS — Shared podcast audio playback manager
 * Used by homepage and /podcasts page so only one episode plays at a time
 * ============================================================================
 */

(function initPodcastAudioManager() {
  const PLAY_ICON =
    '<svg viewBox="0 0 24 24"><polygon points="5,3 19,12 5,21"></polygon></svg>';
  const PAUSE_ICON =
    '<svg viewBox="0 0 24 24"><rect x="6" y="4" width="4" height="16"></rect><rect x="14" y="4" width="4" height="16"></rect></svg>';

  const PodcastAudio = {
    audio: null,
    activeBtn: null,
    activeProgress: null,
    activeTime: null,

    init() {
      if (this.audio) {
        return;
      }

      this.audio = new Audio();
      this.audio.addEventListener('timeupdate', () => this._onTimeUpdate());
      this.audio.addEventListener('ended', () => this._onEnded());
    },

    play(audioUrl, btn) {
      if (!audioUrl || !btn) {
        return;
      }

      this.init();

      if (this.activeBtn === btn && !this.audio.paused) {
        this.pause();
        return;
      }

      if (this.activeBtn && this.activeBtn !== btn) {
        this._resetActiveDisplay();
      }

      if (this.audio.src !== audioUrl) {
        this.audio.src = audioUrl;
      }

      this.activeBtn = btn;
      this._setActiveDisplay(btn);
      btn.innerHTML = PAUSE_ICON;

      const playPromise = this.audio.play();
      if (playPromise && typeof playPromise.catch === 'function') {
        playPromise.catch(() => {
          if (this.activeBtn === btn) {
            this._resetBtn(btn);
          }
        });
      }
    },

    pause() {
      if (!this.audio) {
        return;
      }

      this.audio.pause();
      if (this.activeBtn) {
        this._resetBtn(this.activeBtn);
      }
    },

    seek(value) {
      if (this.audio && this.audio.duration) {
        this.audio.currentTime = (value / 100) * this.audio.duration;
      }
    },

    _setActiveDisplay(btn) {
      const row = btn.closest(
        '.podcast-featured__player, .episode-row--page, .episode-row__inner'
      );

      this.activeProgress = row ? row.querySelector('.progress-bar') : null;
      this.activeTime = row ? row.querySelector('.progress-time') : null;
    },

    _onTimeUpdate() {
      if (!this.audio || !this.audio.duration) {
        return;
      }

      const pct = (this.audio.currentTime / this.audio.duration) * 100;
      if (this.activeProgress) {
        this.activeProgress.value = pct;
      }
      if (this.activeTime) {
        this.activeTime.textContent = this._formatTime(this.audio.currentTime);
      }
    },

    _onEnded() {
      this._resetActiveDisplay();
      this.activeBtn = null;
      this.activeProgress = null;
      this.activeTime = null;
    },

    _resetActiveDisplay() {
      if (this.activeBtn) {
        this._resetBtn(this.activeBtn);
      }
      if (this.activeProgress) {
        this.activeProgress.value = 0;
      }
      if (this.activeTime) {
        this.activeTime.textContent = '0:00';
      }
    },

    _resetBtn(btn) {
      btn.innerHTML = PLAY_ICON;
    },

    _formatTime(seconds) {
      const m = Math.floor(seconds / 60);
      const s = Math.floor(seconds % 60);
      return `${m}:${s.toString().padStart(2, '0')}`;
    }
  };

  window.PodcastAudio = PodcastAudio;
})();
