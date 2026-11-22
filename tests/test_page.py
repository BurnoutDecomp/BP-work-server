from __future__ import annotations

import asyncio
import re
import threading
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from bp_work_server.api import create_app
from bp_work_server.routes.static import THEME_COLOR_META, static_dir
from bp_work_server.services import dashboard as dashboard_service
from bp_work_server.services import page as page_service
from bp_work_server.services.page import Edits, fill_page, fixed, fmt_compact, rel_time, template_ids
from bp_work_server.store import WorkStore, iso

NOW = datetime(2026, 11, 22, 15, 0, tzinfo=timezone.utc)

BYTEMATCH = {
    "available": True,
    "meta": {"b5_commit": "dd72274ead61af76", "generated_at": "2026-11-22T14:30:44Z", "toolchain": "cl 14.00.6502",
             "verified_commit": "dd72274ead61af76", "verified_at": "2026-11-22T13:30:00Z", "behind": 0},
    "totals": {
        "all": {"bytes": 11166660, "functions": 31503, "matched": 10624, "matched_bytes": 3087564},
        "game": {"bytes": 8106804, "exact": 5752, "exact_bytes": 786876, "functions": 23683},
        "sdk": {"bytes": 2300688, "functions": 4872},
        "objects": {"compiled": 3710, "sources": 3710, "stale": 0},
    },
    "status_counts": {
        "exact": {"functions": 5752, "bytes": 786876}, "sdk": {"functions": 4872, "bytes": 2300688},
        "nonmatching": {"functions": 13793, "bytes": 6322204}, "missing": {"functions": 4138, "bytes": 997724},
        "unnamed": {"functions": 2948, "bytes": 760720},
    },
    "percent": {"matched_functions": 33.72, "exact_functions": 24.29, "exact_bytes": 9.71, "fuzzy_bytes": 35.6,
                "image_bytes": 27.65},
    "areas": [],
    "recent": [{"artist": "8268D7B8", "name": "CgsSound::<Prepare>", "size": 20, "at": "2026-11-22T14:14:00Z",
                "commit": "dd72274ead61af76"}],
    "history": [],
}


@pytest.fixture(autouse=True)
def _no_token(monkeypatch):
    monkeypatch.setenv("BP_WORK_REQUIRE_TOKEN", "0")


def template() -> str:
    return static_dir().joinpath("index.html").read_text(encoding="utf-8")


def make_client(tmp_path) -> TestClient:
    store = WorkStore(tmp_path / "page.sqlite3")
    store.migrate()
    with store.connect() as con:
        con.execute(
            """
            INSERT INTO tu(id, source, status, n_funcs, n_decfigs, dest_path, updated_at)
            VALUES
              ('GameSource/A.cpp', 'decfigs', 'done', 1, 1, 'b5-decomp/src/GameSource/A.cpp', ?),
              ('GameSource/<img src=x>.cpp', 'decfigs', 'todo', 1, 1, 'b5-decomp/src/GameSource/B.cpp', ?)
            """,
            (iso(), iso()),
        )
        con.execute("INSERT INTO func(name, tu_id, status) VALUES('A::Run', 'GameSource/A.cpp', 'reviewed')")
        con.execute("INSERT INTO func(name, tu_id) VALUES('B::Run', 'GameSource/<img src=x>.cpp')")
    return TestClient(create_app(store))


def test_the_page_is_sent_with_the_figures_in_it(tmp_path):
    client = make_client(tmp_path)

    page = client.get("/")

    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-cache" and page.headers["vary"] == "User-Agent"
    text = page.text
    assert '<span id="subtitle" class="b5-hint">2 translation units · 2 functions</span>' in text
    assert '<strong id="tuPercent">50.0%</strong>' in text
    assert '<div class="metric-total" id="tuCount">1 / 2 complete</div>' in text
    assert '<span class="lg-label">done</span> <span class="lg-n">1</span> <span class="lg-pct">50.0%</span>' in text
    assert 'class="ring-seg seg-green lead" data-index="0"' in text
    assert '<span id="connectionText">Live</span>' in text
    assert '<span class="b5-license-chip__value" id="licenseChipValue">50.0%</span>' in text
    # the queue's first page, not "No available TUs."
    assert '<div id="nextQueue" class="queue"><div class="queue-row clickable">' in text
    assert "No available TUs." not in text
    assert 'name="description"\n      content="Live progress dashboard for the Burnout Paradise decompilation project: ' \
           '50.0% of translation units complete, 50.0% of functions covered."' in text
    # every element app.js looks up is still there, once
    assert set(template_ids(text)) == set(template_ids(template()))


