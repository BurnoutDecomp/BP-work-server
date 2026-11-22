"""The dashboard page, sent with its figures already in it.

app.js draws every figure on the page from /dashboard/state and /api/bytematch/summary. A
client that runs no script (a link preview, a search engine, an assistant reading the page
for someone) used to get the loading shell alone: "Connecting to work server…", "0%
complete", "No available TUs.". So the server writes the same figures into the page it
sends, from the same data and in the markup app.js draws, and app.js redraws them in place
when it runs: a browser has the figures from the first paint, and nothing moves when the
script arrives.

Written in: the summary rings with their legends and the licence, the Crash FM ticker, byte
matching's ring, bars, status line and recent matches, the first page of each work list,
the server time, and the link-preview description. The maps, the charts, the explorer and
the console gaps stay app.js's alone. Times are UTC here; app.js gives them in the reader's
own zone.

Each function below follows the app.js function of the same name (render, setDonut,
renderLicense, renderTicker, renderBytematchFacts…): change one, change the other.
"""

from __future__ import annotations

import asyncio
import html
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from html.parser import HTMLParser
from typing import Any, Iterator

from bp_work_server.services.dashboard import bytematch_summary_for_page, dashboard_state_for_page

log = logging.getLogger(__name__)

# index.html's description for link previews. The page sent says the same with the
# headline figures in it.
STATIC_DESCRIPTION = "Live progress dashboard for the Burnout Paradise decompilation project."

# Rows on the first page of each work list: a phone's page (app.js pageRows). A wider
# window's script makes it 50.
LIST_ROWS = 10

RING_CIRCUMFERENCE = 326.7


# ---------------------------------------------------------------- the template's elements

VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)


@dataclass(eq=False)
class Element:
    """Where an element of index.html sits: its start tag, its content, its end tag."""

    tag: str
    attrs: dict[str, str | None]
    start: int  # the start tag's "<"
    open_end: int  # just past the start tag
    close_start: int = -1  # the end tag's "<"; -1 while it has none
    parent: Element | None = None
    children: list[Element] = field(default_factory=list)

    def has_class(self, name: str) -> bool:
        return name in (self.attrs.get("class") or "").split()

    def descendants(self) -> Iterator[Element]:
        for child in self.children:
            yield child
            yield from child.descendants()

    def find(self, *, cls: str | None = None, attr: str | None = None) -> Element | None:
        """The first element under this one with the class and the attribute."""
        for node in self.descendants():
            if (cls is None or node.has_class(cls)) and (attr is None or attr in node.attrs):
                return node
        return None

    def closest(self, cls: str) -> Element | None:
        node = self.parent
        while node is not None and not node.has_class(cls):
            node = node.parent
        return node


class _Indexer(HTMLParser):
    def __init__(self, source: str):
        super().__init__()
        self.source = source
        self.line_starts = [0, *(match.end() for match in re.finditer("\n", source))]
        self.stack: list[Element] = []
        self.ids: dict[str, Element] = {}

    def _here(self) -> int:
        line, column = self.getpos()
        return self.line_starts[line - 1] + column

    def _open(self, tag: str, attrs: list[tuple[str, str | None]], closed: bool) -> None:
        start = self._here()
        node = Element(tag, dict(attrs), start, start + len(self.get_starttag_text() or ""),
                       parent=self.stack[-1] if self.stack else None)
        if node.parent is not None:
            node.parent.children.append(node)
        if node.attrs.get("id"):
            self.ids[node.attrs["id"]] = node
        if not closed and tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, attrs, closed=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, attrs, closed=True)

    def handle_endtag(self, tag: str) -> None:
        # Elements an end tag closes implicitly keep close_start -1: their content is
        # never replaced.
        for depth in range(len(self.stack) - 1, -1, -1):
            if self.stack[depth].tag == tag:
                self.stack[depth].close_start = self._here()
                del self.stack[depth:]
                return


@lru_cache(maxsize=4)
def template_ids(source: str) -> dict[str, Element]:
    """index.html's elements by id. The file changes only with a deploy."""
    indexer = _Indexer(source)
    indexer.feed(source)
    indexer.close()
    return indexer.ids


_ATTRIBUTE = re.compile(r"""\s+([^\s"'<>/=]+)(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?""")


def start_tag(raw: str, changes: dict[str, str | bool | None]) -> str:
    """A start tag with attributes set (True: bare, as hidden) or removed (None)."""
    name = re.match(r"<[^\s/>]+", raw)
    close = re.search(r"\s*/?>\Z", raw)
    if not name or not close:
        raise ValueError(f"not a start tag: {raw[:40]!r}")
    parts: list[str] = []
    done: set[str] = set()
    for match in _ATTRIBUTE.finditer(raw, name.end(), close.start()):
        key = match.group(1).lower()
        if key not in changes:
            parts.append(match.group(0).strip())
        elif key not in done:
            done.add(key)
            if changes[key] is not None:
                parts.append(_attribute(key, changes[key]))
    parts += [_attribute(key, value) for key, value in changes.items() if key not in done and value is not None]
    return name.group(0) + "".join(f" {part}" for part in parts) + close.group(0)


