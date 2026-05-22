// Player autocomplete for the big-board add-entry form.
// Calls the existing /players/search endpoint and writes the selected
// player's id into the hidden #player_id input that the form posts.

(function () {
  function init() {
    const input = document.getElementById("player-search");
    const list = document.getElementById("player-autocomplete");
    const hidden = document.getElementById("player_id");
    if (!input || !list || !hidden) return;

    let abortCtrl = null;
    let debounceTimer = null;

    function hide() {
      list.hidden = true;
      list.replaceChildren();
    }

    function show(results) {
      list.replaceChildren();
      if (!results.length) {
        hide();
        return;
      }
      for (const r of results) {
        const li = document.createElement("li");
        li.className = "admin-autocomplete__item";
        const name = document.createElement("strong");
        name.textContent = r.display_name;
        li.appendChild(name);
        if (r.school) {
          const school = document.createElement("span");
          school.className = "admin-text--small";
          school.style.marginLeft = "0.5rem";
          school.textContent = r.school;
          li.appendChild(school);
        }
        li.addEventListener("mousedown", function (ev) {
          ev.preventDefault();
          input.value = r.display_name;
          hidden.value = r.id;
          hide();
        });
        list.appendChild(li);
      }
      list.hidden = false;
    }

    async function query(q) {
      if (abortCtrl) abortCtrl.abort();
      abortCtrl = new AbortController();
      try {
        const resp = await fetch(
          "/players/search?q=" + encodeURIComponent(q),
          { signal: abortCtrl.signal }
        );
        if (!resp.ok) return;
        const results = await resp.json();
        show(results);
      } catch (err) {
        if (err && err.name !== "AbortError") {
          // Network or parse error — silently hide rather than block the form.
          hide();
        }
      }
    }

    input.addEventListener("input", function () {
      // Any keystroke invalidates a previously-selected id.
      hidden.value = "";
      const q = input.value.trim();
      clearTimeout(debounceTimer);
      if (q.length < 2) {
        hide();
        return;
      }
      debounceTimer = setTimeout(() => query(q), 150);
    });

    input.addEventListener("blur", function () {
      // Hide after the click handler has had a chance to fire.
      setTimeout(hide, 120);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
