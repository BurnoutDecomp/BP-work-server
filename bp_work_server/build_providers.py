"""External PC providers confirmed by the current shipping build inputs.

An external reconstruction status alone is not build-inclusion evidence. These
explicit mappings require active link arguments or mounted host backends. They
describe provider availability, not parity of every original console SDK API.
"""
from __future__ import annotations

import re
from pathlib import Path

from bp_work_server.build_link import BUILD_SCRIPT, parse_build_sources


def read_build_providers(workflow_root: str | Path) -> dict[str, dict] | None:
    try:
        text = (Path(workflow_root) / BUILD_SCRIPT).read_text(encoding="utf-8")
    except OSError:
        return None  # no fresh evidence: retain the last imported snapshot
    active = [(n, line.strip()) for n, line in enumerate(text.splitlines(), 1)
              if line.strip() and not re.match(r"(?:rem\b|::|echo\b)", line.strip(), re.I)]
    links = [(n, line) for n, line in active if
             (re.match(r"(?:%PY_CMD%|python3?|py)\s", line, re.I) and
              "compile_exe.py" in line.lower() and re.search(r"\s--\s", line)) or
             re.match(r"cl\s.*\s/link\b", line, re.I)]
    if not links:
        return {}
    libraries = {name.lower() for _, line in links
                 for name in re.findall(r"[\w.-]+\.lib\b", line, re.I)}
    # Use today's response-file list, not a stale /showIncludes inventory, when
    # deciding whether a provider backend has been removed from the build.
    source_spellings = {p.lower(): p for p in parse_build_sources(workflow_root)}
    sources = set(source_spellings)
    result = {}

    def add(targets, name, kind, evidence):
        proof = {"name": name, "kind": kind, "evidence": evidence}
        result.update({tu: proof for tu in targets})

    def link_evidence(required):
        return [{"file": BUILD_SCRIPT, "line": n, "detail": "Link inputs: " + ", ".join(sorted(required))}
                for n, line in links if all(re.search(r"(?<![\w.-])" + re.escape(lib) + r"\b", line, re.I)
                                           for lib in required)][:1]

    def source_evidence(path):
        return {"file": source_spellings.get(path, path), "detail": "Mounted by the shipping build response file"}

    msvc = [(n, line) for n, line in active if re.match(r"call\s.*msvc_env\.bat", line, re.I)]
    if links and msvc:
        add(("vendor:compiler-rt", "vendor:crt"), "MSVC compiler and C/C++ runtime", "toolchain",
            [{"file": BUILD_SCRIPT, "line": msvc[0][0], "detail": "MSVC environment and compile/link driver"}])
    if "lua515.lib" in libraries:
        add(("vendor:lua",), "Lua 5.1.5", "static_library", link_evidence({"lua515.lib"}))

    shim = "b5-decomp/src/pc/gcm/renderengine/xenond3d9shims.cpp"
    if "d3d9.lib" in libraries and shim in sources:
        proof = link_evidence({"d3d9.lib"}) + [source_evidence(shim)]
        add(("vendor:xbox-d3d", "class:D3D"), "Windows Direct3D 9 and PC graphics backend", "platform", proof)
        add(("class:XGRAPHICS::CFG", "class:XGRAPHICS::IRAlu", "class:XGRAPHICS::IRLoadConst"),
            "Windows shader compiler and PC shader backend", "runtime_library", proof +
            [{"file": source_spellings[shim], "detail": "GetD3DCompile loads d3dcompiler_47.dll / d3dcompiler_43.dll"}])

    audio = "b5-decomp/src/gameshared/gameclasses/system/pc/cgsaudiooutputpc.cpp"
    staged_audio = [(n, line) for n, line in active
                    if re.search(r"\bcopy\s.*xaudio2_9redist\.dll", line, re.I)
                    and not re.search(r"\b(?:rem|echo)\b.*\bcopy\s", line, re.I)]
    if audio in sources and staged_audio:
        add(("class:XAUDIO::CEngine", "class:XAUDIO::CRoutedVoice"),
            "Microsoft XAudio2 2.9 and AudioOutputPC", "runtime_library",
            [source_evidence(audio), {"file": BUILD_SCRIPT, "line": staged_audio[0][0],
                                     "detail": "Ships xaudio2_9redist.dll for AudioOutputPC::Open"}])

    dirty = {"b5-decomp/vendor/dirtysdk/src/pc/lan/pclan_core.cpp",
             "b5-decomp/vendor/dirtysdk/src/pc/lan/pclan_link.cpp"}
    if dirty <= sources and "ws2_32.lib" in libraries:
        add(("vendor:ea-dirtysdk",), "Checked-in DirtySDK PC/LAN backend", "vendor_source",
            [source_evidence(p) for p in sorted(dirty)] + link_evidence({"ws2_32.lib"}))
    windows = {"kernel32.lib", "user32.lib", "ntdll.lib", "ws2_32.lib"}
    if windows <= libraries:
        add(("vendor:xbox-xdk",), "Windows host APIs and PC platform layer", "platform", link_evidence(windows))

    # No provider has been attested for CCapture, CReverbEffect or the mixed crypto
    # bucket. A different graphics/audio/crypto library cannot prove those APIs.
    return result


def availability_counts(con) -> dict[str, int]:
    row = con.execute("""
        SELECT SUM(linked=1) AS linked_tus,
               SUM(status='external' AND build_provider IS NOT NULL) AS external_build_tus,
               SUM(linked=1 AND NOT(status='external' AND build_provider IS NOT NULL)) AS source_linked_tus,
               SUM(linked=1 OR (status='external' AND build_provider IS NOT NULL)) AS available_tus
        FROM tu WHERE source IS NULL OR source!='unidentified'
    """).fetchone()
    return {key: row[key] or 0 for key in row.keys()}
