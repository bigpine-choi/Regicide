"""Preserve the two fixed forcing screens and final checks; no binaries included."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

DEST = Path(__file__).resolve().parent
ROOT = DEST.parents[2]
BUILD = ROOT / "build-gambit-forcing"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    archive = DEST / "evidence.zip"
    if archive.exists():
        raise FileExistsError("Evidence already exists; preserve it instead of overwriting")
    screen2 = json.loads((BUILD / "screen-002/source-preservation-receipt.json").read_text(encoding="utf-8"))
    core = {name: value for name, value in screen2["source_files"].items()
            if name.startswith("src/") or name.startswith("include/")}
    source_matches = {name: sha(ROOT / name) == value["sha256"] for name, value in core.items()}
    if not all(source_matches.values()):
        raise ValueError("Final core differs from the second screen source snapshot")
    copies = {
        "screen-001-summary.json": BUILD / "screen-001/summary.json",
        "screen-002-summary.json": BUILD / "screen-002/summary.json",
        "local-chain-probe.json": BUILD / "local-chain-probe.json",
        "final-build-tests.txt": BUILD / "final-build-tests.txt",
        "engine-integration.txt": BUILD / "engine-integration.txt",
    }
    for name, path in copies.items():
        shutil.copyfile(path, DEST / name)
    sources = [*sorted((ROOT / "src").glob("*.cpp")),
               *sorted((ROOT / "include/regicide").glob("*.hpp")),
               ROOT / "tests/forcing_tests.cpp", ROOT / "tests/forcing_validation.py",
               ROOT / "tests/tactical_horizon_validation.py", ROOT / "tests/perpetual_check_validation.py",
               ROOT / "CMakeLists.txt", ROOT / "scripts/build.ps1", ROOT / ".gitattributes"]
    final_source = DEST / "final-source-snapshot.zip"
    with zipfile.ZipFile(final_source, "x", zipfile.ZIP_DEFLATED) as packed:
        for path in sources:
            packed.write(path, path.relative_to(ROOT).as_posix())
    provenance = {
        "recorded_utc": datetime.now(timezone.utc).isoformat(),
        "base_head_before_experiment": "60853b3",
        "core_source_matches_screen002": source_matches,
        "core_source_all_match": all(source_matches.values()),
        "comparison_candidate_sha256": screen2["candidate"]["sha256"],
        "final_test_binary_sha256": sha(BUILD / "regicide.exe"),
        "baseline_sha256": sha(ROOT / "build-gambit-horizon/baseline/regicide.exe"),
        "binary_bit_equivalence": "not asserted; final build was tested separately and not used to rerun the two screens",
        "final_source_files": {p.relative_to(ROOT).as_posix(): {"bytes": p.stat().st_size, "sha256": sha(p)} for p in sources},
        "final_checks": {"cpp_groups": 227, "new_forcing_groups": 12, "uci_groups": 31},
        "control_scope": "Four newly authored controls in screen001 are already-observed regression controls in screen002",
        "promotion": "off by default; no live activation, Elo claim or completed match",
    }
    write(DEST / "provenance.json", provenance)
    paths = [(p, p.relative_to(ROOT).as_posix()) for screen in ("screen-001", "screen-002")
             for p in sorted((BUILD / screen).rglob("*")) if p.is_file() and p.suffix.lower() not in (".exe", ".obj")]
    for name in ("compile.cmd", "build-revision.cmd", "revision-build.log", "forcing-tests.txt"):
        path = BUILD / name
        if path.exists():
            paths.append((path, path.relative_to(ROOT).as_posix()))
    for path in sorted(DEST.iterdir()):
        if path.is_file() and path.name not in ("evidence.zip", "manifest.json"):
            paths.append((path, path.relative_to(ROOT).as_posix()))
    entries = [{"path": name, "bytes": path.stat().st_size, "sha256": sha(path)} for path, name in paths]
    if len({entry["path"] for entry in entries}) != len(entries):
        raise ValueError("Duplicate archive path")
    with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED, compresslevel=9) as packed:
        for path, name in paths:
            packed.write(path, name)
    with zipfile.ZipFile(archive) as packed:
        bad = packed.testzip()
        contents_match = all(hashlib.sha256(packed.read(entry["path"])).hexdigest() == entry["sha256"]
                             for entry in entries)
    if bad or not contents_match:
        raise ValueError("Archive integrity check failed")
    manifest = {"schema": "regicide.forcing-evidence-manifest.v1", "files": entries,
        "count": len(entries), "total_uncompressed_bytes": sum(e["bytes"] for e in entries),
        "evidence_zip": {"bytes": archive.stat().st_size, "sha256": sha(archive)},
        "zip_integrity_passed": bad is None, "member_hashes_match": contents_match,
        "executables_objects_personal_games_included": False}
    write(DEST / "manifest.json", manifest)
    print(json.dumps({key: value for key, value in manifest.items() if key != "files"}))


if __name__ == "__main__":
    main()
