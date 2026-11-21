// The progress dashboard. Controls, the detail drawer, the release chooser and the pagers
// are the shared b5-decomp-webui components in /static/b5-ui (webui.json names the build);
// this module fetches the data and renders the domain views.
import { closeDialog, createFilterGroup, createPager, createSelect, enhance, openDialog, setOdometer, setProgress } from "./b5-ui/b5-ui.js";

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
    view: "map",
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
  progressMap: { controller: null, snapshot: null, inFlight: false, pending: false, restoreFocus: false },
  // the Console gaps panel: its own tab, filters and paging
  evidence: {
    tab: "audit",
    q: "",
    category: "",
    tier: "",
    live: false,
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
  el("connection").dataset.tone = mode === "online" ? "success" : "danger";
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

// Status and finding labels are the shared slanted ribbons; the kind picks the tone.
const RIBBON_TONES = {
  done: "success", reviewed: "success",
  compiled: "warning", compiles: "warning", "cat-mid": "warning", "tier-MEDIUM": "warning",
  blocked: "danger", recovered: "danger", "cat-high": "danger", "tier-HIGH": "danger",
  in_progress: "info", live: "info",
  external: "external",
};
function ribbon(tone, label) {
  const node = span("b5-ribbon", label);
  if (tone) node.dataset.tone = tone;
  return node;
}
function pill(kind, label) {
  return ribbon(RIBBON_TONES[kind], label);
}

// Count plates are the shared summary tiles, toned by the state they count.
const SUMMARY_TONES = { done: "success", progress: "info", compiled: "warning", blocked: "danger" };
function summaryTile(value, label, kind, tag = "div") {
  const tile = document.createElement(tag);
  tile.className = "b5-summary";
  if (SUMMARY_TONES[kind]) tile.dataset.tone = SUMMARY_TONES[kind];
  const number = document.createElement("strong");
  number.textContent = value;
  tile.append(number, span("", label));
  return tile;
}

// Keep a themed dropdown's visible label in step after a programmatic .value change.
function refreshSelect(select) {
  if (select && select.classList.contains("b5-select-native")) createSelect(select).refresh();
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
    loadEvolution();
    loadBytematch();
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
  const externalTus = Number(totals.external_tus ?? counts.external ?? 0);
  const complete = Number(totals.done_tus || 0) + externalTus;
  setDonut("tu", [
    { label: "done", value: counts.done, color: "green" },
    { label: "external", value: externalTus, color: "purple", title: "Supplied by existing vendor source or a host platform implementation; counts toward completion." },
    { label: "compiled", value: counts.compiled, color: "gold" },
    { label: "in progress", value: counts.in_progress, color: "blue" },
    { label: "blocked", value: counts.blocked, color: "red" },
    { label: "todo", value: counts.todo, color: "grey" },
  ], tus ? (complete / tus) * 100 : 0, `${fmtInt(complete)} / ${fmtInt(tus)} complete`);
  renderLicense(complete, tus, (data.ledger_evidence || {}).source_commit);

  const funcs = Number(totals.funcs || 0);
  const recorded = Number(totals.done_funcs || 0);
  const external = Number(totals.external_funcs || 0);
  const covered = recorded + external;
  const unidentified = Number(totals.unidentified_funcs || 0);
  state.funcTotals = { funcs, unidentified };
  setDonut("fn", [
    { label: "recorded", value: recorded, color: "gold" },
    { label: "external", value: external, color: "purple", title: "Provided by vendor source or the host platform; counts toward coverage." },
    { label: "named, unrecorded", value: Math.max(0, funcs - covered - unidentified), color: "grey" },
    { label: "unidentified", value: unidentified, color: "dim",
      title: "Functions IDA found in the binary that nobody has named: no DWARF file, no RTTI class, so no translation unit — but still code to write." },
  ], funcs ? (covered / funcs) * 100 : 0, `${fmtInt(covered)} / ${fmtInt(funcs)} covered`);
  const functionStates = data.function_status_counts || {};
  const recordedStates = Object.entries(functionStates).filter(([status, count]) => status !== "todo" && count);
  text("fnStatusNote", recordedStates.length
    ? `Ledger: ${recordedStates.map(([status, count]) => `${fmtInt(count)} ${status === "compiles" ? "compiled" : status}`).join(" · ")}`
    : "Coverage recorded in the work ledger.");
  const ledgerEvidence = data.ledger_evidence || {};
  const syncNote = el("sourceSyncNote");
  if (syncNote) {
    syncNote.hidden = !ledgerEvidence.source_commit;
    const latestSource = (data.decomp_repo || {}).revision;
    syncNote.textContent = ledgerEvidence.source_commit
      ? (latestSource && latestSource !== ledgerEvidence.source_commit
        ? `Source update pending · showing b5 ${String(ledgerEvidence.source_commit).slice(0, 10)}`
        : `Source reconciled: b5 ${String(ledgerEvidence.source_commit).slice(0, 10)} · ${relTime(ledgerEvidence.reconciled_at)}`)
      : "";
  }
  text("tuEvidenceNote", ledgerEvidence.done_tus_with_missing_bodies
    ? `Ledger status · ${fmtInt(ledgerEvidence.done_tus_with_missing_bodies)} done TUs have missing-body audit findings. ${fmtInt(externalTus)} external TUs count toward completion.`
    : `Ledger status · ${fmtInt(externalTus)} external TUs count toward completion.`);
  text("fnEvidenceNote", ledgerEvidence.recorded_funcs_with_missing_bodies
    ? `${fmtInt(ledgerEvidence.recorded_funcs_with_missing_bodies)} recorded functions have missing-body audit findings; these require source review.`
    : "");

  const linked = Number(totals.linked_tus || 0);
  const externalBuild = Number(totals.external_build_tus || 0);
  const sourceLinked = Number(totals.source_linked_tus ?? linked);
  const available = Number(totals.available_tus ?? (sourceLinked + externalBuild));
  setDonut("exe", [
    { label: "linked source", value: sourceLinked, color: "blue", title: "Source files compiled or included by the build." },
    { label: "external provider", value: externalBuild, color: "purple", title: "External TUs supplied by a confirmed library or host backend in the build inputs. Separate from reconstructed source; each TU counts once." },
    { label: "not included", value: Math.max(0, tus - available), color: "grey",
      title: "No source inclusion or verified external provider found in the build inputs." },
  ], tus ? (available / tus) * 100 : 0, `${fmtInt(available)} / ${fmtInt(tus)} available`);

  renderGaps(data.audit || {});
  text("activeGoal", data.active_goal || "Whole program");
  text("serverTime", fmtTime(data.server_time));

  renderAgents(data.agents || []);
  renderActiveWork(data.active_work || []);
  setQueueData((data.next && data.next.items) || []);
  setEventsData(data.recent_events || []);
  renderGoals(data.goals || []);
  setBlockedData(data.blocked || []);
  if (state.explorer.view === "map") loadProgressMap(true);
  else loadExplorer();
}

function renderAgents(agents) {
  const activeCount = agents.filter((agent) => agent.has_active_work || Number(agent.total || 0) > 0).length;
  text("agentCount", fmtInt(activeCount));
  text("agentSummary", `${fmtInt(agents.length)} users · ${fmtInt(activeCount)} with active work`);
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
      name.appendChild(ribbon("success", "active"));
    }
    if (agent.is_admin) {
      name.appendChild(ribbon("warning", "admin"));
    }
    if (agent.registered && !agent.worker_active) {
      name.appendChild(ribbon("", "disabled"));
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
  // With nothing claimed, the card shows its empty-garage panel instead of the table.
  const table = body.closest(".table-wrap");
  if (table) table.hidden = !items.length;
  if (el("activeWorkEmpty")) el("activeWorkEmpty").hidden = Boolean(items.length);
  if (!items.length) return;
  for (const item of items) {
    const row = document.createElement("tr");
    row.className = "clickable";
    const name = document.createElement("td");
    name.appendChild(tuButton(item.id));
    name.appendChild(div("tu-meta", `${item.source || "unknown"} · ${fmtInt(item.n_funcs)} funcs`));
    const status = document.createElement("td");
    status.appendChild(pill(item.status, item.status.replace("_", " ")));
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
  renderTicker(view.all);
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
  const counter = el("downloadCount");
  if (!counter) return;
  if (n == null) {
    counter.hidden = true;
    return;
  }
  // the drums carry the figure for the eye, the hidden number for everyone else
  const num = counter.querySelector(".dc-num");
  if (num) num.textContent = fmtInt(n);
  setOdometer(el("downloadOdometer"), n, { digits: 3 });
  counter.title = `${fmtInt(n)} downloader${n === 1 ? "" : "s"} (unique addresses per day; the full game and the update each count once)`;
  counter.hidden = false;
}

async function refreshDownload() {
  const group = el("downloadGroup");
  const button = el("downloadBuild");
  if (!group || !button) return;
  try {
    const data = await fetchJson("/api/builds");
    const latest = data && data.latest;
    if (!latest) {
      group.hidden = true;
      state.latestBuild = null;
      return;
    }
    const changed = !state.latestBuild || state.latestBuild.id !== latest.id;
    state.latestBuild = latest;
    state.builds = data.builds || [];
    // the commit rides on the plate (its first seven characters), in words for assistive tech
    const commit = latest.commit_short || String(latest.commit_sha || "").slice(0, 12);
    text("downloadPlateNumber", commit.slice(0, 7).toUpperCase());
    el("downloadPlate").hidden = !commit;
    text("downloadCommit", commit ? `commit ${commit}, ` : "");
    const parts = [];
    if (latest.id != null) parts.push(`No. ${latest.id}`);
    if (latest.built_at) parts.push(relTime(latest.built_at));
    if (latest.size_bytes) parts.push(fmtBytes(latest.size_bytes));
    text("downloadMeta", parts.join(" · ") || "latest");
    button.title = `Download build ${latest.commit_short || latest.commit_sha}: full game (${fmtBytes(latest.size_bytes)})` +
      (latest.update_url ? ` or the exe-only update (${fmtBytes(latest.bundle_size)})` : "");
    setDownloadCount(latest.downloads || 0);
    // leave an open chooser (and any quota note in it) alone unless the build changed
    if (changed || !el("downloadMenu").matches(":popover-open")) renderDownloadMenu();
    group.hidden = false;
  } catch (error) {
    // No builds published yet (or endpoint unavailable): keep the button hidden.
    group.hidden = true;
  }
}

// The button opens a chooser (the shared download menu, a native popover): the full game
// (assets + exe) or the exe-only update for anyone who already has the game folder. The
// server says whether the assets changed since the previous build, which decides between them.
function closeDownloadMenu() {
  const menu = el("downloadMenu");
  if (menu && menu.matches(":popover-open")) menu.hidePopover();
}

function downloadOption(title, size, note, url, kind) {
  const link = document.createElement("a");
  link.className = "b5-download-option";
  link.href = url;
  const icon = span("b5-download-option__icon", "↓");
  icon.setAttribute("aria-hidden", "true");
  link.append(
    icon,
    span("b5-download-option__title", title),
    span("b5-download-option__size", size),
    span("b5-download-option__description", note),
  );
  link.addEventListener("click", (event) => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    startDownload(url, kind);
  });
  return link;
}

function renderDownloadMenu() {
  const menu = el("downloadMenu");
  const b = state.latestBuild;
  if (!menu || !b) return;
  clearNode(menu);
  const header = div("b5-download-menu__header");
  header.append(span("b5-eyebrow", "Choose a package"), span("", b.commit_short || String(b.id || "")));
  menu.appendChild(header);
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
    b.download_url || "/download/latest", "full",
  ));
  if (b.update_url) {
    menu.appendChild(downloadOption(
      "Update only", fmtBytes(b.bundle_size),
      "Burnout_PC.exe, its DLLs and the .cgsmap. Drop them over an existing game folder.",
      b.update_url, "update",
    ));
  }
  const note = document.createElement("footer");
  note.className = "b5-download-menu__note";
  note.id = "downloadNote";
  if (b.update_url && b.assets_changed === false && previous) setDownloadNote(note, assetsNote, "success", "Assets unchanged");
  else if (b.update_url && b.assets_changed === true) setDownloadNote(note, assetsNote, "warning", "Assets changed");
  else setDownloadNote(note, assetsNote);
  menu.appendChild(note);
}

