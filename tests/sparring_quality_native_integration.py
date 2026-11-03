"""Replay recorded incumbents through the actual native quality guard.

The incumbent is fixed from recorded evidence, not promised to recur under a
wall-clock search. Replacement moves come only from native root exclusion.
Independent fresh Stockfish searches audit root, original child, and final
child for 3000ms each after selection; they cannot influence the guard.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from runtime_engine import resolve_runtime_engine
import sparring_evaluation
import sparring_quality
from sparring_referee import SparringReferee

SAVED = ROOT / "docs/matches/2026-11-03-sparring-validation"
SOURCES = ("scripts/sparring_quality.py", "scripts/sparring_referee.py",
           "scripts/sparring_evaluation.py", "scripts/sparring_play.py",
           "scripts/play_human.py", "scripts/runtime_engine.py",
           "tests/sparring_quality_native_integration.py")
SF_OPTIONS = {"Threads": 1, "Hash": 32, "Skill Level": 20,
              "UCI_LimitStrength": False, "UCI_ShowWDL": True}
NATIVE_OPTIONS = {"Hash": 16, "UsePieceActivity": True}
LAUNCH = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
HISTORY = "b1c3 e7e5 g1f3 b8c6 d2d4 e5d4 f3d4 c6d4 d1d4 g8f6 c3b5 f8e7 c1f4 d7d6 e1c1 e8g8"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes():
    return {name: sha(ROOT / name) for name in SOURCES}


def cases():
    source = SAVED / "reviewed-cases.json"
    reviewed = json.loads(source.read_text(encoding="utf-8"))["cases"]
    for game, ply in (("1700-white", 99), ("1700-black", 86),
                      ("1320-black", 30), ("1320-black", 42), ("1320-white", 53)):
        case = next(row for row in reviewed if row["game"] == game and row["ply"] == ply)
        path = SAVED / game / "game.json"
        saved = json.loads(path.read_text(encoding="utf-8"))
        board = chess.Board(saved["initial_fen"])
        for move in saved["uci_moves"][:ply - 1]:
            board.push_uci(move)
        assert board.fen(en_passant="fen") == case["fen"]
        yield {"id": f"{game}-{ply}", "board": board, "original_uci": case["move_uci"],
               "recorded": case, "evidence_sha256": {str(path.relative_to(ROOT)): sha(path),
                                                        str(source.relative_to(ROOT)): sha(source)}}
    board = chess.Board()
    for move in HISTORY.split():
        board.push_uci(move)
    path = ROOT / "tests/sparring_safety_native_integration.py"
    yield {"id": "original-knight-17", "board": board, "original_uci": "b5a7",
           "recorded": {"own_baseline_depth": 5, "own_baseline_cp_own": 17},
           "evidence_sha256": {str(path.relative_to(ROOT)): sha(path)}}
    path = ROOT / "docs/matches/2026-11-03-sparring-quality/1700-white/game.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    for ply in (33, 39, 41):
        row = saved["moves"][ply - 1]
        board = chess.Board(saved["initial_fen"])
        for move in saved["uci_moves"][:ply - 1]:
            board.push_uci(move)
        assert board.fen(en_passant="fen") == row["before_fen"]
        decision = row["sparring_decision"]
        yield {"id": f"quality-1700-white-{ply}", "board": board, "original_uci": row["uci"],
               "recorded": {"san": row["san"], "own_baseline_depth": decision["baseline"]["depth"],
                            "own_baseline_cp_own": decision["baseline"]["cp"],
                            "quality_guard_status": decision["quality_guard"]["status"],
                            "quality_guard_reason": decision["quality_guard"]["reason"]},
               "evidence_sha256": {str(path.relative_to(ROOT)): sha(path)}}


def json_packet(info, own):
    packet = {key: info[key] for key in ("depth", "seldepth", "nodes", "time", "nps",
              "lowerbound", "upperbound") if key in info}
    if "score" in info:
        score = info["score"].pov(own)
        packet.update(cp_own=score.score(), mate_own=score.mate(), raw_score=str(info["score"]))
    packet["pv_uci"] = [move.uci() for move in info.get("pv", [])]
    return packet


def independent_audit(binary, board, own, milliseconds):
    """Separate fresh process per position; no retained guard hash or state."""
    engine = chess.engine.SimpleEngine.popen_uci(str(binary), timeout=10, **LAUNCH)
    packets = []
    started = time.monotonic()
    try:
        engine.configure(SF_OPTIONS)
        with engine.analysis(board.copy(stack=True), chess.engine.Limit(time=milliseconds / 1000),
                             game=object(), info=chess.engine.INFO_ALL) as analysis:
            for info in analysis:
                if "depth" in info and "score" in info and "pv" in info:
                    packets.append(json_packet(info, own))
            result = analysis.wait()
        exact = [row for row in packets if not row.get("lowerbound") and not row.get("upperbound")
                 and row["pv_uci"] and row["pv_uci"][0] == result.move.uci()]
        completed = max(exact, key=lambda row: row["depth"]) if exact else None
        return {"fen": board.fen(en_passant="fen"), "history_uci": [m.uci() for m in board.move_stack],
                "bestmove": result.move.uci(), "completed": completed, "packets": packets,
                "budget_ms": milliseconds, "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                "selection_influence": False, "engine": engine.id, "configured_options": SF_OPTIONS}
    finally:
        engine.quit()


def run_case(case, binary, sf_binary, audit_ms):
    board = case.pop("board")
    original = chess.Move.from_uci(case["original_uci"])
    assert original in board.legal_moves
    trace = []
    native = chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=10, **LAUNCH)
    native.configure(NATIVE_OPTIONS)
    referee = SparringReferee(sf_binary)
    case.update(fen=board.fen(en_passant="fen"), root_fen=board.root().fen(en_passant="fen"),
                history_uci=[move.uci() for move in board.move_stack],
                own="white" if board.turn else "black", native_id=native.id,
                native_configured_options={**{k: v.default for k, v in native.options.items()}, **NATIVE_OPTIONS})

    def search(amount, *, root_moves, fresh, publish, depth=None):
        assert fresh is True and publish is False
        # Same packet retention as HumanGame.search: do not merge partial output.
        latest, packets = {}, {}
        started = time.monotonic()
        with native.analysis(board.copy(stack=True), chess.engine.Limit(time=amount / 1000, depth=depth),
                             game=object(), info=chess.engine.INFO_ALL, root_moves=list(root_moves)) as analysis:
            for info in analysis:
                if type(info.get("depth")) is int and "score" in info:
                    latest = dict(info)
                    packets[info["depth"]] = latest
            reply = analysis.wait()
        trace.append({"budget_ms": amount, "requested_depth": depth, "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                      "allowed_uci": [move.uci() for move in root_moves], "returned_uci": reply.move.uci(),
                      "final": json_packet(latest, board.turn),
                      "packets": [json_packet(packets[d], board.turn) for d in sorted(packets)]})
        return reply, latest, packets

    def assess(move, amount, cancelled):
        return referee.assess_move_quality(game_id=case["id"], revision=len(board.move_stack),
            board=board.copy(stack=True), selected_move=move, budget_ms=int(amount), cancelled=cancelled)

    try:
        with referee.quality_session():
            replacement, receipt, discarded = sparring_quality.verify(
                board, original, search, native.close, assess, budget_ms=8000)
        selected = replacement[0].move if replacement is not None else original
        assert selected.uci() == receipt["selected_uci"] and selected in board.legal_moves
        native_only = replacement is None or any(row["returned_uci"] == selected.uci() for row in trace)
        case.update(selected_uci=selected.uci(), receipt=receipt, native_search_trace=trace,
                    native_only_replacement=native_only, native_discarded=discarded)
    finally:
        referee.close()
        try:
            native.quit()
        except chess.engine.EngineTerminatedError:
            pass
    audits = {"root": independent_audit(sf_binary, board, board.turn, audit_ms)}
    for key, move in (("original_child", original), ("selected_child", selected)):
        child = board.copy(stack=True)
        child.push(move)
        audits[key] = independent_audit(sf_binary, child, board.turn, audit_ms)
    case["independent_audit"] = audits
    scores = {key: (value["completed"] or {}).get("cp_own") for key, value in audits.items()}
    if all(type(value) is int for value in scores.values()):
        if len(board.move_stack) < 40:
            floor = -80 if scores["root"] >= -80 else scores["root"] - 40
        elif len(board.move_stack) < 98:
            floor = sparring_quality.sparring_play.ramp_guard(board, scores["root"])["floor_cp"]
        else:
            floor = scores["root"] - sparring_quality.FINISH_MAX_LOSS_CP
        independent_ok = scores["selected_child"] >= floor - sparring_quality.NOISE_CP
    else:
        floor, independent_ok = None, False
    improvement = (scores["selected_child"] - scores["original_child"]
                   if type(scores["selected_child"]) is int and type(scores["original_child"]) is int else None)
    strict_pass = bool(selected != original and native_only and independent_ok and receipt["status"] == "replaced")
    mitigation_improves = bool(selected != original and native_only and receipt["status"] == "mitigated"
                              and improvement is not None and improvement > 0)
    case["verification"] = {"scores_cp_own": scores, "independent_floor_cp": floor,
        "independent_floor_met": independent_ok, "original_replaced": selected != original,
        "native_only": native_only, "guard_verified": receipt["status"] == "replaced",
        "guard_mitigated": receipt["status"] == "mitigated", "independent_improvement_cp": improvement,
        "mitigation_independently_improves": mitigation_improves,
        "strict_pass": strict_pass, "passed": strict_pass}
    return case


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/checks/2026-11-03-sparring-disagreement/guard-replay.json")
    parser.add_argument("--referee", type=Path)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--audit-ms", type=int, default=3000)
    args = parser.parse_args()
    if args.audit_ms < 1000:
        parser.error("Independent audit must allow at least 1000ms")
    binary = resolve_runtime_engine(ROOT)
    metadata = json.loads((SAVED / "1700-white/metadata.json").read_text(encoding="utf-8"))
    sf_binary = args.referee or Path(metadata["config"]["stockfish"])
    before = source_hashes()
    result = {"schema": "regicide.sparring-quality-native.v1", "passed": False,
        "started_utc": datetime.now(timezone.utc).isoformat(), "source_sha256": before,
        "native": {"path": str(binary), "sha256": sha(binary), "option_overrides": NATIVE_OPTIONS},
        "stockfish": {"path": str(sf_binary), "sha256": sha(sf_binary), "configured_options": SF_OPTIONS},
        "environment": {"platform": platform.platform(), "python": platform.python_version(),
                        "logical_cpu_count": os.cpu_count()},
        "protocol": {"fixed_recorded_incumbent": True, "full_history": True,
                     "guard_budget_ms": 8000, "independent_audit_ms_each": args.audit_ms,
                     "independent_fresh_process_each_position": True, "host_flow_test": False},
        "limitations": "Recorded position regressions only. Incumbents are fixed, not regenerated by timed search. Independent Stockfish has more time but is the same evaluator family, not proof or Elo evidence. Test exercises the production quality module and native/referee callbacks, not the full HumanGame UI.",
        "cases": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def save():
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    save()
    for case in cases():
        if args.case and case["id"] not in args.case:
            continue
        row = run_case(case, binary, sf_binary, args.audit_ms)
        result["cases"].append(row)
        save()
        print(json.dumps({"id": row["id"], "selected": row["selected_uci"],
                          "elapsed_ms": row["receipt"]["elapsed_ms"], **row["verification"]}), flush=True)
    after = source_hashes()
    result.update(source_sha256_after=after, source_unchanged=before == after,
                  native_sha256_after=sha(binary), stockfish_sha256_after=sha(sf_binary),
                  finished_utc=datetime.now(timezone.utc).isoformat())
    result["passed"] = bool(result["cases"] and before == after
        and result["native_sha256_after"] == result["native"]["sha256"]
        and result["stockfish_sha256_after"] == result["stockfish"]["sha256"]
        and all(row["verification"]["passed"] for row in result["cases"]))
    result["strict_pass_count"] = sum(row["verification"]["strict_pass"] for row in result["cases"])
    result["mitigation_improvement_count"] = sum(row["verification"]["mitigation_independently_improves"] for row in result["cases"])
    result["classification_note"] = "Mitigation is reported separately and never counted as a strict floor pass, even if the independent audit happens to reach that floor. passed requires every strict check."
    save()
    if not result["passed"]:
        raise SystemExit("Native quality replay did not meet every check; inspect the saved receipt.")


if __name__ == "__main__":
    main()
