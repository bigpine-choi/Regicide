"""Scoped memory selection for isolated research hosts; no native engine runs."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import boss_play
import gambit_practice_memory as memory
import gambit_repertoire_play as verifier
from gambit_practice_memory_tests import artifact, board, IDENTITY
import human_play_tests as harness


class MemoryRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.default_path = memory.DEFAULT_MEMORY
        cls.default_sha = hashlib.sha256(cls.default_path.read_bytes()).hexdigest()
        cls.default_identity = memory.memory_identity()

    @classmethod
    def tearDownClass(cls):
        if memory.DEFAULT_MEMORY != cls.default_path or hashlib.sha256(cls.default_path.read_bytes()).hexdigest() != cls.default_sha:
            raise AssertionError("Active memory or its path changed")
        if memory.memory_identity() != cls.default_identity:
            raise AssertionError("Scoped memory selection leaked into the default context")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="regicide-memory-runtime-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.first, self.second = self.folder / "first.json", self.folder / "second.json"
        self.values = []
        for path, prefer_castling in ((self.first, False), (self.second, True)):
            value = artifact()
            if prefer_castling:
                value["records"][0]["candidates"][1]["offline_owner_cp"] = 30
            value["memory_id"] = memory.memory_digest(value)
            memory._validate(value)
            path.write_text(json.dumps(value), encoding="utf-8")
            self.values.append(value)
        self.ids = [value["memory_id"] for value in self.values]

    def proposals(self, **kwargs):
        return memory.proposals(board(), "runtime-test", engine_identity=IDENTITY, **kwargs)

    def plan(self):
        return boss_play.plan(board(), 1000, "runtime-test", engine_identity=IDENTITY)

    def key(self):
        return boss_play.memo_key(board(), 1000, IDENTITY["binary_sha256"], IDENTITY["evaluation_id"], seed="runtime-test")

    def test_default_path_and_identity_remain_unchanged_after_a_scoped_read(self):
        self.assertEqual(memory.memory_identity(), self.default_identity)
        with memory.using_memory(self.first) as path:
            self.assertEqual(path, self.first.resolve())
            self.assertEqual(memory.memory_identity()["memory_id"], self.ids[0])
            self.assertEqual(memory.metadata()["memory_id"], self.ids[0])
        self.assertEqual(memory.memory_identity(), self.default_identity)
        self.assertEqual(memory.DEFAULT_MEMORY, self.default_path)

    def test_nested_exception_restores_parent_and_none_temporarily_selects_default(self):
        with memory.using_memory(self.first):
            with self.assertRaisesRegex(RuntimeError, "controlled"):
                with memory.using_memory(self.second):
                    self.assertEqual(memory.memory_identity()["memory_id"], self.ids[1])
                    raise RuntimeError("controlled")
            self.assertEqual(memory.memory_identity()["memory_id"], self.ids[0])
            with memory.using_memory(None):
                self.assertEqual(memory.memory_identity(), self.default_identity)
            self.assertEqual(memory.memory_identity()["memory_id"], self.ids[0])
        self.assertEqual(memory.memory_identity(), self.default_identity)

    def test_explicit_arguments_override_every_implicit_memory_api(self):
        with memory.using_memory(self.first):
            self.assertEqual(memory.memory_identity(self.second)["memory_id"], self.ids[1])
            self.assertEqual(memory.metadata(self.second)["memory_id"], self.ids[1])
            row = self.proposals(memory_path=self.second)[0]
            self.assertEqual((row["memory_id"], row["uci"]), (self.ids[1], "e1g1"))
            self.assertTrue(memory.valid_proposal(board(), row, engine_identity=IDENTITY, memory_path=self.second))
            self.assertFalse(memory.valid_proposal(board(), row, engine_identity=IDENTITY))
            self.assertEqual(memory.memory_identity()["memory_id"], self.ids[0])

    def test_missing_scoped_memory_fails_closed_without_using_active_memory(self):
        with memory.using_memory(self.folder / "missing.json"):
            self.assertEqual(memory.memory_identity()["status"], "unavailable")
            self.assertFalse(memory.metadata()["available"])
            self.assertEqual(self.proposals(), [])
        self.assertEqual(memory.memory_identity(), self.default_identity)

    def test_metadata_proposals_plan_memo_and_authoritative_validation_use_one_memory(self):
        keys, plans = [], []
        for path, expected, move in ((self.first, self.ids[0], "c2c3"), (self.second, self.ids[1], "e1g1")):
            with memory.using_memory(path):
                self.assertEqual(memory.metadata()["memory_id"], expected)
                proposal = self.proposals()[0]
                self.assertEqual((proposal["memory_id"], proposal["uci"]), (expected, move))
                self.assertTrue(memory.valid_proposal(board(), proposal, engine_identity=IDENTITY))
                plan = self.plan()
                self.assertEqual(plan["practice_memory"]["memory_id"], expected)
                self.assertEqual(plan["knowledge_candidates"][0]["memory_id"], expected)
                self.assertEqual(plan["knowledge_candidates"][0]["uci"], move)
                plans.append(plan)
                keys.append(self.key())
        self.assertNotEqual(keys[0], keys[1])
        with memory.using_memory(self.first):
            self.assertEqual(keys[0], self.key())
            self.assertFalse(memory.valid_proposal(board(), plans[1]["knowledge_candidates"][0], engine_identity=IDENTITY))

    def test_verifier_resolves_the_scoped_authoritative_memory_again(self):
        position = board()
        with memory.using_memory(self.first):
            plan = self.plan()
            proposal = self.proposals()[0]
            plan["knowledge_candidates"] = [proposal]
            moves = [chess.Move.from_uci(uci) for uci in proposal["offline_evidence"]["pv_uci"]]
            baseline = moves[0]
            info = {"depth": 6, "score": chess.engine.PovScore(chess.engine.Cp(0), position.turn), "pv": moves}
            accepted, discard = verifier.choose(position, plan, baseline, info, {6: info}, "scoped",
                lambda *_a, **_k: self.fail("An identical baseline needs no additional search"), lambda: None)
            self.assertFalse(discard)
            self.assertEqual(accepted["reason"], "knowledge_move_is_baseline")
            self.assertEqual(accepted["evidence"]["source"]["memory_id"], self.ids[0])
        with memory.using_memory(self.second):
            rejected, discard = verifier.choose(position, plan, baseline, info, {6: info}, "scoped",
                lambda *_a, **_k: self.fail("A proposal from another memory must not authorize a search"), lambda: None)
            self.assertFalse(discard)
            self.assertEqual(rejected["reason"], "knowledge_context_changed")

    def test_simultaneous_thread_scopes_do_not_cross_contaminate(self):
        barrier = threading.Barrier(2)
        def inspect(path, expected):
            with memory.using_memory(path):
                barrier.wait(timeout=5)
                observed = [(memory.memory_identity()["memory_id"], memory.metadata()["memory_id"],
                             self.plan()["practice_memory"]["memory_id"]) for _ in range(5)]
                barrier.wait(timeout=5)
                self.assertTrue(all(row == (expected, expected, expected) for row in observed))
                return self.key()
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(inspect, self.first, self.ids[0])
            b = pool.submit(inspect, self.second, self.ids[1])
            self.assertNotEqual(a.result(timeout=10), b.result(timeout=10))
        self.assertEqual(memory.memory_identity(), self.default_identity)

    def test_independent_contexts_can_share_a_thread_without_global_path_mutation(self):
        contexts = [copy_context(), copy_context()]
        def inspect(path):
            with memory.using_memory(path):
                return memory.memory_identity(), self.key()
        first = contexts[0].run(inspect, self.first)
        second = contexts[1].run(inspect, self.second)
        self.assertEqual(first[0]["memory_id"], self.ids[0])
        self.assertEqual(second[0]["memory_id"], self.ids[1])
        self.assertNotEqual(first[1], second[1])
        self.assertEqual(memory.DEFAULT_MEMORY, self.default_path)

    def test_two_wrapped_hosts_keep_metadata_and_saved_receipts_in_their_own_scope(self):
        binary = self.folder / "fake-peer.exe"
        binary.write_bytes(b"test peer only; never executed")
        hosts = []
        for index, path in enumerate((self.first, self.second)):
            with memory.using_memory(path):
                factory = harness.FakeFactory()
                game = harness.play_human.HumanGame(binary, self.folder / ("host-" + str(index)), factory,
                    hint_engine_factory=harness.FakeFactory(), evaluation_engine_factory=harness.FakeFactory(),
                    opening_variety_enabled=False)
                hosts.append((game, path, factory, self.ids[index]))
                self.addCleanup(self.close_host, game, path)
                game.new_game("white", 100, game.revision, boss_mode=True)
        for game, path, factory, expected in [*hosts, *reversed(hosts)]:
            with memory.using_memory(path):
                state = game.state()
                self.assertTrue(state["human_to_move"])
                move = next(iter(game.board.legal_moves)).uci()
                game.human_move(move, game.revision)
                reply = next(iter(game.board.legal_moves)).uci()
                factory.instances[-1].replies.append(reply)
                game.engine_move(game.revision)
                self.assertEqual(game._boss_metadata()["practice_memory"]["memory_id"], expected)
                saved = json.loads((game.save_dir / (game.game_id + ".json")).read_text(encoding="utf-8"))
                self.assertEqual(saved["boss_play"]["practice_memory"]["memory_id"], expected)
                self.assertIsNone(game.state()["error"])
        self.assertEqual(memory.memory_identity(), self.default_identity)

    @staticmethod
    def close_host(game, path):
        with memory.using_memory(path):
            game.close()


if __name__ == "__main__":
    unittest.main()