def _attribute(name: str, value: str | bool) -> str:
    return name if value is True else f'{name}="{esc(value)}"'


class Edits:
    """Changes to one copy of index.html, made in a single pass at the end."""

    def __init__(self, source: str):
        self.source = source
        self.ids = template_ids(source)
        self._content: dict[Element, str] = {}
        self._attrs: dict[Element, dict[str, str | bool | None]] = {}

    def el(self, target: str | Element | None) -> Element | None:
        return self.ids.get(target) if isinstance(target, str) else target

    def html(self, target: str | Element | None, markup: str) -> None:
        node = self.el(target)
        if node is not None and node.close_start >= 0:
            self._content[node] = markup

    def text(self, target: str | Element | None, value: Any) -> None:
        self.html(target, esc(value))

    def attr(self, target: str | Element | None, name: str, value: str | bool | None) -> None:
        node = self.el(target)
        if node is not None:
            self._attrs.setdefault(node, {})[name] = value

    def render(self) -> str:
        cuts = [(node.start, node.open_end, start_tag(self.source[node.start:node.open_end], changes))
                for node, changes in self._attrs.items()]
        cuts += [(node.open_end, node.close_start, markup) for node, markup in self._content.items()]
        cuts.sort(key=lambda cut: (cut[0], cut[1]))
        out: list[str] = []
        at = 0
        for start, end, replacement in cuts:
            if start < at:
                raise ValueError(f"two edits overlap at offset {start}")
            out += (self.source[at:start], replacement)
            at = end
        out.append(self.source[at:])
        return "".join(out)


# ---------------------------------------------------------------- the browser's formatting

def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def num(value: Any) -> float:
    """Number(value || 0)."""
    if not value:
        return 0
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0


def first_present(*values: Any) -> Any:
    """a ?? b ?? 0."""
    return next((value for value in values if value is not None), 0)


def clamp(value: float, low: float = 0, high: float = 100) -> float:
    return max(low, min(high, value))


def fixed(value: float, digits: int = 1) -> str:
    """Number.prototype.toFixed: a half rounds up, as in the browser."""
    return str(Decimal(value).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))


def trimmed(value: float, digits: int = 1) -> str:
    """Number(value.toFixed(1)): 96.5, but 100 and 41."""
    text = fixed(value, digits)
    return text.rstrip("0").rstrip(".") if "." in text else text


def js_number(value: float) -> str:
    """String(number)."""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def js_string(value: Any) -> str:
    """`${value}` for a value from JSON."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return js_number(value)
    if isinstance(value, list):
        return ",".join(js_string(item) for item in value)
    if isinstance(value, dict):
        return "[object Object]"
    return str(value)


def fmt_int(value: Any) -> str:
    return f"{round(num(value)):,}"


def fmt_compact(value: Any) -> str:
    """950, 1.3k, 11.2k, 2M."""
    count = int(num(value))
    if count < 1000:
        return str(count)
    for size, suffix in ((10**12, "T"), (10**9, "B"), (10**6, "M"), (10**3, "k")):
        if count >= size:
            text = str((Decimal(count) / size).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
            return text.removesuffix(".0") + suffix
    return str(count)


def fmt_bytes(value: Any) -> str:
    if value is None:
        return ""
    size = num(value)
    if size < 1024:
        return f"{js_number(size)} B"
    if size < 1024**2:
        return f"{fixed(size / 1024)} KB"
    if size < 1024**3:
        return f"{fixed(size / 1024 / 1024)} MB"
    return f"{fixed(size / 1024 / 1024 / 1024, 2)} GB"


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def rel_time(value: Any, now: datetime) -> str:
    when = parse_time(value)
    if when is None:
        return ""
    minutes = int(max(0.0, (now - when).total_seconds()) // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    if hours // 24 < 30:
        return f"{hours // 24}d ago"
    return when.strftime("%Y-%m-%d")


def fmt_time(value: Any) -> str:
    """The reader's own date and time in app.js; UTC here, and it says so."""
    if not value:
        return "none"
    when = parse_time(value)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if when else str(value)


def short_time(value: Any) -> str:
    if not value:
        return "none"
    when = parse_time(value)
    return when.astimezone(timezone.utc).strftime("%H:%M:%S") if when else str(value)


# ---------------------------------------------------------------- app.js's markup

RIBBON_TONES = {
    "done": "success", "reviewed": "success",
    "compiled": "warning", "compiles": "warning", "cat-mid": "warning", "tier-MEDIUM": "warning",
    "blocked": "danger", "recovered": "danger", "cat-high": "danger", "tier-HIGH": "danger",
    "in_progress": "info", "live": "info",
    "external": "external",
}


