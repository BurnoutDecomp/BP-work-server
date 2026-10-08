"""Apply source-derived coverage without inventing review passes or taking claims."""
from __future__ import annotations

import hashlib
import json
import re

from bp_work_server import history
from bp_work_server.store import iso

KEY = "source_status_state"
GAP_NOTE = re.compile(r"un[- ]?homed|not homed|missing|not reconstructed|not bodied|stub|keystone|deferred|nothing committed|blocked on", re.IGNORECASE)
REVIEW_NOTE = re.compile(r"review (?:fail|reject)|semantic (?:bug|mismatch)|incorrect|regression|\bwrong\b|\bparity\b|divergen", re.IGNORECASE)


def summary(store):
    with store.connect() as con:
        row = con.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()
    state = json.loads(row[0]) if row else {}
    return {key: state.get(key) for key in ("source_commit", "inputs_hash", "applied_at", "counts")}


def apply(store, evidence):
    # The endpoint accepts evidence only from an authenticated admin CI publisher.
    # Compare-and-swap prevents an older queued job from undoing a newer snapshot.
    payload = dict(evidence)
    payload.pop("base_source_commit", None)
    report_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    with store.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()
        previous = json.loads(row[0]) if row else {}
        if previous.get("report_hash") == report_hash:
            return {"source_commit": previous["source_commit"], "unchanged": True}
        if previous.get("source_commit") != evidence.get("base_source_commit"):
            raise ValueError("Source status advanced while this snapshot was being generated; retry from the current revision")
        funcs = {r["name"]: dict(r) for r in con.execute("SELECT name,tu_id,status FROM func")}
        tus = {r["id"]: dict(r) for r in con.execute("SELECT id,status,owner,lease_expires_at,notes FROM tu")}
        protected = {name for name, r in tus.items()
                     if r["status"] in ("in_progress", "compiled") or r["owner"] or r["lease_expires_at"]}
        present = evidence["functions"]
        complete = evidence["tus"]
        required = {}
        for name, current in funcs.items():
            if "`" not in name:
                required.setdefault(current["tu_id"], set()).add(name)
        for name, proof in complete.items():
            if name not in tus or not proof["functions"] or set(proof["functions"]) != required.get(name, set()) or any(
                fn not in present or fn not in funcs or funcs[fn]["tu_id"] != name
                for fn in proof["functions"]
            ):
                raise ValueError("TU source evidence does not match ledger membership: " + name)
        managed_funcs = set(previous.get("auto_functions", []))
        managed_tus = dict(previous.get("auto_tus", {}))
        if not previous:
            # Adopt the explicit source-reconciliation repairs made before the
            # automatic pipeline was enabled. Reviewed/manual records stay separate.
            source_tus = {name for name, current in tus.items()
                          if (current["notes"] or "").startswith("Source reconciliation")}
            managed_funcs.update(name for name, current in funcs.items()
                                 if current["status"] == "recovered" and name in present
                                 and current["tu_id"] in source_tus)
            managed_tus.update({name: tus[name]["notes"] for name in source_tus
                                if tus[name]["status"] == "done" and name in complete})
        counts = {"functions_recovered": 0, "functions_removed": 0, "tus_completed": 0,
                  "tus_reopened": 0, "protected_tus": len(protected)}
        old_functions = previous.get("functions", {})
        for name, proof in present.items():
            current = funcs.get(name)
            if not current or current["tu_id"] in protected:
                continue
            if current["status"] == "todo" and old_functions.get(name) != proof["digest"]:
                con.execute("UPDATE func SET status='recovered' WHERE name=? AND status='todo'", (name,))
                managed_funcs.add(name)
                counts["functions_recovered"] += 1
        for name in list(managed_funcs):
            current = funcs.get(name)
            if not current:
                managed_funcs.discard(name)
            elif current["tu_id"] in protected:
                continue
            elif current["status"] not in ("todo", "recovered"):
                managed_funcs.discard(name)  # a human/normal workflow now owns this verdict
            elif name not in present:
                con.execute("UPDATE func SET status='todo' WHERE name=? AND status='recovered'", (name,))
                managed_funcs.discard(name)
                counts["functions_removed"] += current["status"] == "recovered"
        old_tus = previous.get("tus", {})
        for name, proof in complete.items():
            current = tus[name]
            if name in protected or current["status"] == "done":
                continue
            notes = current["notes"] or ""
            if current["status"] == "blocked" and (not GAP_NOTE.search(notes) or REVIEW_NOTE.search(notes)):
                continue
            if old_tus.get(name) == proof["digest"]:
                continue  # respect a manual reset/block on unchanged source
            note = f"Source reconciliation at b5 {evidence['source_commit'][:12]}: all {len(proof['functions'])} named non-thunk functions have scoped definitions; no stub candidates."
            con.execute("UPDATE tu SET status='done',notes=?,updated_at=? WHERE id=?", (note, iso(), name))
            managed_tus[name] = note
            counts["tus_completed"] += 1
        prior_managed_tus = dict(previous.get("auto_tus", {}))
        if not previous:
            prior_managed_tus.update(managed_tus)
        for name, note in prior_managed_tus.items():
            current = tus.get(name)
            if not current:
                managed_tus.pop(name)
            elif name in protected:
                continue
            elif current["status"] != "done" or current["notes"] != note:
                managed_tus.pop(name)  # preserve subsequent explicit worker decisions
            elif name not in complete:
                con.execute("UPDATE tu SET status='blocked',notes=?,updated_at=? WHERE id=?",
                            ("Source reconciliation: previously indexed bodies are missing or stub candidates in the new committed source.", iso(), name))
                managed_tus.pop(name)
                counts["tus_reopened"] += 1
        state = {"source_commit": evidence["source_commit"], "report_hash": report_hash,
                 "inputs_hash": evidence.get("inputs_hash"),
                 "applied_at": iso(), "counts": counts,
                 "functions": {name: proof["digest"] for name, proof in present.items()},
                 "tus": {name: proof["digest"] for name, proof in complete.items()},
                 "auto_functions": sorted(managed_funcs), "auto_tus": managed_tus}
        store._set_meta(con, KEY, json.dumps(state, sort_keys=True))
        if any(counts[key] for key in ("functions_recovered", "functions_removed", "tus_completed", "tus_reopened")):
            store._log(con, "server", "reconcile", None, {
                "source": "automatic source-status reconciliation", "b5_commit": evidence["source_commit"],
                **counts, "note": "Committed source evidence; no new review verdict."})
    with store.connect() as con:
        history.record(con, iso(), None, source="source_reconciliation")
    return {"source_commit": evidence["source_commit"], "unchanged": False, **counts}
