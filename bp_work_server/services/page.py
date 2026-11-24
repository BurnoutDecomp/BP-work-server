"""The dashboard page, sent with its figures already in it.

app.js draws everything on the page from the server's JSON (/dashboard/state, the byte-match
summary, the builds, the GitHub overview, the lists' own endpoints). A client that runs no
script (a link preview, a search engine, an assistant reading the page for someone) used to
get the loading shell alone: "Connecting to work server…", "0% complete", "No available
TUs.". So the server writes the same content into the page it sends, from the same data and
in the markup app.js draws, and app.js redraws it in place when it runs: a browser has it
from the first paint, and nothing moves when the script arrives.

Written in: the download sign, the summary rings with their legends and the licence, the
Crash FM ticker; byte matching's ring, bars, status line, recent matches, history legend,
areas and function list; the console gaps' filters, files and largest gaps; each work list;
the evolution chart's series, span and legend; the repository panel; the server time and
the link-preview description. Lists come a phone's page at a time (LIST_ROWS), which a wider
window's script lengthens. Only drawings made to the reader's window and saved settings stay
app.js's alone: the two maps and the two charts' lines. Times are UTC here; app.js gives
them in the reader's own zone.

Each function below follows the app.js function of the same name (render, setDonut,
renderLicense, renderTicker, renderBytematchFacts, renderEvidenceList, renderGithub…), and
the sign follows its design in the UI library's download-designs.js: change one, change the
other. tests/test_page.py renders the sign with the library itself and compares.
"""

from __future__ import annotations

import asyncio
import html
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from html.parser import HTMLParser
from typing import Any, Iterator

from bp_work_server.services.dashboard import dashboard_state_for_page

log = logging.getLogger(__name__)

# index.html's description for link previews. The page sent says the same with the
# headline figures in it.
STATIC_DESCRIPTION = "Live progress dashboard for the Burnout Paradise decompilation project."

# Rows on the first page of each list: a phone's page (app.js pageRows). A wider window's
# script makes it 25 or 50.
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


def js_round(value: float) -> int:
    """Math.round: a half goes up."""
    return math.floor(value + 0.5)


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


def ribbon(tone: str | None, label: str, title: str | None = None) -> str:
    tone_attr = f' data-tone="{esc(tone)}"' if tone else ""
    title_attr = f' title="{esc(title)}"' if title else ""
    return f'<span class="b5-ribbon"{tone_attr}{title_attr}>{esc(label)}</span>'


def pill(kind: str, label: str, title: str | None = None) -> str:
    return ribbon(RIBBON_TONES.get(kind), label, title)


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

@dataclass
class ByteState:
    key: str
    label: str
    color: str
    pill: str
    title: str


BM_STATES = [
    ByteState("exact", "exact", "green", "done",
              "The same bytes as ARTIST outside the fields the linker fills in, with the same callees and constants. "
              "Click: newest first."),
    ByteState("sdk", "XDK library", "purple", "external",
              "Byte-identical to a function of the XDK's own libraries: linked, never written."),
    ByteState("nonmatching", "compiled, not matching", "amber", "compiled",
              "The native tree compiles a body for it, but the instructions differ. Click: closest to matching first."),
    ByteState("missing", "no compiled body", "crimson", "blocked",
              "No object of the native tree defines this function yet. Click: smallest first."),
    ByteState("unnamed", "not yet named", "dim", "todo",
              "Functions of the console image without a name; nothing can be paired with them yet."),
]


def render_bytematch(edits: Edits, summary: dict[str, Any], now: datetime) -> None:
    counts = summary.get("status_counts") or {}
    whole = (summary.get("totals") or {}).get("all") or {}
    set_donut(edits, "bytematch", [
        Segment(st.label, (counts.get(st.key) or {}).get("functions"), st.color, st.title) for st in BM_STATES
    ], num((summary.get("percent") or {}).get("matched_functions")),
        f"{fmt_int(whole.get('matched'))} / {fmt_int(whole.get('functions'))} functions byte-match ARTIST")
    render_bytematch_facts(edits, summary, now)
    render_bytematch_history(edits, summary.get("history") or [])
    render_bytematch_recent(edits, summary.get("recent") or [], now)
    render_bytematch_areas(edits, summary.get("areas") or [])
    render_bytematch_chips(edits, counts)


def render_bytematch_history(edits: Edits, history: list[dict]) -> None:
    """The chart's legend, under a placeholder of the chart's height: app.js draws the lines
    to the box's width, which the server does not know, and the box keeps its height."""
    points = [point for point in history if point.get("functions") is not None][-365:]
    if len(points) < 2:
        first = points[0] if points else None
        edits.html("bytematchHistory", '<div class="muted-text vh-empty">' + esc(
            f"One published summary so far (x360 {str(first.get('commit') or '')[:10]}: {fmt_int(first.get('exact'))} "
            f"exact of {fmt_int(first.get('functions'))}). The lines start with the next one." if first
            else "No summary published yet.") + "</div>")
        return

    def value(point: dict, key: str) -> float:
        return num(point.get("exact")) + num(point.get("nonmatching")) if key == "compiled" else num(point.get(key))

    start, end = points[0], points[-1]
    items = "".join(
        f'<span class=""><span class="vh-sw {cls}"></span> {label} {fmt_int(value(start, key))} → '
        f"{fmt_int(value(end, key))}</span>"
        for cls, label, key in (("vh-sw-paired", "compiled bodies", "compiled"), ("vh-sw-clean", "exact", "exact"),
                                ("vh-sw-nobody", "no compiled body", "missing"))
    )
    edits.html("bytematchHistory", '<svg viewBox="0 0 320 96" style="height: 96px" aria-hidden="true"></svg>'
                                   f'<div class="vp-shape-legend">{items}</div>')


