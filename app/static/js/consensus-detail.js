// Per-analyst source-breakdown expander — shared by the consensus board
// (/consensus) and the home consensus hero (/).
//
// A ▸ toggle in each row's leading column reveals a sibling detail row listing
// where each source ranked the player, with an outbound link to their board.
// The panel is hydrated once, on first expand, from
// GET /api/consensus/player/{id}?draft_year=&kind= (PlayerConsensusDetail).
//
// Each toggle reads draft_year / board_kind from its own table's data-*
// attributes, so the module works unchanged on any page that renders the
// markup (a .cb-toggle button with aria-controls pointing at a .cb-detail-row
// containing a .cb-detail panel, inside a .cb-row in a table carrying
// data-draft-year / data-board-kind).
(function () {
  "use strict";

  /** Replace the panel contents with a single status line (loading / error). */
  function setStatus(container, message, isError) {
    container.textContent = "";
    const p = document.createElement("p");
    p.className =
      "cb-detail__status" + (isError ? " cb-detail__status--error" : "");
    p.textContent = message;
    container.appendChild(p);
  }

  /** Map a per-source rank to a percentage on the [high, low] (consensus-folded) scale. */
  function scalePct(rank, scaleMin, span) {
    return (((rank - scaleMin) / span) * 100).toFixed(1) + "%";
  }

  /** Build a delta chip element (source rank vs. consensus rank). */
  function deltaChip(sourceRank, consensusRank) {
    const d = sourceRank - consensusRank;
    const el = document.createElement("span");
    if (d < 0) {
      el.className = "cb-source__delta cb-source__delta--up";
      el.textContent = "▲" + Math.abs(d);
    } else if (d > 0) {
      el.className = "cb-source__delta cb-source__delta--down";
      el.textContent = "▼" + d;
    } else {
      el.className = "cb-source__delta cb-source__delta--flat";
      el.textContent = "–";
    }
    return el;
  }

  /** Render the full per-analyst breakdown panel from a PlayerConsensusDetail. */
  function renderDetail(container, detail) {
    container.textContent = "";
    const ranks = detail.source_ranks || [];
    const consensusRank = detail.consensus_rank;

    // Header: title + summary line
    const head = document.createElement("div");
    head.className = "cb-detail__head";
    const title = document.createElement("span");
    title.className = "cb-detail__title";
    title.textContent =
      "Where analysts ranked " + (detail.player_name || "this player");
    const summary = document.createElement("span");
    summary.className = "cb-detail__summary";
    summary.textContent =
      ranks.length +
      " of " +
      detail.num_sources +
      " boards · range #" +
      detail.high_rank +
      "–#" +
      detail.low_rank +
      " · consensus #" +
      consensusRank;
    head.appendChild(title);
    head.appendChild(summary);
    container.appendChild(head);

    // Scale strip (skip when every source agrees — no spread to show)
    if (detail.low_rank !== detail.high_rank) {
      const scaleMin = Math.min(detail.high_rank, consensusRank);
      const scaleMax = Math.max(detail.low_rank, consensusRank);
      const span = scaleMax - scaleMin || 1;

      const scale = document.createElement("div");
      scale.className = "cb-detail__scale";
      const edgeLo = document.createElement("span");
      edgeLo.className = "cb-detail__scale-edge";
      edgeLo.textContent = "#" + detail.high_rank;
      const track = document.createElement("div");
      track.className = "cb-detail__scale-track";
      ranks.forEach(function (entry) {
        const tick = document.createElement("span");
        tick.className = "cb-detail__tick";
        tick.style.left = scalePct(entry.source_rank, scaleMin, span);
        tick.title =
          (entry.source_display_name || entry.source_name) +
          " · #" +
          entry.source_rank;
        track.appendChild(tick);
      });
      const cons = document.createElement("span");
      cons.className = "cb-detail__consensus";
      cons.style.left = scalePct(consensusRank, scaleMin, span);
      cons.title = "Consensus #" + consensusRank;
      track.appendChild(cons);
      const edgeHi = document.createElement("span");
      edgeHi.className = "cb-detail__scale-edge";
      edgeHi.textContent = "#" + detail.low_rank;
      scale.appendChild(edgeLo);
      scale.appendChild(track);
      scale.appendChild(edgeHi);
      container.appendChild(scale);
    }

    // Per-source list — outbound link per creator (↗) when an article URL exists
    const list = document.createElement("ul");
    list.className = "cb-detail__sources";
    ranks.forEach(function (entry) {
      const li = document.createElement("li");
      li.className = "cb-source";
      if (Math.abs(entry.source_rank - consensusRank) >= 5) {
        li.classList.add("is-outlier");
      }

      const rank = document.createElement("span");
      rank.className = "cb-source__rank";
      rank.textContent = "#" + entry.source_rank;

      const main = document.createElement("span");
      main.className = "cb-source__main";

      const name = entry.source_display_name || entry.source_name || "Source";
      let nameEl;
      if (entry.article_url) {
        nameEl = document.createElement("a");
        nameEl.className = "cb-source__name cb-source__name--link";
        nameEl.href = entry.article_url;
        nameEl.target = "_blank";
        nameEl.rel = "noopener noreferrer";
        nameEl.title = name + "'s published board (opens in a new tab)";
      } else {
        nameEl = document.createElement("span");
        nameEl.className = "cb-source__name";
      }
      nameEl.textContent = name;
      main.appendChild(nameEl);

      if (entry.article_title && entry.article_url) {
        const board = document.createElement("a");
        board.className = "cb-source__board";
        board.href = entry.article_url;
        board.target = "_blank";
        board.rel = "noopener noreferrer";
        board.textContent = entry.article_title;
        main.appendChild(board);
      }

      li.appendChild(rank);
      li.appendChild(main);
      li.appendChild(deltaChip(entry.source_rank, consensusRank));
      list.appendChild(li);
    });
    container.appendChild(list);

    // Attribution footnote — reinforces aggregate-don't-reproduce stance
    const foot = document.createElement("p");
    foot.className = "cb-detail__foot";
    foot.textContent =
      "Ranks link to each creator's original board. DraftGuru aggregates — it doesn't reproduce — their work.";
    container.appendChild(foot);
  }

  /** Fetch + render a player's per-source breakdown into its panel (once). */
  function loadDetail(row, panel, draftYear, boardKind) {
    const playerId = row.dataset.playerId;
    if (!playerId || !draftYear || !boardKind) {
      setStatus(panel, "No source data available.", true);
      panel.dataset.loaded = "true";
      return;
    }
    setStatus(panel, "Loading source ranks…", false);
    const url =
      "/api/consensus/player/" +
      encodeURIComponent(playerId) +
      "?draft_year=" +
      encodeURIComponent(draftYear) +
      "&kind=" +
      encodeURIComponent(boardKind);

    fetch(url, { headers: { Accept: "application/json" } })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (detail) {
        if (!detail.source_ranks || detail.source_ranks.length === 0) {
          setStatus(panel, "No per-source ranks available for this player.", false);
        } else {
          renderDetail(panel, detail);
        }
        panel.dataset.loaded = "true";
      })
      .catch(function () {
        // Leave data-loaded="false" so a later re-expand retries the fetch.
        setStatus(panel, "Couldn't load source ranks. Try again.", true);
        panel.dataset.loaded = "false";
      });
  }

  /** Wire one toggle button: expand/collapse its detail row, lazy-load once. */
  function wireToggle(btn) {
    btn.addEventListener("click", function (e) {
      // Attached to the button itself so this fires before (and stops) the
      // row's navigate-to-player handler on the ancestor.
      e.stopPropagation();
      const row = btn.closest(".cb-row");
      const detailRow = document.getElementById(btn.getAttribute("aria-controls"));
      if (!row || !detailRow) return;

      const table = btn.closest("table");
      const draftYear = table ? table.dataset.draftYear : null;
      const boardKind = table ? table.dataset.boardKind : null;

      const expanded = row.classList.toggle("is-expanded");
      btn.setAttribute("aria-expanded", expanded ? "true" : "false");
      btn.title = expanded ? "Hide source ranks" : "Show source ranks";
      detailRow.hidden = !expanded;

      if (expanded) {
        const panel = detailRow.querySelector(".cb-detail");
        if (panel && panel.dataset.loaded !== "true") {
          loadDetail(row, panel, draftYear, boardKind);
        }
      }
    });
  }

  function init() {
    document.querySelectorAll(".cb-toggle").forEach(wireToggle);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
