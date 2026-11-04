"""Predeclared node-scaling diagnostic using the existing native/audit helpers.

Six existing development positions, two immutable binaries, three node caps.
All native choices finish before external audit. No game policy is changed.
"""
import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests"))
from tactical_horizon_validation import query, sf_audit, replay, sha, write
import chess
import chess.engine

IDS = ("kings-accepted:9", "kings-accepted:11", "morra-developed-defense:25",
       "budapest-accepted:16", "perpetual-minimal", "legal-queen-investment")
BUDGETS = (100_000, 1_000_000, 3_000_000)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    binaries = {arm: ROOT / "build-gambit-horizon" / arm / "regicide.exe"
                for arm in ("baseline", "candidate")}
    stockfish = ROOT / "build-m2-opponents/stockfish18/package/stockfish/stockfish-windows-x86-64.exe"
    fixture_file = ROOT / "tests/data/tactical_horizon_2026_11_04.json"
    fixtures = json.loads(fixture_file.read_text(encoding="utf-8"))["cases"]
    cases = [next(case for case in fixtures if case["id"] == name) for name in IDS]
    receipt = json.loads((ROOT / "docs/checks/2026-11-04-gambit-queen-defense/build-provenance.json").read_text(encoding="utf-8"))
    for arm, binary in binaries.items():
        if sha(binary) != receipt["binaries"][arm]["sha256"]:
            raise ValueError("Prior comparison binary changed: " + arm)
    paths = [Path(__file__), fixture_file, *binaries.values(), stockfish,
             ROOT / "tests/tactical_horizon_validation.py", ROOT / "tests/perpetual_check_validation.py"]
    hashes = {str(path): sha(path) for path in paths}
    report = {"schema": "regicide.node-scaling.v1", "started_utc": datetime.now(timezone.utc).isoformat(),
        "source_sha256": hashes, "baseline_commit": receipt["baseline_commit"],
        "candidate_commit": receipt["candidate_commit"], "nodes": BUDGETS, "fixtures": cases,
        "python": platform.python_version(), "python_chess": chess.__version__, "platform": platform.platform(),
        "logical_cpus": os.cpu_count(), "conditions": "Fresh serial process; classical activity; Hash16; candidate queen-defense on; no time cap except 15s watchdog per native query",
        "selection": "All four previously declared failures, the unresolved perpetual, and the existing queen-investment control; fixed before queries. All are development data.",
        "native_before_audit": True, "rows": [], "audits": {}, "errors": []}
    write(output / "manifest-before-queries.json", report)
    logger = logging.getLogger("chess.engine")
    handler = logging.FileHandler(output / "raw-uci.log", encoding="utf-8")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        for index, case in enumerate(cases):
            board = replay(case)
            for budget in BUDGETS:
                arms = list(binaries) if index % 2 == 0 else list(reversed(binaries))
                for arm in arms:
                    result = query(binaries[arm], board, chess.engine.Limit(nodes=budget),
                        "UseQuiescenceQueenDefense" if arm == "candidate" else None,
                        True if arm == "candidate" else None)
                    row = {"case_id": case["id"], "arm": arm, "requested_nodes": budget, "query": result}
                    report["rows"].append(row)
                    write(output / "report.json", report)
                    if not result["execution_valid"]:
                        raise RuntimeError(f"Native query failed: {case['id']} {arm} {budget}: {result['error']}")
                    if result["final_total_nodes"] != budget:
                        raise RuntimeError("Fixed-node query did not consume the declared budget")
                    print(json.dumps({"case": case["id"], "arm": arm, "nodes": budget,
                        "move": result["selected_san"], "depth": result["completed_line"]["depth"]}), flush=True)
        report["native_finished_utc"] = datetime.now(timezone.utc).isoformat()
        write(output / "report.json", report)
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        audit = chess.engine.SimpleEngine.popen_uci([str(stockfish)], timeout=10, **flags)
        try:
            settings = {"Threads": 1, "Hash": 32, "UCI_LimitStrength": False, "Skill Level": 20}
            audit.configure(settings)
            report["audit_engine"] = {"id": dict(audit.id), "settings": settings, "depth_cap": 14, "time_cap_s": .5}
            for case in cases:
                moves = [row["query"]["selected_uci"] for row in report["rows"] if row["case_id"] == case["id"]]
                value = sf_audit(audit, replay(case), moves)
                report["audits"][case["id"]] = value
                if value["status"] != "common_exact_depth":
                    raise RuntimeError("Audit has no common exact depth")
                for key, score in value["scores"].items():
                    if value["queries"][key]["returned_uci"] != score["pv_uci"][0]:
                        raise RuntimeError("Audit returned move disagrees with the common-depth line")
                write(output / "report.json", report)
        finally:
            try:
                audit.quit()
            finally:
                audit.close()
            report["audit_exit_code"] = audit.returncode.result(timeout=3)
            if report["audit_exit_code"] != 0:
                raise RuntimeError("Audit did not exit normally")
    except Exception as error:
        report["errors"].append(f"{type(error).__name__}: {error}")
    finally:
        report["finished_utc"] = datetime.now(timezone.utc).isoformat()
        report["sources_unchanged"] = all(sha(Path(path)) == digest for path, digest in hashes.items())
        report["execution_valid"] = (not report["errors"] and report["sources_unchanged"]
            and len(report["rows"]) == len(cases) * len(BUDGETS) * 2 and len(report["audits"]) == len(cases)
            and report.get("audit_exit_code") == 0)
        write(output / "report.json", report)
        logger.removeHandler(handler)
        handler.close()
    return 0 if report["execution_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
