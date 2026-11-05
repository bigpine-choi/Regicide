"""Offline research evidence must preserve history and reject weak labels."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gambit_research as cli
import gambit_research_core as core
from study_gambit_theory import _usable


def board_after(history=()):
    board = chess.Board()
    for uci in history:
        board.push_uci(uci)
    return board


def exact_answer(board, cp=37):
    line = board.copy(stack=True)
    moves = []
    for _ in range(2):
        if line.is_game_over(claim_draw=False):
            break
        move = next(iter(line.legal_moves))
        moves.append(move.uci())
        line.push(move)
    return {"completed": True, "depth": 7, "nodes": 321,
            "score": {"kind": "cp", "stm": cp}, "pv_uci": moves,
            "bestmove_uci": moves[0] if moves else None,
            "lowerbound": False, "upperbound": False}


class FakeTeacher:
    instances = []
    answer = None

    def __init__(self, binary, raw_path, nodes, deadline, search_timeout):
        self.binary, self.raw_path, self.nodes = binary, raw_path, nodes
        self.deadline, self.search_timeout = deadline, search_timeout
        self.options = {"UsePieceActivity": True, "Hash": 16}
        self.identity = {"name": "Regicide research fixture"}
        self.closed = True
        self.cleanup_status = {"quit_requested": False, "transport_closed": False}
        self.calls = []
        self.instances.append(self)

    def __enter__(self):
        self.closed = False
        return self

    def query(self, board, query_id):
        self.calls.append((board.copy(stack=True), query_id, self.nodes))
        answer = type(self).answer
        if isinstance(answer, BaseException):
            self.closed = True
            raise answer
        if callable(answer):
            return answer(board)
        return copy.deepcopy(answer) if answer is not None else exact_answer(board)

    def close(self):
        self.closed = True
        self.cleanup_status.update(quit_requested=True, transport_closed=True)

    def __exit__(self, exc_type, exc, traceback):
        self.close()


class NativeResearchCoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.binary = self.root / "regicide-fixture"
        self.binary.write_bytes(b"regicide research executable fixture\n")
        self.binary.chmod(0o755)
        FakeTeacher.instances = []
        FakeTeacher.answer = None
        self.teacher_patch = patch.object(core, "_RuntimeTeacher", FakeTeacher)
        self.teacher_patch.start()
        self.addCleanup(self.teacher_patch.stop)
        self.hash_patch = patch.object(core, "source_hashes", return_value={"fixture": "sha256"})
        self.hash_patch.start()
        self.addCleanup(self.hash_patch.stop)

    def analyzer(self, name="analysis", **kwargs):
        return core.NativeAnalyzer(self.binary, self.root / name, **kwargs)

    def test_full_standard_history_and_side_to_move_cp_are_preserved(self):
        board = board_after("e2e4 c7c5 g1f3".split())
        original_fen, original_stack = board.fen(), list(board.move_stack)
        FakeTeacher.answer = lambda position: exact_answer(position, cp=-67)
        with self.analyzer() as analyzer:
            result = analyzer.analyze(board, nodes=789)
            sent, query_id, amount = analyzer.teacher.calls[0]
            self.assertEqual(sent.root().fen(), chess.STARTING_FEN)
            self.assertEqual(sent.move_stack, board.move_stack)
            self.assertEqual(result["history_uci"], "e2e4 c7c5 g1f3".split())
            self.assertEqual(result["turn"], "black")
            self.assertEqual(result["cp"], -67)
            self.assertEqual(result["status"], "complete")
            self.assertEqual((query_id, amount), ("q000001", 789))
            self.assertEqual(analyzer.teacher.nodes, analyzer.nodes)
        self.assertEqual((board.fen(), board.move_stack), (original_fen, original_stack))
        persisted = json.loads((self.root / "analysis/queries.jsonl").read_text().strip())
        self.assertEqual(persisted, result)

    def test_bound_shallow_incomplete_missing_and_illegal_lines_are_not_labels(self):
        board = chess.Board()
        variants = {
            "lowerbound": {"lowerbound": True},
            "upperbound": {"upperbound": True},
            "shallow": {"depth": 3},
            "bool_depth": {"depth": True},
            "incomplete": {"completed": False},
            "missing_score": {"score": {"kind": "missing", "stm": None}},
            "missing_pv": {"pv_uci": []},
            "illegal_pv": {"pv_uci": ["e2e4", "e2e5"], "bestmove_uci": "e2e4"},
            "bestmove_mismatch": {"pv_uci": ["e2e4"], "bestmove_uci": "d2d4"},
        }
        for label, changes in variants.items():
            with self.subTest(label=label):
                FakeTeacher.answer = {**exact_answer(board), **changes}
                with self.analyzer(label) as analyzer:
                    result = analyzer.analyze(board)
                self.assertEqual(result["status"], "unknown")
                self.assertFalse(_usable(result, 4))

    def test_malformed_pv_is_checkpointed_as_unknown(self):
        FakeTeacher.answer = {**exact_answer(chess.Board()), "pv_uci": ["not-a-move"],
                              "bestmove_uci": "not-a-move"}
        with self.analyzer() as analyzer:
            result = analyzer.analyze(chess.Board())
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(_usable(result, 4))
        rows = (self.root / "analysis/queries.jsonl").read_text().splitlines()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0])["status"], "unknown")

    def test_null_native_record_or_score_is_checkpointed_as_unknown(self):
        responses = {
            "null_record": lambda board: None,
            "null_score": lambda board: {**exact_answer(board), "score": None},
        }
        for name, response in responses.items():
            with self.subTest(name=name):
                FakeTeacher.answer = response
                with self.analyzer(name) as analyzer:
                    row = analyzer.analyze(chess.Board())
                    self.assertEqual(row["status"], "unknown")
                    self.assertEqual(row["reason"], "invalid_native_response")
                    self.assertIsNone(row["cp"])
                    self.assertFalse(_usable(row, 4))
                output = self.root / name
                array = json.loads((output / "queries.json").read_text())
                journal = [json.loads(line) for line in (output / "queries.jsonl").read_text().splitlines()]
                self.assertEqual(array, [row])
                self.assertEqual(journal, [row])

    def test_final_ledgers_restore_complete_memory_and_preserve_damaged_checkpoint(self):
        boards = [chess.Board(), board_after(["e2e4"]), board_after(["e2e4", "c7c5"])]
        for damage in ("missing_middle_line", "truncated_last_line"):
            with self.subTest(damage=damage):
                output = self.root / damage
                with self.analyzer(damage) as analyzer:
                    for board in boards:
                        analyzer.analyze(board)
                    expected = copy.deepcopy(analyzer.records)
                    journal = output / "queries.jsonl"
                    original_lines = journal.read_bytes().splitlines(keepends=True)
                    self.assertEqual(len(original_lines), 3)
                    if damage == "missing_middle_line":
                        damaged_bytes = original_lines[0] + original_lines[2]
                    else:
                        damaged_bytes = b"".join(original_lines[:2]) + original_lines[2][:17]
                    journal.write_bytes(damaged_bytes)
                final_bytes = (output / "queries.jsonl").read_bytes()
                final_rows = [json.loads(line) for line in final_bytes.splitlines()]
                self.assertEqual(final_rows, expected)
                self.assertEqual(json.loads((output / "queries.json").read_text()), expected)
                self.assertEqual([row["query_id"] for row in final_rows],
                                 ["q000001", "q000002", "q000003"])
                self.assertEqual((output / "queries.incremental.jsonl").read_bytes(), damaged_bytes)
                summary = json.loads((output / "analysis_summary.json").read_text())
                self.assertEqual(summary["queries"], 3)
                self.assertEqual(summary["final_query_ledger_rows"], 3)
                self.assertEqual(summary["final_query_ledger_sha256"], hashlib.sha256(final_bytes).hexdigest())
                self.assertTrue(summary["closed"])

    def test_mate_and_terminal_results_never_become_cp_training_labels(self):
        FakeTeacher.answer = {**exact_answer(chess.Board()), "score": {"kind": "mate", "stm": 3}}
        mate_board = board_after("f2f3 e7e5 g2g4 d8h4".split())
        draw_board = board_after("g1f3 g8f6 f3g1 f6g8".split() * 4)
        with self.analyzer() as analyzer:
            future_mate = analyzer.analyze(chess.Board())
            checkmate = analyzer.analyze(mate_board)
            draw = analyzer.analyze(draw_board)
            self.assertEqual(len(analyzer.teacher.calls), 1)
        self.assertEqual((future_mate["status"], future_mate["cp"], future_mate["mate"]),
                         ("complete", None, 3))
        self.assertEqual((checkmate["status"], checkmate["cp"], checkmate["mate"]),
                         ("terminal", None, 0))
        self.assertEqual((draw["status"], draw["cp"], draw["terminal_result"]),
                         ("terminal", 0, "1/2-1/2"))
        for result in (future_mate, checkmate, draw):
            self.assertFalse(_usable(result, 4))

    def test_interruption_and_subsequent_closed_query_remain_unknown(self):
        FakeTeacher.answer = core.SearchInterrupted("wall_budget", {"completed": False,
                                                                                "nodes": 200})
        with self.analyzer() as analyzer:
            interrupted = analyzer.analyze(chess.Board(), nodes=99)
            closed = analyzer.analyze(chess.Board())
            self.assertEqual(analyzer.teacher.nodes, analyzer.nodes)
            self.assertEqual(len(analyzer.teacher.calls), 1)
        self.assertEqual(interrupted["reason"], "wall_budget")
        self.assertEqual(closed["reason"], "analyzer_closed_or_budget_exhausted")
        self.assertEqual((interrupted["status"], closed["status"]), ("unknown", "unknown"))
        self.assertIsNone(interrupted["cp"])
        self.assertEqual(len((self.root / "analysis/queries.jsonl").read_text().splitlines()), 2)

    def test_cleanup_budget_and_source_identity_are_recorded_on_exception(self):
        with patch.object(core.time, "monotonic", return_value=1000.0):
            with self.assertRaisesRegex(RuntimeError, "caller failure"):
                with self.analyzer(wall_seconds=5, search_timeout=2) as analyzer:
                    self.assertEqual(analyzer.teacher.deadline, 1005.0)
                    analyzer.analyze(chess.Board())
                    raise RuntimeError("caller failure")
        summary = json.loads((self.root / "analysis/analysis_summary.json").read_text())
        manifest = json.loads((self.root / "analysis/analysis_manifest.json").read_text())
        self.assertEqual(summary["statuses"], {"complete": 1})
        self.assertTrue(summary["cleanup"]["transport_closed"])
        self.assertTrue(summary["closed"])
        self.assertTrue(summary["sources_unchanged"])
        self.assertTrue(summary["binary_unchanged"])
        self.assertTrue(manifest["full_history"])
        self.assertTrue(manifest["fresh_uci_game_per_query"])
        self.assertEqual(manifest["wall_seconds"], 5)

    def test_nonstandard_or_detached_board_history_is_rejected(self):
        legal = board_after(["e2e4"])
        detached = chess.Board(legal.fen())
        nonstandard = chess.Board("8/8/8/8/8/3k4/8/3K4 w - - 0 1")
        with self.analyzer() as analyzer:
            for board in (detached, nonstandard):
                with self.subTest(fen=board.fen()), self.assertRaises(ValueError):
                    analyzer.analyze(board)
            self.assertEqual(analyzer.teacher.calls, [])

    def test_nodes_boards_and_restricted_roots_are_validated(self):
        with self.analyzer() as analyzer:
            for value in (0, -1, True, 2.5):
                with self.subTest(nodes=value), self.assertRaises(ValueError):
                    analyzer.analyze(chess.Board(), nodes=value)
            with self.assertRaises(ValueError):
                analyzer.analyze(chess.Board(), root_moves=[chess.Move.from_uci("e2e4")])
            for board in (None, chess.Board(chess960=True), chess.Board.empty()):
                with self.assertRaises(ValueError):
                    analyzer.analyze(board)

    def test_finite_positive_limits_and_one_context_entry(self):
        for kwargs in ({"nodes": True}, {"nodes": 0}, {"minimum_depth": 0},
                       {"wall_seconds": float("nan")}, {"wall_seconds": float("inf")},
                       {"wall_seconds": 0}, {"wall_seconds": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.analyzer(**kwargs)
        analyzer = self.analyzer()
        with analyzer:
            pass
        with self.assertRaises(RuntimeError):
            analyzer.__enter__()

    def test_cli_refuses_existing_output_without_touching_files(self):
        output = self.root / "existing"
        output.mkdir()
        sentinel = output / "summary.json"
        sentinel.write_text("original experiment\n")
        args = cli.parser().parse_args(["curriculum", "--output", str(output)])
        with patch.object(cli, "build_curriculum", side_effect=AssertionError("must not rebuild")):
            with self.assertRaises(FileExistsError):
                cli.run(args)
        self.assertEqual(sentinel.read_text(), "original experiment\n")

    def test_analyzer_refuses_existing_directory_and_preserves_manifest(self):
        output = self.root / "analysis"
        output.mkdir()
        manifest = output / "analysis_manifest.json"
        manifest.write_text("previous analysis\n")
        with self.assertRaises(FileExistsError):
            with self.analyzer():
                self.fail("Existing research directory was reused")
        self.assertEqual(manifest.read_text(), "previous analysis\n")
        self.assertEqual(FakeTeacher.instances, [])

    def test_startup_checkpoint_failure_closes_started_teacher(self):
        with patch.object(core, "write_json", side_effect=OSError("checkpoint unavailable")):
            with self.assertRaisesRegex(OSError, "checkpoint unavailable"):
                self.analyzer().__enter__()
        self.assertEqual(len(FakeTeacher.instances), 1)
        teacher = FakeTeacher.instances[0]
        self.assertTrue(teacher.closed)
        self.assertTrue(teacher.cleanup_status["transport_closed"])

    def test_runtime_requires_own_engine_and_activity_option(self):
        # Bypass process startup only; exercise the real runtime identity/profile
        # check, including cleanup of a rejected teacher.
        self.teacher_patch.stop()
        for name, activity in (("Stockfish fixture", True), ("Regicide fixture", False)):
            with self.subTest(name=name, activity=activity):
                teacher = core._RuntimeTeacher(self.binary, self.root / "raw.jsonl", 10, 1e12, 1)
                teacher.identity = {"name": name}
                teacher.options = {"Hash": 16}
                options = {"Hash": Mock(default=16)}
                if activity:
                    options["UsePieceActivity"] = Mock(default=False)
                teacher._engine = Mock(options=options)
                with patch.object(core.EngineTeacher, "__enter__", return_value=teacher), patch.object(
                        teacher, "close") as close, self.assertRaises(ValueError):
                    teacher.__enter__()
                close.assert_called_once()
        self.teacher_patch.start()


if __name__ == "__main__":
    unittest.main()
