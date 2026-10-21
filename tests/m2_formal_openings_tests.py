"""Rules and exposure checks; never launch or evaluate a chess engine."""
from __future__ import annotations

from collections import Counter
import argparse
import copy
import datetime
import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import prepare_m2_formal_openings as preparation
from scripts import compare_engines

EVIDENCE = ROOT / "docs/checks/2026-10-21-m2-formal/openings"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reflected_piece_keys(board):
    # Independent square/color implementation, rather than the generator's mirror.
    reflected = chess.Board(None)
    for square, piece in board.piece_map().items():
        reflected.set_piece_at(square ^ 56, chess.Piece(piece.piece_type, not piece.color))
    return {board.board_fen(), reflected.board_fen()}


def replay_row(row):
    board, states = chess.Board(), []
    for index, text in enumerate(row["moves"]):
        move = chess.Move.from_uci(text)
        if board.outcome(claim_draw=True) is not None or move not in board.legal_moves:
            raise AssertionError(f"Illegal/post-terminal ply {index + 1}: {text}")
        if board.san(move) != row["san_moves"][index]:
            raise AssertionError("SAN receipt mismatch")
        board.push(move)
        states.append(board.copy(stack=False))
    return board, states


class FormalOpeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.formal = json.loads(preparation.DEFAULT_FORMAL.read_text(encoding="utf-8"))
        cls.preflight = json.loads(preparation.DEFAULT_PREFLIGHT.read_text(encoding="utf-8"))
        cls.audit = json.loads(preparation.DEFAULT_AUDIT.read_text(encoding="utf-8"))
        cls.index_path = EVIDENCE / "exposure-index.json.gz"
        cls.index = json.loads(gzip.decompress(cls.index_path.read_bytes()).decode("utf-8"))

    def test_01_counts_and_distinct_families(self):
        a, b = self.formal["openings"], self.preflight["openings"]
        self.assertEqual((len(a), len(b)), (32, 2))
        rows = a + b
        self.assertEqual(len({r["name"] for r in rows}), 34)
        self.assertEqual(len({r["family_id"] for r in rows}), 34)
        self.assertEqual(Counter(r["side_to_move"] for r in a), {"white": 16, "black": 16})
        self.assertEqual(self.preflight["selected_indices_one_based"], [1])

    def test_02_independent_legality_receipts_and_nonterminal_starts(self):
        for document in (self.formal, self.preflight):
            for row in document["openings"]:
                with self.subTest(row=row["name"]):
                    board, _ = replay_row(row)
                    self.assertTrue(board.is_valid())
                    self.assertFalse(board.is_check())
                    self.assertIsNone(board.outcome(claim_draw=True))
                    self.assertEqual(board.fen(en_passant="fen"), row["fen_after_opening"])
                    self.assertEqual(row["plies"], len(row["moves"]))
                    self.assertEqual(row["parent_prefix_uci"], row["moves"][:8])
                    self.assertEqual(row["parent_prefix_plies"], 8)
                    self.assertGreaterEqual(len(row["moves"]), 12 if document is self.formal else 8)
                    self.assertLessEqual(len(row["moves"]), 16 if document is self.formal else 12)

    def test_03_independent_cross_set_transpositions_and_piece_reflections(self):
        all_states, all_pieces = set(), set()
        for row in self.formal["openings"] + self.preflight["openings"]:
            board, path = replay_row(row)
            # Compare every meaningful branch position, not just final FEN.
            branch_keys = set()
            for position in path[7:]:
                branch_keys.add(" ".join(position.fen(en_passant="legal").split()[:4]))
                branch_keys.add(" ".join(position.mirror().fen(en_passant="legal").split()[:4]))
            self.assertFalse(branch_keys & all_states, row["name"])
            self.assertFalse(reflected_piece_keys(board) & all_pieces, row["name"])
            all_states.update(branch_keys)
            all_pieces.update(reflected_piece_keys(board))

    def test_04_frozen_byte_hashes_and_generator(self):
        self.assertEqual(sha(preparation.DEFAULT_FORMAL), self.audit["files"]["formal_sha256"])
        self.assertEqual(sha(preparation.DEFAULT_PREFLIGHT), self.audit["files"]["preflight_sha256"])
        self.assertEqual(sha(self.index_path), self.audit["exposure_index"]["sha256"])
        for doc in (self.formal, self.preflight):
            self.assertEqual(doc["generator_sha256"], sha(ROOT / doc["generator"]))
        for source in self.index["sources"]:
            self.assertEqual(sha(ROOT / source["path"]), source["sha256"], source["path"])
        self.assertEqual(self.index["sources"], self.audit["sources"])

    def test_05_prior_index_exclusions(self):
        pieces = set(self.index["pieces"])
        parents = set(self.index["parents"])
        histories = [tuple(h) for h in self.index["histories"]]
        for row in self.formal["openings"] + self.preflight["openings"]:
            board, path = replay_row(row)
            self.assertFalse(reflected_piece_keys(board) & pieces, row["name"])
            self.assertNotIn(hashlib.sha256(min(reflected_piece_keys(board)).encode("ascii")).hexdigest(),
                             self.index["feature_hashes"], row["name"])
            for position in path[7:]:
                direct = " ".join(position.fen(en_passant="legal").split()[:4])
                mirror = " ".join(position.mirror().fen(en_passant="legal").split()[:4])
                self.assertNotIn(direct, parents, row["name"])
                self.assertNotIn(mirror, parents, row["name"])
            moves = tuple(row["moves"])
            self.assertFalse(any(moves[:len(h)] == h or h[:len(moves)] == moves for h in histories))

    def test_06_direct_prior_bank_and_ab_parent_replay(self):
        # Independent extraction of declared source parents, without trusting
        # the cached index or reusing collect_exclusions/authored_rows.
        targets = set()
        for row in self.formal["openings"] + self.preflight["openings"]:
            _, path = replay_row(row)
            for board in path[7:]:
                targets.update(reflected_piece_keys(board))
        banks = sorted((ROOT / "tests/data").glob("*opening*.json"))
        banks = [p for p in banks if p.name not in (preparation.DEFAULT_FORMAL.name,
                                                   preparation.DEFAULT_PREFLIGHT.name)]
        banks += sorted((ROOT / "docs/checks/2026-10-20-neural-target-ab").glob("attempt-*/bank.json"))
        inspected = 0
        for path in banks:
            document = json.loads(path.read_text(encoding="utf-8-sig"))
            records = document.get("openings", []) + document.get("sources", [])
            for record in records:
                for start, moves in ((record.get("initial_fen", chess.STARTING_FEN),
                                      record.get("moves", record.get("history", []))),
                                     (record.get("parent_initial_fen", chess.STARTING_FEN),
                                      record.get("parent_history", []))):
                    board = chess.Board(start)
                    if board.fen() != chess.STARTING_FEN:
                        continue
                    for ply, text in enumerate(moves, 1):
                        move = chess.Move.from_uci(text)
                        self.assertIn(move, board.legal_moves)
                        board.push(move)
                        if ply >= 8:
                            self.assertFalse(reflected_piece_keys(board) & targets,
                                             str(path) + ":" + str(ply))
                            inspected += 1
        self.assertGreater(inspected, 100)

    def test_07_source_scope_and_no_independence_overclaim(self):
        names = {Path(s["path"]).name for s in self.index["sources"]}
        self.assertTrue({"comparison_openings.json", "heldout_openings_2026-10-17.json",
                         "lmr_openings_2026-10-18.json", "pvs_openings_2026-10-18.json",
                         "see_cost_openings_2026-10-18.json", "see_replication_openings_2026-10-18.json",
                         "neural_strength_openings_2026-10-19.json", "neural_long_time_openings_2026-10-19.json",
                         "m2_pilot_openings_2026-10-21.json", "learning_openings_v1.json",
                         "learning_arena_openings_v1.json"}.issubset(names))
        for attempt in ("attempt-001", "attempt-002"):
            paths = [s["path"] for s in self.index["sources"] if attempt in s["path"]
                     and "2026-10-20-neural-target-ab" in s["path"]]
            self.assertTrue(any(p.endswith("/bank.json") for p in paths))
            self.assertTrue(any(p.endswith("/train.jsonl") for p in paths))
            self.assertTrue(any(p.endswith("/test.jsonl") for p in paths))
        clusters = Counter(r["cluster_id"] for r in self.formal["openings"])
        self.assertEqual(dict(sorted(clusters.items())), self.audit["formal_cluster_counts"])
        self.assertGreater(len(clusters), 5)
        self.assertLess(len(clusters), 32)
        self.assertIn("not independent", self.formal["independence_limit"])
        self.assertFalse(self.audit["scores_search_models_results_used"])

    def test_08_existing_match_loader_compatibility(self):
        for path, expected in ((preparation.DEFAULT_FORMAL, 32), (preparation.DEFAULT_PREFLIGHT, 2)):
            document, rows = compare_engines.load_openings(path)
            self.assertEqual(len(rows), expected)
            self.assertEqual(rows, document["openings"])
            self.assertTrue(all(r["family_id"] and r["cluster_id"] for r in rows))

    def test_09_reproducible_order_without_engine(self):
        prior = {k: set(self.index[k]) for k in ("pieces", "states", "parents", "family_ids")}
        prior["histories"] = {tuple(h) for h in self.index["histories"]}
        prior.update({k: self.index[k] for k in ("sources", "issues", "counts")})
        a, b, _ = preparation.prepare(prior=prior)
        self.assertEqual(preparation.json_bytes(a), preparation.DEFAULT_FORMAL.read_bytes())
        self.assertEqual(preparation.json_bytes(b), preparation.DEFAULT_PREFLIGHT.read_bytes())

    def test_10_collector_parent_history_fen_and_arena_seed_coverage(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "prior.json"
            line = self.formal["openings"][0]["moves"]
            path.write_text(json.dumps({"sources": [{"family_id": "old", "initial_fen": chess.STARTING_FEN,
                                                       "parent_history": line, "history": line[:8]}]}))
            prior = preparation.collect_exclusions([path], root)
            board, states = replay_row(self.formal["openings"][0])
            self.assertIn(preparation.piece_identity(board), prior["pieces"])
            self.assertIn(preparation.position_identity(states[7]), prior["parents"])
            self.assertIn(tuple(line), prior["histories"])
            self.assertFalse(prior["issues"])
            self.assertEqual(prior["family_ids"], {"old"})
            path.write_text(json.dumps({"schema": "regicide.learning.arena.openings.v1",
                                        "prefixes": [["g1f3"]], "seeds": [731101, 731117]}))
            arena = preparation.collect_exclusions([path], root)
            self.assertEqual(len(arena["histories"]), 2)
            self.assertTrue(all(len(h) == 12 for h in arena["histories"]))

    def test_11_exclusion_filters_reject_renamed_and_transposed_parent(self):
        empty = {"pieces": set(), "parents": set(), "histories": set()}
        line = preparation.DECLARED[0]
        row = preparation.authored_rows((line,), "formal", empty)[0]
        board, states = replay_row(row)
        for key, identity in (("pieces", preparation.piece_identity(board)),
                              ("parents", preparation.position_identity(states[7])),
                              ("histories", tuple(row["moves"][:8]))):
            prior = copy.deepcopy(empty)
            prior[key].add(identity)
            with self.subTest(filter=key), self.assertRaises(ValueError):
                preparation.authored_rows((line,), "formal", prior)
        with self.assertRaises(ValueError):
            preparation.authored_rows((line, line), "formal", empty)

    def test_12_cached_shared_history_retains_all_prefix_exposure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            moves = self.formal["openings"][0]["moves"]
            paths = [root / "long.json", root / "short.json"]
            for path, length in zip(paths, (len(moves), 8)):
                path.write_text(json.dumps({"sources": [{"family_id": "old", "history": moves[:length]}]}))
            forward = preparation.collect_exclusions(paths, root)
            reverse = preparation.collect_exclusions(list(reversed(paths)), root)
            for key in ("pieces", "states", "parents", "histories"):
                self.assertEqual(forward[key], reverse[key], key)
            board = chess.Board()
            for index, text in enumerate(moves, 1):
                board.push_uci(text)
                self.assertIn(preparation.piece_identity(board), forward["pieces"])
                if index >= 8:
                    self.assertIn(preparation.position_identity(board), forward["parents"])

    def test_13_additional_fen_fields_and_illegal_source_visibility(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            board, _ = replay_row(self.formal["openings"][0])
            path = root / "old.json"
            path.write_text(json.dumps({"search_fen": board.fen(), "root_fen": board.fen(),
                                        "pv_final_fen": board.fen(), "sources": [{
                                            "name": "broken", "moves": ["e2e4", "e7e5", "e1e8"]}]}))
            prior = preparation.collect_exclusions([path], root)
            self.assertIn(preparation.piece_identity(board), prior["pieces"])
            self.assertEqual(len(prior["issues"]), 1)
            self.assertTrue(prior["issues"][0]["accepted_legal_prefix_only"])
            path.write_text("Not position data and not the known historical text receipt")
            with self.assertRaisesRegex(ValueError, "Malformed exclusion source"):
                preparation.collect_exclusions([path], root)

    def test_14_nested_pv_uses_enclosing_query_root(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            board, _ = replay_row(self.formal["openings"][0])
            move = sorted(board.legal_moves, key=lambda m: m.uci())[0]
            path = root / "query.json"
            path.write_text(json.dumps({"fen": board.fen(), "result": {"pv_uci": [move.uci()]}}))
            board.push(move)
            prior = preparation.collect_exclusions([path], root)
            self.assertFalse(prior["issues"])
            self.assertIn(preparation.piece_identity(board), prior["pieces"])

    def test_15_recorded_hash_alone_prevents_reusing_known_input(self):
        empty = {"pieces": set(), "parents": set(), "histories": set()}
        declaration = preparation.DECLARED[0]
        row = preparation.authored_rows((declaration,), "formal", empty)[0]
        prior = dict(empty, feature_hashes={row["pieces_identity_sha256"]})
        with self.assertRaisesRegex(ValueError, "explicitly recorded feature hash"):
            preparation.authored_rows((declaration,), "formal", prior)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    started = time.monotonic()
    report = {"status": "RUNNING", "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "command": [sys.executable, *sys.argv], "actual_engine_queries": 0,
              "scope": "Offline rules, declared exposure hashes, direct bank/AB parent replay and deterministic generation; no chess engine or GUI started."}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(FormalOpeningTests))
    report.update(status="PASS" if result.wasSuccessful() else "FAIL", tests_run=result.testsRun,
                  failures=len(result.failures), errors=len(result.errors), elapsed_seconds=time.monotonic() - started,
                  source_sha256=sha(Path(__file__)),
                  finished_utc=datetime.datetime.now(datetime.timezone.utc).isoformat())
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)
