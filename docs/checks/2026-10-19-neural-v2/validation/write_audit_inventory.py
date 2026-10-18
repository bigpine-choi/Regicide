"""Hash retained raw cycle evidence after completion; no engine or product imports."""
import datetime
import hashlib
import json
from pathlib import Path

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "scripts").is_dir())
STATE = ROOT / "build/learning-cycle"
OUTPUT = Path(__file__).with_name("audit-raw-sha256.json")
CATALOG = json.loads((STATE / "catalog.json").read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    paths = [STATE / name for name in ("catalog.json", "latest.json", "bootstrap-snapshot.json", "cycle.lock")]
    paths += [STATE / "bootstrap" / name for name in ("manifest.json", "games.jsonl", "positions.jsonl", "games.pgn")]
    for relative in CATALOG["runs"]:
        run = STATE / relative
        paths += [run / name for name in ("cycle.json", "regicide.exe", "training.log", "summary.md")]
        paths += [run / "data" / name for name in ("manifest.json", "games.jsonl", "positions.jsonl", "games.pgn")]
        paths += [run / "loss-analysis" / name for name in ("loss-report.json", "positions.jsonl")]
        paths += [run / "corpus" / name for name in ("manifest.json", "games.jsonl", "positions.jsonl", "games.pgn")]
        report = json.loads((run / "model/report.json").read_text(encoding="utf-8"))
        paths += [run / "model" / name for name in ("report.json", report["model_path"])]
        paths += [run / "rules" / name for name in ("checks.json", "differential.log", "neural-integration.log")]
        paths += [run / "arena" / name for name in ("arena.json", "games.pgn", "resource-samples.jsonl", "candidate.nn")]
        if (run / "arena/champion.nn").exists():
            paths.append(run / "arena/champion.nn")
    expected = {path.resolve() for path in paths}
    actual = {path.resolve() for path in STATE.rglob("*") if path.is_file()}
    if actual != expected:
        raise ValueError("state contains an unlisted or missing file; audit inventory must be explicit and complete")
    files = [{"path": path.relative_to(STATE).as_posix(), "bytes": path.stat().st_size,
              "sha256": sha(path)} for path in sorted(set(paths))]
    output = {"schema": "regicide.learning.raw-evidence-sha256.v1",
              "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "measured_source_freeze_commit": "6550d6c",
              "post_audit_source_note": "Root applied only a Windows test fixture path .resolve correction after frozen-source checks passed; raw state, native binaries, models and adoption logic were not changed.",
              "scope": "All retained learning inputs/corpora/models/rule logs/arena evidence in the three catalogued attempts; no product source edits or engine calls",
              "file_count": len(files), "total_bytes": sum(row["bytes"] for row in files), "files": files,
              "audit_report_sha256": sha(Path(__file__).with_name("independent-cycle-audit.json")),
              "audit_script_sha256": sha(Path(__file__).with_name("audit_cycle.py"))}
    OUTPUT.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: output[key] for key in ("file_count", "total_bytes", "audit_report_sha256", "audit_script_sha256")}, indent=2))


if __name__ == "__main__":
    main()
