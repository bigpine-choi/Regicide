"""Private actual-Stockfish and Godot coaching verification (no user store)."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]
import chess
from coaching_reference import CoachingReference
from coaching_reference_tests import payload
from coaching_godot_tests import fixture
import godot_integration_tests as integration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--stockfish", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    args.rendered = False
    paths = ["scripts/coaching_reference.py", "scripts/sparring_referee.py", "scripts/play_human.py",
             "godot/scripts/coaching_mode.gd", "godot/scripts/evaluation_bar.gd", "web/human-play/coaching.js"]
    hashes = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}
    service = CoachingReference(args.stockfish)
    receipts = []
    try:
        black_mate = payload(["f2f3", "e7e5", "g2g4"], request_id="black-mate")
        white_mate = payload(root=chess.Board(black_mate["fen"]).mirror().fen(en_passant="fen"), request_id="white-mate")
        cases = [("white-root", payload(request_id="white")),
                 ("black-root", payload(["e2e4"], request_id="black")),
                 ("black-mate", black_mate), ("white-mate", white_mate),
                 ("threefold-claimable", payload(["g1f3", "g8f6", "f3g1", "f6g8"] * 2, request_id="repetition")),
                 ("fifty-move-claimable", payload(root="r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 100 60", request_id="fifty")),
                 ("checkmate", payload(["f2f3", "e7e5", "g2g4", "d8h4"], request_id="terminal"))]
        for label, request in cases:
            value = service.select(request)
            deadline = time.monotonic() + 5
            while value["status"] == "loading" and time.monotonic() < deadline:
                time.sleep(.03)
                value = service.snapshot()
            assert value["status"] in ("ready", "terminal"), (label, value)
            assert value["fen"] == request["fen"]
            assert "pv_uci" not in value and "uci" not in value
            if label.endswith("-mate"):
                assert value["mate"] == (-1 if label == "black-mate" else 1), value
            if label.endswith("claimable"):
                assert value["status"] == "ready" and "result" not in value, value
            receipts.append({"case": label, "request": request, "reference": value})
        cached = service.select(black_mate)
        assert cached.get("cached") and cached["mate"] == -1
        hidden = service.select({"request_id": "black-mate", "hidden": True})
        assert hidden["status"] == "hidden" and hidden["cp"] is None and hidden["mate"] is None
    finally:
        service.close()
    (args.output / "stockfish-smoke.json").write_text(json.dumps({"cases": receipts, "cache_verified": True,
        "retry_hidden": True, "binary_sha256": hashlib.sha256(args.stockfish.read_bytes()).hexdigest()}, ensure_ascii=False, indent=2), encoding="utf-8")
    fixture_file = args.output / "native-fixture.json"
    fixture_file.write_text(json.dumps(fixture(), ensure_ascii=False, indent=2), encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="regicide-coaching-reference-qa-") as temporary:
        private = Path(temporary)
        project = private / "godot"
        shutil.copytree(ROOT / "godot", project, ignore=shutil.ignore_patterns(".godot"))
        config = project / "project.godot"
        config.write_text(config.read_text(encoding="utf-8").replace('config/name="Regicide"',
                         'config/name="Regicide Private Coaching Reference QA"'), encoding="utf-8")
        integration.ROOT = private
        common = ["--fixture", fixture_file, "--evidence-dir", args.output]
        integration.run_godot(args, "coaching_reference_tests.gd", common, "reference-godot")
        integration.run_godot(args, "coaching_tests.gd", common, "coaching-regressions")
        game = integration.backend.HumanGame(args.binary, private / "games", referee_binary=args.stockfish)
        game.new_game("white", 100, game.state()["revision"])
        with game._lock:
            for uci in ["f2f3", "e7e5", "g2g4", "d8h4"]:
                game.board.push_uci(uci)
            game.revision += 1
            game._sync_evaluation()
            game._save()
        server = integration.backend.HumanServer(game)
        server.RequestHandlerClass = integration.AuditedHandler
        server.audit = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        backend_file = private / "backend.json"
        backend_file.write_text(json.dumps({"url": server.origin, "token": server.token}), encoding="utf-8")
        initial = game.state()
        analysis = game.analysis.snapshot()
        coaching = game.coaching.snapshot()
        originals = {p.name: p.read_bytes() for p in game.save_dir.iterdir() if p.is_file()}
        try:
            integration.run_godot(args, "coaching_reference_tests.gd", [*common, "--backend-file", backend_file], "reference-live")
            assert game.coaching.snapshot() == coaching
            assert game.state() == initial and game.analysis.snapshot() == analysis
            assert originals == {p.name: p.read_bytes() for p in game.save_dir.iterdir() if p.is_file()}
            assert game.coaching_reference.snapshot()["status"] == "hidden"
            integration.run_godot(args, "coaching_tests.gd", [*common, "--backend-file", backend_file], "coaching-live-regressions")
            assert game.state() == initial and game.analysis.snapshot() == analysis
            assert originals == {p.name: p.read_bytes() for p in game.save_dir.iterdir() if p.is_file()}
            posts = [row["path"] for row in server.audit if row["method"] == "POST"]
            (args.output / "http-routes.json").write_text(json.dumps(server.audit, indent=2), encoding="utf-8")
            assert posts and set(posts) <= {"/api/pgn-preview", "/api/coaching", "/api/coaching-reference", "/api/opening-explorer"}, posts
            (args.output / "http-isolation.json").write_text(json.dumps({"live_state_unchanged": True,
                "native_analysis_unchanged": True, "coaching_reference_did_not_mutate_native_report": True,
                "original_archive_files_unchanged": True, "routes": server.audit}, indent=2), encoding="utf-8")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            game.close()
    (args.output / "source-hashes.json").write_text(json.dumps({"sources": hashes,
        "unchanged_during_validation": all(hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest for path, digest in hashes.items()),
        "regicide_sha256": hashlib.sha256(args.binary.read_bytes()).hexdigest()}, indent=2), encoding="utf-8")
    print("Actual Stockfish, private Godot scene and authenticated HTTP isolation passed.")


if __name__ == "__main__":
    main()