def ribbon(tone: str | None, label: str) -> str:
    tone_attr = f' data-tone="{esc(tone)}"' if tone else ""
    return f'<span class="b5-ribbon"{tone_attr}>{esc(label)}</span>'


def pill(kind: str, label: str) -> str:
    return ribbon(RIBBON_TONES.get(kind), label)


def tu_button(tu_id: Any, cls: str = "tu-name") -> str:
    return f'<button type="button" class="text-link {cls}">{esc(tu_id)}</button>'


def actor_node(name: Any, login: Any, profiles: dict[str, Any]) -> str:
    if not name:
        return '<span class="muted-text">none</span>'
    profile = login or profiles.get(name) or profiles.get(str(name).strip())
    title = f"Open {name}'s contribution profile" if profile else f"Open {name}'s profile"
    return f'<button type="button" class="text-link actor-link" title="{esc(title)}">{esc(name)}</button>'


def detail_text(detail: Any) -> str:
    if not detail or not isinstance(detail, dict):
        return ""
    if detail.get("reconstructed") or detail.get("source") == "b5-decomp commit reconstruction":
        return "reconstructed from b5-decomp"
    # the stored commit SHA is the bogus backfill one; never surface it
    return " | ".join(
        f"{key}: {', '.join(js_string(item) for item in value) if isinstance(value, list) else js_string(value)}"
        for key, value in detail.items()
        if key != "commit" and value is not None and value != ""
    )


@dataclass
class Segment:
    label: str
    value: Any
    color: str
    title: str | None = None


RING_METERS = {"tu": "meterUnits", "fn": "meterFuncs", "exe": "meterBuild", "bytematch": "meterMatch"}


def set_donut(edits: Edits, ring_id: str, segments: list[Segment], percent: float, total_text: str) -> None:
    ring = edits.el(f"{ring_id}Ring")
    if ring is None:
        return
    total = sum(max(0, num(seg.value)) for seg in segments)
    circles, start = [], 0.0
    for index, seg in enumerate(segments):
        value = max(0, num(seg.value))
        if not value or not total:
            continue
        length = value / total * RING_CIRCUMFERENCE
        # A hairline gap between arcs so the states read as separate. The arcs' hover titles
        # come with app.js: here they would only repeat the legend to a reader of the text.
        dash = max(0, length - (1.2 if len(segments) > 1 and length > 2 else 0))
        circles.append(
            f'<circle cx="60" cy="60" r="52" class="ring-seg seg-{seg.color}{" lead" if index == 0 else ""}"'
            f' data-index="{index}" stroke-dasharray="{js_number(dash)} {js_number(RING_CIRCUMFERENCE - dash)}"'
            f' stroke-dashoffset="{js_number(-start)}"></circle>'
        )
        start += length
    edits.html(ring.find(cls="ring-segments"), "".join(circles))
    edits.text(f"{ring_id}Percent", f"{fixed(percent)}%")
    edits.attr(ring, "style", f"--p: {js_number(clamp(percent))}")
    report_ring(edits, ring_id, percent)
    edits.text(f"{ring_id}Count", total_text)
    rows = []
    for seg in segments:
        value = max(0, num(seg.value))
        title = f' title="{esc(seg.title)}"' if seg.title else ""
        share = f"{fixed(value / total * 100)}%" if total else "–"
        # The spaces fall between grid items, where they take no room; they keep "done
        # 4,244 96.2%" apart for a reader of the text.
        rows.append(
            f'<div class="legend-row"><span class="sw sw-{seg.color}"></span> <span class="lg-label"{title}>'
            f'{esc(seg.label)}</span> <span class="lg-n">{fmt_int(value)}</span> <span class="lg-pct">{share}</span></div>'
        )
    edits.html(f"{ring_id}Legend", "".join(rows))


def report_ring(edits: Edits, ring_id: str, percent: float) -> None:
    """The licence card's meter for a ring; the first ring also fills the header's chip."""
    meter = edits.el(RING_METERS.get(ring_id))
    if meter is None or not math.isfinite(percent):
        return
    value = clamp(percent)
    edits.attr(meter, "aria-valuenow", js_number(value))
    edits.attr(meter, "data-b5-progress", js_number(value))
    edits.attr(meter, "style", f"--b5-meter: {js_number(value / 100)}")
    edits.text(meter.find(attr="data-b5-progress-label"), f"{trimmed(value)}%")
    if ring_id == "tu":
        edits.text("licenseChipValue", f"{fixed(percent)}%")


# The game's license classes, on the share of translation units complete.
LICENSE_RANKS = [
    (0, "Learner's permit", "L"),
    (20, "D-class license", "D"),
    (40, "C-class license", "C"),
    (60, "B-class license", "B"),
    (80, "A-class license", "A"),
    (95, "Burnout license", "B5"),
    (100, "Elite license", "Elite"),
]


