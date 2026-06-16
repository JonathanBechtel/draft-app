/* Summer League Explorer — light in-place enhancement.
 *
 * Progressive enhancement over the server-rendered builder: intercept the
 * filter form submit and the sort/pager links inside the results, fetch the
 * same URL with `?partial=1` (which returns just the results fragment), and
 * swap it in without a full page reload. Everything still works with JS off —
 * the form is a normal GET and the links are real hrefs.
 */
(function () {
  "use strict";

  var RESULTS_ID = "explorer-results";
  var form = document.getElementById("explorer-form");
  var resultsHost = document.getElementById(RESULTS_ID);
  if (!form || !resultsHost) return;

  // The persistent ancestor we delegate clicks from (survives result swaps).
  var main = resultsHost.closest("main") || document.body;

  function withPartial(url) {
    return url + (url.indexOf("?") === -1 ? "?" : "&") + "partial=1";
  }

  // Strip a `partial` param so the address bar shows the canonical URL.
  function canonical(url) {
    return url.replace(/([?&])partial=1(&|$)/, function (_m, pre, post) {
      return post ? pre : pre === "?" ? "" : "";
    });
  }

  function load(url, push) {
    var host = document.getElementById(RESULTS_ID);
    if (host) host.classList.add("is-loading");
    fetch(withPartial(url), { headers: { "X-Requested-With": "fetch" } })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.text();
      })
      .then(function (html) {
        var cur = document.getElementById(RESULTS_ID);
        if (!cur) {
          window.location.assign(url);
          return;
        }
        cur.outerHTML = html;
        if (push) history.pushState({ explorerUrl: url }, "", canonical(url));
        // Keep the viewport anchored to the results, not jumped to top.
        var fresh = document.getElementById(RESULTS_ID);
        if (fresh) fresh.scrollIntoView({ block: "nearest" });
      })
      .catch(function () {
        // Network/parse failure — fall back to a real navigation.
        window.location.assign(url);
      });
  }

  // Build the query URL from the current form state and load it.
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    // Drop empty fields so the shareable URL stays clean (the server treats a
    // missing param the same as a blank one).
    var clean = new URLSearchParams();
    new URLSearchParams(new FormData(form)).forEach(function (v, k) {
      if (v !== "") clean.append(k, v);
    });
    load(form.action + "?" + clean.toString(), true);
  });

  // Sort headers and pager links live inside the swapped results fragment, so
  // delegate from the persistent ancestor.
  main.addEventListener("click", function (e) {
    var link = e.target.closest && e.target.closest("a");
    if (!link) return;
    var host = document.getElementById(RESULTS_ID);
    if (!host || !host.contains(link)) return; // only links inside the results
    if (link.target === "_blank" || e.metaKey || e.ctrlKey) return;
    e.preventDefault();
    load(link.getAttribute("href"), true);
  });

  // Back/forward should restore the corresponding results.
  window.addEventListener("popstate", function () {
    load(window.location.href, false);
  });
})();
