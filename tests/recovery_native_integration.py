"""Same-position native comparison of old pacing and real recovery search.

Only the next turn is played. One prefix is an existing public project match;
the other is explicitly synthetic. No personal game files are opened.
"""
import argparse
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
import zipfile

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
OLD_COMMIT = "d871d04b76b6f6d120f0fbf5e1c0ba972e296940"
SOURCES = ("scripts/play_human.py", "scripts/engine_pacing.py", "scripts/sparring_play.py",
           "scripts/sparring_evaluation.py", "scripts/sparring_pacing.py",
           "tests/recovery_native_integration.py", "tests/recovery_host_tests.py")
FIXTURES = {
    "public_ahead": {
        "history": "b1c3 e7e5 g1f3 b8c6 e2e4 g8f6 f1b5 c6d4 e1g1 d4b5 c3b5 c7c6 b5c3 d8e7 d2d4 h7h6 d4e5 f6h5",
        "seed": "acffc291215f6f3ac8129b238d7d1bab",
        "provenance": "docs/matches/2026-11-02-sparring/raw-records.zip: ratings-1320-1700/game-001.json prefix18; historical +410cp is not reused as a current score",
    },
    "synthetic_behind": {
        "history": "e2e4 e7e5 g1f3 b8c6 f3e5 c6e5",
        "seed": "diagnostic-readonly-no-engine",
        "provenance": "Declared synthetic standard-start legal prefix: White loses a knight for one pawn. No user record.",
    },
}


def hashes(root=ROOT):
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in SOURCES if (root / name).is_file()}


class FixtureClock:
    def __init__(self):
        self.started = None

    def __call__(self):
        return 100.0 if self.started is None else 100.0 + time.monotonic() - self.started


class RecordedAnalysis:
    def __init__(self, native, record):
        self.native, self.record, self.started = native, record, time.monotonic()

    def __enter__(self):
        self.native.__enter__()
        return self

    def __exit__(self, *args):
        self.record["elapsed_ms"] = round((time.monotonic() - self.started) * 1000, 3)
        return self.native.__exit__(*args)

    def __iter__(self):
        for packet in self.native:
            if packet.get("pv") and packet.get("score") is not None:
                self.record.update(depth=packet.get("depth"),
                    cp_own=packet["score"].pov(self.record["turn_white"]).score(),
                    mate_own=packet["score"].pov(self.record["turn_white"]).mate(),
                    pv=[move.uci() for move in packet["pv"]])
            yield packet

    def __getattr__(self, name):
        return getattr(self.native, name)

    def wait(self):
        result = self.native.wait()
        self.record["native_bestmove"] = result.move.uci() if result.move else None
        return result