def render_license(edits: Edits, complete: float, tus: float, source_commit: Any) -> None:
    percent = complete / tus * 100 if tus else 0
    rank = next((r for r in reversed(LICENSE_RANKS) if percent >= r[0]), LICENSE_RANKS[0])
    upcoming = next((r for r in LICENSE_RANKS if r[0] > percent), None)
    for target, value in (("licenseRank", rank[1]), ("licenseClass", rank[2]),
                          ("licenseChipRank", rank[1]), ("licenseChipClass", rank[2])):
        edits.text(target, value)
    if upcoming:
        needed = max(0, math.ceil(upcoming[0] / 100 * tus) - complete)
        edits.text("licenseNext", f"Next: {upcoming[1]} at {upcoming[0]}% · {fmt_int(needed)} "
                                  f"{'unit' if needed == 1 else 'units'} to go")
    else:
        edits.text("licenseNext", "Every unit complete: Paradise City is rebuilt.")
    edits.text("licenseNumber", f"No. {str(source_commit)[:10]}" if source_commit else "No. —")


# ---------------------------------------------------------------- the work state

def render(edits: Edits, data: dict[str, Any], now: datetime) -> None:
    totals = data.get("totals") or {}
    counts = data.get("counts") or {}
    # refresh() says Live once the state arrives; the page has it already
    edits.attr("connection", "data-tone", "success")
    edits.text("connectionText", "Live")
    edits.text("subtitle", f"{fmt_int(totals.get('tus'))} translation units · {fmt_int(totals.get('funcs'))} functions")
    tus = num(totals.get("tus"))
    external_tus = num(first_present(totals.get("external_tus"), counts.get("external")))
    complete = num(totals.get("done_tus")) + external_tus
    set_donut(edits, "tu", [
        Segment("done", counts.get("done"), "green"),
        Segment("external", external_tus, "purple",
                "Supplied by existing vendor source or a host platform implementation; counts toward completion."),
        Segment("compiled", counts.get("compiled"), "gold"),
        Segment("in progress", counts.get("in_progress"), "blue"),
        Segment("blocked", counts.get("blocked"), "red"),
        Segment("todo", counts.get("todo"), "grey"),
    ], complete / tus * 100 if tus else 0, f"{fmt_int(complete)} / {fmt_int(tus)} complete")
    ledger = data.get("ledger_evidence") or {}
    render_license(edits, complete, tus, ledger.get("source_commit"))

    funcs = num(totals.get("funcs"))
    covered = num(totals.get("done_funcs")) + num(totals.get("external_funcs"))
    unidentified = num(totals.get("unidentified_funcs"))
    set_donut(edits, "fn", [
        Segment("recorded", totals.get("done_funcs"), "gold"),
        Segment("external", totals.get("external_funcs"), "purple",
                "Provided by vendor source or the host platform; counts toward coverage."),
        Segment("named, unrecorded", max(0, funcs - covered - unidentified), "grey"),
        Segment("unidentified", unidentified, "dim",
                "Functions IDA found in the binary that nobody has named: no DWARF file, no RTTI class, "
                "so no translation unit — but still code to write."),
    ], covered / funcs * 100 if funcs else 0, f"{fmt_int(covered)} / {fmt_int(funcs)} covered")
    recorded_states = [(status, count) for status, count in (data.get("function_status_counts") or {}).items()
                       if status != "todo" and count]
    edits.text("fnStatusNote", "Ledger: " + " · ".join(
        f"{fmt_int(count)} {'compiled' if status == 'compiles' else status}" for status, count in recorded_states
    ) if recorded_states else "Coverage recorded in the work ledger.")
    source_commit = ledger.get("source_commit")
    if source_commit:
        latest = (data.get("decomp_repo") or {}).get("revision")
        edits.attr("sourceSyncNote", "hidden", None)
        edits.text("sourceSyncNote", f"Source update pending · showing b5 {str(source_commit)[:10]}"
                   if latest and latest != source_commit
                   else f"Source reconciled: b5 {str(source_commit)[:10]} · {rel_time(ledger.get('reconciled_at'), now)}")
    missing_tus = ledger.get("done_tus_with_missing_bodies")
    edits.text("tuEvidenceNote",
               f"Ledger status · {fmt_int(missing_tus)} done TUs have missing-body audit findings. "
               f"{fmt_int(external_tus)} external TUs count toward completion." if missing_tus
               else f"Ledger status · {fmt_int(external_tus)} external TUs count toward completion.")
    missing_funcs = ledger.get("recorded_funcs_with_missing_bodies")
    if missing_funcs:
        edits.text("fnEvidenceNote", f"{fmt_int(missing_funcs)} recorded functions have missing-body audit findings; "
                                     "these require source review.")

    linked = num(totals.get("linked_tus"))
    external_build = num(totals.get("external_build_tus"))
    source_linked = num(first_present(totals.get("source_linked_tus"), linked))
    available = num(first_present(totals.get("available_tus"), source_linked + external_build))
    set_donut(edits, "exe", [
        Segment("linked source", source_linked, "blue", "Source files compiled or included by the build."),
        Segment("external provider", external_build, "purple",
                "External TUs supplied by a confirmed library or host backend in the build inputs. "
                "Separate from reconstructed source; each TU counts once."),
        Segment("not included", max(0, tus - available), "grey",
                "No source inclusion or verified external provider found in the build inputs."),
    ], available / tus * 100 if tus else 0, f"{fmt_int(available)} / {fmt_int(tus)} available")

    edits.text("activeGoal", data.get("active_goal") or "Whole program")
    edits.text("serverTime", fmt_time(data.get("server_time")))

    profiles = data.get("actor_profiles") or {}
    render_agents(edits, data.get("agents") or [], data.get("attribution_cache") or {}, profiles, now)
    render_active_work(edits, data.get("active_work") or [], profiles)
    render_queue(edits, (data.get("next") or {}).get("items") or [])
    events = sorted(data.get("recent_events") or [], key=lambda event: (event_sort_time(event), event.get("id") or 0),
                    reverse=True)
    render_ticker(edits, events, now)
    render_events(edits, events, profiles)
    render_goals(edits, data.get("goals") or [])
    render_blocked(edits, data.get("blocked") or [])


