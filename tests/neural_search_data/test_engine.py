"""UCI adapter tests using a deliberately small fake process, not Regicide."""

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import chess
import chess.engine

from scripts.neural_search_data.engine import (CLASSICAL_OPTIONS, LEGACY_EXTENSION_OPTIONS,
                                               EngineTeacher, SearchInterrupted)


FAKE_UCI = r'''
import sys
import time
import chess

options = OPTIONS
board = chess.Board()
for raw in sys.stdin:
    line = raw.strip()
    if line == "uci":
        print("id name Fake fixed-node test process")
        print("id author Regicide tests")
        for name, value in options.items():
            kind = "check" if isinstance(value, bool) else "spin"
            # Deliberately different defaults exercise every explicit setting.
            default = str(not value if kind == "check" else value + 1).lower()
            suffix = "" if kind == "check" else " min 0 max 256"
            print(f"option name {name} type {kind} default {default}{suffix}")
        print("uciok", flush=True)
    elif line == "isready":
        print("readyok", flush=True)
    elif line.startswith("position "):
        description = line[len("position "):]
        fields = description.split(" moves ", 1)
        board = chess.Board() if fields[0] == "startpos" else chess.Board(fields[0][4:])
        if len(fields) > 1:
            for move in fields[1].split():
                board.push_uci(move)
    elif line.startswith("go nodes "):
        nodes = int(line.split()[2])
        if nodes == 40:
            time.sleep(10)  # Deliberately ignore stop; transport must terminate us.
            continue
        move = sorted(board.legal_moves, key=lambda item: item.uci())[0]
        following = board.copy(stack=True)
        following.push(move)
        pv = [move.uci()]
        if any(following.legal_moves):
            pv.append(sorted(following.legal_moves, key=lambda item: item.uci())[0].uci())
        score = "mate -3" if nodes == 38 else "cp 23"
        bound = " lowerbound" if nodes == 39 else ""
        print(f"info depth 2 seldepth 4 score {score}{bound} nodes {nodes} time 1 pv {' '.join(pv)}")
        print(f"bestmove {move.uci()}", flush=True)
    elif line == "quit":
        break
'''


class EngineTeacherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.fake = self.folder / "fake_uci.py"
        self.fake.write_text(FAKE_UCI.replace("OPTIONS", repr(CLASSICAL_OPTIONS)), encoding="utf-8")
        original = chess.engine.SimpleEngine.popen

        def fake_popen(protocol, command, **kwargs):
            self.assertEqual(command, [str(Path(sys.executable).resolve()), "uci"])
            return original(protocol, [sys.executable, str(self.fake)], **kwargs)

        self.patcher = patch("scripts.neural_search_data.engine.chess.engine.SimpleEngine.popen", side_effect=fake_popen)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temporary.cleanup()

    def teacher(self, *, nodes=37, timeout=1.0):
        return EngineTeacher(Path(sys.executable), self.folder / "raw.jsonl", nodes,
                             time.monotonic() + 10, timeout)

    def raw(self):
        return [json.loads(line) for line in (self.folder / "raw.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_fixed_nodes_fresh_hash_history_and_clean_exit(self):
        board = chess.Board()
        for move in ("g1f3", "g8f6", "f3g1", "f6g8"):
            board.push_uci(move)
        with self.teacher() as teacher:
            first = teacher.query(board, "first")
            second = teacher.query(board, "second")
            self.assertEqual(teacher.options, CLASSICAL_OPTIONS)
            self.assertEqual(teacher.identity["name"], "Fake fixed-node test process")
            self.assertTrue(first["completed"])
            self.assertEqual(first["requested_nodes"], 37)
            self.assertEqual(first["nodes"], 37)
            self.assertEqual(first["depth"], 2)
            self.assertEqual(first["seldepth"], 4)
            self.assertEqual(first["engine_time_ms"], 1)
            self.assertEqual(first["score"], {"kind": "cp", "stm": 23})
            self.assertEqual(first["pv_uci"], second["pv_uci"])
            self.assertEqual(first["score"], second["score"])
            self.assertEqual(board.move_stack[-1].uci(), "f6g8")
        self.assertTrue(teacher.closed)
        self.assertTrue(teacher.cleanup_status["returncode_observed"])
        self.assertEqual(teacher.cleanup_status["returncode"], 0)
        records = self.raw()
        self.assertEqual([row["seq"] for row in records], list(range(1, len(records) + 1)))
        sends = [row["line"] for row in records if row["direction"] == "send"]
        self.assertEqual(sends.count("ucinewgame"), 2)
        self.assertEqual(sends.count("go nodes 37"), 2)
        self.assertIn("position startpos moves g1f3 g8f6 f3g1 f6g8", sends)
        self.assertIn("setoption name UseNeuralEvaluation value false", sends)
        self.assertIn("setoption name UseSEE value true", sends)

    def test_score_remains_root_side_to_move_for_black(self):
        board = chess.Board()
        board.push_uci("e2e4")
        with self.teacher() as teacher:
            record = teacher.query(board, "black")
        self.assertEqual(record["score"], {"kind": "cp", "stm": 23})

    def test_integrated_runtime_disables_new_defaults_and_both_networks(self):
        options = {**CLASSICAL_OPTIONS, **LEGACY_EXTENSION_OPTIONS}
        self.fake.write_text(FAKE_UCI.replace("OPTIONS", repr(options)), encoding="utf-8")
        with self.teacher() as teacher:
            teacher.query(chess.Board(), "integrated-profile")
            self.assertEqual(teacher.options, options)
        sends = [row["line"] for row in self.raw() if row["direction"] == "send"]
        for name in LEGACY_EXTENSION_OPTIONS:
            self.assertIn(f"setoption name {name} value false", sends)

    def test_special_move_histories_are_preserved(self):
        examples = (
            ("4k3/8/8/8/8/8/8/4K2R w K - 0 1", "e1g1"),
            ("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1", "e5d6"),
            ("4k3/P7/8/8/8/8/8/4K3 w - - 0 1", "a7a8q"),
        )
        with self.teacher() as teacher:
            for index, (fen, move) in enumerate(examples):
                board = chess.Board(fen)
                board.push_uci(move)
                record = teacher.query(board, f"special-{index}")
                self.assertTrue(record["completed"])
        sends = [row["line"] for row in self.raw() if row["direction"] == "send"]
        for fen, move in examples:
            self.assertIn(f"position fen {fen} moves {move}", sends)

    def test_mate_is_separate_from_centipawn_target(self):
        with self.teacher(nodes=38) as teacher:
            record = teacher.query(chess.Board(), "mate")
        self.assertEqual(record["score"], {"kind": "mate", "stm": -3})

    def test_bound_flags_are_retained(self):
        with self.teacher(nodes=39) as teacher:
            record = teacher.query(chess.Board(), "bound")
        self.assertTrue(record["lowerbound"])
        self.assertFalse(record["upperbound"])

    def test_node_only_search_timeout_terminates_unresponsive_process(self):
        teacher = self.teacher(nodes=40, timeout=0.05)
        with teacher:
            started = time.monotonic()
            with self.assertRaises(SearchInterrupted) as caught:
                teacher.query(chess.Board(), "timeout")
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertEqual(caught.exception.reason, "search_timeout")
            self.assertFalse(caught.exception.record["completed"])
            self.assertTrue(teacher.closed)
            self.assertTrue(teacher.cleanup_status["returncode_observed"])
            with self.assertRaises(RuntimeError):
                teacher.query(chess.Board(), "after-close")

    def test_deadline_during_search_never_yields_label(self):
        with self.teacher(nodes=40) as teacher:
            teacher.deadline = time.monotonic() + 0.03
            with self.assertRaises(SearchInterrupted) as caught:
                teacher.query(chess.Board(), "deadline")
            self.assertEqual(caught.exception.reason, "wall_budget")
            self.assertFalse(caught.exception.record["completed"])
            self.assertTrue(teacher.closed)

    def test_expired_budget_before_start_creates_no_process_or_log(self):
        teacher = self.teacher()
        teacher.deadline = time.monotonic() - 1
        with self.assertRaises(SearchInterrupted):
            teacher.__enter__()
        self.assertFalse((self.folder / "raw.jsonl").exists())

    def test_invalid_limits_and_exclusive_raw_log(self):
        for nodes in (True, 0, -1, 1.0):
            with self.assertRaises(ValueError):
                self.teacher(nodes=nodes)
        for timeout in (True, 0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                self.teacher(timeout=timeout)
        (self.folder / "raw.jsonl").write_text("preserve", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            self.teacher().__enter__()
        self.assertEqual((self.folder / "raw.jsonl").read_text(encoding="utf-8"), "preserve")


if __name__ == "__main__":
    unittest.main()
