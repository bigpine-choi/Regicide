"""Bounded native-only paired replay of the v6/v7 opening prevention path.

Both policies receive the same real one-second baseline and empty past
observations. All later scores and moves come from fresh Regicide searches.
This diagnostic does not supply independent position or playing-strength truth.
"""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import sparring_play

LAUNCH = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
OPENING = ("e4 e5 Nf3 Nc6 Bc4 Bc5 d3 Nf6 Nc3 d6 O-O O-O h3 h6 a3 a6 "
           "Be3 Be6 Qd2 Qd7 Rae1 Rae8 Kh1 Kh8 Nh2 Nh7 f4 f5 exf5 Bxf5 "
           "fxe5 Nxe5 Bb3 c6 d4 Ng6 Bg1 d5 Qd1 Be6").split()
PLIES = (8, 9, 12, 13, 16, 17, 20, 21, 24, 25, 28, 29, 32, 33, 36, 37, 38, 39)
SOURCES = ("scripts/sparring_play.py", "scripts/sparring_pacing.py", "scripts/sparring_evaluation.py",
           "scripts/evaluation_stability.py", "scripts/boss_play.py", "scripts/opening_variety.py",
           "tests/opening_tension_native_integration.py")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_previous(previous_ref):
    previous = ROOT / "build/previous-sparring-play.py"
    previous.parent.mkdir(parents=True, exist_ok=True)
    revision = subprocess.run(["git", "rev-parse", "--verify", f"{previous_ref}^{{commit}}"],
                              cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    source = f"{revision}:scripts/sparring_play.py"
    previous.write_bytes(subprocess.run(["git", "show", source],
        cwd=ROOT, capture_output=True, check=True).stdout)
    spec = importlib.util.spec_from_file_location("previous_sparring_play", previous)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module.POLICY != "regicide-sparring-play-v6":
        raise ValueError("The frozen comparison revision must contain the recorded v6 policy")
    return module, {"path": str(previous.relative_to(ROOT)), "sha256": sha(previous),
                    "source": source, "requested_revision": previous_ref, "resolved_revision": revision}


def fixtures():
    opening = chess.Board()
    for san in OPENING:
        opening.push_san(san)
    histories = []
    for group in ("2026-11-04-sparring-quality-v2", "2026-11-03-sparring-quality",
                  "2026-11-03-sparring-validation"):
        for game in ("1700-white", "1320-black", "1320-white", "1700-black"):
            path = ROOT / f"docs/matches/{group}/{game}/game.json"
            if not path.is_file():
                continue
            saved = json.loads(path.read_text(encoding="utf-8"))
            board = chess.Board(saved.get("initial_fen", chess.STARTING_FEN))
            for uci in saved["uci_moves"][:40]:
                board.push_uci(uci)
            histories.append((f"{group}-{game}", board,
                              {"path": str(path.relative_to(ROOT)), "sha256": sha(path)}))
    quiet = chess.Board()
    for san in "d4 d5 Nf3 Nf6 e3 a6 Bd3 h6 O-O e6 b3 c5 Bb2 Nc6 Nbd2".split():
        quiet.push_san(san)
    histories.extend([("quiet_development_with_flank_pawns", quiet, None),
                      ("controlled_legal_opening", opening, None)])
    # Interleave sources to avoid spending the scan allowance on one opening.
    for ply in PLIES:
        for name, final, source in histories:
            if len(final.move_stack) < ply:
                continue
            board = final.copy(stack=True)
            while len(board.move_stack) > ply:
                board.pop()
            if not board.is_game_over(claim_draw=True) and not board.is_check():
                yield name, board, source


def start_engine(binary):
    engine = chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=5, **LAUNCH)
    options = {name: value for name, value in {"UsePieceActivity": True, "Threads": 1, "Hash": 16}.items()
               if name in engine.options}
    engine.configure(options)
    return engine, options


def searcher(engine, board, deadline, trace):
    def search(budget, *, root_move=None, root_moves=None, publish=False, fresh=True, depth=None, position=None):
        if publish or not fresh:
            raise ValueError("Diagnostic searches must be fresh and unpublished")
        remaining = (deadline - time.monotonic()) * 1000 - 50
        amount = min(float(budget), remaining)
        if amount <= 0:
            raise TimeoutError("diagnostic deadline")
        current = board if position is None else position
        arguments = {"game": object(), "info": chess.engine.INFO_ALL}
        if root_move is not None:
            arguments["root_moves"] = [root_move]
        elif root_moves is not None:
            arguments["root_moves"] = root_moves
        packets, latest = {}, {}
        started = time.monotonic()
        with engine.analysis(current.copy(stack=True), chess.engine.Limit(time=amount / 1000, depth=depth),
                             **arguments) as stream:
            for info in stream:
                if type(info.get("depth")) is int and "score" in info:
                    latest = dict(info)
                    packets[info["depth"]] = latest
            reply = stream.wait()
        trace.append({"budget_ms": round(amount, 3), "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                      "requested_depth": depth, "completed_depth": latest.get("depth"),
                      "position": "child" if position is not None else "root",
                      "restricted_roots": [move.uci() for move in arguments.get("root_moves", [])],
                      "returned_uci": reply.move.uci() if reply.move else None})
        return reply, latest, packets
    return search