def render_agents(edits: Edits, agents: list[dict], coverage: dict, profiles: dict, now: datetime) -> None:
    active = [agent for agent in agents if agent.get("has_active_work") or num(agent.get("total")) > 0]
    edits.text("agentCount", fmt_int(len(active)))
    edits.text("agentSummary", f"{fmt_int(len(agents))} users · {fmt_int(len(active))} with active work")
    if not agents:
        edits.text("agents", "No users registered.")
        return
    counts_from_older_rev = bool(coverage.get("counts_repo_rev") and coverage.get("repo_rev")
                                 and coverage["counts_repo_rev"] != coverage["repo_rev"])
    complete = bool(coverage.get("file_complete") and coverage.get("function_complete")) or counts_from_older_rev
    label = "contributed to" if complete else "contributed to cached"
    coverage_text = "" if complete else (
        f" (cache {fmt_int(coverage.get('file_cached') or 0)}/{fmt_int(coverage.get('file_total') or 0)} TUs, "
        f"{fmt_int(coverage.get('function_cached') or 0)}/{fmt_int(coverage.get('function_total') or 0)} funcs)")
    rows = []
    for agent in agents:
        busy = bool(agent.get("has_active_work") or num(agent.get("total")) > 0)
        name = agent.get("name") or "unknown"
        # spaced like the legend rows: .agent-name is a flex row
        badges = (" " + ribbon("success", "active") if busy else "") + (" " + ribbon("warning", "admin") if agent.get("is_admin") else "")
        if agent.get("registered") and not agent.get("worker_active"):
            badges += " " + ribbon("", "disabled")
        last = rel_time(agent.get("last_activity") or agent.get("last_update") or agent.get("last_seen"), now) or "never"
        meta = (f"{fmt_int(agent.get('total'))} active | {label} {fmt_int(agent.get('contributed_tus') or 0)} TUs / "
                f"{fmt_int(agent.get('contributed_funcs') or 0)} funcs{coverage_text} | primary on "
                f"{fmt_int(agent.get('primary_tus') or 0)} TUs / {fmt_int(agent.get('primary_funcs') or 0)} funcs | "
                f"lease {short_time(agent.get('lease_expires_at'))} | last {last}")
        work = "".join(tu_button(tu_id, "tu-chip") for tu_id in agent.get("current_work") or [])
        login = agent.get("github_username") or (name if agent.get("registered") else None)
        rows.append(
            f'<div class="agent-row{"" if busy else " agent-idle"}"><div class="agent-name">'
            f"{actor_node(name, login, profiles)}{badges}</div><div class=\"agent-meta\">{esc(meta)}</div>"
            + (f'<div class="agent-work">{work}</div>' if work else "") + "</div>"
        )
    edits.attr("agents", "class", "agent-list")
    edits.html("agents", "".join(rows))


def render_active_work(edits: Edits, items: list[dict], profiles: dict) -> None:
    edits.text("activeWorkCount", f"{len(items)} TUs")
    body = edits.el("activeWork")
    # with nothing claimed, the card shows its empty-garage panel instead of the table
    edits.attr(body.closest("table-wrap") if body else None, "hidden", None if items else True)
    edits.attr("activeWorkEmpty", "hidden", True if items else None)
    rows = []
    for item in items:
        status = str(item.get("status") or "")
        lease = short_time(item.get("lease_expires_at")) if item.get("lease_expires_at") else "no active lease"
        rows.append(
            f'<tr class="clickable"><td>{tu_button(item.get("id"))}<div class="tu-meta">'
            f'{esc(item.get("source") or "unknown")} · {fmt_int(item.get("n_funcs"))} funcs</div></td>'
            f"<td>{pill(status, status.replace('_', ' ', 1))}</td><td>{actor_node(item.get('owner'), None, profiles)}</td>"
            f"<td>{esc(lease)}</td></tr>"
        )
    edits.html(body, "".join(rows))


