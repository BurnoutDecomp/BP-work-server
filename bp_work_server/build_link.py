"""Which translation units are actually compiled into the game exe.

``tools/build/build_game_exe.bat`` in the workflow repo is the ground truth for
what ships: its source list exceeds cmd's ~8191-char command-line limit, so the
script ``echo``s every source file into a ``cl`` response file. A TU whose
destination file appears on that list is *in the executable*; everything else is
decompiled but not yet wired into the build.

Parsing the batch script beats parsing build output: the script is committed
(so it syncs with the rest of the workflow snapshot), while the response file
and the exe only exist on a machine that has actually run the build.

Matching is pure text -- no filesystem access -- because the workflow clone on
the server is shallow and does *not* check out the ``b5-decomp`` submodule, so
the sources the script names are not on disk next to it.
"""

from __future__ import annotations

import re
from pathlib import Path

# Path of the build script inside the workflow repo.
BUILD_SCRIPT = "tools/build/build_game_exe.bat"

# `set NAME=value` / `set "NAME=value"`, the two forms the script uses.
_SET_LINE = re.compile(r'^set\s+"?([A-Za-z_]\w*)=([^"]*)"?\s*$', re.IGNORECASE)

# Canonical absolute-path assignment used by the game build:
# `for %%I in ("%~dp0..\..") do set "ROOT=%%~fI"`.
#
# `%~fI` makes the value absolute at batch runtime.  The dashboard only needs
# repo-relative paths, so resolving the expression relative to this script and
# normalising `..` segments gives the same source-list base without depending on
# the server's checkout location.
_FOR_ABSOLUTE_SET_LINE = re.compile(
    r'^for\s+%%([A-Za-z])\s+in\s+\("([^"]+)"\)\s+do\s+set\s+"?'
    r'([A-Za-z_]\w*)=%%~f([A-Za-z])"?\s*$',
    re.IGNORECASE,
)

# One source file appended to the response file: `  echo "%SRC%\path\to\File.cpp"`.
_SOURCE_LINE = re.compile(
    r'^\s*echo\s+"?%(\w+)%\\([^"\r\n]+?\.(?:cpp|cxx|cc|c))"?\s*$',
    re.IGNORECASE,
)

# %~dp0 expands to the directory holding the script, so ROOT=%~dp0..\.. is the
# repo root -- which is what every path here is expressed relative to.
_SCRIPT_DIR = "tools/build/"

_UNRESOLVED = "\x00"


def normalize_path(value: str) -> str:
    """Collapse `\\`, `.` and `..` into a clean forward-slash relative path."""
    parts: list[str] = []
    for seg in value.replace("\\", "/").split("/"):
        if seg in {"", "."}:
            continue
        if seg == "..":
            if parts:
                parts.pop()
        else:
            parts.append(seg)
    return "/".join(parts)


def _script_vars(text: str) -> dict[str, str]:
    """Directory variables (`SRC`, `VEN`, ...) resolved relative to the repo root."""
    resolved: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("rem"):
            continue

        absolute_match = _FOR_ABSOLUTE_SET_LINE.match(stripped)
        if absolute_match:
            loop_var, value, name, expansion_var = absolute_match.groups()
            if loop_var.lower() != expansion_var.lower():
                continue
            value = value.replace("%~dp0", _SCRIPT_DIR)
            value = re.sub(
                r"%(\w+)%",
                lambda m: resolved.get(m.group(1).upper(), _UNRESOLVED),
                value,
            )
            if _UNRESOLVED not in value:
                resolved[name.upper()] = normalize_path(value)
            continue

        match = _SET_LINE.match(stripped)
        if not match:
            continue
        name, value = match.group(1).upper(), match.group(2)
        value = value.replace("%~dp0", _SCRIPT_DIR)
        value = re.sub(
            r"%(\w+)%",
            lambda m: resolved.get(m.group(1).upper(), _UNRESOLVED),
            value,
        )
        # Environment-dependent vars (VCVARS, ERRORLEVEL, ...) never name sources.
        if _UNRESOLVED in value:
            continue
        resolved[name] = normalize_path(value)
    return resolved


def parse_build_sources(workflow_root: str | Path) -> set[str]:
    """Repo-relative paths of every source file the game build compiles.

    Returns an empty set when the build script is missing or unreadable, which
    callers must treat as "unknown" rather than "nothing is linked".
    """
    script = Path(workflow_root) / BUILD_SCRIPT
    try:
        text = script.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return set()

    directories = _script_vars(text)
    sources: set[str] = set()
    for line in text.splitlines():
        if line.strip().lower().startswith("rem"):
            continue
        match = _SOURCE_LINE.match(line)
        if not match:
            continue
        base = directories.get(match.group(1).upper())
        if base is None:
            continue
        sources.add(normalize_path(f"{base}/{match.group(2)}"))
    return sources


def dest_candidates(dest_path: str | None) -> list[str]:
    """Source files a TU's destination could compile into.

    Headers are decompiled inline into their sibling ``.cpp`` (the same fallback
    ``DecompRepo`` uses for attribution), so a ``*.h`` destination is linked when
    that ``.cpp`` is on the build's source list.
    """
    if not dest_path:
        return []
    path = normalize_path(dest_path)
    if not path:
        return []
    candidates = [path]
    for suffix in (".h", ".hpp", ".inl"):
        if path.endswith(suffix):
            candidates.append(path[: -len(suffix)] + ".cpp")
    return candidates


def is_linked(dest_path: str | None, sources: set[str]) -> bool:
    return any(candidate in sources for candidate in dest_candidates(dest_path))
