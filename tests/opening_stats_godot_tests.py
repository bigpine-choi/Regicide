"""Exercise the actual Godot analysis board and authenticated statistics API.

Uses an isolated three-game test corpus and the real Regicide process. No
Lichess request is made: an explicit empty service token tests setup guidance.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from opening_explorer import OpeningExplorer
from play_human import HumanGame, HumanServer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "build/opening-stats-validation")
    parser.add_argument("--rendered", action="store_true")
    args = parser.parse_args()
    args.godot = args.godot.resolve(strict=True)
    args.binary = args.binary.resolve(strict=True)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="regicide-opening-scene-") as temporary:
        folder = Path(temporary)
        games = folder / "games"
        games.mkdir()
        fixtures = [("1-0", "1. e4 e5 2. Nf3 Nc6 3. Bc4 Bc5 4. b4"),
                    ("0-1", "1. d4 d5 2. c4 e6"),
                    ("1/2-1/2", "1. e4 c5")]
        for index, (result, moves) in enumerate(fixtures):
            (games / f"fixture-{index}.pgn").write_text(
                f'[Event "Automated test fixture"]\n[Result "{result}"]\n\n{moves} {result}\n',
                encoding="utf-8")
        game = HumanGame(args.binary, games)
        server = HumanServer(game, opening_explorer=OpeningExplorer(games, token=""))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        before = game.state()
        saved_before = {path.name: path.read_bytes() for path in games.iterdir() if path.is_file()}
        configuration = folder / "backend.json"
        configuration.write_text(json.dumps({"url": server.origin, "token": server.token}), encoding="utf-8")
        try:
            command = [str(args.godot), "--path", str(ROOT / "godot")]
            if not args.rendered:
                command.append("--headless")
            command += ["--script", "res://tests/opening_stats_live_tests.gd", "--",
                        "--backend-file", str(configuration), "--evidence-dir", str(args.output)]
            completed = subprocess.run(command, cwd=ROOT, text=True, encoding="utf-8",
                                       errors="replace", capture_output=True, timeout=90)
            output = completed.stdout + completed.stderr
            (args.output / "opening-stats-live.log").write_text(output, encoding="utf-8")
            print(output, end="")
            if completed.returncode or "SCRIPT ERROR:" in output or "ERROR:" in output:
                raise AssertionError("Godot opening statistics integration failed")
            assert game.state() == before, "Analysis changed the live game"
            assert saved_before == {path.name: path.read_bytes() for path in games.iterdir() if path.is_file()}, \
                "Analysis changed the persistent game library"
            report = {"engine": before["engine"]["name"], "fixture_games": 3,
                      "live_game_unchanged": True, "saved_games_unchanged": True,
                      "external_requests": 0, "rendered": args.rendered}
            (args.output / "summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print("Live game and persistent PGNs unchanged; no external requests.")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
            game.close()


if __name__ == "__main__":
    main()
