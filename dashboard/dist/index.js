/**
 * Jermes: dashboard tab for the Jev decision layer.
 *
 * Plain IIFE, no build step. React and UI primitives come from
 * window.__HERMES_PLUGIN_SDK__; every request goes through SDK.fetchJSON to
 * this plugin's own routes (/api/plugins/jermes/...), which sit behind the
 * dashboard login.
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  var React = SDK.React;
  var h = React.createElement;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var useCallback = SDK.hooks.useCallback;
  var C = SDK.components;
  var BASE = "/api/plugins/jermes";
  var MODES = ["off", "shadow", "advise", "enforce"];

  function get(path) { return SDK.fetchJSON(BASE + path); }
  function post(path, body) {
    return SDK.fetchJSON(BASE + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
  }
  function ago(ts) {
    if (!ts) return "never";
    return SDK.utils && SDK.utils.timeAgo ? SDK.utils.timeAgo(ts) : new Date(ts * 1000).toLocaleString();
  }
  function usd(x) {
    if (!x) return "$0";
    return x < 0.01 ? "<$0.01" : "$" + x.toFixed(2);
  }
  function card(title, subtitle, body) {
    return h(C.Card, null,
      h(C.CardHeader, null,
        h(C.CardTitle, null, title),
        subtitle ? h("div", { className: "jermes-muted" }, subtitle) : null),
      h(C.CardContent, null, body));
  }

  // ------------------------------------------------------------ status + modes

  function StatusCard(props) {
    var s = props.status;
    if (!s) return card("Status", null, h("div", { className: "jermes-muted" }, "Loading..."));
    return card("Status", "Connection to Jev and where the settings live", h("div", null,
      h("div", { className: "jermes-kv" }, h("span", null, "Provider"), h("span", null, s.backend + " (" + (s.model || "") + ")")),
      h("div", { className: "jermes-kv" }, h("span", null, "API key"),
        h("span", null, s.key_present ? h(C.Badge, null, s.key_env + " set") :
          h("span", { className: "jermes-err" }, s.key_env + " missing"))),
      h("div", { className: "jermes-kv" }, h("span", null, "Last Jev call"), h("span", null, ago(s.last_jev_call))),
      h("div", { className: "jermes-kv" }, h("span", null, "Config file"), h("span", { className: "jermes-muted" }, s.config_path)),
      s.forced_mode ? h("div", { className: "jermes-note" },
        "JERMES_MODE=" + s.forced_mode + " is set in the environment and overrides every switch below.") : null));
  }

  function ModesCard(props) {
    var s = props.status;
    var counts = props.counts || {};
    var st = useState(null), busy = st[0], setBusy = st[1];
    var ms = useState(""), msg = ms[0], setMsg = ms[1];

    function change(p, mode) {
      var point = p.point;
      if (mode === "enforce" && !window.confirm(
        "Switch " + p.label + " to ENFORCE?\n\n" + (p.enforce_effect || "Jermes will act on its decisions.") +
        "\n\nYou can switch it back here at any time.")) return;
      setBusy(point); setMsg("");
      post("/mode", { point: point, mode: mode, confirm: mode === "enforce" }).then(function (r) {
        setMsg((r.previous || "?") + " \u2192 " + r.mode + " for " + point + ". " + r.applies);
        props.reload();
      }).catch(function (e) { setMsg("Could not change " + point + ": " + (e && e.message ? e.message : e)); })
        .finally(function () { setBusy(null); });
    }

    if (!s) return card("Features", null, h("div", { className: "jermes-muted" }, "Loading..."));
    return card("Features", "off: not running. shadow: logs what it would do, changes nothing. advise (where offered): adds notes, suggestions or one-time holds, never blocks. enforce: acts on its decisions (asks you to confirm first).",
      h("div", null,
        s.points.map(function (p) {
          var c = counts[p.point];
          var line = c ? (c.checked + " checked in 24h" + (c.errors ? ", " + c.errors + " errors" : "")) : "no decisions in 24h";
          return h("div", { key: p.point, className: "jermes-point" },
            h("div", { className: "jermes-point-text" },
              h("div", { className: "jermes-point-name" }, p.label),
              h("div", { className: "jermes-muted" }, p.description),
              h("div", { className: "jermes-muted" }, line)),
            p.settable ?
              h("div", { className: "jermes-seg", role: "group", "aria-label": p.label + " mode" },
                (p.modes || MODES).map(function (m) {
                  return h("button", {
                    key: m, type: "button", className: (p.mode === m ? "on" : "") + (m === "enforce" ? " enf" : ""),
                    disabled: busy === p.point || p.mode === m || !!s.forced_mode,
                    onClick: function () { change(p, m); },
                  }, m);
                })) :
              h(C.Badge, null, p.mode + " (set in config)"));
        }),
        msg ? h("div", { className: "jermes-note", style: { marginTop: "0.75rem" } }, msg) : null));
  }

  // ------------------------------------------------------------ cost

  function CostCard() {
    var st = useState(null), cost = st[0], setCost = st[1];
    var ds = useState(30), days = ds[0], setDays = ds[1];
    useEffect(function () { get("/cost?days=" + days).then(setCost).catch(function () { setCost(null); }); }, [days]);
    if (!cost) return card("Jev cost", null, h("div", { className: "jermes-muted" }, "Loading..."));
    var max = Math.max.apply(null, cost.days.map(function (d) { return d.usd; }).concat([0.000001]));
    return card("Jev cost", cost.note, h("div", null,
      h("div", { className: "jermes-kv" }, h("span", null, "Today"), h("span", null, usd(cost.today_usd))),
      h("div", { className: "jermes-kv" }, h("span", null, "Last 7 days"), h("span", null, usd(cost.week_usd))),
      h("div", { className: "jermes-kv" }, h("span", null, "All time"),
        h("span", null, usd(cost.all_time_usd) + " over " + cost.all_time_requests + " requests")),
      h("div", { className: "jermes-chart", title: "Jev spend per day" },
        cost.days.map(function (d) {
          return h("div", {
            key: d.day, className: "jermes-bar" + (d.usd ? "" : " zero"),
            style: { height: Math.max(1, Math.round(100 * d.usd / max)) + "%" },
            title: d.day + ": " + usd(d.usd) + ", " + d.requests + " requests",
          });
        })),
      h("div", { className: "jermes-filters", style: { marginTop: "0.5rem" } },
        h("span", { className: "jermes-muted" }, "Range"),
        h("select", { value: days, onChange: function (e) { setDays(parseInt(e.target.value, 10)); } },
          [7, 30, 90].map(function (n) { return h("option", { key: n, value: n }, n + " days"); })))));
  }

  // ------------------------------------------------------------ decisions

  function DecisionsCard(props) {
    var pt = useState(""), point = pt[0], setPoint = pt[1];
    var fl = useState(true), flagged = fl[0], setFlagged = fl[1];
    var rs = useState(null), rows = rs[0], setRows = rs[1];
    var es = useState(""), err = es[0], setErr = es[1];
    var load = useCallback(function () {
      var q = "/decisions?limit=150" + (point ? "&point=" + encodeURIComponent(point) : "") + (flagged ? "&flagged=true" : "");
      get(q).then(function (r) { setRows(r.rows); setErr(""); }).catch(function (e) { setErr(String(e && e.message || e)); });
    }, [point, flagged]);
    useEffect(load, [load, props.tick]);
    var points = (props.status && props.status.points) || [];
    return card("Shadow decisions", "What Jermes did or would have done, newest first. Previews are redacted.",
      h("div", null,
        h("div", { className: "jermes-filters" },
          h("select", { value: point, onChange: function (e) { setPoint(e.target.value); } },
            h("option", { value: "" }, "All features"),
            points.map(function (p) { return h("option", { key: p.point, value: p.point }, p.label); })),
          h("select", { value: flagged ? "flagged" : "all", onChange: function (e) { setFlagged(e.target.value === "flagged"); } },
            h("option", { value: "flagged" }, "Only would-act (block, ask, trim, hold)"),
            h("option", { value: "all" }, "Everything")),
          h(C.Button, { size: "sm", variant: "outline", onClick: load }, "Refresh")),
        err ? h("div", { className: "jermes-err" }, err) : null,
        !rows ? h("div", { className: "jermes-muted" }, "Loading...") :
          rows.length === 0 ? h("div", { className: "jermes-muted" }, "Nothing logged for this filter yet.") :
            h("div", { className: "jermes-scroll" },
              h("table", { className: "jermes-table" },
                h("thead", null, h("tr", null,
                  ["When", "Feature", "Decision", "Why", "Call"].map(function (x) { return h("th", { key: x }, x); }))),
                h("tbody", null, rows.map(function (r) {
                  var warn = r.action === "block" || r.action === "hold";
                  return h("tr", { key: r.id },
                    h("td", null, ago(r.ts)),
                    h("td", null, r.point.replace(/_/g, " ")),
                    h("td", null, h("span", { className: "jermes-tag" + (warn ? " warn" : "") },
                      r.error ? "error" : (r.mode === "shadow" ? r.label : r.action + (r.applied ? " (applied)" : "")))),
                    h("td", null, r.error ? h("span", { className: "jermes-err" }, r.error) : (r.reason || r.summary || "")),
                    h("td", { className: "pre" }, (r.tool ? r.tool + ": " : "") + (r.preview || "")));
                }))))));
  }

  // ------------------------------------------------------------ readiness

  function TrimCard(props) {
    var st = useState(null), r = st[0], setR = st[1];
    useEffect(function () { get("/trimreport?days=14").then(setR).catch(function () { setR({ items: 0 }); }); }, [props.tick]);
    if (!r) return card("Context trimming: ready for enforce?", null, h("div", { className: "jermes-muted" }, "Loading..."));
    if (!r.items) return card("Context trimming: ready for enforce?", "Last 14 days",
      h("div", { className: "jermes-muted" }, r.error || r.note ||
        "No trimming decisions yet. They appear after a pause of 5+ minutes in a session that uses the jermes context engine."));
    var o = r.outcomes || {};
    var needed = r.needed || [];
    return card("Context trimming: ready for enforce?", "Last 14 days: of the items trimming dropped, did the agent need them again?",
      h("div", null,
        h("div", { className: "jermes-kv" }, h("span", null, "Items dropped"), h("span", null, r.items + " in " + r.sessions + " sessions")),
        h("div", { className: "jermes-kv" }, h("span", null, "Needed again"),
          h("span", null, r.needed_rate == null ? "-" : Math.round(r.needed_rate * 100) + "%")),
        h("div", { className: "jermes-kv" }, h("span", null, "Fully re-read or re-run"),
          h("span", null, String((o["re-read"] || 0) + (o["re-ran"] || 0) + (o["reloaded"] || 0) + (o["full copy opened"] || 0)))),
        h("div", { className: "jermes-kv" }, h("span", null, "Recovered by a search or partial read"), h("span", null, String(o.searched || 0))),
        h("div", { className: "jermes-kv" }, h("span", null, "Characters dropped / brought back"),
          h("span", null, (r.chars_dropped || 0).toLocaleString() + " / " + (r.chars_brought_back || 0).toLocaleString())),
        needed.length ? h("div", { className: "jermes-scroll", style: { marginTop: "0.5rem" } },
          h("table", { className: "jermes-table" },
            h("thead", null, h("tr", null, h("th", null, "Dropped item"), h("th", null, "How it came back"))),
            h("tbody", null, needed.map(function (n, i) {
              return h("tr", { key: i }, h("td", { className: "pre" }, n.tool + " " + n.call),
                h("td", null, n.outcome + (n.evidence ? ": " + n.evidence : "")));
            })))) : null,
        h("div", { className: "jermes-muted", style: { marginTop: "0.5rem" } },
          "Searches that recover a few lines are trimming working as designed. Frequent full re-reads mean it is too aggressive.")));
  }

  function GuardCard(props) {
    var st = useState(null), rows = st[0], setRows = st[1];
    useEffect(function () {
      Promise.all([get("/decisions?point=risk_gate&action=block&limit=50"),
                   get("/decisions?point=memory_filter&action=hold&limit=30"),
                   get("/decisions?point=skill_overlap&limit=30&flagged=true")])
        .then(function (rs) { setRows(rs[0].rows.concat(rs[1].rows, rs[2].rows)); })
        .catch(function () { setRows([]); });
    }, [props.tick]);
    var c = props.counts || {};
    var rg = c.risk_gate || {};
    return card("Guards: ready for advise or enforce?",
      "The number that matters: how often a guard would have stopped something you actually asked for.",
      h("div", null,
        h("div", { className: "jermes-kv" }, h("span", null, "Risk gate, last 24h"),
          h("span", null, (rg.checked || 0) + " checked, " + (rg.review || 0) + " would ask approval, " + (rg.block || 0) + " would block")),
        !rows ? h("div", { className: "jermes-muted" }, "Loading...") :
          rows.length === 0 ? h("div", { className: "jermes-muted" }, "No would-block, would-hold or overlap flags logged yet.") :
            h("div", { className: "jermes-scroll" },
              h("table", { className: "jermes-table" },
                h("thead", null, h("tr", null, ["When", "Feature", "Why", "Call"].map(function (x) { return h("th", { key: x }, x); }))),
                h("tbody", null, rows.map(function (r) {
                  return h("tr", { key: r.point + r.id },
                    h("td", null, ago(r.ts)), h("td", null, r.point.replace(/_/g, " ")),
                    h("td", null, r.reason || r.summary), h("td", { className: "pre" }, (r.tool ? r.tool + ": " : "") + r.preview));
                }))))));
  }

  // ------------------------------------------------------------ skills audit

  function AuditCard() {
    var st = useState(null), a = st[0], setA = st[1];
    var es = useState(""), err = es[0], setErr = es[1];
    var load = useCallback(function () { get("/skills-audit").then(setA).catch(function (e) { setErr(String(e && e.message || e)); }); }, []);
    useEffect(load, [load]);
    useEffect(function () {
      if (!a || a.state !== "running") return undefined;
      var t = setInterval(load, 4000);
      return function () { clearInterval(t); };
    }, [a && a.state, load]);
    function start() {
      if (!window.confirm("Run a duplicate-skill audit? About 6 minutes and $0.04 of Jev for ~100 skills. It only suggests; nothing is changed.")) return;
      setErr("");
      post("/skills-audit").then(load).catch(function (e) { setErr(String(e && e.message || e)); });
    }
    var rep = a && a.report;
    return card("Duplicate-skill audit", "Finds skills that do the same job and suggests which to keep. Suggestions only.",
      h("div", null,
        h("div", { className: "jermes-filters" },
          h(C.Button, { size: "sm", onClick: start, disabled: a && a.state === "running" },
            a && a.state === "running" ? "Running..." : "Run audit (~$0.04)"),
          a && a.state === "running" ? h("span", { className: "jermes-muted" }, a.progress || "") : null,
          a && a.finished_at ? h("span", { className: "jermes-muted" }, "Last run " + ago(a.finished_at)) : null),
        err ? h("div", { className: "jermes-err" }, err) : null,
        a && a.state === "error" ? h("div", { className: "jermes-err" }, a.error) : null,
        rep ? h("div", null,
          h("div", { className: "jermes-muted" }, rep.skills_checked + " skills checked, " + rep.groups.length + " overlap group(s)" +
            (rep.errors ? ", " + rep.errors + " errors" : "")),
          rep.groups.map(function (g, i) {
            return h("div", { key: i, className: "jermes-note", style: { marginTop: "0.5rem" } },
              h("div", { className: "jermes-point-name" }, g.members.join(", ")),
              (g.pairs || []).map(function (p, j) {
                return h("div", { key: j, className: "jermes-muted" },
                  p.a + " / " + p.b + ": " + p.relation.replace(/_/g, " ") + ", same job " + Math.round(p.same_job * 100) + "%");
              }),
              h("div", { style: { marginTop: "0.3rem" } }, g.suggestion));
          })) : null));
  }

  // ------------------------------------------------------------ page

  function JermesPage() {
    var ss = useState(null), status = ss[0], setStatus = ss[1];
    var cs = useState({}), counts = cs[0], setCounts = cs[1];
    var ts = useState(0), tick = ts[0], setTick = ts[1];
    var es = useState(""), err = es[0], setErr = es[1];
    var reload = useCallback(function () {
      get("/status").then(function (s) { setStatus(s); setErr(""); })
        .catch(function (e) { setErr("Jermes API not reachable: " + (e && e.message ? e.message : e)); });
      get("/counts?hours=24").then(function (c) { setCounts(c.points || {}); }).catch(function () {});
      setTick(function (t) { return t + 1; });
    }, []);
    useEffect(reload, [reload]);
    useEffect(function () { var t = setInterval(reload, 60000); return function () { clearInterval(t); }; }, [reload]);
    return h("div", { className: "jermes-page" },
      err ? h("div", { className: "jermes-note jermes-err" }, err) : null,
      h("div", { className: "jermes-grid" },
        h(StatusCard, { status: status }),
        h(CostCard, null)),
      h(ModesCard, { status: status, counts: counts, reload: reload }),
      h("div", { className: "jermes-grid" },
        h(TrimCard, { tick: tick }),
        h(GuardCard, { tick: tick, counts: counts })),
      h(DecisionsCard, { status: status, tick: tick }),
      h(AuditCard, null));
  }

  window.__HERMES_PLUGINS__.register("jermes", JermesPage);
})();
