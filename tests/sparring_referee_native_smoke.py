"""Small authored-position smoke; not a match, strength test or user-game replay."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import chess
import chess.engine
from referee_engine import resolve_referee_engine
from sparring_referee import SparringReferee

OUTPUT = None
engines = []
engine_started = threading.Event()


def factory(binary):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    engine = chess.engine.SimpleEngine.popen_uci([str(binary)], timeout=1.0, **options)
    engines.append({"engine": engine, "pid": engine.transport.get_pid(), "name": engine.id.get("name")})
    engine_started.set()
    return engine


def wait_reference(service):
    end = time.monotonic() + 3
    while time.monotonic() < end:
        value = service.snapshot()
        if value["status"] != "loading":
            return value
        time.sleep(.01)
    return service.snapshot()


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    global OUTPUT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/sparring-referee-verification')
    parser.add_argument('--candidate-budget-ms', type=int, default=1500)
    arguments = parser.parse_args()
    if not 100 <= arguments.candidate_budget_ms <= 2000:
        parser.error('--candidate-budget-ms must be between 100 and 2000')
    candidate_budget_ms = arguments.candidate_budget_ms
    OUTPUT = arguments.output.resolve()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    source_paths = [ROOT / 'scripts/sparring_referee.py', Path(__file__).resolve()]
    source_hashes_before = {str(path.relative_to(ROOT)): sha(path) for path in source_paths}
    metadata = resolve_referee_engine()
    if not metadata.get("available"):
        raise RuntimeError("official Stockfish fixture is unavailable")
    binary = metadata["binary"]
    report = {"started_at_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "authored fixed FEN/module smoke only; no matches, user games or strength claims",
              "external_load": "shared desktop; other agent/user work was not stopped; no isolated CPU claim",
              "source": metadata,
              "settings": {"threads": 1, "hash_mb": 32, "skill_level": 20, "limit_strength": False,
                           "live_budget_ms": 500, "candidate_budget_ms": candidate_budget_ms},
              "module_sha256": sha(ROOT / "scripts/sparring_referee.py"),
              "source_hashes_before": source_hashes_before,
              "script_sha256": sha(Path(__file__)), "cases": [], "failures": []}
    service = SparringReferee(binary, factory)
    try:
        base = chess.Board()
        base.remove_piece_at(chess.D8)
        for color in (chess.WHITE, chess.BLACK):
            board = base.copy(stack=True)
            board.turn = color
            game_id = "native-white-reference" if color else "native-black-reference"
            service.update_position(game_id=game_id, revision=1, board=board, budget_ms=500)
            value = wait_reference(service)
            white_favored = (value.get("cp") is not None and value["cp"] > 0
                             or value.get("mate") is not None and value["mate"] > 0)
            passed = (value["status"] == "ready" and value["game_id"] == game_id
                      and value["revision"] == 1 and value["fen"] == board.fen(en_passant="fen")
                      and value["turn"] == ("white" if color else "black") and white_favored)
            report["cases"].append({"case": game_id, "authored_fen": board.fen(en_passant="fen"),
                                    "passed": passed, "result": value})
            if not passed:
                report["failures"].append(game_id)

        safe = chess.Board()
        service.update_position(game_id="safe-screen", revision=2, board=safe, active=False)
        answer = service.screen_candidates(game_id="safe-screen", revision=2, board=safe,
                                            baseline_move="a2a3", candidates=["e2e4"], budget_ms=candidate_budget_ms)
        target = answer.get("band", {}).get("target_cp_own", 20) if answer.get("band") else 20
        comparable = [row for row in answer.get("candidates", []) if row.get("cp_own") is not None]
        expected = min(comparable, key=lambda row: (abs(row["cp_own"] - target), row["uci"] != "a2a3")) if comparable else None
        passed = (answer["status"] == "ready" and len(comparable) == 2 and expected is not None
                  and answer["selected_uci"] == expected["uci"])
        report["cases"].append({"case": "safe-supplied-target-comparison", "passed": passed,
                                "authored_fen": safe.fen(en_passant="fen"),
                                "expected_uci": expected["uci"] if expected else None,
                                "fixture_note": "both legal moves supplied manually to exercise the API; not represented as Regicide-search output",
                                "result": answer})
        if not passed:
            report["failures"].append("safe-supplied-target-comparison")

        explicit = chess.Board("3rk3/7p/8/8/3Q4/8/P7/4K3 w - - 0 1")
        service.update_position(game_id="explicit-allowed-screen", revision=21, board=explicit, active=False)
        answer = service.screen_candidates(game_id="explicit-allowed-screen", revision=21, board=explicit,
                                            baseline_move="d4e5", candidates=["d4d8"], budget_ms=candidate_budget_ms)
        target = answer.get("band", {}).get("target_cp_own", 20) if answer.get("band") else 20
        comparable = [row for row in answer.get("candidates", []) if row.get("cp_own") is not None]
        expected = min(comparable, key=lambda row: (abs(row["cp_own"] - target), row["uci"] != "d4e5")) if comparable else None
        passed = (answer["status"] == "ready" and len(comparable) == 2 and expected is not None
                  and expected["uci"] == "d4d8" and answer["accepted"] and answer["selected_uci"] == "d4d8")
        report["cases"].append({"case": "explicit-supplied-alternative-allowed", "passed": passed,
                                "authored_fen": explicit.fen(en_passant="fen"),
                                "expected_uci": expected["uci"] if expected else None,
                                "fixture_note": "manual API branch fixture: Qe5 preserves queen; Qxd8 allows Kxd8 into kings-and-pawns; not playing-strength evidence",
                                "result": answer})
        if not passed:
            report["failures"].append("explicit-supplied-alternative-allowed")

        hanging = chess.Board()
        for move in ("e2e4", "e7e5", "d1h5", "g8f6"):
            hanging.push_uci(move)
        service.update_position(game_id="hanging-screen", revision=3, board=hanging, active=False)
        answer = service.screen_candidates(game_id="hanging-screen", revision=3, board=hanging,
                                            baseline_move="h5e5", candidates=["a2a3"], budget_ms=candidate_budget_ms)
        passed = not answer["accepted"] and answer["selected_uci"] == "h5e5"
        report["cases"].append({"case": "hanging-queen-rejected", "passed": passed,
                                "authored_fen": hanging.fen(en_passant="fen"),
                                "fixture_note": "a3 ignores the knight's direct attack on the queen; manually supplied legal fixture",
                                "result": answer})
        if not passed:
            report["failures"].append("hanging-queen-rejected")

        service.update_position(game_id="cancel-screen", revision=4, board=safe, active=False)
        cancellation = threading.Event()
        timer = threading.Timer(.05, cancellation.set)
        timer.start()
        try:
            answer = service.screen_candidates(game_id="cancel-screen", revision=4, board=safe,
                                                baseline_move="a2a3", candidates=["e2e4"], budget_ms=candidate_budget_ms,
                                                cancelled=cancellation.is_set)
        finally:
            timer.cancel()
            timer.join(1)
        passed = not answer["accepted"] and answer["selected_uci"] == "a2a3" and answer["reason"] == "cancelled"
        report["cases"].append({"case": "host-cancellation-baseline", "passed": passed, "result": answer})
        if not passed:
            report["failures"].append("host-cancellation-baseline")

        engine_started.clear()
        service.update_position(game_id="close-live", revision=5, board=base, budget_ms=1500)
        engine_started.wait(1)
        service.close()
        if service._worker is not None:
            service._worker.join(2)
        passed = service._worker is None or not service._worker.is_alive()
        report["cases"].append({"case": "close-live-worker", "passed": passed, "result": service.snapshot()})
        if not passed:
            report["failures"].append("close-live-worker")
    finally:
        service.close()
        owned = []
        for item in engines:
            try:
                code = item["engine"].returncode.result(timeout=2)
                stopped = True
            except Exception:
                code, stopped = None, False
            owned.append({"pid": item["pid"], "name": item["name"], "exited": stopped, "exit_code": code})
        report["owned_processes"] = owned
        report["only_module_owned_engines_closed"] = True
        if not all(item["exited"] for item in owned):
            report["failures"].append("owned-process-cleanup")
        report['source_hashes_after'] = {str(path.relative_to(ROOT)): sha(path) for path in source_paths}
        report['source_unchanged'] = report['source_hashes_before'] == report['source_hashes_after']
        if not report['source_unchanged']:
            report['failures'].append('source-changed-during-smoke')
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        (OUTPUT / "native-smoke.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"cases": len(report["cases"]), "failures": report["failures"],
                      "owned_processes": len(report["owned_processes"]),
                      "all_owned_exited": all(item["exited"] for item in report["owned_processes"]),
                      "source_unchanged": report["source_unchanged"],
                      "scores": [{"case": row["case"], "cp": row["result"].get("cp"),
                                  "depth": row["result"].get("depth"), "reason": row["result"].get("reason")}
                                 for row in report["cases"]]}, ensure_ascii=False))
    if report["failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