def render_bytematch_areas(edits: Edits, areas: list[dict]) -> None:
    if not areas:
        edits.html("bytematchAreas", '<div class="muted-text">No areas yet.</div>')
        return
    rows = []
    for area in areas[:30]:
        size = num(area.get("bytes"))
        exact = 100 * num(area.get("exact_bytes")) / size if size else 0
        fuzzy = 100 * num(area.get("fuzzy_bytes")) / size if size else 0
        title = (f"{area.get('area')}: {fmt_int(area.get('exact'))} exact, {fmt_int(area.get('nonmatching'))} not "
                 f"matching, {fmt_int(area.get('missing'))} missing; {fmt_int(area.get('exact_bytes'))} of "
                 f"{fmt_int(area.get('bytes'))} bytes exact, {fixed(fuzzy)}% fuzzy")
        rows.append(
            f'<button type="button" class="bm-area-row" title="{esc(title)}"><span class="bm-area-name">'
            f'{esc(area.get("area"))}</span><div class="bm-bar"><div class="bm-bar-fuzzy" style="width: '
            f'{fixed(min(100, fuzzy), 2)}%;"></div><div class="bm-bar-exact" style="width: {fixed(min(100, exact), 2)}%;">'
            f'</div></div><span class="bm-area-nums">{fmt_int(area.get("exact"))} / {fmt_int(area.get("functions"))} · '
            f"{fixed(exact)}%</span></button>"
        )
    if len(areas) > 30:
        rows.append(f'<div class="muted-text">{fmt_int(len(areas) - 30)} smaller areas in the list below.</div>')
    edits.html("bytematchAreas", "".join(rows))
    # a phone shows the first 8 (phone.css [data-fold]); app.js adds the button for the rest
    edits.attr("bytematchAreas", "data-fold", "8")


def evidence_chip(label: str, count: Any, active: bool, color: str | None) -> str:
    """A painted filter: an optional state swatch, the label, then its count."""
    swatch = f'<i class="sw sw-{color}" aria-hidden="true"></i>' if color else ""
    number = f'<span class="">{fmt_int(count)}</span>' if count is not None else ""
    return (f'<button type="button" class="b5-filter" aria-pressed="{"true" if active else "false"}">'
            f"{swatch}{esc(label)}{number}</button>")


def render_bytematch_chips(edits: Edits, counts: dict[str, Any]) -> None:
    edits.html("bytematchChips", evidence_chip("all", None, True, None) + "".join(
        evidence_chip(st.label, (counts.get(st.key) or {}).get("functions") or 0, False, st.color) for st in BM_STATES
    ))


def render_bytematch_list(edits: Edits, page: dict[str, Any], measured: bool, repo: dict[str, str], now: datetime) -> None:
    items = page.get("items") or []
    total = int(num(page.get("total")))
    cards = [] if items else [f'<div class="muted-text">{"No functions match." if measured else "Nothing measured yet."}</div>']
    states = {st.key: st for st in BM_STATES}
    for item in items:
        meta = esc(f"ARTIST {js_string(item.get('artist'))} · {fmt_int(item.get('size'))} bytes")
        if item.get("source"):
            href = f"https://github.com/{repo['owner']}/{repo['name']}/blob/x360/{item['source']}"
            meta += f' · <a href="{esc(href)}" target="_blank" rel="noopener">{esc(item["source"])}</a>'
        elif item.get("tu"):
            meta += esc(f" · {item['tu']}")
        if item.get("first_exact"):
            meta += esc(f" · exact since {rel_time(item['first_exact'], now)}")
        state = states.get(item.get("status"))
        nums = pill(state.pill, state.label) if state else pill("todo", str(item.get("status")))
        if item.get("status") == "nonmatching":
            nums += pill("todo", f"{fixed(num(item.get('score')))}%",
                         "Fuzzy match: how alike its instructions are to the original's")
        if item.get("stale"):
            nums += pill("live", "source changed", "Its source file changed on the branch after this verdict was measured.")
        cards.append(
            f'<div class="evidence-file bm-row"><div class="evidence-file-head"><div class="evidence-file-left">'
            f'<div class="evidence-file-path">{esc(item.get("name"))}</div><div class="evidence-file-meta">{meta}</div>'
            f'</div><div class="evidence-file-nums">{nums}</div></div></div>'
        )
    edits.html("bytematchList", "".join(cards))
    edits.text("bytematchRange", f"{fmt_int(1 if total else 0)}–{fmt_int(min(total, LIST_ROWS))} of {fmt_int(total)} functions")
    edits.text("bmPageStatus", f"Page 1 of {fmt_int(max(1, math.ceil(total / LIST_ROWS)))}")
    edits.attr("bmPagePrev", "disabled", True)
    if LIST_ROWS >= total:
        edits.attr("bmPageNext", "disabled", True)


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


# ---------------------------------------------------------------- console gaps

