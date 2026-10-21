"""Run Godot undo/handicap controls against a private, actual Regicide backend.

These are synthesized native-scene inputs, not human manual-play validation.
Usage: python tests/handicap_godot_tests.py --godot GODOT --binary REGICIDE
"""

import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import threading

import chess.engine

import godot_integration_tests as integration


ROOT = integration.ROOT
backend = integration.backend


class RecordingEngine:
    def __init__(self, binary, searches):
        self.peer = chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=10)
        self.id = self.peer.id
        self.searches = searches

    def play(self, board, limit, **kwargs):
        self.searches.append({"history": [move.uci() for move in board.move_stack],
                              "initial_fen": board.root().fen(en_passant="fen"),
                              "fen": board.fen(en_passant="fen"),
                              "requested_seconds": limit.time})
        return self.peer.play(board, limit, **kwargs)

    def quit(self):
        return self.peer.quit()

    def close(self):
        return self.peer.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "build/handicap-godot-validation")
    parser.add_argument("--rendered", action="store_true")
    args = parser.parse_args()
    args.godot = args.godot.resolve(strict=True)
    args.binary = args.binary.resolve(strict=True)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    imported = subprocess.run([str(args.godot), "--headless", "--path", str(ROOT / "godot"),
                               "--editor", "--import"], cwd=ROOT, text=True,
                              encoding="utf-8", errors="replace", stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=90)
    (args.output / "import.log").write_text(imported.stdout, encoding="utf-8")
    if imported.returncode or "SCRIPT ERROR:" in imported.stdout or "ERROR:" in imported.stdout:
        raise AssertionError(f"Godot import failed:\n{imported.stdout}")
    searches = []
    with tempfile.TemporaryDirectory(prefix="regicide-handicap-godot-") as temporary:
        temporary = Path(temporary)
        game = backend.HumanGame(args.binary, temporary / "games",
                                lambda binary: RecordingEngine(binary, searches))
        server = backend.HumanServer(game)
        server.RequestHandlerClass = integration.AuditedHandler
        server.audit = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = temporary / "private-backend.json"
        config.write_text(json.dumps({"url": server.origin, "token": server.token}), encoding="utf-8")
        try:
            integration.run_godot(args, "handicap_live_tests.gd",
                                  ["--backend-file", config, "--evidence-dir", args.output])
            routes = [entry["path"] for entry in server.audit if entry["method"] == "POST"]
            expected = ["/api/move", "/api/engine", "/api/handicap", "/api/forced-move",
                        "/api/move", "/api/engine", "/api/undo", "/api/new", "/api/engine",
                        "/api/handicap", "/api/forced-move", "/api/move", "/api/engine",
                        "/api/handicap", "/api/handicap-cancel", "/api/handicap", "/api/forced-move"]
            assert routes == expected, routes
            assert len(searches) == 4, searches
            assert searches[1]["history"] == ["e2e4", "f7f6", "d2d4"], searches
            assert searches[2]["history"] == [], searches
            assert searches[3]["history"] == ["f2f3", "e7e5"], searches
            assert all(search["requested_seconds"] == 1 for search in searches), searches
            assert all(entry["elapsed_seconds"] >= .5 for entry in server.audit
                       if entry["path"] == "/api/engine"), server.audit
            records = integration.verify_saved_games(temporary / "games")
            forced_moves = game.state()["moves"]
            assert len(forced_moves) == 1 and forced_moves[0]["uci"] == "g2g4"
            assert forced_moves[0]["forced"] is True
            assert not game.error
            result = {"mode": "native rendered synthetic input" if args.rendered else "headless synthetic input",
                      "engine": game.engine_name, "binary_sha256": game.binary_sha256,
                      "routes": server.audit, "actual_uci_search_inputs": searches,
                      "saved_games_independently_replayed": records,
                      "note": "No human manual-play validation is claimed."}
            (args.output / "handicap-integration-result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print("Godot handicap controls, both colors, flipped input, cancel, undo and actual UCI continuation passed.")
        finally:
            (args.output / "http-routes.json").write_text(
                json.dumps(server.audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            game.close()


if __name__ == "__main__":
    main()