def run(binary, *, previous_ref="059f7f70", maximum_cases=4, maximum_scans=48, maximum_seconds=210):
    started = time.monotonic()
    deadline = started + maximum_seconds
    previous, previous_source = load_previous(previous_ref)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True, check=True).stdout.strip()
    report = {"schema": "regicide.opening-tension-native-replay.v1",
              "created_at_utc": datetime.now(timezone.utc).isoformat(), "working_tree_base_commit": commit,
              "baseline_source_commit": previous_source["resolved_revision"],
              "binary": str(binary), "binary_sha256": sha(binary),
              "sources_sha256": {name: sha(ROOT / name) for name in SOURCES},
              "previous_policy_source": previous_source,
              "baseline_budget_ms": 1000, "policy_extra_budget_ms": 2000,
              "maximum_cases": maximum_cases, "maximum_scans": maximum_scans,
              "maximum_seconds": maximum_seconds, "scans": [], "cases": [],
              "independent_audit": {"performed": False, "reason": "native-only functional replay"},
              "scope": "fixed real native baselines, no controlled scores or past observations; bounded wall-time searches are not deterministic; no independent balance, Elo or human-feel claim"}
    seen = set()
    for name, board, fixture_source in fixtures():
        if len(report["cases"]) >= maximum_cases or len(report["scans"]) >= maximum_scans or deadline - time.monotonic() < 10:
            break
        key = tuple(move.uci() for move in board.move_stack)
        if key in seen:
            continue
        seen.add(key)
        trace = []
        engine, configured = start_engine(binary)
        try:
            base, info, packets = searcher(engine, board, deadline, trace)(1000)
        finally:
            engine.quit()
        line = sparring_play._score_line(board, info, base.move) if base.move else None
        eligible = bool(line and line["depth"] >= sparring_play.MIN_DEPTH and 60 < line["cp"] <= 100)
        scan = {"fixture": name, "ply": len(board.move_stack), "fen": board.fen(en_passant="fen"),
                "baseline": line, "eligible": eligible, "trace": trace}
        report["scans"].append(scan)
        print(json.dumps({"scan": len(report["scans"]), "fixture": name, "ply": len(board.move_stack),
                          "cp_own": line["cp"] if line else None, "eligible": eligible}), flush=True)
        if not eligible:
            continue
        case = {"fixture": name, "fixture_source": fixture_source, "ply": len(board.move_stack),
                "history_uci": list(key), "fen": board.fen(en_passant="fen"),
                "baseline": line, "baseline_trace": trace, "configured_options": configured,
                "past_observations": [], "runs": []}
        for module in (previous, sparring_play):
            trace = []
            engine, configured = start_engine(binary)
            try:
                decision, discarded = module.choose(board, base.move, copy.deepcopy(info), copy.deepcopy(packets),
                    searcher(engine, board, deadline, trace), engine.close,
                    decisions=[], seed="20261106-native-opening-tension", movetime_ms=1000)
                selected = chess.Move.from_uci(decision["selected_uci"])
                if selected not in board.legal_moves:
                    raise AssertionError("Policy selected an illegal native move")
                case["runs"].append({"policy": module.POLICY, "decision": decision,
                                      "discarded_engine": discarded, "search_trace": trace})
            finally:
                try:
                    engine.quit()
                except chess.engine.EngineTerminatedError:
                    pass
        report["cases"].append(case)
        print(json.dumps({"paired_case": len(report["cases"]), "ply": case["ply"],
                          "runs": [{"policy": r["policy"], "reason": r["decision"]["reason"],
                                    "selected": r["decision"]["selected_uci"]} for r in case["runs"]]}), flush=True)
    runs = [run for case in report["cases"] for run in case["runs"]]
    current = [run for run in runs if run["policy"] == sparring_play.POLICY]
    report["summary"] = {"scanned_positions": len(report["scans"]), "paired_positions": len(report["cases"]),
        "v7_prevention_requested": sum(r["decision"].get("opening_tension", {}).get("requested", False) for r in current),
        "v7_prevention_searches": sum(r["decision"].get("opening_tension", {}).get("active", False) for r in current),
        "v7_prevention_applied": sum(r["decision"].get("opening_tension", {}).get("applied", False) for r in current),
        "v7_unverified_or_unchanged": sum(not r["decision"].get("opening_tension", {}).get("applied", False) for r in current),
        "elapsed_ms": round((time.monotonic() - started) * 1000, 3)}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--maximum-scans", type=int, default=48)
    parser.add_argument("--previous-ref", default="059f7f70",
                        help="Frozen v6 source revision; default is upstream base 059f7f70, never moving HEAD")
    args = parser.parse_args()
    result = run(args.binary.resolve(strict=True), previous_ref=args.previous_ref,
                 maximum_scans=min(48, max(1, args.maximum_scans)))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "summary": result["summary"]}), flush=True)


if __name__ == "__main__":
    main()
