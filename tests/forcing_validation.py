"""Compare the forcing option with the existing cold-search/audit harness.

The six old diagnostic positions stay development data. New controls are
declared from legal boards before either binary is queried. This is a screen,
not a strength/Elo promotion tool, and never installs a playing binary.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from tactical_horizon_validation import main as run_screen, replay, sha, write

OLD_FIXTURES = ROOT / "tests/data/tactical_horizon_2026_11_04.json"
DEVELOPMENT_IDS = (
    "kings-accepted:9", "kings-accepted:11", "morra-developed-defense:25",
    "budapest-accepted:16", "perpetual-minimal", "legal-queen-investment",
    "standard-start-control",
)
# Fixed using board legality only. No engine result or target evaluation was
# consulted to select these controls. Do not tune using their screen results
# and continue to describe them as unseen controls afterward.
NEW_CONTROLS = (
    ("forcing-control-king-pressure", "6k1/5ppp/8/4N3/8/8/5PPP/3QR1K1 w - - 0 1",
     ["king_pressure", "mate_resource"]),
    ("forcing-control-queen-under-rook-attack", "6k1/5ppp/8/8/3r4/3Q4/5PPP/6K1 w - - 0 1",
     ["queen_attack", "capture_resource"]),
    ("forcing-control-knight-check", "6k1/5r2/8/3q4/4N3/8/8/R5K1 w - - 0 1",
     ["checking_candidate", "fork_resource"]),
    ("forcing-control-quiet-pawn-ending", "8/6k1/5p2/8/3P4/8/4K3/8 w - - 0 1",
     ["quiet_control", "pawn_ending"]),
)


def fixture_document(development_only=False):
    old = json.loads(OLD_FIXTURES.read_text(encoding="utf-8"))
    cases = [deepcopy(next(case for case in old["cases"] if case["id"] == name))
             for name in DEVELOPMENT_IDS]
    for case in cases:
        case["split"] = "development"
    if not development_only:
        for name, fen, tags in NEW_CONTROLS:
            board = chess.Board(fen)
            if not board.is_valid() or board.is_game_over(claim_draw=False):
                raise ValueError("Invalid or terminal authored control: " + name)
            cases.append({"id": name, "initial_fen": fen, "history_uci": [],
                "before_fen": board.fen(en_passant="fen"), "split": "new_control",
                "tags": tags, "source": "Project-authored legal board; fixed before native queries"})
    for case in cases:
        board = replay(case)
        if not board.is_valid() or board.fen(en_passant="fen") != case["before_fen"]:
            raise ValueError("Illegal fixture history or mismatching FEN: " + case["id"])
    return {"schema": "regicide.forcing-fixtures.v1", "cases": cases,
        "selection": "Six prior diagnostics plus the old standard-start parity root; four separately authored controls if requested. No result-based selection.",
        "new_control_use": "Selected from legality only. Once results are inspected, these roots must not be called unseen for further tuning.",
        "source_fixture_sha256": sha(OLD_FIXTURES), "wrapper_sha256": sha(Path(__file__))}


def summarize(report):
    rows = []
    for case in report["cases"]:
        audit = case.get("post_selection_sf_audit", {})
        scores, losses = audit.get("scores", {}), audit.get("loss_cp", {})
        for mode, pair in case["queries"].items():
            row = {"case_id": case["fixture"]["id"], "split": case["fixture"]["split"],
                "mode": mode, "audit_status": audit.get("status"), "audit_depth": audit.get("depth"),
                "selected_move_changed": pair["selected_move_changed"]}
            for arm in ("baseline", "candidate"):
                query = pair[arm]
                uci = query["selected_uci"]
                line = query.get("completed_line") or {}
                row[arm] = {"uci": uci, "san": query.get("selected_san"),
                    "completed_depth": line.get("depth"), "cp_root": line.get("cp_root"),
                    "mate_root": line.get("mate_root"), "final_total_nodes": query.get("final_total_nodes"),
                    "elapsed_ms": query["elapsed_ms"], "loss_cp": losses.get(uci),
                    "audit_mate_root": scores.get(uci, {}).get("mate_root")}
            before, after = row["baseline"]["loss_cp"], row["candidate"]["loss_cp"]
            row["candidate_minus_baseline_loss_cp"] = after - before if before is not None and after is not None else None
            row["large_regression_warning"] = (after > 150 and after - before >= 100
                if before is not None and after is not None else None)
            old_mate, new_mate = row["baseline"]["audit_mate_root"], row["candidate"]["audit_mate_root"]
            row["new_losing_mate_warning"] = (new_mate is not None and new_mate < 0
                and not (old_mate is not None and old_mate < 0))
            rows.append(row)
    return {"schema": "regicide.forcing-screen-summary.v1", "rows": rows,
        "execution_valid": report["execution_valid"], "audit_execution_valid": report["audit_execution_valid"],
        "host_policy_contracts_passed": report["host_policy_contracts_passed"],
        "large_regression_warnings": [row for row in rows if row["large_regression_warning"]],
        "new_losing_mate_warnings": [row for row in rows if row["new_losing_mate_warning"]],
        "scope": "Separate requested node/time caps, same-root post-selection audit. No game score, Elo, global safety claim or automatic activation."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--candidate-option", default="UseQuiescenceForcing")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--nodes", type=int, default=850_000)
    parser.add_argument("--movetime-ms", type=int, default=2085)
    parser.add_argument("--stockfish", type=Path)
    parser.add_argument("--host-smoke", action="store_true")
    parser.add_argument("--development-only", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args(argv)
    if min(args.nodes, args.movetime_ms) < 1:
        parser.error("Positive node and time caps are required")
    for path in (args.baseline, args.candidate, *([args.stockfish] if args.stockfish else [])):
        path.resolve(strict=True)
    document = fixture_document(args.development_only)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    fixture = output / "fixtures.json"
    write(fixture, document)
    provenance = {"started_utc": datetime.now(timezone.utc).isoformat(),
        "wrapper_sha256": sha(Path(__file__)), "fixture_sha256": sha(fixture),
        "old_fixture_sha256": sha(OLD_FIXTURES), "harness_sha256": sha(ROOT / "tests/tactical_horizon_validation.py"),
        "binary_sha256": {"baseline": sha(args.baseline), "candidate": sha(args.candidate)},
        "nodes": args.nodes, "movetime_ms": args.movetime_ms,
        "candidate_option": args.candidate_option, "new_controls_included": not args.development_only,
        "all_fixtures_declared_before_queries": True, "prepare_only": args.prepare_only}
    write(output / "predeclared.json", provenance)
    if args.prepare_only:
        print(str(fixture), flush=True)
        return 0
    native_output = output / "native"
    command = ["--baseline", str(args.baseline), "--candidate", str(args.candidate),
        "--candidate-option", args.candidate_option, "--output-dir", str(native_output),
        "--fixtures", str(fixture), "--nodes", str(args.nodes), "--movetime-ms", str(args.movetime_ms)]
    if args.stockfish:
        command += ["--stockfish", str(args.stockfish)]
    if args.host_smoke:
        command.append("--host-smoke")
    result = run_screen(command)
    report = json.loads((native_output / "report.json").read_text(encoding="utf-8"))
    summary = summarize(report)
    summary["wrapper_unchanged"] = sha(Path(__file__)) == provenance["wrapper_sha256"]
    summary["harness_unchanged"] = sha(ROOT / "tests/tactical_horizon_validation.py") == provenance["harness_sha256"]
    write(output / "summary.json", summary)
    print(json.dumps({key: value for key, value in summary.items() if key != "rows"}, ensure_ascii=False), flush=True)
    return result if summary["wrapper_unchanged"] and summary["harness_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