def mini_foot(edits: Edits, prefix: str, total: int) -> None:
    pages = max(1, math.ceil(total / LIST_ROWS))
    edits.text(f"{prefix}Range", f"{fmt_int(1 if total else 0)}-{fmt_int(min(total, LIST_ROWS))} of {fmt_int(total)}")
    edits.text(f"{prefix}PageStatus", f"Page 1 of {fmt_int(pages)}")
    edits.attr(f"{prefix}Prev", "disabled", True)
    if pages <= 1:
        edits.attr(f"{prefix}Next", "disabled", True)


def render_queue(edits: Edits, items: list[dict]) -> None:
    edits.text("nextCount", f"{fmt_compact(len(items))} ready")
    if items:
        edits.attr("nextQueue", "class", "queue")
        edits.html("nextQueue", "".join(
            f'<div class="queue-row clickable">{tu_button(item.get("id"))}<div class="tu-meta">'
            f'{esc(item.get("source") or "unknown")} · {fmt_int(item.get("n_funcs"))} funcs · '
            f'{fmt_int(item.get("unresolved_deps"))} unresolved deps</div></div>'
            for item in items[:LIST_ROWS]
        ))
    mini_foot(edits, "queue", len(items))


def is_backfilled_event(event: dict) -> bool:
    source = str((event.get("detail") or {}).get("source") or "").lower()
    return "pre-server" in source or "commit delta" in source


def event_sort_time(event: dict) -> float:
    """Newest first; a backfilled row app.js could not date sinks to the bottom."""
    when = None if is_backfilled_event(event) else parse_time(event.get("ts"))
    return when.timestamp() if when else -math.inf


def render_events(edits: Edits, events: list[dict], profiles: dict) -> None:
    edits.text("eventCount", f"{fmt_compact(len(events))} events")
    if not events:
        edits.text("events", "No events yet.")
    else:
        rows = ['<div class="event-row event-head">'
                + "".join(f'<div class="event-cell">{label}</div>' for label in ("Time", "Event", "Actor", "Target", "Details"))
                + "</div>"]
        for event in events[:LIST_ROWS]:
            ts = event.get("ts")
            title = f' title="{esc(fmt_time(ts))}"' if ts else ""
            agent = event.get("agent")
            actor = actor_node(agent, event.get("agentLogin"), profiles) if agent else '<span class="muted-text">server</span>'
            target = (f'<div class="event-cell event-target">{tu_button(event["tu_id"], "event-tu")}</div>' if event.get("tu_id")
                      # a phone's event card leaves it out
                      else '<div class="event-cell event-target is-server">server</div>')
            rows.append(
                f'<div class="event-row"><div class="event-cell event-time"{title}>{esc(short_time(ts))}</div>'
                f'<div class="event-cell event-action">{esc(event.get("action") or "event")}</div>'
                f'<div class="event-cell">{actor}</div>{target}'
                f'<div class="event-cell event-detail">{esc(detail_text(event.get("detail")) or "-")}</div></div>'
            )
        edits.attr("events", "class", "event-table")
        edits.html("events", "".join(rows))
    mini_foot(edits, "events", len(events))


def ticker_item(title: str, rest: str) -> str:
    return f'<li class="b5-ticker__item"><strong>{esc(title)}</strong> {esc(rest)}</li>'


def render_ticker(edits: Edits, events: list[dict], now: datetime) -> None:
    """Crash FM: the newest events. app.js lays them twice over, the copy hidden from
    assistive technology, so the crawl loops without a seam; it reaches the loop minutes
    after app.js has run, so the page sends them once and a reader of the text reads
    them once."""
    latest = events[:14]
    if not latest:
        edits.html("tickerTrack", ticker_item("Quiet", "no events on the work server yet"))
        edits.attr("tickerTrack", "style", "animation: none;")
        return
    items = []
    for event in latest:
        tu_id = event.get("tu_id")
        where = str(tu_id).split("/")[-1] if tu_id and tu_id != "None" else ""
        rest = " · ".join(part for part in (event.get("agent") or "server", where, rel_time(event.get("ts"), now)) if part)
        items.append(ticker_item(event.get("action") or "event", rest))
    edits.html("tickerTrack", "".join(items))


def render_goals(edits: Edits, goals: list[dict]) -> None:
    edits.text("goalCount", f"{len(goals)} goals")
    if not goals:
        return
    rows = []
    for goal in goals:
        done, total = num(goal.get("done")), num(goal.get("total"))
        width = clamp(done / total * 100 if total else 0)
        rows.append(
            f'<div class="goal-row clickable"><button type="button" class="text-link tu-name goal-title">{esc(goal.get("name"))}'
            f'</button><div class="goal-meta">{esc(goal.get("category") or "uncategorized")} · {fmt_int(done)} / '
            f'{fmt_int(total)} done</div><div class="bar"><span style="width: {js_number(width)}%;"></span></div></div>'
        )
    edits.attr("goals", "class", "goal-list")
    edits.html("goals", "".join(rows))


