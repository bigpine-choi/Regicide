"""Isolated real-engine HTTP checks for the click-only sparring clocks.

A declared synthetic middlegame leaves enough think time to exercise concurrent
requests. Native move selection, pacing, clock ticks and HTTP are unmodified.
This is functional integration evidence, not a complete match or Elo estimate.
"""
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
import unittest
import urllib.error
import urllib.request

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import play_human
from game_archive import GameArchive

FEN = "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 20"
SOURCES = ("scripts/play_human.py", "scripts/game_clock.py", "scripts/engine_pacing.py",
           "scripts/decision_support.py", "scripts/game_archive.py",
           "godot/scripts/main.gd", "tests/clock_adjust_live_tests.py")


def hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}


class ClockAdjustmentNativeTests(unittest.TestCase):
    receipts = []
    binary = None
    all_closed = True

    def setUp(self):
        # Native integration is explicit, not an ordinary unit-test dependency.
        if self.binary is None:
            self.skipTest("Run this script with --output to resolve the active native engine")
        self.directory = tempfile.TemporaryDirectory(prefix="regicide-click-clock-native-")
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
        closed = not self.thread.is_alive() and self.game._engine is None
        type(self).all_closed = type(self).all_closed and closed
        self.directory.cleanup()
        self.assertTrue(closed, "Only this test's native engine and server are stopped")

    def request(self, path, body=None, expected=200):
        request = urllib.request.Request(self.server.origin + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"X-Regicide-Token": self.server.token, "Origin": self.server.origin,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=70) as response:
                status, value = response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            status, value = error.code, json.loads(error.read())
        self.assertEqual(status, expected, (path, value))
        return value

    def click(self, state, side, delta):
        return self.request("/api/clock-adjust", {"game_id": state["game_id"],
            "clock_revision": state["clock"]["edit_revision"], "side": side, "delta_ms": delta})

    def new_position(self, color):
        current = self.request("/api/state")
        self.request("/api/new", {"revision": current["revision"], "color": color,
            "sparring_mode": True, "time_control": "10+0"})
        # Only test setup injects a position. All moves/clock edits use HTTP.
        with self.game._lock:
            self.game.board = chess.Board(FEN)
            self.game._opening_seed = "native-click-clock-" + color
            self.game._sync_evaluation()
            self.game._save()
        state = self.request("/api/state")
        self.assertTrue(state["clock"]["editable"])
        self.assertEqual(state["engine"]["time_management"], "automatic")
        if state["human_to_move"]:
            state = self.request("/api/move", {"revision": state["revision"],
                "move": "e2a6" if "e2a6" in state["legal_moves"] else state["legal_moves"][0]})
        self.assertTrue(state["engine_to_move"])
        return state

    def wait_thinking(self, future):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not future.done():
            state = self.request("/api/state")
            pacing = state["engine"].get("pacing") or {}
            if state["engine_thinking"] and pacing.get("enabled"):
                return state
            time.sleep(.005)
        self.fail("Native engine did not expose a concurrently editable thinking state")

    def test_both_roles_during_native_thinking_and_five_minute_pressure(self):
        for color in ("white", "black"):
            with self.subTest(human_color=color):
                state = self.new_position(color)
                engine_color = "black" if color == "white" else "white"
                for delta in [-60000] * 4 + [-10000] * 4:
                    state = self.click(state, "engine", delta)
                self.assertGreater(state["clock"][engine_color + "_ms"], 310000)
                start_revision, start_fen = state["revision"], state["fen"]
                start_moves = len(state["moves"])
                with ThreadPoolExecutor(max_workers=1) as workers:
                    future = workers.submit(self.request, "/api/engine", {"revision": start_revision})
                    thinking = self.wait_thinking(future)
                    self.assertEqual(thinking["engine"]["pacing"]["pressure_factor"], 1.0)
                    self.assertEqual(thinking["engine"]["pacing"]["pressure_start_ms"], 300000)
                    before_human = thinking["clock"][color + "_ms"]
                    human_edit = self.click(thinking, "human", 10000)
                    self.assertEqual(human_edit["clock"][color + "_ms"], before_human + 10000)
                    self.assertTrue(human_edit["engine_thinking"])
                    pressed = self.click(human_edit, "engine", -60000)
                    self.assertTrue(pressed["engine_thinking"])
                    self.assertLess(pressed["clock"][engine_color + "_ms"], 300000)
                    self.assertLess(pressed["engine"]["pacing"]["pressure_factor"], 1.0)
                    self.assertEqual(pressed["engine"]["pacing"]["clock_edit_revision"], pressed["clock"]["edit_revision"])
                    relieved = self.click(pressed, "engine", 60000)
                    self.assertGreater(relieved["clock"][engine_color + "_ms"], 300000)
                    self.assertEqual(relieved["engine"]["pacing"]["pressure_factor"], 1.0)
                    self.assertGreater(relieved["engine"]["pacing"]["target_total_ms"], pressed["engine"]["pacing"]["target_total_ms"])
                    self.assertTrue(relieved["engine_thinking"])
                    self.assertEqual((relieved["revision"], relieved["fen"], len(relieved["moves"])),
                                     (start_revision, start_fen, start_moves))
                    self.assertEqual(relieved["engine"]["time_management"], "automatic")
                    state = future.result(timeout=70)
                self.assertEqual(state["result"], "*")
                self.assertIsNone(state["error"])
                self.assertTrue(state["human_to_move"])
                self.assertEqual(len(state["moves"]), start_moves + 1)
                position = chess.Board(start_fen)
                move = chess.Move.from_uci(state["moves"][-1]["uci"])
                self.assertIn(move, position.legal_moves)
                position.push(move)
                self.assertEqual(state["fen"], position.fen(en_passant="fen"))
                self.assertEqual(state["clock"]["edit_revision"], relieved["clock"]["edit_revision"])
                record = json.loads((self.game.save_dir / (self.game.game_id + ".json")).read_text(encoding="utf-8"))
                pacing = record["engine_pacing"]["decisions"][-1]
                self.assertEqual(pacing["clock_edit_revision"], relieved["clock"]["edit_revision"])
                GameArchive(self.game.save_dir).get_game(self.game.game_id)
                self.receipts.append({"human_color":color, "native_legal_reply":move.uci(),
                    "human_click_during_thinking":True, "engine_clicks_during_thinking":True,
                    "board_revision_unchanged_by_edits":True,
                    "pressure_before":thinking["engine"]["pacing"]["pressure_factor"],
                    "pressure_below_three_minutes":pressed["engine"]["pacing"]["pressure_factor"],
                    "pressure_after_adding_time":relieved["engine"]["pacing"]["pressure_factor"],
                    "pressed_target_ms":pressed["engine"]["pacing"]["target_total_ms"],
                    "relieved_target_ms":relieved["engine"]["pacing"]["target_total_ms"],
                    "completed_pacing":pacing, "archive_validated":True})

    def test_human_zero_continues_engine_zero_cannot_be_resurrected(self):
        for color in ("white", "black"):
            with self.subTest(human_color=color):
                current = self.request("/api/state")
                state = self.request("/api/new", {"revision":current["revision"], "color":color,
                    "sparring_mode":True, "time_control":"10+0"})
                if state["engine_to_move"]:
                    state = self.request("/api/engine", {"revision":state["revision"]})
                self.assertTrue(state["human_to_move"])
                for _ in range(10):
                    state = self.click(state, "human", -60000)
                self.assertEqual(state["clock"][color + "_ms"], 0)
                self.assertEqual(state["result"], "*")
                self.assertTrue(state["human_to_move"] and state["legal_moves"])
                legal = state["legal_moves"][0]
                before_plies = len(state["moves"])
                state = self.request("/api/move", {"revision":state["revision"], "move":legal})
                self.assertEqual(len(state["moves"]), before_plies + 1)
                self.assertEqual(state["result"], "*")
                self.assertTrue(state["engine_to_move"])
                for _ in range(10):
                    if state["result"] != "*":
                        break
                    state = self.click(state, "engine", -60000)
                self.assertEqual(state["result"], "1-0" if color == "white" else "0-1")
                self.assertEqual(state["termination"], "time_forfeit")
                self.assertFalse(state["clock"]["editable"])
                revision, edit_revision = state["revision"], state["clock"]["edit_revision"]
                rejected = self.request("/api/clock-adjust", {"game_id":state["game_id"],
                    "clock_revision":edit_revision, "side":"engine", "delta_ms":60000}, expected=400)
                self.assertEqual(rejected["state"]["revision"], revision)
                self.assertEqual(rejected["state"]["clock"]["edit_revision"], edit_revision)
                self.assertEqual(rejected["state"]["result"], state["result"])
                self.assertEqual(self.request("/api/state")["revision"], revision)
                GameArchive(self.game.save_dir).get_game(self.game.game_id)
                self.receipts.append({"human_color":color, "human_zero_continues":True,
                    "legal_human_move_after_zero":legal, "engine_zero_is_terminal":True,
                    "adding_time_after_terminal_rejected":True, "archive_validated":True})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    binary = Path(subprocess.check_output([sys.executable, str(ROOT / "scripts/runtime_engine.py"),
        "--resolve"], cwd=ROOT, text=True).strip()).resolve(strict=True)
    ClockAdjustmentNativeTests.binary = binary
    before = hashes()
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ClockAdjustmentNativeTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    after = hashes()
    passed = result.wasSuccessful() and before == after and ClockAdjustmentNativeTests.all_closed
    evidence = {"schema":"regicide.clock-adjustment-native.v1", "passed":passed,
        "started_utc":started, "finished_utc":datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source_commit":subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_sha256":before, "source_sha256_after":after, "source_stable":before == after,
        "binary":str(binary), "binary_sha256":hashlib.sha256(binary.read_bytes()).hexdigest(),
        "test_methods":result.testsRun, "failures":len(result.failures), "errors":len(result.errors),
        "test_details":{"failures":result.failures, "errors":result.errors} if not result.wasSuccessful() else {},
        "engine_and_server_closed":ClockAdjustmentNativeTests.all_closed,
        "synthetic_concurrency_start_fen":FEN, "checks":ClockAdjustmentNativeTests.receipts,
        "scope":"Actual runtime native engine and authenticated HTTP with isolated temporary games; button-equivalent payloads, not physical UI clicks; concurrent tests start from declared synthetic middlegame; no full match or playing-strength claim"}
    # Serialize failed unittest test ids without leaking object representations.
    for kind in ("failures", "errors"):
        if kind in evidence["test_details"]:
            evidence["test_details"][kind] = [(test.id(), message) for test, message in evidence["test_details"][kind]]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed":passed, "output":str(args.output), "checks":len(evidence["checks"])}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
