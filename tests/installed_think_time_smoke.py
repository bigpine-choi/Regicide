"""Private installed-client smoke: fixed model, streamed score, hints and time.

Starts real engines and Godot only when explicitly invoked. Uses temporary
games and the existing optional model without changing desktop launch settings.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import play_human as host


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class RecordingEngine:
    def __init__(self, binary, role, records, owned):
        self.peer = chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=10)
        self.role, self.records = role, records
        owned.append(self)

    def __getattr__(self, name):
        return getattr(self.peer, name)

    def configure(self, options):
        self.records.append({"role": self.role, "kind": "configure",
                             "options": dict(options),
                             "session_model_sha256": digest(options["EvalFile"])})
        return self.peer.configure(options)

    def note(self, kind, board, limit):
        self.records.append({"role": self.role, "kind": kind, "seconds": limit.time,
                             "history": [move.uci() for move in board.move_stack]})

    def analysis(self, board, limit, **kwargs):
        self.note("analysis", board, limit)
        return self.peer.analysis(board, limit, **kwargs)

    def play(self, board, limit, **kwargs):
        self.note("play", board, limit)
        return self.peer.play(board, limit, **kwargs)


def run(args):
    started = time.monotonic()
    result = {"started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "status": "FAIL", "binary": str(args.binary), "binary_sha256": digest(args.binary),
              "explicit_existing_model": str(args.model), "model_sha256": digest(args.model),
              "model_selection_scope": "Explicit existing optional model in a private smoke; desktop default remains unchanged.",
              "rendered": args.rendered, "engine_calls": [], "engines_closed": False}
    game = server = thread = None
    owned = []
    with tempfile.TemporaryDirectory(prefix="regicide-installed-ui-smoke-") as temporary:
        temporary = Path(temporary)
        try:
            game = host.HumanGame(args.binary, temporary / "games",
                                  engine_factory=lambda binary: RecordingEngine(binary, "opponent", result["engine_calls"], owned),
                                  hint_engine_factory=lambda binary: RecordingEngine(binary, "hints", result["engine_calls"], owned),
                                  neural_model=args.model)
            server = host.HumanServer(game)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            config = temporary / "backend.json"
            config.write_text(json.dumps({"url": server.origin, "token": server.token}), encoding="utf-8")
            scene_result = temporary / "scene-result.json"
            command = [str(args.godot), "--path", str(ROOT / "godot")]
            if not args.rendered:
                command.append("--headless")
            command += ["--script", "res://tests/installed_think_time_smoke.gd", "--", "--backend-file", str(config),
                        "--expected-binary-sha", result["binary_sha256"], "--expected-model-sha", result["model_sha256"],
                        "--smoke-result", str(scene_result), "--evidence-dir", str(args.output.parent)]
            remaining = max(1, 55 - (time.monotonic() - started))
            completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                       encoding="utf-8", errors="replace", timeout=remaining)
            result["godot_stdout"], result["godot_stderr"] = completed.stdout, completed.stderr
            assert completed.returncode == 0 and "SCRIPT ERROR:" not in completed.stdout + completed.stderr
            assert "ERROR:" not in completed.stdout + completed.stderr
            result["scene"] = json.loads(scene_result.read_text(encoding="utf-8"))
            assert result["scene"]["failures"] == 0
            configurations = [row for row in result["engine_calls"] if row["kind"] == "configure"]
            assert {row["role"] for row in configurations} == {"opponent", "hints"}
            assert all(row["session_model_sha256"] == result["model_sha256"] for row in configurations)
            assert len({row["options"]["EvalFile"] for row in configurations}) == 1
            assert all(row["options"]["UseNeuralEvaluation"] is True for row in configurations)
            main = [row for row in result["engine_calls"] if row["role"] == "opponent" and row["kind"] == "analysis"]
            hints = [row for row in result["engine_calls"] if row["role"] == "hints" and row["kind"] == "play"]
            assert len(main) == 1 and main[0]["seconds"] == 3 and main[0]["history"] == ["e2e4"]
            assert [row["seconds"] for row in hints] == [1, 5]
            assert not game.error and len(game.board.move_stack) == 2
            assert digest(args.binary) == result["binary_sha256"] and digest(args.model) == result["model_sha256"]
            result["status"] = "PASS"
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
            if thread is not None:
                thread.join(timeout=3)
            if game is not None:
                game.close()
                result["session_model_snapshot_cleaned"] = game._model_snapshot is None
            result["engine_exit_codes"] = []
            for engine in owned:
                try:
                    result["engine_exit_codes"].append(engine.peer.returncode.result(
                        timeout=max(.1, min(3, 60 - (time.monotonic() - started)))))
                except TimeoutError:
                    result["engine_exit_codes"].append(None)
            result["engines_closed"] = (game is not None and game._engine is None
                                         and all(code is not None for code in result["engine_exit_codes"]))
    result["elapsed_seconds"] = time.monotonic() - started
    result["finished_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" and result["engines_closed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rendered", action="store_true")
    args = parser.parse_args()
    args.binary, args.model, args.godot = [path.resolve(strict=True) for path in (args.binary, args.model, args.godot)]
    args.output = args.output.resolve()
    if args.output.exists():
        parser.error("Preserve the previous smoke result")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
