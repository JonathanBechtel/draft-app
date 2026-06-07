// Big-board detail page client-side helpers.
//
// Two responsibilities:
//   1. Player autocomplete on the "Add Entry" form (calls /players/search).
//      When the search returns no results for a typed name, the dropdown
//      surfaces an inline "Create stub for '<name>'" affordance that mints a
//      stub and adds the entry in one POST, then reloads the page.
//   2. Per-row auto-save: when the admin tabs out of a rank or tier input
//      whose value has changed, POST the new values to the existing
//      /admin/boards/{board}/entries/{entry}/update route.

(function () {
  function initAutocomplete() {
    const input = document.getElementById("player-search");
    const list = document.getElementById("player-autocomplete");
    const hidden = document.getElementById("player_id");
    if (!input || !list || !hidden) return;

    let abortCtrl = null;
    let debounceTimer = null;
    // Track whether the last search returned zero matches so the submit
    // handler can offer stub creation instead of the pick-from-dropdown prompt.
    let lastQueryHadNoResults = false;
    let lastQueryText = "";

    function hide() {
      list.hidden = true;
      list.replaceChildren();
      lastQueryHadNoResults = false;
    }

    function showStubOption(q) {
      // Append a special "Create stub for '<name>'" item at the bottom of an
      // empty dropdown (or after existing results).
      const li = document.createElement("li");
      li.className = "admin-autocomplete__item admin-autocomplete__item--stub";
      li.style.borderTop = list.children.length
        ? "1px solid var(--color-border, #e5e7eb)"
        : "";
      li.style.fontStyle = "italic";
      li.style.color = "var(--color-accent-blue, #3b82f6)";
      li.textContent = "Create stub for “" + q + "”";
      li.addEventListener("mousedown", function (ev) {
        ev.preventDefault();
        hidden.value = "__stub__";
        input.dataset.stubName = q;
        hide();
        // Programmatically submit the form to trigger the stub path.
        const form = input.closest("form");
        if (form) form.requestSubmit();
      });
      list.appendChild(li);
      list.hidden = false;
    }

    function show(results, q) {
      list.replaceChildren();
      lastQueryHadNoResults = results.length === 0;
      lastQueryText = q;
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
      // Always append the stub option so the admin can create a new stub even
      // when some fuzzy matches exist (the name might be genuinely new).
      if (q.length >= 2) {
        showStubOption(q);
      }
      if (list.children.length) {
        list.hidden = false;
      }
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
        show(results, q);
      } catch (err) {
        if (err && err.name !== "AbortError") {
          hide();
        }
      }
    }

    input.addEventListener("input", function () {
      hidden.value = "";
      delete input.dataset.stubName;
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

    // Submit handler: intercept the form to route stub creation through the
    // dedicated endpoint instead of the normal add-entry POST.
    const form = input.closest("form");
    if (form) {
      form.addEventListener("submit", async function (ev) {
        const stubName = input.dataset.stubName;
        if (hidden.value === "__stub__" && stubName) {
          // Stub creation path: POST to the inline-stub endpoint.
          ev.preventDefault();
          const boardId = form.action.match(/\/boards\/(\d+)\//)?.[1];
          if (!boardId) return;

          // Collect sibling form fields (position, tier, round, team_id, etc.)
          const body = new URLSearchParams();
          body.set("name", stubName);
          form.querySelectorAll("[name]").forEach(function (el) {
            const n = el.getAttribute("name");
            if (!n || n === "player_id") return;
            body.set(n, el.value);
          });

          const submitBtn = form.querySelector("[type=submit]");
          if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.textContent = "Creating…";
          }

          try {
            const resp = await fetch(
              "/admin/boards/" + boardId + "/entries/inline-stub",
              {
                method: "POST",
                headers: { "Content-Type": "application/x-www-form-urlencoded" },
                body: body.toString(),
                credentials: "same-origin",
              }
            );
            const data = await resp.json();
            if (data.outcome === "created") {
              // Success: reload the board page with a flash message.
              window.location.href =
                "/admin/boards/" + boardId + "?success=stub_minted_inline";
            } else {
              // Show the server error inline near the input.
              let msg = data.message || "Stub creation failed.";
              if (data.outcome === "blocked_existing" && data.player_id) {
                msg += " You can select them from the autocomplete.";
              }
              alert(msg);
              if (submitBtn) {
                submitBtn.disabled = false;
                submitBtn.textContent = "Add";
              }
              hidden.value = "";
              delete input.dataset.stubName;
            }
          } catch (err) {
            alert("Stub creation failed: " + err.message);
            if (submitBtn) {
              submitBtn.disabled = false;
              submitBtn.textContent = "Add";
            }
          }
          return;
        }

        // Normal path: player must have been selected from autocomplete.
        if (!hidden.value || hidden.value === "__stub__") {
          ev.preventDefault();
          input.setCustomValidity("Pick a player from the dropdown list, or choose 'Create stub for...' to add a new name.");
          input.reportValidity();
          input.focus();
        }
      });
      input.addEventListener("input", function () {
        input.setCustomValidity("");
      });
    }
  }

  function initAutosave() {
    const inputs = document.querySelectorAll(".bb-autosave");
    if (!inputs.length) return;

    const statusEl = document.getElementById("bb-autosave-status");
    let statusTimer = null;
    // In-flight save promises. Flushed before any form on the page submits
    // so Approve/Reject/Reopen/Clone/Move/Delete can't race past a pending
    // autosave and overwrite or lose typed-but-not-yet-POSTed changes.
    const inFlight = new Set();

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

      // Serialize every autosave control in the row by its `name`, so
      // mock-draft fields (team_id, round, trade_note) are persisted too —
      // not just position/tier. The update route accepts all of them.
      const fieldEls = row.querySelectorAll(".bb-autosave[name]");
      const body = new URLSearchParams();
      let positionVal = "";
      let positionEl = null;
      fieldEls.forEach((f) => {
        const name = f.getAttribute("name");
        if (!name) return;
        const val = f.value.trim();
        if (name === "position") {
          positionVal = val;
          positionEl = f;
        }
        body.append(name, val);
      });

      if (positionVal === "") {
        setStatus("Rank is required.", true);
        if (positionEl) positionEl.focus();
        return;
      }

      setStatus("Saving…", false);
      try {
        const resp = await fetch(
          "/admin/boards/" + boardId + "/entries/" + entryId + "/update",
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

    function trackedSave(el) {
      const p = save(el).finally(() => inFlight.delete(p));
      inFlight.add(p);
      return p;
    }

    async function flushPending() {
      const dirty = [];
      document.querySelectorAll(".bb-autosave").forEach((el) => {
        if (el.value !== el.dataset.originalValue) dirty.push(el);
      });
      dirty.forEach((el) => trackedSave(el));
      if (inFlight.size === 0) return;
      await Promise.allSettled(Array.from(inFlight));
    }

    inputs.forEach((el) => {
      el.addEventListener("blur", function () {
        if (el.value === el.dataset.originalValue) return;
        trackedSave(el);
      });
      // Tabbing through fields is faster if Enter also commits.
      el.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") {
          ev.preventDefault();
          el.blur();
        }
      });
    });

    // Intercept every POST form on the page (Approve, Reject, Delete,
    // Reopen, Clone, Move up/down, Remove entry, Add entry) and flush
    // any pending autosaves before allowing the navigation. This closes
    // a race where the in-flight tier/rank POST got cancelled by the
    // browser when the action form submitted and navigated away.
    function hasPendingWork() {
      if (inFlight.size > 0) return true;
      for (const el of document.querySelectorAll(".bb-autosave")) {
        if (el.value !== el.dataset.originalValue) return true;
      }
      return false;
    }

    // Move inline `onsubmit="return confirm('...')"` to data-bb-confirm so
    // confirmation runs once in the capture-phase submit listener. Without
    // this, form.submit() (called after flushPending resolves) bypasses
    // the inline onsubmit handler and a clicked-Cancel would still execute
    // the destructive POST. The inline attribute stays as the JS-disabled
    // fallback at template render time but is detached during init.
    document
      .querySelectorAll("form[onsubmit]")
      .forEach(function (form) {
        const raw = form.getAttribute("onsubmit") || "";
        const match = raw.match(/confirm\(['"]([\s\S]*?)['"]\)/);
        if (match) {
          form.dataset.bbConfirm = match[1].replace(/\\'/g, "'");
          form.removeAttribute("onsubmit");
        }
      });

    function userConfirmed(form) {
      const msg = form.dataset.bbConfirm;
      if (!msg) return true;
      return window.confirm(msg);
    }

    document.addEventListener(
      "submit",
      function (ev) {
        const form = ev.target;
        if (!(form instanceof HTMLFormElement)) return;
        if (form.method.toLowerCase() !== "post") return;
        if (form.dataset.bbFlushed === "1") return; // already flushed; let through

        // Run any confirmation gate FIRST so a clicked-Cancel cleanly
        // cancels the navigation regardless of whether there's pending
        // autosave work to flush.
        if (!userConfirmed(form)) {
          ev.preventDefault();
          return;
        }

        if (!hasPendingWork()) return; // nothing to flush; normal submit
        ev.preventDefault();
        setStatus("Saving changes…", false);
        flushPending().then(() => {
          form.dataset.bbFlushed = "1";
          form.submit();
        });
      },
      true
    );
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
