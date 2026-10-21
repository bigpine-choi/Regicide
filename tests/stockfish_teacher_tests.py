"""Small offline-label boundary and publication tests; no engine download."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import chess
import chess.engine
import stockfish_teacher as teacher


def source(history=()):
    board = chess.Board()
    for text in history:
        board.push_uci(text)
    return {"query_id": "old-query", "initial_fen": chess.STARTING_FEN,
            "history": list(history), "fen": board.fen(en_passant="fen"),
            "family_id": "fixed-family", "partition": "train", "target_white_cp": -17}


def info_for(board, value=100, **extra):
    return {"score": chess.engine.PovScore(chess.engine.Cp(value), board.turn),
            "depth": 8, "nodes": 10003, "pv": [next(iter(board.legal_moves))], **extra}


class FakeAnalysis:
    def __init__(self, board, packet):
        self.packet = packet
        # An older bound is deliberately left in aggregate info. Only the
        # latest score packet determines whether the final label is exact.
        self.info = {**packet, "lowerbound": True}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def __iter__(self):
        yield {**self.packet, "lowerbound": True}
        yield self.packet

    def wait(self):
        return SimpleNamespace(move=self.packet["pv"][0])


class FakeEngine:
    def __init__(self, packets=None):
        self.options = {"Clear Hash": None, "MultiPV": None}
        self.id = {"name": "fake-unit-only", "author": "tests"}
        self.configs, self.games, self.boards, self.limits = [], [], [], []
        self.packets = packets

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def configure(self, options):
        self.configs.append(options.copy())

    def analysis(self, board, limit, **kwargs):
        assert limit.nodes == 10000
        self.limits.append(limit)
        self.games.append(kwargs["game"])
        self.boards.append(board.copy(stack=True))
        packet = (self.packets[len(self.boards) - 1] if self.packets
                  else info_for(board))
        return FakeAnalysis(board, packet)


class StockfishTeacherTests(unittest.TestCase):
    def test_history_retained_and_mismatch_rejected(self):
        row = source(["g1f3", "g8f6", "f3g1", "f6g8"])
        board = teacher.source_board(row)
        self.assertEqual(len(board.move_stack), 4)
        self.assertTrue(board.is_repetition(2))
        row["fen"] = chess.STARTING_FEN
        with self.assertRaisesRegex(ValueError, "does not match"):
            teacher.source_board(row)
        with self.assertRaises(ValueError):
            teacher.source_board({**source(), "history": ["a1a8"]})

    def test_black_score_converted_to_white(self):
        board = teacher.source_board(source(["e2e4"]))
        info = info_for(board, 110)
        score, reasons = teacher.assess_info(board, info, info["pv"][0])
        self.assertEqual(reasons, [])
        self.assertEqual((score["stm_cp"], score["white_cp"]), (110, -110))

    def test_bounds_mates_range_pov_and_illegal_pv_excluded(self):
        board = chess.Board()
        info = info_for(board)
        for key in ("lowerbound", "upperbound"):
            self.assertIn("bound", teacher.assess_info(board, {**info, key: True}, info["pv"][0])[1])
        mate = {**info, "score": chess.engine.PovScore(chess.engine.Mate(2), board.turn)}
        values, reasons = teacher.assess_info(board, mate, info["pv"][0])
        self.assertEqual(values["white_cp"], None)
        self.assertIn("mate", reasons)
        self.assertNotIn("score_range", teacher.assess_info(board, info_for(board, 1500), info["pv"][0])[1])
        self.assertIn("score_range", teacher.assess_info(board, info_for(board, -1501), info["pv"][0])[1])
        wrong_pov = {**info, "score": chess.engine.PovScore(chess.engine.Cp(1), chess.BLACK)}
        self.assertIn("score_pov", teacher.assess_info(board, wrong_pov, info["pv"][0])[1])
        bad = {**info, "pv": [chess.Move.from_uci("a1a8")]}
        self.assertIn("illegal_pv", teacher.assess_info(board, bad, bad["pv"][0])[1])

    def test_publication_keeps_partition_history_and_original_target(self):
        rows = [source(), {**source(["e2e4"]), "partition": "validation"}]
        original = deepcopy(rows)
        fake = FakeEngine()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "engine.fake"
            binary.write_bytes(b"unit-test-only")
            output = root / "labels"
            accepted, report = teacher.label_rows(binary, rows, output,
                engine_factory=lambda *args, **kwargs: fake)
            self.assertEqual(rows, original)
            self.assertEqual(report["accepted_by_partition"], {"train": 1, "validation": 1})
            self.assertEqual([row["target_white_cp"] for row in accepted], [100, -100])
            self.assertEqual([row["source_target_white_cp"] for row in accepted], [-17, -17])
            self.assertEqual(accepted[1]["history"], ["e2e4"])
            receipts = [json.loads(line) for line in (output / "teacher-queries.jsonl").read_text().splitlines()]
            self.assertEqual([row["status"] for row in receipts], ["accepted", "accepted"])
            self.assertTrue(receipts[0]["raw_info"]["lowerbound"])
            self.assertNotIn("lowerbound", receipts[0]["raw_score_info_packet"])
            self.assertEqual(len(fake.boards[1].move_stack), 1)
            self.assertIsNot(fake.games[0], fake.games[1])
            self.assertEqual(fake.configs, [teacher.OPTIONS, {"Clear Hash": None}, {"Clear Hash": None}])
            self.assertEqual(report["files"]["labels.jsonl"], teacher.sha256(output / "labels.jsonl"))
            self.assertNotIn("requested_depth", report)
            self.assertTrue(all("requested_depth" not in row for row in receipts))
            self.assertTrue(all(limit.depth is None for limit in fake.limits))
            with self.assertRaises(FileExistsError):
                teacher.label_rows(binary, rows, output, engine_factory=lambda *a, **kw: fake)

    def test_rejected_rows_not_replaced_and_invalid_bank_prevents_launch(self):
        rows = [source(), source(["e2e4"])]
        packets = [info_for(teacher.source_board(rows[0]), 1600),
                   info_for(teacher.source_board(rows[1]), 50)]
        fake = FakeEngine(packets)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "engine.fake"
            binary.write_bytes(b"unit-test-only")
            accepted, report = teacher.label_rows(binary, rows, root / "labels",
                engine_factory=lambda *a, **kw: fake)
            self.assertEqual(len(fake.boards), 2)
            self.assertEqual(len(accepted), 1)
            self.assertEqual(report["exclusion_reasons"], {"score_range": 1})
            invalid = {**source(), "fen": "invalid"}
            with self.assertRaises(ValueError):
                teacher.label_rows(binary, [invalid], root / "bad",
                    engine_factory=lambda *a, **kw: self.fail("must not launch"))
            self.assertFalse((root / "bad").exists())

    def test_optional_depth_limit_is_explicit_in_search_and_receipts(self):
        fake = FakeEngine()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "engine.fake"
            binary.write_bytes(b"unit-test-only")
            _, report = teacher.label_rows(binary, [source()], root / "labels", max_depth=10,
                engine_factory=lambda *a, **kw: fake)
            receipt = json.loads((root / "labels/teacher-queries.jsonl").read_text().strip())
            self.assertEqual(fake.limits[0].depth, 10)
            self.assertEqual(receipt["requested_depth"], 10)
            self.assertEqual(report["requested_depth"], 10)
            self.assertIn("either limit", report["policies"]["budget"])
            self.assertNotIn("either limit", teacher.POLICIES["budget"])

    def test_invalid_optional_depth_prevents_engine_launch_and_publication(self):
        for depth in (0, -1, True, 1.5):
            with self.subTest(depth=depth), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "not-created"
                with self.assertRaisesRegex(ValueError, "depth"):
                    teacher.label_rows("missing-binary", [source()], output, max_depth=depth,
                        engine_factory=lambda *a, **kw: self.fail("must not launch"))
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