EVIDENCE_AUDIT_CHIPS = [
    ("", "all files", "", ""),
    ("NO_BODY", "no body", "crimson", "NO_BODY"),
    ("MISSING_CASE", "switch gaps", "red", "MISSING_CASE"),
    ("EXTRA_CASE", "misfiled case ids", "red", "EXTRA_CASE"),
    ("MISSING_EVENT", "missing event posts", "red", "MISSING_EVENT"),
    ("MISSING_CALLEE", "missing callees", "red", "MISSING_CALLEE"),
    ("MISSING_ASSERT", "missing asserts", "amber", "MISSING_ASSERT"),
]


def render_evidence_summary(edits: Edits, audit: dict[str, Any], now: datetime) -> None:
    """The audit tab, as the panel opens: every file with a gap, no filter."""
    funcaudit = audit.get("funcaudit") or {}
    if funcaudit.get("commit"):
        when = funcaudit.get("generated_at") or funcaudit.get("imported_at")
        edits.text("evidenceMeta", f"b5-decomp {str(funcaudit['commit'])[:10]} · {rel_time(when, now)}")
    else:
        edits.text("evidenceMeta", "no audit yet")
    if funcaudit.get("generated_at"):
        edits.attr("evidenceMeta", "title", f"audited {fmt_time(funcaudit['generated_at'])}")
    edits.text("evidenceIntro", "Files ranked by their gaps. Open one to see each function and what the console's "
                                "version has that ours lacks.")
    categories = funcaudit.get("categories") or {}
    chips = []
    for key, label, color, category in EVIDENCE_AUDIT_CHIPS:
        count = (categories.get(category) or {}).get("functions", 0) if category else None
        if category and not count:
            continue
        chips.append(evidence_chip(label, count, key == "", color))
    edits.html("evidenceChips", "".join(chips))


def pager(total: int, page_size: int, unit: str, page: int = 1) -> str:
    """The UI library's numbered pager (pager.js), as createPager().set() draws it."""
    pages = max(1, math.ceil(total / page_size))
    page = max(1, min(pages, page))
    first, last = ((page - 1) * page_size + 1 if total else 0), min(page * page_size, total)
    shown = (f"{fmt_int(first)}–{fmt_int(last)} of {fmt_int(total)}{f' {unit}' if unit else ''}" if total
             else f"No {unit or 'results'}")

    def button(text: str, target: int, key: str, disabled: bool = False) -> str:
        extra = ""
        if key == "number":
            extra = f' aria-label="Page {target}"' + (' aria-current="page"' if target == page else "")
        return (f'<button type="button" class="b5-page-button" data-b5-page="{target}" data-b5-page-key="{key}"'
                f'{" disabled" if disabled else ""}{extra}>{esc(text)}</button>')

    parts = [button("Previous", page - 1, "previous", page == 1)]
    previous = 0
    for number in sorted(n for n in {1, pages, page - 1, page, page + 1} if 1 <= n <= pages):
        if previous and number - previous > 1:
            parts.append('<span class="b5-pagination__gap" aria-hidden="true">…</span>')
        parts.append(button(str(number), number, "number"))
        previous = number
    parts.append(button("Next", page + 1, "next", page == pages))
    parts.append(f'<label class="b5-pagination__jump"><span>Jump</span><input class="b5-input" type="number" min="1" '
                 f'max="{pages}" value="{page}" aria-label="Jump to page" data-b5-page-key="jump"></label>')
    return f'<span class="b5-pagination__range" role="status">{esc(shown)}</span><div class="b5-pagination__controls">' \
           f'{"".join(parts)}</div>'


def render_evidence_list(edits: Edits, page: dict[str, Any], audited: bool) -> None:
    items = page.get("items") or []

    def count(n: Any, one: str, many: str) -> str:
        return f"{fmt_int(n)} {one if num(n) == 1 else many}"

    cards = [] if items else [
        f'<div class="muted-text">{"No files match." if audited else "No audit has been imported yet."}</div>']
    for item in items:
        meta = (f'<div class="evidence-file-meta">{count(item["functions"], "function", "functions")} to review</div>'
                if item.get("functions") else "")
        nums = pill("cat-high", count(item.get("weight"), "gap", "gaps"))
        if item.get("no_body"):
            nums += pill("tier-HIGH", f"{fmt_int(item['no_body'])} no body")
        for key, kind, one, many in (("missing_case", "cat-mid", "switch gap", "switch gaps"),
                                     ("missing_event", "cat-mid", "event", "events"),
                                     ("missing_callee", "cat-low", "callee", "callees"),
                                     ("missing_assert", "cat-low", "assert", "asserts")):
            if item.get(key):
                nums += pill(kind, count(item[key], one, many))
        cards.append(
            '<div class="evidence-file"><div class="evidence-file-head" title="Open every function of this file in the '
            f'drawer"><div class="evidence-file-left"><div class="evidence-file-path">'
            f'{esc(item.get("file") or "(functions the ledger files nowhere)")}</div>{meta}</div>'
            f'<div class="evidence-file-nums">{nums}</div></div></div>'
        )
    edits.html("evidenceList", "".join(cards))
    edits.html("evidencePager", pager(int(num(page.get("total"))), LIST_ROWS, "files"))