def render_blocked(edits: Edits, items: list[dict]) -> None:
    edits.text("blockedListCount", f"{fmt_compact(len(items))} TUs")
    if items:
        edits.attr("blockedList", "class", "blocked-list")
        edits.html("blockedList", "".join(
            f'<div class="blocked-row clickable">{tu_button(item.get("id"))}'
            f'<div class="tu-meta">{esc(item.get("notes") or "No reason recorded.")}</div></div>'
            for item in items[:LIST_ROWS]
        ))
    mini_foot(edits, "blocked", len(items))


# ---------------------------------------------------------------- byte matching

BM_STATES = [
    ("exact", "exact", "green",
     "The same bytes as ARTIST outside the fields the linker fills in, with the same callees and constants. "
     "Click: newest first."),
    ("sdk", "XDK library", "purple", "Byte-identical to a function of the XDK's own libraries: linked, never written."),
    ("nonmatching", "compiled, not matching", "amber",
     "The native tree compiles a body for it, but the instructions differ. Click: closest to matching first."),
    ("missing", "no compiled body", "crimson",
     "No object of the native tree defines this function yet. Click: smallest first."),
    ("unnamed", "not yet named", "dim",
     "Functions of the console image without a name; nothing can be paired with them yet."),
]


def render_bytematch(edits: Edits, summary: dict[str, Any], now: datetime) -> None:
    counts = summary.get("status_counts") or {}
    whole = (summary.get("totals") or {}).get("all") or {}
    set_donut(edits, "bytematch", [
        Segment(label, (counts.get(key) or {}).get("functions"), color, title) for key, label, color, title in BM_STATES
    ], num((summary.get("percent") or {}).get("matched_functions")),
        f"{fmt_int(whole.get('matched'))} / {fmt_int(whole.get('functions'))} functions byte-match ARTIST")
    render_bytematch_facts(edits, summary, now)
    render_bytematch_recent(edits, summary.get("recent") or [], now)


def bytematch_meter(label: str, value: float, note: str, title: str, tone: str | None = None) -> str:
    tone_attr = f' data-tone="{tone}"' if tone else ""
    # spaced like the legend rows: a meter is a grid
    return (
        f'<div class="b5-meter bm-meter"{tone_attr} style="--b5-meter: {js_number(clamp(value) / 100)};"'
        f' title="{esc(title)}"><span class="b5-meter__label">{esc(label)}</span> <span class="b5-meter__value">'
        f'{fixed(value)}%</span> <span class="b5-meter__bar" aria-hidden="true"></span> '
        f'<small class="bm-meter__note">{esc(note)}</small></div>'
    )


def render_bytematch_facts(edits: Edits, summary: dict[str, Any], now: datetime) -> None:
    meta = summary.get("meta") or {}
    if meta.get("b5_commit"):
        edits.attr("bytematchStamp", "hidden", None)
        edits.attr("bytematchStamp", "title", f"summary built {fmt_time(meta.get('generated_at'))}"
                   + (f" · {meta['toolchain']}" if meta.get("toolchain") else ""))
        edits.text("bytematchStamp", f"x360 {str(meta['b5_commit'])[:10]} · {rel_time(meta.get('generated_at'), now)}")
    percent = summary.get("percent") or {}
    totals = summary.get("totals") or {}
    whole, game, sdk = totals.get("all") or {}, totals.get("game") or {}, totals.get("sdk") or {}
    edits.html("bytematchFacts", "".join((
        bytematch_meter(
            "Code that matches", num(percent.get("image_bytes")),
            f"Like the ring, counted by size: {fmt_bytes(whole.get('matched_bytes'))} of the game's "
            f"{fmt_bytes(whole.get('bytes'))} of code.",
            f"{fmt_int(whole.get('matched_bytes'))} of {fmt_int(whole.get('bytes'))} bytes of code: "
            "our exact functions plus XDK library code.", "success"),
        bytematch_meter(
            "Our functions exact", num(percent.get("exact_functions")),
            f"{fmt_int(game.get('exact'))} of the {fmt_int(game.get('functions'))} functions compiled from our source "
            f"({fixed(num(percent.get('exact_bytes')))}% of their code).",
            f"Our source is the image less the {fmt_int(sdk.get('functions'))} XDK library functions and the unnamed ones."),
        bytematch_meter(
            "Fuzzy match", num(percent.get("fuzzy_bytes")),
            "How alike our instructions are to the original, by size: an exact function counts 100%, one with no body 0%.",
            "Each function's instruction similarity to ARTIST's, weighted by its size, over all the functions "
            "compiled from our source.", "info"),
    )))
    render_bytematch_status(edits, summary, now)


