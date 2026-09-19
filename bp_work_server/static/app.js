const state = {
  lastEventId: 0,
  refreshTimer: null,
  githubTimer: null,
  eventSource: null,
  dashboardInFlight: false,
  githubInFlight: false,
  treeCollapsed: {}, // path -> bool, remembers folder state across refreshes
  zipCollapsed: {}, // same, for the build-contents preview tree
  latestBuild: null, // most recent published build (for the download button + preview)
  actorProfiles: {},
  repo: { owner: "BurnoutDecomp", name: "b5-decomp", ref: "dev" },
  explorer: {
    tab: "tus",
    q: "",
    status: "",
    source: "",
    goal: "",
    sort: "id",
    order: "asc",
    limit: 50,
    offset: 0,
    total: 0,
    items: [],
    searchTimer: null,
    requestId: 0,
  },
  // the Console Evidence section: its own tab, filters and paging
  evidence: {
    tab: "audit",
    q: "",
    category: "",
    tier: "",
    live: false,
    asmTier: "",
    asmFlagged: false,
    offset: 0,
    limit: 25,
    total: 0,
    items: [],
    summary: null,
    expanded: {},
    searchTimer: null,
    requestId: 0,
  },
  // Client-side mini-explorers for the Live Events and Next Queue panels:
  // the dashboard payload carries the full lists; we filter/search/page here.
  eventsView: { all: [], q: "", action: "", actor: "", page: 1, perPage: 50, searchTimer: null },
  queueView: { all: [], q: "", source: "", page: 1, perPage: 50, searchTimer: null },
  blockedView: { all: [], q: "", source: "", page: 1, perPage: 50, searchTimer: null },
  // Live Events reconstruction. Backfilled rows share one import timestamp, one
  // bogus commit, and a guessed author; we replace each with one event per real
  // commit (2026+) that touched the file. rawEvents is the unexpanded payload;
  // eventHistory maps TU id -> [{date, author}, ...].
  rawEvents: [],
  eventHistory: {},
  eventHistoryFetched: false,
  detailNav: { current: null, stack: [] },
};

/* Build a github.com/blob URL for a path inside the mirrored repo. */
function ghBlobUrl(path) {
  if (!path) return null;
  const { owner, name, ref } = state.repo;
  return `https://github.com/${owner}/${name}/blob/${ref}/${path}`;
}

/* dest_path looks like "b5-decomp/src/...": strip the repo prefix for blob links. */
function destToRepoPath(dest) {
  if (!dest) return null;
  const prefix = `${state.repo.name}/`;
  return dest.startsWith(prefix) ? dest.slice(prefix.length) : dest;
}

const el = (id) => document.getElementById(id);
const RING_CIRCUMFERENCE = 326.7;

function text(id, value) {
  const node = el(id);
  if (node) node.textContent = value;
}

function pct(value) {
  return `${Number(value || 0).toFixed(2)}%`;
}

function fmtInt(value) {
  return Number(value || 0).toLocaleString();
}

// Compact count for badges: 950 -> "950", 1300 -> "1.3k", 2_000_000 -> "2M".
function fmtCompact(value) {
  const n = Number(value || 0);
  if (n < 1000) return String(n);
  return n
    .toLocaleString("en", { notation: "compact", maximumFractionDigits: 1 })
    .replace("K", "k");
}

