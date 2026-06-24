// Draft Recap — client-side pick filtering.
// Filters the pick-by-pick board to all / round 1 / round 2 / surprises without
// a reload, so each view is a clean, self-contained screenshot for sharing.

document.addEventListener("DOMContentLoaded", () => {
  const filters = document.querySelectorAll(".dr-filter");
  const rows = document.querySelectorAll(".dr-row");
  if (!filters.length || !rows.length) return;

  function matches(row, filter) {
    switch (filter) {
      case "round-1":
        return row.dataset.round === "1";
      case "round-2":
        return row.dataset.round === "2";
      case "movers":
        return row.dataset.mover === "1";
      default:
        return true;
    }
  }

  function apply(filter) {
    rows.forEach((row) => {
      row.style.display = matches(row, filter) ? "" : "none";
    });
  }

  filters.forEach((btn) => {
    btn.addEventListener("click", () => {
      filters.forEach((b) => b.classList.remove("is-active"));
      btn.classList.add("is-active");
      apply(btn.dataset.filter);
    });
  });
});

// Scatter hover card — name, position, expected vs. realized draft slot.
document.addEventListener("DOMContentLoaded", () => {
  const svg = document.querySelector(".recap-scatter");
  if (!svg) return;

  const tip = document.createElement("div");
  tip.className = "recap-tip";
  tip.hidden = true;
  document.body.appendChild(tip);

  const DIR_LABEL = { riser: "Riser", faller: "Faller", "in range": "On the board" };

  function row(cls, text) {
    const el = document.createElement("span");
    el.className = cls;
    el.textContent = text; // textContent, never innerHTML — names are untrusted
    return el;
  }

  function show(face, evt) {
    const d = face.dataset;
    const dir = DIR_LABEL[d.dir] || "";
    const pos = d.pos ? `${d.pos} · ` : "";
    tip.replaceChildren(
      row("recap-tip__name", d.name),
      row("recap-tip__meta", `${pos}${dir}`),
      row("recap-tip__slot", `Expected #${d.exp} → Drafted #${d.act}`),
    );
    tip.dataset.dir = d.dir;
    tip.hidden = false;
    move(evt);
  }

  function move(evt) {
    const pad = 14;
    let x = evt.clientX + pad;
    let y = evt.clientY + pad;
    const r = tip.getBoundingClientRect();
    if (x + r.width > window.innerWidth) x = evt.clientX - r.width - pad;
    if (y + r.height > window.innerHeight) y = evt.clientY - r.height - pad;
    tip.style.left = `${x}px`;
    tip.style.top = `${y}px`;
  }

  svg.addEventListener("mouseover", (e) => {
    const face = e.target.closest(".recap-scatter__face");
    if (face) show(face, e);
  });
  svg.addEventListener("mousemove", (e) => {
    if (!tip.hidden) move(e);
  });
  svg.addEventListener("mouseout", (e) => {
    if (!e.relatedTarget || !e.relatedTarget.closest(".recap-scatter__face")) {
      tip.hidden = true;
    }
  });
});