def render_bytematch_status(edits: Edits, summary: dict[str, Any], now: datetime) -> None:
    """The measurement's own state in one quiet line; only a problem takes a colour."""
    meta = summary.get("meta") or {}
    objects = (summary.get("totals") or {}).get("objects") or {}
    parts = []

    def add(words: str, title: str, tone: str | None = None) -> None:
        tone_attr = f' data-tone="{tone}"' if tone else ""
        parts.append(f'<span{tone_attr} title="{esc(title)}">{esc(words)}</span>')

    sources = num(objects.get("sources"))
    if sources:
        failed = sources - num(objects.get("compiled"))
        add(f"{fmt_int(failed)} of {fmt_int(sources)} objects fail to compile" if failed
            else f"all {fmt_int(sources)} native sources compile",
            f"{fmt_int(sources)} native sources measured with the XDK compiler.", "danger" if failed else None)
    if num(objects.get("stale")):
        add(f"{fmt_int(objects.get('stale'))} measured before a source change",
            "Records whose source file changed on the branch since it was measured. "
            "Their verdicts stand until the next measurement.", "warning")
    if meta.get("verified_commit"):
        # unknown (null) is neither behind nor known to be current
        behind = None if meta.get("behind") is None else num(meta.get("behind"))
        verified_at = meta.get("verified_at")
        proven = f"x360 {str(meta['verified_commit'])[:7]}" + (f", {rel_time(verified_at, now)}" if verified_at else "")
        add(f"{fmt_int(behind)} commits since the last full refresh ({proven})" if behind
            else f"{'records last proven' if behind is None else 'every record proven'} at {proven}",
            f"Every record was last proven current at x360 {str(meta['verified_commit'])[:10]}"
            + (f" ({fmt_time(verified_at)})" if verified_at else "") + ".", "warning" if behind else None)
    edits.html("bytematchStatus", " · ".join(parts))


def render_bytematch_recent(edits: Edits, recent: list[dict], now: datetime) -> None:
    if not recent:
        edits.html("bytematchRecent", '<div class="muted-text">No function has matched yet.</div>')
        return
    items = []
    for match in recent[:7]:
        title = (f"first exact {fmt_time(match.get('at'))} at x360 {str(match.get('commit') or '')[:10]}"
                 + (f" · {match['source']}" if match.get("source") else ""))
        meta = (f"ARTIST {js_string(match.get('artist'))} · {js_string(match.get('size'))} bytes · "
                f"{rel_time(match.get('at'), now)}")
        items.append(
            f'<li title="{esc(title)}"><span class="top-n">{fmt_compact(match.get("size"))}</span><div>'
            f'<div class="evidence-file-path">{esc(match.get("name") or match.get("artist"))}</div>'
            f'<div class="evidence-file-meta">{esc(meta)}</div></div></li>'
        )
    edits.html("bytematchRecent", "".join(items))


# ---------------------------------------------------------------- the page

def describe(state: dict[str, Any] | None, summary: dict[str, Any] | None) -> str | None:
    """The link-preview description, with the headline figures."""
    figures = []
    if state:
        totals, counts = state.get("totals") or {}, state.get("counts") or {}
        tus, funcs = num(totals.get("tus")), num(totals.get("funcs"))
        if tus:
            complete = num(totals.get("done_tus")) + num(first_present(totals.get("external_tus"), counts.get("external")))
            figures.append(f"{fixed(complete / tus * 100)}% of translation units complete")
        if funcs:
            covered = num(totals.get("done_funcs")) + num(totals.get("external_funcs"))
            figures.append(f"{fixed(covered / funcs * 100)}% of functions covered")
    if summary and summary.get("available"):
        matched = num((summary.get("percent") or {}).get("matched_functions"))
        figures.append(f"{fixed(matched)}% of the Xbox 360 functions byte-matching the original")
    if not figures:
        return None
    return f"{STATIC_DESCRIPTION.removesuffix('.')}: {', '.join(figures)}."


def fill_page(source: str, state: dict[str, Any] | None, summary: dict[str, Any] | None, now: datetime) -> str:
    """index.html with the figures of the state and the byte-match summary written in."""
    edits = Edits(source)
    if state:
        render(edits, state, now)
    if summary and summary.get("available"):
        render_bytematch(edits, summary, now)
    page = edits.render()
    description = describe(state, summary)
    if description:
        page = page.replace(f'content="{STATIC_DESCRIPTION}"', f'content="{esc(description)}"')
    return page


async def dashboard_page(app: Any, source: str) -> str:
    """The page to send: index.html with the current figures, or as it is if they fail."""
    try:
        state, summary = await asyncio.gather(dashboard_state_for_page(app), bytematch_summary_for_page(app))
        return fill_page(source, state, summary, datetime.now(timezone.utc))
    except Exception:
        log.exception("the dashboard page went out without its figures")
        return source
