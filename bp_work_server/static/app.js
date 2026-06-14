const state = {
  lastEventId: 0,
  refreshTimer: null,
  githubTimer: null,
  eventSource: null,
  treeCollapsed: {}, // path -> bool, remembers folder state across refreshes
};

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

/* ---------------- Work dashboard ---------------- */

async function refresh() {
  try {
    const response = await fetch("/dashboard/state", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    render(data);
    setConnection("online", "Live");
  } catch (error) {
    setConnection("offline", "Disconnected");
    text("subtitle", `Dashboard update failed: ${error.message}`);
  }
}

function render(data) {
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
  text("agentCount", `${agents.length} active`);
  const root = el("agents");
  clearNode(root);
  root.className = agents.length ? "agent-list" : "agent-list empty";
  if (!agents.length) {
    root.textContent = "No active claims.";
    return;
  }
  for (const agent of agents) {
    const row = div("agent-row");
    row.appendChild(div("agent-name", agent.name || "unknown"));
    row.appendChild(
      div(
        "agent-meta",
        `${fmtInt(agent.in_progress)} in progress · ${fmtInt(agent.compiled)} compiled · lease ${shortTime(
          agent.lease_expires_at,
        )}`,
      ),
    );
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
    const name = document.createElement("td");
    name.appendChild(div("tu-name", item.id));
    name.appendChild(div("tu-meta", `${item.source || "unknown"} · ${fmtInt(item.n_funcs)} funcs`));
    const status = document.createElement("td");
    status.appendChild(span(`pill ${item.status}`, item.status.replace("_", " ")));
    const owner = document.createElement("td");
    owner.textContent = item.owner || "none";
    const lease = document.createElement("td");
    lease.textContent = shortTime(item.lease_expires_at);
    row.append(name, status, owner, lease);
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
    row.appendChild(div("tu-name", item.id));
    row.appendChild(
      div(
        "tu-meta",
        `${item.source || "unknown"} · ${fmtInt(item.n_funcs)} funcs · unresolved deps ${fmtInt(
          item.unresolved_deps,
        )}`,
      ),
    );
    root.appendChild(row);
  }
}

function renderEvents(events) {
  text("eventCount", `${events.length} events`);
  const root = el("events");
  clearNode(root);
  root.className = events.length ? "event-list" : "event-list empty";
  if (!events.length) {
    const row = document.createElement("li");
    row.textContent = "No events yet.";
    root.appendChild(row);
    return;
  }
  for (const event of events) {
    state.lastEventId = Math.max(state.lastEventId, event.id || 0);
    const row = document.createElement("li");
    row.appendChild(div("event-title", `${event.action}${event.agent ? ` by ${event.agent}` : ""}`));
    row.appendChild(div("event-meta", `${shortTime(event.ts)} · ${event.tu_id || "server"}`));
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
    row.appendChild(div("tu-name", item.id));
    row.appendChild(div("tu-meta", item.notes || "No reason recorded."));
    root.appendChild(row);
  }
}

/* ---------------- GitHub panel ---------------- */

async function refreshGithub() {
  try {
    const response = await fetch("/github/overview", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    renderGithub(await response.json());
  } catch (error) {
    text("repoDesc", `GitHub data unavailable: ${error.message}`);
  }
}

function renderGithub(data) {
  const repo = data.repo || {};
  const info = data.info || {};
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
    row.appendChild(span("name", child.name));
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

/* ---------------- Live stream ---------------- */

function connectStream() {
  if (!window.EventSource) {
    state.refreshTimer = window.setInterval(refresh, 5000);
    return;
  }
  const source = new EventSource(`/events/stream?after=${state.lastEventId}`);
  state.eventSource = source;
  source.addEventListener("connected", () => setConnection("online", "Live"));
  source.addEventListener("work-event", () => refresh());
  source.addEventListener("tick", () => refresh());
  source.onerror = () => {
    setConnection("offline", "Reconnecting");
  };
}

refresh();
connectStream();
refreshGithub();
// GitHub data changes slowly and is cached server-side; poll gently.
state.githubTimer = window.setInterval(refreshGithub, 90000);