def test_text_from_the_database_is_escaped(tmp_path):
    page = make_client(tmp_path).get("/").text

    assert "GameSource/&lt;img src=x&gt;.cpp" in page
    assert "<img src=x>" not in page


def test_without_figures_the_page_is_the_template(tmp_path, monkeypatch):
    async def nothing(_app):
        return None

    monkeypatch.setattr(page_service, "dashboard_state_for_page", nothing)
    monkeypatch.setattr(page_service, "bytematch_summary_for_page", nothing)

    page = make_client(tmp_path).get("/").text

    assert page == template().replace(THEME_COLOR_META, "")


def test_a_failure_to_fill_the_page_sends_the_template(tmp_path, monkeypatch):
    def broken(*_args):
        raise ValueError("boom")

    monkeypatch.setattr(page_service, "fill_page", broken)

    page = make_client(tmp_path).get("/").text

    assert page == template().replace(THEME_COLOR_META, "")


def test_a_slow_rebuild_leaves_the_page_with_the_last_state(tmp_path, monkeypatch):
    app = make_client(tmp_path).app
    app.state.dashboard_cache.update(data=None, expires_at=0.0, last={"totals": {"tus": 7}})
    release = threading.Event()

    def slow_build(*_args):
        release.wait(5)
        return {"totals": {"tus": 9}}

    monkeypatch.setattr(dashboard_service, "build_dashboard_state", slow_build)

    async def read():
        try:
            return await dashboard_service.dashboard_state_for_page(app, wait=0.05)
        finally:
            release.set()

    assert asyncio.run(read()) == {"totals": {"tus": 7}}


def test_byte_matching_is_written_in():
    page = fill_page(template(), None, BYTEMATCH, NOW)

    assert '<strong id="bytematchPercent">33.7%</strong>' in page
    assert '<div class="metric-total" id="bytematchCount">10,624 / 31,503 functions byte-match ARTIST</div>' in page
    assert re.search(r'<span id="bytematchStamp" class="b5-ribbon b5-ribbon--gold" title="summary built '
                     r'2026-11-22 14:30:44 UTC · cl 14.00.6502">x360 dd72274ead · 29m ago</span>', page)
    # 27.65 is 27.6499… as a double, which toFixed(1) also gives as 27.6
    assert '<span class="b5-meter__label">Code that matches</span> <span class="b5-meter__value">27.6%</span>' in page
    assert "2.9 MB of the game&#x27;s 10.6 MB of code." in page
    assert "all 3,710 native sources compile</span> · <span" in page
    assert "every record proven at x360 dd72274, 1h ago" in page
    assert "CgsSound::&lt;Prepare&gt;" in page
    # the licence card's meter, and a description with the byte match alone
    assert 'aria-label="X360 byte match" aria-valuenow="33.72"' in page
    assert 'X360 byte match</span> <span class="b5-meter__value" data-b5-progress-label>33.7%</span>' in page
    assert "33.7% of the Xbox 360 functions byte-matching the original." in page


def test_edits_change_contents_and_attributes_by_id():
    source = ('<div id="a" class="x"><div id="b">old</div><div>keep</div></div>'
              '<p id="c" hidden>t</p><svg><circle id="d" r="1"/></svg>')
    edits = Edits(source)
    edits.text("b", "<new>")
    edits.attr("a", "class", "y")
    edits.attr("a", "data-z", "1")
    edits.attr("c", "hidden", None)
    edits.attr("d", "r", "2")

    assert edits.render() == ('<div id="a" class="y" data-z="1"><div id="b">&lt;new&gt;</div><div>keep</div></div>'
                              '<p id="c">t</p><svg><circle id="d" r="2"/></svg>')


def test_edits_find_the_end_of_nested_elements_and_refuse_overlaps():
    source = '<div id="outer"><div>inner</div>tail</div><span>after</span>'
    edits = Edits(source)
    edits.text("outer", "x")
    assert edits.render() == '<div id="outer">x</div><span>after</span>'

    nested = Edits('<div id="a"><div id="b">b</div></div>')
    nested.text("a", "x")
    nested.text("b", "y")
    with pytest.raises(ValueError):
        nested.render()


def test_numbers_are_formatted_as_the_browser_formats_them():
    assert fixed(96.25) == "96.3"  # toFixed takes a half up
    assert fixed(0.04) == "0.0"
    assert fmt_compact(950) == "950"
    assert fmt_compact(1150) == "1.2k"
    assert fmt_compact(11168) == "11.2k"
    assert fmt_compact(2_000_000) == "2M"
    assert rel_time("2026-11-22T14:59:30+00:00", NOW) == "just now"
    assert rel_time("2026-11-22T12:00:00Z", NOW) == "3h ago"
    assert rel_time("2026-09-01T00:00:00Z", NOW) == "2026-09-01"
