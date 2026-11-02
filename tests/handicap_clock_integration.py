"""Real HTTP/native continuation after replacing a Regicide move.

Uses only isolated temporary game storage. The six short opening cases cover
both human colors and all rapid controls; they are not complete timed games.
"""
import argparse
import datetime
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import play_human
from game_archive import GameArchive

SOURCES = ("scripts/play_human.py", "scripts/game_clock.py", "scripts/game_archive.py",
           "scripts/engine_pacing.py", "tests/handicap_clock_integration.py")


def hashes():
    return {name:hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}


class HandicapClockNativeTests(unittest.TestCase):
    binary = None
    receipts = []
    engine_closed = False

    def setUp(self):
        if self.binary is None:
            self.skipTest("Explicit --output execution resolves and starts the active native engine")
        self.directory = tempfile.TemporaryDirectory(prefix="regicide-handicap-clock-")
        self.game = play_human.HumanGame(self.binary, Path(self.directory.name) / "games",
                                         opening_variety_enabled=False)
        self.server = play_human.HumanServer(self.game)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.game.close()
        type(self).engine_closed = self.game._engine is None and not self.thread.is_alive()
        self.directory.cleanup()
        self.assertTrue(self.engine_closed)

    def request(self, path, body=None):
        request = urllib.request.Request(self.server.origin + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"X-Regicide-Token":self.server.token, "Origin":self.server.origin,
                     "Content-Type":"application/json"})
        with urllib.request.urlopen(request, timeout=70) as response:
            self.assertEqual(response.status, 200)
            return json.loads(response.read())

    def record(self):
        return json.loads((self.game.save_dir / (self.game.game_id + ".json")).read_text(encoding="utf-8"))

    def human(self, state, preferred=None):
        self.assertTrue(state["human_to_move"])
        move = preferred if preferred in state["legal_moves"] else state["legal_moves"][0]
        return self.request("/api/move", {"revision":state["revision"], "move":move})

    def engine(self, state):
        self.assertTrue(state["engine_to_move"])
        position = chess.Board(state["fen"])
        reply = self.request("/api/engine", {"revision":state["revision"]})
        self.assertIsNone(reply["error"])
        self.assertEqual(reply["result"], "*")
        move = chess.Move.from_uci(reply["last_move"])
        self.assertIn(move, position.legal_moves)
        position.push(move)
        self.assertEqual(reply["fen"], position.fen(en_passant="fen"))
        return reply

    def test_timed_sparring_survives_preview_cancel_and_replacement(self):
        for control, initial, increment in (("10+0",600000,0), ("10+5",600000,5000), ("15+10",900000,10000)):
            for color in ("white", "black"):
                with self.subTest(control=control, human=color):
                    current = self.request("/api/state")
                    state = self.request("/api/new", {"revision":current["revision"], "color":color,
                        "sparring_mode":True, "time_control":control})
                    with self.game._lock:
                        self.game._opening_seed = "handicap-clock-integration-" + color + "-" + control
                    if state["engine_to_move"]:
                        state = self.engine(state)
                    state = self.human(state, "e2e4" if color == "white" else "e7e5")
                    state = self.engine(state)
                    ply = len(state["moves"])
                    original_fen = state["fen"]
                    original_move = state["last_move"]
                    completed_clock = self.record()["clock"]["history"][-1]
                    preview = self.request("/api/handicap", {"revision":state["revision"], "ply":ply})
                    self.assertTrue(preview["handicap_mode"])
                    self.assertEqual(preview["clock"]["status"], "paused")
                    self.assertTrue(preview["clock"]["enabled"])
                    frozen = (preview["clock"]["white_ms"], preview["clock"]["black_ms"])
                    time.sleep(.08)
                    paused = self.request("/api/state")
                    self.assertEqual(frozen, (paused["clock"]["white_ms"], paused["clock"]["black_ms"]))
                    cancelled = self.request("/api/handicap-cancel", {"revision":paused["revision"]})
                    self.assertEqual(cancelled["fen"], original_fen)
                    self.assertEqual(cancelled["clock"]["running_color"], color)
                    self.assertEqual(cancelled["clock"]["control_id"], control)
                    preview = self.request("/api/handicap", {"revision":cancelled["revision"], "ply":ply})
                    replacement = next(move for move in preview["legal_moves"] if move != original_move)
                    changed = self.request("/api/forced-move", {"revision":preview["revision"], "move":replacement})
                    self.assertFalse(changed["handicap_mode"])
                    self.assertEqual(len(changed["moves"]), ply)
                    self.assertEqual(changed["last_move"], replacement)
                    self.assertTrue(changed["moves"][-1]["forced"])
                    self.assertTrue(changed["clock"]["enabled"])
                    self.assertEqual(changed["clock"]["control_id"], control)
                    self.assertEqual(changed["clock"]["initial_ms"], initial)
                    self.assertEqual(changed["clock"]["increment_ms"], increment)
                    self.assertEqual(changed["clock"]["running_color"], color)
                    self.assertEqual(changed["clock"]["status"], "running")
                    self.assertEqual(changed["clock"]["timeout_policy"], "engine_only")
                    self.assertEqual(changed["clock"]["soft_timeout_color"], color)
                    self.assertGreater(changed["clock"]["edit_revision"], preview["clock"]["edit_revision"])
                    self.assertTrue(changed["engine"]["automatic_think_time"])
                    self.assertEqual(changed["engine"]["time_management"], "automatic")
                    changed_record = self.record()
                    self.assertEqual(changed_record["clock"]["history"][-1], completed_clock,
                                     "Replacement keeps original completed-ply clock exactly, with no second increment")
                    other = "black" if color == "white" else "white"
                    before = changed["clock"]
                    time.sleep(.08)
                    ticking = self.request("/api/state")
                    self.assertLess(ticking["clock"][color + "_ms"], before[color + "_ms"] - 30)
                    self.assertEqual(ticking["clock"][other + "_ms"], before[other + "_ms"])
                    state = self.engine(self.human(ticking))
                    self.assertEqual(len(state["moves"]), ply + 2)
                    self.assertTrue(state["human_to_move"] and state["clock"]["enabled"])
                    self.assertEqual(state["engine"]["time_management"], "automatic")
                    record = self.record()
                    self.assertEqual(record["clock"]["history"][ply], completed_clock)
                    self.assertEqual(len(record["clock"]["history"]), ply + 3)
                    self.assertTrue(record["engine_pacing"]["decisions"][-1]["allocation"]["automatic"])
                    archive = GameArchive(self.game.save_dir).get_game(self.game.game_id)
                    parsed = chess.pgn.read_game(io.StringIO(archive["pgn"]))
                    self.assertFalse(parsed.errors)
                    self.assertEqual(parsed.headers["TimeControl"], f"{initial // 1000}+{increment // 1000}")
                    self.assertTrue(all(node.clock() is not None for node in parsed.mainline()))
                    self.receipts.append({"control":control, "human_color":color, "replacement_ply":ply,
                        "original_move":original_move, "replacement":replacement,
                        "restored_completed_clock":completed_clock, "preview_frozen":True,
                        "cancel_restored_original_fen":True, "replacement_no_double_increment":True,
                        "human_countdown_resumed":True, "native_next_reply":state["last_move"],
                        "automatic_allocation_after_replacement":record["engine_pacing"]["decisions"][-1]["allocation"],
                        "archive_and_pgn_clocks_valid":True})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    binary = Path(subprocess.check_output([sys.executable, str(ROOT / "scripts/runtime_engine.py"),
        "--resolve"], cwd=ROOT, text=True).strip()).resolve(strict=True)
    HandicapClockNativeTests.binary = binary
    before = hashes()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(HandicapClockNativeTests))
    after = hashes()
    passed = result.wasSuccessful() and before == after and HandicapClockNativeTests.engine_closed
    evidence = {"schema":"regicide.handicap-clock-native.v1", "passed":passed,
        "created_utc":datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source_commit":subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_sha256":before, "source_sha256_after":after, "source_stable":before == after,
        "binary":str(binary), "binary_sha256":hashlib.sha256(binary.read_bytes()).hexdigest(),
        "engine_closed":HandicapClockNativeTests.engine_closed, "cases":HandicapClockNativeTests.receipts,
        "failures":[(test.id(),message) for test,message in result.failures],
        "errors":[(test.id(),message) for test,message in result.errors],
        "scope":"Six authenticated HTTP scenarios with real active native replies and temporary game storage; both human colors and all rapid controls. No user running games, complete matches, or strength measurements."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({"passed":passed,"cases":len(evidence["cases"]),"output":str(args.output)}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
