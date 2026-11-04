"""Stdlib-only UCI integration: python tests/mate_uci_tests.py BINARY."""
from __future__ import annotations
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import unittest

if len(sys.argv) < 2:
    raise SystemExit("usage: mate_uci_tests.py BINARY")
BINARY = str(Path(sys.argv.pop(1)).resolve())
M1_FEN = "7k/5K2/6Q1/8/8/8/8/8 w - - 0 1"
LEGAL_POSITION = ("position startpos moves e2e4 e7e5 g1f3 b8c6 f1c4 d7d6 "
                  "b1c3 c8g4 f3e5 g4d1")

class Engine:
    def __init__(self):
        self.process = subprocess.Popen([BINARY, "uci"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.lines = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.send("uci")
        self.until(lambda line: line == "uciok")
        self.send("isready")
        self.until(lambda line: line == "readyok")

    def _read(self):
        try:
            for line in self.process.stdout:
                self.lines.put(line.rstrip("\r\n"))
        finally:
            self.lines.put(None)

    def send(self, command):
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()

    def until(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        collected = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"UCI timeout: {collected!r}")
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty as error:
                raise AssertionError(f"UCI timeout: {collected!r}") from error
            if line is None:
                raise AssertionError(f"early EOF: {self.process.poll()}, {collected!r}")
            collected.append(line)
            if predicate(line):
                return collected

    def search(self, position, command):
        self.send(position)
        self.send(command)
        return self.until(lambda line: line.startswith("bestmove "))

    def close(self):
        if self.process.poll() is None:
            try:
                self.send("quit")
                self.process.wait(timeout=5)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        self.process.stdin.close()
        self.process.stdout.close()

class MateUciTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        self.addCleanup(self.engine.close)

    def assert_status(self, lines, status, scope="all"):
        records = [l for l in lines if l.startswith("info string mate_status ")]
        self.assertEqual(len(records), 1, lines)
        record = records[0]
        self.assertRegex(record, rf"^info string mate_status {re.escape(status)}(?: |$)")
        self.assertRegex(record, rf"(?:^| )scope {scope}(?: |$)")
        for key in ("target", "nodes", "time", "completed"):
            self.assertRegex(record, rf"(?:^| ){key} \d+(?: |$)")
        return record

    def assert_no_proof(self, lines):
        self.assertFalse(any(re.search(r"\bscore mate\b", l) for l in lines), lines)
        self.assertFalse(any(re.search(r"\bpv [a-h][1-8][a-h][1-8]", l) for l in lines), lines)

    def assert_proof(self, lines, moves):
        self.assert_status(lines, "proven")
        scores = [l for l in lines if re.search(rf"\bscore mate {moves}(?: |$)", l)]
        self.assertTrue(scores, lines)
        pv_lines = [l for l in scores if " pv " in l]
        self.assertTrue(pv_lines, lines)
        pv = pv_lines[-1].split(" pv ", 1)[1].split()
        self.assertEqual(len(pv), 2*moves-1, lines)
        for move in pv:
            self.assertRegex(move, r"^[a-h][1-8][a-h][1-8][qrbn]?$")
        self.assertEqual(lines[-1], "bestmove " + pv[0], lines)
        return pv

    def test_white_mate_one(self):
        self.assert_proof(self.engine.search("position fen " + M1_FEN,
                                             "go mate 1 nodes 100000"), 1)

    def test_black_mate_one(self):
        lines = self.engine.search("position startpos moves f2f3 e7e5 g2g4",
                                   "go mate 1 nodes 100000")
        self.assertEqual(self.assert_proof(lines, 1), ["d8h4"])

    def test_legal_mate_two_and_shorter_bound(self):
        short = self.engine.search(LEGAL_POSITION, "go mate 1 nodes 100000")
        self.assert_status(short, "no_mate_within_bound")
        self.assert_no_proof(short)
        long = self.engine.search(LEGAL_POSITION, "go mate 2 nodes 1000000")
        self.assertEqual(self.assert_proof(long, 2), ["c4f7", "e8e7", "c3d5"])

    def test_completed_refutation_is_not_unknown(self):
        lines = self.engine.search("position startpos", "go mate 1 nodes 100000")
        self.assert_status(lines, "no_mate_within_bound")
        self.assert_no_proof(lines)
        self.assertRegex(lines[-1], r"^bestmove [a-h][1-8][a-h][1-8]$")
        self.assertIn("info string mate_fallback unproven", lines)

    def test_node_exhaustion_is_unknown(self):
        lines = self.engine.search("position startpos", "go mate 16 nodes 1")
        self.assert_status(lines, "unknown")
        self.assert_no_proof(lines)
        self.assertRegex(lines[-1], r"^bestmove [a-h][1-8][a-h][1-8]$")

    def test_time_exhaustion_is_unknown(self):
        lines = self.engine.search("position startpos",
                                   "go mate 64 nodes 1000000000 movetime 1")
        self.assert_status(lines, "unknown")
        self.assert_no_proof(lines)

    def test_searchmoves_proof_scope(self):
        lines = self.engine.search("position fen " + M1_FEN,
                                   "go searchmoves g6g7 mate 1 nodes 100000")
        self.assert_status(lines, "proven", scope="searchmoves")
        self.assertTrue(any("score mate 1" in l and " pv g6g7" in l for l in lines))
        self.assertEqual(lines[-1], "bestmove g6g7")

    def test_searchmoves_refutation_scope(self):
        lines = self.engine.search("position fen " + M1_FEN,
                                   "go mate 1 searchmoves g6g5 nodes 100000")
        self.assert_status(lines, "no_mate_within_bound", scope="searchmoves")
        self.assert_no_proof(lines)
        self.assertEqual(lines[-1], "bestmove g6g5")

    def test_stalemate_has_no_bestmove_or_mate(self):
        lines = self.engine.search("position fen " + M1_FEN.replace(" w ", " b "),
                                   "go mate 2 nodes 100000")
        self.assert_status(lines, "no_mate_within_bound")
        self.assert_no_proof(lines)
        self.assertEqual(lines[-1], "bestmove 0000")

    def test_attacker_may_decline_fifty_move_claim(self):
        fen = M1_FEN.rsplit(" ", 2)[0] + " 100 1"
        self.assert_proof(self.engine.search("position fen " + fen,
                                             "go mate 1 nodes 100000"), 1)

    def test_checkmate_precedes_seventy_five_move_draw(self):
        fen = M1_FEN.rsplit(" ", 2)[0] + " 149 1"
        self.assert_proof(self.engine.search("position fen " + fen,
                                             "go mate 1 nodes 100000"), 1)

    def test_automatic_draw_before_search(self):
        fen = M1_FEN.rsplit(" ", 2)[0] + " 150 1"
        lines = self.engine.search("position fen " + fen, "go mate 1 nodes 100000")
        self.assert_status(lines, "no_mate_within_bound")
        self.assert_no_proof(lines)
        self.assertEqual(lines[-1], "bestmove 0000")

    def test_invalid_limits_and_mixed_modes(self):
        commands = ["go mate", "go mate 0", "go mate -1", "go mate 65",
            "go mate xyz", "go mate 18446744073709551616", "go mate 1 mate 2",
            "go mate 1 depth 2", "go depth 2 mate 1", "go mate 1 infinite",
            "go infinite mate 1", "go mate 1 nodes 0", "go mate 1 searchmoves",
            "go mate 1 searchmoves e2e5", "go mate 1 searchmoves e2e4 e2e4"]
        for command in commands:
            with self.subTest(command=command):
                lines = self.engine.search("position startpos", command)
                self.assertEqual(lines[-1], "bestmove 0000", lines)
                self.assertTrue(any(l.startswith("info string ") for l in lines))
                self.assert_no_proof(lines)
                self.assertFalse(any("mate_status proven" in l for l in lines))

    def test_stop_ready_and_normal_search_reuse(self):
        self.engine.send("position startpos")
        self.engine.send("go mate 64 nodes 1000000000")
        self.engine.send("isready")
        ready = self.engine.until(lambda line: line == "readyok", timeout=5)
        self.assertFalse(any(l.startswith("bestmove ") for l in ready), ready)
        self.engine.send("stop")
        stopped = self.engine.until(lambda line: line.startswith("bestmove "), timeout=5)
        self.assert_status(stopped, "unknown")
        self.assert_no_proof(stopped)
        normal = self.engine.search("position startpos", "go depth 2")
        self.assertRegex(normal[-1], r"^bestmove [a-h][1-8][a-h][1-8][qrbn]?$")
        self.assertTrue(any(l.startswith("info depth 2 ") for l in normal), normal)
        self.assertFalse(any("mate_status " in l for l in normal), normal)
        self.assert_proof(self.engine.search("position fen " + M1_FEN,
                                             "go mate 1 nodes 100000"), 1)

if __name__ == "__main__":
    unittest.main(verbosity=2)
