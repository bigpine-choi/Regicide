"""Freeze pre-match recovery research evidence, excluding executables and games."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import zipfile

DEST = Path(__file__).resolve().parent
ROOT = DEST.parents[2]
BUILD = ROOT / "build-gambit-forcing-next"
PROTOTYPE = ROOT / "build/prototypes/2026-11-05-checking-proof"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    output = DEST / "research-inputs.zip"
    if output.exists():
        raise FileExistsError("Preserve the frozen recovery inputs")
    allowed = {".py", ".json", ".jsonl", ".log", ".txt", ".tsv", ".cpp", ".hpp", ".cmd"}
    paths = [path for directory in (BUILD, PROTOTYPE) for path in sorted(directory.rglob("*"))
             if path.is_file() and (path.suffix.lower() in allowed or path.name == "source-snapshot.zip")]
    receipt = json.loads((BUILD / "screen-final-inputs/receipt.json").read_text(encoding="utf-8"))
    source_state = {name: sha(ROOT / name) == entry["sha256"]
                    for name, entry in receipt["sources"].items()
                    if name.startswith("src/") or name.startswith("include/")}
    metadata = {"schema": "regicide.forcing-recovery-research.v1",
        "recorded_utc": datetime.now(timezone.utc).isoformat(),
        "base_head": "7a3ca1cb474dc5f806a8cdd09a2a7079bda36125",
        "screen_binary_sha256": receipt["binaries"],
        "current_core_matches_comparison_snapshot": source_state,
        "snapshot_scope": "Comparison candidate946adcd9 source bytes are in screen-final-inputs/source-snapshot.zip; later source edits do not retroactively change these experiment inputs.",
        "native_before_audit": True,
        "d14_screen_and_d18_rook_audit_are_distinct": True,
        "prototype": "Original backup header/probe and budget-exhausted results preserved; no runtime connection.",
        "private_games_executables_objects_included": False,
        "match_status": "not run; this archive freezes pre-match research only"}
    provenance = DEST / "research-provenance.json"
    provenance.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    paths += [Path(__file__), provenance]
    entries = [{"path": path.relative_to(ROOT).as_posix(), "bytes": path.stat().st_size, "sha256": sha(path)} for path in paths]
    if len({row["path"] for row in entries}) != len(entries):
        raise ValueError("Duplicate archive names")
    with zipfile.ZipFile(output, "x", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path, row in zip(paths, entries):
            archive.write(path, row["path"])
    with zipfile.ZipFile(output) as archive:
        crc_ok = archive.testzip() is None
        hashes_ok = all(hashlib.sha256(archive.read(row["path"])).hexdigest() == row["sha256"] for row in entries)
    manifest = {"schema": "regicide.forcing-recovery-research-manifest.v1", "files": entries,
        "count": len(entries), "uncompressed_bytes": sum(row["bytes"] for row in entries),
        "zip": {"bytes": output.stat().st_size, "sha256": sha(output)},
        "zip_integrity_passed": crc_ok, "member_hashes_match": hashes_ok}
    (DEST / "research-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in manifest.items() if key != "files"}))
    return 0 if crc_ok and hashes_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
