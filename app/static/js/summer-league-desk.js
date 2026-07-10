/**
 * Summer League Desk (#509) -- page-scoped JS.
 *
 * The ONLY interactive behavior here is the Morning slate's "show all N
 * games" tail-collapse toggle. There is deliberately no state-switcher JS:
 * the server renders exactly one state per request (behavior spec §1/§2),
 * so there is nothing to toggle between Morning/Live/Ledger client-side.
 *
 * Progressive enhancement: every slate card is present in the server-rendered
 * HTML (`hidden` attribute on the tail cards, not a client-side render), so
 * with JS disabled every game is still in the DOM -- just not all visible.
 */
document.addEventListener('DOMContentLoaded', function () {
  var toggle = document.getElementById('deskSlateToggle');
  var slate = document.getElementById('deskSlate');
  if (!toggle || !slate) {
    return;
  }

  toggle.addEventListener('click', function () {
    var expanded = toggle.getAttribute('aria-expanded') === 'true';
    var next = !expanded;
    toggle.setAttribute('aria-expanded', String(next));
    toggle.textContent = next ? toggle.dataset.less : toggle.dataset.more;

    var tailCards = slate.querySelectorAll('.desk__game-card--tail');
    tailCards.forEach(function (card) {
      if (next) {
        card.removeAttribute('hidden');
      } else {
        card.setAttribute('hidden', '');
      }
    });
  });
});