def render_evidence_top(edits: Edits, items: list[dict]) -> None:
    """The largest switch gaps: the side list as the audit tab opens."""
    edits.text("evidenceTopNote", "Functions with the most console case ids that have no arm in our body — whole "
                                  "behaviours the console dispatches and we never reach.")
    rows = []
    for item in items:
        count = item.get("ids") if item.get("ids") is not None else item.get("count")
        preview = item.get("preview")
        where = js_string(item.get("file")) + (f" — {preview}" if preview else "")
        rows.append(f'<li><span class="top-n">{fmt_int(count)}</span><span class="top-name">{esc(item.get("name"))}'
                    f'<span class="top-meta">{esc(where)}</span></span></li>')
    edits.html("evidenceTop", "".join(rows))
    # a phone shows the first 5 (phone.css [data-fold]); app.js adds the button for the rest
    edits.attr("evidenceTop", "data-fold", "5")


# ---------------------------------------------------------------- the latest build

# app.js draws the download control in this design (its DOWNLOAD_DESIGN), and the page goes
# out with the same markup, so the hero has its sign from the first paint. The test suite
# renders the sign with download-designs.js itself and compares.
DOWNLOAD_DESIGN = "sign-game"
ICONS = "/static/b5-ui/assets/icons.svg"
PACKAGES = {
    "full": {"title": "Full game", "short": "First install", "icon": "folder",
             "blurb": "Game files, DLLs and assets. For your first install or when the assets change."},
    "update": {"title": "Update only", "short": "Over your game folder", "icon": "refresh",
               "blurb": "Executable, DLLs and .cgsmap. Extract over your existing game folder."},
}
_LIBRARY_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}
ARROW = ('<svg class="b5-sign__arrow" viewBox="0 0 44 62" aria-hidden="true">'
         '<path d="M17 0h10v38h13L22 62 4 38h13z"/></svg>')
LANE_ARROW = ('<svg class="dd-lane__arrow" viewBox="0 0 44 62" aria-hidden="true">'
              '<path d="M17 0h10v38h13L22 62 4 38h13z"/></svg>')


def lib_esc(value: Any) -> str:
    """download-designs.js's esc(): String(value ?? '') with &<>"' as entities."""
    text = value if isinstance(value, str) else js_string(value)
    return re.sub(r"""[&<>"']""", lambda match: _LIBRARY_ESCAPES[match.group(0)], text)


def download_view(build: dict[str, Any], builds: list[dict], now: datetime) -> dict[str, Any]:
    """app.js downloadView(): the latest build as the download designs read it."""
    previous = next((other for other in builds if other.get("id") != build.get("id")), None)
    update_url, changed = build.get("update_url"), build.get("assets_changed")
    if not update_url:
        text = "This build predates the exe-only update; the next published build will offer one."
    elif changed is False and previous:
        since = f" ({rel_time(previous['built_at'], now)})" if previous.get("built_at") else ""
        text = (f"Game assets unchanged since build {previous.get('commit_short') or previous.get('id')}{since}: "
                "if you already have the game folder, the update is all you need.")
    elif changed is True:
        text = "The game assets changed in this build: take the full game unless you know which files moved."
    else:
        text = "First published build: take the full game."
    if update_url and changed is False and previous:
        note = {"tone": "success", "label": "Assets unchanged", "text": text}
    elif update_url and changed is True:
        note = {"tone": "warning", "label": "Assets changed", "text": text}
    else:
        note = {"text": text}
    return {
        "number": build.get("id") if build.get("id") is not None else "",
        "commit": build.get("commit_short") or str(build.get("commit_sha") or "")[:12],
        "age": rel_time(build["built_at"], now) if build.get("built_at") else "",
        "count": num(build.get("downloads")),
        "full": {"size": fmt_bytes(build.get("size_bytes")), "href": build.get("download_url") or "/download/latest",
                 "blurb": "Everything: the exe, its DLLs and every game asset. For a first install, or when the "
                          "assets changed."},
        "update": {"size": fmt_bytes(build.get("bundle_size")), "href": update_url,
                   "blurb": "Burnout_PC.exe, its DLLs and the .cgsmap. Drop them over an existing game folder."}
        if update_url else None,
        "note": note,
    }


def download_data(data: dict[str, Any]) -> dict[str, Any]:
    """download-designs.js downloadData(): the packages filled in from their defaults."""
    def pack(key: str) -> dict[str, Any] | None:
        given = data.get(key)
        return {**PACKAGES[key], "href": "#", "file": "", "size": "", **given, "key": key} if given else None

    full = pack("full") or {**PACKAGES["full"], "href": "#", "file": "", "size": "", "key": "full"}
    update = pack("update")
    return {
        "number": data.get("number") if data.get("number") is not None else "",
        "commit": js_string(data.get("commit")),
        "age": js_string(data.get("age")),
        "count": max(0, math.floor(num(data.get("count")))),
        "place": data.get("place") if data.get("place") is not None else "Paradise City",
        "full": full,
        "update": update,
        "packages": [item for item in (full, update) if item],
        "note": data.get("note"),
    }


