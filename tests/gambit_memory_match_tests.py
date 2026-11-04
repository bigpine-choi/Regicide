"""Two production hosts and controlled peers, without native searches."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import chess
import chess.engine
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import gambit_memory_match as match
import gambit_practice_memory as memory
from boss_strength_match_tests import Peer

CYCLE = ["g1f3", "g8f6", "f3g1", "f6g8"]
EVANS = "e2e4 e7e5 g1f3 b8c6 f1c4 f8c5 b2b4 c5b4".split()


def artifact(identity, castle=False):
    board = memory._board(EVANS)
    words = ["c2c3", "e1g1"] if castle else ["c2c3"]
    candidates = []
    for word in words:
        child = board.copy(stack=True)
        child.push_uci(word)
        candidates.append({"uci": word, "offline_owner_cp": 0, "native_depth": 4, "requested_nodes": 100000,
            "query_id": "query-" + word, "goal": "development", "pv_uci": [word, next(iter(child.legal_moves)).uci()]})
    value = {"schema": memory.SCHEMA, "policy": memory.POLICY, "status": "approved",
        "engine_identity": identity, "heldout_family_ids": ["from", "benko"], "heldout_position_keys": [],
        "records": [{"record_id": "evans:1", "case_id": "evans", "family_id": "evans", "split": "train",
            "owner": "white", "goal": "development", "fen": board.fen(en_passant="fen"), "history_uci": EVANS,
            "position_key": memory.position_key(board), "candidates": candidates}], "acceptance": {"passed": True}}
    value["memory_id"] = memory.memory_digest(value)
    memory._validate(value)
    return value


class MemoryPeer(Peer):
    def __init__(self, *, illegal=False, repeating=False, exit_code=0):
        super().__init__(illegal=illegal)
        self.repeating = repeating
        self.memory_ids = []
        self.returncode = self
        self.exit_code = exit_code

    def result(self, timeout=None):
        return self.exit_code

    def play(self, board, limit, **kwargs):
        self.memory_ids.append(memory.memory_identity()["memory_id"])
        if self.repeating:
            move = chess.Move.from_uci(CYCLE[len(board.move_stack) % 4])
            self.calls.append({"board": board.copy(stack=True), "limit": limit, **kwargs})
            return chess.engine.PlayResult(move, None, {"depth": 2,
                "score": chess.engine.PovScore(chess.engine.Cp(9000), board.turn), "pv": [move]})
        return super().play(board, limit, **kwargs)


class MemoryMatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="regicide-memory-match-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.binary = self.folder / "controlled.exe"
        self.binary.write_bytes(b"controlled own-engine peer; never executed")
        self.identity = {"binary_sha256": hashlib.sha256(self.binary.read_bytes()).hexdigest(), "evaluation_id": match.EVALUATION_ID}
        self.paths, self.values = {}, {}
        for arm, castle in (("baseline", False), ("candidate", True)):
            value = artifact(copy.deepcopy(self.identity), castle)
            path = self.folder / (arm + ".json")
            path.write_text(json.dumps(value), encoding="utf-8")
            self.paths[arm], self.values[arm] = path, value
        self.cases = self.folder / "cases.json"
        self.write_cases([{"id": "style", "stratum": "style", "history_uci": [], "seed": "paired-seed"}])
        self.config = {"binary": str(self.binary), "baseline_memory": str(self.paths["baseline"]),
            "candidate_memory": str(self.paths["candidate"]), "cases": str(self.cases), "pairs": 1,
            "pair_start": 1, "movetime_ms": 500, "max_plies": 240, "seed": "match-test",
            "output_dir": str(self.folder / "out"), "workers": 1}
        self.peers = {arm: [] for arm in match.ARMS}
        self.hosts = []

    def write_cases(self, rows):
        self.cases.write_text(json.dumps(rows), encoding="utf-8")

    def run_match(self, *, illegal=False, repeating=False, broken_relay=False, exit_code=0):
        owner = self
        class ObservedHost(match.HumanGame):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                owner.hosts.append(self)
            def human_move(self, *args):
                state = super().human_move(*args)
                if broken_relay:
                    self.board.pop()
                return state
        def factory(arm):
            def make(path):
                peer = MemoryPeer(illegal=illegal and arm == "candidate", repeating=repeating,
                                  exit_code=exit_code if arm == "candidate" else 0)
                self.peers[arm].append(peer)
                return peer
            return make
        with patch.object(match, "HumanGame", ObservedHost), contextlib.redirect_stdout(io.StringIO()):
            return match.run_match(self.config, baseline_factory=factory("baseline"), candidate_factory=factory("candidate"))

    def record(self, row):
        return json.loads((self.folder / "out" / row["record_file"]).read_text(encoding="utf-8"))

    def test_schedule_has_sixteen_games_shared_seeds_and_alternating_first_color(self):
        cases = [{"id": str(i), "stratum": "style" if i < 4 else "target", "history_uci": []} for i in range(8)]
        rows = match.schedule(cases)
        self.assertEqual(len(rows), 16)
        self.assertEqual([row["game"] for row in rows], list(range(1, 17)))
        for index, (left, right) in enumerate(zip(rows[::2], rows[1::2])):
            self.assertEqual(left["pair_seed"], right["pair_seed"])
            self.assertEqual(left["case"], right["case"])
            self.assertNotEqual(left["candidate_color"], right["candidate_color"])
            self.assertEqual(left["candidate_color"], "white" if index % 2 == 0 else "black")
        self.assertEqual(rows[4:8], match.schedule(cases, pairs=2, pair_start=3))

    def test_real_hosts_use_scoped_memories_same_modes_budget_and_legal_pgn(self):
        originals = {arm: path.read_bytes() for arm, path in self.paths.items()}
        report = self.run_match()
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["summary"]["scored_games"], 2)
        self.assertEqual((report["summary"]["wins"], report["summary"]["losses"]), (1, 1))
        self.assertTrue(report["sources_unchanged"] and report["inputs_unchanged"])
        for row in report["games"]:
            saved = self.record(row)
            self.assertTrue(saved["pgn_verification"]["passed"])
            self.assertEqual(saved["uci_moves"], ["f2f3", "e7e5", "g2g4", "d8h4"])
            for arm in match.ARMS:
                host = saved["hosts"][arm]
                self.assertEqual(host["memory_id"], self.values[arm]["memory_id"])
                self.assertEqual(host["scoped_memory"]["memory_id"], self.values[arm]["memory_id"])
                self.assertTrue(host["boss_play"]["enabled"])
                self.assertFalse(host["forced_plies"] or host["clock_enabled"])
                self.assertEqual(host["uci_moves"], saved["uci_moves"])
                for engine in host["engines"]:
                    self.assertEqual(engine["process_exit_code"], 0)
                    self.assertTrue(engine["closed"])
            for turn in saved["moves"]:
                self.assertEqual(turn["baseline_budget_ms"], 500)
                self.assertLessEqual(turn["advertised_extra_budget_ms"], 500)
                self.assertEqual(turn["history_uci"], saved["uci_moves"][:turn["ply"] - 1])
                self.assertIn("activity", turn["metrics"])
            pgn = chess.pgn.read_game(io.StringIO((self.folder / "out" / row["pgn_file"]).read_text()))
            self.assertEqual(pgn.headers["DrawPolicy"], match.DRAW_POLICY)
            self.assertEqual(pgn.end().board().fen(en_passant="fen"), saved["final_fen"])
        for arm in match.ARMS:
            self.assertEqual(self.paths[arm].read_bytes(), originals[arm])
            for peer in self.peers[arm]:
                self.assertTrue(peer.closed)
                self.assertEqual(set(peer.memory_ids), {self.values[arm]["memory_id"]})
                self.assertEqual(peer.configured, {"Hash": 16, "Threads": 1, "UsePieceActivity": True})
        self.assertTrue(all(host.boss_mode and host.dynamic_play_enabled and host.opening_variety_enabled for host in self.hosts))

    def test_full_prefix_is_installed_without_forced_flags_and_cap_counts_new_moves_only(self):
        prefix = ["f2f3", "e7e5"]
        self.write_cases([{"id": "prefix", "stratum": "style", "history_uci": prefix}])
        self.config["max_plies"] = 1
        report = self.run_match()
        self.assertEqual(report["summary"]["scored_games"], 0)
        for row in report["games"]:
            saved = self.record(row)
            self.assertEqual((saved["result"], saved["played_plies"], saved["total_plies"]), ("*", 1, 3))
            self.assertEqual(saved["initial_history_uci"], prefix)
            self.assertEqual(len(saved["moves"]), 1)
            self.assertEqual(saved["moves"][0]["history_uci"], prefix)
            self.assertEqual(saved["moves"][0]["metrics"]["ply"], 3)

    def test_intended_repetition_is_not_a_draw_until_actual_eighth_ply(self):
        prefix = (CYCLE * 2)[:7]
        self.write_cases([{"id": "repetition", "stratum": "target", "history_uci": prefix}])
        report = self.run_match(repeating=True)
        self.assertEqual(report["strata"]["target"]["draws"], 2)
        for row in report["games"]:
            self.assertEqual(row["played_plies"], 1)
            self.assertEqual(row["termination"], "threefold_repetition")
            self.assertTrue(self.record(row)["pgn_verification"]["passed"])

    def test_end_state_fifty_move_and_mate_priority(self):
        board = chess.Board("8/8/8/8/8/2k5/8/R5K1 w - - 99 51")
        self.assertIsNone(match.end_state(board, 0, 240))
        board.push_uci("a1a2")
        self.assertEqual(match.end_state(board, 1, 240)["termination"], "fifty_moves")
        board = chess.Board("7k/8/5KQ1/8/8/8/8/8 w - - 149 75")
        board.push_uci("g6g7")
        self.assertEqual(match.end_state(board, 1, 1)["termination"], "checkmate")

    def test_actionable_comparison_ignores_memory_id_only_changes(self):
        config = {**self.config, "engine_identity": self.identity}
        entry = {"pair_seed": "same"}
        plan = {"eligible": True, "mode": "initiative", "knowledge_candidates": [
            {"source": "native_practice_memory", "uci": "c2c3", "goal": "development"}]}
        with patch.object(match.boss_play, "plan", return_value=plan):
            value = match._plans(memory._board(EVANS), [], entry, config)
        self.assertFalse(value["actionable_delta"])
        self.assertNotEqual(value["arms"]["baseline"]["memory_id"], value["arms"]["candidate"]["memory_id"])
        with patch.object(match.boss_play, "plan", side_effect=[plan, {**plan, "knowledge_candidates": []}]):
            self.assertTrue(match._plans(memory._board(EVANS), [], entry, config)["actionable_delta"])
        second = {"source": "native_practice_memory", "uci": "e1g1", "goal": "development"}
        ordered = {**plan, "knowledge_candidates": [*plan["knowledge_candidates"], second]}
        reversed_plan = {**plan, "knowledge_candidates": list(reversed(ordered["knowledge_candidates"]))}
        with patch.object(match.boss_play, "plan", side_effect=[ordered, reversed_plan]):
            self.assertTrue(match._plans(memory._board(EVANS), [], entry, config)["actionable_delta"])

    def test_illegal_peer_move_is_unfinished_and_stops_without_leaking(self):
        report = self.run_match(illegal=True)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(len(report["games"]), 1)
        self.assertEqual(report["summary"]["scored_games"], 0)
        self.assertEqual(report["games"][0]["result"], "*")
        self.assertTrue(all(peer.closed for peers in self.peers.values() for peer in peers))

    def test_relay_divergence_and_pgn_failure_cannot_be_scored(self):
        report = self.run_match(broken_relay=True)
        self.assertEqual(report["status"], "failed")
        self.assertIn("diverged after receiving", report["errors"][0])
        self.assertEqual(report["summary"]["scored_games"], 0)

    def test_pgn_export_error_still_closes_hosts_and_returns_unfinished(self):
        with patch.object(match, "match_pgn", side_effect=ValueError("controlled export failure")):
            report = self.run_match()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["games"][0]["result"], "*")
        self.assertEqual(report["summary"]["scored_games"], 0)
        self.assertTrue(all(peer.closed for peers in self.peers.values() for peer in peers))

    def test_nonzero_observed_exit_is_not_a_successful_match(self):
        report = self.run_match(exit_code=7)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["summary"]["scored_games"], 0)
        self.assertIn("exit", report["errors"][0])

    def test_existing_output_bad_time_and_memory_identity_are_rejected_before_engines(self):
        output = self.folder / "out"
        output.mkdir()
        marker = output / "keep.txt"
        marker.write_bytes(b"preserved")
        with self.assertRaises((ValueError, FileExistsError)):
            self.run_match()
        self.assertEqual(marker.read_bytes(), b"preserved")
        self.assertFalse(any(self.peers.values()))
        self.config.update(output_dir=str(self.folder / "other"), movetime_ms=499)
        with self.assertRaises(ValueError):
            self.run_match()
        self.config["movetime_ms"] = 500
        changed = copy.deepcopy(self.values["candidate"])
        changed["engine_identity"]["binary_sha256"] = "b" * 64
        changed["memory_id"] = memory.memory_digest(changed)
        self.paths["candidate"].write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.run_match()
        self.assertFalse(any(self.peers.values()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
