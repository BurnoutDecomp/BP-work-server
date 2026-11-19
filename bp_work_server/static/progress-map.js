/* Progress geometry and Canvas UI. d3-hierarchy 3.1.2 is bundled locally. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory(require("./d3-hierarchy.min.js"));
  } else {
    root.BPProgressMap = factory(root.d3);
  }
})(globalThis, function (d3) {
  "use strict";

  const colors = {
    done: "#6fcf57", compiled: "#e3a52c", in_progress: "#57a8e0",
    blocked: "#ec1c24", todo: "#4a423b", recorded: "#e3a52c",
    external: "#b294df",
    unrecorded: "#4a423b", unidentified: "#2e2926", linked: "#57a8e0",
    unlinked: "#4a423b",
    // X360 byte matching (the native tree against ARTIST)
    bm_exact: "#6fcf57", bm_close: "#e3a52c", bm_nonmatching: "#d7742a", bm_missing: "#4a423b",
    bm_sdk: "#b294df", bm_unnamed: "#2e2926",
    bm_matched: "#6fcf57", bm_partial: "#e3a52c", bm_compiled: "#d7742a",
  };
  const labels = {
    done: "Done", compiled: "Compiled", in_progress: "In progress",
    blocked: "Blocked", todo: "Todo", recorded: "Recorded",
    external: "External",
    unrecorded: "Named, unrecorded", unidentified: "Unidentified",
    linked: "Linked source", unlinked: "Not included",
    bm_exact: "Exact", bm_close: "Close (90%+)", bm_nonmatching: "Compiled, not matching",
    bm_missing: "No compiled body", bm_sdk: "XDK library", bm_unnamed: "Not yet named",
    bm_matched: "Every function matches", bm_partial: "Some functions match", bm_compiled: "Compiled, none match",
  };
  const X360_MODES = new Set(["x360tus", "x360funcs"]);
  const FUNCTION_MODES = new Set(["funcs", "x360funcs"]);
  const DARK_KEYS = new Set(["todo", "unrecorded", "unidentified", "unlinked", "bm_missing", "bm_unnamed"]);
  // a function at or above this fuzzy score is drawn as "close"
  const CLOSE_SCORE = 90;
  const number = (n) => Number(n || 0).toLocaleString();
  const statusLabel = (s) => s === "compiles" ? "compiled" : String(s).replaceAll("_", " ");

  function colorKey(data, mode) {
    if (mode === "x360funcs") {
      if (data.unidentified || data.status === "unnamed") return "bm_unnamed";
      if (data.status === "nonmatching") return Number(data.score || 0) >= CLOSE_SCORE ? "bm_close" : "bm_nonmatching";
      return `bm_${["exact", "missing", "sdk"].includes(data.status) ? data.status : "missing"}`;
    }
    if (mode === "x360tus") {
      const state = data.unit?.status ?? data.status;
      return `bm_${["matched", "partial", "compiled", "missing", "sdk", "unnamed"].includes(state) ? state : "missing"}`;
    }
    if (mode === "funcs") {
      // A function's category is always its own status, never its unit's status.
      if (data.unidentified) return "unidentified";
      if (data.status === "external") return "external";
      return data.status === "todo" ? "unrecorded" : "recorded";
    }
    if (mode === "linked") return data.unit?.external_build_provider ? "external" : data.linked ? "linked" : "unlinked";
    return data.status;
  }

  function buildTree(snapshot, mode = "tus", filters = {}) {
    const root = { id: "root", name: "Whole program", kind: "directory", children: [] };
    const summary = { items: 0, functions: 0, recorded: 0, states: {}, zeroFunctions: 0 };
    const groups = new Map([["[]", root]]);
    const query = (filters.q || "").toLowerCase();
    const matches = (text) => !query || String(text || "").toLowerCase().includes(query);
    const countState = (data) => {
      const key = colorKey(data, mode);
      summary.states[key] = (summary.states[key] || 0) + 1;
    };
    for (const unit of snapshot.units || []) {
      const fnMode = FUNCTION_MODES.has(mode);
      if (!fnMode && unit.unidentified) continue;
      if (!fnMode) {
        if (!matches([unit.id, unit.source, unit.dest_path].join(" "))) continue;
        if (filters.status && unit.status !== filters.status) continue;
        if (filters.source && unit.source !== filters.source) continue;
        if (filters.goal && !(unit.goals || []).includes(filters.goal)) continue;
      }
      const functions = fnMode ? (unit.functions || []).filter((fn) =>
        matches(fn.name) && (!filters.status || fn.status === filters.status)) : null;
      if (functions && !functions.length) continue;
      // Original DWARF identifiers sometimes contain Unity/../ paths. Keep
      // their exact IDs for lookups, but normalize the visual directory tree.
      const parts = [];
      for (const part of unit.id.replaceAll("\\", "/").split("/")) {
        if (!part || part === ".") continue;
        if (part === ".." && parts.length && parts.at(-1) !== "..") parts.pop();
        else parts.push(part);
      }
      const isClass = unit.id.startsWith("class:");
      const path = unit.unidentified ? ["Unidentified functions"] :
        isClass ? ["Classes"] : parts.slice(0, -1);
      let parent = root;
      for (let i = 0; i < path.length; i++) {
        const key = JSON.stringify(path.slice(0, i + 1));
        if (!groups.has(key)) {
          const group = { id: `dir:${key}`, name: path[i], kind: "directory", children: [] };
          groups.set(key, group);
          parent.children.push(group);
        }
        parent = groups.get(key);
      }
      const unitNode = {
        id: `tu:${unit.id}`, name: isClass ? unit.id.slice(6) : parts.at(-1) || unit.id,
        kind: "unit", tuId: unit.id, status: unit.status, linked: unit.linked,
        unit, weight: Math.max(1, unit.function_count),
      };
      if (functions) {
        // The synthetic container is a bucket of functions, never a real TU.
        const container = unit.unidentified ? parent : unitNode;
        if (!container.children) container.children = [];
        for (const fn of functions) {
          const leaf = {
            id: `fn:${JSON.stringify([unit.id, fn.artist || fn.name])}`, name: fn.name, kind: "function",
            tuId: unit.id, unit, status: fn.status, unidentified: unit.unidentified, weight: 1,
            score: fn.score, size: fn.size, artist: fn.artist, source: fn.source,
          };
          container.children.push(leaf);
          summary.items++;
          summary.functions++;
          summary.recorded += mode === "x360funcs" ? (fn.status === "exact" ? 1 : 0)
            : !["todo", "external"].includes(fn.status) ? 1 : 0;
          countState(leaf);
        }
        if (!unit.unidentified) parent.children.push(unitNode);
      } else {
        parent.children.push(unitNode);
        summary.items++;
        summary.functions += unit.function_count;
        summary.recorded += unit.recorded_funcs;
        summary.zeroFunctions += unit.function_count === 0 ? 1 : 0;
        countState(unitNode);
      }
    }
    return { root, summary };
  }

  function layoutTree(data, width, height) {
    const root = d3.hierarchy(data)
      .sum((node) => node.children ? 0 : node.weight || 0)
      .sort((a, b) => b.value - a.value || a.data.id.localeCompare(b.data.id));
    d3.treemap().size([Math.max(1, width), Math.max(1, height)])
      .tile(d3.treemapSquarify).paddingInner(1).paddingOuter(1)
      .paddingTop((node) => {
        node.header = node.depth && node.children &&
          node.x1 - node.x0 > 90 && node.y1 - node.y0 > 42 ? 19 : 0;
        return node.header;
      })(root);
    return root;
  }

  function describeX360(node, mode) {
    const data = node.data;
    if (data.kind === "function") {
      const state = data.status === "nonmatching" ? `not matching, ${Number(data.score || 0).toFixed(1)}% fuzzy`
        : data.status === "missing" ? "no compiled body" : data.status === "sdk" ? "provided by an XDK library"
        : data.status === "unnamed" ? "not yet named" : "exact";
      const where = data.source ? data.source : data.unidentified ? "Console image" : data.tuId;
      return { type: data.unidentified ? "Unnamed function" : "Function", name: data.name,
        meta: `${state} \u00b7 ARTIST ${data.artist || "?"} \u00b7 ${number(data.size)} bytes \u00b7 ${where}` };
    }
    if (data.kind === "unit") {
      const unit = data.unit;
      // matched = exact from our source + provided by an XDK library; then what is left
      const parts = [`${number(unit.recorded_funcs)} of ${number(unit.function_count)} functions match`];
      const detail = [];
      if (unit.sdk_funcs) detail.push(`${number(unit.sdk_funcs)} XDK library`);
      if (unit.exact_funcs ?? unit.recorded_funcs) detail.push(`${number(unit.exact_funcs ?? unit.recorded_funcs)} exact`);
      if (detail.length) parts[0] += ` (${detail.join(", ")})`;
      if (unit.nonmatching_funcs) parts.push(`${number(unit.nonmatching_funcs)} not matching`);
      if (unit.missing_funcs) parts.push(`${number(unit.missing_funcs)} with no compiled body`);
      const meta = parts.join(" \u00b7 ");
      if (mode === "x360funcs") return { type: "Functions in translation unit", name: data.tuId, meta: `${meta} \u00b7 select to zoom` };
      return { type: "Translation unit", name: data.tuId, meta: `${labels[colorKey(data, mode)]} \u00b7 ${meta}` };
    }
    return { type: "Group", name: data.name,
      meta: `${number(node.value)} ${mode === "x360funcs" ? "functions" : "function-weighted area"} \u00b7 select to zoom` };
  }

  function describe(node, mode) {
    if (X360_MODES.has(mode)) return describeX360(node, mode);
    const data = node.data;
    if (data.kind === "function") {
      return {
        type: data.unidentified ? "Unidentified function" : "Function",
        name: data.name,
        meta: `Function status: ${statusLabel(data.status)} · ${data.unidentified ? "Console image" : data.tuId}`,
      };
    }
    if (data.kind === "unit") {
      if (mode === "funcs") {
        const functions = node.leaves();
        const recorded = functions.filter((fn) => !["todo", "external"].includes(fn.data.status)).length;
        return { type: "Functions in translation unit", name: data.tuId,
          meta: `${number(functions.length)} functions · ${number(recorded)} recorded · select to zoom into individual functions` };
      }
      return { type: "Translation unit", name: data.tuId,
        meta: `TU status: ${statusLabel(data.status)} · ${number(data.unit.function_count)} tracked functions` +
          (mode === "linked" ? ` · ${data.unit.external_build_provider ? `external provider: ${data.unit.external_build_provider.name}` : data.linked ? "linked source" : "no confirmed build provider"}` : "") };
    }
    return { type: "Group", name: data.name,
      meta: `${number(node.value)} ${mode === "funcs" ? "functions" : "function-weighted area"} · select to zoom` };
  }

  class MapController {
    constructor(options) {
      this.options = options;
      // Element ids are the progress explorer's; a prefix ("x360") selects a second set
      // (x360ProgressMapCanvas, x360MapUp, ...), so one renderer draws both maps.
      const prefix = options.prefix || "";
      this.$ = (id) => document.getElementById(prefix ? prefix + id[0].toUpperCase() + id.slice(1) : id);
      this.canvas = this.$("progressMapCanvas");
      this.context = this.canvas.getContext("2d");
      const theme = getComputedStyle(document.documentElement);
      const token = (name, fallback) => theme.getPropertyValue(name).trim() || fallback;
      this.palette = { ...colors, done: token("--green", colors.done),
        compiled: token("--gold", colors.compiled), in_progress: token("--blue", colors.in_progress),
        blocked: token("--red-bright", colors.blocked), recorded: token("--gold", colors.recorded),
        external: token("--purple", colors.external),
        linked: token("--blue", colors.linked) };
      this.theme = { background: token("--bg", "#0a0809"), group: "#211c19",
        text: token("--text", "#ece6da"), headFont: token("--font-head", "Oswald, sans-serif"),
        monoFont: token("--font-mono", "'JetBrains Mono', monospace") };
      this.viewport = this.$("progressMapViewport");
      this.tooltip = this.$("progressMapTooltip");
      this.highlight = this.$("mapHighlight");
      this.mode = "tus";
      this.zoomId = "root";
      this.selectedId = null;
      this.signature = null;
      this.nodes = new Map();
      this.frame = null;
      this.canvas.addEventListener("pointermove", (event) => {
        if (event.pointerType === "touch") return;
        const node = this.hit(event);
        if (!node) return this.hideTooltip();
        this.select(node, false);
        const desc = describe(node, this.mode);
        this.tooltip.replaceChildren();
        for (const [className, text] of [["map-tooltip-type", desc.type],
          ["map-tooltip-name", desc.name], ["map-tooltip-meta", desc.meta]]) {
          const line = document.createElement("div");
          line.className = className;
          line.textContent = text;
          this.tooltip.appendChild(line);
        }
        this.tooltip.hidden = false;
        const bounds = this.canvas.getBoundingClientRect();
        this.tooltip.style.left = `${Math.max(4, Math.min(event.clientX - bounds.left + 14,
          bounds.width - this.tooltip.offsetWidth - 4))}px`;
        this.tooltip.style.top = `${Math.max(4, Math.min(event.clientY - bounds.top + 14,
          bounds.height - this.tooltip.offsetHeight - 4))}px`;
      });
      this.canvas.addEventListener("pointerleave", () => this.hideTooltip());
      this.canvas.addEventListener("pointerup", (event) => {
        const node = this.hit(event);
        if (!node) return;
        this.select(node, true);
        if (event.pointerType !== "touch") this.activate(node);
      });
      this.canvas.addEventListener("keydown", (event) => this.keydown(event));
      this.canvas.addEventListener("focus", () => {
        if (!this.selectedId && this.layout?.children?.length) this.select(this.layout.children[0], true);
      });
      this.$("mapUp").addEventListener("click", () => this.up());
      this.$("mapReset").addEventListener("click", () => this.zoom("root"));
      this.$("mapActivate").addEventListener("click", () => {
        const node = this.layout?.descendants().find((n) => n.data.id === this.selectedId);
        if (node) this.activate(node);
      });
      this.$("mapUnitDetails").addEventListener("click", () => {
        const node = this.nodes.get(this.selectedId);
        if (node?.data.tuId) this.options.onOpen({ kind: "unit", tuId: node.data.tuId });
      });
      this.observer = new ResizeObserver(() => this.scheduleDraw());
      this.observer.observe(this.viewport);
      if (document.fonts) document.fonts.ready.then(() => this.scheduleDraw());
    }

    update(snapshot, mode, filters) {
      const signature = JSON.stringify([mode, filters, snapshot.units]);
      if (signature === this.signature) {
        // List view may have replaced the shared results badge while this map
        // was hidden. Restore its summary even when geometry is unchanged.
        this.renderSummary();
        return;
      }
      this.signature = signature;
      if (mode !== this.mode) this.selectedId = null;
      this.mode = mode;
      const { root, summary } = buildTree(snapshot, mode, filters);
      this.summary = summary;
      this.root = d3.hierarchy(root).sum((node) => node.children ? 0 : node.weight || 0);
      const previousZoom = this.nodes.get(this.zoomId);
      this.nodes = new Map(this.root.descendants().map((node) => [node.data.id, node]));
      // A changed filter or import may remove the focused unit. Walk back to
      // the nearest surviving parent rather than leaving a blank zoomed view.
      let ancestor = previousZoom;
      while (!this.nodes.has(this.zoomId) && ancestor) {
        ancestor = ancestor.parent;
        this.zoomId = ancestor?.data.id || "root";
      }
      if (!this.nodes.has(this.zoomId)) this.zoomId = "root";
      // A unit is a group only in Functions mode. Switching back must show
      // its containing directory, not try to zoom into a TU leaf.
      const zoom = this.nodes.get(this.zoomId);
      if (!zoom.children && zoom.parent) this.zoomId = zoom.parent.data.id;
      if (!this.nodes.has(this.selectedId)) this.selectedId = null;
      this.renderSummary();
      this.draw();
      const selected = this.layout.descendants().find((node) => node.data.id === this.selectedId);
      if (selected) this.select(selected, true);
      else this.clearSelection();
    }

    renderSummary() {
      const unitMode = !FUNCTION_MODES.has(this.mode);
      this.options.onSummary(this.summary, this.mode);
      this.$("mapExplanation").textContent = this.mode === "x360tus"
        ? "One tile = one translation unit of the ledger. Area = its function count; color = how much of it matches ARTIST byte for byte (exact from our source, or linked from the XDK)."
        : this.mode === "x360funcs"
        ? `One tile = one ARTIST function. Equal weight; color = its byte-match state. Close means a fuzzy score of ${CLOSE_SCORE}% or more.`
        : unitMode
        ? "One tile = one translation unit. Area = tracked function count; headline percentages count units." +
          (this.mode === "linked" ? " Purple means a confirmed external provider is included in the PC build inputs." : "")
        : "One tile = one function. Equal weight; colors use each function’s own recorded status.";
      const zero = this.$("mapZeroNote");
      zero.hidden = !unitMode || !this.summary.zeroFunctions;
      zero.textContent = `${number(this.summary.zeroFunctions)} units have no tracked functions and receive minimum-sized tiles.`;
      const legend = this.$("mapLegend");
      legend.replaceChildren();
      const keys = this.mode === "x360funcs" ? ["bm_exact", "bm_close", "bm_nonmatching", "bm_missing", "bm_sdk", "bm_unnamed"] :
        this.mode === "x360tus" ? ["bm_matched", "bm_partial", "bm_compiled", "bm_missing", "bm_sdk"] :
        this.mode === "funcs" ? ["recorded", "external", "unrecorded", "unidentified"] :
        this.mode === "linked" ? ["linked", "external", "unlinked"] : ["done", "external", "compiled", "in_progress", "blocked", "todo"];
      for (const key of keys) {
        const item = document.createElement("span");
        item.className = "map-legend-item";
        const swatch = document.createElement("i");
        swatch.style.background = this.palette[key];
        const label = document.createElement("span");
        label.textContent = `${this.mode === "linked" && key === "external" ? "External provider" : labels[key]} ${number(this.summary.states[key])}`;
        item.append(swatch, label);
        legend.appendChild(item);
      }
    }

    scheduleDraw() {
      if (this.frame) return;
      this.frame = requestAnimationFrame(() => { this.frame = null; this.draw(); });
    }

    draw() {
      const width = this.viewport.clientWidth;
      const height = this.viewport.clientHeight;
      if (!this.root || !width || !height) return;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      this.canvas.width = Math.round(width * dpr);
      this.canvas.height = Math.round(height * dpr);
      const ctx = this.context;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.fillStyle = this.theme.background;
      ctx.fillRect(0, 0, width, height);
      const zoomNode = this.nodes.get(this.zoomId) || this.root;
      this.layout = layoutTree(zoomNode.data, width, height);
      const descendants = this.layout.descendants();
      for (const node of descendants) {
        if (!node.depth || node.x1 <= node.x0 || node.y1 <= node.y0) continue;
        ctx.fillStyle = node.children ? this.theme.group : this.palette[colorKey(node.data, this.mode)] || this.palette.todo;
        ctx.fillRect(node.x0, node.y0, node.x1 - node.x0, node.y1 - node.y0);
      }
      ctx.textBaseline = "middle";
      for (const node of descendants) {
        if (!node.depth) continue;
        const w = node.x1 - node.x0;
        const h = node.y1 - node.y0;
        if (w < 55 || h < 18 || (node.children && !node.header)) continue;
        ctx.save();
        ctx.beginPath();
        ctx.rect(node.x0 + 4, node.y0, Math.max(0, w - 8), node.children ? node.header : h);
        ctx.clip();
        ctx.font = node.children ? `600 12px ${this.theme.headFont}` : `11px ${this.theme.monoFont}`;
        ctx.fillStyle = node.children ? this.theme.text : "#10120f";
        if (!node.children && DARK_KEYS.has(colorKey(node.data, this.mode))) {
          ctx.fillStyle = this.theme.text;
        }
        ctx.fillText(node.data.name, node.x0 + 5, node.y0 + (node.children ? node.header : h) / 2);
        ctx.restore();
      }
      this.updateHighlight(descendants.find((node) => node.data.id === this.selectedId));
      const empty = this.$("mapEmpty");
      empty.hidden = this.summary.items > 0;
      this.renderBreadcrumbs(zoomNode);
    }

    renderBreadcrumbs(node) {
      const root = this.$("mapBreadcrumbs");
      root.replaceChildren();
      for (const ancestor of node.ancestors().reverse()) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "map-crumb";
        button.textContent = ancestor.data.name;
        button.disabled = ancestor === node;
        if (ancestor === node) button.setAttribute("aria-current", "location");
        button.addEventListener("click", () => this.zoom(ancestor.data.id));
        root.appendChild(button);
      }
      this.$("mapUp").disabled = !node.parent;
      this.$("mapReset").disabled = !node.parent;
      const count = node.children ? node.leaves().length : 0;
      this.$("mapScopeNote").textContent =
        `Viewing ${number(count)} ${FUNCTION_MODES.has(this.mode) ? "functions" : "translation units"}`;
    }

    hit(event) {
      if (!this.layout) return null;
      const box = this.canvas.getBoundingClientRect();
      const x = event.clientX - box.left;
      const y = event.clientY - box.top;
      let node = this.layout;
      while (node.children) {
        const child = node.children.find((n) => x >= n.x0 && x < n.x1 && y >= n.y0 && y < n.y1);
        if (!child || child.x1 - child.x0 < 6 || child.y1 - child.y0 < 6) break;
        node = child;
      }
      return node === this.layout ? null : node;
    }

    select(node, announce) {
      if (this.selectedId === node.data.id && !announce) return;
      this.selectedId = node.data.id;
      const desc = describe(node, this.mode);
      this.$("mapSelectionType").textContent = desc.type;
      this.$("mapSelectionName").textContent = desc.name;
      this.$("mapSelectionMeta").textContent = desc.meta;
      const activate = this.$("mapActivate");
      activate.hidden = false;
      activate.textContent = node.children ? "Zoom into group" : "Open details";
      this.$("mapUnitDetails").hidden = !node.children || !node.data.tuId;
      if (announce) this.$("mapAnnouncement").textContent = `${desc.type}: ${desc.name}. ${desc.meta}`;
      // Selection is a lightweight overlay; hovering must not rebuild or
      // redraw tens of thousands of function rectangles on every mouse move.
      this.updateHighlight(node);
    }

    updateHighlight(node) {
      this.highlight.hidden = !node;
      if (!node) return;
      Object.assign(this.highlight.style, {
        left: `${node.x0}px`, top: `${node.y0}px`,
        width: `${Math.max(0, node.x1 - node.x0)}px`, height: `${Math.max(0, node.y1 - node.y0)}px`,
      });
    }

    clearSelection() {
      const name = this.nodes.get(this.zoomId)?.data.name || "Whole program";
      this.$("mapSelectionType").textContent = this.options.title || "Progress map";
      this.$("mapSelectionName").textContent = name;
      this.$("mapSelectionMeta").textContent = "Select a tile to inspect it; select a group to zoom.";
      this.$("mapActivate").hidden = true;
      this.$("mapUnitDetails").hidden = true;
    }

    activate(node) {
      this.hideTooltip();
      if (node.children) this.zoom(node.data.id);
      else this.options.onOpen(node.data);
    }

    zoom(id) {
      if (!this.nodes.has(id)) return;
      this.zoomId = id;
      this.selectedId = null;
      this.hideTooltip();
      this.$("mapActivate").hidden = true;
      this.$("mapUnitDetails").hidden = true;
      const desc = describe(this.nodes.get(id), this.mode);
      this.$("mapSelectionType").textContent = "Group";
      this.$("mapSelectionName").textContent = desc.name;
      this.$("mapSelectionMeta").textContent = "Select a tile to inspect it; select a group to zoom further.";
      this.$("mapAnnouncement").textContent = `Zoomed to ${desc.name}`;
      this.draw();
      this.canvas.focus({ preventScroll: true });
    }

    up() {
      const node = this.nodes.get(this.zoomId);
      if (node?.parent) this.zoom(node.parent.data.id);
    }

    keydown(event) {
      if (["Escape", "Backspace"].includes(event.key)) {
        event.preventDefault();
        this.up();
        return;
      }
      const children = this.layout?.children || [];
      if (!children.length) return;
      let selected = this.layout.descendants().find((n) => n.data.id === this.selectedId);
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        this.activate(selected || children[0]);
        return;
      }
      if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      while (selected?.parent && selected.parent !== this.layout) selected = selected.parent;
      const current = children.indexOf(selected);
      const forward = event.key === "ArrowRight" || event.key === "ArrowDown";
      const index = event.key === "Home" ? 0 : event.key === "End" ? children.length - 1 :
        (current + (forward ? 1 : -1) + children.length) % children.length;
      this.select(children[index], true);
    }

    hideTooltip() { this.tooltip.hidden = true; }
  }

  return { buildTree, layoutTree, colorKey, describe, colors, MapController };
});
