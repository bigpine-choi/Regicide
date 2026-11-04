"""Paired own-engine selector diagnostics with one shared baseline per root.

No engine runs on import. v3 is extracted byte-for-byte from the declared Git
commit. Optional searches receive equal wall-time ceilings; requested ceilings
are not added as if they were actual CPU time. This does not grade Chess.com.
"""
import argparse
import importlib.util
import json
import logging
from pathlib import Path
import platform
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import gambit_tactics_validation as native

BASELINE_REF = "02b513c323898fe3507d678cb5d0db859e823ee0"
BASELINE_FILE = ROOT / "build/gambit-paired-verification/baseline_tactics.py"


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def extract_baseline(path):
    blob = subprocess.check_output(["git", "show", BASELINE_REF + ":scripts/gambit_tactics.py"], cwd=ROOT)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != blob:
            raise ValueError("Preserved baseline module differs from the declared Git blob")
    else:
        path.write_bytes(blob)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / "build-gambit-horizon/baseline/regicide.exe")
    parser.add_argument("--output", type=Path, required=True, help="Fresh output directory")
    parser.add_argument("--baseline-depth", type=int, choices=range(4, 64),
                        help="Optional low-depth functional diagnostic; baseline still has a 1000ms ceiling")
    parser.add_argument("--fixtures-only", action="store_true", help="No engine execution")
    args = parser.parse_args(argv)
    declared = native.fixture_document()
    if args.fixtures_only:
        print(json.dumps(declared, ensure_ascii=False, indent=2))
        return 0
    binary = args.binary.resolve(strict=True)
    baseline_path = extract_baseline(BASELINE_FILE)
    baseline = load_module("paired_baseline_tactics", baseline_path)
    candidate_path = ROOT / "scripts/gambit_tactics.py"
    candidate = load_module("paired_candidate_tactics", candidate_path)
    if baseline.POLICY == candidate.POLICY:
        parser.error("Baseline and candidate policy identifiers must differ")
    fixture_path = native.FIXTURE_PATH
    if json.loads(fixture_path.read_text(encoding="utf-8")) != declared:
        parser.error("Predeclared fixtures changed")
    args.output.mkdir(parents=True, exist_ok=False)
    paths = [Path(__file__).resolve(), Path(native.__file__).resolve(), baseline_path, candidate_path,
             ROOT / "scripts/sound_sacrifice.py", ROOT / "scripts/sparring_material.py",
             ROOT / "scripts/gambit_candidate_probe.py", binary, fixture_path]
    hashes = {str(path): native.digest(path) for path in paths}
    report = {"schema": "regicide.gambit-paired-selector.v1", "started_utc": native.utc(),
        "baseline_ref": BASELINE_REF, "source_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_sha256": hashes, "policies": {"baseline": baseline.POLICY, "candidate": candidate.POLICY},
        "python": platform.python_version(), "conditions": {"native_binary_shared": str(binary),
            "baseline_ms": 1000, "baseline_depth_cap": args.baseline_depth, "optional_ms_per_arm": 1000,
            "baseline": "One native search per fixture; exact same in-memory move, score and packets copied into each policy",
            "order": "Alternating baseline/candidate first per fixture; serial fresh processes, each optional callback fresh=True",
            "evaluation": "Classical UsePieceActivity=true, Hash16, LMR on"},
        "scope": ("Depth-capped low-budget functional diagnostic; not an ordinary 1000ms-baseline comparison. " if args.baseline_depth is not None else "")
            + "Eight existing development fixtures; functional policy A/B, not heldout, games, Elo or Chess.com grades",
        "native_before_external_audit": True, "external_audit": "not performed; save full roots/selections for post-selection audit",
        "cases": [], "errors": []}
    write(args.output / "input-fixtures.json", declared)
    with zipfile.ZipFile(args.output / "executed-sources.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            if path != binary:
                archive.writestr(path.relative_to(ROOT).as_posix(), path.read_bytes())
    handler = logging.FileHandler(args.output / "raw-uci.log", encoding="utf-8")
    logger = logging.getLogger("chess.engine")
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        for index, fixture in enumerate(declared["cases"]):
            order = [("baseline", baseline), ("candidate", candidate)]
            if index % 2:
                order.reverse()
            row = {"fixture": fixture, "order": [arm for arm, _ in order], "arms": {}}
            report["cases"].append(row)
            shared = {}
            for arm, selector in order:
                logger.info("BEGIN fixture=%s arm=%s policy=%s", fixture["id"], arm, selector.POLICY)
                value = native.run_case(binary, fixture, selector, baseline_seed=shared or None,
                                        baseline_sink=shared if not shared else None, baseline_depth=args.baseline_depth)
                row["arms"][arm] = value
                if not value["passed"]:
                    report["errors"].append(f"{fixture['id']} {arm}: {value['error'] or 'functional validation failed'}")
                if not shared:
                    raise RuntimeError("Common baseline could not be completed; second arm was not run")
                write(args.output / "report.json", report)
            row["baseline_identical"] = (row["arms"]["baseline"]["baseline_seed_sha256"] == row["arms"]["candidate"]["baseline_seed_sha256"])
            row["baseline_searches"] = sum(value["baseline_origin"] == "native_once" for value in row["arms"].values())
            row["selected_move_changed"] = row["arms"]["baseline"].get("selected_uci") != row["arms"]["candidate"].get("selected_uci")
            row["passed"] = row["baseline_identical"] and row["baseline_searches"] == 1 and all(value["passed"] for value in row["arms"].values())
            print(json.dumps({"case": fixture["id"], "passed": row["passed"], "selected": {arm: value.get("selected_uci") for arm, value in row["arms"].items()}}), flush=True)
    except Exception as error:
        report["errors"].append(f"{type(error).__name__}: {error}")
    finally:
        report.update(finished_utc=native.utc(), sources_unchanged=all(native.digest(path) == digest for path, digest in hashes.items()))
        report["passed"] = (not report["errors"] and report["sources_unchanged"] and len(report["cases"]) == len(declared["cases"])
                            and all(row.get("passed") for row in report["cases"]))
        write(args.output / "report.json", report)
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        handler.close()
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