def sign_game(prefix: str, d: dict[str, Any]) -> str:
    """download-designs.js's 'sign-game' design (a sign() road sign) for these data."""
    count = d["count"]
    word = "Download" if count == 1 else "Downloads"

    def icon(name: str) -> str:
        return f'<svg class="b5-icon" aria-hidden="true"><use href="{ICONS}#{name}"></use></svg>'

    said = ('<span class="b5-sr-only"> ' + (f"number {lib_esc(d['number'])}, " if d["number"] != "" else "")
            + (f"commit {lib_esc(d['commit'])}, " if d["commit"] else "")
            + f'<span data-dl="downloads">{count} download{"" if count == 1 else "s"}</span>, </span>')
    note = d["note"]
    note_inner = ""
    if note:
        if note.get("label"):
            tone = f' data-tone="{lib_esc(note["tone"])}"' if note.get("tone") else ""
            note_inner = f'<span class="b5-status"{tone}>{lib_esc(note["label"])}</span> '
        note_inner += f"<span>{lib_esc(note.get('text'))}</span>"
    lanes = "".join(
        f'<a class="dd-choice dd-choice--{item["key"]} dd-lane" href="{lib_esc(item["href"])}"'
        + (f' download="{lib_esc(item["file"])}"' if item.get("file") else "")
        + f'><span class="dd-lane__lamp" aria-hidden="true"></span><span class="dd-lane__text"><span class="dd-lane__name">'
          f'{lib_esc(item["title"])}</span><span class="dd-lane__size">{lib_esc(item["size"])}</span></span>{LANE_ARROW}'
          f'<span class="b5-sr-only">, {lib_esc(item["blurb"])}</span></a>'
        for item in d["packages"]
    )
    panel = (f'<div class="dd-chooser dd-lanes" id="{prefix}-menu" role="region" aria-labelledby="{prefix}-menu-title" '
             f'data-dd-panel inert><span class="b5-sr-only" id="{prefix}-menu-title">Choose your download</span>'
             f'<span class="dd-lanes__row">{lanes}</span><p class="dd-note dd-lanes__note" data-dl="note"'
             f'{"" if note else " hidden"}>{note_inner}</p></div>')
    states = [("hover", "NEW", f"BUILD {js_string(d['number'])}"), ("open", "PICK", "A LANE")]
    for item in d["packages"]:
        figure = re.match(r"^\s*([\d.,]+)\s*(\S*)", item["size"] or "")
        states.append((item["key"], *(figure.groups() if figure else (item["size"], ""))))
    vms = (f'<span class="b5-vms" aria-hidden="true"><span class="b5-vms__value" data-dl="count">{count}</span>'
           f'<span class="b5-vms__label" data-dl="downloads-word">{word}</span>'
           + "".join(f'<span class="dd-vms dd-vms--{state}"><b>{lib_esc(big)}</b><small>{lib_esc(small)}</small></span>'
                     for state, big, small in states) + "</span>")
    inside = (f'<span class="b5-sign__legend"><strong>Download<span class="b5-sr-only"> build</span></strong>'
              f'<small class="b5-sr-only">{said}{lib_esc(d["place"])}, {lib_esc(d["full"]["size"])}</small></span>{ARROW}')
    extra = (f'<span class="sv-supp" aria-hidden="true">Build {lib_esc(d["number"])} · {lib_esc(d["full"]["size"])} '
             "<i>▾</i></span>")
    preview = (f'<button class="b5-download-preview" type="button" data-dl="preview" aria-label="Preview build contents">'
               f'{icon("eye")}</button>')
    return (f'<div class="b5-download-group b5-download-group--sign dd-sign sv-game" id="{prefix}-group" data-dd-chooser>\n'
            '<div class="b5-sign">\n'
            f'<button class="b5-download" id="{prefix}" type="button" aria-expanded="false" aria-controls="{prefix}-menu" '
            f'data-dd-trigger><span class="b5-sign__lamps" aria-hidden="true"></span>{inside}</button>\n'
            f"{panel}\n"
            f"{vms}{extra}\n"
            f'<span class="b5-sign__plaque">{preview}<span>commit {lib_esc(d["commit"])} · '
            f'<span data-dl="age">{lib_esc(d["age"])}</span></span></span>\n'
            "</div>\n"
            "</div>")


def render_download(edits: Edits, builds: dict[str, Any], now: datetime) -> None:
    """app.js refreshDownload()'s first draw: the sign in the hero, its chooser closed."""
    latest = builds.get("latest")
    if not latest:
        return  # no build published: the slot stays hidden, as app.js leaves it
    data = download_data(download_view(latest, builds.get("builds") or [], now))
    # createDownloadChooser() marks the chooser closed as it wires it
    markup = sign_game("download", data).replace(" data-dd-chooser>", ' data-dd-chooser data-phase="closed">', 1)
    edits.attr("downloadSlot", "hidden", None)
    edits.attr("downloadSlot", "data-download-design", DOWNLOAD_DESIGN)
    edits.html("downloadSlot", markup)


# ---------------------------------------------------------------- the repository

def collation_key(name: str) -> tuple:
    """Close to localeCompare() in English, which app.js sorts the file tree with: spaces,
    then punctuation, digits and letters, case only breaking ties (lowercase first)."""
    punctuation = "_-,;:!?.'\"()[]{}@*/\\&#%`^+<=>|~$"
    primary = []
    for char in name:
        if char.isspace():
            primary.append((0, 0))
        elif char in punctuation:
            primary.append((1, punctuation.index(char)))
        elif char.isdigit():
            primary.append((2, ord(char)))
        elif char.isascii() and char.isalpha():
            primary.append((3, ord(char.lower())))
        else:
            primary.append((4, ord(char.lower())))
    return tuple(primary), tuple(0 if char.islower() else 1 for char in name)


