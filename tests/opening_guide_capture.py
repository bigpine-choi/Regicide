"""Capture the opening UI with actual catalog data in a hidden Windows helper.

The application renders into a 1280x900 SubViewport. This is an automated
rendered fixture, not a manually played game or a screenshot of the user's app.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

import chess
import godot_integration_tests as integration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.godot, args.binary, args.output = args.godot.resolve(strict=True), args.binary.resolve(strict=True), args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="regicide-opening-render-") as temporary:
        game = integration.backend.HumanGame(args.binary, Path(temporary) / "games")
        try:
            with game._lock:
                game.board = chess.Board()
                for move in ("e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6"):
                    game.board.push_uci(move)
                game.revision += 1
                game._sync_evaluation()
                game._save()
            state = game.state()
            assert state["opening_guide"]["current_matches"], state["opening_guide"]
            state_file = args.output / "actual-catalog-state.json"
            state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        finally:
            game.close()
    command = [str(args.godot), "--path", str(integration.ROOT / "godot"), "--script",
               "res://tests/opening_guide_capture.gd", "--", "--state-file", str(state_file), "--output", str(args.output)]
    startup = {}
    if os.name == "nt":
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = 0
        startup = {"startupinfo": info, "creationflags": subprocess.CREATE_NO_WINDOW}
    completed = subprocess.run(command, cwd=integration.ROOT, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=40, **startup)
    text = completed.stdout + completed.stderr
    (args.output / "capture.txt").write_text(text, encoding="utf-8")
    print(text, end="")
    assert completed.returncode == 0 and "ERROR:" not in text, text
    result = json.loads((args.output / "capture-result.json").read_text(encoding="utf-8"))
    assert result["success"] and result["live_position_unchanged"], result
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
