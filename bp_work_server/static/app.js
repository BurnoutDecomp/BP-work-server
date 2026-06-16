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
    searchTimer: null,
    requestId: 0,
  },
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
  if (!profile) return span("actor-name", name);
  const link = document.createElement("a");
  link.className = "actor-link";
  link.href = `https://github.com/${profile}`;
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = name;
  link.title = `Open ${name} on GitHub`;
  return link;
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
  return Object.entries(detail)
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
  renderNextQueue((data.next && data.next.items) || []);
  renderEvents(data.recent_events || []);
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
        `${fmtInt(agent.total)} active | ${fmtInt(agent.completed)} completed | lease ${shortTime(
          agent.lease_expires_at,
        )} | last ${relTime(agent.last_activity || agent.last_update || agent.last_seen) || "never"}`,
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

function renderNextQueue(items) {
  text("nextCount", `${items.length} ready`);
  const root = el("nextQueue");
  clearNode(root);
  root.className = items.length ? "queue" : "queue empty";
  if (!items.length) {
    root.textContent = "No available TUs.";
    return;
  }
  for (const item of items) {
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

function renderEvents(events) {
  text("eventCount", `${events.length} events`);
  const root = el("events");
  clearNode(root);
  root.className = events.length ? "event-table" : "event-table empty";
  if (!events.length) {
    root.textContent = "No events yet.";
    return;
  }
  const head = div("event-row event-head");
  ["Time", "Event", "Actor", "Target", "Details"].forEach((label) =>
    head.appendChild(div("event-cell", label)),
  );
  root.appendChild(head);
  for (const event of events) {
    state.lastEventId = Math.max(state.lastEventId, event.id || 0);
    const row = div("event-row");
    row.appendChild(div("event-cell event-time", shortTime(event.ts)));
    row.appendChild(div("event-cell event-action", event.action || "event"));
    const actor = div("event-cell");
    actor.appendChild(event.agent ? actorNode(event.agent) : span("muted-text", "server"));
    row.appendChild(actor);
    const target = div("event-cell event-target");
    if (event.tu_id) target.appendChild(tuButton(event.tu_id, "event-tu"));
    else target.textContent = "server";
    row.appendChild(target);
    row.appendChild(div("event-cell event-detail", detailText(event.detail) || "-"));
    root.appendChild(row);
  }
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
    row.appendChild(div("tu-name", goal.name));
    row.appendChild(div("goal-meta", `${goal.category || "uncategorized"} · ${fmtInt(done)} / ${fmtInt(total)} done`));
    const bar = div("bar");
    const fill = document.createElement("span");
    fill.style.width = `${Math.max(0, Math.min(100, percent))}%`;
    bar.appendChild(fill);
    row.appendChild(bar);
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
    if (ex.tab === "funcs") renderFuncRows(data.items || []);
    else renderTuRows(data.items || []);
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
    } else if (item.completed_by) {
      owner.appendChild(actorNode(item.completed_by));
      owner.appendChild(div("tu-meta", "completed"));
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
  setHead(["Function", "Status", "Translation Unit"]);
  const body = el("explorerBody");
  clearNode(body);
  if (!items.length) return emptyRow(body, 3, "No functions match.");
  for (const item of items) {
    const row = document.createElement("tr");
    row.className = "clickable";
    const name = document.createElement("td");
    name.appendChild(div("fn-name", item.name));
    const status = document.createElement("td");
    status.appendChild(statusPill(item.status));
    const tu = document.createElement("td");
    tu.appendChild(tuButton(item.tu_id, "tu-meta"));
    row.append(name, status, tu);
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

async function openDetail(tuId) {
  const overlay = el("detailOverlay");
  overlay.classList.remove("hidden");
  text("detailTitle", tuId);
  el("detailBody").innerHTML = '<p class="muted-text">Loading…</p>';
  try {
    renderDetail(await fetchJson(`/api/tu?id=${encodeURIComponent(tuId)}`, 15000));
  } catch (error) {
    el("detailBody").innerHTML = "";
    el("detailBody").appendChild(div("muted-text", `Failed to load: ${error.message}`));
  }
}

function closeDetail() {
  el("detailOverlay").classList.add("hidden");
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
  if (d.completed_by) facts.appendChild(kv("Completed by", actorNode(d.completed_by)));
  else if (d.last_actor) facts.appendChild(kv("Last actor", actorNode(d.last_actor)));
  facts.appendChild(kv("Updated", d.updated_at ? `${fmtTime(d.updated_at)} (${relTime(d.updated_at)})` : null));
  if (d.commit) facts.appendChild(kv("Commit", d.commit));
  const repoPath = destToRepoPath(d.dest_path);
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
      const row = div("dep-row");
      row.appendChild(span("dep-name", fn.name));
      row.appendChild(statusPill(fn.status));
      funcs.appendChild(row);
    });
  body.appendChild(funcs);
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
// GitHub data changes slowly and is cached server-side; poll gently.
state.githubTimer = window.setInterval(refreshGithub, 90000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});