function fmtTime(value) {
  if (!value) return "none";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function shortTime(value) {
  if (!value) return "none";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

// Label for the Live Events "Time" column. All event sources use the same
// compact label; the full date remains available in the title.
function eventTimeLabel(event) {
  const value = event && event.ts;
  if (!value) return "none";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return shortTime(value);
}

function relTime(value) {
  if (!value) return "";
  const then = new Date(value).getTime();
  if (Number.isNaN(then)) return "";
  const diff = Math.max(0, Date.now() - then);
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(value).toLocaleDateString();
}

function fmtBytes(n) {
  if (n == null) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function setConnection(mode, label) {
  const node = el("connection");
  node.classList.remove("online", "offline");
  node.classList.add(mode);
  text("connectionText", label);
}

function setRing(id, value) {
  const node = el(id);
  if (!node) return;
  const percent = Math.max(0, Math.min(100, Number(value || 0)));
  node.style.setProperty("--p", percent);
  const fill = node.querySelector(".ring-fill");
  if (fill) {
    fill.style.strokeDashoffset = String(
      RING_CIRCUMFERENCE - (RING_CIRCUMFERENCE * percent) / 100,
    );
  }
}

function clearNode(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

async function fetchJson(url, timeoutMs = 15000) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { cache: "no-store", signal: controller.signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json();
  } catch (error) {
    if (error.name === "AbortError") throw new Error("request timed out");
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

function div(className, content) {
  const node = document.createElement("div");
  node.className = className;
  if (content !== undefined) node.textContent = content;
  return node;
}

function span(className, content) {
  const node = document.createElement("span");
  node.className = className;
  node.textContent = content;
  return node;
}

function actorNode(name, githubUsername) {
  if (!name) return span("muted-text", "none");
  const profile = githubUsername || state.actorProfiles[name] || state.actorProfiles[String(name).trim()];
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "text-link actor-link";
  btn.textContent = name;
  btn.title = profile ? `Open ${name}'s contribution profile` : `Open ${name}'s profile`;
  btn.addEventListener("click", (event) => {
    event.stopPropagation();
    openProfile(name, profile);
  });
  return btn;
}

function attributionMeta(item, fallback = "primary contributor") {
  if (item.primary_contributor) {
    const parts = [fallback];
    if (item.primary_contributor_lines != null) {
      parts.push(`${fmtInt(item.primary_contributor_lines)} surviving lines`);
    }
    if (Number(item.contributor_count || 0) > 1) {
      parts.push(`+${fmtInt(Number(item.contributor_count) - 1)}`);
    }
    return parts.join(" · ");
  }
  if (item.completed_at) return relTime(item.completed_at) || fmtTime(item.completed_at);
  return "completed";
}

function compactAttributionMeta(item) {
  if (Number(item.contributor_count || 0) > 1) {
    return `+${fmtInt(Number(item.contributor_count) - 1)}`;
  }
  return "";
}

function appendAttribution(cell, item, emptyText = "unattributed") {
  if (item.primary_contributor) {
    cell.appendChild(actorNode(item.primary_contributor, item.primary_contributor_login));
    const meta = compactAttributionMeta(item);
    if (meta) cell.appendChild(div("tu-meta", meta));
  } else if (item.completed_by) {
    cell.appendChild(actorNode(item.completed_by, item.completed_by_login));
    cell.appendChild(div("tu-meta", item.completed_at ? (relTime(item.completed_at) || fmtTime(item.completed_at)) : "completed"));
  } else {
    cell.textContent = emptyText;
  }
}

function tuButton(tuId, className = "tu-name") {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = `text-link ${className}`;
  btn.textContent = tuId;
  btn.addEventListener("click", (event) => {
    event.stopPropagation();
    openDetail(tuId);
  });
  return btn;
}

function detailText(detail) {
  if (!detail || !Object.keys(detail).length) return "";
  if (detail.reconstructed || detail.source === "b5-decomp commit reconstruction") {
    return "reconstructed from b5-decomp";
  }
  return Object.entries(detail)
    // The stored commit SHA is the bogus backfill one; never surface it.
    .filter(([key]) => key !== "commit")
    .filter(([, value]) => value !== null && value !== "" && value !== undefined)
    .map(([key, value]) => `${key}: ${Array.isArray(value) ? value.join(", ") : value}`)
    .join(" | ");
}

/* ---------------- Work dashboard ---------------- */

async function refresh() {
  if (state.dashboardInFlight) return;
  state.dashboardInFlight = true;
  try {
    // 45 s: a fresh process builds its first state in seconds on a quiet box and
    // ~15 s on a loaded one (it is warmed at startup, but a visitor can beat it).
    render(await fetchJson("/dashboard/state", 45000));
    setConnection("online", "Live");
  } catch (error) {
    setConnection("offline", "Disconnected");
    text("subtitle", `Dashboard update failed: ${error.message}`);
  } finally {
    state.dashboardInFlight = false;
  }
}

function render(data) {
  state.actorProfiles = data.actor_profiles || {};
  state.attributionCache = data.attribution_cache || {};
  state.attributionWarming = Boolean(data.attribution_cache_warming);
  state.decompRepo = data.decomp_repo || {};
  const totals = data.totals || {};
  const counts = data.counts || {};
  text("subtitle", `${fmtInt(totals.tus)} translation units · ${fmtInt(totals.funcs)} functions`);
  const tus = Number(totals.tus || 0);
  setDonut("tu", [
    { label: "done", value: counts.done, color: "green" },
    { label: "compiled", value: counts.compiled, color: "gold" },
    { label: "in progress", value: counts.in_progress, color: "blue" },
    { label: "blocked", value: counts.blocked, color: "red" },
    { label: "todo", value: counts.todo, color: "grey" },
  ], totals.tu_percent, `${fmtInt(totals.done_tus)} / ${fmtInt(tus)} done`);

  const funcs = Number(totals.funcs || 0);
  const covered = Number(totals.done_funcs || 0);
  const unidentified = Number(totals.unidentified_funcs || 0);
  state.funcTotals = { funcs, unidentified };
  setDonut("fn", [
    { label: "covered", value: covered, color: "gold" },
    { label: "named, uncovered", value: Math.max(0, funcs - covered - unidentified), color: "grey" },
    { label: "unidentified", value: unidentified, color: "dim",
      title: "Functions IDA found in the binary that nobody has named: no DWARF file, no RTTI class, so no translation unit — but still code to write." },
  ], totals.func_percent, `${fmtInt(covered)} / ${fmtInt(funcs)} covered`);

  const linked = Number(totals.linked_tus || 0);
  setDonut("exe", [
    { label: "linked", value: linked, color: "blue" },
    { label: "not linked", value: Math.max(0, tus - linked), color: "grey",
      title: "Translation units whose file is not on the game build's compile line yet." },
  ], totals.linked_percent, `${fmtInt(linked)} / ${fmtInt(tus)} linked`);

  renderAudit(data.audit || {});
  text("activeGoal", data.active_goal || "Whole program");
  text("serverTime", fmtTime(data.server_time));

  renderAgents(data.agents || []);
  renderActiveWork(data.active_work || []);
  setQueueData((data.next && data.next.items) || []);
  setEventsData(data.recent_events || []);
  renderGoals(data.goals || []);
  setBlockedData(data.blocked || []);
}

function renderAgents(agents) {
  const activeCount = agents.filter((agent) => agent.has_active_work || Number(agent.total || 0) > 0).length;
  text("agentCount", `${fmtInt(agents.length)} users | ${fmtInt(activeCount)} active`);
  const root = el("agents");
  clearNode(root);
  root.className = agents.length ? "agent-list" : "agent-list empty";
  if (!agents.length) {
    root.textContent = "No users registered.";
    return;
  }
  const coverage = state.attributionCache || {};
  const fullContributionCoverage = Boolean(coverage.file_complete && coverage.function_complete);
  // The counts can be read from an earlier revision than the checked-out tip
  // while a warm is in flight; then they are complete, just behind, and the
  // note below says so instead of the per-row "cache x/y" caveat.
  const countsFromOlderRev = Boolean(
    coverage.counts_repo_rev && coverage.repo_rev && coverage.counts_repo_rev !== coverage.repo_rev,
  );
  const countsComplete = fullContributionCoverage || countsFromOlderRev;
  const contributionLabel = countsComplete ? "contributed to" : "contributed to cached";
  const coverageText = countsComplete
    ? ""
    : ` (cache ${fmtInt(coverage.file_cached || 0)}/${fmtInt(coverage.file_total || 0)} TUs, ${fmtInt(
        coverage.function_cached || 0,
      )}/${fmtInt(coverage.function_total || 0)} funcs)`;
  renderAttributionNote(countsFromOlderRev);
  for (const agent of agents) {
    const row = div("agent-row");
    row.classList.toggle("agent-idle", !agent.has_active_work && Number(agent.total || 0) === 0);
    const name = div("agent-name");
    name.appendChild(
      actorNode(agent.name || "unknown", agent.github_username || (agent.registered ? agent.name : null)),
    );
    if (agent.has_active_work || Number(agent.total || 0) > 0) {
      name.appendChild(span("agent-badge active", "active"));
    }
    if (agent.is_admin) {
      name.appendChild(span("agent-badge admin", "admin"));
    }
    if (agent.registered && !agent.worker_active) {
      name.appendChild(span("agent-badge inactive", "disabled"));
    }
    row.appendChild(name);
    row.appendChild(
      div(
        "agent-meta",
        `${fmtInt(agent.total)} active | ${contributionLabel} ${fmtInt(agent.contributed_tus || 0)} TUs / ${fmtInt(
          agent.contributed_funcs || 0,
        )} funcs${coverageText} | primary on ${fmtInt(agent.primary_tus || 0)} TUs / ${fmtInt(
          agent.primary_funcs || 0,
        )} funcs | lease ${shortTime(agent.lease_expires_at)} | last ${
          relTime(agent.last_activity || agent.last_update || agent.last_seen) || "never"
        }`,
      ),
    );
    if (agent.current_work && agent.current_work.length) {
      const work = div("agent-work");
      for (const tuId of agent.current_work) work.appendChild(tuButton(tuId, "tu-chip"));
      row.appendChild(work);
    }
    root.appendChild(row);
  }
}

// Contribution counts read as a judgement on people, so the one thing the
// roster must never do is show stale numbers as if they were current. A git
// index.lock left by a killed process once froze the clone for 41 days: every
// refresh "succeeded", HEAD never moved, and two thirds of one contributor's
// work simply did not exist as far as this panel was concerned.
function renderAttributionNote(countsFromOlderRev) {
  const note = el("attributionNote");
  if (!note) return;
  const repo = state.decompRepo || {};
  const behind = Number(repo.behind || 0);
  const messages = [];
  let warming = false;
  if (repo.available === false) {
    messages.push("No local decomp clone: contribution counts cannot be computed.");
  } else if (behind > 0) {
    messages.push(
      `Attribution is ${fmtInt(behind)} commit${behind === 1 ? "" : "s"} behind ${
        state.repo.name
      }/${state.repo.ref} — the counts below miss that work.`,
    );
  }
  if (countsFromOlderRev || state.attributionWarming) {
    warming = true;
    messages.push("Recomputing contributions for the newest commits; the counts below are from the previous pass.");
  }
  if (!messages.length) {
    note.hidden = true;
    note.textContent = "";
    return;
  }
  note.hidden = false;
  note.className = warming && behind <= 0 ? "attribution-note warming" : "attribution-note";
  note.textContent = messages.join(" ");
}

function renderActiveWork(items) {
  text("activeWorkCount", `${items.length} TUs`);
  const body = el("activeWork");
  clearNode(body);
  if (!items.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 4;
    cell.className = "empty";
    cell.textContent = "No active claims.";
    row.appendChild(cell);
    body.appendChild(row);
    return;
  }
  for (const item of items) {
    const row = document.createElement("tr");
    row.className = "clickable";
    const name = document.createElement("td");
    name.appendChild(tuButton(item.id));
    name.appendChild(div("tu-meta", `${item.source || "unknown"} · ${fmtInt(item.n_funcs)} funcs`));
    const status = document.createElement("td");
    status.appendChild(span(`pill ${item.status}`, item.status.replace("_", " ")));
    const owner = document.createElement("td");
    owner.appendChild(actorNode(item.owner));
    const lease = document.createElement("td");
    lease.textContent = item.lease_expires_at ? shortTime(item.lease_expires_at) : "no active lease";
    row.append(name, status, owner, lease);
    row.addEventListener("click", () => openDetail(item.id));
    body.appendChild(row);
  }
}

/* ---------------- Next Queue (client-side filter / search / pager) ---------------- */

function setQueueData(items) {
  const view = state.queueView;
  view.all = items || [];
  fillSelect(
    "queueFilterSource",
    [...new Set(view.all.map((i) => i.source).filter(Boolean))].sort(),
    "All sources",
  );
  renderQueue();
}

function filteredQueue() {
  const view = state.queueView;
  const q = view.q.toLowerCase();
  return view.all.filter((item) => {
    if (view.source && item.source !== view.source) return false;
    if (!q) return true;
    return (
      String(item.id || "").toLowerCase().includes(q) ||
      String(item.dest_path || "").toLowerCase().includes(q) ||
      String(item.source || "").toLowerCase().includes(q)
    );
  });
}

function renderQueue() {
  const view = state.queueView;
  text("nextCount", `${fmtCompact(view.all.length)} ready`);
  const items = filteredQueue();
  const { slice, from, to, page, totalPages } = paginate(items, view);
  view.page = page;

  const root = el("nextQueue");
  clearNode(root);
  root.className = slice.length ? "queue" : "queue empty";
  if (!slice.length) {
    root.textContent = view.all.length ? "No TUs match." : "No available TUs.";
  } else {
    for (const item of slice) {
      const row = div("queue-row");
      row.classList.add("clickable");
      row.appendChild(tuButton(item.id));
      row.appendChild(
        div(
          "tu-meta",
          `${item.source || "unknown"} · ${fmtInt(item.n_funcs)} funcs · ${fmtInt(
            item.unresolved_deps,
          )} unresolved deps`,
        ),
      );
      row.addEventListener("click", () => openDetail(item.id));
      root.appendChild(row);
    }
  }
  renderMiniFoot("queue", items.length, from, to, page, totalPages);
}

/* ---------------- Live Events (client-side filter / search / pager) ---------------- */

// Epoch millis used to order the list, newest first. Reconstructed and live
// events sort by their real timestamp; any leftover backfilled row we could not
// reconstruct sinks to the bottom instead of floating up on its meaningless
// shared import timestamp.
function eventSortTime(event) {
  if (isBackfilledEvent(event)) return -Infinity;
  const t = event.ts ? new Date(event.ts).getTime() : NaN;
  return Number.isNaN(t) ? -Infinity : t;
}

// Backfilled rows carry a source tag; they are placeholders to be replaced by
// one reconstructed event per real commit (see expandEvents).
function isBackfilledEvent(event) {
  const source = String((event.detail && event.detail.source) || "").toLowerCase();
  return source.includes("pre-server") || source.includes("commit delta");
}

// Replace each backfilled row with one event per real commit (2026+) on its
// file: the commit's author and date, no bogus commit/source detail. Rows whose
// file has no resolved history are kept as-is (and sink via eventSortTime).
function expandEvents(events) {
  const out = [];
  const reconstructed = new Set();
  for (const event of events || []) {
    const history = isBackfilledEvent(event) ? state.eventHistory[event.tu_id] : null;
    if (history && history.length) {
      for (const commit of history) {
        const key = [event.tu_id, event.action, commit.date, commit.author].join("\x1f");
        if (reconstructed.has(key)) continue;
        reconstructed.add(key);
        out.push({
          id: event.id || 0,
          ts: commit.date,
          tu_id: event.tu_id,
          agent: commit.author,
          agentLogin: commit.login || null,
          action: event.action,
          detail: {},
          reconstructed: true,
        });
      }
    } else {
      out.push(event);
    }
  }
  return out;
}

function setEventsData(events) {
  state.rawEvents = events || [];
  for (const event of state.rawEvents) {
    state.lastEventId = Math.max(state.lastEventId, event.id || 0);
  }
  rebuildEvents();
  resolveEventHistory();
}

// Build the view list from the raw payload plus whatever history we have, then
// render. Re-run whenever new history arrives so the expansion updates in place.
function rebuildEvents() {
  const view = state.eventsView;
  view.all = expandEvents(state.rawEvents);
  fillSelect(
    "eventsFilterAction",
    [...new Set(view.all.map((e) => e.action).filter(Boolean))].sort(),
    "All events",
  );
  fillSelect(
    "eventsFilterActor",
    [...new Set(view.all.map((e) => e.agent).filter(Boolean))].sort(),
    "All actors",
  );
  renderEvents();
}

// Fetch per-file commit history once backfilled rows appear. One call returns
// the whole TU-id -> commits map (computed from the local decomp clone), so this
// runs once per session; failures retry on a later refresh.
async function resolveEventHistory() {
  if (state.eventHistoryFetched) return;
  if (!state.rawEvents.some(isBackfilledEvent)) return;
  state.eventHistoryFetched = true;
  try {
    const data = await fetchJson("/events/file-history");
    Object.assign(state.eventHistory, data.history || {});
    rebuildEvents();
  } catch (_) {
    // Non-fatal: keep the original rows, and allow a retry next refresh.
    state.eventHistoryFetched = false;
  }
}

function filteredEvents() {
  const view = state.eventsView;
  const q = view.q.toLowerCase();
  return view.all.filter((event) => {
    if (view.action && event.action !== view.action) return false;
    if (view.actor && event.agent !== view.actor) return false;
    if (!q) return true;
    return (
      String(event.agent || "").toLowerCase().includes(q) ||
      String(event.tu_id || "").toLowerCase().includes(q) ||
      String(event.action || "").toLowerCase().includes(q) ||
      detailText(event.detail).toLowerCase().includes(q)
    );
  });
}

function renderEvents() {
  const view = state.eventsView;
  text("eventCount", `${fmtCompact(view.all.length)} events`);
  // Newest first by effective date, tie-broken by id so equal-dated rows stay stable.
  const events = filteredEvents().sort(
    (a, b) => eventSortTime(b) - eventSortTime(a) || (b.id || 0) - (a.id || 0),
  );
  const { slice, from, to, page, totalPages } = paginate(events, view);
  view.page = page;

  const root = el("events");
  clearNode(root);
  root.className = slice.length ? "event-table" : "event-table empty";
  if (!slice.length) {
    root.textContent = view.all.length ? "No events match." : "No events yet.";
  } else {
    const head = div("event-row event-head");
    ["Time", "Event", "Actor", "Target", "Details"].forEach((label) =>
      head.appendChild(div("event-cell", label)),
    );
    root.appendChild(head);
    for (const event of slice) {
      const row = div("event-row");
      const timeCell = div("event-cell event-time", eventTimeLabel(event));
      if (event.ts) {
        timeCell.title = event.reconstructed
          ? `commit date: ${fmtTime(event.ts)}`
          : fmtTime(event.ts);
      }
      row.appendChild(timeCell);
      row.appendChild(div("event-cell event-action", event.action || "event"));
      const actor = div("event-cell");
      actor.appendChild(
        event.agent ? actorNode(event.agent, event.agentLogin) : span("muted-text", "server"),
      );
      row.appendChild(actor);
      const target = div("event-cell event-target");
      if (event.tu_id) target.appendChild(tuButton(event.tu_id, "event-tu"));
      else target.textContent = "server";
      row.appendChild(target);
      const detail = div("event-cell event-detail", detailText(event.detail) || "-");
      row.appendChild(detail);
      root.appendChild(row);
    }
  }
  renderMiniFoot("events", events.length, from, to, page, totalPages);
}

/* ---------------- Shared mini-panel pager ---------------- */

function paginate(items, view) {
  const totalPages = Math.max(1, Math.ceil(items.length / view.perPage));
  const page = Math.min(Math.max(1, view.page), totalPages);
  const start = (page - 1) * view.perPage;
  const slice = items.slice(start, start + view.perPage);
  return {
    slice,
    page,
    totalPages,
    from: items.length ? start + 1 : 0,
    to: start + slice.length,
  };
}

function renderMiniFoot(prefix, total, from, to, page, totalPages) {
  text(`${prefix}Range`, `${fmtInt(from)}-${fmtInt(to)} of ${fmtInt(total)}`);
  text(`${prefix}PageStatus`, `Page ${fmtInt(page)} of ${fmtInt(totalPages)}`);
  el(`${prefix}Prev`).disabled = page <= 1;
  el(`${prefix}Next`).disabled = page >= totalPages;
}

function renderGoals(goals) {
  text("goalCount", `${goals.length} goals`);
  const root = el("goals");
  clearNode(root);
  root.className = goals.length ? "goal-list" : "goal-list empty";
  if (!goals.length) {
    root.textContent = "No goals imported.";
    return;
  }
  for (const goal of goals) {
    const done = Number(goal.done || 0);
    const total = Number(goal.total || 0);
    const percent = total ? (done / total) * 100 : 0;
    const row = div("goal-row");
    row.classList.add("clickable");
    const title = document.createElement("button");
    title.type = "button";
    title.className = "text-link tu-name goal-title";
    title.textContent = goal.name;
    title.addEventListener("click", (event) => {
      event.stopPropagation();
      openGoalDetail(goal.name);
    });
    row.appendChild(title);
    row.appendChild(div("goal-meta", `${goal.category || "uncategorized"} · ${fmtInt(done)} / ${fmtInt(total)} done`));
    const bar = div("bar");
    const fill = document.createElement("span");
    fill.style.width = `${Math.max(0, Math.min(100, percent))}%`;
    bar.appendChild(fill);
    row.appendChild(bar);
    row.addEventListener("click", () => openGoalDetail(goal.name));
    root.appendChild(row);
  }
}

function setBlockedData(items) {
  const view = state.blockedView;
  view.all = items || [];
  fillSelect(
    "blockedFilterSource",
    [...new Set(view.all.map((i) => i.source).filter(Boolean))].sort(),
    "All sources",
  );
  renderBlocked();
}

function filteredBlocked() {
  const view = state.blockedView;
  const q = view.q.toLowerCase();
  return view.all.filter((item) => {
    if (view.source && item.source !== view.source) return false;
    if (!q) return true;
    return (
      String(item.id || "").toLowerCase().includes(q) ||
      String(item.dest_path || "").toLowerCase().includes(q) ||
      String(item.source || "").toLowerCase().includes(q) ||
      String(item.notes || "").toLowerCase().includes(q)
    );
  });
}

function renderBlocked() {
  const view = state.blockedView;
  text("blockedListCount", `${fmtCompact(view.all.length)} TUs`);
  const items = filteredBlocked();
  const { slice, from, to, page, totalPages } = paginate(items, view);
  view.page = page;

  const root = el("blockedList");
  clearNode(root);
  root.className = slice.length ? "blocked-list" : "blocked-list empty";
  if (!slice.length) {
    root.textContent = view.all.length ? "No blocked TUs match." : "No blocked work.";
  } else {
    for (const item of slice) {
      const row = div("blocked-row");
      row.classList.add("clickable");
      row.appendChild(tuButton(item.id));
      row.appendChild(div("tu-meta", item.notes || "No reason recorded."));
      row.addEventListener("click", () => openDetail(item.id));
      root.appendChild(row);
    }
  }
  renderMiniFoot("blocked", items.length, from, to, page, totalPages);
}

/* ---------------- GitHub panel ---------------- */

async function refreshGithub() {
  if (state.githubInFlight) return;
  state.githubInFlight = true;
  try {
    renderGithub(await fetchJson("/github/overview", 20000));
  } catch (error) {
    text("repoDesc", `GitHub data unavailable: ${error.message}`);
  } finally {
    state.githubInFlight = false;
  }
}

function setDownloadCount(n) {
  const chip = el("downloadCount");
  if (!chip) return;
  if (n == null) {
    chip.hidden = true;
    return;
  }
  const num = chip.querySelector(".dc-num");
  if (num) num.textContent = fmtInt(n);
  chip.title = `${fmtInt(n)} downloader${n === 1 ? "" : "s"} (unique addresses per day; the full game and the update each count once)`;
  chip.hidden = false;
}

async function refreshDownload() {
  const group = el("downloadGroup");
  const link = el("downloadBuild");
  if (!group || !link) return;
  try {
    const data = await fetchJson("/api/builds");
    const latest = data && data.latest;
    if (!latest) {
      group.hidden = true;
      state.latestBuild = null;
      return;
    }
    state.latestBuild = latest;
    state.builds = data.builds || [];
    const parts = [];
    if (latest.commit_short) parts.push(latest.commit_short);
    if (latest.built_at) parts.push(relTime(latest.built_at));
    if (latest.size_bytes) parts.push(fmtBytes(latest.size_bytes));
    text("downloadMeta", parts.join(" · ") || "latest");
    link.title = `Download build ${latest.commit_short || latest.commit_sha}: full game (${fmtBytes(latest.size_bytes)})` +
      (latest.update_url ? ` or the exe-only update (${fmtBytes(latest.bundle_size)})` : "");
    setDownloadCount(latest.downloads || 0);
    group.hidden = false;
    if (!el("downloadMenu").hidden) renderDownloadMenu();
  } catch (error) {
    // No builds published yet (or endpoint unavailable): keep the button hidden.
    group.hidden = true;
  }
}

// The button opens a chooser: the full game (assets + exe) or the exe-only update for
// anyone who already has the game folder. The server says whether the assets changed
// since the previous build, which is what decides between the two.
function onDownloadClick(e) {
  e.preventDefault();
  const menu = el("downloadMenu");
  if (!menu || !state.latestBuild) return;
  if (menu.hidden) {
    renderDownloadMenu();
    menu.hidden = false;
    el("downloadBuild").setAttribute("aria-expanded", "true");
  } else {
    closeDownloadMenu();
  }
}

function closeDownloadMenu() {
  const menu = el("downloadMenu");
  if (!menu) return;
  menu.hidden = true;
  const btn = el("downloadBuild");
  if (btn) btn.setAttribute("aria-expanded", "false");
}

function downloadOption(title, size, note, onPick) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "dl-opt";
  b.setAttribute("role", "menuitem");
  b.appendChild(document.createElement("strong")).textContent = title;
  b.appendChild(span("dl-size", size));
  const small = document.createElement("small");
  small.textContent = note;
  b.appendChild(small);
  b.addEventListener("click", onPick);
  return b;
}

function renderDownloadMenu() {
  const menu = el("downloadMenu");
  const b = state.latestBuild;
  if (!menu || !b) return;
  clearNode(menu);
  const previous = (state.builds || []).find((x) => x.id !== b.id);
  const assetsNote = !b.update_url
    ? "This build predates the exe-only update; the next published build will offer one."
    : b.assets_changed === false && previous
      ? `Game assets unchanged since build ${previous.commit_short || previous.id}${previous.built_at ? ` (${relTime(previous.built_at)})` : ""}: if you already have the game folder, the update is all you need.`
      : b.assets_changed === true
        ? "The game assets changed in this build: take the full game unless you know which files moved."
        : "First published build: take the full game.";
  menu.appendChild(downloadOption(
    "Full game", fmtBytes(b.size_bytes),
    "Everything: the exe, its DLLs and every game asset. For a first install, or when the assets changed.",
    () => startDownload(b.download_url || "/download/latest", "full"),
  ));
  if (b.update_url) {
    menu.appendChild(downloadOption(
      "Update only", fmtBytes(b.bundle_size),
      "Burnout_PC.exe, its DLLs and the .cgsmap. Drop them over an existing game folder.",
      () => startDownload(b.update_url, "update"),
    ));
  }
  const note = div(`dl-note${b.assets_changed ? " warn" : ""}`, assetsNote);
  note.id = "downloadNote";
  menu.appendChild(note);
}

// Ask first (HEAD is never counted), then navigate. A 429 means this address spent
// today's allowance for that kind; the menu says so instead of opening a JSON page.
async function startDownload(url, kind) {
  const note = el("downloadNote");
  try {
    const probe = await fetch(url, { method: "HEAD", cache: "no-store" });
    if (probe.status === 429) {
      if (note) {
        note.className = "dl-note warn";
        note.textContent = kind === "full"
          ? "This address has used today's full-game downloads (3 per day). The exe-only update has a larger allowance; the full game is back tomorrow."
          : "This address has used today's update downloads. Back tomorrow.";
      }
      return;
    }
    if (!probe.ok) {
      if (note) { note.className = "dl-note warn"; note.textContent = `Download unavailable (HTTP ${probe.status}).`; }
      return;
    }
  } catch (_) {
    /* the probe is a courtesy: fall through and let the browser try */
  }
  closeDownloadMenu();
  window.location.href = url;
  window.setTimeout(refreshDownload, 4000);
}

/* ---------------- Build contents preview ---------------- */

function buildZipTree(entries) {
  const root = { name: "", dir: true, size: 0, children: new Map() };
  for (const entry of entries || []) {
    const parts = entry.path.split("/").filter(Boolean);
    if (!parts.length) continue;
    let node = root;
    parts.forEach((part, i) => {
      const leaf = i === parts.length - 1;
      let child = node.children.get(part);
      if (!child) {
        child = { name: part, dir: !leaf || entry.is_dir, size: 0, children: new Map() };
        node.children.set(part, child);
      }
      if (leaf && !entry.is_dir) child.size = entry.size || 0;
      node = child;
    });
  }
  // Roll folder sizes up from their files.
  const sum = (node) => {
    if (!node.dir) return node.size || 0;
    let total = 0;
    for (const child of node.children.values()) total += sum(child);
    node.size = total;
    return total;
  };
  sum(root);
  return root;
}

function renderZipNode(node, depth, container) {
  const kids = [...node.children.values()].sort((a, b) => {
    if (a.dir !== b.dir) return a.dir ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
  for (const child of kids) {
    child.path = `${node.path || ""}/${child.name}`;
    const row = div(child.dir ? "tree-node dir" : "tree-node");
    row.style.paddingLeft = `${6 + depth * 16}px`;
    const collapsed = child.path in state.zipCollapsed ? state.zipCollapsed[child.path] : depth >= 1;
    const twist = span("twist", child.dir ? (collapsed ? "▶" : "▼") : "");
    row.appendChild(twist);
    row.appendChild(span("icon", child.dir ? "📁" : "📄"));
    row.appendChild(span("name", child.name));
    if (child.size != null) row.appendChild(span("size", fmtBytes(child.size)));
    container.appendChild(row);
    if (child.dir) {
      const box = div("tree-children" + (collapsed ? " collapsed" : ""));
      renderZipNode(child, depth + 1, box);
      container.appendChild(box);
      row.addEventListener("click", () => {
        const nowCollapsed = !box.classList.contains("collapsed");
        box.classList.toggle("collapsed", nowCollapsed);
        twist.textContent = nowCollapsed ? "▶" : "▼";
        state.zipCollapsed[child.path] = nowCollapsed;
      });
    }
  }
}

async function openBuildContents(buildId) {
  if (buildId == null) return;
  state.zipCollapsed = {};
  // Standalone drawer view (not part of the TU/profile back-stack).
  state.detailNav.stack = [];
  state.detailNav.current = { type: "build", id: buildId };
  showDetailOverlay();
  updateDetailBack();
  text("detailTitle", "Build contents");
  const body = el("detailBody");
  body.innerHTML = '<p class="muted-text">Reading zip…</p>';
  try {
    const data = await fetchJson(`/api/builds/${buildId}/contents`, 20000);
    clearNode(body);
    const summary = div("zip-summary");
    summary.appendChild(span("zip-summary-file", data.filename));
    summary.appendChild(
      span(
        "zip-summary-meta",
        `${fmtInt(data.total_files)} files · ${fmtBytes(data.total_size)} uncompressed` +
          (data.truncated ? " · list truncated" : ""),
      ),
    );
    body.appendChild(summary);
    const tree = div("file-tree zip-tree");
    renderZipNode(buildZipTree(data.entries), 0, tree);
    body.appendChild(tree);
  } catch (error) {
    body.innerHTML = "";
    body.appendChild(div("muted-text", `Failed to read build contents: ${error.message}`));
  }
}

function renderGithub(data) {
  const repo = data.repo || {};
  const info = data.info || {};
  if (repo.owner) state.repo = { owner: repo.owner, name: repo.name, ref: repo.ref };
  text("repoBranch", repo.ref || "dev");

  const link = el("repoLink");
  link.textContent = info.full_name || `${repo.owner}/${repo.name}`;
  link.href = info.html_url || `https://github.com/${repo.owner}/${repo.name}`;

  text("repoDesc", info.description || "No description provided.");

  // Rate limit indicator
  const rate = data.rate_limit || {};
  const rateNode = el("ghRate");
  if (rate.remaining != null) {
    const auth = rate.authenticated ? "auth" : "anon";
    rateNode.textContent = `API ${rate.remaining}/${rate.limit} (${auth})`;
    rateNode.classList.toggle("warn", rate.remaining <= 5);
  } else {
    rateNode.textContent = "";
  }

  renderStats(info);
  renderLatestCommit(data.latest_commit);
  renderCommits(data.commits || []);
  renderTree(data.tree);

  if (data.errors && data.errors.length) {
    el("treeMeta").textContent = data.errors[0];
  }
}

function renderStats(info) {
  const root = el("ghStats");
  clearNode(root);
  const stats = [
    ["★", info.stargazers_count, "stars"],
    ["⑂", info.forks_count, "forks"],
    ["◎", info.open_issues_count, "issues"],
    ["⊙", info.watchers_count, "watching"],
  ];
  for (const [icon, value, label] of stats) {
    if (value == null) continue;
    const node = div("gh-stat");
    node.appendChild(document.createTextNode(`${icon} `));
    const strong = document.createElement("strong");
    strong.textContent = fmtInt(value);
    node.appendChild(strong);
    node.appendChild(document.createTextNode(` ${label}`));
    root.appendChild(node);
  }
  if (info.language) {
    const node = div("gh-stat");
    const strong = document.createElement("strong");
    strong.textContent = info.language;
    node.appendChild(strong);
    root.appendChild(node);
  }
  if (info.pushed_at) {
    root.appendChild(div("gh-stat", `pushed ${relTime(info.pushed_at)}`));
  }
}

function commitAvatar(commit) {
  if (!commit.avatar_url) return null;
  const img = document.createElement("img");
  img.className = "avatar";
  img.src = `${commit.avatar_url}&s=36`;
  img.alt = commit.login || "";
  img.loading = "lazy";
  return img;
}

function renderLatestCommit(commit) {
  const root = el("latestCommit");
  clearNode(root);
  if (!commit) {
    root.className = "latest-commit empty";
    root.textContent = "No commits found.";
    return;
  }
  root.className = "latest-commit";
  const msg = div("commit-msg");
  if (commit.html_url) {
    const a = document.createElement("a");
    a.href = commit.html_url;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = commit.message || "(no message)";
    msg.appendChild(a);
  } else {
    msg.textContent = commit.message || "(no message)";
  }
  root.appendChild(msg);

  const meta = div("commit-meta");
  const avatar = commitAvatar(commit);
  if (avatar) meta.appendChild(avatar);
  meta.appendChild(span("sha", commit.short_sha || ""));
  meta.appendChild(document.createTextNode(`${commit.author || commit.login || "unknown"} · ${relTime(commit.date)}`));
  root.appendChild(meta);
}

function renderCommits(commits) {
  const root = el("commitList");
  clearNode(root);
  const rest = commits.slice(1);
  root.className = rest.length ? "commit-list" : "commit-list empty";
  if (!rest.length) {
    const li = document.createElement("li");
    li.textContent = "No earlier commits.";
    root.appendChild(li);
    return;
  }
  for (const commit of rest) {
    const li = document.createElement("li");
    li.appendChild(span("sha", commit.short_sha || ""));
    const body = div("body");
    const title = document.createElement("div");
    if (commit.html_url) {
      const a = document.createElement("a");
      a.href = commit.html_url;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = commit.message || "(no message)";
      title.appendChild(a);
    } else {
      title.textContent = commit.message || "(no message)";
    }
    body.appendChild(title);
    body.appendChild(div("commit-meta", `${commit.author || commit.login || "unknown"} · ${relTime(commit.date)}`));
    li.appendChild(body);
    root.appendChild(li);
  }
}

/* Build a nested tree from the flat path list GitHub returns. */
function buildTree(entries) {
  const root = { name: "", children: new Map(), type: "tree" };
  for (const entry of entries) {
    const parts = entry.path.split("/");
    let node = root;
    parts.forEach((part, i) => {
      const isLeaf = i === parts.length - 1;
      let child = node.children.get(part);
      if (!child) {
        child = {
          name: part,
          children: new Map(),
          type: isLeaf ? entry.type : "tree",
          size: isLeaf ? entry.size : undefined,
          path: parts.slice(0, i + 1).join("/"),
        };
        node.children.set(part, child);
      }
      node = child;
    });
  }
  return root;
}

function sortedChildren(node) {
  return [...node.children.values()].sort((a, b) => {
    if (a.type !== b.type) return a.type === "tree" ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
}

function renderTreeNode(node, depth, container) {
  for (const child of sortedChildren(node)) {
    const isDir = child.type === "tree";
    const row = div(isDir ? "tree-node dir" : "tree-node");
    row.style.paddingLeft = `${6 + depth * 16}px`;

    // default collapse deep folders to keep it tidy
    const collapsed =
      child.path in state.treeCollapsed ? state.treeCollapsed[child.path] : depth >= 1;

    const twist = span("twist", isDir ? (collapsed ? "▶" : "▼") : "");
    row.appendChild(twist);
    row.appendChild(span("icon", isDir ? "📁" : "📄"));
    if (isDir) {
      row.appendChild(span("name", child.name));
    } else {
      // File rows link straight to the file on GitHub.
      const link = document.createElement("a");
      link.className = "name file-link";
      link.textContent = child.name;
      link.href = ghBlobUrl(child.path);
      link.target = "_blank";
      link.rel = "noopener";
      link.title = `Open ${child.path} on GitHub`;
      row.appendChild(link);
    }
    if (!isDir && child.size != null) row.appendChild(span("size", fmtBytes(child.size)));
    container.appendChild(row);

    if (isDir) {
      const kids = div("tree-children" + (collapsed ? " collapsed" : ""));
      renderTreeNode(child, depth + 1, kids);
      container.appendChild(kids);
      row.addEventListener("click", () => {
        const nowCollapsed = !kids.classList.contains("collapsed");
        kids.classList.toggle("collapsed", nowCollapsed);
        twist.textContent = nowCollapsed ? "▶" : "▼";
        state.treeCollapsed[child.path] = nowCollapsed;
      });
    }
  }
}

function renderTree(tree) {
  const root = el("fileTree");
  clearNode(root);
  if (!tree || !tree.tree || !tree.tree.length) {
    root.className = "file-tree empty";
    root.textContent = "File tree unavailable.";
    el("treeMeta").textContent = "";
    return;
  }
  root.className = "file-tree";
  el("treeMeta").textContent = `${fmtInt(tree.count)} entries${tree.truncated ? " (truncated)" : ""}`;
  const built = buildTree(tree.tree);
  renderTreeNode(built, 0, root);
}

/* ---------------- Explorer (search / browse) ---------------- */

const STATUS_LABELS = {
  todo: "todo",
  in_progress: "in progress",
  compiled: "compiled",
  done: "done",
  blocked: "blocked",
};

function statusPill(status) {
  return span(`pill ${status}`, (STATUS_LABELS[status] || status || "—").replace("_", " "));
}

async function loadFacets() {
  try {
    const f = await fetchJson("/api/facets", 15000);
    fillSelect("filterSource", f.sources, "All sources");
    fillSelect("filterGoal", f.goals, "All goals");
    // status options swap per tab; remember both sets
    state.explorer.tuStatuses = f.tu_statuses || [];
    state.explorer.funcStatuses = f.func_statuses || [];
    syncStatusOptions();
  } catch (_) {
    /* facets are best-effort */
  }
}

function fillSelect(id, values, allLabel) {
  const sel = el(id);
  if (!sel) return;
  const options = [
    { value: "", text: allLabel },
    ...(values || []).map((value) => ({ value: String(value), text: String(value) })),
  ];
  const existing = [...sel.options];
  const unchanged =
    existing.length === options.length &&
    existing.every(
      (option, i) => option.value === options[i].value && option.textContent === options[i].text,
    );
  if (unchanged) return;

  const current = sel.value;
  clearNode(sel);
  for (const { value, text } of options) {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = text;
    sel.appendChild(opt);
  }
  sel.value = current;
}

function syncStatusOptions() {
  const list =
    state.explorer.tab === "funcs" ? state.explorer.funcStatuses : state.explorer.tuStatuses;
  fillSelect("filterStatus", list, "All statuses");
}

function explorerParams() {
  const ex = state.explorer;
  const p = new URLSearchParams();
  if (ex.q) p.set("q", ex.q);
  if (ex.status) p.set("status", ex.status);
  p.set("limit", ex.limit);
  p.set("offset", ex.offset);
  if (ex.tab === "tus") {
    if (ex.source) p.set("source", ex.source);
    if (ex.goal) p.set("goal", ex.goal);
    p.set("sort", ex.sort);
    p.set("order", ex.order);
  }
  return p;
}

async function loadExplorer() {
  const ex = state.explorer;
  const path = ex.tab === "funcs" ? "/api/funcs" : "/api/tus";
  const requestId = ++ex.requestId;
  try {
    const data = await fetchJson(`${path}?${explorerParams()}`, 15000);
    if (requestId !== ex.requestId) return;
    ex.total = data.total || 0;
    ex.items = data.items || [];
    if (ex.tab === "funcs") renderFuncRows(ex.items);
    else renderTuRows(ex.items);
    renderExplorerFoot();
  } catch (error) {
    if (requestId !== ex.requestId) return;
    el("explorerBody").innerHTML = "";
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 6;
    cell.className = "empty";
    cell.textContent = `Explorer unavailable: ${error.message}`;
    row.appendChild(cell);
    el("explorerBody").appendChild(row);
  }
}

function refreshExplorerTuRow(detail) {
  const ex = state.explorer;
  if (ex.tab !== "tus" || !detail || !detail.id || !Array.isArray(ex.items)) return;
  const index = ex.items.findIndex((item) => item.id === detail.id);
  if (index < 0) return;
  ex.items[index] = { ...ex.items[index], ...detail };
  renderTuRows(ex.items);
}

function renderExplorerFoot() {
  const ex = state.explorer;
  text("explorerCount", `${fmtInt(ex.total)} results`);
  const from = ex.total === 0 ? 0 : ex.offset + 1;
  const to = Math.min(ex.offset + ex.limit, ex.total);
  const totalPages = Math.max(1, Math.ceil(ex.total / ex.limit));
  const currentPage = Math.min(totalPages, Math.floor(ex.offset / ex.limit) + 1);
  text("explorerRange", `${fmtInt(from)}-${fmtInt(to)} of ${fmtInt(ex.total)}`);
  text("pageStatus", `Page ${fmtInt(currentPage)} of ${fmtInt(totalPages)}`);
  el("pagePrev").disabled = ex.offset <= 0;
  el("pageNext").disabled = ex.offset + ex.limit >= ex.total;
  const jump = el("pageJump");
  jump.max = totalPages;
  jump.value = currentPage;
  renderPageButtons(currentPage, totalPages);
}

function goToPage(page) {
  const ex = state.explorer;
  const totalPages = Math.max(1, Math.ceil(ex.total / ex.limit));
  const nextPage = Math.max(1, Math.min(totalPages, Number(page) || 1));
  const nextOffset = (nextPage - 1) * ex.limit;
  if (nextOffset === ex.offset) {
    renderExplorerFoot();
    return;
  }
  ex.offset = nextOffset;
  loadExplorer();
}

function renderPageButtons(currentPage, totalPages, rootId = "pageButtons", onPage = goToPage) {
  const root = el(rootId);
  if (!root) return;
  clearNode(root);
  const pages = new Set([1, totalPages]);
  for (let page = currentPage - 1; page <= currentPage + 1; page += 1) {
    if (page >= 1 && page <= totalPages) pages.add(page);
  }
  let prev = 0;
  for (const page of [...pages].sort((a, b) => a - b)) {
    if (page - prev > 1) root.appendChild(span("page-ellipsis", "..."));
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "filter page-btn";
    btn.textContent = page;
    btn.disabled = page === currentPage;
    btn.addEventListener("click", () => onPage(page));
    root.appendChild(btn);
    prev = page;
  }
}

function setHead(cols) {
  const head = el("explorerHead");
  clearNode(head);
  const tr = document.createElement("tr");
  for (const c of cols) {
    const th = document.createElement("th");
    th.textContent = c;
    tr.appendChild(th);
  }
  head.appendChild(tr);
}

function renderTuRows(items) {
  setHead(["Translation Unit", "Status", "Funcs", "Source", "Unresolved Deps", "Actor"]);
  const body = el("explorerBody");
  clearNode(body);
  if (!items.length) return emptyRow(body, 6, "No translation units match.");
  for (const item of items) {
    const row = document.createElement("tr");
    row.className = "clickable";
    const name = document.createElement("td");
    name.appendChild(div("tu-name", item.id));
    if (item.dest_path) name.appendChild(div("tu-meta", item.dest_path));
    const fn = document.createElement("td");
    fn.textContent = fmtInt(item.n_funcs);
    const src = document.createElement("td");
    src.textContent = item.source || "—";
    const deps = document.createElement("td");
    const unresolved = item.unresolved_deps == null ? null : Number(item.unresolved_deps);
    deps.textContent =
      unresolved == null ? "dependency data unavailable" : `${fmtInt(unresolved)} unresolved`;
    deps.title =
      item.total_deps == null
        ? "Dependency tracking is unavailable for this row."
        : `${fmtInt(item.total_deps)} recorded dependencies`;
    const owner = document.createElement("td");
    if (item.owner && item.status === "in_progress" && item.lease_expires_at) {
      owner.appendChild(actorNode(item.owner));
      owner.appendChild(div("tu-meta", "active claim"));
    } else if (item.primary_contributor || item.completed_by) {
      appendAttribution(owner, item, "no contributor data");
    } else if (item.last_actor) {
      owner.appendChild(actorNode(item.last_actor));
      owner.appendChild(div("tu-meta", item.last_action || "last activity"));
    } else {
      owner.textContent = "no live claim";
      owner.title = "No reliable completed-by owner is stored for imported status rows.";
    }
    const status = document.createElement("td");
    status.appendChild(statusPill(item.status));
    row.append(name, status, fn, src, deps, owner);
    row.addEventListener("click", () => openDetail(item.id));
    body.appendChild(row);
  }
}

function renderFuncRows(items) {
  setHead(["Function", "Status", "Translation Unit", "Actor"]);
  const body = el("explorerBody");
  clearNode(body);
  if (!items.length) return emptyRow(body, 4, "No functions match.");
  for (const item of items) {
    const row = document.createElement("tr");
    row.className = "clickable";
    const name = document.createElement("td");
    name.appendChild(div("fn-name", item.name));
    const status = document.createElement("td");
    status.appendChild(statusPill(item.status));
    const tu = document.createElement("td");
    tu.appendChild(tuButton(item.tu_id, "tu-meta"));
    const actor = document.createElement("td");
    if (item.primary_contributor || item.completed_by) {
      appendAttribution(actor, item);
    } else {
      actor.textContent = "unattributed";
      actor.title = "No actor has been linked to this function yet.";
    }
    row.append(name, status, tu, actor);
    row.addEventListener("click", () => openDetail(item.tu_id));
    body.appendChild(row);
  }
}

function emptyRow(body, span, message) {
  const row = document.createElement("tr");
  const cell = document.createElement("td");
  cell.colSpan = span;
  cell.className = "empty";
  cell.textContent = message;
  row.appendChild(cell);
  body.appendChild(row);
}

function resetAndLoad() {
  state.explorer.offset = 0;
  loadExplorer();
}

// Wire the search box, filter selects, and Prev/Next for the dashboard's
// paginated mini-panels. Filtering happens over the in-memory `view.all`.
function initMiniPanels() {
  const panels = [
    { view: state.eventsView, prefix: "events", render: renderEvents, filters: [
      ["eventsFilterAction", "action"],
      ["eventsFilterActor", "actor"],
    ] },
    { view: state.queueView, prefix: "queue", render: renderQueue, filters: [
      ["queueFilterSource", "source"],
    ] },
    { view: state.blockedView, prefix: "blocked", render: renderBlocked, filters: [
      ["blockedFilterSource", "source"],
    ] },
  ];
  for (const { view, prefix, render, filters } of panels) {
    el(`${prefix}Search`).addEventListener("input", (e) => {
      const value = e.target.value.trim();
      clearTimeout(view.searchTimer);
      view.searchTimer = setTimeout(() => {
        view.q = value;
        view.page = 1;
        render();
      }, 200);
    });
    for (const [id, key] of filters) {
      el(id).addEventListener("change", (e) => {
        view[key] = e.target.value;
        view.page = 1;
        render();
      });
    }
    el(`${prefix}Prev`).addEventListener("click", () => {
      view.page = Math.max(1, view.page - 1);
      render();
    });
    el(`${prefix}Next`).addEventListener("click", () => {
      view.page += 1;
      render();
    });
  }
}

function initExplorer() {
  const ex = state.explorer;

  el("explorerTabs").addEventListener("click", (e) => {
    const btn = e.target.closest(".tab");
    if (!btn) return;
    ex.tab = btn.dataset.tab;
    for (const t of el("explorerTabs").querySelectorAll(".tab")) {
      t.classList.toggle("active", t === btn);
    }
    document
      .querySelectorAll(".tus-only")
      .forEach((node) => node.classList.toggle("hidden", ex.tab !== "tus"));
    ex.status = "";
    syncStatusOptions();
    resetAndLoad();
  });

  el("explorerSearch").addEventListener("input", (e) => {
    ex.q = e.target.value.trim();
    clearTimeout(ex.searchTimer);
    ex.searchTimer = setTimeout(resetAndLoad, 250);
  });

  el("filterStatus").addEventListener("change", (e) => {
    ex.status = e.target.value;
    resetAndLoad();
  });
  el("filterSource").addEventListener("change", (e) => {
    ex.source = e.target.value;
    resetAndLoad();
  });
  el("filterGoal").addEventListener("change", (e) => {
    ex.goal = e.target.value;
    resetAndLoad();
  });
  el("sortBy").addEventListener("change", (e) => {
    ex.sort = e.target.value;
    resetAndLoad();
  });
  el("sortOrder").addEventListener("click", () => {
    ex.order = ex.order === "asc" ? "desc" : "asc";
    el("sortOrder").textContent = ex.order === "asc" ? "↑" : "↓";
    resetAndLoad();
  });

  el("pagePrev").addEventListener("click", () => {
    ex.offset = Math.max(0, ex.offset - ex.limit);
    loadExplorer();
  });
  el("pageNext").addEventListener("click", () => {
    if (ex.offset + ex.limit < ex.total) {
      ex.offset += ex.limit;
      loadExplorer();
    }
  });
  el("pageJump").addEventListener("change", (e) => goToPage(e.target.value));
  el("pageJump").addEventListener("keydown", (e) => {
    if (e.key === "Enter") goToPage(e.target.value);
  });

  el("detailBack").addEventListener("click", goBackDetail);
  el("detailClose").addEventListener("click", closeDetail);
  el("detailOverlay").addEventListener("click", (e) => {
    if (e.target === el("detailOverlay")) closeDetail();
  });

  const previewBtn = el("downloadPreview");
  if (previewBtn) {
    previewBtn.addEventListener("click", () => {
      if (state.latestBuild) openBuildContents(state.latestBuild.id);
    });
  }
  const downloadLink = el("downloadBuild");
  if (downloadLink) downloadLink.addEventListener("click", onDownloadClick);
  document.addEventListener("click", (e) => {
    const group = el("downloadGroup");
    if (group && !group.contains(e.target)) closeDownloadMenu();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeDetail();
      closeDownloadMenu();
    }
  });

  loadFacets();
  loadExplorer();
}

/* ---------------- TU detail drawer ---------------- */

function detailIsOpen() {
  return !el("detailOverlay").classList.contains("hidden");
}

function detailEntryKey(entry) {
  if (!entry) return "";
  if (entry.type === "profile") return `${entry.type}:${entry.name}`;
  if (entry.type === "func") return `${entry.type}:${entry.tuId}:${entry.name}`;
  return `${entry.type}:${entry.id}`;
}

function detailEntryLabel(entry) {
  if (!entry) return "";
  if (entry.type === "profile") return `Profile: ${entry.name}`;
  if (entry.type === "goal") return `Goal: ${entry.id}`;
  if (entry.type === "func") return entry.name;
  if (entry.type === "audit") return `Audit: ${entry.id}`;
  if (entry.type === "stubs") return `Stubs: ${entry.id}`;
  if (entry.type === "asm") return `Instruction shape: ${entry.id}`;
  return entry.id;
}

function setCurrentDetail(entry, options = {}) {
  const wasOpen = detailIsOpen();
  if (!wasOpen) state.detailNav.stack = [];
  if (wasOpen && options.push !== false && state.detailNav.current) {
    const currentKey = detailEntryKey(state.detailNav.current);
    const nextKey = detailEntryKey(entry);
    if (currentKey && currentKey !== nextKey) state.detailNav.stack.push(state.detailNav.current);
  }
  state.detailNav.current = entry;
  updateDetailBack();
}

function updateDetailBack() {
  const btn = el("detailBack");
  if (!btn) return;
  const canGoBack = state.detailNav.stack.length > 0;
  btn.classList.toggle("hidden", !canGoBack);
  btn.disabled = !canGoBack;
  const previous = state.detailNav.stack[state.detailNav.stack.length - 1];
  btn.title = previous ? `Back to ${detailEntryLabel(previous)}` : "";
}

function goBackDetail() {
  const previous = state.detailNav.stack.pop();
  if (!previous) return updateDetailBack();
  if (previous.type === "profile") openProfile(previous.name, previous.githubUsername, { push: false });
  else if (previous.type === "goal") openGoalDetail(previous.id, { push: false });
  else if (previous.type === "func") openFunctionDetail(previous.tu, previous.fn, { push: false });
  else if (previous.type === "audit") openAuditFile(previous.id, { push: false });
  else if (previous.type === "stubs") openStubFile(previous.id, { push: false });
  else if (previous.type === "asm") openAsmFile(previous.id, { push: false });
  else openDetail(previous.id, { push: false });
}

function showDetailOverlay() {
  el("detailOverlay").classList.remove("hidden");
  document.documentElement.classList.add("detail-open");
  document.body.classList.add("detail-open");
}

function hideDetailOverlay() {
  el("detailOverlay").classList.add("hidden");
  document.documentElement.classList.remove("detail-open");
  document.body.classList.remove("detail-open");
}

async function openDetail(tuId, options = {}) {
  setCurrentDetail({ type: "tu", id: tuId }, options);
  showDetailOverlay();
  text("detailTitle", tuId);
  el("detailBody").innerHTML = '<p class="muted-text">Loading…</p>';
  try {
    const detail = await fetchJson(`/api/tu?id=${encodeURIComponent(tuId)}`, 15000);
    refreshExplorerTuRow(detail);
    renderDetail(detail);
  } catch (error) {
    el("detailBody").innerHTML = "";
    el("detailBody").appendChild(div("muted-text", `Failed to load: ${error.message}`));
  }
}

async function openGoalDetail(goalName, options = {}) {
  setCurrentDetail({ type: "goal", id: goalName }, options);
  showDetailOverlay();
  text("detailTitle", `Goal: ${goalName}`);
  el("detailBody").innerHTML = '<p class="muted-text">Loading...</p>';
  try {
    renderGoalDetail(await fetchJson(`/api/goal?name=${encodeURIComponent(goalName)}`, 15000));
  } catch (error) {
    el("detailBody").innerHTML = "";
    el("detailBody").appendChild(div("muted-text", `Failed to load: ${error.message}`));
  }
}

function openFunctionDetail(tu, fn, options = {}) {
  setCurrentDetail({ type: "func", name: fn.name, tuId: tu.id, tu, fn }, options);
  showDetailOverlay();
  text("detailTitle", `Function: ${fn.name}`);
  renderFunctionDetail(tu, fn);
}

async function openProfile(name, githubUsername, options = {}) {
  setCurrentDetail({ type: "profile", name, githubUsername }, options);
  showDetailOverlay();
  text("detailTitle", `Profile: ${name}`);
  el("detailBody").innerHTML = '<p class="muted-text">Loading...</p>';
  try {
    const profile = await fetchJson(`/api/profile?name=${encodeURIComponent(name)}`, 15000);
    state.actorProfiles[profile.name] = profile.github_username || githubUsername || profile.name;
    state.detailNav.current = {
      type: "profile",
      name: profile.name,
      githubUsername: profile.github_username || githubUsername,
    };
    text("detailTitle", `Profile: ${profile.name}`);
    renderProfileDetail(profile);
  } catch (error) {
    el("detailBody").innerHTML = "";
    el("detailBody").appendChild(div("muted-text", `Failed to load profile: ${error.message}`));
  }
}

function closeDetail() {
  hideDetailOverlay();
  state.detailNav.current = null;
  state.detailNav.stack = [];
  updateDetailBack();
}

function detailSection(title) {
  const wrap = div("detail-section");
  wrap.appendChild(div("detail-section-title", title));
  return wrap;
}

function kv(label, value) {
  const row = div("kv");
  row.appendChild(span("kv-label", label));
  const val = span("kv-value");
  if (value instanceof Node) val.appendChild(value);
  else val.textContent = value == null || value === "" ? "—" : String(value);
  row.appendChild(val);
  return row;
}

function actorWithMeta(name, login, meta) {
  const node = div("actor-stack");
  node.appendChild(actorNode(name, login));
  if (meta) node.appendChild(div("tu-meta", meta));
  return node;
}

function latestChangeNode(item) {
  if (!item.latest_change_by && !item.latest_change_at) return null;
  const node = div("actor-stack");
  if (item.latest_change_by) node.appendChild(actorNode(item.latest_change_by, item.latest_change_by_login));
  if (item.latest_change_at) node.appendChild(div("tu-meta", `${fmtTime(item.latest_change_at)} (${relTime(item.latest_change_at)})`));
  return node;
}

function contributorSection(item, title = "Contributors") {
  const contributors = item.contributors || [];
  const section = detailSection(`${title} (${contributors.length})`);
  if (!contributors.length) {
    section.appendChild(div("muted-text", "No surviving-line attribution available."));
    return section;
  }
  for (const contributor of contributors) {
    const row = div("contributor-row");
    const main = div("contributor-main");
    main.appendChild(actorNode(contributor.author, contributor.login));
    main.appendChild(div("tu-meta", `${fmtInt(contributor.lines)} surviving lines · ${contributor.percent}%`));
    const meter = div("contributor-meter");
    const fill = div("contributor-meter-fill");
    fill.style.width = `${Math.max(0, Math.min(100, Number(contributor.percent) || 0))}%`;
    meter.appendChild(fill);
    row.append(main, meter);
    section.appendChild(row);
  }
  return section;
}

function functionRangeMeta(fn) {
  if (!fn.line_range || fn.line_range.length !== 2) {
    return fn.function_range_found === false ? "using containing file attribution" : null;
  }
  return `lines ${fmtInt(fn.line_range[0])}-${fmtInt(fn.line_range[1])}`;
}

function renderFunctionDetail(tu, fn) {
  const body = el("detailBody");
  body.innerHTML = "";

  const banner = div("detail-banner");
  banner.appendChild(statusPill(fn.status));
  banner.appendChild(span("tag goal-tag", "function"));
  body.appendChild(banner);

  const facts = detailSection("Overview");
  facts.appendChild(kv("Translation Unit", tuButton(tu.id)));
  facts.appendChild(kv("Status", fn.status));
  if (fn.primary_contributor) {
    facts.appendChild(kv("Primary contributor", actorWithMeta(fn.primary_contributor, fn.primary_contributor_login, attributionMeta(fn))));
  } else if (fn.completed_by) {
    facts.appendChild(kv("Completed by", actorNode(fn.completed_by, fn.completed_by_login)));
  }
  if (fn.completed_at) facts.appendChild(kv("Completed", `${fmtTime(fn.completed_at)} (${relTime(fn.completed_at)})`));
  const range = functionRangeMeta(fn);
  if (range) facts.appendChild(kv("Attribution range", range));
  if (tu.source) facts.appendChild(kv("Source", tu.source));
  if (tu.dest_path) facts.appendChild(kv("Destination", tu.dest_path));
  body.appendChild(facts);

  body.appendChild(contributorSection(fn));

  if (fn.audit_findings && Object.keys(fn.audit_findings).length) {
    const audit = detailSection(`Console audit (${fmtInt(fn.audit_weight || 0)} high-signal)`);
    audit.appendChild(findingsList(fn.audit_findings));
    body.appendChild(audit);
  }

  const related = detailSection("Containing TU");
  const row = div("dep-row clickable");
  row.appendChild(span("dep-name", tu.id));
  row.appendChild(statusPill(tu.status));
  row.addEventListener("click", () => openDetail(tu.id));
  related.appendChild(row);
  body.appendChild(related);
}

function profileMetric(label, value, meta) {
  const node = div("profile-metric");
  node.appendChild(div("profile-metric-value", value));
  node.appendChild(div("profile-metric-label", label));
  if (meta) node.appendChild(div("tu-meta", meta));
  return node;
}

function profileBarList(title, items, labelKey, valueKey, emptyText, metaKey) {
  const section = detailSection(title);
  if (!items || !items.length) {
    section.appendChild(div("muted-text", emptyText));
    return section;
  }
  const max = Math.max(...items.map((item) => Number(item[valueKey] || 0)), 1);
  for (const item of items) {
    const row = div("profile-bar-row");
    const label = div("profile-bar-label");
    label.appendChild(span("profile-bar-name", item[labelKey] || "unknown"));
    const meta =
      metaKey && Number(item[metaKey] || 0) > 0 ? ` (${fmtInt(item[metaKey])} lines)` : "";
    label.appendChild(span("profile-bar-value", `${fmtInt(item[valueKey])}${meta}`));
    const bar = div("profile-bar");
    const fill = div("profile-bar-fill");
    fill.style.width = `${Math.max(4, (Number(item[valueKey] || 0) / max) * 100)}%`;
    bar.appendChild(fill);
    row.append(label, bar);
    section.appendChild(row);
  }
  return section;
}

function profileSparkline(points) {
  const wrap = div("profile-spark");
  if (!points || !points.length) {
    wrap.appendChild(div("muted-text", "No activity yet."));
    return wrap;
  }
  const max = Math.max(...points.map((point) => Number(point.count || 0)), 1);
  for (const point of points.slice(-28)) {
    const bar = div("profile-spark-bar");
    bar.style.height = `${Math.max(8, (Number(point.count || 0) / max) * 76)}px`;
    bar.title = `${point.date}: ${fmtInt(point.count)} TUs`;
    wrap.appendChild(bar);
  }
  return wrap;
}

function renderProfileDetail(profile) {
  const body = el("detailBody");
  body.innerHTML = "";
  const summary = profile.summary || {};

  const banner = div("detail-banner");
  banner.appendChild(span("tag goal-tag", profile.registered ? "registered" : "external contributor"));
  if (profile.is_admin) banner.appendChild(span("tag goal-tag", "admin"));
  if (profile.worker_active === false) banner.appendChild(span("tag goal-tag", "disabled"));
  if (profile.github_username) {
    const link = document.createElement("a");
    link.className = "profile-external";
    link.href = `https://github.com/${profile.github_username}`;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = `GitHub: ${profile.github_username}`;
    banner.appendChild(link);
  }
  body.appendChild(banner);

  const metrics = div("profile-metrics");
  metrics.appendChild(profileMetric("Active TUs", fmtInt(summary.active_tus)));
  metrics.appendChild(profileMetric("Contributed TUs", fmtInt(summary.contributed_tus)));
  metrics.appendChild(profileMetric("Contributed funcs", fmtInt(summary.contributed_funcs)));
  metrics.appendChild(profileMetric("Surviving lines", fmtInt(summary.contributed_lines)));
  body.appendChild(metrics);

  const overview = detailSection("Overview");
  overview.appendChild(kv("Last activity", summary.last_activity ? `${fmtTime(summary.last_activity)} (${relTime(summary.last_activity)})` : null));
  if (profile.aliases && profile.aliases.length) overview.appendChild(kv("Known aliases", profile.aliases.join(", ")));
  const coverage = profile.attribution_cache || {};
  overview.appendChild(
    kv(
      "Attribution cache",
      `${fmtInt(coverage.file_cached || 0)}/${fmtInt(coverage.file_total || 0)} TUs, ${fmtInt(
        coverage.function_cached || 0,
      )}/${fmtInt(coverage.function_total || 0)} funcs`,
    ),
  );
  body.appendChild(overview);

  const activity = detailSection("Activity");
  activity.appendChild(profileSparkline(profile.activity_by_day || []));
  body.appendChild(activity);

  const statusItems = Object.entries(profile.status_counts || {})
    .filter(([, value]) => Number(value || 0) > 0)
    .map(([status, count]) => ({ name: status.replace("_", " "), count }));
  body.appendChild(profileBarList("Contributed TU Status", statusItems, "name", "count", "No contributed TUs found in the current attribution cache."));
  body.appendChild(profileBarList("Actions", profile.action_counts || [], "action", "count", "No recorded events for this actor."));
  body.appendChild(profileBarList("Sources", profile.sources || [], "name", "tus", "No source breakdown available.", "lines"));
  body.appendChild(profileBarList("Goals", profile.goals || [], "name", "tus", "No goal-linked contributions found."));

  body.appendChild(profileTuList("Active Work", profile.active_work || [], "No active claims."));
  body.appendChild(profileTuList("Top TUs", profile.top_tus || [], "No TU attribution found."));
  body.appendChild(profileFuncList("Top Functions", profile.top_funcs || []));
  body.appendChild(profileEventList(profile.recent_events || []));
}

function profileTuList(title, items, emptyText) {
  const section = detailSection(`${title} (${items.length})`);
  if (!items.length) {
    section.appendChild(div("muted-text", emptyText));
    return section;
  }
  for (const item of items) {
    const row = div("dep-row clickable profile-tu-row");
    const main = div("goal-tu-main");
    main.appendChild(span("dep-name", item.id));
    const meta = [];
    if (item.source) meta.push(item.source);
    if (Number(item.lines || 0) > 0) meta.push(`${fmtInt(item.lines)} lines`);
    else if (item.basis === "review_pass") meta.push("reviewed");
    if (item.percent) meta.push(`${item.percent}%`);
    if (item.dest_path) meta.push(item.dest_path);
    main.appendChild(div("tu-meta", meta.join(" | ")));
    row.appendChild(main);
    if (item.status) row.appendChild(statusPill(item.status));
    row.addEventListener("click", () => openDetail(item.id));
    section.appendChild(row);
  }
  return section;
}

function profileFuncList(title, items) {
  const section = detailSection(`${title} (${items.length})`);
  if (!items.length) {
    section.appendChild(div("muted-text", "No function attribution found."));
    return section;
  }
  for (const item of items) {
    const row = div("dep-row clickable profile-tu-row");
    const main = div("goal-tu-main");
    main.appendChild(span("dep-name", item.name));
    const meta = [];
    if (item.tu_id) meta.push(item.tu_id);
    if (Number(item.lines || 0) > 0) meta.push(`${fmtInt(item.lines)} lines`);
    else if (item.basis === "completed_by") meta.push("completed");
    if (item.percent) meta.push(`${item.percent}%`);
    main.appendChild(div("tu-meta", meta.join(" | ")));
    row.appendChild(main);
    if (item.status) row.appendChild(statusPill(item.status));
    row.addEventListener("click", () => openDetail(item.tu_id));
    section.appendChild(row);
  }
  return section;
}

function profileEventList(events) {
  const section = detailSection(`Recent Events (${events.length})`);
  if (!events.length) {
    section.appendChild(div("muted-text", "No events recorded for this actor."));
    return section;
  }
  for (const event of events) {
    const row = div("profile-event-row");
    row.appendChild(div("profile-event-time", event.ts ? fmtTime(event.ts) : "none"));
    const main = div("goal-tu-main");
    main.appendChild(div("event-title", event.action || "event"));
    const target = event.tu_id || detailText(event.detail) || "server";
    main.appendChild(div("tu-meta", target));
    row.appendChild(main);
    if (event.tu_id) {
      row.classList.add("clickable");
      row.addEventListener("click", () => openDetail(event.tu_id));
    }
    section.appendChild(row);
  }
  return section;
}

function renderDetail(d) {
  const body = el("detailBody");
  body.innerHTML = "";

  // Status banner
  const banner = div("detail-banner");
  banner.appendChild(statusPill(d.status));
  if (d.goals && d.goals.length) {
    for (const g of d.goals) banner.appendChild(span("tag goal-tag", g));
  }
  body.appendChild(banner);

  // Facts the agent receives
  const facts = detailSection("Overview");
  facts.appendChild(kv("Source", d.source));
  facts.appendChild(kv("Functions", fmtInt(d.n_funcs)));
  facts.appendChild(kv("Decfigs", fmtInt(d.n_decfigs)));
  facts.appendChild(kv("Active claim", d.owner ? actorNode(d.owner) : null));
  if (d.primary_contributor) {
    facts.appendChild(kv("Primary contributor", actorWithMeta(d.primary_contributor, d.primary_contributor_login, attributionMeta(d))));
  } else if (d.completed_by) {
    facts.appendChild(kv("Completed by", actorNode(d.completed_by, d.completed_by_login)));
  }
  const latest = latestChangeNode(d);
  if (latest) facts.appendChild(kv("Latest change", latest));
  else if (d.last_actor) facts.appendChild(kv("Last actor", actorNode(d.last_actor)));
  facts.appendChild(kv("Updated", d.updated_at ? `${fmtTime(d.updated_at)} (${relTime(d.updated_at)})` : null));
  // Prefer the server-resolved repo_path: it points at the file that actually
  // exists (a .h destination is inlined into its .cpp), so the link never 404s.
  const repoPath = d.repo_path || destToRepoPath(d.dest_path);
  if (repoPath) {
    const a = document.createElement("a");
    a.href = ghBlobUrl(repoPath);
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = d.dest_path;
    facts.appendChild(kv("Destination", a));
  } else {
    facts.appendChild(kv("Destination", d.dest_path));
  }
  if (d.notes) facts.appendChild(kv("Notes", d.notes));
  body.appendChild(facts);

  body.appendChild(contributorSection(d));

  // Dependencies
  const deps = detailSection(`Dependencies (${(d.deps || []).length})`);
  if (!d.deps || !d.deps.length) deps.appendChild(div("muted-text", "No recorded dependencies."));
  else d.deps.forEach((dep) => deps.appendChild(depRow(dep, dep.status)));
  body.appendChild(deps);

  // Dependents
  const dependents = detailSection(`Dependents (${(d.dependents || []).length})`);
  if (!d.dependents || !d.dependents.length)
    dependents.appendChild(div("muted-text", "Nothing depends on this TU."));
  else d.dependents.forEach((dep) => dependents.appendChild(depRow(dep, dep.status)));
  body.appendChild(dependents);

  body.appendChild(tuAuditSection(d));

  // Functions
  const funcs = detailSection(`Functions (${(d.funcs || []).length})`);
  if (!d.funcs || !d.funcs.length) funcs.appendChild(div("muted-text", "No functions recorded."));
  else
    d.funcs.forEach((fn) => {
      const row = div("dep-row clickable");
      row.appendChild(span("dep-name", fn.name));
      row.appendChild(statusPill(fn.status));
      if (fn.audit_weight) row.appendChild(span("pill cat-high", `${fmtInt(fn.audit_weight)} audit`));
      if (fn.primary_contributor) row.appendChild(actorNode(fn.primary_contributor, fn.primary_contributor_login));
      else if (fn.completed_by) row.appendChild(actorNode(fn.completed_by, fn.completed_by_login));
      row.addEventListener("click", (event) => {
        if (event.target.closest("a, button")) return;
        openFunctionDetail(d, fn);
      });
      funcs.appendChild(row);
    });
  body.appendChild(funcs);
}

function renderGoalDetail(goal) {
  const body = el("detailBody");
  body.innerHTML = "";
  const total = Number(goal.total || 0);
  const done = Number(goal.done || 0);
  const remaining = Number(goal.remaining_count || 0);
  const counts = goal.counts || {};

  const banner = div("detail-banner");
  banner.appendChild(span("tag goal-tag", goal.category || "goal"));
  if (goal.source) banner.appendChild(span("tag goal-tag", goal.source));
  body.appendChild(banner);

  const overview = detailSection("Overview");
  overview.appendChild(kv("Progress", `${fmtInt(done)} / ${fmtInt(total)} done`));
  overview.appendChild(kv("Remaining", fmtInt(remaining)));
  overview.appendChild(kv("Ready now", fmtInt((goal.ready || []).length)));
  overview.appendChild(kv("In progress", fmtInt((goal.active || []).length)));
  overview.appendChild(kv("Waiting review", fmtInt((goal.waiting_review || []).length)));
  overview.appendChild(kv("Blocked", fmtInt((goal.blocked || []).length)));
  if (goal.description) overview.appendChild(kv("Description", goal.description));
  body.appendChild(overview);

  const status = detailSection("Status Breakdown");
  for (const key of ["todo", "in_progress", "compiled", "blocked", "done"]) {
    const row = div("dep-row");
    row.appendChild(statusPill(key));
    row.appendChild(span("dep-name", `${fmtInt(counts[key] || 0)} TUs`));
    status.appendChild(row);
  }
  body.appendChild(status);

  body.appendChild(goalTuSection("Ready Next", goal.ready, "No ready TUs inside this goal."));
  body.appendChild(goalTuSection("In Progress", goal.active, "No active claims inside this goal."));
  body.appendChild(goalTuSection("Waiting Review", goal.waiting_review, "No compiled TUs waiting for review."));
  body.appendChild(goalTuSection("Blocked", goal.blocked, "No blocked TUs inside this goal."));
  body.appendChild(goalTuSection("Dependency Locked", goal.locked, "No remaining TUs are waiting on goal dependencies."));
  body.appendChild(goalTuSection("All Remaining", goal.remaining, "This goal is complete."));
}

function goalTuSection(title, items, emptyText) {
  const section = detailSection(`${title} (${(items || []).length})`);
  if (!items || !items.length) {
    section.appendChild(div("muted-text", emptyText));
    return section;
  }
  for (const item of items) section.appendChild(goalTuRow(item));
  return section;
}

function goalTuRow(item) {
  const row = div("dep-row clickable goal-tu-row");
  const main = div("goal-tu-main");
  main.appendChild(span("dep-name", item.id));
  const meta = [];
  if (item.dest_path) meta.push(item.dest_path);
  if (item.unresolved_deps) meta.push(`${fmtInt(item.unresolved_deps)} unresolved deps`);
  else if (item.status === "todo") meta.push("ready");
  if (item.owner) meta.push(`claimed by ${item.owner}`);
  if (item.notes) meta.push(item.notes);
  main.appendChild(div("tu-meta", meta.join(" | ")));
  row.appendChild(main);
  row.appendChild(statusPill(item.status));
  row.addEventListener("click", () => openDetail(item.id));
  return row;
}

function depRow(dep, status) {
  const row = div("dep-row clickable");
  row.appendChild(span("dep-name", dep.id));
  if (dep.weight) row.appendChild(span("dep-weight", `×${dep.weight}`));
  if (status) row.appendChild(statusPill(status));
  // Only navigable if it's a real TU (has a known status).
  if (status) row.addEventListener("click", () => openDetail(dep.id));
  else row.classList.remove("clickable");
  return row;
}

/* ---------------- Live stream ---------------- */

function connectStream() {
  if (!window.EventSource) {
    state.refreshTimer = window.setInterval(refresh, 15000);
    return;
  }
  const source = new EventSource(`/events/stream?after=${state.lastEventId}`);
  state.eventSource = source;
  source.addEventListener("connected", () => {
    setConnection("online", "Live");
    refresh();
  });
  source.addEventListener("work-event", (event) => {
    state.lastEventId = Math.max(state.lastEventId, Number(event.lastEventId) || 0);
    refresh();
  });
  source.addEventListener("tick", () => setConnection("online", "Live"));
  source.onerror = () => {
    setConnection("offline", "Reconnecting");
  };
  state.refreshTimer = window.setInterval(refresh, 30000);
}

// Replace the browser-native <select> popups with themed dropdowns. The native
// <select> stays in the DOM as the source of truth, so all the .value reads,
// "change" handlers, and .tus-only/.hidden toggling elsewhere keep working.
function enhanceSelect(sel) {
  const wrap = document.createElement("div");
  wrap.className = "cs-wrap";
  sel.parentNode.insertBefore(wrap, sel);
  wrap.appendChild(sel);
  sel.classList.add("cs-native");

  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "filter cs-trigger";
  trigger.setAttribute("aria-haspopup", "listbox");
  wrap.appendChild(trigger);

  const menu = document.createElement("ul");
  menu.className = "cs-menu";
  menu.setAttribute("role", "listbox");
  wrap.appendChild(menu);

  const close = () => wrap.classList.remove("open");

  const syncLabel = () => {
    const opt = sel.options[sel.selectedIndex];
    trigger.textContent = opt ? opt.textContent : "";
  };

  const markSelected = () => {
    [...menu.children].forEach((li, i) => li.classList.toggle("selected", i === sel.selectedIndex));
  };

  const buildMenu = () => {
    menu.replaceChildren();
    [...sel.options].forEach((opt, i) => {
      const li = document.createElement("li");
      li.className = "cs-option";
      li.setAttribute("role", "option");
      li.textContent = opt.textContent;
      li.addEventListener("click", () => {
        if (sel.selectedIndex !== i) {
          sel.selectedIndex = i;
          sel.dispatchEvent(new Event("change", { bubbles: true }));
        }
        syncLabel();
        markSelected();
        close();
      });
      menu.appendChild(li);
    });
    syncLabel();
    markSelected();
  };

  trigger.addEventListener("click", (e) => {
    e.stopPropagation();
    const willOpen = !wrap.classList.contains("open");
    for (const w of document.querySelectorAll(".cs-wrap.open")) w.classList.remove("open");
    wrap.classList.toggle("open", willOpen);
  });

  // fillSelect() repopulates options dynamically; rebuild the menu to match.
  new MutationObserver(buildMenu).observe(sel, { childList: true });
  buildMenu();
}

for (const sel of document.querySelectorAll("select.filter")) enhanceSelect(sel);
document.addEventListener("click", () => {
  for (const w of document.querySelectorAll(".cs-wrap.open")) w.classList.remove("open");
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    for (const w of document.querySelectorAll(".cs-wrap.open")) w.classList.remove("open");
  }
});

refresh();
connectStream();
refreshGithub();
refreshDownload();
initExplorer();
initEvidence();
initMiniPanels();
// GitHub data changes slowly and is cached server-side; poll gently.
state.githubTimer = window.setInterval(refreshGithub, 90000);
// New builds land only when CI publishes; a slow poll keeps the button fresh.
state.downloadTimer = window.setInterval(refreshDownload, 120000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});

/* ---------------- Evidence layer: audit + stub inventory ---------------- */

// Categories the audit itself documents as reliable, in the order they are shown.
// Anything else (log strings, uncited data, the parameter hint) is context, not a count.
const AUDIT_HIGH = ["NO_BODY", "MISSING_CASE", "EXTRA_CASE", "MISSING_EVENT", "MISSING_CALLEE", "MISSING_ASSERT"];
const AUDIT_ORDER = [
  ...AUDIT_HIGH,
  "MISSING_EVENT?",
  "MISSING_STRING",
  "UNCITED_DATA",
  "FEWER_PARAMS",
  "INFO_TRUNCATED_CALLEES",
  "INFO_UNNAMED_CALLEES",
];
const AUDIT_LABELS = {
  NO_BODY: "no body",
  MISSING_CASE: "missing case ids",
  EXTRA_CASE: "misfiled case ids",
  MISSING_EVENT: "missing event posts",
  "MISSING_EVENT?": "event posts (unsure)",
  MISSING_CALLEE: "missing callees",
  MISSING_ASSERT: "missing asserts",
  MISSING_STRING: "missing strings",
  UNCITED_DATA: "uncited data",
  FEWER_PARAMS: "fewer params (hint)",
  INFO_TRUNCATED_CALLEES: "truncated callee names",
  INFO_UNNAMED_CALLEES: "unnamed callees",
};

function auditCatClass(cat) {
  if (AUDIT_HIGH.includes(cat)) return "cat-high";
  if (cat === "MISSING_EVENT?" || cat === "MISSING_STRING") return "cat-mid";
  return "cat-low";
}

function sortedCats(findings) {
  return Object.keys(findings || {}).sort((a, b) => {
    const ia = AUDIT_ORDER.indexOf(a);
    const ib = AUDIT_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });
}

function findingsList(findings) {
  const list = document.createElement("ul");
  list.className = "audit-findings";
  for (const cat of sortedCats(findings)) {
    const items = findings[cat] || [];
    const li = document.createElement("li");
    if (AUDIT_HIGH.includes(cat)) li.classList.add("high");
    li.appendChild(span("audit-cat", `${AUDIT_LABELS[cat] || cat} (${fmtInt(items.length)})`));
    li.appendChild(span("audit-items", items.join("; ")));
    list.appendChild(li);
  }
  return list;
}

function numCell(value, title) {
  const cell = document.createElement("td");
  const n = Number(value || 0);
  cell.className = n ? "num-cell" : "num-cell zero";
  cell.textContent = fmtInt(n);
  if (title) cell.title = title;
  return cell;
}

function fileRepoLink(file) {
  const a = document.createElement("a");
  a.href = ghBlobUrl(`src/${file}`);
  a.target = "_blank";
  a.rel = "noopener";
  a.textContent = file;
  return a;
}

function verifiedTrend(history) {
  const points = (history || []).filter((point) => point.paired != null);
  if (points.length < 2) return null;
  const last = points[points.length - 1];
  const prev = points[points.length - 2];
  const dClean = Number(last.clean || 0) - Number(prev.clean || 0);
  const dWeight = Number(last.weight || 0) - Number(prev.weight || 0);
  const parts = [];
  if (dClean) parts.push(`${dClean > 0 ? "+" : ""}${fmtInt(dClean)} verified`);
  if (dWeight) parts.push(`${dWeight > 0 ? "+" : ""}${fmtInt(dWeight)} findings`);
  if (!parts.length) return { text: "unchanged since the previous commit", cls: "" };
  const good = dClean > 0 || dWeight < 0;
  return {
    text: `${parts.join(", ")} since the previous audited commit`,
    cls: good ? "up" : "down",
  };
}

async function openAuditFile(file, options = {}) {
  setCurrentDetail({ type: "audit", id: file }, options);
  showDetailOverlay();
  text("detailTitle", `Audit: ${file}`);
  el("detailBody").innerHTML = '<p class="muted-text">Loading…</p>';
  try {
    renderAuditFile(await fetchJson(`/api/audit/functions?file=${encodeURIComponent(file)}`, 15000));
  } catch (error) {
    el("detailBody").innerHTML = "";
    el("detailBody").appendChild(div("muted-text", `Failed to load: ${error.message}`));
  }
}

async function openStubFile(file, options = {}) {
  setCurrentDetail({ type: "stubs", id: file }, options);
  showDetailOverlay();
  text("detailTitle", `Stubs: ${file}`);
  el("detailBody").innerHTML = '<p class="muted-text">Loading…</p>';
  try {
    renderStubFile(await fetchJson(`/api/stubs?file=${encodeURIComponent(file)}`, 15000));
  } catch (error) {
    el("detailBody").innerHTML = "";
    el("detailBody").appendChild(div("muted-text", `Failed to load: ${error.message}`));
  }
}

function auditRollupChips(rollup) {
  const wrap = div("audit-summary");
  const chips = [
    ["weight", "Findings", "done"],
    ["no_body", "No body", "blocked"],
    ["missing_case", "Case ids", "progress"],
    ["missing_event", "Event posts", "progress"],
    ["missing_callee", "Callees", "progress"],
    ["missing_assert", "Asserts", "todo"],
  ];
  for (const [key, label, cls] of chips) {
    const chip = div(`chip ${cls}`);
    chip.appendChild(span("", fmtInt(rollup[key] || 0)));
    const l = document.createElement("label");
    l.textContent = label;
    chip.appendChild(l);
    wrap.appendChild(chip);
  }
  return wrap;
}

function renderAuditFile(data) {
  const body = el("detailBody");
  body.innerHTML = "";
  const rollup = data.rollup || {};

  const banner = div("detail-banner");
  banner.appendChild(span("tag goal-tag", "console audit"));
  if (data.tu_id) banner.appendChild(tuButton(data.tu_id, "tag goal-tag"));
  body.appendChild(banner);

  const facts = detailSection("Overview");
  facts.appendChild(kv("File", fileRepoLink(data.file)));
  facts.appendChild(kv("Functions", `${fmtInt(rollup.functions || 0)} with findings`));
  facts.appendChild(auditRollupChips(rollup));
  facts.appendChild(
    div(
      "audit-meta",
      "Each function is the reconstructed body (plus the PC-only helpers it calls) compared with the console's pseudocode. A missing case id, event post, callee or body is a difference; missing log strings and uncited data are context. Click a function to open its findings.",
    ),
  );
  body.appendChild(facts);

  const items = data.items || [];
  const list = detailSection(`Functions (${fmtInt(items.length)})`);
  const filters = [{ key: "high", label: "high-signal", cls: "red", test: (fn) => fn.weight > 0 }];
  for (const cat of AUDIT_ORDER) {
    if (items.some((fn) => fn.findings && fn.findings[cat])) {
      filters.push({ key: cat, label: AUDIT_LABELS[cat] || cat, cls: AUDIT_HIGH.includes(cat) ? "red" : "grey",
        test: (fn) => Boolean(fn.findings && fn.findings[cat]) });
    }
  }
  filters.push({ key: "flagged", label: "flagged in source", cls: "blue", test: (fn) => Boolean(fn.flagged) });
  list.appendChild(functionBrowser({
    items,
    key: (fn) => fn.name,
    search: (fn) => `${fn.name} ${Object.values(fn.findings || {}).flat().join(" ")}`,
    filters,
    sorts: [
      { key: "weight", label: "most high-signal first", cmp: (a, b) => (b.weight || 0) - (a.weight || 0) || (a.line || 0) - (b.line || 0) },
      { key: "line", label: "source order", cmp: (a, b) => (a.line || 0) - (b.line || 0) },
      { key: "name", label: "by name", cmp: (a, b) => a.name.localeCompare(b.name) },
    ],
    row: (fn) => {
      const head = [span("dep-name", fn.name)];
      if (fn.weight) head.push(span("pill cat-high", `${fmtInt(fn.weight)} high-signal`));
      if (fn.flagged) head.push(span("pill todo", "flagged in source"));
      if (fn.helpers) head.push(span("pill todo", `+${fmtInt(fn.helpers)} helpers`));
      const where = [];
      if (fn.addr) where.push(`X360 ${fn.addr}`);
      if (fn.line) where.push(`line ${fmtInt(fn.line)}`);
      if (where.length) head.push(span("tu-meta", where.join(" \u00b7 ")));
      return { head, body: findingsList(fn.findings || {}) };
    },
    emptyText: "No findings match.",
  }));
  body.appendChild(list);
}

function renderStubFile(data) {
  const body = el("detailBody");
  body.innerHTML = "";
  const rollup = data.rollup || {};

  const banner = div("detail-banner");
  banner.appendChild(span("tag goal-tag", "stub inventory"));
  if (data.tu_id) banner.appendChild(tuButton(data.tu_id, "tag goal-tag"));
  body.appendChild(banner);

  const facts = detailSection("Overview");
  facts.appendChild(kv("File", fileRepoLink(data.file)));
  facts.appendChild(kv("Stubs", `${fmtInt(rollup.stubs || 0)} (${fmtInt(rollup.high || 0)} high, ${fmtInt(rollup.medium || 0)} medium, ${fmtInt(rollup.low || 0)} low)`));
  facts.appendChild(kv("Live today", `${fmtInt(rollup.live || 0)} reached by reconstructed code`));
  facts.appendChild(kv("Console lines", `${fmtInt(rollup.console_lines || 0)} of work behind them`));
  facts.appendChild(
    div(
      "audit-meta",
      "High: the file or the body says it is a stand-in, or it traps. Medium: a trivial body under a softer marker. Low: a trivial, unmarked body whose console function does real work \u2014 possibly a legitimate empty default. Click a stub for its reason and callers.",
    ),
  );
  body.appendChild(facts);

  const items = data.items || [];
  const tierRank = { HIGH: 0, MEDIUM: 1, LOW: 2 };
  const list = detailSection(`Stub bodies (${fmtInt(items.length)})`);
  list.appendChild(functionBrowser({
    items,
    key: (st) => `${st.name}@${st.line}`,
    search: (st) => `${st.name} ${st.why || ""} ${(st.live_callers || []).join(" ")}`,
    filters: [
      { key: "HIGH", label: "high", cls: "red", test: (st) => st.tier === "HIGH" },
      { key: "MEDIUM", label: "medium", cls: "amber", test: (st) => st.tier === "MEDIUM" },
      { key: "LOW", label: "low", cls: "grey", test: (st) => st.tier === "LOW" },
      { key: "live", label: "live today", cls: "blue", test: (st) => (st.live_callers || []).length > 0 },
    ],
    sorts: [
      { key: "tier", label: "tier, then source order", cmp: (a, b) => (tierRank[a.tier] ?? 3) - (tierRank[b.tier] ?? 3) || (a.line || 0) - (b.line || 0) },
      { key: "console", label: "biggest console body first", cmp: (a, b) => (b.console_lines || 0) - (a.console_lines || 0) },
      { key: "name", label: "by name", cmp: (a, b) => a.name.localeCompare(b.name) },
    ],
    row: (st) => {
      const head = [span("dep-name", st.name), tierPill(st.tier)];
      if (st.live_callers && st.live_callers.length) head.push(span("pill live", "live today"));
      const where = [];
      if (st.addr) where.push(`X360 ${st.addr}`);
      if (st.line) where.push(`line ${fmtInt(st.line)}`);
      if (st.console_lines != null) where.push(`${fmtInt(st.console_lines)} console lines`);
      if (where.length) head.push(span("tu-meta", where.join(" \u00b7 ")));
      const detail = div("");
      if (st.why) detail.appendChild(div("audit-meta", `why: ${st.why}`));
      if (st.live_callers && st.live_callers.length) detail.appendChild(div("audit-meta", `called by reconstructed code: ${st.live_callers.join(", ")}`));
      return { head, body: detail.childNodes.length ? detail : null };
    },
    emptyText: "No stubs match.",
  }));
  body.appendChild(list);
}

async function openAsmFile(file, options = {}) {
  setCurrentDetail({ type: "asm", id: file }, options);
  showDetailOverlay();
  text("detailTitle", `Instruction shape: ${file}`);
  el("detailBody").innerHTML = '<p class="muted-text">Loading\u2026</p>';
  try {
    renderAsmFile(await fetchJson(`/api/asm/functions?file=${encodeURIComponent(file)}`, 15000));
  } catch (error) {
    el("detailBody").innerHTML = "";
    el("detailBody").appendChild(div("muted-text", `Failed to load: ${error.message}`));
  }
}

function renderAsmFile(data) {
  const body = el("detailBody");
  body.innerHTML = "";
  const rollup = data.rollup || {};

  const banner = div("detail-banner");
  banner.appendChild(span("tag goal-tag", "instruction shape"));
  if (data.tu_id) banner.appendChild(tuButton(data.tu_id, "tag goal-tag"));
  body.appendChild(banner);

  const facts = detailSection("Overview");
  facts.appendChild(kv("File", fileRepoLink(data.file)));
  facts.appendChild(kv("In the built exe", `${fmtInt(rollup.functions || 0)} functions \u00b7 mean score ${rollup.mean_score != null ? rollup.mean_score : "\u2013"}`));
  const tiers = div("audit-summary");
  for (const [key, label, cls] of [["a", "A same shape", "done"], ["b", "B close", "compiled"], ["c", "C diverges", "blocked"], ["t", "T trivial", "todo"], ["flagged", "flagged in source", "todo"]]) {
    const chip = div(`chip ${cls}`);
    chip.appendChild(span("", fmtInt(rollup[key] || 0)));
    const l = document.createElement("label");
    l.textContent = label;
    chip.appendChild(l);
    tiers.appendChild(chip);
  }
  facts.appendChild(tiers);
  facts.appendChild(div("audit-meta",
    "Each function of the exe CI built against the console's machine code: the named callees, the conditional-branch count, the constants. A tier C is a diff to read, not a verdict: click a function for the callees and constants that exist on one side only."));
  body.appendChild(facts);

  const items = data.items || [];
  const tierRank = { C: 0, B: 1, A: 2, T: 3 };
  const list = detailSection(`Functions (${fmtInt(items.length)})`);
  list.appendChild(functionBrowser({
    items,
    key: (fn) => fn.name,
    search: (fn) => {
      const d = fn.diff || {};
      return `${fn.name} ${(d.calls_only_console || []).join(" ")} ${(d.calls_only_pc || []).join(" ")}`;
    },
    filters: [
      { key: "A", label: "A same shape", cls: "green", test: (fn) => fn.tier === "A" },
      { key: "B", label: "B close", cls: "amber", test: (fn) => fn.tier === "B" },
      { key: "C", label: "C diverges", cls: "red", test: (fn) => fn.tier === "C" },
      { key: "T", label: "T trivial", cls: "grey", test: (fn) => fn.tier === "T" },
      { key: "flagged", label: "flagged in source", cls: "blue", test: (fn) => Boolean(fn.flags && fn.flags.total) },
    ],
    sorts: [
      { key: "worst", label: "worst score first", cmp: (a, b) => (tierRank[a.tier] ?? 4) - (tierRank[b.tier] ?? 4) || (a.score ?? 101) - (b.score ?? 101) },
      { key: "size", label: "biggest console body first", cmp: (a, b) => ((b.counts || {}).n || [0])[0] - ((a.counts || {}).n || [0])[0] },
      { key: "name", label: "by name", cmp: (a, b) => a.name.localeCompare(b.name) },
    ],
    row: (fn) => {
      const head = [span("dep-name", fn.name), asmTierPill(fn.tier)];
      if (fn.score != null) head.push(span("pill todo", `score ${fn.score}`));
      if (fn.flags && fn.flags.total) head.push(span("pill todo", `flagged ${fmtInt(fn.flags.total)}`));
      const c = fn.counts || {};
      const where = [];
      if (fn.addr) where.push(`X360 ${fn.addr}`);
      if (c.n) where.push(`${fmtInt(c.n[0])} console / ${fmtInt(c.n[1])} pc insns`);
      if (c.cond) where.push(`branches ${fmtInt(c.cond[0])} / ${fmtInt(c.cond[1])}`);
      if (c.calls) where.push(`calls ${fmtInt(c.calls[0])} / ${fmtInt(c.calls[1])}`);
      if (where.length) head.push(span("tu-meta", where.join(" \u00b7 ")));
      const detail = asmDiffList(fn);
      return { head, body: detail.childNodes.length ? detail : null };
    },
    emptyText: "No functions match.",
  }));
  body.appendChild(list);
}

/* ---- function browser: one file's functions in a drawer, filterable, collapsed until clicked ---- */
function functionBrowser(opts) {
  const wrap = div("fb");
  const controls = div("fb-controls");
  const search = document.createElement("input");
  search.type = "search";
  search.className = "search-input";
  search.placeholder = opts.placeholder || "Filter by function name or content\u2026";
  search.autocomplete = "off";
  controls.appendChild(search);
  const chips = div("fb-chips");
  controls.appendChild(chips);
  const tools = div("fb-tools");
  const sortSel = document.createElement("select");
  sortSel.className = "fb-sort";
  sortSel.title = "Sort";
  for (const so of opts.sorts || []) {
    const o = document.createElement("option");
    o.value = so.key;
    o.textContent = so.label;
    sortSel.appendChild(o);
  }
  if ((opts.sorts || []).length > 1) tools.appendChild(sortSel);
  const expandBtn = document.createElement("button");
  expandBtn.type = "button";
  expandBtn.className = "filter";
  expandBtn.textContent = "Expand all";
  const collapseBtn = document.createElement("button");
  collapseBtn.type = "button";
  collapseBtn.className = "filter";
  collapseBtn.textContent = "Collapse all";
  const count = span("fb-count", "");
  tools.append(expandBtn, collapseBtn, count);
  controls.appendChild(tools);
  const list = div("fb-list");
  wrap.append(controls, list);

  const st = { filter: "", q: "", sort: ((opts.sorts || [])[0] || {}).key, open: new Set() };
  const all = opts.items || [];
  let shown = [];

  function render() {
    clearNode(list);
    clearNode(chips);
    for (const f of [{ key: "", label: "all", cls: "", test: () => true }, ...(opts.filters || [])]) {
      const n = f.key ? all.filter(f.test).length : all.length;
      if (f.key && !n) continue;
      chips.appendChild(evidenceChip(f.label, n, st.filter === f.key, f.cls || "", () => {
        st.filter = st.filter === f.key ? "" : f.key;
        render();
      }));
    }
    const q = st.q.toLowerCase();
    const filt = (opts.filters || []).find((f) => f.key === st.filter);
    shown = all.filter((it) => (!filt || filt.test(it)) && (!q || (opts.search(it) || "").toLowerCase().includes(q)));
    const sorter = (opts.sorts || []).find((so) => so.key === st.sort);
    if (sorter) shown = shown.slice().sort(sorter.cmp);
    count.textContent = shown.length === all.length ? `${fmtInt(all.length)}` : `${fmtInt(shown.length)} of ${fmtInt(all.length)}`;
    if (!shown.length) list.appendChild(div("muted-text", opts.emptyText || "Nothing matches."));
    for (const it of shown) {
      const key = opts.key(it);
      const r = opts.row(it);
      const row = div(`fb-row${st.open.has(key) ? " open" : ""}${r.body ? "" : " no-body"}`);
      const head = div("fb-head");
      head.appendChild(span("fb-caret", ""));
      for (const n of r.head) head.appendChild(n);
      row.appendChild(head);
      if (r.body) {
        const bodyEl = div("fb-body");
        bodyEl.appendChild(r.body);
        row.appendChild(bodyEl);
        head.addEventListener("click", (e) => {
          if (e.target.closest("a, button")) return;
          const open = !row.classList.contains("open");
          row.classList.toggle("open", open);
          if (open) st.open.add(key); else st.open.delete(key);
        });
      }
      list.appendChild(row);
    }
  }
  search.addEventListener("input", () => { st.q = search.value.trim(); render(); });
  sortSel.addEventListener("change", () => { st.sort = sortSel.value; render(); });
  expandBtn.addEventListener("click", () => { for (const it of shown) st.open.add(opts.key(it)); render(); });
  collapseBtn.addEventListener("click", () => { st.open.clear(); render(); });
  render();
  return wrap;
}

function tierPill(tier) {
  return span(`pill tier-${tier}`, String(tier || "").toLowerCase());
}

function stubRow(stub) {
  const row = div("audit-row");
  const head = div("audit-row-head");
  head.appendChild(span("dep-name", stub.name));
  head.appendChild(tierPill(stub.tier));
  if (stub.live_callers && stub.live_callers.length) head.appendChild(span("pill live", "live today"));
  const where = [];
  if (stub.addr) where.push(`X360 ${stub.addr}`);
  if (stub.line) where.push(`line ${fmtInt(stub.line)}`);
  if (stub.console_lines != null) where.push(`${fmtInt(stub.console_lines)} console lines`);
  if (where.length) head.appendChild(span("tu-meta", where.join(" · ")));
  row.appendChild(head);
  if (stub.why) row.appendChild(div("audit-meta", `why: ${stub.why}`));
  if (stub.live_callers && stub.live_callers.length) {
    row.appendChild(div("audit-meta", `called by reconstructed code: ${stub.live_callers.join(", ")}`));
  }
  return row;
}


function tuAuditSection(d) {
  const audit = d.audit || {};
  const rollup = audit.rollup;
  const stubs = audit.stubs || [];
  const funcs = audit.funcs || {};
  const asm = audit.asm || {};
  const section = detailSection("Console audit");
  if (!rollup && !Object.keys(funcs).length && !stubs.length && !Object.keys(asm).length) {
    section.appendChild(
      div("muted-text", audit.file ? "Nothing the audit can name differs in this file." : "No audit data for this TU."),
    );
    return section;
  }
  if (rollup) {
    section.appendChild(auditRollupChips(rollup));
    const open = document.createElement("button");
    open.type = "button";
    open.className = "filter";
    open.textContent = "Open file audit";
    open.addEventListener("click", () => openAuditFile(audit.file));
    section.appendChild(open);
  }
  const names = Object.keys(funcs).sort((a, b) => (funcs[b].weight || 0) - (funcs[a].weight || 0));
  for (const name of names.slice(0, 40)) {
    const fnAudit = funcs[name];
    const row = div("audit-row");
    const head = div("audit-row-head");
    head.appendChild(span("dep-name", name));
    if (fnAudit.weight) head.appendChild(span("pill cat-high", `${fmtInt(fnAudit.weight)} high-signal`));
    if (fnAudit.flagged) head.appendChild(span("pill todo", "flagged"));
    row.appendChild(head);
    row.appendChild(findingsList(fnAudit.findings || {}));
    section.appendChild(row);
  }
  if (names.length > 40) section.appendChild(div("audit-meta", `${fmtInt(names.length - 40)} more functions in the file audit.`));
  const asmNames = Object.keys(asm).sort((a, b) =>
    (asm[a].score == null ? 101 : asm[a].score) - (asm[b].score == null ? 101 : asm[b].score));
  if (asmNames.length) {
    const sub = div("detail-section-title");
    const r = audit.asm_rollup || {};
    sub.textContent = `Instruction shape (${fmtInt(asmNames.length)} of this TU's functions in the built exe` +
      (r.functions ? `; file: ${fmtInt(r.a || 0)} A, ${fmtInt(r.b || 0)} B, ${fmtInt(r.c || 0)} C` : "") + ")";
    sub.style.marginTop = "14px";
    section.appendChild(sub);
    for (const name of asmNames.slice(0, 40)) section.appendChild(asmRow({ name, ...asm[name] }));
    if (asmNames.length > 40) section.appendChild(div("audit-meta", `${fmtInt(asmNames.length - 40)} more in the file's instruction-shape view.`));
  }
  if (stubs.length) {
    const sub = div("detail-section-title");
    sub.textContent = `Stubs in this file (${fmtInt(stubs.length)})`;
    sub.style.marginTop = "14px";
    section.appendChild(sub);
    for (const stub of stubs.slice(0, 40)) section.appendChild(stubRow(stub));
    if (stubs.length > 40) {
      const open = document.createElement("button");
      open.type = "button";
      open.className = "filter";
      open.textContent = `All ${fmtInt(stubs.length)} stubs`;
      open.addEventListener("click", () => openStubFile(audit.file));
      section.appendChild(open);
    }
  }
  return section;
}

/* ---------------- Donut rings ---------------- */

// One arc per state. `segments` = [{label, value, color, title}], drawn in order from
// 12 o'clock; the first segment is the state the centre percentage is about.
function setDonut(id, segments, percent, totalText) {
  const ring = el(`${id}Ring`);
  if (!ring) return;
  const group = ring.querySelector(".ring-segments");
  const total = segments.reduce((sum, seg) => sum + Math.max(0, Number(seg.value || 0)), 0);
  if (group) {
    clearNode(group);
    let start = 0;
    segments.forEach((seg, index) => {
      const value = Math.max(0, Number(seg.value || 0));
      if (!value || !total) return;
      const len = (value / total) * RING_CIRCUMFERENCE;
      const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      circle.setAttribute("cx", "60");
      circle.setAttribute("cy", "60");
      circle.setAttribute("r", "52");
      circle.setAttribute("class", `ring-seg seg-${seg.color}${index === 0 ? " lead" : ""}`);
      circle.dataset.index = String(index);
      // a hairline gap between arcs so the states read as separate
      const gap = segments.length > 1 && len > 2 ? 1.2 : 0;
      circle.setAttribute("stroke-dasharray", `${Math.max(0, len - gap)} ${RING_CIRCUMFERENCE - Math.max(0, len - gap)}`);
      circle.setAttribute("stroke-dashoffset", String(-start));
      const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
      title.textContent = `${seg.label}: ${fmtInt(value)} (${((value / total) * 100).toFixed(1)}%)`;
      circle.appendChild(title);
      group.appendChild(circle);
      start += len;
    });
  }
  const pct = Number(percent == null ? (total ? (Number(segments[0]?.value || 0) / total) * 100 : 0) : percent);
  text(`${id}Percent`, `${pct.toFixed(1)}%`);
  ring.style.setProperty("--p", Math.max(0, Math.min(100, pct)));
  if (totalText != null) text(`${id}Count`, totalText);
  const legend = el(`${id}Legend`);
  if (legend) {
    clearNode(legend);
    segments.forEach((seg, index) => {
      const value = Math.max(0, Number(seg.value || 0));
      const row = div("legend-row");
      row.appendChild(span(`sw sw-${seg.color}`));
      const label = span("lg-label", seg.label);
      if (seg.title) label.title = seg.title;
      row.appendChild(label);
      row.appendChild(span("lg-n", fmtInt(value)));
      row.appendChild(span("lg-pct", total ? `${((value / total) * 100).toFixed(1)}%` : "\u2013"));
      if (seg.onClick) {
        row.addEventListener("click", seg.onClick);
        row.setAttribute("role", "button");
        row.tabIndex = 0;
        row.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); seg.onClick(); } });
      }
      // hovering a legend row lights its arc, and the other way round
      const arc = () => ring.querySelector(`.ring-seg[data-index="${index}"]`);
      row.addEventListener("mouseenter", () => { const a = arc(); if (a) a.classList.add("hot"); });
      row.addEventListener("mouseleave", () => { const a = arc(); if (a) a.classList.remove("hot"); });
      const a = arc();
      if (a) {
        a.addEventListener("mouseenter", () => row.classList.add("hot"));
        a.addEventListener("mouseleave", () => row.classList.remove("hot"));
      }
      legend.appendChild(row);
    });
  }
}

function renderAudit(audit) {
  const fa = audit.funcaudit || {};
  const seg = audit.segments || {};
  const paired = Number(fa.paired || 0);
  const clean = Number(fa.clean || 0);
  const trend = el("verifiedTrend");
  const ft = state.funcTotals || { funcs: 0, unidentified: 0 };
  // every named function the audit did not select: templates, operators, compiler-generated
  const selected = Number(fa.selected || 0);
  const notComparable = ft.funcs && selected ? Math.max(0, ft.funcs - ft.unidentified - selected) : 0;
  if (!paired) {
    setDonut("verified", [{ label: "no audit imported yet", value: 1, color: "dim" }], 0, "no audit yet");
    if (trend) trend.hidden = true;
  } else {
    setDonut("verified", [
      { label: "clean", value: seg.clean, color: "green",
        title: "Nothing the static audit can name differs: case ids, event posts, callees, asserts, cited data. Click: the glue audit.",
        onClick: () => openEvidence("audit") },
      { label: "high-signal findings", value: seg.high, color: "red",
        title: "A missing case id, event post, callee or assert the console has and the body does not. Click: files by findings.",
        onClick: () => openEvidence("audit", { category: "MISSING_CASE" }) },
      { label: "context-only findings", value: seg.soft, color: "amber",
        title: "Only log strings, uncited data symbols or the parameter hint differ. Click: missing log strings.",
        onClick: () => openEvidence("audit", { category: "MISSING_STRING" }) },
      { label: "named, no body", value: seg.no_body, color: "crimson",
        title: "The ledger names a PC file for the function but no definition exists anywhere. Click: the no-body list.",
        onClick: () => openEvidence("audit", { category: "NO_BODY" }) },
      { label: "not audited", value: seg.unpaired, color: "dim",
        title: "Console functions with no known PC file; not compared. Click: the stub inventory (the bodies that do exist but are stand-ins).",
        onClick: () => openEvidence("stubs") },
      { label: "not comparable by name", value: notComparable, color: "dim",
        title: "Named functions the audit skips: template instantiations, operators and compiler-generated bodies." },
      { label: "not yet identified", value: ft.unidentified, color: "dim",
        title: "Functions of the console image that have no name in the ledger yet." },
    ], fa.verified_percent, `${fmtInt(clean)} / ${fmtInt(paired)} comparable bodies` +
      (ft.funcs ? ` \u00b7 of ${fmtInt(ft.funcs)} functions` : ""));
    renderVerifiedHistory(audit.history || []);
    if (trend) {
      const t = verifiedTrend(audit.history);
      trend.hidden = !t;
      if (t) {
        trend.textContent = t.text;
        trend.className = `metric-note audit-trend ${t.cls}`;
      }
    }
  }
  const asmNote = el("verifiedAsm");
  if (asmNote) {
    const asm = audit.asm || {};
    const scoreable = Number(asm.scoreable || 0);
    asmNote.hidden = !scoreable;
    if (scoreable) {
      asmNote.textContent = `${fmtInt(asm.A)} of ${fmtInt(scoreable)} scoreable functions have the console's shape (${asm.shape_percent}%)` +
        (asm.commit ? ` \u00b7 build of b5 ${String(asm.commit).slice(0, 10)}` : "");
      asmNote.title = "tools/re/asmaudit.py on every published build: the exe's callees, branch counts and constants against the console's machine code";
    }
    renderShapeBar(audit.asm || {});
  }
  renderVerifiedFacts(audit);
  state.evidence.summary = audit;
  renderEvidenceSummary();
}

// The panel's top right: which commit the audits are from, and the few totals the
// legend and the tier bar do not already say. Each opens its evidence view.
function renderVerifiedFacts(audit) {
  const host = el("verifiedFacts");
  if (!host) return;
  clearNode(host);
  const fa = audit.funcaudit || {};
  const st = audit.stubs || {};
  const asm = audit.asm || {};
  if (fa.commit) {
    const b = span("badge", `b5-decomp ${String(fa.commit).slice(0, 10)} \u00b7 ${relTime(fa.generated_at || fa.imported_at)}`);
    b.title = fa.generated_at ? `audited ${fmtTime(fa.generated_at)}` : "";
    host.appendChild(b);
  }
  const fact = (n, label, title, cls, onClick) => {
    const c = document.createElement("button");
    c.type = "button";
    c.className = `chip ${cls}`;
    c.appendChild(span("", fmtInt(n)));
    const l = document.createElement("label");
    l.textContent = label;
    c.appendChild(l);
    if (title) c.title = title;
    c.addEventListener("click", onClick);
    host.appendChild(c);
  };
  if (fa.files) fact(fa.files, "files with findings", "Files with at least one function the glue audit flagged. Opens the glue audit.", "blocked", () => openEvidence("audit"));
  if (st.stubs) fact(st.stubs, "stub bodies", `${fmtInt(st.live || 0)} on live paths. Opens the stub inventory.`, "compiled", () => openEvidence("stubs"));
  if (asm.paired_in_exe) fact(asm.paired_in_exe, "in the built exe", "Named functions with a symbol in the exe CI built. Opens the instruction shape.", "progress", () => openEvidence("asm"));
  if (asm.flagged) fact(asm.flagged, "flagged in source", "Functions whose body carries [FLAG PC ...] markers. Opens the instruction shape, flagged only.", "todo", () => { openEvidence("asm"); state.evidence.asmFlagged = true; loadEvidenceList(true); renderEvidenceSummary(); });
}

/* ---- the Verified vs Console band: history chart, shape bar, click-through ---- */

function openEvidence(tab, opts = {}) {
  const ev = state.evidence;
  ev.category = opts.category || "";
  ev.asmTier = opts.asmTier || "";
  ev.asmFlagged = false;
  ev.tier = opts.tier || "";
  ev.live = false;
  ev.q = "";
  const search = el("evidenceSearch");
  if (search) search.value = "";
  const tabs = el("evidenceTabs");
  const btn = tabs && tabs.querySelector(`.tab[data-tab="${tab}"]`);
  if (btn) btn.click();       // initEvidence's handler: sets the tab, re-renders, reloads
  const section = el("evidence");
  if (section) section.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderVerifiedHistory(history) {
  const host = el("verifiedHistory");
  if (!host) return;
  clearNode(host);
  const points = (history || []).filter((p) => p.paired != null).slice(-180);
  state.verifiedHistory = history;
  if (points.length < 2) {
    const p = points[0];
    host.appendChild(div("muted-text vh-empty", points.length
      ? `One audited commit so far (b5 ${String(p.commit).slice(0, 10)}: ${fmtInt(p.clean)} clean of ${fmtInt(p.paired)} paired). The lines start with the next one.`
      : "No audited commits yet."));
    return;
  }
  // Three COUNTS, not a ratio: a ratio falls every time a new body is written (it starts
  // with findings), which reads as regress when it is the opposite. Grey = bodies the audit
  // could pair, green = of those, clean; red = named functions still without a body.
  const ns = "http://www.w3.org/2000/svg";
  const W = Math.max(220, Math.round(host.clientWidth || 320)), H = 96, padL = 40, padR = 8, padT = 6, padB = 14;
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("width", String(W));
  svg.setAttribute("height", String(H));
  const series = [
    { key: "paired", cls: "vh-paired", area: true },
    { key: "clean", cls: "vh-clean", area: true },
    { key: "no_body", cls: "vh-nobody", area: false },
  ];
  const top = Math.max(1, ...points.map((p) => Math.max(Number(p.paired || 0), Number(p.no_body || 0))));
  // the x axis is TIME (a point per audited day since the backfill; a build day may add
  // more), so a day nobody audited is a gap, not a step -- index spacing only as a fallback
  const stamps = points.map((p) => Date.parse(p.imported_at || ""));
  const timed = stamps.every((t) => Number.isFinite(t)) && stamps[stamps.length - 1] > stamps[0];
  const t0 = timed ? stamps[0] : 0, tSpan = timed ? stamps[stamps.length - 1] - stamps[0] : 1;
  const x = (i) => padL + ((timed ? (stamps[i] - t0) / tSpan : i / (points.length - 1)) * (W - padL - padR));
  const days = timed ? Math.round(tSpan / 86400000) + 1 : points.length;
  const y = (v) => padT + (1 - v / top) * (H - padT - padB);
  for (const sr of series) {
    const d = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(Number(p[sr.key] || 0)).toFixed(1)}`).join(" ");
    if (sr.area) {
      const a = document.createElementNS(ns, "path");
      a.setAttribute("class", `${sr.cls}-area`);
      a.setAttribute("d", `${d} L${x(points.length - 1).toFixed(1)},${(H - padB).toFixed(1)} L${x(0).toFixed(1)},${(H - padB).toFixed(1)} Z`);
      svg.appendChild(a);
    }
    const l = document.createElementNS(ns, "path");
    l.setAttribute("class", `vh-line ${sr.cls}`);
    l.setAttribute("d", d);
    svg.appendChild(l);
  }
  for (const [v, label] of [[top, fmtInt(top)], [0, "0"]]) {
    const t = document.createElementNS(ns, "text");
    t.setAttribute("x", "0"); t.setAttribute("y", (y(v) + (v ? 3 : 0)).toFixed(1)); t.setAttribute("class", "vh-axis");
    t.textContent = label;
    svg.appendChild(t);
  }
  points.forEach((p, i) => {
    const c = document.createElementNS(ns, "circle");
    c.setAttribute("class", "vh-dot");
    c.setAttribute("cx", x(i).toFixed(1)); c.setAttribute("cy", y(Number(p.clean || 0)).toFixed(1)); c.setAttribute("r", "2");
    const title = document.createElementNS(ns, "title");
    title.textContent = `${String(p.commit).slice(0, 10)}${p.imported_at ? ` \u00b7 ${String(p.imported_at).slice(0, 10)}` : ""}: ${fmtInt(p.clean)} clean of ${fmtInt(p.paired)} paired (${Number(p.verified_percent || 0).toFixed(1)}%), ${fmtInt(p.no_body)} named with no body, ${fmtInt(p.weight)} high-signal findings`;
    c.appendChild(title);
    svg.appendChild(c);
  });
  const first = document.createElementNS(ns, "text");
  first.setAttribute("x", padL.toFixed(1)); first.setAttribute("y", (H - 3).toFixed(1)); first.setAttribute("class", "vh-axis");
  first.textContent = points[0].imported_at ? String(points[0].imported_at).slice(0, 10) : String(points[0].commit).slice(0, 7);
  svg.appendChild(first);
  const last = document.createElementNS(ns, "text");
  last.setAttribute("x", (W - padR).toFixed(1)); last.setAttribute("y", (H - 3).toFixed(1)); last.setAttribute("class", "vh-axis");
  last.setAttribute("text-anchor", "end");
  const lastP = points[points.length - 1];
  last.textContent = `${lastP.imported_at ? String(lastP.imported_at).slice(0, 10) : String(lastP.commit).slice(0, 7)} \u00b7 ${fmtInt(days)} days, ${fmtInt(points.length)} audited`;
  svg.appendChild(last);
  host.appendChild(svg);
  const lg = div("vp-shape-legend");
  const a = points[0], b = points[points.length - 1];
  for (const [cls, label, va, vb] of [
    ["vh-sw-paired", "paired bodies", a.paired, b.paired],
    ["vh-sw-clean", "clean", a.clean, b.clean],
    ["vh-sw-nobody", "named, no body", a.no_body, b.no_body],
  ]) {
    const item = span("", "");
    item.appendChild(span(`vh-sw ${cls}`, ""));
    item.appendChild(document.createTextNode(` ${label} ${fmtInt(va)} \u2192 ${fmtInt(vb)}`));
    lg.appendChild(item);
  }
  host.appendChild(lg);
}

function renderShapeBar(asm) {
  const host = el("verifiedShape");
  if (!host) return;
  clearNode(host);
  const legend = host.parentNode.querySelector(".vp-shape-legend");
  if (legend) legend.remove();
  const tiers = [["A", "same shape", Number(asm.A || 0)], ["B", "close", Number(asm.B || 0)],
                 ["C", "diverges", Number(asm.C || 0)], ["T", "trivial", Number(asm.T || 0)]];
  const total = tiers.reduce((n, t) => n + t[2], 0);
  if (!total) {
    host.appendChild(span("", ""));
    return;
  }
  for (const [tier, label, n] of tiers) {
    const part = span(`sh-${tier}`);
    part.style.width = `${(n / total) * 100}%`;
    part.title = `${tier} ${label}: ${fmtInt(n)} (${((n / total) * 100).toFixed(1)}%)`;
    part.addEventListener("click", () => openEvidence("asm", { asmTier: tier }));
    host.appendChild(part);
  }
  const lg = div("vp-shape-legend");
  for (const [tier, label, n] of tiers) {
    const item = span("", "");
    const b = document.createElement("b");
    b.textContent = `${tier} `;
    item.appendChild(b);
    item.appendChild(document.createTextNode(`${label} ${fmtInt(n)}`));
    lg.appendChild(item);
  }
  host.after(lg);
}

/* ---------------- Console Evidence section ---------------- */

const EVIDENCE_AUDIT_CHIPS = [
  ["", "all files", "", ""],
  ["NO_BODY", "no body", "crimson", "NO_BODY"],
  ["MISSING_CASE", "missing case ids", "red", "MISSING_CASE"],
  ["EXTRA_CASE", "misfiled case ids", "red", "EXTRA_CASE"],
  ["MISSING_EVENT", "missing event posts", "red", "MISSING_EVENT"],
  ["MISSING_CALLEE", "missing callees", "red", "MISSING_CALLEE"],
  ["MISSING_ASSERT", "missing asserts", "amber", "MISSING_ASSERT"],
  ["MISSING_STRING", "missing log strings", "grey", "MISSING_STRING"],
  ["UNCITED_DATA", "uncited data", "grey", "UNCITED_DATA"],
];

function evidenceChip(label, count, active, color, onClick) {
  const chip = document.createElement("button");
  chip.type = "button";
  chip.className = `evidence-chip${active ? " on" : ""}`;
  if (color) chip.appendChild(span(`sw sw-${color}`));
  chip.appendChild(span("", label));
  if (count != null) chip.appendChild(span("n", fmtInt(count)));
  chip.addEventListener("click", onClick);
  return chip;
}

function evidenceTile(label, value, cls) {
  const chip = div(`chip ${cls || "todo"}`);
  chip.appendChild(span("", typeof value === "number" ? fmtInt(value) : value));
  const l = document.createElement("label");
  l.textContent = label;
  chip.appendChild(l);
  return chip;
}

function renderEvidenceSummary() {
  const ev = state.evidence;
  const audit = ev.summary || {};
  const fa = audit.funcaudit || {};
  const st = audit.stubs || {};
  const meta = el("evidenceMeta");
  if (meta) {
    meta.textContent = fa.commit
      ? `b5-decomp ${String(fa.commit).slice(0, 10)} · ${relTime(fa.generated_at || fa.imported_at)}`
      : "no audit yet";
    meta.title = fa.generated_at ? `audited ${fmtTime(fa.generated_at)}` : "";
  }
  const intro = el("evidenceIntro");
  const chips = el("evidenceChips");
  if (!intro || !chips) return;
  clearNode(chips);
  if (ev.tab === "audit") {
    intro.textContent =
      "Every reconstructed body (plus the PC-only helpers it calls) compared with the console's own pseudocode, per file. A missing switch case id, event post, callee or body is a difference the console's code has and ours does not; missing log strings and uncited data symbols are context. Rebuilt by CI on every commit — nothing here is declared by hand.";
    const cats = fa.categories || {};
    for (const [key, label, color, catKey] of EVIDENCE_AUDIT_CHIPS) {
      const count = catKey ? (cats[catKey] ? cats[catKey].functions : 0) : null;
      if (catKey && !count) continue;
      chips.appendChild(evidenceChip(label, count, ev.category === key, color, () => {
        ev.category = key;
        loadEvidenceList(true);
        renderEvidenceSummary();
      }));
    }
  } else if (ev.tab === "asm") {
    const asm = audit.asm || {};
    if (meta) {
      meta.textContent = asm.commit
        ? `build of b5 ${String(asm.commit).slice(0, 10)} \u00b7 ${relTime(asm.generated_at || asm.imported_at)}`
        : "no build audited yet";
      meta.title = asm.exe_mtime ? `exe built ${fmtTime(asm.exe_mtime)}` : "";
    }
    intro.textContent =
      "The exe CI built, function by function, against the console's own machine code: the same named callees, the same number of conditional branches, the same constants. Byte-matching is impossible across the x64 widening, so this is shape, not identity \u2014 tier A is the same shape, C diverges, and a divergence is a diff to read (which callees and constants exist on one side only), not a verdict. Recomputed on every published build.";
    for (const [key, label, color, count] of [
      ["", "all tiers", "", null],
      ["A", "same shape", "green", asm.A],
      ["B", "close", "amber", asm.B],
      ["C", "diverges", "red", asm.C],
      ["T", "trivial", "grey", asm.T],
    ]) {
      chips.appendChild(evidenceChip(label, count, ev.asmTier === key, color, () => {
        ev.asmTier = key;
        loadEvidenceList(true);
        renderEvidenceSummary();
      }));
    }
    const flaggedChip = evidenceChip("flagged in source only", asm.flagged, ev.asmFlagged, "blue", () => {
      ev.asmFlagged = !ev.asmFlagged;
      loadEvidenceList(true);
      renderEvidenceSummary();
    });
    flaggedChip.title = "Functions whose body carries [FLAG PC ...] markers: bring-up gates, boot gates, witnesses, diagnostics. They compile into the exe and are meant to differ from the console; the score does not discount them, this only says which divergences are known.";
    chips.appendChild(flaggedChip);
  } else {
    intro.textContent =
      "Every body in the tree that is still a stand-in, per file. High: the file or the body says so, or it traps. Medium: a trivial body under a softer marker. Low: a trivial, unmarked body whose console function does real work — possibly a legitimate empty default. \"Live today\" means a body we have reconstructed calls the stub right now.";
    for (const [key, label, color, count] of [
      ["", "all tiers", "", null],
      ["HIGH", "high", "red", st.high],
      ["MEDIUM", "medium", "amber", st.medium],
      ["LOW", "low", "grey", st.low],
    ]) {
      chips.appendChild(evidenceChip(label, count, ev.tier === key, color, () => {
        ev.tier = key;
        loadEvidenceList(true);
        renderEvidenceSummary();
      }));
    }
    chips.appendChild(evidenceChip("live today only", st.live, ev.live, "blue", () => {
      ev.live = !ev.live;
      loadEvidenceList(true);
      renderEvidenceSummary();
    }));
  }
}

function evidenceSparkline(history) {
  const existing = document.querySelector(".evidence-spark");
  if (existing) existing.remove();
  const points = history.filter((p) => p.paired != null);
  if (points.length < 2) return null;
  const wrap = div("evidence-spark");
  wrap.title = "verified % per audited commit";
  const max = Math.max(...points.map((p) => Number(p.verified_percent || 0)), 1);
  for (const p of points.slice(-40)) {
    const bar = div("bar");
    bar.style.height = `${Math.max(4, (Number(p.verified_percent || 0) / max) * 44)}px`;
    bar.title = `${String(p.commit).slice(0, 10)}: ${Number(p.verified_percent || 0).toFixed(1)}% verified, ${fmtInt(p.clean)} clean`;
    wrap.appendChild(bar);
  }
  return wrap;
}

function evidenceParams(reset) {
  const ev = state.evidence;
  const p = new URLSearchParams();
  if (ev.q) p.set("q", ev.q);
  p.set("limit", ev.limit);
  p.set("offset", reset ? 0 : ev.offset);
  p.set("order", "desc");
  if (ev.tab === "audit") {
    if (ev.category) p.set("category", ev.category);
    p.set("sort", "weight");
  } else if (ev.tab === "asm") {
    if (ev.asmTier) p.set("tier", ev.asmTier);
    if (ev.asmFlagged) p.set("flagged", "true");
    p.set("sort", ev.asmFlagged ? "flagged" : ev.asmTier ? ev.asmTier.toLowerCase() : "c");
  } else {
    if (ev.tier) p.set("tier", ev.tier);
    if (ev.live) p.set("live", "true");
    p.set("sort", ev.live ? "live" : "stubs");
  }
  return p;
}

/* ---- the instruction-shape tier (tools/re/asmaudit.py) ---- */
const ASM_TIER_LABELS = { A: "same shape", B: "close", C: "diverges", T: "trivial" };
const ASM_TIER_PILL = { A: "done", B: "compiled", C: "blocked", T: "todo" };

function asmTierPill(tier) {
  return span(`pill ${ASM_TIER_PILL[tier] || "todo"}`, `${tier} ${ASM_TIER_LABELS[tier] || ""}`.trim());
}

function asmRow(fn) {
  const row = div("audit-row");
  const head = div("audit-row-head");
  head.appendChild(span("dep-name", fn.name));
  head.appendChild(asmTierPill(fn.tier));
  if (fn.score != null) head.appendChild(span("pill todo", `score ${fn.score}`));
  if (fn.flags && fn.flags.total) {
    const kinds = Object.entries(fn.flags.kinds || {}).map(([k, n]) => `${n} ${k}`).join(", ");
    const pill = span("pill todo", `flagged ${fmtInt(fn.flags.total)}`);
    pill.title = `[FLAG] markers in the source body: ${kinds}. PC additions that compile in and are meant to differ from the console; the score does not discount them.`;
    head.appendChild(pill);
  }
  const c = fn.counts || {};
  const where = [];
  if (fn.addr) where.push(`X360 ${fn.addr}`);
  if (c.n) where.push(`${fmtInt(c.n[0])} console / ${fmtInt(c.n[1])} pc insns`);
  if (c.cond) where.push(`branches ${fmtInt(c.cond[0])} / ${fmtInt(c.cond[1])}`);
  if (c.calls) where.push(`calls ${fmtInt(c.calls[0])} / ${fmtInt(c.calls[1])}`);
  if (where.length) head.appendChild(span("tu-meta", where.join(" \u00b7 ")));
  row.appendChild(head);
  const list = asmDiffList(fn);
  if (list.childNodes.length) row.appendChild(list);
  return row;
}

function asmDiffList(fn) {
  const d = fn.diff || {};
  const list = document.createElement("ul");
  list.className = "audit-findings";
  const add = (label, items, high) => {
    if (!items || !items.length) return;
    const li = document.createElement("li");
    if (high) li.classList.add("high");
    li.appendChild(span("audit-cat", label));
    li.appendChild(span("audit-items", items.join("; ")));
    list.appendChild(li);
  };
  add(`callees only on the console (${fmtInt(d.calls_only_console_n || 0)})`, d.calls_only_console, true);
  add(`callees only in our exe (${fmtInt(d.calls_only_pc_n || 0)})`, d.calls_only_pc, false);
  add("constants only on the console", d.imm_only_console, true);
  add("constants only in our exe", d.imm_only_pc, false);
  if (fn.notes && fn.notes.length) add("notes", fn.notes.filter((n) => !n.startsWith("flagged in source")), false);
  if (fn.flags && fn.flags.total) {
    add("flagged PC additions in the body", Object.entries(fn.flags.kinds || {}).map(([k, n]) => `${n} [FLAG ${k}]`), false);
  }
  return list;
}

async function loadEvidenceList(reset) {
  const ev = state.evidence;
  if (reset) {
    ev.offset = 0;
    ev.items = [];
  }
  const requestId = ++ev.requestId;
  const path = ev.tab === "audit" ? "/api/audit/files" : ev.tab === "asm" ? "/api/asm/files" : "/api/stubs/files";
  try {
    const data = await fetchJson(`${path}?${evidenceParams(reset)}`, 15000);
    if (requestId !== ev.requestId) return;
    ev.total = data.total || 0;
    ev.items = data.items || [];
    renderEvidenceList();
    renderEvidenceFoot();
  } catch (error) {
    if (requestId !== ev.requestId) return;
    const list = el("evidenceList");
    clearNode(list);
    list.appendChild(div("muted-text", `Evidence unavailable: ${error.message}`));
  }
  loadEvidenceTop();
}

function renderEvidenceList() {
  const ev = state.evidence;
  const list = el("evidenceList");
  clearNode(list);
  if (!ev.items.length) {
    const have = ev.tab === "asm"
      ? ((ev.summary || {}).asm || {}).paired_in_exe
      : ev.summary && (ev.summary.funcaudit || {}).paired;
    list.appendChild(div("muted-text", have ? "No files match." : ev.tab === "asm" ? "No build has been audited yet." : "No audit has been imported yet."));
  }
  for (const item of ev.items) {
    const card = div("evidence-file");
    const head = div("evidence-file-head");
    const left = div("evidence-file-left");
    left.appendChild(div("evidence-file-path", item.file || "(functions the ledger files nowhere)"));
    const nums = div("evidence-file-nums");
    if (ev.tab === "audit") {
      const meta = [];
      if (item.functions) meta.push(`${fmtInt(item.functions)} functions with findings`);
      if (item.missing_string) meta.push(`${fmtInt(item.missing_string)} log strings`);
      if (item.uncited_data) meta.push(`${fmtInt(item.uncited_data)} uncited data`);
      left.appendChild(div("evidence-file-meta", meta.join(" · ")));
      nums.appendChild(span("pill cat-high", `${fmtInt(item.weight)} findings`));
      if (item.no_body) nums.appendChild(span("pill tier-HIGH", `${fmtInt(item.no_body)} no body`));
      if (item.missing_case) nums.appendChild(span("pill cat-mid", `${fmtInt(item.missing_case)} case ids`));
      if (item.missing_event) nums.appendChild(span("pill cat-mid", `${fmtInt(item.missing_event)} events`));
      if (item.missing_callee) nums.appendChild(span("pill cat-low", `${fmtInt(item.missing_callee)} callees`));
      if (item.missing_assert) nums.appendChild(span("pill cat-low", `${fmtInt(item.missing_assert)} asserts`));
    } else if (ev.tab === "asm") {
      const meta = [];
      if (item.mean_score) meta.push(`mean score ${item.mean_score}`);
      left.appendChild(div("evidence-file-meta", meta.join(" \u00b7 ")));
      nums.appendChild(span("pill todo", `${fmtInt(item.functions)} in exe`));
      if (item.a) nums.appendChild(span("pill done", `${fmtInt(item.a)} A`));
      if (item.b) nums.appendChild(span("pill compiled", `${fmtInt(item.b)} B`));
      if (item.c) nums.appendChild(span("pill blocked", `${fmtInt(item.c)} C`));
      if (item.t) nums.appendChild(span("pill todo", `${fmtInt(item.t)} T`));
      if (item.flagged) nums.appendChild(span("pill live", `${fmtInt(item.flagged)} flagged`));
    } else {
      const meta = [];
      if (item.console_lines) meta.push(`${fmtInt(item.console_lines)} console lines behind them`);
      left.appendChild(div("evidence-file-meta", meta.join(" · ")));
      nums.appendChild(span("pill cat-high", `${fmtInt(item.stubs)} stubs`));
      if (item.high) nums.appendChild(span("pill tier-HIGH", `${fmtInt(item.high)} high`));
      if (item.medium) nums.appendChild(span("pill tier-MEDIUM", `${fmtInt(item.medium)} medium`));
      if (item.low) nums.appendChild(span("pill tier-LOW", `${fmtInt(item.low)} low`));
      if (item.live) nums.appendChild(span("pill live", `${fmtInt(item.live)} live`));
    }
    head.append(left, nums);
    head.title = "Open every function of this file in the drawer";
    head.addEventListener("click", () => openEvidenceFile(item.file));
    card.appendChild(head);
    list.appendChild(card);
  }
}

function renderEvidenceFoot() {
  const ev = state.evidence;
  const from = ev.total === 0 ? 0 : ev.offset + 1;
  const to = Math.min(ev.offset + ev.limit, ev.total);
  const totalPages = Math.max(1, Math.ceil(ev.total / ev.limit));
  const currentPage = Math.min(totalPages, Math.floor(ev.offset / ev.limit) + 1);
  text("evidenceRange", ev.total ? `${fmtInt(from)}-${fmtInt(to)} of ${fmtInt(ev.total)} files` : "no files");
  text("evPageStatus", `Page ${fmtInt(currentPage)} of ${fmtInt(totalPages)}`);
  const prev = el("evPagePrev");
  const next = el("evPageNext");
  if (prev) prev.disabled = ev.offset <= 0;
  if (next) next.disabled = ev.offset + ev.limit >= ev.total;
  const jump = el("evPageJump");
  if (jump) {
    jump.max = totalPages;
    jump.value = currentPage;
  }
  renderPageButtons(currentPage, totalPages, "evPageButtons", goToEvidencePage);
}

function goToEvidencePage(page) {
  const ev = state.evidence;
  const totalPages = Math.max(1, Math.ceil(ev.total / ev.limit));
  const nextPage = Math.max(1, Math.min(totalPages, Number(page) || 1));
  const nextOffset = (nextPage - 1) * ev.limit;
  if (nextOffset === ev.offset) {
    renderEvidenceFoot();
    return;
  }
  ev.offset = nextOffset;
  loadEvidenceList(false);
  const list = el("evidenceList");
  if (list) list.scrollIntoView({ behavior: "smooth", block: "start" });
}

function openEvidenceFile(file) {
  const ev = state.evidence;
  if (ev.tab === "audit") openAuditFile(file);
  else if (ev.tab === "asm") openAsmFile(file);
  else openStubFile(file);
}


async function loadEvidenceTop() {
  const ev = state.evidence;
  const title = el("evidenceTopTitle");
  const note = el("evidenceTopNote");
  const list = el("evidenceTop");
  if (!title || !list) return;
  try {
    if (ev.tab === "audit") {
      const cat = ev.category && ev.category !== "NO_BODY" ? ev.category : "MISSING_CASE";
      title.textContent = cat === "MISSING_CASE" ? "Largest switch gaps" : `Most ${AUDIT_LABELS[cat] || cat}`;
      note.textContent = cat === "MISSING_CASE"
        ? "Functions with the most console case ids that have no arm in our body — whole behaviours the console dispatches and we never reach."
        : "Functions with the most items of this category.";
      const data = await fetchJson(`/api/audit/top?category=${encodeURIComponent(cat)}&limit=12`, 15000);
      clearNode(list);
      for (const item of data.items || []) {
        const li = document.createElement("li");
        li.appendChild(span("top-n", fmtInt(item.ids != null ? item.ids : item.count)));
        const name = span("top-name", item.name);
        name.appendChild(span("top-meta", `${item.file}${item.preview ? ` — ${item.preview}` : ""}`));
        li.appendChild(name);
        li.addEventListener("click", () => openAuditFile(item.file));
        list.appendChild(li);
      }
    } else if (ev.tab === "asm") {
      title.textContent = "Largest divergences";
      note.textContent = "Tier C functions by the size of the console body: the biggest bodies whose calls, branches or constants do not line up with the console's machine code. Click one to open its file.";
      const data = await fetchJson(`/api/asm/top?tier=C&limit=12`, 15000);
      clearNode(list);
      for (const item of data.items || []) {
        const li = document.createElement("li");
        li.appendChild(span("top-n", item.score != null ? String(item.score) : "?"));
        const name = span("top-name", item.name);
        const c = item.counts || {};
        name.appendChild(span("top-meta", `${item.file || "(no file)"} \u00b7 ${fmtInt(c.n ? c.n[0] : 0)} console insns`));
        li.appendChild(name);
        li.addEventListener("click", () => openAsmFile(item.file));
        list.appendChild(li);
      }
    } else {
      title.textContent = "Biggest stubs on live paths";
      note.textContent = "Stand-ins a reconstructed body calls today, by the size of the console function behind them — the work that is running wrong right now.";
      const data = await fetchJson(`/api/stubs/top?live=true&limit=12`, 15000);
      clearNode(list);
      for (const item of data.items || []) {
        const li = document.createElement("li");
        li.appendChild(span("top-n", fmtInt(item.console_lines || 0)));
        const name = span("top-name", item.name);
        name.appendChild(span("top-meta", `${item.file} — called by ${(item.live_callers || []).slice(0, 2).join(", ")}`));
        li.appendChild(name);
        li.addEventListener("click", () => openStubFile(item.file));
        list.appendChild(li);
      }
    }
  } catch (_) {
    /* the side list is best-effort */
  }
}

function initEvidence() {
  const ev = state.evidence;
  const tabs = el("evidenceTabs");
  if (!tabs) return;
  tabs.addEventListener("click", (e) => {
    const btn = e.target.closest(".tab");
    if (!btn) return;
    ev.tab = btn.dataset.tab;
    for (const t of tabs.querySelectorAll(".tab")) t.classList.toggle("active", t === btn);
    renderEvidenceSummary();
    loadEvidenceList(true);
  });
  el("evidenceSearch").addEventListener("input", (e) => {
    ev.q = e.target.value.trim();
    clearTimeout(ev.searchTimer);
    ev.searchTimer = setTimeout(() => loadEvidenceList(true), 250);
  });
  el("evPagePrev").addEventListener("click", () => goToEvidencePage(Math.floor(state.evidence.offset / state.evidence.limit)));
  el("evPageNext").addEventListener("click", () => goToEvidencePage(Math.floor(state.evidence.offset / state.evidence.limit) + 2));
  el("evPageJump").addEventListener("change", (e) => goToEvidencePage(e.target.value));
  el("evPageJump").addEventListener("keydown", (e) => {
    if (e.key === "Enter") goToEvidencePage(e.target.value);
  });
  for (const btn of document.querySelectorAll("#verifiedCard .vp-actions [data-open]")) {
    btn.addEventListener("click", () => openEvidence(btn.dataset.open));
  }
  // the history chart is drawn at the box's pixel width: redraw when that changes
  window.addEventListener("resize", () => {
    clearTimeout(state.historyResizeTimer);
    state.historyResizeTimer = setTimeout(() => renderVerifiedHistory(state.verifiedHistory || []), 150);
  });
  loadEvidenceList(true);
}
