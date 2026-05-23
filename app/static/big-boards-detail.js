// Big-board detail page client-side helpers.
//
// Two responsibilities:
//   1. Player autocomplete on the "Add Entry" form (calls /players/search).
//   2. Per-row auto-save: when the admin tabs out of a rank or tier input
//      whose value has changed, POST the new values to the existing
//      /admin/big-boards/{board}/entries/{entry}/update route.

(function () {
  function initAutocomplete() {
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
          hide();
        }
      }
    }

    input.addEventListener("input", function () {
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
      setTimeout(hide, 120);
    });
  }

  function initAutosave() {
    const inputs = document.querySelectorAll(".bb-autosave");
    if (!inputs.length) return;

    const statusEl = document.getElementById("bb-autosave-status");
    let statusTimer = null;

    function setStatus(text, isError) {
      if (!statusEl) return;
      statusEl.textContent = text;
      statusEl.style.color = isError ? "var(--color-accent-rose, #f43f5e)" : "";
      clearTimeout(statusTimer);
      if (!isError) {
        statusTimer = setTimeout(() => {
          statusEl.textContent = "";
        }, 1500);
      }
    }

    // Capture starting values so we only POST on actual change.
    inputs.forEach((el) => {
      el.dataset.originalValue = el.value;
    });

    async function save(el) {
      const row = el.closest("tr[data-entry-id]");
      if (!row) return;
      const entryId = row.dataset.entryId;
      const boardId = row.dataset.boardId;
      if (!entryId || !boardId) return;

      const rankEl = row.querySelector('[data-field="rank"]');
      const tierEl = row.querySelector('[data-field="tier"]');
      const rankVal = rankEl ? rankEl.value.trim() : "";
      const tierVal = tierEl ? tierEl.value.trim() : "";

      if (rankVal === "") {
        setStatus("Rank is required.", true);
        if (rankEl) rankEl.focus();
        return;
      }

      const body = new URLSearchParams();
      body.append("rank", rankVal);
      body.append("tier", tierVal);

      setStatus("Saving…", false);
      try {
        const resp = await fetch(
          "/admin/big-boards/" + boardId + "/entries/" + entryId + "/update",
          {
            method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded" },
            body: body.toString(),
            redirect: "follow",
            credentials: "same-origin",
          }
        );
        if (resp.ok || resp.redirected) {
          // FastAPI returns a 303 redirect which fetch follows; success
          // = either 200/2xx after redirect, OR a redirected response.
          setStatus("Saved", false);
          el.dataset.originalValue = el.value;
          // Surface server-side error encoded in the redirect URL.
          if (resp.redirected && resp.url.includes("error=")) {
            const url = new URL(resp.url);
            const msg = url.searchParams.get("error");
            if (msg) setStatus(decodeURIComponent(msg), true);
          }
        } else {
          setStatus("Save failed (" + resp.status + ")", true);
        }
      } catch (err) {
        setStatus("Save failed: " + err.message, true);
      }
    }

    inputs.forEach((el) => {
      el.addEventListener("blur", function () {
        if (el.value === el.dataset.originalValue) return;
        save(el);
      });
      // Tabbing through fields is faster if Enter also commits.
      el.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") {
          ev.preventDefault();
          el.blur();
        }
      });
    });
  }

  function init() {
    initAutocomplete();
    initAutosave();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
