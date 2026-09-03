from __future__ import annotations

import subprocess
import time

import pytest

from bp_work_server.decomp import STALE_LOCK_SECONDS, DecompRepo


def _git(root, *args, env=None):
    subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True, env=env
    )


def _commit(root, author, message, when=None):
    import os

    env = {**os.environ, "GIT_AUTHOR_NAME": author, "GIT_COMMITTER_NAME": author}
    if when:
        env["GIT_AUTHOR_DATE"] = when
        env["GIT_COMMITTER_DATE"] = when
    _git(root, "-c", "commit.gpgsign=false", "commit", "-q", "-m", message, env=env)


@pytest.fixture
def decomp_repo(tmp_path):
    """A tiny git repo mirroring the b5-decomp layout: a committed .cpp, no .h."""
    root = tmp_path / "b5-decomp"
    (root / "src" / "World").mkdir(parents=True)
    foo = root / "src" / "World" / "Foo.cpp"
    foo.write_text("// foo\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "add", ".")
    # Old pre-workflow commit (must be filtered out), then two 2026 commits.
    _commit(root, "OldDev", "ancient decomp", when="2020-01-02T00:00:00")
    foo.write_text("// foo v2\n")
    _git(root, "add", ".")
    _commit(root, "Adriwin06", "Continue decomp", when="2026-06-12T10:00:00")
    foo.write_text("// foo v3\n")
    _git(root, "add", ".")
    _commit(root, "JeBobs", "lots more debug", when="2026-06-16T10:00:00")
    return DecompRepo(root=root, branch="main")


def test_history_is_newest_first_and_year_filtered(decomp_repo):
    history = decomp_repo.history("b5-decomp/src/World/Foo.cpp")
    # The 2020 commit is excluded; the two 2026 commits remain, newest first.
    assert [c["name"] for c in history] == ["JeBobs", "Adriwin06"]
    assert history[0]["date"].startswith("2026-06-16")
    assert history[1]["date"].startswith("2026-06-12")
    assert all(c["email"] for c in history)


def test_resolve_uses_latest_qualifying_commit(decomp_repo):
    path, date = decomp_repo.resolve("b5-decomp/src/World/Foo.cpp")
    assert path == "src/World/Foo.cpp"
    assert date.startswith("2026-06-16")


def test_header_falls_back_to_cpp_sibling(decomp_repo):
    # Headers are inlined into the .cpp, so a .h destination resolves to .cpp.
    assert decomp_repo.repo_path("b5-decomp/src/World/Foo.h") == "src/World/Foo.cpp"
    assert decomp_repo.history("b5-decomp/src/World/Foo.h")  # non-empty


def test_contributors_use_surviving_lines(decomp_repo):
    root = decomp_repo.root
    foo = root / "src" / "World" / "Foo.cpp"
    foo.write_text("int a = 1;\nint b = 2;\n")
    _git(root, "add", ".")
    _commit(root, "Adriwin06", "write base", when="2026-06-17T10:00:00")
    foo.write_text("int a = 1;\nint b = 2;\nint c = 3;\nint d = 4;\nint e = 5;\n")
    _git(root, "add", ".")
    _commit(root, "JeBobs", "append more", when="2026-06-17T11:00:00")

    contributors = decomp_repo.contributors("b5-decomp/src/World/Foo.cpp")["contributors"]

    assert contributors[0]["name"] == "JeBobs"
    assert contributors[0]["lines"] == 3
    assert contributors[1]["name"] == "Adriwin06"
    assert contributors[1]["lines"] == 2


def test_function_contributors_use_parsed_body_range(tmp_path):
    root = tmp_path / "b5-decomp"
    (root / "src" / "World").mkdir(parents=True)
    foo = root / "src" / "World" / "Foo.cpp"
    foo.write_text(
        "void Foo::Other() {\n"
        "  int old_line = 1;\n"
        "}\n\n"
        "void Foo::Run() {\n"
        "  int a = 1;\n"
        "  int b = 2;\n"
        "}\n"
    )
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "add", ".")
    _commit(root, "Adriwin06", "write functions", when="2026-06-17T10:00:00")
    foo.write_text(
        "void Foo::Other() {\n"
        "  int old_line = 1;\n"
        "}\n\n"
        "void Foo::Run() {\n"
        "  int a = 10;\n"
        "  int b = 20;\n"
        "  int c = 3;\n"
        "  int d = 4;\n"
        "}\n"
    )
    _git(root, "add", ".")
    _commit(root, "JeBobs", "expand run", when="2026-06-17T11:00:00")
    repo = DecompRepo(root=root, branch="main")

    result = repo.function_contributors("b5-decomp/src/World/Foo.cpp", "Foo::Run")

    assert result["function_range_found"] is True
    assert result["line_range"] == [5, 10]
    assert result["contributors"][0]["name"] == "JeBobs"
    assert result["contributors"][0]["lines"] == 4


