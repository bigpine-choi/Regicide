"""Headless in-game PGN chooser, real disk save/open, and isolated HTTP roundtrip.

No existing GUI process, personal game directory, or original checkout is used.
Synthetic mode replaces only HTTP; --binary also runs the real preview endpoint.
"""

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading

import chess
import chess.engine
import chess.pgn

import godot_integration_tests as integration


ROOT = integration.ROOT
backend = integration.backend


def run_scene(args, work_dir, label, extra=()):
    command = [str(args.godot), "--headless", "--path", str(ROOT / "godot"),
               "--script", "res://tests/pgn_file_tests.gd", "--", "--work-dir", str(work_dir),
               *map(str, extra)]
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=100)
    output = completed.stdout + completed.stderr
    (args.output / f"{label}.txt").write_text(output, encoding="utf-8")
    print(output, end="", flush=True)
    checks = re.findall(r"checks: (\d+), failures: (\d+)", output)
    passed = (completed.returncode == 0 and "SCRIPT ERROR:" not in output and "ERROR:" not in output
              and len(checks) == 1 and int(checks[0][0]) > 0 and int(checks[0][1]) == 0)
    if not passed:
        failure_dir = args.output / f"{label}-failure-artifacts"
        failure_dir.mkdir(exist_ok=True)
        for path in work_dir.iterdir():
            if path.is_file() and (path.suffix == ".pgn" or path.name == "scene-roundtrip.json"):
                shutil.copyfile(path, failure_dir / path.name)
        report_path = args.output / "report.json"
        failed = json.loads(report_path.read_text(encoding="utf-8"))
        failed.update({"status": "FAIL", "failed_suite": label, "failed_command": command,
                       "exit_code": completed.returncode, "checks": checks})
        report_path.write_text(json.dumps(failed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        raise AssertionError(output)
    return {"command": command, "exit_code": completed.returncode,
            "checks": int(checks[0][0]), "failures": int(checks[0][1])}


def independently_replay(text):
    stream = io.StringIO(text)
    game = chess.pgn.read_game(stream)
    assert game is not None and not game.errors
    assert chess.pgn.read_game(stream) is None, "Export must contain exactly one game"
    board = game.board()
    nodes = list(game.mainline())
    for node in nodes:
        assert node.move in board.legal_moves
        board.push(node.move)
    return {"uci_moves": [node.move.uci() for node in nodes],
            "final_fen": board.fen(en_passant="fen"), "result": game.headers["Result"],
            "root_fen": game.board().fen(en_passant="fen"),
            "headers": dict(game.headers), "comments": [node.comment for node in nodes]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "build/pgn-file-godot")
    args = parser.parse_args()
    args.godot = args.godot.resolve(strict=True)
    args.binary = args.binary.resolve(strict=True) if args.binary else None
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"status": "RUNNING", "mode": "headless automated in-game FileDialog controls and disk IO",
              "manual_play": False, "existing_gui_process_used": False,
              "personal_games_used": False, "engine_peers_started": 0, "engine_search_calls": 0}
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    imported = subprocess.run([str(args.godot), "--headless", "--editor", "--path", str(ROOT / "godot"),
                               "--import", "--quit"], cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=90)
    import_log = imported.stdout + imported.stderr
    (args.output / "import.txt").write_text(import_log, encoding="utf-8")
    assert imported.returncode == 0 and "SCRIPT ERROR:" not in import_log and "ERROR:" not in import_log, import_log
    report["headless_import_exit_code"] = imported.returncode
    with tempfile.TemporaryDirectory(prefix="regicide-pgn-file-godot-") as temporary:
        temporary = Path(temporary)
        synthetic_dir = temporary / "synthetic"
        synthetic_dir.mkdir()
        report["synthetic"] = run_scene(args, synthetic_dir, "godot-synthetic")
        for name in ("synthetic-selected.pgn", "retry-no-extension.pgn"):
            independently_replay((synthetic_dir / name).read_text(encoding="utf-8"))
            shutil.copyfile(synthetic_dir / name, args.output / name)
        if args.binary:
            peers = []

            class NoSearchPeer:
                def __init__(self, binary):
                    self.peer = chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=10)
                    peers.append(self.peer)
                    report["engine_peers_started"] += 1

                def __getattr__(self, name):
                    return getattr(self.peer, name)

                def play(self, *_args, **_kwargs):
                    report["engine_search_calls"] += 1
                    raise AssertionError("PGN file preview must never play or search a move")

                analysis = play

            def forbid_hint(_binary):
                raise AssertionError("PGN file preview must not launch a hint peer")

            folder = temporary / "games"
            game = backend.HumanGame(args.binary, folder, engine_factory=NoSearchPeer,
                                     hint_engine_factory=forbid_hint)
            server = None
            worker = None
            try:
                with game._lock:
                    game.board = chess.Board()
                    for uci in ("e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6"):
                        game.board.push_uci(uci)
                    game._forced_plies = {4}
                    game._engine_movetimes = {2: 100, 6: 500}
                    game.revision += 1
                    game._sync_evaluation()
                    game._save()
                original_pgn = game.pgn()
                original = independently_replay(original_pgn)
                before_fen = game.board.fen(en_passant="fen")
                before_revision = game.revision
                persisted_before = {path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()}
                server = backend.HumanServer(game)
                server.RequestHandlerClass = integration.AuditedHandler
                server.audit = []
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                config = temporary / "private-backend.json"
                config.write_text(json.dumps({"url": server.origin, "token": server.token}), encoding="utf-8")
                black_root_board = chess.Board("4k3/8/8/8/3Pp3/8/8/4K3 b - d3 0 2")
                assert black_root_board.is_valid()
                black_root_board.push_uci("e4d3")
                black_root_pgn = str(chess.pgn.Game.from_board(black_root_board)) + "\n\n"
                black_root_original = independently_replay(black_root_pgn)
                expected = temporary / "expected.json"
                expected.write_text(json.dumps({"pgn": original_pgn, "uci_moves": original["uci_moves"],
                                                "final_board": {chess.square_name(square): piece.symbol()
                                                                for square, piece in game.board.piece_map().items()},
                                                "black_root_final_board": {chess.square_name(square): piece.symbol()
                                                                           for square, piece in black_root_board.piece_map().items()}}),
                                    encoding="utf-8")
                live_dir = temporary / "live"
                live_dir.mkdir()
                bom_contents = b"\xef\xbb\xbf" + original_pgn.replace("\n", "\r\n").encode("utf-8")
                (live_dir / "bom-crlf-source.pgn").write_bytes(bom_contents)
                (live_dir / "black-root.pgn").write_text(black_root_pgn, encoding="utf-8", newline="\n")
                report["live"] = run_scene(args, live_dir, "godot-live",
                                             ["--backend-file", config, "--expected-file", expected])
                outputs = {}
                for name in ("live-selected.pgn", "roundtrip-reexport.pgn", "bom-crlf-source.pgn", "bom-crlf-reexport.pgn"):
                    path = live_dir / name
                    actual = independently_replay(path.read_text(encoding="utf-8"))
                    assert actual == original, {"file": name, "expected": original, "actual": actual}
                    outputs[name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                     "plies": len(actual["uci_moves"]), "result": actual["result"],
                                     "independent_replay": True}
                    shutil.copyfile(path, args.output / name)
                assert (live_dir / "bom-crlf-source.pgn").read_bytes() == bom_contents
                assert (live_dir / "bom-crlf-reexport.pgn").read_bytes() == bom_contents
                assert (live_dir / "black-root.pgn").read_bytes() == black_root_pgn.encode("utf-8")
                assert independently_replay((live_dir / "black-root.pgn").read_text(encoding="utf-8")) == black_root_original
                shutil.copyfile(live_dir / "black-root.pgn", args.output / "black-root.pgn")
                report["black_root"] = black_root_original
                report["scene_roundtrip"] = json.loads((live_dir / "scene-roundtrip.json").read_text(encoding="utf-8"))
                assert report["scene_roundtrip"]["source_unchanged"] and report["scene_roundtrip"]["live_state_unchanged"]
                assert game.board.fen(en_passant="fen") == before_fen and game.revision == before_revision
                assert [move.uci() for move in game.board.move_stack] == original["uci_moves"]
                persisted_after = {path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()}
                assert persisted_after == persisted_before, "Preview must neither rewrite nor add persisted games"
                posts = [row["path"] for row in server.audit if row["method"] == "POST"]
                assert posts == ["/api/pgn-preview"] * 3, posts
                assert report["engine_peers_started"] == 1 and report["engine_search_calls"] == 0
                report.update({"outputs": outputs, "live_position_and_revision_unchanged": True,
                               "persisted_games_unchanged": True, "routes": server.audit,
                               "binary_sha256": hashlib.sha256(args.binary.read_bytes()).hexdigest()})
            finally:
                if server is not None:
                    (args.output / "http-routes.json").write_text(json.dumps(server.audit, indent=2) + "\n", encoding="utf-8")
                    server.shutdown()
                    server.server_close()
                if worker is not None:
                    worker.join(timeout=5)
                game.close()
                report["engine_exit_codes"] = [peer.returncode.result(timeout=3) for peer in peers]
    report["status"] = "PASS"
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Godot PGN file chooser, save, read-only reopen and independent roundtrip: PASS")


if __name__ == "__main__":
    main()