def build_tree(entries: list[dict]) -> dict[str, Any]:
    """app.js buildTree(): the nested tree from GitHub's flat path list."""
    root: dict[str, Any] = {"name": "", "children": {}, "type": "tree"}
    for entry in entries:
        parts = str(entry.get("path") or "").split("/")
        node = root
        for index, part in enumerate(parts):
            leaf = index == len(parts) - 1
            child = node["children"].get(part)
            if child is None:
                child = node["children"][part] = {
                    "name": part, "children": {}, "type": entry.get("type") if leaf else "tree",
                    "size": entry.get("size") if leaf else None, "path": "/".join(parts[: index + 1]),
                }
            node = child
    return root


# Rows under the top level the page sends. The tree's box scrolls, and its height never
# follows its content (the commits' column sets it; a phone caps it at 320px): these fill
# it, and app.js draws the rest (tests/ alone opens on 818 entries).
TREE_ROWS = 40


def tree_rows(node: dict[str, Any], depth: int, blob_url, budget: list[int]) -> str:
    """app.js renderTreeNode(), as it first draws: top-level folders open on their children,
    deeper ones closed. Every top-level entry is sent, and the first `budget` rows under
    them; a closed folder's contents are drawn by app.js alone."""
    rows = []
    for child in sorted(node["children"].values(),
                        key=lambda entry: (entry["type"] != "tree", collation_key(entry["name"]))):
        if depth >= 1:
            if budget[0] <= 0:
                break
            budget[0] -= 1
        folder = child["type"] == "tree"
        closed = depth >= 1
        twist = ("▶" if closed else "▼") if folder else ""
        if folder:
            name = f'<span class="name">{esc(child["name"])}</span>'
        else:
            name = (f'<a class="name file-link" href="{esc(blob_url(child["path"]))}" target="_blank" rel="noopener" '
                    f'title="{esc("Open " + child["path"] + " on GitHub")}">{esc(child["name"])}</a>')
        size = f'<span class="size">{fmt_bytes(child["size"])}</span>' if not folder and child.get("size") is not None else ""
        rows.append(f'<div class="tree-node{" dir" if folder else ""}" style="padding-left: {6 + depth * 16}px;">'
                    f'<span class="twist">{twist}</span><span class="icon">{"📁" if folder else "📄"}</span>{name}{size}</div>')
        if folder:
            inner = "" if closed else tree_rows(child, depth + 1, blob_url, budget)
            rows.append(f'<div class="tree-children{" collapsed" if closed else ""}">{inner}</div>')
    return "".join(rows)


def commit_message(commit: dict[str, Any]) -> str:
    message = esc(commit.get("message") or "(no message)")
    if commit.get("html_url"):
        return f'<a href="{esc(commit["html_url"])}" target="_blank" rel="noopener">{message}</a>'
    return message


def render_github(edits: Edits, data: dict[str, Any], repo: dict[str, str], now: datetime) -> None:
    """app.js renderGithub(): the repository panel."""
    info = data.get("info") or {}
    edits.text("repoBranch", repo["ref"])
    edits.text("repoLink", info.get("full_name") or f"{repo['owner']}/{repo['name']}")
    edits.attr("repoLink", "href", info.get("html_url") or f"https://github.com/{repo['owner']}/{repo['name']}")
    edits.text("repoDesc", info.get("description") or "No description provided.")
    rate = data.get("rate_limit") or {}
    if rate.get("remaining") is not None:
        edits.text("ghRate", f"API {js_string(rate['remaining'])}/{js_string(rate.get('limit'))} "
                             f"({'auth' if rate.get('authenticated') else 'anon'})")
        if num(rate["remaining"]) <= 5:
            edits.attr("ghRate", "class", "gh-rate warn")
    stats = [f'<div class="gh-stat">{mark} <strong>{fmt_int(info[key])}</strong> {label}</div>'
             for mark, key, label in (("★", "stargazers_count", "stars"), ("⑂", "forks_count", "forks"),
                                      ("◎", "open_issues_count", "issues"), ("⊙", "watchers_count", "watching"))
             if info.get(key) is not None]
    if info.get("language"):
        stats.append(f'<div class="gh-stat"><strong>{esc(info["language"])}</strong></div>')
    if info.get("pushed_at"):
        stats.append(f'<div class="gh-stat">pushed {esc(rel_time(info["pushed_at"], now))}</div>')
    edits.html("ghStats", "".join(stats))

    def byline(commit: dict[str, Any]) -> str:
        return esc(f"{commit.get('author') or commit.get('login') or 'unknown'} · {rel_time(commit.get('date'), now)}")

    latest = data.get("latest_commit")
    if latest:
        avatar = (f'<img class="avatar" src="{esc(latest["avatar_url"] + "&s=36")}" alt="{esc(latest.get("login") or "")}" '
                  'loading="lazy">' if latest.get("avatar_url") else "")
        edits.attr("latestCommit", "class", "latest-commit")
        edits.html("latestCommit", f'<div class="commit-msg">{commit_message(latest)}</div><div class="commit-meta">'
                                   f'{avatar}<span class="sha">{esc(latest.get("short_sha") or "")}</span>{byline(latest)}</div>')
    else:
        edits.attr("latestCommit", "class", "latest-commit empty")
        edits.text("latestCommit", "No commits found.")
    earlier = (data.get("commits") or [])[1:]
    edits.attr("commitList", "class", "commit-list" if earlier else "commit-list empty")
    edits.html("commitList", "".join(
        f'<li><span class="sha">{esc(commit.get("short_sha") or "")}</span><div class="body"><div>{commit_message(commit)}'
        f'</div><div class="commit-meta">{byline(commit)}</div></div></li>' for commit in earlier
    ) if earlier else "<li>No earlier commits.</li>")

    tree = data.get("tree") or {}
    if tree.get("tree"):
        def blob_url(path: str) -> str:
            return f"https://github.com/{repo['owner']}/{repo['name']}/blob/{repo['ref']}/{path}"

        edits.attr("fileTree", "class", "file-tree")
        edits.html("fileTree", tree_rows(build_tree(tree["tree"]), 0, blob_url, [TREE_ROWS]))
        edits.text("treeMeta", f"{fmt_int(tree.get('count'))} entries{' (truncated)' if tree.get('truncated') else ''}")
    else:
        edits.attr("fileTree", "class", "file-tree empty")
        edits.text("fileTree", "File tree unavailable.")
        edits.text("treeMeta", "")
    if data.get("errors"):
        edits.text("treeMeta", data["errors"][0])


