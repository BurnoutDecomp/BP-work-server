from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bp_work_server.api import create_app
from bp_work_server.routes.static import THEME_COLOR_META, static_dir
from bp_work_server.services import dashboard as dashboard_service
from bp_work_server.services import page as page_service
from bp_work_server.services.page import (
    DOWNLOAD_DESIGN, ICONS, Edits, collation_key, download_data, download_view, fill_page, fixed, fmt_compact, pager,
    rel_time, sign_game, template_ids,
)
from bp_work_server.store import WorkStore, iso

NOW = datetime(2026, 11, 22, 15, 0, tzinfo=timezone.utc)
STATIC = Path(str(static_dir()))

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
    "areas": [{"area": "GameSource/Gui", "bytes": 896804, "exact": 686, "exact_bytes": 88168, "functions": 2383,
               "fuzzy_bytes": 410410.9, "missing": 30, "nonmatching": 1667}],
    "recent": [{"artist": "8268D7B8", "name": "CgsSound::<Prepare>", "size": 20, "at": "2026-11-22T14:14:00Z",
                "commit": "dd72274ead61af76"}],
    "history": [
        {"commit": "aaa", "imported_at": "2026-11-01T10:00:00+00:00", "functions": 23600, "exact": 1761,
         "nonmatching": 16889, "missing": 5758},
        {"commit": "bbb", "imported_at": "2026-11-22T14:30:00+00:00", "functions": 23700, "exact": 5775,
         "nonmatching": 13791, "missing": 4134},
    ],
}

BUILDS = {
    "latest": {"id": 180, "commit_short": "12a1d5500e0b", "built_at": "2026-11-22T12:27:42.101229Z", "downloads": 3,
               "size_bytes": 5037977319, "download_url": "/download/180", "update_url": "/download/180/update",
               "bundle_size": 10024449, "assets_changed": False},
    "builds": [
        {"id": 180, "commit_short": "12a1d5500e0b", "built_at": "2026-11-22T12:27:42Z"},
        {"id": 179, "commit_short": "3add8adc5a37", "built_at": "2026-11-21T11:00:00Z"},
    ],
}

