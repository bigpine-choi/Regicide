"""Opening presentation checks; optional real host uses only temporary games.

The live fixture prepares a legal Ruy Lopez history. The host starts its normal
main UCI peer; opening controls must never search or launch a hint peer. --rendered uses a hidden
Windows helper window to capture automated catalog presentation, not manual play.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading

import chess
import chess.engine
import godot_integration_tests as integration


ROOT = integration.ROOT
backend = integration.backend


def run_scene(args, script, extra=(), label=None):
    command = [str(args.godot), "--path", str(ROOT / "godot")]
    if not args.rendered:
        command.append("--headless")
    command += ["--script", f"res://tests/{script}", "--", "--evidence-dir", str(args.output), *map(str, extra)]
    options = {}
    if os.name == "nt" and args.rendered:
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        options = {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=90, **options)
    text = completed.stdout + completed.stderr
    (args.output / f"{label or Path(script).stem}.txt").write_text(text, encoding="utf-8")
    print(text, end="", flush=True)
    assert completed.returncode == 0 and "SCRIPT ERROR:" not in text and "ERROR:" not in text, text
    return {"script": script, "command": command, "exit_code": completed.returncode,
            "checks": re.findall(r"checks: (\d+), failures: (\d+)", text)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "build/opening-guide-godot")
    parser.add_argument("--rendered", action="store_true")
    args = parser.parse_args()
    args.godot = args.godot.resolve(strict=True)
    args.binary = args.binary.resolve(strict=True) if args.binary else None
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    result = {"status": "RUNNING", "rendered": args.rendered,
              "mode": "automated hidden rendered scene" if args.rendered else "headless automated scene",
              "manual_play": False, "engine_peers_started": 0, "engine_search_calls": 0, "suites": []}
    imported = subprocess.run([str(args.godot), "--headless", "--editor", "--path", str(ROOT / "godot"),
                               "--import", "--quit"], cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=90)
    import_text = imported.stdout + imported.stderr
    (args.output / "import.txt").write_text(import_text, encoding="utf-8")
    assert imported.returncode == 0 and "SCRIPT ERROR:" not in import_text and "ERROR:" not in import_text, import_text
    result["headless_import_exit_code"] = imported.returncode
    result["suites"].append(run_scene(args, "opening_guide_tests.gd", label="godot-synthetic"))
    if args.binary:
        with tempfile.TemporaryDirectory(prefix="regicide-opening-guide-godot-") as temporary:
            temporary = Path(temporary)

            peers = []

            class ReadOnlyPeer:
                def __init__(self, binary):
                    self.peer = chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=10)
                    result["engine_peers_started"] += 1
                    peers.append(self.peer)

                def __getattr__(self, name):
                    return getattr(self.peer, name)

                def play(self, *_args, **_kwargs):
                    result["engine_search_calls"] += 1
                    raise AssertionError("opening learning must not choose or search a move")

                analysis = play

            def forbid_hint(_binary):
                raise AssertionError("opening learning must not launch a recommendation peer")

            game = backend.HumanGame(args.binary, temporary / "games", engine_factory=ReadOnlyPeer,
                                     hint_engine_factory=forbid_hint)
            with game._lock:
                game.board = chess.Board()
                for move in ("e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6"):
                    game.board.push_uci(move)
                game.revision += 1
                game._sync_evaluation()
                game._save()
            before = game.board.fen(en_passant="fen")
            original_moves = [move.uci() for move in game.board.move_stack]
            server = backend.HumanServer(game)
            server.RequestHandlerClass = integration.AuditedHandler
            server.audit = []
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            config = temporary / "private-backend.json"
            config.write_text(json.dumps({"url": server.origin, "token": server.token}), encoding="utf-8")
            try:
                result["source"] = game.state()["opening_guide"]["source"]
                result["suites"].append(run_scene(args, "opening_guide_live_tests.gd",
                                                   ["--backend-file", config], label="godot-live"))
                assert game.board.fen(en_passant="fen") == before
                assert [move.uci() for move in game.board.move_stack] == original_moves
                assert not any(row["method"] == "POST" for row in server.audit), server.audit
                assert result["engine_peers_started"] == 1 and result["engine_search_calls"] == 0
                result["routes"] = server.audit
                result["fixture_uci"] = original_moves
                result["binary_sha256"] = hashlib.sha256(args.binary.read_bytes()).hexdigest()
                result["position_and_history_preserved"] = True
                result["personal_games_used"] = False
            finally:
                (args.output / "http-routes.json").write_text(json.dumps(server.audit, indent=2) + "\n", encoding="utf-8")
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)
                game.close()
                result["engine_exit_codes"] = [peer.returncode.result(timeout=3) for peer in peers]
    result["status"] = "PASS"
    (args.output / "report.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("Godot opening display, read-only preview and replay: PASS")


if __name__ == "__main__":
    main()
