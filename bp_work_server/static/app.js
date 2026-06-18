const state = {
  lastEventId: 0,
  refreshTimer: null,
  githubTimer: null,
  eventSource: null,
  dashboardInFlight: false,
  githubInFlight: false,
  treeCollapsed: {}, // path -> bool, remembers folder state across refreshes
  actorProfiles: {},
  repo: { owner: "Adriwin06", name: "b5-decomp", ref: "dev" },
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
  // Client-side mini-explorers for the Live Events and Next Queue panels:
  // the dashboard payload carries the full lists; we filter/search/page here.
  eventsView: { all: [], q: "", action: "", actor: "", page: 1, perPage: 50, searchTimer: null },
  queueView: { all: [], q: "", source: "", page: 1, perPage: 50, searchTimer: null },
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
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function setConnection(mode, label) {
  const node = el("connection");
  node.classList.remove("online", "offline");
  node.classList.add(mode);
  text("connectionText", label);
}

function setRing(id, value) {
  const node = el(id);
  if (node) node.style.setProperty("--p", Math.max(0, Math.min(100, Number(value || 0))));
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
    render(await fetchJson("/dashboard/state", 15000));
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
  const totals = data.totals || {};
  const counts = data.counts || {};
  text("subtitle", `${fmtInt(totals.tus)} translation units · ${fmtInt(totals.funcs)} functions`);
  text("tuPercent", `${Number(totals.tu_percent || 0).toFixed(1)}%`);
  text("fnPercent", `${Number(totals.func_percent || 0).toFixed(1)}%`);
  setRing("tuRing", totals.tu_percent);
  setRing("fnRing", totals.func_percent);
  text("tuCount", `${fmtInt(totals.done_tus)} / ${fmtInt(totals.tus)} done`);
  text("fnCount", `${fmtInt(totals.done_funcs)} / ${fmtInt(totals.funcs)} covered`);
  text("activeGoal", data.active_goal || "Whole program");
  text("serverTime", fmtTime(data.server_time));

  text("todoCount", fmtInt(counts.todo));
  text("progressCount", fmtInt(counts.in_progress));
  text("compiledCount", fmtInt(counts.compiled));
  text("doneCount", fmtInt(counts.done));
  text("blockedCount", fmtInt(counts.blocked));

  renderAgents(data.agents || []);
  renderActiveWork(data.active_work || []);
  setQueueData((data.next && data.next.items) || []);
  setEventsData(data.recent_events || []);
  renderGoals(data.goals || []);
  renderBlocked(data.blocked || []);
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
  const contributionLabel = fullContributionCoverage ? "contributed to" : "contributed to cached";
  const coverageText = fullContributionCoverage
    ? ""
    : ` (cache ${fmtInt(coverage.file_cached || 0)}/${fmtInt(coverage.file_total || 0)} TUs, ${fmtInt(
        coverage.function_cached || 0,
      )}/${fmtInt(coverage.function_total || 0)} funcs)`;
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
        `${fmtInt(agent.total)} active | reviewed ${fmtInt(agent.completed_tus ?? agent.completed)} TUs / ${fmtInt(
          agent.completed_funcs,
        )} funcs | ${contributionLabel} ${fmtInt(agent.contributed_tus || 0)} TUs / ${fmtInt(
          agent.contributed_funcs || 0,
        )} funcs${coverageText} | lease ${shortTime(agent.lease_expires_at)} | last ${
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

function renderBlocked(items) {
  text("blockedListCount", `${items.length} TUs`);
  const root = el("blockedList");
  clearNode(root);
  root.className = items.length ? "blocked-list" : "blocked-list empty";
  if (!items.length) {
    root.textContent = "No blocked work.";
    return;
  }
  for (const item of items) {
    const row = div("blocked-row");
    row.classList.add("clickable");
    row.appendChild(tuButton(item.id));
    row.appendChild(div("tu-meta", item.notes || "No reason recorded."));
    row.addEventListener("click", () => openDetail(item.id));
    root.appendChild(row);
  }
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
  const current = sel.value;
  clearNode(sel);
  const all = document.createElement("option");
  all.value = "";
  all.textContent = allLabel;
  sel.appendChild(all);
  for (const v of values || []) {
    const opt = document.createElement("option");
    opt.value = v;
    opt.textContent = v;
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

function renderPageButtons(currentPage, totalPages) {
  const root = el("pageButtons");
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
    btn.addEventListener("click", () => goToPage(page));
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

// Wire the search box, filter selects, and Prev/Next for the Live Events and
// Next Queue mini-panels. Filtering happens over the in-memory `view.all`.
function initMiniPanels() {
  const panels = [
    { view: state.eventsView, prefix: "events", render: renderEvents, filters: [
      ["eventsFilterAction", "action"],
      ["eventsFilterActor", "actor"],
    ] },
    { view: state.queueView, prefix: "queue", render: renderQueue, filters: [
      ["queueFilterSource", "source"],
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
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeDetail();
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
    wrap.appendChild(div("muted-text", "No event activity yet."));
    return wrap;
  }
  const max = Math.max(...points.map((point) => Number(point.count || 0)), 1);
  for (const point of points.slice(-28)) {
    const bar = div("profile-spark-bar");
    bar.style.height = `${Math.max(8, (Number(point.count || 0) / max) * 76)}px`;
    bar.title = `${point.date}: ${fmtInt(point.count)} events`;
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
  metrics.appendChild(profileMetric("Completed TUs", fmtInt(summary.completed_tus)));
  metrics.appendChild(profileMetric("Completed funcs", fmtInt(summary.completed_funcs)));
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

  // Functions
  const funcs = detailSection(`Functions (${(d.funcs || []).length})`);
  if (!d.funcs || !d.funcs.length) funcs.appendChild(div("muted-text", "No functions recorded."));
  else
    d.funcs.forEach((fn) => {
      const row = div("dep-row clickable");
      row.appendChild(span("dep-name", fn.name));
      row.appendChild(statusPill(fn.status));
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
initExplorer();
initMiniPanels();
// GitHub data changes slowly and is cached server-side; poll gently.
state.githubTimer = window.setInterval(refreshGithub, 90000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});
