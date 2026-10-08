const assert = require("node:assert/strict");
const { test } = require("node:test");
const { buildTree, layoutTree, colorKey } = require("../bp_work_server/static/progress-map.js");

const snapshot = { units: [
  { id: "GameSource/Open.cpp", status: "blocked", linked: true, source: "decfigs", goals: ["boot"],
    function_count: 2, recorded_funcs: 1, functions: [
      { name: "Open::Pending", status: "todo" }, { name: "Open::Reviewed", status: "reviewed" },
    ] },
  { id: "class:Done", status: "done", linked: false, source: "class", goals: [],
    function_count: 1, recorded_funcs: 0, functions: [{ name: "Done::Pending", status: "todo" }] },
  { id: "GameSource/Empty.h", status: "todo", linked: false, source: "decfigs", goals: [],
    function_count: 0, recorded_funcs: 0, functions: [] },
  { id: "unidentified:console", status: "todo", linked: false, unidentified: true, goals: [],
    function_count: 1, recorded_funcs: 0, functions: [{ name: "sub_100", status: "todo" }] },
] };
const leaves = (tree) => tree.children ? tree.children.flatMap(leaves) : [tree];

test("build view uses confirmed providers and counts overlap once", () => {
  const units = [
    { id: "Source", status: "done", linked: true },
    { id: "vendor:lua", status: "external", linked: false, external_build_provider: { name: "Lua" } },
    { id: "class:D3D", status: "external", linked: true, external_build_provider: { name: "D3D9" } },
    { id: "vendor:crypto", status: "external", linked: false },
  ].map((u) => ({ ...u, goals: [], function_count: 1, recorded_funcs: 0 }));
  const map = buildTree({ units }, "linked");
  assert.deepEqual(map.summary.states, { linked: 1, external: 2, unlinked: 1 });
  assert.equal(map.summary.items, 4);
  assert.deepEqual(buildTree({ units }, "linked", { status: "external" }).summary.states,
                   { external: 2, unlinked: 1 });
});

test("external source has a distinct color and does not inflate recorded functions", () => {
  const external = { units: [{ id: "vendor:lua", status: "external", linked: false,
    goals: [], function_count: 2, recorded_funcs: 1, external_funcs: 1,
    functions: [{ name: "lua_call", status: "external" }, { name: "lua_helper", status: "reviewed" }] }] };
  const tus = buildTree(external, "tus");
  assert.deepEqual(tus.summary.states, { external: 1 });
  const funcs = buildTree(external, "funcs");
  assert.deepEqual(funcs.summary.states, { external: 1, recorded: 1 });
  assert.equal(funcs.summary.recorded, 1);
  assert.equal(buildTree(external, "funcs", { status: "external" }).summary.items, 1);
});

test("TU, function and linkage modes have different leaves and independent states", () => {
  const tus = buildTree(snapshot, "tus");
  const funcs = buildTree(snapshot, "funcs");
  const linked = buildTree(snapshot, "linked");
  assert.equal(tus.summary.items, 3);
  assert.equal(funcs.summary.items, 4);
  assert.equal(linked.summary.items, 3);
  assert.equal(tus.summary.zeroFunctions, 1);
  assert.ok(leaves(tus.root).every((n) => n.kind === "unit"));
  assert.ok(leaves(funcs.root).every((n) => n.kind === "function" && n.weight === 1));
  assert.deepEqual(funcs.summary.states, { unrecorded: 2, recorded: 1, unidentified: 1 });
  assert.deepEqual(tus.summary.states, { blocked: 1, done: 1, todo: 1 });
  assert.deepEqual(linked.summary.states, { linked: 1, unlinked: 2 });
  const pendingInDoneUnit = leaves(funcs.root).find((f) => f.name === "Done::Pending");
  assert.equal(colorKey(pendingInDoneUnit, "funcs"), "unrecorded");
  const reviewedInBlockedUnit = leaves(funcs.root).find((f) => f.name === "Open::Reviewed");
  assert.equal(colorKey(reviewedInBlockedUnit, "funcs"), "recorded");
});

test("function search and status filter inspect functions, TU filters inspect units", () => {
  assert.equal(buildTree(snapshot, "funcs", { q: "Reviewed", status: "reviewed" }).summary.items, 1);
  assert.equal(buildTree(snapshot, "funcs", { q: "Open.cpp" }).summary.items, 0);
  assert.equal(buildTree(snapshot, "tus", { status: "blocked", goal: "boot" }).summary.items, 1);
  assert.equal(buildTree(snapshot, "tus", { status: "reviewed" }).summary.items, 0);
  assert.equal(buildTree(snapshot, "tus", { source: "class" }).summary.items, 1);
  assert.equal(buildTree(snapshot, "funcs", { status: "todo" }).summary.items, 3);
});

test("weighted areas and geometry stay stable when only statuses change", () => {
  const initial = buildTree(snapshot, "tus").root;
  const changed = structuredClone(snapshot);
  changed.units[0].status = "done";
  const after = buildTree(changed, "tus").root;
  const geometry = (root) => layoutTree(root, 1200, 600).leaves().map((n) =>
    [n.data.id, n.x0, n.x1, n.y0, n.y1]);
  assert.deepEqual(geometry(initial), geometry(after));
  assert.deepEqual(geometry(initial), geometry(buildTree(snapshot, "linked").root));
  for (const n of layoutTree(initial, 390, 460).descendants()) {
    assert.ok(Number.isFinite(n.x0) && n.x0 >= 0 && n.x1 <= 390);
    assert.ok(Number.isFinite(n.y0) && n.y0 >= 0 && n.y1 <= 460);
    assert.ok(n.x1 >= n.x0 && n.y1 >= n.y0);
  }
});

test("each actual function remains a unique leaf at full project scale", () => {
  const units = Array.from({ length: 4500 }, (_, u) => ({
    id: `GameSource/Area${u % 30}/Unit${u}.cpp`, status: "done", goals: [],
    function_count: 7, recorded_funcs: 4,
    functions: Array.from({ length: 7 }, (_, f) => ({ name: `Unit${u}::Function${f}`, status: f < 4 ? "reviewed" : "todo" })),
  }));
  const tree = buildTree({ units }, "funcs");
  assert.equal(tree.summary.items, 31500);
  const map = layoutTree(tree.root, 1400, 600);
  assert.equal(map.leaves().length, 31500);
  assert.equal(new Set(map.leaves().map((n) => n.data.id)).size, 31500);
});
