"""Archive the fixed core/test/screen/match evidence; exclude private games/UI."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

DEST = Path(__file__).resolve().parent
ROOT = DEST.parents[2]
CORE = "6aa8319f51919cd12ae336c3dd5747c67f540d2d"
NEXT = ROOT / "build-gambit-forcing-next"
MATCH = ROOT / "build-gambit-forcing-next-final/core-match-001"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    archive = DEST / "evidence.zip"
    if archive.exists():
        raise FileExistsError("Preserve the final evidence archive")
    protocol = json.loads((MATCH / "protocol.json").read_text(encoding="utf-8"))
    match = json.loads((MATCH / "match.json").read_text(encoding="utf-8"))
    copies = {"final-screen-summary.json": NEXT / "screen-bounded64k/summary.json",
        "final-repetition-probe.json": NEXT / "final-repetition-probe.json",
        "replay-audit.json": MATCH / "replay-audit.json",
        **{name: NEXT / name for name in ("final-build-tests.txt", "final-uci-tests.txt", "final-trace-tests.txt")}}
    for name, source in copies.items():
        shutil.copyfile(source, DEST / name)
    write(DEST / "core-match-summary.json", match["summary"])
    repetition = json.loads((DEST / "final-repetition-probe.json").read_text(encoding="utf-8"))
    if repetition["binary_sha256"] != protocol["sha256"]["candidate"]:
        raise ValueError("Final repetition probe and match used different binaries")
    source_names = subprocess.run(["git", "ls-tree", "-r", "--name-only", CORE, "src", "include"],
        cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines()
    source_names += ["CMakeLists.txt", ".gitattributes", "scripts/build.ps1", "scripts/trace_gambit_leaf.cpp",
        "scripts/compare_engines.py", "tests/forcing_tests.cpp", "tests/forcing_validation.py",
        "tests/tactical_horizon_validation.py", "tests/perpetual_check_validation.py",
        "tests/data/tactical_horizon_2026_11_04.json", "tests/data/comparison_openings.json"]
    source_rows = []
    with zipfile.ZipFile(DEST / "final-core-source.zip", "x", zipfile.ZIP_DEFLATED) as packed:
        for name in sorted(set(source_names)):
            data = subprocess.run(["git", "show", CORE + ":" + name], cwd=ROOT,
                capture_output=True, check=True).stdout
            packed.writestr(name, data)
            source_rows.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    provenance = {"schema": "regicide.forcing-recovery-final-provenance.v1",
        "recorded_utc": datetime.now(timezone.utc).isoformat(), "core_commit": CORE,
        "baseline_commit": protocol["commits"]["baseline"], "binary_sha256": protocol["sha256"],
        "middle_candidate_sha256": "946adcd9cce89b8501d80db9078f3e5408a4d54e934caa76bc62b73e4e4c7553",
        "source_archive_scope": "Exact Git blobs from core commit6aa; UI/host follow-up source excluded. A source commit and build receipt are not a claim of bit-reproducible binaries.",
        "source_files": source_rows, "source_zip_sha256": sha(DEST / "final-core-source.zip"),
        "final_checks": {"cpp_groups": 231, "forcing_groups_included": 16, "uci_groups": 31, "trace_tests": 4},
        "final_screen": "11 previous development/regression roots; node/time caps separately; two local improvements, no claimed new heldout generalization",
        "match": {"wins": 1, "draws": 7, "losses": 8, "complete": 16, "incomplete": 0,
            "score": .28125, "movetime_ms": 100, "max_plies": 320, "draw_policy": "claim"},
        "decision": "Do not activate; local position gains did not pass the whole-game screening criterion.",
        "live_runtime": "Existing5011 retained; no live activation by this experiment",
        "excluded": ["executables", "objects", "personal full PGN/history", "UI/idle follow-up implementation"]}
    write(DEST / "provenance.json", provenance)
    paths = []
    for directory in (NEXT / "screen-bounded64k", MATCH):
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.suffix.lower() in (".json", ".log", ".txt", ".py", ".pgn"):
                paths.append(path)
    paths += [ROOT / "build-gambit-forcing-next-final/compile.cmd"]
    outer_names = ["README.md", "archive_final.py", "final-core-source.zip", "provenance.json",
        "final-screen-summary.json", "final-repetition-probe.json", "core-match-summary.json", "replay-audit.json",
        "final-build-tests.txt", "final-uci-tests.txt", "final-trace-tests.txt", "finalcore-regression.json"]
    paths += [DEST / name for name in outer_names]
    entries = [{"path": path.relative_to(ROOT).as_posix(), "bytes": path.stat().st_size, "sha256": sha(path)} for path in paths]
    if len({row["path"] for row in entries}) != len(entries):
        raise ValueError("Duplicate archive member")
    with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED, compresslevel=9) as packed:
        for path, row in zip(paths, entries):
            packed.write(path, row["path"])
    with zipfile.ZipFile(archive) as packed:
        crc_ok = packed.testzip() is None
        hashes_ok = all(hashlib.sha256(packed.read(row["path"])).hexdigest() == row["sha256"] for row in entries)
    manifest = {"schema": "regicide.forcing-recovery-final-manifest.v1", "files": entries,
        "count": len(entries), "uncompressed_bytes": sum(row["bytes"] for row in entries),
        "zip": {"bytes": archive.stat().st_size, "sha256": sha(archive)},
        "zip_integrity_passed": crc_ok, "member_hashes_match": hashes_ok,
        "executables_objects_private_full_games_ui_included": False}
    write(DEST / "manifest.json", manifest)
    print(json.dumps({key: value for key, value in manifest.items() if key != "files"}))
    return 0 if crc_ok and hashes_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
