"""Run the real Godot scene against an isolated, real Regicide backend.

The Godot scripts synthesize input events; this is automated scene validation,
not a claim that a person manually played through the application. The existing
human_play_tests.py remains the independent rules, HTTP and PGN test suite.

Usage: python tests/godot_integration_tests.py --godot GODOT --binary REGICIDE
Add --rendered to exercise a native rendering window and capture screenshots.
"""

import argparse
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import chess
import chess.pgn


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("regicide_godot_backend", ROOT / "scripts/play_human.py")
backend = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = backend
SPEC.loader.exec_module(backend)


class AuditedHandler(backend.HumanHandler):
    """Record route names only; session tokens must never enter evidence files."""

    def do_GET(self):
        self.server.audit.append({"method": "GET", "path": self.path})
        super().do_GET()

    def do_POST(self):
        entry = {"method": "POST", "path": self.path, "started": time.monotonic()}
        self.server.audit.append(entry)
        if self.path == "/api/move" and getattr(self.server, "move_delay", 0):
            time.sleep(self.server.move_delay)
        super().do_POST()
        entry["elapsed_seconds"] = time.monotonic() - entry["started"]


def run_godot(args, script, extra_args=(), label=None):
    command = [str(args.godot), "--path", str(ROOT / "godot")]
    if not args.rendered:
        command.append("--headless")
    command += ["--script", f"res://tests/{script}", "--", *map(str, extra_args)]
    completed = subprocess.run(command, cwd=ROOT, text=True, encoding="utf-8",
                               errors="replace", stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=90)
    (args.output / f"{label or Path(script).stem}.log").write_text(completed.stdout, encoding="utf-8")
    print(completed.stdout, end="")
    if completed.returncode or "SCRIPT ERROR:" in completed.stdout or "ERROR:" in completed.stdout:
        raise AssertionError(f"Godot {script} failed (exit {completed.returncode})")