# ---------------------------------------------------------------- evolution

# app.js EVOLUTION_SERIES and EVOLUTION_DEFAULT_ON. A reader's own choice of series, scale
# and range is kept in their browser: the page shows the default, every point (All).
EVOLUTION_SERIES = [
    ("Translation units", "tu_done", "done", "#6fcf57"),
    ("Translation units", "tu_compiled", "compiled", "#edbd54"),
    ("Translation units", "tu_in_progress", "in progress", "#57a8e0"),
    ("Translation units", "tu_blocked", "blocked", "#e72530"),
    ("Translation units", "tu_external", "external", "#b294df"),
    ("Translation units", "tu_todo", "todo", "#8a7f72"),
    ("Translation units", "tu_total", "tracked", "#d9cfc0"),
    ("Functions", "funcs_done", "covered", "#f2b53f"),
    ("Functions", "funcs_external", "external", "#b294df"),
    ("Functions", "funcs_named_uncovered", "named, uncovered", "#a08e6c"),
    ("Functions", "funcs_unidentified", "unidentified", "#6a6159"),
    ("Functions", "funcs_total", "in the binary", "#efe6d2"),
    ("Executable", "tu_linked", "linked", "#57a8e0"),
    ("Executable", "tu_available", "available in build", "#d9cfc0"),
    ("Executable", "tu_source_linked", "linked source", "#57a8e0"),
    ("Executable", "tu_external_build", "external provider", "#b294df"),
    ("Byte matching (X360)", "bm_exact", "exact functions", "#6fcf57"),
    ("Byte matching (X360)", "bm_nonmatching", "compiled, not matching", "#d7742a"),
    ("Byte matching (X360)", "bm_exact_bytes", "exact code bytes", "#3ec9b0"),
]
EVOLUTION_DEFAULT_ON = ("tu_done", "funcs_done", "tu_linked", "bm_exact", "bm_exact_bytes")
# The page opens on Shares: each series over its own total (the `of` of app.js). The two
# totals themselves have none, so they are counts only and their chips are disabled.
EVOLUTION_TOTALS = {
    "tu_total": None, "funcs_total": None,
    "bm_exact": "bm_functions", "bm_nonmatching": "bm_functions", "bm_exact_bytes": "bm_bytes",
}


def _evolution_total(key: str) -> str | None:
    if key in EVOLUTION_TOTALS:
        return EVOLUTION_TOTALS[key]
    return "funcs_total" if key.startswith("funcs_") else "tu_total"


def render_evolution(edits: Edits, history: list[dict]) -> None:
    """app.js renderEvolution() without the chart's lines, which it draws to the window."""
    groups: dict[str, list[str]] = {}
    for group, key, label, color in EVOLUTION_SERIES:
        counts_only = _evolution_total(key) is None
        disabled = ' disabled title="A count with no natural total: shown in Counts only."' if counts_only else ""
        on = " on" if key in EVOLUTION_DEFAULT_ON and not counts_only else ""
        groups.setdefault(group, []).append(
            f'<button type="button" data-key="{key}" class="evo-chip{on}"{disabled}>'
            f'<span class="sw" style="background: {color};"></span>{esc(label)}</button>')
    edits.html("evolutionPicker", "".join(
        f'<div class="evo-group"><span class="evo-group-name">{esc(group)}</span>{"".join(chips)}</div>'
        for group, chips in groups.items()))
    points = [point for point in history if parse_time(point.get("ts"))]
    if points:
        days = js_round((parse_time(points[-1]["ts"]) - parse_time(points[0]["ts"])).total_seconds() / 86400) + 1
        edits.text("evolutionCount", f"{fmt_int(days)} days · {points[0].get('date')} → {points[-1].get('date')}")
    if len(points) < 2:
        edits.html("evolutionChart", '<div class="muted-text vh-empty">' + (
            "Not enough points in this range yet." if history
            else "No history yet: the first snapshot is taken at the next workflow import.") + "</div>")
        return
    items = []
    for _group, key, label, color in EVOLUTION_SERIES:
        if key not in EVOLUTION_DEFAULT_ON:
            continue
        total = _evolution_total(key)
        column = []
        for point in points:
            if point.get(key) is None or total is None:
                continue
            whole = num(point.get(total))
            if whole > 0:   # app.js evolutionValue(): a share of its own total
                column.append(num(point[key]) / whole * 100)
        shown = f"{fixed(column[0])}% → {fixed(column[-1])}%" if column else "–"
        items.append(f'<span class="evo-legend-item"><span class="sw" style="background: {color};"></span>{esc(label)} '
                     f'<span class="evo-legend-values">{shown}</span></span>')
    edits.html("evolutionLegend", "".join(items))


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


