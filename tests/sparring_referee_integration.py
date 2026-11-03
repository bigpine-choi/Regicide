"""Actual local Regicide + Stockfish + HTTP/native functional verification.

Only temporary games and explicitly selected binaries are used. This is not a
match, a playing-strength comparison, an Elo estimate, or a guarantee of balance.
The referee is real; no engine result or HTTP response is replaced by a fixture.
"""

import argparse
import contextlib
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import chess
import chess.engine
from game_archive import GameArchive
import play_human
from referee_engine import resolve_referee_engine
from runtime_engine import resolve_runtime_engine
from sparring_referee import SparringReferee, POLICY, SOURCE

SOURCES = ("scripts/play_human.py", "scripts/sparring_referee.py", "scripts/referee_engine.py",
           "scripts/game_archive.py", "godot/scripts/main.gd", "web/human-play/app.js",
           "tests/sparring_referee_integration.py", "godot/tests/sparring_referee_live_tests.gd")


class NoOpeningQueries:
    """This verification never reads a user's Lichess credentials or cache."""
    def close(self):
        pass


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(binary, referee_binary, output, godot=None, rendered=False):
    output.mkdir(parents=True, exist_ok=False)
    before = {name: digest(ROOT / name) for name in SOURCES}
    checks, engines, colors = [], [], []
    engines_lock = threading.Lock()
    report = {"scope": __doc__, "passed": False, "checks": checks, "colors": colors,
              "binary": str(binary), "binary_sha256": digest(binary),
              "referee_binary": str(referee_binary), "referee_binary_sha256": digest(referee_binary),
              "source_sha256": before, "conditions": {"temporary_games": True,
                "opening_variety": False, "dynamic_play": True,
                "referee_threads": 1, "referee_hash_mb": 32,
                "engine_wait": "artificial pacing waits explicitly skipped through the practice API"}}

    def verify(ok, message):
        if not ok:
            raise AssertionError(message)
        checks.append(message)

    def factory(role):
        def create(path):
            command = [str(path)] + (["uci"] if role == "Regicide" else [])
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            engine = chess.engine.SimpleEngine.popen_uci(command, timeout=5, **options)
            with engines_lock:
                engines.append({"role": role, "pid": engine.transport.get_pid(),
                                "name": engine.id.get("name"), "engine": engine})
            return engine
        return create

    def identity(state):
        return {key: state[key] for key in ("game_id", "revision", "fen", "turn")}

    server = game = referee = thread = None
    temporary_owner = tempfile.TemporaryDirectory(prefix="regicide-real-referee-")
    try:
        with contextlib.nullcontext(temporary_owner.name) as folder:
            temporary = Path(folder)
            info = resolve_referee_engine(ROOT, referee_binary)
            verify(info["available"], "selected local Stockfish executable is available")
            referee = SparringReferee(referee_binary, engine_factory=factory("Stockfish"))
            own_factory = factory("Regicide")
            game = play_human.HumanGame(binary, temporary / "games", engine_factory=own_factory,
                evaluation_engine_factory=own_factory, hint_engine_factory=own_factory,
                opening_variety_enabled=False, dynamic_play_enabled=True,
                referee=referee, referee_info=info)
            server = play_human.HumanServer(game, explorer=NoOpeningQueries())
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def request(path, body=None, timeout=90):
                req = urllib.request.Request(server.origin + path,
                    data=json.dumps(body).encode("utf-8") if body is not None else None,
                    headers={"X-Regicide-Token": server.token, "Origin": server.origin,
                             "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    data = response.read()
                    return json.loads(data) if response.headers.get("Content-Type", "").startswith("application/json") else data.decode("utf-8")

            def reference_for(state, timeout=14, require_own=True):
                started = time.monotonic()
                sample = None
                while time.monotonic() - started < timeout:
                    envelope = request("/api/evaluation")
                    verify(identity(envelope) == identity(state), "own evaluation envelope identifies current board")
                    sample = envelope.get("reference_evaluation", {})
                    if sample.get("status") == "ready":
                        verify(identity(sample) == identity(state), "reference identity matches game/revision/FEN/turn")
                        verify(sample.get("source") == SOURCE and sample.get("bound") == "exact" and sample.get("depth", 0) > 0,
                               "real Stockfish supplies an exact completed-depth packet")
                        verify(sample.get("binary_sha256") == report["referee_binary_sha256"], "reference binary hash is independent and pinned to selected executable")
                        verify(type(sample.get("cp")) is int or type(sample.get("mate")) is int, "reference score preserves cp/mate separation")
                        verify(envelope.get("source") != SOURCE, "Stockfish does not overwrite own root score channel")
                        if not require_own or envelope.get("cp") is not None or envelope.get("mate") is not None:
                            return {"elapsed_ms_from_poll": round((time.monotonic() - started) * 1000, 3),
                                    "own": {key: envelope.get(key) for key in ("cp", "mate", "depth", "source", "status", "bound")},
                                    "reference": sample, "metadata": envelope["sparring_referee"]}
                    if sample.get("status") in ("unavailable", "error"):
                        raise AssertionError("Real referee did not finish: " + str(sample.get("message")))
                    time.sleep(.03)
                raise AssertionError("No completed real Stockfish reference within bounded wait; final status=" + str(sample.get("status") if sample else None))

            def engine_turn(state):
                start_board = chess.Board(state["fen"])
                started = time.monotonic()
                skipped = False
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(request, "/api/engine", {"revision": state["revision"]})
                    while not future.done():
                        current = request("/api/state")
                        if not skipped and current.get("engine_thinking") and current["revision"] == state["revision"]:
                            request("/api/engine/skip-wait", {"revision": current["revision"]})
                            skipped = True
                        time.sleep(.03)
                    result = future.result()
                verify(result.get("error") is None and result["result"] == "*", "real Regicide engine completes a nonterminal practice move")
                chosen = chess.Move.from_uci(result["moves"][-1]["uci"])
                verify(chosen in start_board.legal_moves, "actual opponent move is legal on its original board")
                start_board.push(chosen)
                verify(start_board.fen(en_passant="fen") == result["fen"], "actual HTTP move and serialized position agree")
                return result, {"uci": chosen.uci(), "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                                "pacing_wait_skipped": skipped}

            state = request("/api/state")
            for color in ("white", "black"):
                state = request("/api/new", {"revision": state["revision"], "color": color,
                    "sparring_mode": True, "time_control": "10+5"})
                # Before White's first opponent move the own search has not
                # started; a real reference may correctly precede its score.
                initial = reference_for(state, require_own=bool(state["human_to_move"]))
                verify(state["engine"]["sparring_referee"]["enabled"], "sparring state enables separate referee metadata")
                if color == "white":
                    state = request("/api/move", {"revision": state["revision"], "move": "e2e4"})
                state, moved = engine_turn(state)
                after = reference_for(state)
                if color == "black":
                    human_move = state["legal_moves"][0]
                    verify(chess.Move.from_uci(human_move) in chess.Board(state["fen"]).legal_moves, "Black human input is a legal generated move")
                    state = request("/api/move", {"revision": state["revision"], "move": human_move})
                    state, second = engine_turn(state)
                    after = reference_for(state)
                    moved["second_opponent_move"] = second
                same = request("/api/state")
                verify(identity(same) == identity(state) and same["moves"] == state["moves"], "reference analysis leaves live position and history unchanged")
                pgn = request("/api/pgn")
                verify('[AssistedSparring "true"]' in pgn and '[SparringReferee "Stockfish' in pgn,
                       "live PGN distinguishes assisted sparring and Stockfish identity")
                verify('[EngineSHA256 "' + report["binary_sha256"] + '"]' in pgn and
                       '[RefereeBinarySHA256 "' + report["referee_binary_sha256"] + '"]' in pgn,
                       "PGN retains separate opponent/referee binary identities")
                detail = GameArchive(game.save_dir).get_game(state["game_id"])
                verify(detail["summary"]["assisted_sparring"] and detail["summary"]["sparring_referee"]["binary_sha256"] == report["referee_binary_sha256"],
                       "archive independently labels assisted practice without replacing opponent")
                (output / f"{color}.pgn").write_text(pgn, encoding="utf-8")
                record = json.loads((game.save_dir / f"{state['game_id']}.json").read_text(encoding="utf-8"))
                (output / f"{color}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                old = identity(state)
                state = request("/api/undo", {"revision": state["revision"]})
                immediate = request("/api/evaluation")
                verify(state["revision"] > old["revision"] and identity(immediate) == identity(state), "undo advances revision and invalidates own envelope")
                verify(identity(immediate["reference_evaluation"]) == identity(state), "undo never exposes the old referee packet as the new position")
                reference_for(state)
                colors.append({"human_color": color, "initial": initial, "after_move": after,
                               "opponent_move": moved, "undo_invalidated": True,
                               "archive_identity_preserved": True,
                               "recorded_screen_reasons": [row.get("reason") for row in record.get("sparring_referee", {}).get("decisions", [])]})

            if godot is not None:
                state = request("/api/new", {"revision": state["revision"], "color": "white",
                    "sparring_mode": True, "time_control": "10+5"})
                reference_for(state)
                startup = temporary / "native-startup.json"
                startup.write_text(json.dumps({"url": server.origin, "token": server.token}), encoding="utf-8")
                command = [str(godot), "--path", str(ROOT / "godot")]
                command += ["--rendering-method", "gl_compatibility"] if rendered else ["--headless"]
                command += ["--script", "res://tests/sparring_referee_live_tests.gd", "--", "--backend-file", str(startup),
                            "--evidence-dir", str(output / "native")]
                completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=40,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                (output / "native.stdout.log").write_text(completed.stdout, encoding="utf-8")
                (output / "native.stderr.log").write_text(completed.stderr, encoding="utf-8")
                verify(completed.returncode == 0 and "failures: 0" in completed.stdout,
                       "actual native scene consumes real HTTP reference and preserves game")
                report["native"] = {"passed": True, "rendered": rendered, "stdout": completed.stdout}
                state = request("/api/state")

            state = request("/api/new", {"revision": state["revision"], "color": "white",
                "sparring_mode": False, "boss_mode": False, "movetime_ms": 100})
            ordinary = request("/api/evaluation")
            verify(not state["engine"]["sparring_referee"]["enabled"], "normal mode disables referee independently of installed binary")
            verify("reference_evaluation" not in ordinary and "sparring_referee" not in ordinary,
                   "normal evaluation preserves legacy own-only schema without previous sparring reference")
            normal_pgn = request("/api/pgn")
            verify('[AssistedSparring "true"]' not in normal_pgn, "normal PGN is not mislabeled as assisted practice")
            request("/api/shutdown", {"revision": state["revision"]})
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            game.close()
            verify(not thread.is_alive(), "temporary HTTP server shuts down")
            end = time.monotonic() + 6
            while time.monotonic() < end:
                with engines_lock:
                    waiting = [entry for entry in engines if not entry["engine"].returncode.done()]
                if not waiting:
                    break
                time.sleep(.03)
            verify(not waiting, "every engine process created by this harness has exited")
            report["engine_processes"] = [{"role": entry["role"], "pid": entry["pid"], "name": entry["name"],
                "returncode": entry["engine"].returncode.result(timeout=.1)} for entry in engines]
            report["passed"] = True
    except Exception as error:
        report["failure"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        cleanup_errors = []
        operations = []
        if server is not None:
            if thread is not None and thread.is_alive():
                operations.append(("server.shutdown", server.shutdown))
            operations.append(("server.close", server.server_close))
        if game is not None:
            operations.append(("game.close", game.close))
        if referee is not None:
            operations.append(("referee.close", referee.close))
        for label, operation in operations:
            try:
                operation()
            except Exception as error:
                cleanup_errors.append({"operation": label, "error": type(error).__name__})
        with engines_lock:
            for entry in engines:
                entry["engine"].close()
        for entry in engines:
            try:
                entry["engine"].returncode.result(timeout=3)
            except Exception as error:
                cleanup_errors.append({"operation": "engine.exit", "pid": entry["pid"], "error": type(error).__name__})
        report["cleanup_errors"] = cleanup_errors
        report["source_sha256_after"] = {name: digest(ROOT / name) for name in SOURCES}
        report["source_unchanged"] = before == report["source_sha256_after"]
        report["passed"] = bool(report["passed"] and report["source_unchanged"] and not cleanup_errors)
        (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary_owner.cleanup()
    print(json.dumps({"passed": report["passed"], "colors": len(colors), "checks": len(checks),
                      "source_unchanged": report["source_unchanged"], "native": bool(godot)}, ensure_ascii=False))
    if not report["passed"]:
        raise AssertionError("Source changed during real-engine integration; repeat on a frozen source version")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--referee-engine", type=Path, required=True)
    parser.add_argument("--godot", type=Path)
    parser.add_argument("--rendered", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run((args.binary or resolve_runtime_engine(ROOT)).resolve(strict=True), args.referee_engine.resolve(strict=True),
        args.output.resolve(), args.godot.resolve(strict=True) if args.godot else None, args.rendered)