GITHUB = {
    "repo": {"owner": "BurnoutDecomp", "name": "b5-decomp", "ref": "dev"},
    "info": {"full_name": "BurnoutDecomp/b5-decomp", "description": "Burnout Paradise decompilation effort",
             "html_url": "https://github.com/BurnoutDecomp/b5-decomp", "stargazers_count": 26, "forks_count": 1,
             "open_issues_count": 11, "watchers_count": 4, "language": "C++", "pushed_at": "2026-11-22T13:00:00Z"},
    "latest_commit": {"short_sha": "d211923", "message": "ICE editor <HUD>", "author": "Adriwin", "login": "Adriwin06",
                      "avatar_url": "https://avatars.githubusercontent.com/u/1?v=4", "date": "2026-11-20T15:00:00Z",
                      "html_url": "https://github.com/BurnoutDecomp/b5-decomp/commit/d211923"},
    "commits": [{"short_sha": "d211923"}, {"short_sha": "e6e283f", "message": "Earlier", "author": "macadelic",
                                           "date": "2026-11-09T15:00:00Z"}],
    "tree": {"count": 6, "truncated": False, "tree": [
        {"path": "src", "type": "tree"}, {"path": "src/Game", "type": "tree"}, {"path": "src/Game/a.cpp", "type": "blob", "size": 10},
        {"path": "README.md", "type": "blob", "size": 2048}, {"path": "docs", "type": "tree"},
        {"path": "docs/GUIDE.md", "type": "blob", "size": 600},
    ]},
    "rate_limit": {"remaining": 39, "limit": 60, "authenticated": False},
    "errors": [],
    "fetched_at": 0,
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
    # the store's empty lists, as app.js words them: nothing published, measured or audited
    assert '<div id="downloadSlot" class="wp-download" hidden></div>' in text
    assert "Nothing measured yet." in text and "No audit has been imported yet." in text
    assert '<button type="button" data-key="tu_done" class="evo-chip on">' in text
    # the repository panel waits for a GitHub overview, which the page never asks for first
    assert '<p id="repoDesc" class="gh-desc">Loading repository…</p>' in text
    # every element app.js looks up is still there, once
    assert set(template_ids(text)) == set(template_ids(template()))


def test_text_from_the_database_is_escaped(tmp_path):
    page = make_client(tmp_path).get("/").text

    assert "GameSource/&lt;img src=x&gt;.cpp" in page
    assert "<img src=x>" not in page


def test_without_figures_the_page_is_the_template(tmp_path, monkeypatch):
    async def nothing(_app):
        return None

    async def no_reads(_app):
        return {}

    monkeypatch.setattr(page_service, "dashboard_state_for_page", nothing)
    monkeypatch.setattr(page_service, "page_reads", no_reads)
    monkeypatch.setattr(page_service, "github_for_page", nothing)

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


def test_the_github_overview_is_reused_and_a_slow_refresh_waited_for_briefly(tmp_path, monkeypatch):
    app = make_client(tmp_path).app
    github = app.state.github
    calls = []

    async def slow_overview():
        calls.append(1)
        await asyncio.sleep(5)

    monkeypatch.setattr(github, "overview", slow_overview)

    assert asyncio.run(page_service.github_for_page(app)) is None  # never the first request
    github.last_overview = {**GITHUB, "fetched_at": time.time()}
    assert asyncio.run(page_service.github_for_page(app)) is github.last_overview and not calls
    github.last_overview = {**GITHUB, "fetched_at": time.time() - 600}
    assert asyncio.run(page_service.github_for_page(app, wait=0.05)) is github.last_overview and calls


def test_byte_matching_is_written_in():
    page = fill_page(template(), {"bytematch": BYTEMATCH}, NOW)

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
    # the history's legend under a placeholder as tall as the chart app.js draws
    assert '<svg viewBox="0 0 320 96" style="height: 96px" aria-hidden="true"></svg>' in page
    assert "compiled bodies 18,650 → 19,566</span>" in page
    # areas, folded to 8 on a phone, and the state filters with their counts
    assert '<div id="bytematchAreas" class="bm-area-list" data-fold="8">' in page
    assert '<span class="bm-area-nums">686 / 2,383 · 9.8%</span>' in page
    assert '<i class="sw sw-amber" aria-hidden="true"></i>compiled, not matching<span class="">13,793</span>' in page
    # the licence card's meter, and a description with the byte match alone
    assert 'aria-label="X360 byte match" aria-valuenow="33.72"' in page
    assert 'X360 byte match</span> <span class="b5-meter__value" data-b5-progress-label>33.7%</span>' in page
    assert "33.7% of the Xbox 360 functions byte-matching the original." in page


def test_the_byte_match_list_is_written_in():
    functions = {"total": 34244, "items": [
        {"artist": "823A0A18", "name": "ProcessGameEvents", "size": 17744, "status": "nonmatching", "score": 91.26,
         "source": "src/GameSource/GameState/A.cpp", "stale": 1, "tu": "GameSource/A.cpp", "first_exact": None},
    ]}

    page = fill_page(template(), {"bytematch": BYTEMATCH, "bytematch_functions": functions}, NOW)

    assert ('<div class="evidence-file-meta">ARTIST 823A0A18 · 17,744 bytes · <a href="https://github.com/BurnoutDecomp/'
            'b5-decomp/blob/x360/src/GameSource/GameState/A.cpp" target="_blank" rel="noopener">') in page
    assert '<span class="b5-ribbon" data-tone="warning">compiled, not matching</span>' in page
    assert '<span class="b5-ribbon" title="Fuzzy match: how alike its instructions are to the original&#x27;s">91.3%</span>' in page
    assert '<span class="b5-ribbon" data-tone="info" title="Its source file' in page
    assert '<span id="bytematchRange" class="muted-text">1–10 of 34,244 functions</span>' in page
    assert '<span id="bmPageStatus" class="page-status">Page 1 of 3,425</span>' in page
    assert '<button id="bmPagePrev" class="b5-page-button" type="button" disabled>' in page


def test_the_latest_build_is_the_sign_app_js_draws():
    page = fill_page(template(), {"builds": BUILDS}, NOW)

    assert '<div id="downloadSlot" class="wp-download" data-download-design="sign-game">' in page
    assert 'id="download-group" data-dd-chooser data-phase="closed">' in page
    assert '<a class="dd-choice dd-choice--full dd-lane" href="/download/180">' in page
    assert '<a class="dd-choice dd-choice--update dd-lane" href="/download/180/update">' in page
    assert "<span>commit 12a1d5500e0b · <span data-dl=\"age\">2h ago</span></span>" in page
    assert "Game assets unchanged since build 3add8adc5a37 (1d ago)" in page
    assert '<span class="sv-supp" aria-hidden="true">Build 180 · 4.69 GB <i>▾</i></span>' in page


def test_the_download_notes_follow_app_js():
    latest = BUILDS["latest"]

    assert download_view({**latest, "update_url": None}, [], NOW)["note"] == {
        "text": "This build predates the exe-only update; the next published build will offer one."}
    assert download_view({**latest, "assets_changed": True}, [], NOW)["note"]["label"] == "Assets changed"
    assert download_view(latest, [latest], NOW)["note"] == {"text": "First published build: take the full game."}


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js to run the UI library")
def test_the_sign_is_the_ui_library_s_own_markup(tmp_path):
    view = download_view(BUILDS["latest"], BUILDS["builds"], NOW)
    module = (STATIC / "b5-ui" / "download-designs.js").as_uri()
    (tmp_path / "view.json").write_text(json.dumps(view), encoding="utf-8")
    script = tmp_path / "sign.mjs"
    script.write_text(
        f"import {{ readFileSync }} from 'node:fs';\n"
        f"import {{ downloadDesigns, downloadData }} from {json.dumps(module)};\n"
        f"const design = downloadDesigns.find((item) => item.id === {json.dumps(DOWNLOAD_DESIGN)});\n"
        "const view = JSON.parse(readFileSync(process.argv[2], 'utf8'));\n"
        "process.stdout.write(design.markup('download', downloadData(view)));\n",
        encoding="utf-8",
    )
    drawn = subprocess.run(["node", str(script), str(tmp_path / "view.json")], capture_output=True, encoding="utf-8",
                           check=True).stdout
    icons = (STATIC / "b5-ui" / "assets" / "icons.svg").as_uri()

    assert drawn.replace(icons, ICONS) == sign_game("download", download_data(view))


def test_the_page_draws_the_design_app_js_draws():
    app_js = STATIC.joinpath("app.js").read_text(encoding="utf-8")

    assert re.search(r'const DOWNLOAD_DESIGN = "([^"]+)"', app_js).group(1) == DOWNLOAD_DESIGN


def test_crawlers_are_kept_off_the_downloads(tmp_path):
    robots = make_client(tmp_path).get("/robots.txt")

    assert robots.status_code == 200 and robots.headers["content-type"].startswith("text/plain")
    assert "User-agent: *\nDisallow: /download/\n" in robots.text


def test_the_console_gaps_are_written_in():
    state = {"audit": {"funcaudit": {"commit": "d21192332d58f729", "generated_at": "2026-11-22T09:00:00Z",
                                     "paired": 9000, "categories": {"MISSING_CASE": {"functions": 354},
                                                                    "NO_BODY": {"functions": 268}}}}}
    files = {"total": 1401, "items": [{"file": "GameSource/<B>.cpp", "functions": 43, "weight": 186, "no_body": 2,
                                       "missing_case": 3, "missing_event": 1, "missing_callee": 137, "missing_assert": 43}]}
    top = [{"name": "ProcessGameEvents", "file": "GameSource/A.cpp", "ids": 134, "count": 1, "preview": "0, 3, 8"}]

    page = fill_page(template(), {"state": state, "gap_files": files, "gap_top": top}, NOW)

    assert '<span class="b5-ribbon b5-ribbon--gold" id="evidenceMeta" title="audited 2026-11-22 09:00:00 UTC">' \
           "b5-decomp d21192332d · 6h ago</span>" in page
    assert ('<button type="button" class="b5-filter" aria-pressed="true">all files</button><button type="button" '
            'class="b5-filter" aria-pressed="false"><i class="sw sw-crimson" aria-hidden="true"></i>no body'
            '<span class="">268</span></button>') in page
    assert "missing callees" not in page.split('id="evidenceChips"')[1].split("</div>")[0]  # no count, no filter
    assert '<div class="evidence-file-path">GameSource/&lt;B&gt;.cpp</div>' in page
    assert ('<span class="b5-ribbon" data-tone="danger">186 gaps</span><span class="b5-ribbon" data-tone="danger">'
            '2 no body</span><span class="b5-ribbon" data-tone="warning">3 switch gaps</span>') in page
    assert '<span class="b5-pagination__range" role="status">1–10 of 1,401 files</span>' in page
    assert '<ol id="evidenceTop" class="evidence-top" data-fold="5"><li><span class="top-n">134</span>' in page
    assert "GameSource/A.cpp — 0, 3, 8" in page


def test_the_pager_is_the_libraries():
    drawn = pager(1401, 10, "files")

    assert drawn.startswith('<span class="b5-pagination__range" role="status">1–10 of 1,401 files</span>')
    assert ('<button type="button" class="b5-page-button" data-b5-page="0" data-b5-page-key="previous" disabled>'
            "Previous</button>") in drawn
    assert ('data-b5-page="1" data-b5-page-key="number" aria-label="Page 1" aria-current="page">1</button>'
            '<button type="button" class="b5-page-button" data-b5-page="2" data-b5-page-key="number" aria-label="Page 2">'
            '2</button><span class="b5-pagination__gap" aria-hidden="true">…</span>') in drawn
    assert 'max="141" value="1"' in drawn
    assert pager(0, 10, "files").startswith('<span class="b5-pagination__range" role="status">No files</span>')


def test_the_repository_is_written_in():
    page = fill_page(template(), {"github": GITHUB}, NOW)

    assert '<a id="repoLink" href="https://github.com/BurnoutDecomp/b5-decomp" target="_blank" rel="noopener">' \
           "BurnoutDecomp/b5-decomp</a>" in page
    assert '<p id="repoDesc" class="gh-desc">Burnout Paradise decompilation effort</p>' in page
    assert '<span id="ghRate" class="gh-rate">API 39/60 (anon)</span>' in page
    assert '<div class="gh-stat">★ <strong>26</strong> stars</div>' in page
    assert '<div class="commit-msg"><a href="https://github.com/BurnoutDecomp/b5-decomp/commit/d211923" ' \
           'target="_blank" rel="noopener">ICE editor &lt;HUD&gt;</a></div>' in page
    assert '<img class="avatar" src="https://avatars.githubusercontent.com/u/1?v=4&amp;s=36" alt="Adriwin06" loading="lazy">' in page
    assert '<li><span class="sha">e6e283f</span><div class="body"><div>Earlier</div>' in page
    # folders first; the top level open, the next one closed and empty until app.js
    tree = page.split('id="fileTree"')[1]
    assert tree.index(">docs<") < tree.index(">src<") < tree.index(">README.md<")
    assert ('<div class="tree-node dir" style="padding-left: 22px;"><span class="twist">▶</span><span class="icon">📁'
            '</span><span class="name">Game</span></div><div class="tree-children collapsed"></div>') in tree
    assert 'href="https://github.com/BurnoutDecomp/b5-decomp/blob/dev/docs/GUIDE.md"' in tree
    assert '<span id="treeMeta" class="tree-meta">6 entries</span>' in page


def test_the_tree_sends_its_top_level_and_the_first_rows_under_it():
    entries = [{"path": "tests", "type": "tree"}] + [
        {"path": f"tests/case{n:03}.json", "type": "blob", "size": 1} for n in range(300)] + [
        {"path": "README.md", "type": "blob", "size": 1}]

    page = fill_page(template(), {"github": {**GITHUB, "tree": {"count": 302, "tree": entries}}}, NOW)

    tree = page.split('id="fileTree"')[1].split('id="treeMeta"')[0]
    assert tree.count('class="tree-node"') == page_service.TREE_ROWS + 1  # 40 under tests/, and README.md
    assert ">README.md<" in tree


def test_the_evolution_panel_is_written_in():
    history = [
        {"date": "2026-06-11", "ts": "2026-06-11T21:53:05+00:00", "tu_done": 5, "funcs_done": 5, "tu_linked": None},
        {"date": "2026-11-22", "ts": "2026-11-22T15:43:29+00:00", "tu_done": 4244, "funcs_done": 22628, "tu_linked": 3346},
    ]

    page = fill_page(template(), {"history": history}, NOW)

    assert '<div class="metric-total" id="evolutionCount">165 days · 2026-06-11 → 2026-11-22</div>' in page
    assert ('<div class="evo-group"><span class="evo-group-name">Translation units</span><button type="button" '
            'data-key="tu_done" class="evo-chip on"><span class="sw" style="background: #6fcf57;"></span>done</button>'
            '<button type="button" data-key="tu_compiled" class="evo-chip">') in page
    assert 'done <span class="evo-legend-values">5 → 4,244</span>' in page
    assert 'linked <span class="evo-legend-values">3,346 → 3,346</span>' in page
    assert "No history yet" in fill_page(template(), {"history": []}, NOW)


def test_the_file_tree_sorts_as_the_browser_does():
    names = ["README.md", "cmake", ".gitignore", "CMakeLists.txt", "a10", "a2", "_x", "Docs", "docs"]

    assert sorted(names, key=collation_key) == [
        "_x", ".gitignore", "a10", "a2", "cmake", "CMakeLists.txt", "docs", "Docs", "README.md"]


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
