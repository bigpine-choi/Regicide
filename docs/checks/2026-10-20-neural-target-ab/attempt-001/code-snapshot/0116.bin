"""Isolated real-engine smoke for next-search time changes (optional Godot)."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import play_human as host


def run(args):
    result = {"started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "binary": str(args.binary.resolve()), "godot": str(args.godot) if args.godot else None,
              "checks": [], "engine_closed": False, "status": "FAIL", "rendered": args.rendered}
    with tempfile.TemporaryDirectory(prefix="regicide-think-time-live-") as folder:
        folder = Path(folder)
        game = host.HumanGame(args.binary, folder / "games")
        server, thread = None, None
        try:
            if args.godot:
                server = host.HumanServer(game)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                config = folder / "backend.json"
                config.write_text(json.dumps({"url": server.origin, "token": server.token}), encoding="utf-8")
                command = [str(args.godot), "--path", str(ROOT / "godot")]
                if not args.rendered:
                    command.append("--headless")
                command += ["--script", "res://tests/think_time_live_tests.gd", "--", "--backend-file", str(config)]
                if args.rendered:
                    command += ["--evidence-dir", str(args.output.resolve().parent)]
                completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                           encoding="utf-8", errors="replace", timeout=60)
                result["godot_stdout"] = completed.stdout
                result["godot_stderr"] = completed.stderr
                if completed.returncode or "SCRIPT ERROR:" in completed.stdout + completed.stderr or "ERROR:" in completed.stdout + completed.stderr:
                    raise AssertionError(f"Godot think-time checks failed: {completed.returncode}")
                result["checks"].append("Godot scene live time settings and search callbacks")
                if args.rendered:
                    screenshot = args.output.resolve().parent / "thinking-time-next-move.png"
                    assert screenshot.is_file()
                    result["screenshot"] = {"path": str(screenshot),
                                            "sha256": hashlib.sha256(screenshot.read_bytes()).hexdigest()}
            else:
                state = game.new_game("white", 1000, game.revision)
                state = game.human_move("e2e4", state["revision"])
                with ThreadPoolExecutor(max_workers=1) as worker:
                    search = worker.submit(game.engine_move, state["revision"])
                    deadline = time.monotonic() + 3
                    while not game.state()["engine_thinking"] and time.monotonic() < deadline:
                        time.sleep(.005)
                    current = game.state()
                    assert current["engine_thinking"] and current["engine"]["active_movetime_ms"] == 1000
                    changed = game.set_think_time(100, current["game_id"], current["engine"]["settings_revision"])
                    assert not search.done() and changed["revision"] == state["revision"]
                    assert changed["engine"]["active_movetime_ms"] == 1000
                    record = json.loads((game.save_dir / (game.game_id + ".json")).read_text(encoding="utf-8"))
                    assert record["state"]["engine"]["movetime_ms"] == 100
                    state = search.result(timeout=5)
                assert state["human_to_move"] and state["error"] is None
                legal = state["legal_moves"][0]
                state = game.human_move(legal, state["revision"])
                state = game.engine_move(state["revision"])
                assert state["human_to_move"] and state["error"] is None
                record = json.loads((game.save_dir / (game.game_id + ".json")).read_text(encoding="utf-8"))
                assert record["engine_movetimes_ms"] == {"2": 1000, "4": 100}
                assert len(state["moves"]) == 4
                result["checks"] += ["real active search retains 1000 ms", "100 ms next budget saved during search",
                                     "same game continues with legal engine replies", "per-move requested times saved"]
            result["binary_sha256"] = game.binary_sha256
            result["status"] = "PASS"
        finally:
            if server:
                server.shutdown()
                server.server_close()
            if thread:
                thread.join(timeout=5)
                assert not thread.is_alive()
            game.close()
            result["engine_closed"] = game._engine is None
    result["finished_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--godot", type=Path)
    parser.add_argument("--rendered", action="store_true", help="Render a native Godot window and capture the current/next budgets")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.rendered and not args.godot:
        parser.error("--rendered requires --godot")
    result = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
