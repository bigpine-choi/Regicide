"""Offline discovery correctness tests: real chess rules, fake native scores."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import gambit_discovery as discovery


def case_from(fen=chess.STARTING_FEN, moves=(), **extra):
    board = chess.Board(fen)
    for uci in moves:
        board.push_uci(uci)
    return {"case_id": "test", "root_fen": fen, "history_uci": list(moves),
            "moves": list(moves), "fen": board.fen(en_passant="fen"),
            "owner": "white" if board.turn else "black", **extra}


class FakeAnalyzer:
    """Scores have exactly the documented side-to-move perspective."""
    def __init__(self, root, baseline, *, owner_score=0, scores=None, max_queries=None, depth=8):
        self.root = root
        self.baseline = baseline
        self.owner = root.turn
        self.owner_score = owner_score
        self.scores = scores
        self.max_queries = max_queries
        self.depth = depth
        self.calls = []

    def analyze(self, board, *, nodes=None, root_moves=None):
        assert root_moves is None, "discovery must use the after-move board API"
        self.calls.append((board.copy(stack=True), nodes))
        query_id = "q" + str(len(self.calls))
        if self.max_queries is not None and len(self.calls) > self.max_queries:
            return {"status": "unknown", "cp": None, "mate": None, "depth": 0,
                    "nodes": 0, "pv_uci": [], "query_id": query_id,
                    "reason": "fake_budget_exhausted"}
        outcome = board.outcome(claim_draw=True)
        if outcome is not None:
            return {"status": "terminal", "cp": 0 if outcome.winner is None else None,
                    "mate": None if outcome.winner is None else (1 if outcome.winner == board.turn else -1),
                    "depth": 0, "nodes": 0, "pv_uci": [], "query_id": query_id,
                    "bestmove_uci": None}
        is_root = (board.fen(en_passant="fen") == self.root.fen(en_passant="fen")
                   and len(board.move_stack) == len(self.root.move_stack))
        move = chess.Move.from_uci(self.baseline) if is_root else min(
            board.legal_moves, key=lambda item: (board.is_capture(item), item.uci()))
        assert move in board.legal_moves
        value = self.scores(board, is_root) if self.scores else self.owner_score
        cp = value if board.turn == self.owner else -value
        return {"status": "complete", "cp": cp, "mate": None, "depth": self.depth,
                "nodes": nodes or 100, "pv_uci": [move.uci()],
                "bestmove_uci": move.uci(), "query_id": query_id}


class DiscoveryTests(unittest.TestCase):
    NONMOVING = "4k3/8/8/8/8/8/3q4/3R2K1 w - - 0 1"

    def setUp(self):
        self.source_patch = patch.object(discovery.gambit_knowledge, "repertoire",
                                         return_value=[{"moves": ["e2e4"]}])
        self.source_mock = self.source_patch.start()
        self.real_full_loader = discovery._pinned_opening_source
        self.full_patch = patch.object(discovery, "_pinned_opening_source", return_value=(
            [], {"source_identity_sha256": "test-full-source", "catalog_rows": 0,
                 "license": "CC0-1.0", "all_file_hashes_verified": True}))
        self.full_mock = self.full_patch.start()
        discovery._INDEX_CACHE.clear()

    def tearDown(self):
        self.source_patch.stop()
        self.full_patch.stop()
        discovery._INDEX_CACHE.clear()

    def run_case(self, fen, baseline, *, case_extra=None, fake_extra=None, **kwargs):
        case = case_from(fen, **(case_extra or {}))
        analyzer = FakeAnalyzer(discovery.replay_case(case), baseline, **(fake_extra or {}))
        result = discovery.discover_case(case, analyzer, candidate_limit=1, **kwargs)
        return result, analyzer

    def test_full_history_and_fen_are_validated_before_query(self):
        case = case_from(moves=["e2e4", "e7e5"])
        board = discovery.replay_case(case)
        self.assertEqual([move.uci() for move in board.move_stack], ["e2e4", "e7e5"])
        analyzer = FakeAnalyzer(board, "g1f3")
        case["history_uci"] = ["e2e5"]
        case["moves"] = ["e2e5"]
        result = discovery.discover_case(case, analyzer)
        self.assertEqual(result["status"], "invalid")
        self.assertFalse(analyzer.calls)
        case = case_from(moves=["e2e4"])
        case["fen"] = chess.STARTING_FEN
        self.assertEqual(discovery.discover_case(case, analyzer)["status"], "invalid")

    def test_actual_curriculum_discovery_roots_accept_legal_and_raw_ep_fen(self):
        # Exercise the real curriculum-to-discovery boundary: after ...d5 c4
        # the stored legal-EP FEN omits c3, while replay retains the raw square.
        from gambit_research import discovery_cases
        from gambit_theory_curriculum import build_curriculum
        curriculum = build_curriculum(seed=20261105, roots_per_family=8)
        cases = discovery_cases(curriculum, 4)
        self.assertEqual(len(cases), 4)
        self.assertEqual({case["family_id"] for case in cases},
                         {"albin", "benko", "evans", "smith-morra"})
        for case in cases:
            with self.subTest(family=case["family_id"]):
                board = discovery.replay_case(case)
                self.assertEqual([move.uci() for move in board.move_stack], case["history_uci"])
                self.assertEqual(board.fen(en_passant="legal"), case["fen"])
                raw_case = {**case, "fen": board.fen(en_passant="fen")}
                self.assertEqual(discovery.replay_case(raw_case).fen(en_passant="fen"),
                                 board.fen(en_passant="fen"))
                wrong_counters = board.copy(stack=True)
                wrong_counters.halfmove_clock += 1
                with self.assertRaises(ValueError):
                    discovery.replay_case({**case, "fen": wrong_counters.fen(en_passant="legal")})
        albin = next(case for case in cases if case["family_id"] == "albin")
        self.assertEqual(albin["history_uci"], ["d2d4", "d7d5", "c2c4"])
        board = discovery.replay_case(albin)
        self.assertEqual(board.fen(en_passant="fen").split()[3], "c3")
        self.assertEqual(albin["fen"].split()[3], "-")

    def test_owner_and_history_alias_disagreement_rejected(self):
        case = case_from()
        analyzer = FakeAnalyzer(chess.Board(), "e2e4")
        case["owner"] = "black"
        self.assertEqual(discovery.discover_case(case, analyzer)["status"], "invalid")
        case = case_from()
        case["moves"] = ["e2e4"]
        self.assertEqual(discovery.discover_case(case, analyzer)["status"], "invalid")

    def test_missing_reference_index_does_not_make_everything_novel(self):
        self.source_mock.return_value = []
        case = case_from(self.NONMOVING)
        analyzer = FakeAnalyzer(discovery.replay_case(case), "g1h1")
        result = discovery.discover_case(case, analyzer)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "reference_scope_unavailable")
        self.assertFalse(result["candidates"])
        self.assertFalse(analyzer.calls)

    def test_sacrificed_piece_need_not_be_the_moving_piece(self):
        result, analyzer = self.run_case(self.NONMOVING, "g1h1")
        candidate = result["candidates"][0]
        self.assertTrue(candidate["bounded_screen_passed"])
        self.assertTrue(candidate["all_replies_complete"])
        accepted = next(row for row in candidate["replies"] if row["uci"] == "d2d1")
        evidence = accepted["material_evidence"]
        self.assertEqual(evidence["captured_piece"], "R")
        self.assertEqual(evidence["captured_square"], "d1")
        self.assertTrue(evidence["persistent_material_investment"])
        self.assertLess(evidence["balance_after_native_continuation_cp"],
                        evidence["balance_before_offer_cp"])
        self.assertGreater(len(analyzer.calls), 2)
        self.assertTrue(any(row["kind"] == "quiet_defense" for row in candidate["replies"]))

    def test_black_cp_perspective_is_converted_at_every_probe(self):
        fen = "3r2k1/3Q4/8/8/8/8/8/4K3 b - - 0 1"
        result, _ = self.run_case(fen, "g8h8", case_extra={"absolute_floor_cp": 0},
                                  fake_extra={"owner_score": 30}, max_loss_cp=0)
        row = result["candidates"][0]
        self.assertEqual(result["baseline"]["cp"], 30)
        self.assertEqual(row["native_after_offer"]["cp"], -30)
        self.assertEqual(row["native_after_offer"]["owner_cp"], 30)
        self.assertTrue(all(reply["native"]["owner_cp"] == 30 for reply in row["replies"]))
        self.assertTrue(row["bounded_screen_passed"])

    def test_reply_limit_is_explicitly_unknown_never_a_pass(self):
        result, _ = self.run_case(self.NONMOVING, "g1h1", reply_limit=1)
        row = result["candidates"][0]
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(row["status"], "unknown")
        self.assertTrue(row["replies_truncated"])
        self.assertFalse(row["all_replies_complete"])
        self.assertFalse(row["bounded_screen_passed"])
        self.assertGreater(row["legal_reply_count"], row["examined_reply_count"])

    def test_exhausted_native_budget_is_unknown(self):
        result, _ = self.run_case(self.NONMOVING, "g1h1", fake_extra={"max_queries": 2})
        row = result["candidates"][0]
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(row["all_replies_complete"])
        self.assertFalse(row["bounded_screen_passed"])
        self.assertTrue(any(reply["native"]["status"] == "unknown" for reply in row["replies"]))

    def test_insufficient_depth_cannot_pass(self):
        result, _ = self.run_case(self.NONMOVING, "g1h1", fake_extra={"depth": 3})
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["candidates"][0]["bounded_screen_passed"])

    def test_absolute_cp_floor_and_relative_loss_are_independent(self):
        floor, _ = self.run_case(self.NONMOVING, "g1h1", fake_extra={"owner_score": -150})
        self.assertEqual(floor["candidates"][0]["reason"], "native_score_gate_failed")
        relative, _ = self.run_case(self.NONMOVING, "g1h1", fake_extra={
            "scores": lambda board, is_root: 100 if is_root else 20})
        self.assertEqual(relative["candidates"][0]["reason"], "native_score_gate_failed")

    def test_immediate_recapture_is_not_certified_as_a_sacrifice(self):
        fen = "4k3/8/8/3p4/8/3P4/4P3/4K3 w - - 0 1"
        result, _ = self.run_case(fen, "e2e4")
        row = result["candidates"][0]
        accepted = next(item for item in row["replies"] if item["uci"] == "d5e4")
        self.assertIn("d3e4", accepted["material_evidence"]["immediate_material_recovery_uci"])
        self.assertFalse(row["bounded_screen_passed"])
        self.assertEqual(row["status"], "rejected")

    def test_equal_capture_exchange_not_even_a_material_offer(self):
        fen = "3qk3/8/8/3p4/2P5/8/8/4K3 w - - 0 1"
        result, _ = self.run_case(fen, "c4d5")
        row = result["candidates"][0]
        self.assertEqual(row["reason"], "no_immediate_material_offer")
        self.assertFalse(row["bounded_screen_passed"])

    def test_en_passant_acceptance_uses_actual_captured_square(self):
        fen = "4k3/8/8/8/3p4/8/4P3/4K3 w - - 0 1"
        result, _ = self.run_case(fen, "e2e4")
        row = result["candidates"][0]
        accepted = next(item for item in row["replies"] if item["uci"] == "d4e3")
        self.assertTrue(accepted["material_evidence"]["en_passant"])
        self.assertEqual(accepted["material_evidence"]["captured_square"], "e4")
        self.assertTrue(row["bounded_screen_passed"])

    def test_underpromotion_uses_promoted_piece_and_net_material_delta(self):
        fen = "1r2k3/P7/8/8/8/8/7K/8 w - - 0 1"
        result, _ = self.run_case(fen, "a7a8n")
        row = result["candidates"][0]
        accepted = next(item for item in row["replies"] if item["uci"] == "b8a8")
        self.assertEqual(accepted["material_evidence"]["captured_piece"], "N")
        self.assertEqual(accepted["material_evidence"]["balance_before_offer_cp"], -400)
        self.assertEqual(accepted["material_evidence"]["balance_after_acceptance_cp"], -500)
        self.assertTrue(row["bounded_screen_passed"])

    def test_transposition_is_known_position_even_when_edge_is_unseen(self):
        self.source_mock.return_value = [{"line_id": "one", "moves":
                                          ["e2e4", "e7e5", "g1f3", "b8c6"]}]
        case = case_from(moves=["g1f3", "b8c6", "e2e4"])
        analyzer = FakeAnalyzer(discovery.replay_case(case), "e7e5")
        result = discovery.discover_case(case, analyzer, candidate_limit=1)
        novelty = result["candidates"][0]["novelty"]
        self.assertEqual(novelty["classification"], "known_transposition")
        self.assertTrue(novelty["unseen_move"])
        self.assertFalse(novelty["unseen_position"])

    def test_known_transition_retains_source_scope_hash(self):
        self.source_mock.return_value = [{"moves": ["e2e4"]}]
        case = case_from()
        analyzer = FakeAnalyzer(chess.Board(), "e2e4")
        result = discovery.discover_case(case, analyzer, candidate_limit=1)
        novelty = result["candidates"][0]["novelty"]
        self.assertEqual(novelty["classification"], "known_transition")
        self.assertFalse(novelty["unseen_move"])
        self.assertEqual(novelty["source_scope_sha256"], result["knowledge_manifest"]["scope_sha256"])

    def test_ordinary_opening_transposition_is_not_reported_as_new_position(self):
        self.source_mock.return_value = [{"moves": ["f2f4"]}]
        self.full_mock.return_value = ([{"moves": ["e2e4", "e7e5", "g1f3", "b8c6"]}],
                                       {"source_identity_sha256": "ordinary-source", "catalog_rows": 1,
                                        "license": "CC0-1.0", "all_file_hashes_verified": True})
        case = case_from(moves=["g1f3", "b8c6", "e2e4"])
        analyzer = FakeAnalyzer(discovery.replay_case(case), "e7e5")
        result = discovery.discover_case(case, analyzer, candidate_limit=1)
        novelty = result["candidates"][0]["novelty"]
        self.assertEqual(novelty["classification"], "known_transposition")
        self.assertFalse(novelty["unseen_position"])
        self.assertEqual(result["knowledge_manifest"]["full_opening_source"]["catalog_rows"], 1)

    def test_full_opening_content_changes_source_scope_hash(self):
        first = discovery.knowledge_index()["manifest"]["scope_sha256"]
        self.full_mock.return_value = ([{"moves": ["d2d4"]}],
                                       {"source_identity_sha256": "another-source", "catalog_rows": 1})
        second = discovery.knowledge_index()["manifest"]["scope_sha256"]
        self.assertNotEqual(first, second)

    def test_pinned_source_loader_checks_hashes_and_records_license(self):
        source = {"schema": "regicide.opening-guide.catalog.v1", "license": "CC0-1.0",
                  "commit": "a" * 40, "repository_url": "https://example.invalid/source",
                  "files": {}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for filename in ("a.tsv", "b.tsv", "c.tsv", "d.tsv", "e.tsv", "LICENSE"):
                text = "CC0 fixture" if filename == "LICENSE" else "eco\tname\tpgn\nA00\tTest\t1. e4\n"
                raw = text.encode()
                (root / filename).write_bytes(raw)
                source["files"][filename] = {"sha256": hashlib.sha256(raw).hexdigest()}
            (root / "manifest.json").write_text(json.dumps(source), encoding="utf-8")
            # Invoke the real loader while normal discovery tests use a fixture.
            loader = self.real_full_loader
            rows, manifest = loader(root)
            self.assertEqual(len(rows), 5)
            self.assertEqual(manifest["catalog_rows"], 5)
            self.assertEqual(manifest["license"], "CC0-1.0")
            self.assertTrue(manifest["all_file_hashes_verified"])
            (root / "c.tsv").write_text("tampered", encoding="utf-8")
            with self.assertRaises(discovery.SourceUnavailableError):
                loader(root)

    def test_optional_reference_pgn_indexes_variations_and_content_hash(self):
        source = '[Event "Reference"]\n\n1. d4 d5 (1... Nf6) 2. c4 *\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reference.pgn"
            path.write_text(source, encoding="utf-8")
            index = discovery.knowledge_index([path])
            board = chess.Board()
            board.push_uci("d2d4")
            before = discovery.position_key(board)
            board.push_uci("g8f6")
            self.assertIn((before, "g8f6"), index["transitions"])
            self.assertIn(discovery.position_key(board), index["positions"])
            reference = index["manifest"]["reference_pgns"][0]
            self.assertEqual(reference["sha256"], hashlib.sha256(source.encode()).hexdigest())
            self.assertEqual(reference["games"], 1)

    def test_native_pv_illegality_is_unknown(self):
        case = case_from(self.NONMOVING)
        analyzer = FakeAnalyzer(discovery.replay_case(case), "g1h1")
        original = analyzer.analyze
        def illegal(board, **kwargs):
            value = original(board, **kwargs)
            value["pv_uci"] = ["a1a8"]
            return value
        analyzer.analyze = illegal
        result = discovery.discover_case(case, analyzer, candidate_limit=1)
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["candidates"][0]["bounded_screen_passed"])

    def test_node_request_and_query_ids_are_retained(self):
        result, analyzer = self.run_case(self.NONMOVING, "g1h1", case_extra={"discovery_nodes": 777})
        self.assertTrue(all(nodes == 777 for _board, nodes in analyzer.calls))
        self.assertEqual(result["query_ids"], ["q" + str(index + 1) for index in range(len(analyzer.calls))])
        self.assertTrue(all(len(board.move_stack) >= 1 for board, _nodes in analyzer.calls[1:]))


if __name__ == "__main__":
    unittest.main()