def repository(github: dict[str, Any] | None) -> dict[str, str]:
    """The decomp repository the page links into (app.js state.repo)."""
    repo = (github or {}).get("repo") or {}
    return {"owner": repo.get("owner") or "BurnoutDecomp", "name": repo.get("name") or "b5-decomp",
            "ref": repo.get("ref") or "dev"}


def fill_page(source: str, data: dict[str, Any], now: datetime) -> str:
    """index.html with everything in `data` written in: the dashboard state ("state"), the
    GitHub overview ("github") and the page's reads (read_page). A read that is missing or
    None leaves its part of the page as index.html has it."""
    edits = Edits(source)
    state, summary, github = data.get("state"), data.get("bytematch"), data.get("github")
    repo = repository(github)
    measured = bool(summary and summary.get("available"))
    audit = (state or {}).get("audit") or {}
    if state:
        render(edits, state, now)
        render_evidence_summary(edits, audit, now)
    if data.get("builds"):
        render_download(edits, data["builds"], now)
    if measured:
        render_bytematch(edits, summary, now)
    if summary is not None and data.get("bytematch_functions") is not None:
        render_bytematch_list(edits, data["bytematch_functions"], measured, repo, now)
    if data.get("gap_files") is not None:
        render_evidence_list(edits, data["gap_files"], bool((audit.get("funcaudit") or {}).get("paired")))
    if data.get("gap_top") is not None:
        render_evidence_top(edits, data["gap_top"])
    if data.get("history") is not None:
        render_evolution(edits, data["history"])
    if github:
        render_github(edits, github, repo, now)
    page = edits.render()
    description = describe(state, summary)
    if description:
        page = page.replace(f'content="{STATIC_DESCRIPTION}"', f'content="{esc(description)}"')
    return page


# app.js reloads these once a minute or less often; the page's copy is kept as long.
PAGE_READS_TTL = 60.0
# The GitHub overview is refreshed once it is this old (the commits' own TTL), waited for
# PAGE_GITHUB_WAIT at most: a slow GitHub leaves the page with the previous overview.
PAGE_GITHUB_FRESH = 180.0
PAGE_GITHUB_WAIT = 1.5


def read_page(store: Any) -> dict[str, Any]:
    """What the page shows besides the dashboard state, read as app.js asks for it on load.
    A read that fails is left out, and its part of the page with it."""
    from bp_work_server.routes.downloads import list_builds  # the /api/builds reply itself

    reads: dict[str, Any] = {}
    for name, read in (
        ("bytematch", store.bytematch_summary),
        ("builds", lambda: list_builds(store=store).model_dump(mode="json")),
        ("bytematch_functions", lambda: store.bytematch_functions(sort="size", order="desc", limit=LIST_ROWS, offset=0)),
        ("gap_files", lambda: store.audit_files(gaps_only=True, sort="weight", order="desc", limit=LIST_ROWS, offset=0)),
        ("gap_top", lambda: store.audit_top("MISSING_CASE", 12)),
        ("history", lambda: store.history_points(None)["points"]),
    ):
        try:
            reads[name] = read()
        except Exception:
            log.exception("the page's %s read failed", name)
    return reads


async def page_reads(app: Any) -> dict[str, Any]:
    cache = app.state.page_reads_cache
    if cache["data"] is not None and time.monotonic() < cache["expires_at"]:
        return cache["data"]
    data = await asyncio.to_thread(read_page, app.state.store)
    cache.update(data=data, expires_at=time.monotonic() + PAGE_READS_TTL)
    return data


def _report_github(done: asyncio.Future) -> None:
    if not done.cancelled() and done.exception() is not None:
        log.error("GitHub overview for the page failed", exc_info=done.exception())


async def github_for_page(app: Any, wait: float = PAGE_GITHUB_WAIT) -> dict[str, Any] | None:
    """The GitHub overview the page is sent with: the last overview() (one at startup, then
    each browser's /github/overview), refreshed once it is PAGE_GITHUB_FRESH old. The page
    never makes the first GitHub request itself, so GitHub never holds it up."""
    github = app.state.github
    last = getattr(github, "last_overview", None)
    if last is None:
        return None
    if time.time() - num(last.get("fetched_at")) < PAGE_GITHUB_FRESH:
        return last
    task = getattr(app.state, "page_github_refresh", None)
    if task is None or task.done():
        task = app.state.page_github_refresh = asyncio.ensure_future(github.overview())
        task.add_done_callback(_report_github)
    try:
        return await asyncio.wait_for(asyncio.shield(task), wait)
    except Exception:  # a timeout too: the refresh still lands, for the next reader
        return last


async def dashboard_page(app: Any, source: str) -> str:
    """The page to send: index.html with everything written in, or as it is if that fails."""
    try:
        state, reads, github = await asyncio.gather(dashboard_state_for_page(app), page_reads(app), github_for_page(app))
        return fill_page(source, {**reads, "state": state, "github": github}, datetime.now(timezone.utc))
    except Exception:
        log.exception("the dashboard page went out without its figures")
        return source
