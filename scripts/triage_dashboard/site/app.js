// ExtensionsIndex triage dashboard: renders data.json and keeps it current.
//
// Every string from GitHub (titles, comments, entry fields) is untrusted and is inserted as text,
// never as HTML. Links are only made from http(s) URLs.
(function () {
  "use strict";

  var POLL_MS = 30000;
  var STALE_MIN = 45; // if the data has not been regenerated for this long, say so
  var NEW_DAYS = 7;

  var state = { data: null, me: "", filter: "all", term: "", changed: new Set(), lastPoll: null, pollError: null };

  // --------------------------------------------------------------------------- small helpers

  function h(tag, props) {
    var el = document.createElement(tag);
    if (props) {
      Object.keys(props).forEach(function (k) {
        var v = props[k];
        if (v === undefined || v === null || v === false) return;
        if (k === "class") el.className = v;
        else if (k === "text") el.textContent = v;
        else if (k.slice(0, 2) === "on") el.addEventListener(k.slice(2), v);
        else el.setAttribute(k, v === true ? "" : v);
      });
    }
    for (var i = 2; i < arguments.length; i++) add(el, arguments[i]);
    return el;
  }
  function add(el, kid) {
    if (kid === undefined || kid === null || kid === false) return;
    if (Array.isArray(kid)) { kid.forEach(function (k) { add(el, k); }); return; }
    el.appendChild(typeof kid === "string" || typeof kid === "number" ? document.createTextNode(String(kid)) : kid);
  }
  function safeUrl(u) { return typeof u === "string" && /^https?:\/\//i.test(u) ? u : null; }
  function a(url, text, cls) {
    var u = safeUrl(url);
    if (!u) return h("span", { class: cls }, text);
    return h("a", { href: u, target: "_blank", rel: "noopener noreferrer", class: cls }, text);
  }
  function sep(list, s) {
    var out = [];
    list.filter(Boolean).forEach(function (x, i) { if (i) out.push(s || " · "); out.push(x); });
    return out;
  }
  function daysSince(t) { return t ? (Date.now() - Date.parse(t)) / 86400000 : 0; }
  function fmtDays(d) {
    if (d < 1) return "today";
    if (d < 60) return Math.round(d) + "d";
    if (d < 730) return Math.round(d / 30.4) + "mo";
    return (d / 365).toFixed(1) + "y";
  }
  function minutesAgo(t) {
    var m = (Date.now() - Date.parse(t)) / 60000;
    if (m < 1) return "just now";
    if (m < 60) return Math.round(m) + " min ago";
    if (m < 48 * 60) return Math.round(m / 60) + " h ago";
    return Math.round(m / 1440) + " days ago";
  }
  function fmtDate(t) {
    return t ? new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) : "";
  }
  function store(k, v) { try { if (v) localStorage.setItem(k, v); else localStorage.removeItem(k); } catch (e) { /* storage unavailable */ } }
  function load(k) { try { return localStorage.getItem(k) || ""; } catch (e) { return ""; } }
  function same(x, y) { return (x || "").toLowerCase() === (y || "").toLowerCase(); }

  // --------------------------------------------------------------------------- classification

  function isNew(p) { return !p.draft && p.turn !== "hold" && (!p.first_response || daysSince(p.created) < NEW_DAYS); }

  function status(p) {
    if (p.draft) return ["Draft", "muted"];
    if (p.turn === "hold") return ["On hold", "bad"];
    if (p.steps.some(function (s) { return s.kind === "merge"; })) return ["Ready to merge", "ok"];
    if (!p.first_response) return ["New", "new"];
    if (p.changes_by.length && p.turn === "contributor") return ["Changes requested", "warn"];
    if (p.turn === "maintainers") return ["Maintainers' turn", "turn"];
    return ["Contributor's turn", "muted"];
  }

  var WHY_MINE = { assigned: "You claimed it", requested: "Review requested from you", replied: "They answered you",
    updated: "They pushed to the source after your comment" };
  var WHY_OTHER = { assigned: "claimed by", requested: "review requested from", replied: "answered",
    updated: "source updated after a comment by" };

  // Splits each PR's next steps into: mine, anyone's, and other named maintainers'.
  function lanes(data, me) {
    var out = { mine: [], team: [], others: [], waiting: [], hold: [], fresh: [] };
    data.prs.forEach(function (p) {
      var mineSteps = [];
      p.steps.forEach(function (s) {
        var o = s.owners.find(function (x) { return me && same(x.login, me); });
        if (o) mineSteps.push({ step: s, why: WHY_MINE[o.why] || "" });
        else if (!s.owners.length) { if (s.kind !== "first") out.team.push({ p: p, step: s }); }
        else out.others.push({ p: p, step: s });
      });
      if (mineSteps.length) out.mine.push({ p: p, steps: mineSteps });
      if (isNew(p)) out.fresh.push(p);
      if (p.turn === "contributor") out.waiting.push(p);
      if (p.turn === "hold") out.hold.push(p);
    });
    var order = { merge: 0, approve: 1, respond: 2, label: 3 };
    out.team.sort(function (x, y) { return (order[x.step.kind] - order[y.step.kind]) || (x.p.number - y.p.number); });
    out.mine.sort(function (x, y) { return Date.parse(x.p.since) - Date.parse(y.p.since); });
    out.fresh.sort(function (x, y) { return y.number - x.number; });
    out.waiting.sort(function (x, y) { return Date.parse(x.since) - Date.parse(y.since); });
    return out;
  }

  // --------------------------------------------------------------------------- pieces

  function prHead(p, extra) {
    var st = status(p);
    return h("div", { class: "head" },
      a(p.url, "#" + p.number, "prnum"), h("span", { class: "name", text: p.name }),
      h("span", { class: "pill " + st[1], text: st[0] }),
      state.changed.has(p.number) ? h("span", { class: "changed", text: "updated" }) : null,
      extra);
  }

  function quote(label, e, clamp) {
    if (!e || !e.text) return null;
    var body = h("div", { class: "ctx quote" + (clamp ? " clamp" : "") },
      h("span", { class: "who", text: label + " @" + e.who + (e.ai ? " (AI-assisted)" : "") }), " ",
      a(e.url, fmtDays(daysSince(e.t)) === "today" ? "today" : fmtDays(daysSince(e.t)) + " ago"), ": ", e.text);
    if (!clamp) return body;
    var more = h("button", { class: "more", type: "button", text: "Show all", onclick: function () {
      body.classList.toggle("clamp"); more.textContent = body.classList.contains("clamp") ? "Show all" : "Show less"; } });
    return [body, more];
  }

  function hintChips(p, max) {
    var hs = p.hints || [];
    var shown = hs.filter(function (x) { return x.level !== "info"; });
    var chips = shown.slice(0, max).map(function (x) {
      return h("span", { class: "hint " + x.level, text: x.text });
    });
    if (shown.length > max) chips.push(h("span", { class: "hint info", text: "+" + (shown.length - max) + " more below" }));
    if (!shown.length && p.scan) chips.push(h("span", { class: "hint good", text: "No problems found by the source checks" }));
    var info = hs.filter(function (x) { return x.level === "info"; });
    info.forEach(function (x) { chips.push(h("span", { class: "hint info", text: x.text })); });
    return chips.length ? h("div", { class: "hints" }, chips) : null;
  }

  function quickLinks(p) {
    var ai = p.checks.find(function (c) { return c.name.indexOf("ai-analysis") === 0; });
    return h("div", { class: "links" }, sep([
      a(p.url, "PR discussion"), a(p.url + "/files", "entry diff"),
      a(p.src && (p.src.html || p.src.url), "extension source"),
      ai ? a(ai.url, "AI analysis") : null,
      p.pending_run ? a(p.pending_run, "CI waiting for approval") : null,
    ], ""));
  }

  function description(p) {
    var d = (p.scan && p.scan.cmake && p.scan.cmake.description) || "";
    return d ? h("div", { class: "what", text: d }) : h("div", { class: "what", text: p.title });
  }

  function sourceUpdate(p) {
    if (!p.source_update || !p.src) return null;
    return h("div", { class: "by" }, "Newest source commit ", a(p.src.head_url, (p.src.head_sha || "").slice(0, 7)),
      " (" + fmtDays(daysSince(p.src.head_date)) + (fmtDays(daysSince(p.src.head_date)) === "today" ? "" : " ago") + "): " + (p.src.head_msg || ""),
      p.src.commits_url ? [" · ", a(p.src.commits_url, "history")] : null);
  }

  function ownersText(s) {
    return s.owners.map(function (o) { return (WHY_OTHER[o.why] || "") + " @" + o.login; }).join(", ");
  }

  // --------------------------------------------------------------------------- sections

  function renderTiles(L, data) {
    var tiles = [
      ["Yours", L.mine.length, "mine", "mine"],
      ["New submissions", L.fresh.length, "new", "new"],
      ["Any maintainer", new Set(L.team.map(function (x) { return x.p.number; })).size, "team", ""],
      ["With other maintainers", new Set(L.others.map(function (x) { return x.p.number; })).size, "others", ""],
      ["Waiting on contributors", L.waiting.length, "contributor", ""],
      ["On hold", L.hold.length, "hold", ""],
      ["Open extension PRs", data.prs.length, "all", ""],
    ];
    var box = document.getElementById("tiles");
    box.replaceChildren.apply(box, tiles.map(function (t) {
      return h("button", { class: "tile " + t[3] + (state.filter === t[2] && t[2] !== "all" ? " on" : ""), type: "button",
        onclick: function () { setFilter(state.filter === t[2] ? "all" : t[2]); document.getElementById("all-section").scrollIntoView({ block: "start" }); } },
        h("span", { class: "tv", text: String(t[1]) }), h("span", { class: "tl", text: t[0] }));
    }));
  }

  function renderMine(L) {
    var sec = document.getElementById("mine-section");
    var title = h("h2", { text: state.me ? "For you (@" + state.me + ")" : "For you" });
    if (!state.me) {
      sec.replaceChildren(title, h("div", { class: "panel mine" },
        h("p", { class: "empty", text: "Enter your GitHub login above to see the PRs you have claimed (assigned to you), where your review was requested, or where a contributor answered you." })));
      return;
    }
    if (!L.mine.length) {
      sec.replaceChildren(title, h("div", { class: "panel mine" },
        h("p", { class: "empty", text: "Nothing needs you specifically. To claim a PR, assign yourself on GitHub." })));
      return;
    }
    var items = L.mine.map(function (m) {
      var p = m.p;
      return h("li", { class: "card" },
        prHead(p, h("span", { class: "age", text: "waiting " + fmtDays(daysSince(p.since)) })),
        m.steps.map(function (s) {
          return h("div", null, a(s.step.url, s.step.label, "step"), " ", h("span", { class: "why", text: s.why }));
        }),
        sourceUpdate(p),
        quote(p.turn === "maintainers" && p.reply && !p.source_update ? "Latest from" : "Last ask by",
          p.turn === "maintainers" && p.reply && !p.source_update ? p.reply : p.ask, true),
        quickLinks(p));
    });
    sec.replaceChildren(title, h("div", { class: "panel mine" }, h("ul", { class: "cards" }, items)));
  }

  function renderNew(L) {
    var sec = document.getElementById("new-section");
    var title = h("h2", null, "New submissions ", h("span", { class: "count", text: String(L.fresh.length) }));
    var note = h("p", { class: "empty", text: "Opened in the last " + NEW_DAYS + " days, or not yet answered by any maintainer. Newest first. The checks read the extension's source as text; they are hints, not verdicts." });
    if (!L.fresh.length) { sec.replaceChildren(title, note); return; }
    var items = L.fresh.map(function (p) {
      var answered = p.first_response
        ? h("span", { class: "age", text: "first reply " + fmtDays(daysSince(p.first_response)) + " ago" })
        : h("span", { class: "pill new", text: "no maintainer reply yet" });
      var claimed = p.assignees.length ? h("span", { class: "owner", text: "claimed by @" + p.assignees.join(", @") }) : null;
      var cm = (p.scan && p.scan.cmake) || {};
      var facts = [cm.category || p.entry.category, p.src && p.src.license, p.src && p.src.size_mb !== undefined ? p.src.size_mb + " MB" : null,
        (p.entry.build_dependencies || []).length ? "needs " + p.entry.build_dependencies.join(", ") : null].filter(Boolean).join(" · ");
      return h("li", { class: p.first_response ? "" : "fresh" },
        prHead(p, null),
        h("div", { class: "by" }, "by ", a("https://github.com/" + encodeURIComponent(p.author), "@" + p.author), " · opened " + fmtDays(daysSince(p.created)) + (fmtDays(daysSince(p.created)) === "today" ? "" : " ago") + " · ", answered, claimed ? " · " : null, claimed),
        description(p),
        facts ? h("div", { class: "by", text: facts }) : null,
        hintChips(p, 4),
        quickLinks(p));
    });
    sec.replaceChildren(title, note, h("ul", { class: "cardgrid" }, items));
  }

  function renderIgnore(L) {
    var sec = document.getElementById("ignore-section");
    function group(name, rows, empty) {
      return h("div", { class: "igroup" },
        h("h3", null, name, " ", h("span", { class: "count", text: String(rows.length) })),
        rows.length ? h("ul", null, rows) : h("p", { class: "empty", text: empty }));
    }
    var team = L.team.map(function (x) {
      return h("li", null, h("div", { class: "head" }, a(x.p.url, "#" + x.p.number, "prnum"), h("span", { class: "name", text: x.p.name })),
        h("div", null, a(x.step.url, x.step.label)));
    });
    var others = L.others.filter(function (x) { return !(state.me && x.step.owners.some(function (o) { return same(o.login, state.me); })); }).map(function (x) {
      return h("li", null, h("div", { class: "head" }, a(x.p.url, "#" + x.p.number, "prnum"), h("span", { class: "name", text: x.p.name }),
        h("span", { class: "owner", text: ownersText(x.step) })), h("div", null, a(x.step.url, x.step.label)));
    });
    var waiting = L.waiting.map(function (p) {
      var ask = p.ask ? p.ask.text : p.reason;
      return h("li", null,
        h("div", { class: "head" }, a(p.url, "#" + p.number, "prnum"), h("span", { class: "name", text: p.name }),
          h("span", { class: "by", text: "@" + p.author }), h("span", { class: "age", text: p.draft ? "draft" : "asked " + fmtDays(daysSince(p.since)) + " ago" })),
        p.ask ? h("div", { class: "ctx clamp" }, a(p.ask.url, "@" + p.ask.who), ": " + ask) : h("div", { class: "ctx", text: ask }));
    });
    var hold = L.hold.map(function (p) {
      return h("li", null, h("div", { class: "head" }, a(p.url, "#" + p.number, "prnum"), h("span", { class: "name", text: p.name })),
        p.ask ? h("div", { class: "ctx clamp" }, a(p.ask.url, "@" + p.ask.who), ": " + p.ask.text) : null);
    });
    sec.replaceChildren(
      h("h2", { text: "You can ignore these: the next step belongs to someone else" }),
      h("div", { class: "igroups" },
        group("Any maintainer can pick these up", team, "Nothing unclaimed."),
        group("With other maintainers", others, "Nobody else has a claimed step."),
        group("Waiting on contributors", waiting, "None."),
        L.hold.length ? group("On hold", hold, "") : null));
  }

  function renderChart(data) {
    var sec = document.getElementById("chart-section");
    var rows = data.prs.slice().sort(function (x, y) { return daysSince(y.since) - daysSince(x.since); });
    var NS = "http://www.w3.org/2000/svg";
    function s(tag, attrs, text) {
      var el = document.createElementNS(NS, tag);
      Object.keys(attrs || {}).forEach(function (k) { el.setAttribute(k, attrs[k]); });
      if (text !== undefined) el.textContent = text;
      return el;
    }
    var W = 760, left = 210, right = 24, rowh = 18, top = 30, H = top + rowh * rows.length + 30;
    var lo = 0, hi = Math.log10(2000);
    function x(d) { return left + (Math.log10(Math.max(d, 1)) - lo) / (hi - lo) * (W - left - right); }
    var svg = s("svg", { viewBox: "0 0 " + W + " " + H, class: "chart", role: "img", "aria-label": "Days each PR has been waiting, log scale" });
    [[1, "1d"], [7, "1w"], [30, "1mo"], [90, "3mo"], [365, "1y"], [1000, "~3y"]].forEach(function (g) {
      svg.appendChild(s("line", { x1: x(g[0]), x2: x(g[0]), y1: top - 6, y2: H - 24, class: "grid" }));
      svg.appendChild(s("text", { x: x(g[0]), y: H - 8, class: "axis", "text-anchor": "middle" }, g[1]));
    });
    var b = data.baseline || {};
    [["p50", "median"], ["p75", "75%"], ["p90", "90%"]].forEach(function (r, i) {
      if (!b[r[0]]) return;
      var xv = x(b[r[0]]);
      svg.appendChild(s("line", { x1: xv, x2: xv, y1: top - 12, y2: H - 24, class: "ref " + r[0] }));
      svg.appendChild(s("text", { x: xv + 3, y: i === 1 ? top - 2 : top - 16, class: "reflbl" }, r[1] + " merge " + fmtDays(b[r[0]])));
    });
    rows.forEach(function (p, i) {
      var y = top + i * rowh, d = daysSince(p.since);
      var cls = p.turn === "hold" ? "barh" : !p.first_response && !p.draft ? "barn" : p.turn === "maintainers" ? "barm" : "barc";
      var lbl = s("text", { x: left - 8, y: y + 12, class: "rowlbl", "text-anchor": "end" }, "#" + p.number + " " + (p.name.length > 22 ? p.name.slice(0, 21) + "…" : p.name));
      lbl.addEventListener("click", function () { openRow(p.number); });
      svg.appendChild(lbl);
      var bar = s("rect", { x: left, y: y + 3, width: Math.max(x(d) - left, 2), height: rowh - 7, rx: 2, class: cls });
      bar.appendChild(s("title", {}, "#" + p.number + " " + p.name + ": waiting " + Math.round(d) + " days on " + p.turn));
      svg.appendChild(bar);
    });
    sec.replaceChildren(h("h2", { text: "How long each PR has been waiting" }),
      h("div", { class: "chartbox" }, svg,
        h("div", { class: "legend" },
          h("span", null, h("span", { class: "sw n" }), "new, no maintainer reply"),
          h("span", null, h("span", { class: "sw m" }), "maintainers' turn"),
          h("span", null, h("span", { class: "sw c" }), "contributor's turn"),
          h("span", null, h("span", { class: "sw h" }), "on hold"),
          b.n ? h("span", { text: "Dashed lines: time to merge for " + b.n + " new-extension PRs merged since " + (b.since || "2024").slice(0, 4) + " (median " + fmtDays(b.p50) + ", 75% " + fmtDays(b.p75) + ", 90% " + fmtDays(b.p90) + ")." }) : null)));
  }

  function ciBadges(p) {
    if (!p.checks.length) return [h("span", { class: "ci skip", text: "no checks" })];
    var out = p.checks.map(function (c) {
      var st = c.state;
      var cls = st === "SUCCESS" ? "pass" : /FAILURE|ERROR|TIMED_OUT/.test(st) ? "fail" : /SKIPPED|NEUTRAL|CANCELLED/.test(st) ? "skip" : "run";
      var short = c.name.replace(/^Package (.+) on (\w+)$/, "pkg $2").replace("validate-extensions", "validate")
        .replace("ai-analysis", "ai").replace("find-extensions", "find").replace("pre-commit", "lint");
      return a(c.url, short, "ci " + cls);
    });
    if (!p.validation_ran) out.push(h("span", { class: "ci warnci", text: "validation has not run" }));
    return out;
  }

  function prRow(p) {
    var st = status(p), src = p.src || {}, scan = p.scan || {};
    var det = h("details", { class: "pr", id: "pr-" + p.number });
    var sum = h("summary", null,
      h("span", { class: "stripe " + st[1] }),
      a(p.url, "#" + p.number, "prnum"),
      h("span", { class: "s-name" }, p.name, state.changed.has(p.number) ? h("span", { class: "changed", text: " updated" }) : null, h("span", { class: "by", text: p.author })),
      h("span", { class: "pill " + st[1], text: st[0] }),
      h("span", { class: "s-age", title: "Open since " + fmtDate(p.created), text: fmtDays(daysSince(p.created)) + " open" }),
      h("span", { class: "s-src" }, a(src.html || src.url, "source")));
    det.appendChild(sum);
    det.addEventListener("toggle", function () { if (det.open && !det.dataset.filled) { fill(); } });
    function fill() {
      det.dataset.filled = "1";
      var steps = p.steps.length ? p.steps.map(function (s) {
        return h("li", { class: s.kind }, a(s.url, s.label), s.owners.length ? h("span", { class: "owner", text: "  " + ownersText(s) }) : h("span", { class: "by", text: "  anyone" }));
      }) : [h("li", { class: "by", text: p.turn === "contributor" ? "Nothing for maintainers until the contributor responds." : "No maintainer step." })];
      var chips = ["category", "tier", "scm_revision"].filter(function (k) { return p.entry[k] !== undefined && p.entry[k] !== ""; })
        .map(function (k) { return h("span", { class: "chip" }, h("b", { text: k }), String(p.entry[k])); })
        .concat((p.entry.build_dependencies || []).map(function (d) { return h("span", { class: "chip" }, h("b", { text: "depends" }), d); }));
      var srcItems = [];
      if (src.html || src.url) srcItems.push(h("li", null, "Repository: ", a(src.html || src.url, src.full_name || src.url), src.archived ? " (archived)" : ""));
      if (src.head_sha) srcItems.push(h("li", null, "Tracked revision ", h("code", { text: src.rev || "" }), " is at ", a(src.head_url, src.head_sha.slice(0, 7)),
        " (" + fmtDate(src.head_date) + "): " + (src.head_msg || ""), src.commits_url ? [" · ", a(src.commits_url, "history")] : null));
      if (src.host === "github") srcItems.push(h("li", { text: "License: " + (src.license || "none detected by GitHub") + " · size " + src.size_mb + " MB · last push " + fmtDate(src.pushed_at) }));
      var cm = scan.cmake || {};
      if (cm.contributors) srcItems.push(h("li", { text: "Contributors: " + cm.contributors }));
      if (cm.homepage) srcItems.push(h("li", null, "Homepage: ", a(cm.homepage, cm.homepage)));
      (scan.modules || []).forEach(function (m) {
        srcItems.push(h("li", { text: "Module " + m.name + " (" + m.dir + "/): " + (m.glob ? "Python files picked up by a glob or variable" : m.listed + " Python file(s) listed" + (m.unlisted ? ", " + m.unlisted.length + " not listed" : "") + (m.missing ? ", " + m.missing.length + " missing" : "")) }));
      });
      if (scan.pip && scan.pip.length) srcItems.push(h("li", null, "Install specs: ", sep(scan.pip.map(function (x) {
        return a(src.html ? src.html + "/blob/" + scan.sha + "/" + x.path : null, x.spec);
      }), ", ")));
      var hints = (p.hints || []).map(function (x) {
        var link = x.path && src.html ? a(src.html + "/blob/" + (scan.sha || src.head_sha) + "/" + x.path, " (file)") : null;
        return h("li", { class: x.level }, x.text, link);
      });
      var timeline = (p.events || []).slice().reverse().map(function (e) {
        return h("li", null, h("span", { class: "tl-date", text: fmtDate(e.t) }), h("span", { class: "tl-who " + e.role, text: e.who }), " ",
          a(e.url, e.kind), " ", h("span", { class: "tl-text", text: (e.ai ? "[AI-assisted] " : "") + (e.text || "").replace(/\s+/g, " ").slice(0, 240) }));
      });
      det.appendChild(h("div", { class: "body" },
        description(p),
        h("div", { class: "cols" },
          h("div", { class: "col" },
            h("h4", { text: "Next steps" }), h("ul", { class: "steps" }, steps),
            p.ask ? [h("h4", { text: "Latest maintainer comment" }), quote("", p.ask, true)] : null,
            p.reply ? [h("h4", { text: "Latest from the contributor" }), quote("", p.reply, true)] : null,
            h("h4", { text: "Catalog entry" }), h("div", { class: "chips" }, chips.length ? chips : h("span", { class: "chip", text: "no fields parsed" })),
            p.entry_text ? h("details", null, h("summary", { class: "by", text: "Entry JSON" }), h("pre", { class: "entry", text: p.entry_text })) : null,
            h("h4", { text: "CI" }), h("div", { class: "cirow" }, ciBadges(p))),
          h("div", { class: "col" },
            h("h4", { text: "Source checks" }), hints.length ? h("ul", { class: "hintlist" }, hints) : h("p", { class: "by", text: p.scan ? "No problems found." : "Not scanned (only GitHub sources are scanned)." }),
            h("h4", { text: "Extension source" }), h("ul", null, srcItems),
            h("h4", { text: "Recent activity" }), h("ul", { class: "timeline" }, timeline.length ? timeline : h("li", { text: "No activity yet." })))),
        h("p", { class: "meta", text: "Opened " + fmtDate(p.created) + " · " + p.reason + " · labels: " + (p.labels.join(", ") || "none") +
          (p.assignees.length ? " · assigned: " + p.assignees.join(", ") : "") + (p.draft ? " · draft" : "") })));
    }
    sum.querySelectorAll("a").forEach(function (x) { x.addEventListener("click", function (e) { e.stopPropagation(); }); });
    det.dataset.search = (p.name + " " + p.title + " " + p.author + " " + p.number).toLowerCase();
    det.dataset.number = p.number;
    return det;
  }

  var FILTERS = [["all", "All"], ["mine", "Yours"], ["new", "New"], ["maintainers", "Maintainers' turn"], ["contributor", "Contributor's turn"],
    ["ready", "Ready to merge"], ["hold", "On hold"], ["expand", "Expand all"]];

  function matches(p, f, L) {
    switch (f) {
      case "all": return true;
      case "mine": return L.mine.some(function (m) { return m.p.number === p.number; });
      case "new": return isNew(p);
      case "team": return L.team.some(function (x) { return x.p.number === p.number; });
      case "others": return L.others.some(function (x) { return x.p.number === p.number; });
      case "ready": return p.steps.some(function (s) { return s.kind === "merge"; });
      default: return p.turn === f;
    }
  }

  function renderList(data, L) {
    var list = document.getElementById("list");
    var open = new Set(Array.prototype.map.call(list.querySelectorAll("details.pr[open]"), function (d) { return d.id; }));
    var rows = data.prs.slice().sort(function (x, y) { return y.number - x.number; }).map(prRow);
    list.replaceChildren.apply(list, rows);
    rows.forEach(function (r) { if (open.has(r.id)) r.open = true; });
    var fb = document.getElementById("filters");
    fb.replaceChildren.apply(fb, FILTERS.map(function (f) {
      return h("button", { class: "fbtn" + (state.filter === f[0] ? " on" : ""), type: "button", text: f[1], onclick: function () {
        if (f[0] === "expand") {
          var vis = rows.filter(function (r) { return !r.hidden; });
          var openAll = !vis.every(function (r) { return r.open; });
          vis.forEach(function (r) { r.open = openAll; });
          return;
        }
        setFilter(state.filter === f[0] ? "all" : f[0]);
      } });
    }));
    applyFilter(L);
  }

  function applyFilter(L) {
    var term = state.term.toLowerCase().trim();
    var byNum = {};
    state.data.prs.forEach(function (p) { byNum[p.number] = p; });
    document.querySelectorAll("#list details.pr").forEach(function (r) {
      var p = byNum[r.dataset.number];
      r.hidden = !(matches(p, state.filter, L) && (!term || r.dataset.search.indexOf(term) >= 0));
    });
  }

  function setFilter(f) { state.filter = f; render(); }

  function openRow(n) {
    var r = document.getElementById("pr-" + n);
    if (!r) return;
    r.hidden = false; r.open = true; r.scrollIntoView({ block: "center" });
  }

  function renderInfra(data) {
    var sec = document.getElementById("infra-section");
    sec.replaceChildren(h("h2", { text: "Other open PRs (index infrastructure)" }),
      data.infra.length ? h("ul", null, data.infra.map(function (p) {
        return h("li", null, a(p.url, "#" + p.number, "prnum"), " " + p.title + " ", h("span", { class: "by", text: p.author + " · " + fmtDays(daysSince(p.created)) + " open" }));
      })) : h("p", { class: "empty", text: "None." }));
  }

  function renderFoot(data) {
    var repo = data.repo;
    document.getElementById("foot").replaceChildren(
      h("p", null, "Built from the GitHub API for ", a("https://github.com/" + repo + "/pulls", repo),
        data.run_url ? [" by ", a(data.run_url, "this workflow run")] : null,
        ". Whose turn: the contributor's latest comment or push (including pushes to the extension's own repository) against the latest comment or review by a repository member or collaborator. A PR is yours when it is assigned to you, your review was requested, or the contributor answered you."),
      h("p", { text: "The source checks read CMake and Python files as text and never run them; treat them as pointers for a human review. Made with Claude (Anthropic's AI assistant) under @pieper's direction." }));
  }

  // --------------------------------------------------------------------------- live status

  function renderLive() {
    var el = document.getElementById("live");
    var d = state.data;
    if (!d) return;
    var gen = Date.parse(d.generated_at), mins = (Date.now() - gen) / 60000;
    var cls = state.pollError ? "down" : mins > STALE_MIN ? "stale" : "ok";
    var text = (state.pollError ? "Cannot reach the server · " : mins > STALE_MIN ? "Updates may be paused · " : "Live · ") +
      "checked GitHub " + minutesAgo(d.generated_at) + " · last change " + minutesAgo(d.data_changed_at || d.generated_at);
    el.className = "live " + cls;
    el.textContent = text;
    el.title = "Generated " + new Date(gen).toLocaleString() + ". This page checks for a new snapshot every " + (POLL_MS / 1000) + " seconds.";
  }

  function render() {
    var d = state.data;
    if (!d) return;
    var L = lanes(d, state.me);
    document.getElementById("repo-link").replaceChildren(a("https://github.com/" + d.repo + "/pulls", d.repo));
    renderTiles(L, d);
    renderMine(L);
    renderNew(L);
    renderIgnore(L);
    renderChart(d);
    renderList(d, L);
    renderInfra(d);
    renderFoot(d);
    renderLive();
  }

  // --------------------------------------------------------------------------- data loading

  function getJSON(url) {
    return fetch(url, { cache: "no-cache" }).then(function (r) {
      if (!r.ok) throw new Error(url + ": HTTP " + r.status);
      return r.json();
    });
  }

  function loadData() {
    return getJSON("data.json").then(function (d) {
      var old = state.data;
      state.changed = new Set();
      if (old) {
        var before = {};
        old.prs.forEach(function (p) { before[p.number] = p.updated + "|" + p.turn + "|" + p.steps.length + "|" + (p.src && p.src.head_sha); });
        d.prs.forEach(function (p) {
          if (before[p.number] !== p.updated + "|" + p.turn + "|" + p.steps.length + "|" + (p.src && p.src.head_sha)) state.changed.add(p.number);
        });
      }
      state.data = d;
      if (!state.me && d.default_me) setMe(d.default_me, false);
      var dl = document.getElementById("maintainer-list");
      dl.replaceChildren.apply(dl, (d.maintainers || []).map(function (m) { return h("option", { value: m }); }));
      render();
      state.changed.forEach(function (n) {
        var row = document.getElementById("pr-" + n);
        if (row) row.classList.add("flash");
      });
    });
  }

  function poll() {
    getJSON("version.json").then(function (v) {
      state.pollError = null;
      if (!state.data || v.data_hash !== state.data.data_hash) return loadData();
      state.data.generated_at = v.generated_at;
      renderLive();
    }).catch(function (e) {
      state.pollError = e;
      renderLive();
    }).then(function () {
      clearTimeout(state.timer);
      state.timer = setTimeout(poll, POLL_MS);
    });
  }

  function setMe(login, save) {
    state.me = (login || "").replace(/^@/, "").trim();
    var input = document.getElementById("me");
    if (input.value !== state.me) input.value = state.me;
    input.classList.toggle("unset", !state.me);
    if (save) store("triage.me", state.me);
  }

  function init() {
    var params = new URLSearchParams(location.search);
    setMe(params.get("me") || load("triage.me"), !!params.get("me"));
    var input = document.getElementById("me");
    input.addEventListener("change", function () { setMe(input.value, true); render(); });
    document.getElementById("q").addEventListener("input", function (e) {
      state.term = e.target.value;
      if (state.data) applyFilter(lanes(state.data, state.me));
    });
    document.addEventListener("visibilitychange", function () { if (document.visibilityState === "visible") poll(); });
    setInterval(renderLive, 60000);
    loadData().catch(function (e) {
      document.getElementById("live").textContent = "Could not load data.json (" + e.message + ").";
      document.getElementById("live").className = "live down";
    }).then(function () {
      var m = location.hash.match(/^#pr-(\d+)$/);
      var target = params.get("pr") || (m && m[1]);
      if (target) openRow(target);
      state.timer = setTimeout(poll, POLL_MS);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