class RecordedEngine:
    def __init__(self, binary, searches):
        self.native = chess.engine.SimpleEngine.popen_uci([str(binary), "uci"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10)
        self.searches = searches
        self.native.configure({name: value for name, value in {"Threads": 1, "Hash": 16}.items()
                               if name in self.native.options})

    def __getattr__(self, name):
        return getattr(self.native, name)

    def analysis(self, board, limit, **options):
        record = {"ply": len(board.move_stack), "fen": board.fen(en_passant="fen"),
                  "turn_white": board.turn,
                  "requested_ms": round(limit.time * 1000, 3) if limit.time is not None else None,
                  "requested_depth": limit.depth,
                  "root_moves": None if options.get("root_moves") is None else
                      [move.uci() for move in options["root_moves"]]}
        self.searches.append(record)
        return RecordedAnalysis(self.native.analysis(board, limit, **options), record)


def audit(binary, board, move):
    """Fresh 2s reply search, recorded from the original engine's viewpoint."""
    child = board.copy(stack=True)
    child.push_uci(move)
    engine = RecordedEngine(binary, [])
    try:
        packet = engine.analyse(child, chess.engine.Limit(time=2.0))
        score = packet["score"].pov(board.turn)
        return {"requested_ms": 2000, "depth": packet.get("depth"),
                "cp_own": score.score(), "mate_own": score.mate(),
                "pv": [m.uci() for m in packet.get("pv", [])]}
    finally:
        engine.quit()


def worker(source_root, binary, case_name, output):
    sys.path.insert(0, str(source_root / "scripts"))
    import play_human
    fixture = FIXTURES[case_name]
    board = chess.Board()
    for uci in fixture["history"].split():
        assert chess.Move.from_uci(uci) in board.legal_moves
        board.push_uci(uci)
    assert not board.is_game_over(claim_draw=True)
    searches, waits, clock = [], [], FixtureClock()
    def wait(seconds, event):
        started = time.monotonic()
        event.wait(seconds)
        waits.append(round((time.monotonic() - started) * 1000, 3))
    with tempfile.TemporaryDirectory(prefix="regicide-recovery-native-game-") as folder:
        game = play_human.HumanGame(binary, Path(folder) / "games",
            engine_factory=lambda executable: RecordedEngine(executable, searches),
            opening_variety_enabled=False, monotonic=clock, pacing_wait=wait)
        try:
            game.new_game("black" if board.turn else "white", 1000, game.revision,
                          sparring_mode=True, time_control="10+0")
            game._opening_seed = fixture["seed"]
            for ply in range(len(board.move_stack)):
                game._clock.complete_move("white" if ply % 2 == 0 else "black", ply + 1)
            game.board = board.copy(stack=True)
            game._sync_evaluation()
            clock.started = time.monotonic()
            started = time.monotonic()
            state = game.engine_move(game.revision)
            elapsed_ms = round((time.monotonic() - started) * 1000, 3)
            assert state["error"] is None, state["error"]
            assert len(game.board.move_stack) == len(board.move_stack) + 1
            selected = state["last_move"]
            assert chess.Move.from_uci(selected) in board.legal_moves
            decision = game._sparring_decisions.get(len(board.move_stack) + 1)
            result = {"fixture": case_name, "provenance": fixture["provenance"],
                "history_uci": fixture["history"], "seed": fixture["seed"],
                "fen": board.fen(en_passant="fen"), "engine_color": "white" if board.turn else "black",
                "selected_uci": selected, "baseline_uci": searches[0]["native_bestmove"],
                "elapsed_ms": elapsed_ms, "cosmetic_wait_ms": sum(waits),
                "native_search_trace": searches, "decision": decision,
                "clock": state["clock"], "pacing": state["engine"]["pacing"],
                "legal_reply": True}
        finally:
            game.close()
            assert game._engine is None
    result["selected_child_audit"] = audit(binary, board, selected)
    cp = result["selected_child_audit"]["cp_own"]
    result["audit_within_100cp"] = cp is not None and abs(cp) <= 100
    result["engine_closed"] = True
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(output, old_commit):
    binary = Path(subprocess.check_output([sys.executable, str(ROOT / "scripts/runtime_engine.py"),
        "--resolve"], cwd=ROOT, text=True).strip()).resolve(strict=True)
    before, results = hashes(), []
    with tempfile.TemporaryDirectory(prefix="regicide-recovery-snapshot-") as folder:
        snapshot = Path(folder)
        archived = subprocess.check_output(["git", "archive", "--format=zip", old_commit,
                                            "scripts", "data/openings"], cwd=ROOT)
        with zipfile.ZipFile(io.BytesIO(archived)) as archive:
            for member in archive.infolist():
                destination = (snapshot / member.filename).resolve()
                assert destination.is_relative_to(snapshot.resolve())
            archive.extractall(snapshot)
        old_hashes = hashes(snapshot)
        for case_name in FIXTURES:
            for variant, source_root in (("old", snapshot), ("current", ROOT)):
                case_output = snapshot / f"{case_name}-{variant}.json"
                command = [sys.executable, str(Path(__file__).resolve()), "--worker",
                           "--source-root", str(source_root), "--binary", str(binary),
                           "--case", case_name, "--output", str(case_output)]
                completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=90)
                assert completed.returncode == 0, completed.stdout + completed.stderr
                case = json.loads(case_output.read_text(encoding="utf-8"))
                case["variant"] = variant
                case["worker_stdout"] = completed.stdout
                case["worker_stderr"] = completed.stderr
                results.append(case)
                receipt = (case.get("decision") or {}).get("balance_recovery", {})
                print(json.dumps({"case": case_name, "variant": variant,
                    "searches": len(case["native_search_trace"]), "elapsed_ms": case["elapsed_ms"],
                    "wait_ms": case["cosmetic_wait_ms"], "direction": receipt.get("direction"),
                    "reason": (case.get("decision") or {}).get("reason"),
                    "audit_cp": case["selected_child_audit"]["cp_own"]}), flush=True)
    after = hashes()
    assert before == after, "Sources changed during verification; rerun is required"
    current = [case for case in results if case["variant"] == "current"]
    for case in current:
        recovery = case["decision"].get("balance_recovery", {})
        assert recovery.get("active"), f"Fixture did not activate recovery: {case['fixture']}"
        assert len(case["native_search_trace"]) > 1
        assert case["cosmetic_wait_ms"] == 0
    document = {"schema": "regicide.balance-recovery-native.v1", "passed": True,
        "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "old_commit": old_commit, "current_base_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_sha256": before, "source_sha256_after": after, "old_source_sha256": old_hashes,
        "binary": str(binary), "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "conditions": {"threads": 1, "hash_mb": 16, "time_control": "10+0",
            "initial_clock_ms_each": 600000, "fresh_engine_per_case": True,
            "engine_move_calls": 4, "audit_child_ms": 2000,
            "platform": platform.platform(), "processor": platform.processor(),
            "logical_cpu_count": os.cpu_count(), "python": platform.python_version(),
            "external_load_controlled": False}, "cases": results,
        "limitations": "Four single-turn searches, not matches or an Elo study. Previous/current use identical position, seed, clocks, native binary and resources. Extra search time deliberately differs because that is the treatment. Native time-limited search can vary with load. Independent child scores use a fresh instance of the same Regicide binary, not an external Stockfish oracle; they are finite-depth estimates, not proof of minimax values or guaranteed balance. Synthetic deficit cannot necessarily be repaired against perfect opposition."}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": True, "cases": len(results), "output": str(output)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--old-commit", default=OLD_COMMIT)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--case", choices=tuple(FIXTURES))
    arguments = parser.parse_args()
    if arguments.worker:
        worker(arguments.source_root, arguments.binary, arguments.case, arguments.output)
    else:
        run(arguments.output.resolve(), arguments.old_commit)
