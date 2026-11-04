"""Restricted native query/cache contracts; only fake UCI processes are used."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import gambit_research_session as research
from neural_search_data.engine import CLASSICAL_OPTIONS, EngineTeacher, SearchInterrupted, normalize_root_moves


FAKE_UCI = r'''
import sys
import time
import chess
board = chess.Board()
for raw in sys.stdin:
    line = raw.strip()
    if line == "uci":
        print("id name Restricted query test double")
        for name, value in OPTIONS.items():
            kind = "check" if isinstance(value, bool) else "spin"
            suffix = "" if kind == "check" else " min 0 max 256"
            print(f"option name {name} type {kind} default {str(value).lower()}{suffix}")
        print("uciok", flush=True)
    elif line == "isready":
        print("readyok", flush=True)
    elif line.startswith("position "):
        fields = line[len("position "):].split(" moves ", 1)
        board = chess.Board() if fields[0] == "startpos" else chess.Board(fields[0][4:])
        if len(fields) > 1:
            for uci in fields[1].split():
                board.push_uci(uci)
    elif line.startswith("go "):
        fields = line.split()
        nodes = int(fields[fields.index("nodes") + 1])
        if nodes == 40:
            time.sleep(10)
            continue
        candidates = [chess.Move.from_uci(uci) for uci in fields[fields.index("searchmoves") + 1:]] if "searchmoves" in fields else list(board.legal_moves)
        move = min(candidates, key=lambda item: item.uci())
        following = board.copy(stack=True)
        following.push(move)
        pv = [move.uci()]
        if any(following.legal_moves):
            pv.append(min(following.legal_moves, key=lambda item: item.uci()).uci())
        print(f"info depth 4 score cp 25 nodes {nodes} time 1 pv {' '.join(pv)}")
        print(f"bestmove {move.uci()}", flush=True)
    elif line == "quit":
        break
'''


class TeacherRestrictionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="regicide-root-query-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.fake = self.folder / "fake_uci.py"
        self.fake.write_text(FAKE_UCI.replace("OPTIONS", repr(CLASSICAL_OPTIONS)), encoding="utf-8")
        original = chess.engine.SimpleEngine.popen
        def launch(protocol, command, **kwargs):
            return original(protocol, [sys.executable, str(self.fake)], **kwargs)
        patcher = patch("neural_search_data.engine.chess.engine.SimpleEngine.popen", side_effect=launch)
        patcher.start()
        self.addCleanup(patcher.stop)

    def teacher(self, *, nodes=37, timeout=1):
        return EngineTeacher(Path(sys.executable), self.folder / "raw.jsonl", nodes,
                             time.monotonic() + 10, timeout)

    def sent(self):
        rows = [json.loads(line) for line in (self.folder / "raw.jsonl").read_text(encoding="utf-8").splitlines()]
        return [row["line"] for row in rows if row["direction"] == "send"]

    def test_default_result_and_go_remain_unrestricted(self):
        with self.teacher() as teacher:
            result = teacher.query(chess.Board(), "free")
        self.assertNotIn("root_moves_uci", result)
        self.assertIn("go nodes 37", self.sent())
        self.assertFalse(any("searchmoves" in line for line in self.sent()))
        self.assertTrue(result["completed"])
        self.assertEqual(result["score"], {"kind": "cp", "stm": 25})
        self.assertEqual(teacher.cleanup_status["returncode"], 0)

    def test_root_set_reaches_uci_with_full_history_and_stable_provenance(self):
        board = chess.Board()
        for uci in ("g1f3", "g8f6", "f3g1", "f6g8"):
            board.push_uci(uci)
        before = board.fen(en_passant="fen"), list(board.move_stack)
        roots = [chess.Move.from_uci(uci) for uci in ("e2e4", "d2d4", "e2e4")]
        with self.teacher() as teacher:
            result = teacher.query(board, "restricted", root_moves=roots)
        self.assertEqual(result["root_moves_uci"], ["d2d4", "e2e4"])
        self.assertEqual(result["bestmove_uci"], "d2d4")
        self.assertEqual(result["pv_uci"][0], "d2d4")
        self.assertIn("go nodes 37 searchmoves d2d4 e2e4", self.sent())
        self.assertIn("position startpos moves g1f3 g8f6 f3g1 f6g8", self.sent())
        self.assertEqual(before, (board.fen(en_passant="fen"), list(board.move_stack)))
        self.assertEqual([move.uci() for move in roots], ["e2e4", "d2d4", "e2e4"])
        self.assertEqual(teacher.cleanup_status["returncode"], 0)

    def test_invalid_root_sets_fail_before_any_go_command(self):
        with self.teacher() as teacher:
            for roots in ([], (), "e2e4", ["e2e4"], [None], [chess.Move.null()],
                          [chess.Move.from_uci("e2e5")], [chess.Move(64, 0)]):
                with self.subTest(roots=roots), self.assertRaises(ValueError):
                    teacher.query(chess.Board(), "invalid", root_moves=roots)
        self.assertFalse(any(line.startswith("go ") for line in self.sent()))

    def test_interruption_retains_root_provenance_without_completion(self):
        with self.teacher(nodes=40, timeout=.05) as teacher:
            with self.assertRaises(SearchInterrupted) as caught:
                teacher.query(chess.Board(), "timeout", root_moves=[chess.Move.from_uci("e2e4")])
        self.assertFalse(caught.exception.record["completed"])
        self.assertEqual(caught.exception.record["root_moves_uci"], ["e2e4"])
        self.assertTrue(teacher.closed)
        self.assertTrue(teacher.cleanup_status["returncode_observed"])

    def test_special_legal_roots_and_copy_isolation(self):
        for fen, uci in (("4k3/8/8/8/8/8/8/4K2R w K - 0 1", "e1g1"),
                         ("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1", "e5d6"),
                         ("4k3/P7/8/8/8/8/8/4K3 w - - 0 1", "a7a8n")):
            with self.subTest(uci=uci):
                original = chess.Move.from_uci(uci)
                roots = normalize_root_moves(chess.Board(fen), [original])
                self.assertEqual(roots, [original])
                self.assertIsNot(roots[0], original)


class FakeTeacher:
    def __init__(self, raw_path, *, invalid=None):
        self.raw_path = raw_path
        self.nodes = 37
        self.identity = {"name": "restricted-cache-fake"}
        self.options = {"Hash": 16}
        self.runtime_defaults = {"Hash": 16}
        self.calls = []
        self.seq = 0
        self.invalid = invalid

    def query(self, board, query_id, **kwargs):
        self.calls.append({"query_id": query_id, "root_moves": copy.deepcopy(kwargs),
                           "history": [move.uci() for move in board.move_stack]})
        roots = kwargs.get("root_moves")
        move = min(roots if roots is not None else board.legal_moves, key=lambda item: item.uci())
        if self.invalid == "outside":
            move = next(move for move in board.legal_moves if move not in roots)
        following = board.copy(stack=True)
        following.push(move)
        pv = [move.uci()]
        if any(following.legal_moves):
            pv.append(min(following.legal_moves, key=lambda item: item.uci()).uci())
        begin = self.seq + 1
        with self.raw_path.open("a", encoding="utf-8", newline="\n") as stream:
            for direction, line in (("send", "go nodes 37"), ("recv", "bestmove " + move.uci())):
                self.seq += 1
                stream.write(json.dumps({"seq": self.seq, "direction": direction, "line": line}) + "\n")
        result = {"query_id": query_id, "requested_nodes": 37, "nodes": 37, "depth": 4,
                  "score": {"kind": "cp", "stm": 25}, "bestmove_uci": move.uci(), "pv_uci": pv,
                  "completed": True, "lowerbound": False, "upperbound": False,
                  "uci_begin_seq": begin, "uci_end_seq": self.seq}
        if roots is not None:
            result["root_moves_uci"] = [move.uci() for move in roots]
        if self.invalid == "missing":
            result.pop("root_moves_uci", None)
        elif self.invalid == "mismatch":
            result["root_moves_uci"] = ["a2a3"]
        elif self.invalid == "unexpected":
            result["root_moves_uci"] = [move.uci()]
        elif self.invalid == "nodes":
            result["nodes"] = 38
        elif self.invalid == "bound":
            result["lowerbound"] = True
        elif self.invalid == "mutate_request":
            roots.clear()
            board.push(move)
        return result


class SessionRestrictionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="regicide-root-cache-")
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name) / "run"
        self.board = chess.Board()
        self.roots = [chess.Move.from_uci(uci) for uci in ("e2e4", "d2d4")]

    def session(self, **kwargs):
        return research.ResearchSession(self.output, {"nodes": 37}, **kwargs)

    def cache(self):
        return next((self.output / "completed-queries").glob("*.json"))

    def test_normalized_same_set_reuses_deepcopy_and_raw_evidence_across_resume(self):
        with self.session() as session:
            teacher = FakeTeacher(session.raw_path)
            result = session.query(teacher, self.board, "same", root_moves=self.roots + self.roots[:1])
            first_raw = result["raw_log"]
            cached = json.loads(self.cache().read_text(encoding="utf-8"))
            self.assertEqual(cached["request"]["root_moves_uci"], ["d2d4", "e2e4"])
            self.assertEqual(len(teacher.calls), 1)
            result["root_moves_uci"].clear()
            result["pv_uci"].clear()
        with self.session(resume=True, max_new_queries=0) as session:
            teacher = FakeTeacher(session.raw_path)
            reused = session.query(teacher, self.board, "same", root_moves=list(reversed(self.roots)))
            self.assertEqual(reused["root_moves_uci"], ["d2d4", "e2e4"])
            self.assertEqual(reused["pv_uci"][0], "d2d4")
            self.assertEqual(reused["raw_log"], first_raw)
            self.assertEqual((session.new_queries, session.reused_queries), (0, 1))
            self.assertEqual(teacher.calls, [])

    def test_changed_set_or_removed_restriction_cannot_reuse_same_query_id(self):
        with self.session() as session:
            teacher = FakeTeacher(session.raw_path)
            session.query(teacher, self.board, "same", root_moves=self.roots)
            for roots in (None, self.roots[:1], self.roots + [chess.Move.from_uci("c2c4")]):
                with self.subTest(roots=roots), self.assertRaisesRegex(ValueError, "request"):
                    session.query(teacher, self.board, "same", root_moves=roots)
            self.assertEqual(len(teacher.calls), 1)

    def test_unrestricted_requests_retain_old_shape_and_cannot_be_relabelled_restricted(self):
        with self.session() as session:
            teacher = FakeTeacher(session.raw_path)
            result = session.query(teacher, self.board, "free")
            self.assertNotIn("root_moves_uci", result)
            self.assertEqual(teacher.calls[0]["root_moves"], {})
            cached = json.loads(self.cache().read_text(encoding="utf-8"))
            self.assertNotIn("root_moves_uci", cached["request"])
            with self.assertRaisesRegex(ValueError, "request"):
                session.query(teacher, self.board, "free", root_moves=self.roots)

    def test_invalid_roots_do_not_consume_query_budget_or_call_teacher(self):
        with self.session(max_new_queries=1) as session:
            teacher = FakeTeacher(session.raw_path)
            for roots in ([], ["e2e4"], [chess.Move.from_uci("e2e5")], [chess.Move(64, 0)], (self.roots[0],)):
                with self.subTest(roots=roots), self.assertRaises(ValueError):
                    session.query(teacher, self.board, "invalid", root_moves=roots)
            self.assertEqual((session.new_queries, session.reused_queries), (0, 0))
            self.assertEqual(teacher.calls, [])
            self.assertEqual(list((self.output / "completed-queries").iterdir()), [])

    def test_outside_move_missing_or_wrong_provenance_bound_and_nodes_never_checkpoint(self):
        for invalid in ("outside", "missing", "mismatch", "bound", "nodes"):
            with self.subTest(invalid=invalid):
                self.output = self.output.parent / invalid
                with self.session() as session:
                    teacher = FakeTeacher(session.raw_path, invalid=invalid)
                    with self.assertRaises(ValueError):
                        session.query(teacher, self.board, "bad", root_moves=self.roots)
                    self.assertEqual(list((self.output / "completed-queries").iterdir()), [])
                    self.assertEqual(session.new_queries, 1)

    def test_unrestricted_answer_cannot_smuggle_in_restricted_provenance(self):
        with self.session() as session:
            teacher = FakeTeacher(session.raw_path, invalid="unexpected")
            with self.assertRaisesRegex(ValueError, "provenance"):
                session.query(teacher, self.board, "free")
            self.assertEqual(list((self.output / "completed-queries").iterdir()), [])

    def test_recomputed_checksum_does_not_bypass_cached_root_validation(self):
        for corruption in ("outside", "provenance"):
            with self.subTest(corruption=corruption):
                self.output = self.output.parent / corruption
                with self.session() as session:
                    teacher = FakeTeacher(session.raw_path)
                    session.query(teacher, self.board, "cached", root_moves=self.roots)
                    path = self.cache()
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if corruption == "outside":
                        value["result"]["bestmove_uci"] = "a2a3"
                        value["result"]["pv_uci"] = ["a2a3", "a7a6"]
                    else:
                        value["result"]["root_moves_uci"] = ["d2d4"]
                    value["checksum"] = research._digest({key: item for key, item in value.items() if key != "checksum"})
                    research.atomic_write(path, value)
                    with self.assertRaisesRegex(ValueError, "root restriction"):
                        session.query(teacher, self.board, "cached", root_moves=self.roots)
                    self.assertEqual(len(teacher.calls), 1)
                    self.assertEqual(session.reused_queries, 0)

    def test_teacher_cannot_mutate_caller_history_or_normalized_validation_roots(self):
        for uci in ("g1f3", "g8f6", "f3g1", "f6g8"):
            self.board.push_uci(uci)
        before = self.board.fen(en_passant="fen"), list(self.board.move_stack)
        with self.session() as session:
            teacher = FakeTeacher(session.raw_path, invalid="mutate_request")
            result = session.query(teacher, self.board, "isolated", root_moves=self.roots)
            self.assertEqual(result["root_moves_uci"], ["d2d4", "e2e4"])
            self.assertEqual(before, (self.board.fen(en_passant="fen"), list(self.board.move_stack)))
            self.assertEqual([move.uci() for move in self.roots], ["e2e4", "d2d4"])
            request = json.loads(self.cache().read_text(encoding="utf-8"))["request"]
            self.assertEqual(request["history_uci"], ["g1f3", "g8f6", "f3g1", "f6g8"])


if __name__ == "__main__":
    unittest.main()
