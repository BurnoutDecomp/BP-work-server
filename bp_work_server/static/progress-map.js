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
  };
  const labels = {
    done: "Done", compiled: "Compiled", in_progress: "In progress",
    blocked: "Blocked", todo: "Todo", recorded: "Recorded",
    external: "External",
    unrecorded: "Named, unrecorded", unidentified: "Unidentified",
    linked: "Linked source", unlinked: "Not included",
  };
  const number = (n) => Number(n || 0).toLocaleString();
  const statusLabel = (s) => s === "compiles" ? "compiled" : String(s).replaceAll("_", " ");

  function colorKey(data, mode) {
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
      if (mode !== "funcs" && unit.unidentified) continue;
      if (mode !== "funcs") {
        if (!matches([unit.id, unit.source, unit.dest_path].join(" "))) continue;
        if (filters.status && unit.status !== filters.status) continue;
        if (filters.source && unit.source !== filters.source) continue;
        if (filters.goal && !(unit.goals || []).includes(filters.goal)) continue;
      }
      const functions = mode === "funcs" ? (unit.functions || []).filter((fn) =>
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
            id: `fn:${JSON.stringify([unit.id, fn.name])}`, name: fn.name, kind: "function",
            tuId: unit.id, unit, status: fn.status, unidentified: unit.unidentified, weight: 1,
          };
          container.children.push(leaf);
          summary.items++;
          summary.functions++;
          summary.recorded += !["todo", "external"].includes(fn.status) ? 1 : 0;
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

  function describe(node, mode) {
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
      this.canvas = document.getElementById("progressMapCanvas");
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
      this.viewport = document.getElementById("progressMapViewport");
      this.tooltip = document.getElementById("progressMapTooltip");
      this.highlight = document.getElementById("mapHighlight");
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
      document.getElementById("mapUp").addEventListener("click", () => this.up());
      document.getElementById("mapReset").addEventListener("click", () => this.zoom("root"));
      document.getElementById("mapActivate").addEventListener("click", () => {
        const node = this.layout?.descendants().find((n) => n.data.id === this.selectedId);
        if (node) this.activate(node);
      });
      document.getElementById("mapUnitDetails").addEventListener("click", () => {
        const node = this.nodes.get(this.selectedId);
        if (node?.data.tuId) this.options.onOpen({ kind: "unit", tuId: node.data.tuId });
      });
      this.observer = new ResizeObserver(() => this.scheduleDraw());
      this.observer.observe(this.viewport);
      if (document.fonts) document.fonts.ready.then(() => this.scheduleDraw());
    }

    update(snapshot, mode, filters) {
      const signature = JSON.stringify([mode, filters, snapshot.units]);
      if (signature === this.signature) return;
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
      const unitMode = this.mode !== "funcs";
      this.options.onSummary(this.summary, this.mode);
      document.getElementById("mapExplanation").textContent = unitMode
        ? "One tile = one translation unit. Area = tracked function count; headline percentages count units." +
          (this.mode === "linked" ? " Purple means a confirmed external provider is included in the PC build inputs." : "")
        : "One tile = one function. Equal weight; colors use each function’s own recorded status.";
      const zero = document.getElementById("mapZeroNote");
      zero.hidden = !unitMode || !this.summary.zeroFunctions;
      zero.textContent = `${number(this.summary.zeroFunctions)} units have no tracked functions and receive minimum-sized tiles.`;
      const legend = document.getElementById("mapLegend");
      legend.replaceChildren();
      const keys = this.mode === "funcs" ? ["recorded", "external", "unrecorded", "unidentified"] :
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
        if (!node.children && ["todo", "unrecorded", "unidentified", "unlinked"].includes(colorKey(node.data, this.mode))) {
          ctx.fillStyle = this.theme.text;
        }
        ctx.fillText(node.data.name, node.x0 + 5, node.y0 + (node.children ? node.header : h) / 2);
        ctx.restore();
      }
      this.updateHighlight(descendants.find((node) => node.data.id === this.selectedId));
      const empty = document.getElementById("mapEmpty");
      empty.hidden = this.summary.items > 0;
      this.renderBreadcrumbs(zoomNode);
    }

    renderBreadcrumbs(node) {
      const root = document.getElementById("mapBreadcrumbs");
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
      document.getElementById("mapUp").disabled = !node.parent;
      document.getElementById("mapReset").disabled = !node.parent;
      const count = node.children ? node.leaves().length : 0;
      document.getElementById("mapScopeNote").textContent =
        `Viewing ${number(count)} ${this.mode === "funcs" ? "functions" : "translation units"}`;
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
      document.getElementById("mapSelectionType").textContent = desc.type;
      document.getElementById("mapSelectionName").textContent = desc.name;
      document.getElementById("mapSelectionMeta").textContent = desc.meta;
      const activate = document.getElementById("mapActivate");
      activate.hidden = false;
      activate.textContent = node.children ? "Zoom into group" : "Open details";
      document.getElementById("mapUnitDetails").hidden = !node.children || !node.data.tuId;
      if (announce) document.getElementById("mapAnnouncement").textContent = `${desc.type}: ${desc.name}. ${desc.meta}`;
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
      document.getElementById("mapSelectionType").textContent = "Progress map";
      document.getElementById("mapSelectionName").textContent = name;
      document.getElementById("mapSelectionMeta").textContent = "Select a tile to inspect it; select a group to zoom.";
      document.getElementById("mapActivate").hidden = true;
      document.getElementById("mapUnitDetails").hidden = true;
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
      document.getElementById("mapActivate").hidden = true;
      document.getElementById("mapUnitDetails").hidden = true;
      const desc = describe(this.nodes.get(id), this.mode);
      document.getElementById("mapSelectionType").textContent = "Group";
      document.getElementById("mapSelectionName").textContent = desc.name;
      document.getElementById("mapSelectionMeta").textContent = "Select a tile to inspect it; select a group to zoom further.";
      document.getElementById("mapAnnouncement").textContent = `Zoomed to ${desc.name}`;
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