def test_qualified_function_does_not_match_unrelated_short_name(tmp_path):
    root = tmp_path / "b5-decomp"
    (root / "src" / "World").mkdir(parents=True)
    foo = root / "src" / "World" / "Foo.cpp"
    foo.write_text("namespace Other { void Reset() { int x = 1; } }\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "add", ".")
    _commit(root, "Derneuere", "short reset", when="2026-06-17T10:00:00")
    repo = DecompRepo(root=root, branch="main")

    result = repo.function_contributors("b5-decomp/src/World/Foo.cpp", "BrnAI::AICar::Reset")

    assert result["function_range_found"] is False
    assert result["line_range"] is None


def test_missing_file_is_empty(decomp_repo):
    assert decomp_repo.history("b5-decomp/src/World/Missing.cpp") == []
    assert decomp_repo.resolve("b5-decomp/src/World/Missing.cpp") == (None, None)


def test_missing_clone_is_graceful(tmp_path):
    repo = DecompRepo(root=tmp_path / "nope", branch="main")
    assert repo.available is False
    assert repo.history("b5-decomp/src/World/Foo.cpp") == []
    assert repo.resolve("b5-decomp/src/World/Foo.cpp") == (None, None)


def test_git_reads_repository_metadata_as_utf8(monkeypatch, tmp_path):
    received = {}

    class Result:
        returncode = 0
        stdout = "revision\\n"

    def fake_run(*args, **kwargs):
        received.update(kwargs)
        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    repo = DecompRepo(root=tmp_path, branch="main")

    assert repo._git("rev-parse", "HEAD") == "revision\\n"
    assert received["encoding"] == "utf-8"
    assert received["errors"] == "replace"


@pytest.fixture
def cloned_decomp(tmp_path, decomp_repo):
    """A clone of the fixture repo whose origin has advanced one commit ahead."""
    upstream = decomp_repo.root
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(upstream), str(clone)], check=True, capture_output=True
    )
    (upstream / "src" / "World" / "Foo.cpp").write_text("// foo v4\n")
    _git(upstream, "add", ".")
    _commit(upstream, "Adriwin06", "newer work", when="2026-07-01T10:00:00")
    repo = DecompRepo(root=clone, branch="main")
    return repo, clone


def test_refresh_advances_the_worktree(cloned_decomp):
    repo, clone = cloned_decomp
    repo.force_refresh()
    assert (clone / "src" / "World" / "Foo.cpp").read_text() == "// foo v4\n"
    assert repo.health()["behind"] == 0


def test_refresh_clears_a_stale_index_lock(cloned_decomp):
    """A killed git leaves index.lock behind; fetch keeps working, reset never does.

    That pair silently froze production's attribution for 41 days, so the
    refresh has to notice a lock nothing owns and get past it.
    """
    repo, clone = cloned_decomp
    lock = clone / ".git" / "index.lock"
    lock.write_text("")
    import os

    stale = time.time() - (STALE_LOCK_SECONDS + 60)
    os.utime(lock, (stale, stale))

    repo.force_refresh()

    assert not lock.exists()
    assert (clone / "src" / "World" / "Foo.cpp").read_text() == "// foo v4\n"
    assert repo.health()["behind"] == 0


def test_refresh_leaves_a_fresh_index_lock_alone(cloned_decomp):
    """A lock a live git may still own is not ours to delete; report behind instead."""
    repo, clone = cloned_decomp
    lock = clone / ".git" / "index.lock"
    lock.write_text("")

    repo.force_refresh()

    assert lock.exists()
    assert (clone / "src" / "World" / "Foo.cpp").read_text() == "// foo v3\n"
    assert repo.health()["behind"] == 1


def test_function_ranges_sanitize_each_file_once(tmp_path, monkeypatch):
    """Sanitising is a pure-Python character walk; it must not run per function.

    It used to: every one of production's 21,238 function targets re-walked its
    whole file, which is what made a full attribution warm take hours.
    """
    root = tmp_path / "b5-decomp"
    (root / "src").mkdir(parents=True)
    (root / "src" / "Many.cpp").write_text(
        "// header comment\n"
        "void Many::A() { int a = 0; }\n"
        "void Many::B() { int b = 1; }\n"
        "void Many::C() { int c = 2; }\n"
    )
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "add", ".")
    _commit(root, "Adriwin06", "three methods", when="2026-06-12T10:00:00")
    repo = DecompRepo(root=root, branch="main")

    calls = {"n": 0}
    original = DecompRepo._sanitize_cpp

    def counting(text):
        calls["n"] += 1
        return original(text)

    monkeypatch.setattr(DecompRepo, "_sanitize_cpp", staticmethod(counting))

    for name in ("Many::A", "Many::B", "Many::C"):
        assert repo.function_range("b5-decomp/src/Many.cpp", name) is not None

    assert calls["n"] == 1
