"""Godot game library replay against an isolated real Regicide HTTP host.

Automated scene controls and synthesized input, not human manual validation.
All persisted games and private connection credentials use a temporary folder.
"""

import argparse
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading

import chess
import chess.pgn

import godot_integration_tests as integration


ROOT = integration.ROOT
backend = integration.backend


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "build/archive-godot-validation")
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
    with tempfile.TemporaryDirectory(prefix="regicide-archive-godot-") as temporary:
        temporary = Path(temporary)
        folder = temporary / "games"
        seed = backend.HumanGame(args.binary, folder)
        try:
            seed.new_game("white", 100, seed.state()["revision"])
            seed.human_move("e2e4", seed.state()["revision"])
            seed.engine_move(seed.state()["revision"])
            seed.begin_handicap(2, seed.state()["revision"])
            seed.forced_move("f7f6", seed.state()["revision"])
            seed.human_move("d2d4", seed.state()["revision"])
            seed.engine_move(seed.state()["revision"])
            seed.resign(seed.state()["revision"])
            selected_id = seed.game_id
            selected_pgn = seed.pgn()
            board = seed.board.root()
            frames = []
            moves = seed.board.move_stack
            for ply in range(len(moves) + 1):
                frames.append({"board": {chess.square_name(square): piece.symbol()
                                         for square, piece in board.piece_map().items()},
                               "turn": "white" if board.turn else "black",
                               "last_move": moves[ply - 1].uci() if ply else None})
                if ply < len(moves):
                    board.push(moves[ply])
            seed.new_game("black", 100, seed.state()["revision"])
            seed.engine_move(seed.state()["revision"])
            seed.resign(seed.state()["revision"])
        finally:
            seed.close()
        # Older saved games can lack engine timing metadata. Exercise the real
        # HTTP null contract rather than a synthetic scene-only dictionary.
        selected_record = folder / (selected_id + ".json")
        legacy = json.loads(selected_record.read_text(encoding="utf-8"))
        legacy["state"]["engine"].pop("movetime_ms", None)
        selected_record.write_text(json.dumps(legacy), encoding="utf-8")
        saved_before_restart = {path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()}
        game = backend.HumanGame(args.binary, folder)
        server = backend.HumanServer(game)
        server.RequestHandlerClass = integration.AuditedHandler
        server.audit = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = temporary / "private-backend.json"
        config.write_text(json.dumps({"url": server.origin, "token": server.token}), encoding="utf-8")
        expected = temporary / "independent-frames.json"
        expected.write_text(json.dumps({"game_id": selected_id, "frames": frames}), encoding="utf-8")
        try:
            integration.run_godot(args, "archive_live_tests.gd",
                                  ["--backend-file", config, "--expected-file", expected,
                                   "--evidence-dir", args.output])
            routes = [entry["path"] for entry in server.audit if entry["method"] == "POST"]
            assert routes == ["/api/move", "/api/engine"], routes
            first_post = next(index for index, route in enumerate(server.audit) if route["method"] == "POST")
            browsing = server.audit[:first_post]
            assert all(route["method"] == "GET" for route in browsing), browsing
            assert any(route["path"] == "/api/games" for route in browsing), browsing
            assert any(route["path"] == "/api/games/" + selected_id for route in browsing), browsing
            for name, contents in saved_before_restart.items():
                assert (folder / name).read_bytes() == contents, name
            exported = chess.pgn.read_game(io.StringIO((args.output / "selected-game.pgn").read_text(encoding="utf-8")))
            original = chess.pgn.read_game(io.StringIO(selected_pgn))
            assert exported is not None and not exported.errors
            assert list(exported.mainline_moves()) == list(original.mainline_moves())
            assert exported.headers["Result"] == "0-1"
            assert "User-forced Regicide move" in str(exported)
            assert len(game.board.move_stack) == 2 and not game.error
            records = integration.verify_saved_games(folder)
            result = {"mode": "native rendered scene controls and synthetic input" if args.rendered else "headless scene controls and synthetic input",
                      "engine": game.engine_name, "binary_sha256": game.binary_sha256,
                      "selected_archive_plies": len(frames) - 1,
                      "routes": server.audit, "saved_games_independently_replayed": records,
                      "note": "No human manual-play validation is claimed. All game folders are temporary."}
            (args.output / "archive-integration-result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print("Godot persistent library, replay frames, both views, selected PGN export and live-game return passed.")
        finally:
            (args.output / "http-routes.json").write_text(
                json.dumps(server.audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            game.close()


if __name__ == "__main__":
    main()