function setDownloadNote(note, message, tone, label) {
  clearNode(note);
  if (label) {
    const status = span("b5-status", label);
    if (tone) status.dataset.tone = tone;
    note.appendChild(status);
  }
  const p = document.createElement("p");
  p.textContent = message;
  note.appendChild(p);
}

// Ask first (HEAD is never counted), then navigate. A 429 means this address spent
// today's allowance for that kind; the menu says so instead of opening a JSON page.
async function startDownload(url, kind) {
  const note = el("downloadNote");
  try {
    const probe = await fetch(url, { method: "HEAD", cache: "no-store" });
    if (probe.status === 429) {
      if (note) {
        setDownloadNote(note, kind === "full"
          ? "This address has used today's full-game downloads (3 per day). The exe-only update has a larger allowance; the full game is back tomorrow."
          : "This address has used today's update downloads. Back tomorrow.", "warning", "Daily limit reached");
      }
      return;
    }
    if (!probe.ok) {
      if (note) setDownloadNote(note, `Download unavailable (HTTP ${probe.status}).`, "danger", "Unavailable");
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
  drawerTitle("Build contents", "Latest PC build");
  const body = el("detailBody");
  body.innerHTML = '<p class="muted-text">Reading zip…</p>';
  try {
    const data = await fetchJson(`/api/builds/${buildId}/contents`, 20000);
    clearNode(body);
    drawerTitle("Build contents", data.filename);
    const summary = div("zip-summary");
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
  external: "external",
};

function statusPill(status) {
  return pill(status, (STATUS_LABELS[status] || status || "—").replace("_", " "));
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
  refreshSelect(sel);
}

function syncStatusOptions() {
  const list =
    state.explorer.tab === "funcs" ? state.explorer.funcStatuses : state.explorer.tuStatuses;
  fillSelect("filterStatus", list, "All statuses");
  el("filterStatus").value = state.explorer.status;
  refreshSelect(el("filterStatus"));
}

function explorerParams() {
  const ex = state.explorer;
  const p = new URLSearchParams();
  if (ex.q) p.set("q", ex.q);
  if (ex.status) p.set("status", ex.status);
  p.set("limit", ex.limit);
  p.set("offset", ex.offset);
  if (ex.tab !== "funcs") {
    if (ex.source) p.set("source", ex.source);
    if (ex.goal) p.set("goal", ex.goal);
    p.set("sort", ex.sort);
    p.set("order", ex.order);
  }
  return p;
}

async function loadExplorer() {
  const ex = state.explorer;
  if (ex.view === "map") return loadProgressMap();
  const path = ex.tab === "funcs" ? "/api/funcs" : "/api/tus";
  const requestId = ++ex.requestId;
  try {
    const data = await fetchJson(`${path}?${explorerParams()}`, 15000);
    if (requestId !== ex.requestId || ex.view !== "list") return;
    ex.total = data.total || 0;
    ex.items = data.items || [];
    if (ex.tab === "funcs") renderFuncRows(ex.items);
    else renderTuRows(ex.items);
    renderExplorerFoot();
  } catch (error) {
    if (requestId !== ex.requestId || ex.view !== "list") return;
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
  if (ex.view !== "list" || ex.tab === "funcs" || !detail || !detail.id || !Array.isArray(ex.items)) return;
  const index = ex.items.findIndex((item) => item.id === detail.id);
  if (index < 0) return;
  ex.items[index] = { ...ex.items[index], ...detail };
  renderTuRows(ex.items);
}

function renderExplorerFoot() {
  const ex = state.explorer;
  text("explorerCount", `${fmtInt(ex.total)} results`);
  createPager(el("explorerPager")).set({ total: ex.total, pageSize: ex.limit, page: Math.floor(ex.offset / ex.limit) + 1 });
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
  const linkage = state.explorer.tab === "linked";
  const columns = ["Translation Unit", "Status", "Funcs", "Source", "Unresolved Deps", "Actor"];
  if (linkage) columns.splice(2, 0, "Available in build");
  setHead(columns);
  const body = el("explorerBody");
  clearNode(body);
  if (!items.length) return emptyRow(body, columns.length, "No translation units match.");
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
    if (linkage) {
      const linked = document.createElement("td");
      const provider = item.external_build_provider;
      const badge = span(`link-state${provider ? " is-external" : item.linked ? " is-linked" : ""}`,
                         provider ? "External provider" : item.linked ? "Linked source" : "Not included");
      badge.title = provider ? provider.name : "Source inclusion evidence from the build.";
      linked.appendChild(badge);
      row.insertBefore(linked, fn);
    }
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
    row.addEventListener("click", () => openDetail(item.tu_id, { functionName: item.name }));
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

function saveExplorerView() {
  try {
    localStorage.setItem("bp-progress-explorer-v1", JSON.stringify({
      view: state.explorer.view, tab: state.explorer.tab,
    }));
  } catch (_) { /* A disabled or full storage area must not break browsing. */ }
}

function syncExplorerView() {
  const ex = state.explorer;
  const isMap = ex.view === "map";
  el("progressMap").classList.toggle("hidden", !isMap);
  el("explorerList").classList.toggle("hidden", isMap);
  createFilterGroup(el("explorerViews")).select(ex.view, { emit: false });
  createFilterGroup(el("explorerTabs")).select(ex.tab, { emit: false });
  // hidden on the native select also hides its themed control
  document.querySelectorAll(".tus-only").forEach((node) => { node.hidden = ex.tab === "funcs"; });
  el("sortBy").hidden = isMap || ex.tab === "funcs";
  el("sortOrder").hidden = isMap || ex.tab === "funcs";
  el("explorerSearch").placeholder = ex.tab === "funcs" ? "Search functions by name…" : "Search by name, source, or path…";
  el("filterStatus").setAttribute("aria-label", ex.tab === "funcs" ? "Function status" : "Translation unit status");
  if (isMap) state.progressMap.controller?.scheduleDraw();
}

async function loadProgressMap(force = false) {
  const map = state.progressMap;
  const ex = state.explorer;
  if (!map.controller || ex.view !== "map") return;
  const needsFunctions = ex.tab === "funcs";
  const renderSnapshot = () => {
    if (ex.view !== "map") return;
    if (ex.tab === "funcs" && !map.snapshot.include_functions) {
      map.pending = true;
      return;
    }
    map.controller.update(map.snapshot, ex.tab, {
      q: ex.q, status: ex.status, source: ex.source, goal: ex.goal,
    });
    el("mapStatus").hidden = true;
    el("progressMapViewport").classList.remove("is-loading");
  };
  if (!force && map.snapshot && (!needsFunctions || map.snapshot.include_functions)) {
    renderSnapshot();
    return;
  }
  if (map.inFlight) { map.pending = true; return; }
  map.inFlight = true;
  if (!map.snapshot || (needsFunctions && !map.snapshot.include_functions)) {
    el("mapStatus").hidden = false;
    el("mapRetry").hidden = true;
    text("mapStatusText", needsFunctions ? "Loading individual functions…" : "Loading translation units…");
    text("mapExplanation", needsFunctions ? "One tile = one function. Loading function statuses…" : "Loading unit statuses…");
    el("mapLegend").replaceChildren();
    el("progressMapViewport").classList.add("is-loading");
  }
  try {
    map.snapshot = await fetchJson(`/api/progress-map?include_functions=${needsFunctions}`, 30000);
    renderSnapshot();
  } catch (error) {
    el("mapStatus").hidden = false;
    text("mapStatusText", `Progress map unavailable: ${error.message}. List view is also available.`);
    el("mapRetry").hidden = false;
  } finally {
    map.inFlight = false;
    if (map.pending) { map.pending = false; loadProgressMap(true); }
  }
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
  try {
    const saved = JSON.parse(localStorage.getItem("bp-progress-explorer-v1") || "null");
    if (["map", "list"].includes(saved?.view)) ex.view = saved.view;
    if (["tus", "funcs", "linked"].includes(saved?.tab)) ex.tab = saved.tab;
  } catch (_) { /* Default to the TU map when preferences are unavailable. */ }
  try {
    state.progressMap.controller = new BPProgressMap.MapController({
      onSummary(summary, mode) {
        if (ex.view === "map") text("explorerCount", `${fmtInt(summary.items)} ${mode === "funcs" ? "functions" : "translation units"}`);
      },
      async onOpen(data) {
        state.progressMap.restoreFocus = true;
        await openDetail(data.tuId, { functionName: data.kind === "function" ? data.name : undefined });
        if (detailIsOpen()) el("detailClose").focus();
      },
    });
  } catch (error) {
    text("mapStatusText", `Map unavailable: ${error.message}. Switch to List view to browse progress.`);
  }
  el("mapRetry").addEventListener("click", () => loadProgressMap(true));
  el("explorerViews").addEventListener("b5:filterchange", (event) => {
    ex.view = event.detail.value;
    ++ex.requestId;
    saveExplorerView();
    syncExplorerView();
    loadExplorer();
  });

  el("explorerTabs").addEventListener("b5:filterchange", (event) => {
    ex.tab = event.detail.value;
    const statuses = ex.tab === "funcs" ? ex.funcStatuses : ex.tuStatuses;
    if (!statuses.includes(ex.status)) ex.status = "";
    saveExplorerView();
    syncExplorerView();
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

  syncExplorerView();

  el("explorerPager").addEventListener("b5:pagechange", (event) => {
    ex.offset = (event.detail.page - 1) * ex.limit;
    loadExplorer();
  });

  el("detailBack").addEventListener("click", goBackDetail);
  el("detailClose").addEventListener("click", closeDetail);
  // Escape and a backdrop click close the native drawer; reset its history the same way.
  el("detailDialog").addEventListener("close", () => {
    if (state.detailNav.current) closeDetail();
  });

  const previewBtn = el("downloadPreview");
  if (previewBtn) {
    previewBtn.addEventListener("click", () => {
      if (state.latestBuild) openBuildContents(state.latestBuild.id);
    });
  }

  loadFacets();
  loadExplorer();
}

/* ---------------- TU detail drawer ---------------- */

function detailIsOpen() {
  return el("detailDialog").open;
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
  btn.hidden = !canGoBack;
  btn.disabled = !canGoBack;
  const previous = state.detailNav.stack[state.detailNav.stack.length - 1];
  btn.title = previous ? `Back to ${detailEntryLabel(previous)}` : "";
}

function goBackDetail() {
  const previous = state.detailNav.stack.pop();
  if (!previous) return updateDetailBack();
  if (previous.type === "profile") openProfile(previous.name, previous.githubUsername, { push: false });
  else if (previous.type === "goal") openGoalDetail(previous.id, { push: false });
  else if (previous.type === "func") {
    if (previous.tu && previous.fn) openFunctionDetail(previous.tu, previous.fn, { push: false });
    else openDetail(previous.tuId, { functionName: previous.name, push: false });
  }
  else if (previous.type === "audit") openAuditFile(previous.id, { push: false });
  else if (previous.type === "stubs") openStubFile(previous.id, { push: false });
  else if (previous.type === "asm") openAsmFile(previous.id, { push: false });
  else openDetail(previous.id, { push: false });
}

function showDetailOverlay() {
  openDialog(el("detailDialog"));
}

function hideDetailOverlay() {
  closeDialog(el("detailDialog"));
}

async function openDetail(tuId, options = {}) {
  const entry = options.functionName
    ? { type: "func", name: options.functionName, tuId }
    : { type: "tu", id: tuId };
  setCurrentDetail(entry, options);
  showDetailOverlay();
  drawerTitle(options.functionName ? "Function" : "Translation unit", options.functionName || tuId);
  el("detailBody").innerHTML = '<p class="muted-text">Loading…</p>';
  try {
    const detail = await fetchJson(`/api/tu?id=${encodeURIComponent(tuId)}`, 15000);
    if (detailEntryKey(state.detailNav.current) !== detailEntryKey(entry)) return;
    refreshExplorerTuRow(detail);
    const fn = options.functionName && detail.funcs?.find((item) => item.name === options.functionName);
    if (fn) openFunctionDetail(detail, fn, { push: false });
    else {
      if (options.functionName) {
        setCurrentDetail({ type: "tu", id: tuId }, { push: false });
        drawerTitle("Translation unit", tuId);
      }
      renderDetail(detail);
    }
  } catch (error) {
    if (detailEntryKey(state.detailNav.current) !== detailEntryKey(entry)) return;
    el("detailBody").innerHTML = "";
    el("detailBody").appendChild(div("muted-text", `Failed to load: ${error.message}`));
  }
}

async function openGoalDetail(goalName, options = {}) {
  setCurrentDetail({ type: "goal", id: goalName }, options);
  showDetailOverlay();
  drawerTitle("Goal", goalName);
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
  drawerTitle("Function", fn.name);
  renderFunctionDetail(tu, fn);
}

async function openProfile(name, githubUsername, options = {}) {
  setCurrentDetail({ type: "profile", name, githubUsername }, options);
  showDetailOverlay();
  drawerTitle("Profile", name);
  el("detailBody").innerHTML = '<p class="muted-text">Loading...</p>';
  try {
    const profile = await fetchJson(`/api/profile?name=${encodeURIComponent(name)}`, 15000);
    state.actorProfiles[profile.name] = profile.github_username || githubUsername || profile.name;
    state.detailNav.current = {
      type: "profile",
      name: profile.name,
      githubUsername: profile.github_username || githubUsername,
    };
    drawerTitle("Profile", profile.name);
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
  if (state.progressMap.restoreFocus) {
    state.progressMap.restoreFocus = false;
    el("progressMapCanvas").focus({ preventScroll: true });
  }
}

// The drawer's header: the kind of view on the red paint, the name it shows under it.
function drawerTitle(kind, name) {
  text("detailKind", kind);
  text("detailTitle", name);
}

function detailSection(title) {
  const wrap = div("detail-section");
  wrap.appendChild(div("detail-section-title b5-drawer-heading", title));
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

// The shared summary tile, which leans inside the drawer.
function profileMetric(label, value, meta) {
  const node = summaryTile(value, label, "compiled");
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

// ---- Profile activity: units laid on a real calendar ----------------------
// The API lists only the days that had activity. The chart puts them on the calendar,
// empty days and weeks included, so a quiet stretch reads as one. The bucket follows the
// span: days up to about six weeks, then weeks up to a year, then months. Dates are UTC.
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const DAY_MS = 86400000;
let activityChartSeq = 0;

const utcDay = (key) => Date.parse(`${key}T00:00:00Z`);
const dayKey = (time) => new Date(time).toISOString().slice(0, 10);
const dayMonth = (time) => `${new Date(time).getUTCDate()} ${MONTHS[new Date(time).getUTCMonth()]}`;
const monthTick = (date) => (date.getUTCMonth() === 0 ? `Jan ${date.getUTCFullYear()}` : MONTHS[date.getUTCMonth()]);

function activityBuckets(points, now = Date.now()) {
  const counts = new Map();
  for (const point of points || []) {
    const count = Number(point.count || 0);
    if (count > 0 && /^\d{4}-\d{2}-\d{2}$/.test(String(point.date || ""))) {
      counts.set(point.date, (counts.get(point.date) || 0) + count);
    }
  }
  if (!counts.size) return null;
  const keys = [...counts.keys()].sort();
  const today = utcDay(dayKey(now));
  const first = utcDay(keys[0]);
  const end = Math.max(today, utcDay(keys[keys.length - 1]));
  const start = Math.min(first, end - 13 * DAY_MS); // never fewer than two weeks
  const spanDays = Math.round((end - start) / DAY_MS) + 1;
  const unit = spanDays <= 45 ? "day" : spanDays <= 371 ? "week" : "month";
  let from = start;
  if (unit === "week") from -= ((new Date(from).getUTCDay() + 6) % 7) * DAY_MS; // back to Monday
  if (unit === "month") from = Date.UTC(new Date(from).getUTCFullYear(), new Date(from).getUTCMonth(), 1);
  const buckets = [];
  while (from <= end) {
    const date = new Date(from);
    const to =
      unit === "day" ? from
      : unit === "week" ? from + 6 * DAY_MS
      : Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 1) - DAY_MS;
    let count = 0;
    let days = 0;
    for (let time = from; time <= to; time += DAY_MS) {
      const value = counts.get(dayKey(time)) || 0;
      count += value;
      if (value) days += 1;
    }
    buckets.push({ from, to, count, days, current: from <= today && today <= to });
    from = to + DAY_MS;
  }
  return { unit, buckets, first, end, activeDays: counts.size };
}

function activityPeriod(unit, bucket) {
  const from = new Date(bucket.from);
  if (unit === "day") return `${WEEKDAYS[from.getUTCDay()]} ${dayMonth(bucket.from)}`;
  if (unit === "month") return `${MONTHS[from.getUTCMonth()]} ${from.getUTCFullYear()}`;
  return from.getUTCMonth() === new Date(bucket.to).getUTCMonth()
    ? `${from.getUTCDate()}–${dayMonth(bucket.to)}`
    : `${dayMonth(bucket.from)} – ${dayMonth(bucket.to)}`;
}

// Time labels under the bars: Mondays for days, the week that holds the 1st for weeks,
// every month for months; at most seven, so they never crowd. Day labels name the month
// only on the first label and where it changes (26 Oct, 2 Nov, 9, 16).
function activityTicks(unit, buckets) {
  let ticks = [];
  buckets.forEach((bucket, index) => {
    const from = new Date(bucket.from);
    const to = new Date(bucket.to);
    if (unit === "day" && from.getUTCDay() === 1) ticks.push({ index, time: bucket.from });
    else if (unit === "month") ticks.push({ index, label: monthTick(from) });
    else if (unit === "week" && from.getUTCDate() === 1) ticks.push({ index, label: monthTick(from) });
    else if (unit === "week" && from.getUTCMonth() !== to.getUTCMonth()) ticks.push({ index, label: monthTick(to) });
  });
  if (unit !== "month" && (!ticks.length || ticks[0].index >= 4)) {
    ticks.unshift({ index: 0, time: buckets[0].from, label: unit === "week" ? monthTick(new Date(buckets[0].from)) : undefined });
  }
  if (ticks.length > 7) {
    const every = Math.ceil(ticks.length / 7);
    ticks = ticks.filter((_, i) => i % every === 0);
  }
  if (unit === "day") {
    let month = -1;
    for (const tick of ticks) {
      const date = new Date(tick.time);
      tick.label = date.getUTCMonth() === month ? String(date.getUTCDate()) : dayMonth(tick.time);
      month = date.getUTCMonth();
    }
  }
  return ticks;
}

// Round gridline values from 0 up to the tallest bar, three or four of them.
function activityScale(max) {
  const raw = max / 4;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / magnitude;
  const step = Math.max(1, (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * magnitude);
  const values = [];
  for (let value = 0; value <= max; value += step) values.push(value);
  return values;
}

// Place a label along the time axis; near either end it hangs inward instead of centring.
function placeAlong(node, fraction) {
  node.style.setProperty("--x", String(fraction));
  if (fraction < 0.05) node.classList.add("is-start");
  else if (fraction > 0.95) node.classList.add("is-end");
}

function profileActivity(points) {
  const figure = document.createElement("figure");
  figure.className = "activity";
  const data = activityBuckets(points);
  if (!data) {
    figure.appendChild(div("muted-text", "No activity yet."));
    return figure;
  }
  const { unit, buckets } = data;
  const max = Math.max(...buckets.map((bucket) => bucket.count), 1);
  const peak = buckets.reduce((best, bucket, index) => (bucket.count > buckets[best].count ? index : best), 0);
  const seq = ++activityChartSeq;
  const firstDate = new Date(data.first);
  const endDate = new Date(data.end);
  const range = `${dayMonth(data.first)}${firstDate.getUTCFullYear() !== endDate.getUTCFullYear() ? ` ${firstDate.getUTCFullYear()}` : ""} – ${dayMonth(data.end)} ${endDate.getUTCFullYear()}`;
  const daysText = (count) => `${fmtInt(count)} active day${count === 1 ? "" : "s"}`;

  const head = document.createElement("figcaption");
  head.className = "activity-head";
  head.append(span("activity-title", `Units per ${unit}`), span("activity-range", `${range} · ${daysText(data.activeDays)}`));

  const chart = div("activity-chart");
  const yAxis = div("activity-y");
  const grid = div("activity-grid");
  yAxis.setAttribute("aria-hidden", "true");
  grid.setAttribute("aria-hidden", "true");
  const scale = activityScale(max);
  for (const value of scale) {
    const label = span("", fmtInt(value));
    label.style.setProperty("--at", String(value / max));
    yAxis.appendChild(label);
    if (value > 0) {
      const line = document.createElement("i");
      line.style.setProperty("--at", String(value / max));
      grid.appendChild(line);
    }
  }
  yAxis.style.width = `${Math.max(...scale.map((value) => fmtInt(value).length))}ch`;

  const bars = div("activity-bars");
  bars.tabIndex = 0;
  bars.setAttribute("role", "listbox");
  bars.setAttribute("aria-label", `Units per ${unit}, ${range}. Arrow keys step through the ${unit}s.`);
  const describe = (bucket) =>
    `${activityPeriod(unit, bucket)}${bucket.current ? " (so far)" : ""}: ${fmtInt(bucket.count)} unit${bucket.count === 1 ? "" : "s"}` +
    (unit === "day" ? "" : `, ${daysText(bucket.days)}`);
  const cols = buckets.map((bucket, index) => {
    const col = div(index === peak ? "activity-col is-peak" : "activity-col");
    col.id = `activity-${seq}-${index}`;
    col.setAttribute("role", "option");
    col.setAttribute("aria-selected", "false");
    col.setAttribute("aria-label", describe(bucket));
    col.style.setProperty("--v", String(bucket.count / max));
    if (bucket.count) col.appendChild(span("activity-bar"));
    bars.appendChild(col);
    return col;
  });

  const peakLabel = span("activity-peak", fmtInt(buckets[peak].count));
  peakLabel.setAttribute("aria-hidden", "true");
  placeAlong(peakLabel, (peak + 0.5) / buckets.length);
  const frame = div("activity-frame");
  frame.append(grid, bars, peakLabel);

  const xAxis = div("activity-x");
  xAxis.setAttribute("aria-hidden", "true");
  for (const tick of activityTicks(unit, buckets)) {
    const label = span("", tick.label);
    placeAlong(label, (tick.index + 0.5) / buckets.length);
    xAxis.appendChild(label);
  }
  chart.append(yAxis, frame, xAxis);

  // The readout names the bar under the pointer, or the selected one (the busiest at first).
  const readout = div("activity-readout");
  readout.setAttribute("aria-hidden", "true");
  const when = span("activity-when");
  const tag = span("activity-tag");
  const value = document.createElement("strong");
  value.className = "activity-value";
  const units = span("activity-unit");
  const days = span("activity-days");
  readout.append(when, tag, value, units, days);

  let selected = peak;
  const show = (index) => {
    const bucket = buckets[index];
    when.textContent = activityPeriod(unit, bucket);
    tag.textContent = bucket.current ? "so far" : index === peak ? `busiest ${unit}` : "";
    tag.hidden = !tag.textContent;
    value.textContent = fmtInt(bucket.count);
    units.textContent = bucket.count === 1 ? "unit" : "units";
    days.textContent = unit === "day" ? "" : daysText(bucket.days);
    cols.forEach((col, i) => col.classList.toggle("is-shown", i === index));
  };
  const select = (index) => {
    selected = Math.max(0, Math.min(buckets.length - 1, index));
    cols.forEach((col, i) => col.setAttribute("aria-selected", String(i === selected)));
    bars.setAttribute("aria-activedescendant", cols[selected].id);
    show(selected);
  };
  const indexOf = (event) => cols.indexOf(event.target.closest(".activity-col"));
  bars.addEventListener("pointermove", (event) => {
    const index = indexOf(event);
    if (index >= 0) show(index);
  });
  bars.addEventListener("pointerleave", () => show(selected));
  bars.addEventListener("click", (event) => {
    const index = indexOf(event);
    if (index >= 0) select(index);
  });
  bars.addEventListener("keydown", (event) => {
    const moves = { ArrowLeft: -1, ArrowDown: -1, ArrowRight: 1, ArrowUp: 1 };
    let next = null;
    if (event.key in moves) next = selected + moves[event.key];
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = buckets.length - 1;
    if (next === null) return;
    event.preventDefault();
    select(next);
  });
  select(peak);

  const note = div(
    "activity-note",
    "Each unit holding their code counts once, on the day its file last changed — by anyone, so a big shared commit lands in everyone's chart.",
  );
  figure.append(head, chart, readout, note);
  return figure;
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
  activity.appendChild(profileActivity(profile.activity_by_day || []));
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

// A path or qualified name that wraps at its separators (/ and ::), not mid-word.
function pathNode(className, text) {
  const node = span(className, "");
  String(text ?? "")
    .split(/(?<=\/|::)/)
    .forEach((part, index) => {
      if (index) node.appendChild(document.createElement("wbr"));
      node.appendChild(document.createTextNode(part));
    });
  return node;
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
    main.appendChild(pathNode("dep-name", item.id));
    const meta = [];
    if (item.source) meta.push(item.source);
    if (Number(item.lines || 0) > 0) meta.push(`${fmtInt(item.lines)} lines`);
    else if (item.basis === "review_pass") meta.push("reviewed");
    if (item.percent) meta.push(`${item.percent}%`);
    if (meta.length) main.appendChild(div("tu-meta", meta.join(" | ")));
    if (item.dest_path) main.appendChild(pathNode("tu-path", item.dest_path));
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
    main.appendChild(pathNode("dep-name", item.name));
    const meta = [];
    if (Number(item.lines || 0) > 0) meta.push(`${fmtInt(item.lines)} lines`);
    else if (item.basis === "completed_by") meta.push("completed");
    if (item.percent) meta.push(`${item.percent}%`);
    if (meta.length) main.appendChild(div("tu-meta", meta.join(" | ")));
    if (item.tu_id) main.appendChild(pathNode("tu-path", item.tu_id));
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
  facts.appendChild(kv("Build availability", d.external_build_provider ? "External provider" : d.linked ? "Linked source" : "Not included"));
  if (d.external_build_provider) {
    facts.appendChild(kv("Provider", d.external_build_provider.name));
    for (const proof of d.external_build_provider.evidence || []) {
      facts.appendChild(kv("Build evidence", `${proof.file}${proof.line ? `:${proof.line}` : ""} · ${proof.detail}`));
    }
  }
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
      if (fn.audit_weight) row.appendChild(pill("cat-high", `${fmtInt(fn.audit_weight)} audit`));
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
  for (const key of ["todo", "in_progress", "compiled", "blocked", "done", "external"]) {
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

/* ---------------- License and ticker ---------------- */

// The game's license classes, from a learner's permit to Elite, on the share of
// translation units complete (done and external, as the first ring counts them).
const LICENSE_RANKS = [
  { min: 0, name: "Learner's permit", badge: "L" },
  { min: 20, name: "D-class license", badge: "D" },
  { min: 40, name: "C-class license", badge: "C" },
  { min: 60, name: "B-class license", badge: "B" },
  { min: 80, name: "A-class license", badge: "A" },
  { min: 95, name: "Burnout license", badge: "B5" },
  { min: 100, name: "Elite license", badge: "Elite" },
];
// Ring id -> license meter id.
const RING_METERS = { tu: "meterUnits", fn: "meterFuncs", exe: "meterBuild", bytematch: "meterMatch" };

function reportRing(id, pct) {
  const meter = el(RING_METERS[id]);
  if (!meter || !Number.isFinite(pct)) return;
  setProgress(meter, pct);
  meter.removeAttribute("aria-valuetext");
  if (id === "tu") text("licenseChipValue", `${pct.toFixed(1)}%`);
}

// The license chip arrives in the header once the rings have gone under it, and leaves
// when they come back, taking the open card with it. The card opens under the chip.
function initLicenseChip() {
  const chip = el("licenseChip");
  const card = el("licenseCard");
  const summary = document.querySelector(".wp-metrics");
  if (!chip || !card || !summary) return;
  const header = document.querySelector(".b5-site-header");
  // A scroll check, not an IntersectionObserver: on a phone the rings start below the
  // fold, and a jump past them (a link to a section) never crosses an observer's edge.
  let frame = 0;
  const update = () => {
    frame = 0;
    const past = summary.getBoundingClientRect().bottom < (header ? header.offsetHeight : 106);
    if (past === chip.hasAttribute("data-b5-shown")) return;
    chip.toggleAttribute("data-b5-shown", past);
    if (!past && card.matches(":popover-open")) card.hidePopover();
  };
  const schedule = () => {
    if (!frame) frame = requestAnimationFrame(update);
  };
  window.addEventListener("scroll", schedule, { passive: true });
  const place = () => {
    const box = chip.getBoundingClientRect();
    const width = Math.min(352, window.innerWidth - 32);
    card.style.setProperty("--license-top", `${Math.round(box.bottom + 12)}px`);
    card.style.setProperty("--license-left", `${Math.round(Math.max(16, Math.min(box.left, window.innerWidth - width - 16)))}px`);
  };
  card.addEventListener("beforetoggle", (event) => {
    if (event.newState !== "open") return;
    // a click as the chip fades out (the rings coming back) does not open it
    if (!chip.hasAttribute("data-b5-shown")) event.preventDefault();
    else place();
  });
  card.addEventListener("toggle", (event) => chip.setAttribute("aria-expanded", String(event.newState === "open")));
  window.addEventListener("resize", () => {
    schedule();
    if (card.matches(":popover-open")) place();
  });
  update();
}

function renderLicense(complete, tus, sourceCommit) {
  const pct = tus ? (complete / tus) * 100 : 0;
  const rank = [...LICENSE_RANKS].reverse().find((r) => pct >= r.min) || LICENSE_RANKS[0];
  const next = LICENSE_RANKS.find((r) => r.min > pct);
  text("licenseRank", rank.name);
  text("licenseClass", rank.badge);
  text("licenseChipRank", rank.name);
  text("licenseChipClass", rank.badge);
  if (next) {
    const needed = Math.max(0, Math.ceil((next.min / 100) * tus) - complete);
    text("licenseNext", `Next: ${next.name} at ${next.min}% · ${fmtInt(needed)} ${needed === 1 ? "unit" : "units"} to go`);
  } else {
    text("licenseNext", "Every unit complete: Paradise City is rebuilt.");
  }
  text("licenseNumber", sourceCommit ? `No. ${String(sourceCommit).slice(0, 10)}` : "No. —");
}

// Crash FM: the newest events, twice over (the copy hidden from assistive technology)
// so the crawl loops without a seam. It runs at about four letters a second, whatever
// the items' length and the LED size. The library's enhance() wires its pause toggle.
function renderTicker(events) {
  const track = el("tickerTrack");
  if (!track) return;
  const latest = [...events]
    .sort((a, b) => eventSortTime(b) - eventSortTime(a) || (b.id || 0) - (a.id || 0))
    .slice(0, 14);
  clearNode(track);
  if (!latest.length) {
    track.appendChild(tickerItem("Quiet", "no events on the work server yet"));
    track.style.animation = "none";
    return;
  }
  track.style.removeProperty("animation");
  for (const copy of [false, true]) {
    for (const event of latest) {
      const where = event.tu_id && event.tu_id !== "None" ? String(event.tu_id).split("/").pop() : "";
      const item = tickerItem(event.action || "event", [event.agent || "server", where, relTime(event.ts)].filter(Boolean).join(" · "));
      if (copy) item.setAttribute("aria-hidden", "true");
      track.appendChild(item);
    }
  }
  const pace = () => {
    // a letter is six LEDs wide; the copy makes the track twice the loop
    const pitch = parseFloat(getComputedStyle(track).getPropertyValue("--b5-ticker-pitch")) || 3;
    const loop = track.offsetWidth / 2;
    if (loop) track.style.setProperty("--b5-ticker-duration", `${Math.max(12, Math.round(loop / (pitch * 6 * 4)))}s`);
  };
  if (document.fonts) document.fonts.ready.then(pace);
  else pace();
}

function tickerItem(title, rest) {
  const item = document.createElement("li");
  item.className = "b5-ticker__item";
  const strong = document.createElement("strong");
  strong.textContent = title;
  item.append(strong, document.createTextNode(` ${rest}`));
  return item;
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

// Native selects marked data-b5-select get the shared themed dropdown; download groups,
// menu filter groups and pagers are wired the same way. One enhancement root.
enhance(document.body);

refresh();
connectStream();
refreshGithub();
refreshDownload();
initExplorer();
initEvidence();
initMiniPanels();
initLicenseChip();
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

async function openAuditFile(file, options = {}) {
  setCurrentDetail({ type: "audit", id: file }, options);
  showDetailOverlay();
  drawerTitle("Console audit", file);
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
  drawerTitle("Stubs", file);
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
    ["weight", "Gaps", "done"],
    ["no_body", "No body", "blocked"],
    // one MISSING_CASE item is a function's whole list of missing ids: a switch, not an id
    ["missing_case", "Switch gaps", "progress"],
    ["missing_event", "Event posts", "progress"],
    ["missing_callee", "Callees", "progress"],
    ["missing_assert", "Asserts", "todo"],
  ];
  for (const [key, label, cls] of chips) wrap.appendChild(summaryTile(fmtInt(rollup[key] || 0), label, cls));
  return wrap;
}

function renderAuditFile(data) {
  const body = el("detailBody");
  body.innerHTML = "";
  const rollup = data.rollup || {};

  // the header already says "Console audit"; the banner keeps the unit's link
  if (data.tu_id) {
    const banner = div("detail-banner");
    banner.appendChild(tuButton(data.tu_id, "tag goal-tag"));
    body.appendChild(banner);
  }

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
      if (fn.weight) head.push(pill("cat-high", `${fmtInt(fn.weight)} high-signal`));
      if (fn.flagged) head.push(pill("todo", "flagged in source"));
      if (fn.helpers) head.push(pill("todo", `+${fmtInt(fn.helpers)} helpers`));
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
      if (st.live_callers && st.live_callers.length) head.push(pill("live", "live today"));
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
  drawerTitle("Instruction shape", file);
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
    tiers.appendChild(summaryTile(fmtInt(rollup[key] || 0), label, cls));
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
      if (fn.score != null) head.push(pill("todo", `score ${fn.score}`));
      if (fn.flags && fn.flags.total) head.push(pill("todo", `flagged ${fmtInt(fn.flags.total)}`));
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
  search.className = "b5-input fb-search";
  search.placeholder = opts.placeholder || "Filter by function name or content\u2026";
  search.autocomplete = "off";
  controls.appendChild(search);
  const chips = div("fb-chips");
  controls.appendChild(chips);
  const tools = div("fb-tools");
  const sortSel = document.createElement("select");
  sortSel.className = "b5-select fb-sort";
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
  expandBtn.className = "b5-button b5-button--ink";
  expandBtn.textContent = "Expand all";
  const collapseBtn = document.createElement("button");
  collapseBtn.type = "button";
  collapseBtn.className = "b5-button b5-button--ink";
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
  return pill(`tier-${tier}`, String(tier || "").toLowerCase());
}

function stubRow(stub) {
  const row = div("audit-row");
  const head = div("audit-row-head");
  head.appendChild(span("dep-name", stub.name));
  head.appendChild(tierPill(stub.tier));
  if (stub.live_callers && stub.live_callers.length) head.appendChild(pill("live", "live today"));
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
    open.className = "b5-button b5-button--ink";
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
    if (fnAudit.weight) head.appendChild(pill("cat-high", `${fmtInt(fnAudit.weight)} high-signal`));
    if (fnAudit.flagged) head.appendChild(pill("todo", "flagged"));
    row.appendChild(head);
    row.appendChild(findingsList(fnAudit.findings || {}));
    section.appendChild(row);
  }
  if (names.length > 40) section.appendChild(div("audit-meta", `${fmtInt(names.length - 40)} more functions in the file audit.`));
  const asmNames = Object.keys(asm).sort((a, b) =>
    (asm[a].score == null ? 101 : asm[a].score) - (asm[b].score == null ? 101 : asm[b].score));
  if (asmNames.length) {
    const sub = div("detail-section-title b5-drawer-heading");
    const r = audit.asm_rollup || {};
    sub.textContent = `Instruction shape (${fmtInt(asmNames.length)} of this TU's functions in the built exe` +
      (r.functions ? `; file: ${fmtInt(r.a || 0)} A, ${fmtInt(r.b || 0)} B, ${fmtInt(r.c || 0)} C` : "") + ")";
    sub.style.marginTop = "14px";
    section.appendChild(sub);
    for (const name of asmNames.slice(0, 40)) section.appendChild(asmRow({ name, ...asm[name] }));
    if (asmNames.length > 40) section.appendChild(div("audit-meta", `${fmtInt(asmNames.length - 40)} more in the file's instruction-shape view.`));
  }
  if (stubs.length) {
    const sub = div("detail-section-title b5-drawer-heading");
    sub.textContent = `Stubs in this file (${fmtInt(stubs.length)})`;
    sub.style.marginTop = "14px";
    section.appendChild(sub);
    for (const stub of stubs.slice(0, 40)) section.appendChild(stubRow(stub));
    if (stubs.length > 40) {
      const open = document.createElement("button");
      open.type = "button";
      open.className = "b5-button b5-button--ink";
      open.textContent = `All ${fmtInt(stubs.length)} stubs`;
      open.addEventListener("click", () => openStubFile(audit.file));
      section.appendChild(open);
    }
  }
  return section;
}

/* ---------------- Donut rings ---------------- */

// One arc per state. `segments` = [{label, value, color, title}], drawn in order from
// 12 o'clock. Callers can pass an aggregate percentage for several segments.
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
  reportRing(id, pct);
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

// The Console gaps panel: the latest audits' totals label its filters; the lists load
// themselves (initEvidence). No score: a gap is a lead to check, not a measure of progress.
function renderGaps(audit) {
  state.evidence.summary = audit;
  renderEvidenceSummary();
}

/* ---------------- Evolution: every ring over time ---------------- */
// group, key, label, colour, share-of (null = counts only; hidden in share mode)
const EVOLUTION_SERIES = [
  { group: "Translation units", key: "tu_done", label: "done", color: "#6fcf57", of: "tu_total" },
  { group: "Translation units", key: "tu_compiled", label: "compiled", color: "#edbd54", of: "tu_total" },
  { group: "Translation units", key: "tu_in_progress", label: "in progress", color: "#57a8e0", of: "tu_total" },
  { group: "Translation units", key: "tu_blocked", label: "blocked", color: "#e72530", of: "tu_total" },
  { group: "Translation units", key: "tu_external", label: "external", color: "#b294df", of: "tu_total" },
  { group: "Translation units", key: "tu_todo", label: "todo", color: "#8a7f72", of: "tu_total" },
  { group: "Translation units", key: "tu_total", label: "tracked", color: "#d9cfc0", of: null },
  { group: "Functions", key: "funcs_done", label: "covered", color: "#f2b53f", of: "funcs_total" },
  { group: "Functions", key: "funcs_external", label: "external", color: "#b294df", of: "funcs_total" },
  { group: "Functions", key: "funcs_named_uncovered", label: "named, uncovered", color: "#a08e6c", of: "funcs_total" },
  { group: "Functions", key: "funcs_unidentified", label: "unidentified", color: "#6a6159", of: "funcs_total" },
  { group: "Functions", key: "funcs_total", label: "in the binary", color: "#efe6d2", of: null },
  { group: "Executable", key: "tu_linked", label: "linked", color: "#57a8e0", of: "tu_total" },
  { group: "Executable", key: "tu_available", label: "available in build", color: "#d9cfc0", of: "tu_total" },
  { group: "Executable", key: "tu_source_linked", label: "linked source", color: "#57a8e0", of: "tu_total" },
  { group: "Executable", key: "tu_external_build", label: "external provider", color: "#b294df", of: "tu_total" },
  { group: "Byte matching (X360)", key: "bm_exact", label: "exact functions", color: "#6fcf57", of: "bm_functions" },
  { group: "Byte matching (X360)", key: "bm_nonmatching", label: "compiled, not matching", color: "#d7742a", of: "bm_functions" },
  { group: "Byte matching (X360)", key: "bm_exact_bytes", label: "exact code bytes", color: "#3ec9b0", of: "bm_bytes" },
];
const EVOLUTION_DEFAULT_ON = ["tu_done", "funcs_done", "tu_linked"];
const EVOLUTION_STORE_KEY = "bp.evolution";

state.evolution = { points: [], range: 0, mode: "count", on: new Set(EVOLUTION_DEFAULT_ON), loadedAt: 0, wired: false, hot: null };
try {
  const saved = JSON.parse(localStorage.getItem(EVOLUTION_STORE_KEY) || "null");
  if (saved && typeof saved === "object") {
    if (Number.isFinite(saved.range)) state.evolution.range = saved.range;
    if (saved.mode === "share" || saved.mode === "count") state.evolution.mode = saved.mode;
    // a saved choice may name series that no longer exist: keep the ones that do
    const known = new Set(EVOLUTION_SERIES.map((s) => s.key));
    const on = Array.isArray(saved.on) ? saved.on.filter((key) => known.has(key)) : [];
    if (on.length) state.evolution.on = new Set(on);
  }
} catch (_) { /* a private window or blocked storage: defaults */ }

function saveEvolutionPrefs() {
  try {
    localStorage.setItem(EVOLUTION_STORE_KEY, JSON.stringify({
      range: state.evolution.range, mode: state.evolution.mode, on: [...state.evolution.on],
    }));
  } catch (_) { /* nothing to do: the choice still holds for this page */ }
}

async function loadEvolution(force = false) {
  const ev = state.evolution;
  if (!el("evolutionChart")) return;
  // the series move a few times a day at most: refetch at most every 10 minutes
  if (!force && ev.loadedAt && Date.now() - ev.loadedAt < 10 * 60 * 1000) return;
  try {
    const body = await fetchJson("/api/history", 20000);
    ev.points = (body && body.points) || [];
    ev.loadedAt = Date.now();
    renderEvolution();
  } catch (error) {
    const host = el("evolutionChart");
    if (host && !host.querySelector("svg")) {
      clearNode(host);
      host.appendChild(div("muted-text vh-empty", `History unavailable: ${error.message}`));
    }
  }
}

function wireEvolution() {
  const ev = state.evolution;
  if (ev.wired) return;
  ev.wired = true;
  const range = el("evolutionRange"), mode = el("evolutionMode");
  if (range) range.addEventListener("b5:filterchange", (event) => {
    ev.range = Number(event.detail.value) || 0;
    saveEvolutionPrefs();
    renderEvolution();
  });
  if (mode) mode.addEventListener("b5:filterchange", (event) => {
    ev.mode = event.detail.value === "share" ? "share" : "count";
    saveEvolutionPrefs();
    renderEvolution();
  });
  const picker = el("evolutionPicker");
  if (picker) picker.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-key]");
    if (!b || b.disabled) return;
    if (ev.on.has(b.dataset.key)) ev.on.delete(b.dataset.key); else ev.on.add(b.dataset.key);
    saveEvolutionPrefs();
    renderEvolution();
  });
  let timer = null;
  window.addEventListener("resize", () => {
    clearTimeout(timer);
    timer = setTimeout(() => { if (ev.points.length) drawEvolutionChart(); }, 150);
  });
}

function evolutionVisible() {
  const ev = state.evolution;
  return EVOLUTION_SERIES.filter((sr) => ev.on.has(sr.key) && (ev.mode === "count" || sr.of));
}

function evolutionValue(sr, p) {
  const v = p[sr.key];
  if (v == null) return null;
  if (state.evolution.mode === "count") return Number(v);
  const d = Number(p[sr.of]);
  return d > 0 ? (Number(v) / d) * 100 : null;
}

function evolutionPoints() {
  const ev = state.evolution;
  if (!ev.range) return ev.points;
  const cutoff = new Date(Date.now() - ev.range * 86400000).toISOString().slice(0, 10);
  return ev.points.filter((p) => p.date >= cutoff);
}

function renderEvolution() {
  wireEvolution();
  const ev = state.evolution;
  createFilterGroup(el("evolutionRange")).select(String(ev.range || 0), { emit: false });
  createFilterGroup(el("evolutionMode")).select(ev.mode, { emit: false });
  // the picker: one chip per series, grouped by ring
  const picker = el("evolutionPicker");
  if (picker) {
    clearNode(picker);
    const groups = new Map();
    for (const sr of EVOLUTION_SERIES) {
      if (!groups.has(sr.group)) groups.set(sr.group, []);
      groups.get(sr.group).push(sr);
    }
    for (const [name, list] of groups) {
      const g = div("evo-group");
      g.appendChild(span("evo-group-name", name));
      for (const sr of list) {
        const b = document.createElement("button");
        b.type = "button";
        b.dataset.key = sr.key;
        const countsOnly = ev.mode === "share" && !sr.of;
        b.className = `evo-chip${ev.on.has(sr.key) && !countsOnly ? " on" : ""}`;
        if (countsOnly) { b.disabled = true; b.title = "A count with no natural total: shown in Counts only."; }
        const sw = span("sw", "");
        sw.style.background = sr.color;
        b.appendChild(sw);
        b.appendChild(document.createTextNode(sr.label));
        g.appendChild(b);
      }
      picker.appendChild(g);
    }
  }
  drawEvolutionChart();
}

function evolutionNice(max) {
  if (max <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(max)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= max) return m * p;
  return 10 * p;
}

function drawEvolutionChart() {
  const host = el("evolutionChart"), legend = el("evolutionLegend");
  if (!host) return;
  clearNode(host);
  if (legend) clearNode(legend);
  const ev = state.evolution;
  const points = evolutionPoints().filter((p) => Number.isFinite(Date.parse(p.ts)));
  const series = evolutionVisible();
  // the span in days, not the number of measurements (two sources add a point each)
  const spanDays = points.length
    ? Math.round((Date.parse(points[points.length - 1].ts) - Date.parse(points[0].ts)) / 86400000) + 1
    : 0;
  text("evolutionCount", points.length
    ? `${fmtInt(spanDays)} days \u00b7 ${points[0].date} \u2192 ${points[points.length - 1].date}`
    : "\u00b7");
  if (points.length < 2) {
    host.appendChild(div("muted-text vh-empty", ev.points.length
      ? "Not enough points in this range yet."
      : "No history yet: the first snapshot is taken at the next workflow import."));
    return;
  }
  if (!series.length) {
    host.appendChild(div("muted-text vh-empty", "Pick at least one series above."));
    return;
  }
  const ns = "http://www.w3.org/2000/svg";
  const W = Math.max(280, Math.round(host.clientWidth || 600)), H = 280;
  const padL = 54, padR = 14, padT = 12, padB = 26;
  const t0 = Date.parse(points[0].ts), t1 = Date.parse(points[points.length - 1].ts);
  const tSpan = Math.max(1, t1 - t0);   // not `span`: that is the element helper
  const x = (t) => padL + ((t - t0) / tSpan) * (W - padL - padR);
  const values = series.map((sr) => points.map((p) => evolutionValue(sr, p)));
  let top = 0;
  for (const col of values) for (const v of col) if (v != null && v > top) top = v;
  top = ev.mode === "share" ? 100 : evolutionNice(top);
  const y = (v) => padT + (1 - v / top) * (H - padT - padB);
  const fmtV = (v) => ev.mode === "share" ? `${v.toFixed(1)}%` : fmtInt(Math.round(v));

  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("width", String(W));
  svg.setAttribute("height", String(H));
  // horizontal grid + labels
  for (let i = 0; i <= 4; i++) {
    const v = (top * i) / 4;
    const g = document.createElementNS(ns, "line");
    g.setAttribute("class", "evo-grid");
    g.setAttribute("x1", padL); g.setAttribute("x2", W - padR);
    g.setAttribute("y1", y(v).toFixed(1)); g.setAttribute("y2", y(v).toFixed(1));
    svg.appendChild(g);
    const t = document.createElementNS(ns, "text");
    t.setAttribute("class", "evo-axis");
    t.setAttribute("x", padL - 6); t.setAttribute("y", (y(v) + 3.5).toFixed(1));
    t.setAttribute("text-anchor", "end");
    t.textContent = fmtV(v);
    svg.appendChild(t);
  }
  // a tick at the first point of every month
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  let lastMonth = points[0].date.slice(0, 7);
  points.forEach((p, i) => {
    const m = p.date.slice(0, 7);
    if (i && m !== lastMonth) {
      const xx = x(Date.parse(p.ts));
      const l = document.createElementNS(ns, "line");
      l.setAttribute("class", "evo-grid evo-month");
      l.setAttribute("x1", xx.toFixed(1)); l.setAttribute("x2", xx.toFixed(1));
      l.setAttribute("y1", padT); l.setAttribute("y2", H - padB);
      svg.appendChild(l);
      const t = document.createElementNS(ns, "text");
      t.setAttribute("class", "evo-axis");
      t.setAttribute("x", (xx + 4).toFixed(1)); t.setAttribute("y", H - padB + 14);
      t.textContent = months[Number(m.slice(5, 7)) - 1];
      svg.appendChild(t);
    }
    lastMonth = m;
  });
  const first = document.createElementNS(ns, "text");
  first.setAttribute("class", "evo-axis"); first.setAttribute("x", padL); first.setAttribute("y", H - 4);
  first.textContent = points[0].date;
  svg.appendChild(first);
  const last = document.createElementNS(ns, "text");
  last.setAttribute("class", "evo-axis"); last.setAttribute("x", W - padR); last.setAttribute("y", H - 4);
  last.setAttribute("text-anchor", "end");
  last.textContent = points[points.length - 1].date;
  svg.appendChild(last);
  // the lines: a null value breaks the line (a series that did not exist yet)
  const lines = [];
  series.forEach((sr, si) => {
    let d = "", pen = false;
    points.forEach((p, i) => {
      const v = values[si][i];
      if (v == null) { pen = false; return; }
      d += `${pen ? "L" : "M"}${x(Date.parse(p.ts)).toFixed(1)},${y(v).toFixed(1)} `;
      pen = true;
    });
    const path = document.createElementNS(ns, "path");
    path.setAttribute("class", "evo-line");
    path.setAttribute("d", d.trim());
    path.setAttribute("stroke", sr.color);
    path.dataset.key = sr.key;
    svg.appendChild(path);
    lines.push(path);
  });
  // hover: crosshair + markers + a tooltip listing every visible series at that point
  const cross = document.createElementNS(ns, "line");
  cross.setAttribute("class", "evo-cross");
  cross.setAttribute("y1", padT); cross.setAttribute("y2", H - padB);
  cross.style.display = "none";
  svg.appendChild(cross);
  const marks = series.map((sr) => {
    const c = document.createElementNS(ns, "circle");
    c.setAttribute("r", "3.2");
    c.setAttribute("fill", sr.color);
    c.setAttribute("class", "evo-mark");
    c.style.display = "none";
    svg.appendChild(c);
    return c;
  });
  const tip = div("evo-tip");
  tip.style.display = "none";
  host.appendChild(svg);
  host.appendChild(tip);
  const xs = points.map((p) => x(Date.parse(p.ts)));
  const show = (clientX) => {
    const rect = svg.getBoundingClientRect();
    const px = ((clientX - rect.left) / rect.width) * W;
    let best = 0;
    for (let i = 1; i < xs.length; i++) if (Math.abs(xs[i] - px) < Math.abs(xs[best] - px)) best = i;
    const p = points[best];
    cross.setAttribute("x1", xs[best].toFixed(1)); cross.setAttribute("x2", xs[best].toFixed(1));
    cross.style.display = "";
    clearNode(tip);
    const head = div("evo-tip-head", p.date);
    if (p.commit) head.appendChild(span("evo-tip-commit", ` \u00b7 ${String(p.commit).slice(0, 7)}`));
    tip.appendChild(head);
    series.forEach((sr, si) => {
      const v = values[si][best];
      if (v == null) { marks[si].style.display = "none"; return; }
      marks[si].setAttribute("cx", xs[best].toFixed(1)); marks[si].setAttribute("cy", y(v).toFixed(1));
      marks[si].style.display = "";
      const row = div("evo-tip-row");
      const sw = span("sw", ""); sw.style.background = sr.color;
      const l = span("evo-tip-label", ""); l.appendChild(sw); l.appendChild(document.createTextNode(`${sr.group === "Translation units" ? "TUs " : sr.group === "Functions" ? "functions " : ""}${sr.label}`));
      row.appendChild(l);
      row.appendChild(span("evo-tip-value", fmtV(v)));
      tip.appendChild(row);
    });
    tip.style.display = "";
    const hostRect = host.getBoundingClientRect();
    const left = ((xs[best] / W) * rect.width) + (rect.left - hostRect.left);
    const flip = left > hostRect.width * 0.6;
    tip.style.left = flip ? "" : `${Math.round(left + 12)}px`;
    tip.style.right = flip ? `${Math.round(hostRect.width - left + 12)}px` : "";
    tip.style.top = `${Math.round(padT)}px`;
  };
  const hide = () => {
    cross.style.display = "none";
    tip.style.display = "none";
    for (const m of marks) m.style.display = "none";
  };
  svg.addEventListener("mousemove", (e) => show(e.clientX));
  svg.addEventListener("mouseleave", hide);
  svg.addEventListener("touchstart", (e) => { if (e.touches[0]) show(e.touches[0].clientX); }, { passive: true });
  svg.addEventListener("touchmove", (e) => { if (e.touches[0]) show(e.touches[0].clientX); }, { passive: true });
  // the legend: first -> last in the range; hovering it lifts that line
  if (legend) {
    series.forEach((sr, si) => {
      const col = values[si].filter((v) => v != null);
      const item = span("evo-legend-item", "");
      const sw = span("sw", ""); sw.style.background = sr.color;
      item.appendChild(sw);
      item.appendChild(document.createTextNode(`${sr.label} `));
      item.appendChild(span("evo-legend-values", col.length ? `${fmtV(col[0])} \u2192 ${fmtV(col[col.length - 1])}` : "\u2013"));
      item.addEventListener("mouseenter", () => lines.forEach((l, i) => l.classList.toggle("dim", i !== si)));
      item.addEventListener("mouseleave", () => lines.forEach((l) => l.classList.remove("dim")));
      legend.appendChild(item);
    });
  }
}

/* ---------------- Console gaps: the per-file lists ---------------- */

const EVIDENCE_AUDIT_CHIPS = [
  ["", "all files", "", ""],
  ["NO_BODY", "no body", "crimson", "NO_BODY"],
  ["MISSING_CASE", "switch gaps", "red", "MISSING_CASE"],
  ["EXTRA_CASE", "misfiled case ids", "red", "EXTRA_CASE"],
  ["MISSING_EVENT", "missing event posts", "red", "MISSING_EVENT"],
  ["MISSING_CALLEE", "missing callees", "red", "MISSING_CALLEE"],
  ["MISSING_ASSERT", "missing asserts", "amber", "MISSING_ASSERT"],
];

// A painted filter (the shared .b5-filter, in a .b5-filter-group--paint container): an
// optional state swatch, the label, then its count. The caller owns the state and
// re-renders the group on click.
function evidenceChip(label, count, active, color, onClick) {
  const chip = document.createElement("button");
  chip.type = "button";
  chip.className = "b5-filter";
  chip.setAttribute("aria-pressed", String(Boolean(active)));
  if (color) {
    const swatch = document.createElement("i");
    swatch.className = `sw sw-${color}`;
    swatch.setAttribute("aria-hidden", "true");
    chip.appendChild(swatch);
  }
  chip.appendChild(document.createTextNode(label));
  if (count != null) chip.appendChild(span("", fmtInt(count)));
  chip.addEventListener("click", onClick);
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
      "Files ranked by their gaps. Open one to see each function and what the console's version has that ours lacks.";
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
  } else {
    intro.textContent =
      "Bodies that are still stand-ins, by file. High: the code says so, or it traps. Medium: a trivial body under a softer marker. Low: a trivial, unmarked body whose console function does real work (possibly a legitimate empty default). Called today: reconstructed code calls it right now.";
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
    chips.appendChild(evidenceChip("called today only", st.live, ev.live, "blue", () => {
      ev.live = !ev.live;
      loadEvidenceList(true);
      renderEvidenceSummary();
    }));
  }
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
    else p.set("gaps", "true");   // a file with only context (log strings, data) has no gap
    p.set("sort", "weight");
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
  return pill(ASM_TIER_PILL[tier] || "todo", `${tier} ${ASM_TIER_LABELS[tier] || ""}`.trim());
}

function asmRow(fn) {
  const row = div("audit-row");
  const head = div("audit-row-head");
  head.appendChild(span("dep-name", fn.name));
  head.appendChild(asmTierPill(fn.tier));
  if (fn.score != null) head.appendChild(pill("todo", `score ${fn.score}`));
  if (fn.flags && fn.flags.total) {
    const kinds = Object.entries(fn.flags.kinds || {}).map(([k, n]) => `${n} ${k}`).join(", ");
    const flagged = pill("todo", `flagged ${fmtInt(fn.flags.total)}`);
    flagged.title = `[FLAG] markers in the source body: ${kinds}. PC additions that compile in and are meant to differ from the console; the score does not discount them.`;
    head.appendChild(flagged);
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
  const path = ev.tab === "audit" ? "/api/audit/files" : "/api/stubs/files";
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
  list.scrollTop = 0;   // the list is its own scroller: a new page starts at its top
  if (!ev.items.length) {
    const have = ev.summary && (ev.summary.funcaudit || {}).paired;
    list.appendChild(div("muted-text", have ? "No files match." : "No audit has been imported yet."));
  }
  for (const item of ev.items) {
    const card = div("evidence-file");
    const head = div("evidence-file-head");
    const left = div("evidence-file-left");
    left.appendChild(div("evidence-file-path", item.file || "(functions the ledger files nowhere)"));
    const nums = div("evidence-file-nums");
    const count = (n, one, many) => `${fmtInt(n)} ${Number(n) === 1 ? one : many}`;
    if (ev.tab === "audit") {
      if (item.functions) left.appendChild(div("evidence-file-meta", `${count(item.functions, "function", "functions")} to review`));
      nums.appendChild(pill("cat-high", count(item.weight, "gap", "gaps")));
      if (item.no_body) nums.appendChild(pill("tier-HIGH", `${fmtInt(item.no_body)} no body`));
      if (item.missing_case) nums.appendChild(pill("cat-mid", count(item.missing_case, "switch gap", "switch gaps")));
      if (item.missing_event) nums.appendChild(pill("cat-mid", count(item.missing_event, "event", "events")));
      if (item.missing_callee) nums.appendChild(pill("cat-low", count(item.missing_callee, "callee", "callees")));
      if (item.missing_assert) nums.appendChild(pill("cat-low", count(item.missing_assert, "assert", "asserts")));
    } else {
      const meta = [];
      if (item.console_lines) meta.push(`${count(item.console_lines, "console line", "console lines")} behind them`);
      left.appendChild(div("evidence-file-meta", meta.join(" · ")));
      nums.appendChild(pill("cat-high", count(item.stubs, "stub", "stubs")));
      if (item.high) nums.appendChild(pill("tier-HIGH", `${fmtInt(item.high)} high`));
      if (item.medium) nums.appendChild(pill("tier-MEDIUM", `${fmtInt(item.medium)} medium`));
      if (item.low) nums.appendChild(pill("tier-LOW", `${fmtInt(item.low)} low`));
      if (item.live) nums.appendChild(pill("live", `${fmtInt(item.live)} called today`));
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
  createPager(el("evidencePager")).set({ total: ev.total, pageSize: ev.limit, page: Math.floor(ev.offset / ev.limit) + 1 });
}

function openEvidenceFile(file) {
  const ev = state.evidence;
  if (ev.tab === "audit") openAuditFile(file);
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
    } else {
      title.textContent = "Biggest stubs called today";
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
  tabs.addEventListener("b5:filterchange", (event) => {
    ev.tab = event.detail.value;
    renderEvidenceSummary();
    loadEvidenceList(true);
  });
  el("evidenceSearch").addEventListener("input", (e) => {
    ev.q = e.target.value.trim();
    clearTimeout(ev.searchTimer);
    ev.searchTimer = setTimeout(() => loadEvidenceList(true), 250);
  });
  el("evidencePager").addEventListener("b5:pagechange", (event) => {
    ev.offset = (event.detail.page - 1) * ev.limit;
    loadEvidenceList(false);
    el("evidenceList")?.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  loadEvidenceList(true);
}

/* ---------------- Byte matching: the native x360 tree against ARTIST ---------------- */
// Everything below runs from loadBytematch(), which refresh() calls after its first await:
// the whole script has evaluated by then, so these consts are initialised.
const BM_STATES = [
  { key: "exact", label: "exact", color: "green", pill: "done",
    title: "The same bytes as ARTIST outside the fields the linker fills in, with the same callees and constants. Click: newest first." },
  { key: "nonmatching", label: "compiled, not matching", color: "amber", pill: "compiled",
    title: "The native tree compiles a body for it, but the instructions differ. Click: closest to matching first." },
  { key: "missing", label: "no compiled body", color: "crimson", pill: "blocked",
    title: "No object of the native tree defines this function yet. Click: smallest first." },
  { key: "sdk", label: "XDK library", color: "purple", pill: "external",
    title: "Byte-identical to a function of the XDK's own libraries: linked, never written." },
  { key: "unnamed", label: "not yet named", color: "dim", pill: "todo",
    title: "Functions of the console image without a name; nothing can be paired with them yet." },
];
const BM_SORT = { exact: ["first_exact", "desc"], nonmatching: ["score", "desc"], missing: ["size", "asc"] };

function bmState() {
  if (!state.bytematch) {
    state.bytematch = {
      summary: null, loadedAt: 0, wired: false, status: "", area: "", tu: "", q: "",
      offset: 0, limit: 25, total: 0, items: [], listSeq: 0,
      map: null, mapMode: "x360tus", mapSnapshot: null, mapLoadedAt: 0, mapInFlight: false,
    };
  }
  return state.bytematch;
}

async function loadBytematch(force = false) {
  if (!el("bytematchCard")) return;
  const bm = bmState();
  // the summary moves when someone pushes records or the native branch: once a minute is plenty
  if (!force && bm.loadedAt && Date.now() - bm.loadedAt < 60 * 1000) return;
  try {
    const summary = await fetchJson("/api/bytematch/summary", 20000);
    const changed = !bm.summary || JSON.stringify(bm.summary.meta) !== JSON.stringify(summary.meta);
    bm.summary = summary;
    bm.loadedAt = Date.now();
    wireBytematch();
    renderBytematch(summary);
    if (changed || force) {
      loadBytematchList();
      loadX360Map(true);
    }
  } catch (error) {
    text("bytematchCount", `Byte-match summary unavailable: ${error.message}`);
  }
}

function wireBytematch() {
  const bm = bmState();
  if (bm.wired) return;
  bm.wired = true;
  const search = el("bytematchSearch");
  search.addEventListener("input", () => {
    clearTimeout(bm.searchTimer);
    bm.searchTimer = setTimeout(() => {
      bm.q = search.value.trim();
      bm.offset = 0;
      loadBytematchList();
      loadX360Map();
    }, 250);
  });
  el("bmPagePrev").addEventListener("click", () => {
    bm.offset = Math.max(0, bm.offset - bm.limit);
    loadBytematchList();
  });
  el("bmPageNext").addEventListener("click", () => {
    if (bm.offset + bm.limit < bm.total) bm.offset += bm.limit;
    loadBytematchList();
  });
  el("x360MapTabs").addEventListener("b5:filterchange", (event) => {
    bm.mapMode = event.detail.value;
    loadX360Map();
  });
  initX360Map();
  // the history chart is drawn at its box's width
  window.addEventListener("resize", () => {
    clearTimeout(bm.resizeTimer);
    bm.resizeTimer = setTimeout(() => renderBytematchHistory((bm.summary || {}).history || []), 150);
  });
}

function bmFilter(opts = {}) {
  const bm = bmState();
  bm.status = opts.status ?? "";
  bm.area = opts.area ?? "";
  bm.tu = opts.tu ?? "";
  bm.q = opts.q ?? "";
  bm.offset = 0;
  const search = el("bytematchSearch");
  if (search) search.value = bm.q;
  loadBytematchList();
  loadX360Map();
  if (opts.scroll !== false) el("bytematchExplorer")?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderBytematch(s) {
  if (!s.available) {
    setDonut("bytematch", [{ label: "nothing measured yet", value: 1, color: "dim" }], 0, "not measured yet");
    text("bytematchPercent", "0%");
    // the license meter says so too, not 0%
    const meter = el("meterMatch");
    if (meter) {
      meter.setAttribute("aria-valuetext", "not measured yet");
      meter.querySelector("[data-b5-progress-label]").textContent = "not measured yet";
    }
    clearNode(el("bytematchFacts"));
    if (el("bytematchStamp")) el("bytematchStamp").hidden = true;
    renderBytematchHistory([]);
    renderBytematchRecent([]);
    renderBytematchAreas([]);
    return;
  }
  const counts = s.status_counts || {};
  const whole = (s.totals || {}).all || {};
  // The ring counts every ARTIST function; its centre is the share that byte-matches
  // (exact from our source, or linked from the XDK), so the arcs and the number agree.
  setDonut("bytematch", BM_STATES.map((st) => ({
    label: st.label, value: Number((counts[st.key] || {}).functions || 0), color: st.color, title: st.title,
    onClick: () => bmFilter({ status: st.key }),
  })), Number((s.percent || {}).matched_functions || 0),
  `${fmtInt(whole.matched)} / ${fmtInt(whole.functions)} functions byte-match ARTIST`);
  renderBytematchFacts(s);
  renderBytematchHistory(s.history || []);
  renderBytematchRecent(s.recent || []);
  renderBytematchAreas(s.areas || []);
  renderBytematchChips();
}

function renderBytematchFacts(s) {
  const host = el("bytematchFacts");
  if (!host) return;
  clearNode(host);
  const meta = s.meta || {};
  const objects = (s.totals || {}).objects || {};
  // the summary's commit and age, at the right of the section's header
  const stamp = el("bytematchStamp");
  if (stamp) {
    stamp.hidden = !meta.b5_commit;
    if (meta.b5_commit) {
      stamp.textContent = `x360 ${String(meta.b5_commit).slice(0, 10)} · ${relTime(meta.generated_at)}`;
      stamp.title = `summary built ${fmtTime(meta.generated_at)}${meta.toolchain ? ` · ${meta.toolchain}` : ""}`;
    }
  }
  // A fact: its figure and label, then (for a share) a car-stat bar, and the counts
  // behind it in a line; the title keeps the longer explanation.
  const fact = (value, label, detail, title, kind, share) => {
    const tile = summaryTile(value, label, kind);
    tile.classList.add("bm-fact");
    if (share == null) tile.classList.add("bm-fact--count");
    else {
      const bar = span("b5-meter__bar");
      bar.setAttribute("aria-hidden", "true");
      bar.style.setProperty("--b5-meter", String(Math.max(0, Math.min(100, share)) / 100));
      tile.appendChild(bar);
    }
    const note = document.createElement("small");
    note.className = "bm-fact__detail";
    note.textContent = detail;
    tile.appendChild(note);
    if (title) tile.title = title;
    host.appendChild(tile);
  };
  const pct = (k) => Number((s.percent || {})[k] || 0);
  const pctOf = (k) => `${pct(k).toFixed(2)}%`;
  const all = (s.totals || {}).all || {};
  const game = (s.totals || {}).game || {};
  const sdk = (s.totals || {}).sdk || {};
  fact(pctOf("image_bytes"), "of the image's code bytes match", `${fmtInt(all.matched_bytes)} of ${fmtInt(all.bytes)} bytes`,
    `By size instead of count: ${fmtInt(all.matched_bytes)} of ${fmtInt(all.bytes)} bytes of code, ` +
    "exact functions of ours plus XDK library code.", "done", pct("image_bytes"));
  fact(pctOf("exact_functions"), "of our functions exact", `${fmtInt(game.exact)} of ${fmtInt(game.functions)} functions · ${pctOf("exact_bytes")} by size`,
    `${fmtInt(game.exact)} of the ${fmtInt(game.functions)} functions we compile from source (the image minus ` +
    `${fmtInt(sdk.functions)} XDK library functions and the unnamed ones). By size: ${pctOf("exact_bytes")} of their code.`, "compiled", pct("exact_functions"));
  fact(pctOf("fuzzy_bytes"), "fuzzy match of our code", "instruction score, weighted by size",
    "How close our compiled code is overall: each function's fuzzy instruction score weighted by its size (exact = 100%, no body = 0%).", "progress", pct("fuzzy_bytes"));
  if (objects.sources) {
    fact(fmtInt(objects.sources - (objects.compiled || 0)), "objects fail to compile", `of ${fmtInt(objects.sources)} native sources measured`,
      `${fmtInt(objects.sources)} native sources measured; these failed the XDK compile.`, "blocked");
  }
  if (objects.stale) {
    fact(fmtInt(objects.stale), "measured before a source change", "their verdicts stand until the next measurement",
      "Records whose source file changed on the branch since it was measured. Their verdicts stand until the next measurement.", "todo");
  }
  if (meta.verified_commit) {
    const behind = meta.behind;
    fact(behind == null ? "?" : fmtInt(behind), "commits since the last full refresh",
      `every record proven at x360 ${String(meta.verified_commit).slice(0, 10)}${meta.verified_at ? ` · ${relTime(meta.verified_at)}` : ""}`,
      `Every record was last proven current at x360 ${String(meta.verified_commit).slice(0, 10)}${meta.verified_at ? ` (${fmtTime(meta.verified_at)})` : ""}.`,
      behind ? "todo" : "compiled");
  }
  // two or three counts share the row under the shares
  host.dataset.counts = String(host.querySelectorAll(".bm-fact--count").length);
}

function renderBytematchHistory(history) {
  const host = el("bytematchHistory");
  if (!host) return;
  clearNode(host);
  const points = (history || []).filter((p) => p.functions != null).slice(-365);
  if (points.length < 2) {
    const p = points[0];
    host.appendChild(div("muted-text vh-empty", p
      ? `One published summary so far (x360 ${String(p.commit || "").slice(0, 10)}: ${fmtInt(p.exact)} exact of ${fmtInt(p.functions)}). The lines start with the next one.`
      : "No summary published yet."));
    return;
  }
  const ns = "http://www.w3.org/2000/svg";
  const W = Math.max(220, Math.round(host.clientWidth || 320)), H = 96, padL = 40, padR = 8, padT = 6, padB = 14;
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("width", String(W));
  svg.setAttribute("height", String(H));
  const value = (p, key) => key === "compiled" ? Number(p.exact || 0) + Number(p.nonmatching || 0) : Number(p[key] || 0);
  const series = [
    { key: "compiled", cls: "vh-paired", area: true },
    { key: "exact", cls: "vh-clean", area: true },
    { key: "missing", cls: "vh-nobody", area: false },
  ];
  const top = Math.max(1, ...points.map((p) => Math.max(value(p, "compiled"), value(p, "missing"))));
  const stamps = points.map((p) => Date.parse(p.imported_at || ""));
  const timed = stamps.every((t) => Number.isFinite(t)) && stamps[stamps.length - 1] > stamps[0];
  const t0 = timed ? stamps[0] : 0, tSpan = timed ? stamps[stamps.length - 1] - stamps[0] : 1;
  const x = (i) => padL + ((timed ? (stamps[i] - t0) / tSpan : i / (points.length - 1)) * (W - padL - padR));
  const y = (v) => padT + (1 - v / top) * (H - padT - padB);
  for (const sr of series) {
    const d = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(value(p, sr.key)).toFixed(1)}`).join(" ");
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
    c.setAttribute("cx", x(i).toFixed(1)); c.setAttribute("cy", y(value(p, "exact")).toFixed(1)); c.setAttribute("r", "2");
    const title = document.createElementNS(ns, "title");
    title.textContent = `x360 ${String(p.commit || "").slice(0, 10)} · ${String(p.imported_at || "").slice(0, 16).replace("T", " ")}: ` +
      `${fmtInt(p.exact)} exact, ${fmtInt(p.nonmatching)} not matching, ${fmtInt(p.missing)} missing of ${fmtInt(p.functions)}`;
    c.appendChild(title);
    svg.appendChild(c);
  });
  host.appendChild(svg);
  const lg = div("vp-shape-legend");
  const a = points[0], b = points[points.length - 1];
  for (const [cls, label, key] of [["vh-sw-paired", "compiled bodies", "compiled"], ["vh-sw-clean", "exact", "exact"],
    ["vh-sw-nobody", "no compiled body", "missing"]]) {
    const item = span("", "");
    item.appendChild(span(`vh-sw ${cls}`, ""));
    item.appendChild(document.createTextNode(` ${label} ${fmtInt(value(a, key))} → ${fmtInt(value(b, key))}`));
    lg.appendChild(item);
  }
  host.appendChild(lg);
}

function renderBytematchRecent(recent) {
  const host = el("bytematchRecent");
  if (!host) return;
  clearNode(host);
  if (!recent.length) {
    host.appendChild(div("muted-text", "No function has matched yet."));
    return;
  }
  // seven beside the ring and its facts; the narrower layouts show five (app.css)
  for (const r of recent.slice(0, 7)) {
    const li = document.createElement("li");
    li.appendChild(span("top-n", fmtCompact(r.size)));
    const body = div("");
    body.appendChild(div("evidence-file-path", r.name || r.artist));
    body.appendChild(div("evidence-file-meta", `ARTIST ${r.artist} · ${r.size} bytes · ${relTime(r.at)}`));
    li.appendChild(body);
    li.title = `first exact ${fmtTime(r.at)} at x360 ${String(r.commit || "").slice(0, 10)}${r.source ? ` · ${r.source}` : ""}`;
    li.addEventListener("click", () => bmFilter({ q: r.artist }));
    host.appendChild(li);
  }
}

function renderBytematchAreas(areas) {
  const host = el("bytematchAreas");
  if (!host) return;
  clearNode(host);
  if (!areas.length) {
    host.appendChild(div("muted-text", "No areas yet."));
    return;
  }
  for (const a of areas.slice(0, 30)) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "bm-area-row";
    const exact = a.bytes ? (100 * a.exact_bytes) / a.bytes : 0;
    const fuzzy = a.bytes ? (100 * a.fuzzy_bytes) / a.bytes : 0;
    row.appendChild(span("bm-area-name", a.area));
    const bar = div("bm-bar");
    const fz = div("bm-bar-fuzzy");
    fz.style.width = `${Math.min(100, fuzzy).toFixed(2)}%`;
    const ex = div("bm-bar-exact");
    ex.style.width = `${Math.min(100, exact).toFixed(2)}%`;
    bar.append(fz, ex);
    row.appendChild(bar);
    row.appendChild(span("bm-area-nums", `${fmtInt(a.exact)} / ${fmtInt(a.functions)} · ${exact.toFixed(1)}%`));
    row.title = `${a.area}: ${fmtInt(a.exact)} exact, ${fmtInt(a.nonmatching)} not matching, ${fmtInt(a.missing)} missing; ` +
      `${fmtInt(a.exact_bytes)} of ${fmtInt(a.bytes)} bytes exact, ${fuzzy.toFixed(1)}% fuzzy`;
    row.addEventListener("click", () => bmFilter({ area: a.area }));
    host.appendChild(row);
  }
  if (areas.length > 30) host.appendChild(div("muted-text", `${fmtInt(areas.length - 30)} smaller areas in the list below.`));
}

function renderBytematchChips() {
  const bm = bmState();
  const host = el("bytematchChips");
  if (!host) return;
  clearNode(host);
  const counts = (bm.summary || {}).status_counts || {};
  host.appendChild(evidenceChip("all", null, !bm.status, null, () => bmFilter({ area: bm.area, tu: bm.tu, q: bm.q, scroll: false })));
  for (const st of BM_STATES) {
    host.appendChild(evidenceChip(st.label, (counts[st.key] || {}).functions || 0, bm.status === st.key, st.color,
      () => bmFilter({ status: st.key, area: bm.area, tu: bm.tu, q: bm.q, scroll: false })));
  }
  for (const [key, label] of [["area", "area"], ["tu", "unit"]]) {
    if (!bm[key]) continue;
    host.appendChild(evidenceChip(`${label}: ${bm[key]} ✕`, null, true, null,
      () => bmFilter({ status: bm.status, q: bm.q, area: key === "area" ? "" : bm.area, tu: key === "tu" ? "" : bm.tu, scroll: false })));
  }
}

async function loadBytematchList() {
  const bm = bmState();
  const [sort, order] = BM_SORT[bm.status] || ["size", "desc"];
  const params = new URLSearchParams({ sort, order, limit: String(bm.limit), offset: String(bm.offset) });
  for (const key of ["status", "area", "tu", "q"]) if (bm[key]) params.set(key, bm[key]);
  const seq = ++bm.listSeq;
  renderBytematchChips();
  try {
    const page = await fetchJson(`/api/bytematch/functions?${params}`, 20000);
    if (seq !== bm.listSeq) return;   // a newer filter already asked
    bm.items = page.items || [];
    bm.total = page.total || 0;
    renderBytematchList();
  } catch (error) {
    clearNode(el("bytematchList"));
    el("bytematchList").appendChild(div("muted-text", `Function list unavailable: ${error.message}`));
  }
}

function renderBytematchList() {
  const bm = bmState();
  const list = el("bytematchList");
  clearNode(list);
  list.scrollTop = 0;
  if (!bm.items.length) {
    list.appendChild(div("muted-text", (bm.summary || {}).available ? "No functions match." : "Nothing measured yet."));
  }
  const pills = Object.fromEntries(BM_STATES.map((s) => [s.key, s]));
  for (const item of bm.items) {
    const card = div("evidence-file bm-row");
    const head = div("evidence-file-head");
    const left = div("evidence-file-left");
    left.appendChild(div("evidence-file-path", item.name));
    const meta = div("evidence-file-meta", "");
    meta.appendChild(document.createTextNode(`ARTIST ${item.artist} · ${fmtInt(item.size)} bytes`));
    if (item.source) {
      meta.appendChild(document.createTextNode(" · "));
      const a = document.createElement("a");
      a.href = `https://github.com/${state.repo.owner}/${state.repo.name}/blob/x360/${item.source}`;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = item.source;
      meta.appendChild(a);
    } else if (item.tu) {
      meta.appendChild(document.createTextNode(` · ${item.tu}`));
    }
    if (item.first_exact) meta.appendChild(document.createTextNode(` · exact since ${relTime(item.first_exact)}`));
    left.appendChild(meta);
    head.appendChild(left);
    const nums = div("evidence-file-nums");
    const st = pills[item.status] || { pill: "todo", label: item.status };
    nums.appendChild(pill(st.pill, st.label));
    if (item.status === "nonmatching") nums.appendChild(pill("todo", `${Number(item.score || 0).toFixed(1)}%`));
    if (item.stale) {
      const s = pill("live", "source changed");
      s.title = "Its source file changed on the branch after this verdict was measured.";
      nums.appendChild(s);
    }
    head.appendChild(nums);
    card.appendChild(head);
    list.appendChild(card);
  }
  const from = bm.total ? bm.offset + 1 : 0;
  const to = Math.min(bm.total, bm.offset + bm.limit);
  text("bytematchRange", `${fmtInt(from)}–${fmtInt(to)} of ${fmtInt(bm.total)} functions`);
  const pages = Math.max(1, Math.ceil(bm.total / bm.limit));
  text("bmPageStatus", `Page ${fmtInt(Math.floor(bm.offset / bm.limit) + 1)} of ${fmtInt(pages)}`);
  el("bmPagePrev").disabled = bm.offset <= 0;
  el("bmPageNext").disabled = bm.offset + bm.limit >= bm.total;
}

/* The X360 treemap: the PC progress map's renderer on the byte-match states. */
function initX360Map() {
  const bm = bmState();
  if (bm.map || !window.BPProgressMap) return;
  try {
    bm.map = new BPProgressMap.MapController({
      prefix: "x360",
      title: "X360 map",
      onSummary(summary, mode) {
        text("x360MapCount", `${fmtInt(summary.items)} ${mode === "x360funcs" ? "functions" : "translation units"}`);
      },
      onOpen(data) {
        if (data.kind === "function") bmFilter({ q: data.artist || data.name });
        else if (data.tuId) bmFilter({ tu: data.tuId });
      },
    });
  } catch (error) {
    text("x360MapStatusText", `X360 map unavailable: ${error.message}. The function list below has the same data.`);
  }
}

async function loadX360Map(force = false) {
  const bm = bmState();
  if (!bm.map) return;
  const needsFunctions = bm.mapMode === "x360funcs";
  const render = () => {
    bm.map.update(bm.mapSnapshot, bm.mapMode, { q: bm.q, status: bm.mapMode === "x360funcs" ? bm.status : "" });
    el("x360MapStatus").hidden = true;
    el("x360ProgressMapViewport").classList.remove("is-loading");
  };
  const fresh = bm.mapSnapshot && Date.now() - bm.mapLoadedAt < 60 * 1000;
  if (!force && fresh && (!needsFunctions || bm.mapSnapshot.include_functions)) {
    render();
    return;
  }
  if (bm.mapInFlight) { bm.mapPending = true; return; }
  bm.mapInFlight = true;
  if (!bm.mapSnapshot || (needsFunctions && !bm.mapSnapshot.include_functions)) {
    el("x360MapStatus").hidden = false;
    text("x360MapStatusText", needsFunctions ? "Loading every function…" : "Loading translation units…");
    el("x360ProgressMapViewport").classList.add("is-loading");
  }
  try {
    bm.mapSnapshot = await fetchJson(`/api/bytematch/map?include_functions=${needsFunctions}`, 30000);
    bm.mapLoadedAt = Date.now();
    if (!bm.mapSnapshot.units.length) {
      el("x360MapStatus").hidden = false;
      text("x360MapStatusText", "No byte-match summary has been imported yet.");
    } else {
      render();
    }
  } catch (error) {
    el("x360MapStatus").hidden = false;
    text("x360MapStatusText", `X360 map unavailable: ${error.message}. The function list below has the same data.`);
  } finally {
    bm.mapInFlight = false;
    if (bm.mapPending) { bm.mapPending = false; loadX360Map(); }
  }
}