def verify_saved_games(folder):
    records = []
    for path in sorted(folder.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        board = chess.Board(record["initial_fen"])
        for uci in record["uci_moves"]:
            move = chess.Move.from_uci(uci)
            assert move in board.legal_moves, (path.name, uci, board.fen())
            board.push(move)
        assert board.fen(en_passant="fen") == record["final_fen"], path.name
        pgn = chess.pgn.read_game(io.StringIO(path.with_suffix(".pgn").read_text(encoding="utf-8")))
        assert pgn is not None and not pgn.errors, path.name
        assert list(pgn.mainline_moves()) == list(board.move_stack), path.name
        records.append({"plies": len(board.move_stack), "result": record["state"]["result"],
                        "human_color": record["state"]["human_color"]})
    assert records, "No games were saved"
    return records


def verify_close(args, mode):
    with tempfile.TemporaryDirectory(prefix="regicide-godot-close-") as temporary:
        temporary = Path(temporary)
        game = backend.HumanGame(args.binary, temporary / "games")
        server = backend.HumanServer(game)
        server.RequestHandlerClass = AuditedHandler
        server.audit = []
        server.move_delay = .3 if mode == "pending" else 0
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = temporary / "backend.json"
        config.write_text(json.dumps({"url": server.origin, "token": server.token}), encoding="utf-8")
        try:
            run_godot(args, "close_backend_tests.gd",
                      ["--backend-file", config, "--own-backend", "--close-case", mode],
                      label="close-" + mode)
            thread.join(timeout=5)
            assert not thread.is_alive(), (mode, "Godot did not shut down its backend")
            routes = [entry["path"] for entry in server.audit if entry["method"] == "POST"]
            expected = {"ready": ["/api/shutdown"],
                        "busy": ["/api/move", "/api/engine", "/api/shutdown"],
                        "pending": ["/api/move", "/api/shutdown"]}[mode]
            assert routes == expected, (mode, routes)
            assert not game.error, (mode, game.error)
            expected_plies = {"ready": 0, "busy": 2, "pending": 1}[mode]
            assert len(game.board.move_stack) == expected_plies, (mode, game.state())
            records = verify_saved_games(temporary / "games")
            (args.output / f"close-{mode}.json").write_text(
                json.dumps({"mode": mode, "routes": server.audit, "saved_games": records}, indent=2) + "\n",
                encoding="utf-8")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            game.close()


def verify_fresh_checkout(args):
    # Users launch the app directly. Global class-name annotations must not
    # silently depend on an editor-created .godot cache in the developer tree.
    with tempfile.TemporaryDirectory(prefix="regicide-godot-fresh-") as temporary:
        project = Path(temporary) / "godot"
        shutil.copytree(ROOT / "godot", project, ignore=shutil.ignore_patterns(".godot"))
        completed = subprocess.run([str(args.godot), "--headless", "--path", str(project),
                                    "--quit-after", "3"], cwd=ROOT, text=True,
                                   encoding="utf-8", errors="replace", stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, timeout=30)
        (args.output / "fresh-checkout.log").write_text(completed.stdout, encoding="utf-8")
        if completed.returncode or "SCRIPT ERROR:" in completed.stdout or "ERROR:" in completed.stdout:
            raise AssertionError(f"Fresh Godot launch failed (exit {completed.returncode})\n{completed.stdout}")
        print("Godot fresh checkout launches without an editor cache.")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "build/godot-validation")
    parser.add_argument("--rendered", action="store_true")
    args = parser.parse_args()
    args.godot = args.godot.resolve(strict=True)
    args.binary = args.binary.resolve(strict=True)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    verify_fresh_checkout(args)
    # Import before script execution so custom types and SVG assets are available
    # on a fresh checkout, without relying on an editor cache.
    imported = subprocess.run([str(args.godot), "--headless", "--path", str(ROOT / "godot"),
                               "--editor", "--import"], cwd=ROOT, text=True,
                              encoding="utf-8", errors="replace", stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=90)
    (args.output / "import.log").write_text(imported.stdout, encoding="utf-8")
    if imported.returncode or "SCRIPT ERROR:" in imported.stdout or "ERROR:" in imported.stdout:
        raise AssertionError(f"Godot import failed (exit {imported.returncode})\n{imported.stdout}")
    run_godot(args, "orientation_tests.gd", ["--evidence-dir", args.output])
    run_godot(args, "evaluation_bar_tests.gd")
    with tempfile.TemporaryDirectory(prefix="regicide-godot-") as temporary:
        temporary = Path(temporary)
        game = backend.HumanGame(args.binary, temporary / "games")
        server = backend.HumanServer(game)
        server.RequestHandlerClass = AuditedHandler
        server.audit = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = temporary / "backend.json"
        config.write_text(json.dumps({"url": server.origin, "token": server.token,
                                      "pid": 0, "engine_name": game.engine_name,
                                      "binary_sha256": game.binary_sha256}), encoding="utf-8")
        try:
            run_godot(args, "live_backend_tests.gd",
                      ["--backend-file", config, "--evidence-dir", args.output])
            records = verify_saved_games(temporary / "games")
            audit = server.audit.copy()
            routes = [entry["path"] for entry in audit if entry["method"] == "POST"]
            # Every authoritative mutation was explicitly requested by the test;
            # its many flips must not manufacture moves or extra backend calls.
            expected = ["/api/move", "/api/engine", "/api/undo", "/api/new",
                        "/api/engine", "/api/move", "/api/engine", "/api/resign"]
            assert routes == expected, routes
            searches = [entry for entry in audit if entry["path"] == "/api/engine"]
            assert len(searches) == 3
            assert all(entry.get("elapsed_seconds", 0) >= .5 for entry in searches), searches
            summary = {"mode": "native rendered synthetic input" if args.rendered else "headless synthetic input",
                       "engine": game.engine_name, "binary_sha256": game.binary_sha256,
                       "requested_engine_movetime_ms": 1000,
                       "routes": audit, "saved_games_independently_replayed": records,
                       "note": "No human manual-play validation is claimed."}
            (args.output / "integration-result.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print("Godot scene, live callbacks, both colors, busy flips and independent PGN replay passed.")
        finally:
            (args.output / "http-routes.json").write_text(
                json.dumps(server.audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            game.close()
    for mode in ("ready", "busy", "pending"):
        verify_close(args, mode)


if __name__ == "__main__":
    main()
