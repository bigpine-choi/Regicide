"""Own-search data validation without launching an engine or training a model."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

import chess


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from neural_search_data import schema


def seeds():
    return [
        {"seed_id": "start-e", "family_id": "e", "kind": "selfplay_start",
         "initial_fen": chess.STARTING_FEN, "history": ["e2e4"]},
        {"seed_id": "start-d", "family_id": "d", "kind": "selfplay_start",
         "initial_fen": chess.STARTING_FEN, "history": ["d2d4"]},
        {"seed_id": "end-c", "family_id": "c", "kind": "synthetic_endgame",
         "initial_fen": "8/6k1/8/8/8/8/2P5/2K5 w - - 0 1", "history": []},
        {"seed_id": "end-f", "family_id": "f", "kind": "synthetic_endgame",
         "initial_fen": "8/6k1/8/8/8/8/5P2/2K5 w - - 0 1", "history": []},
    ]


def query(board, query_id="q", value=31):
    move = min(board.legal_moves, key=lambda item: item.uci()).uci()
    return {"query_id": query_id, "completed": True, "score": {"kind": "cp", "stm": value},
            "lowerbound": False, "upperbound": False, "depth": 2, "nodes": 100,
            "requested_nodes": 100, "pv_uci": [move], "bestmove_uci": move}


def write_rows(path, rows):
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def make_dataset(path):
    source = seeds()
    families = schema.split_families(source, 0.25, 123)
    plan = {"schema_version": 2, "config": {"nodes": 100, "sample_every": 1, "quiet": True,
                                             "validation_fraction": 0.25, "seed": 123,
                                             "max_plies": 1, "max_searches": 4,
                                             "max_seconds": 10.0, "search_timeout": 1.0,
                                             "static_control": False},
            "seeds": source, "families": families}
    schema.write_json(path / "plan.json", plan)
    games, queries = [], []
    parts = {name: [] for name in schema.PARTITIONS}
    for index, seed in enumerate(source):
        game_id = f"g-{index}"
        partition = families[seed["family_id"]]
        board = schema.board_from(seed["initial_fen"], seed["history"])
        current = query(board, f"q-{index}", index + 1)
        current.update({"family_id": seed["family_id"], "game_id": game_id,
                        "initial_fen": seed["initial_fen"], "history": seed["history"],
                        "fen": board.fen(en_passant="fen"), "feature_hash": schema.feature_hash(board),
                        "state_hash": schema.state_hash(seed["initial_fen"], seed["history"]),
                        "partition": partition, "kind": seed["kind"], "ply": 0,
                        "sampled": True, "excluded_reasons": []})
        sample = schema.make_sample(board, current, family_id=seed["family_id"], game_id=game_id,
                                    ply=0, initial_fen=seed["initial_fen"], history=seed["history"],
                                    partition=partition)
        sample["kind"] = seed["kind"]
        parts[partition].append(sample)
        board.push_uci(current["bestmove_uci"])
        history = seed["history"] + [current["bestmove_uci"]]
        games.append({"game_id": game_id, "seed_id": seed["seed_id"], "family_id": seed["family_id"],
                      "partition": partition, "kind": seed["kind"], "initial_fen": seed["initial_fen"],
                      "initial_history": seed["history"], "moves_uci": history,
                      "final_fen": board.fen(en_passant="fen"), "generated_plies": 1,
                      "termination": "max_plies", "result": None, "outcome_termination": None,
                      "query_ids": [current["query_id"]]})
        queries.append(current)
    write_rows(path / "queries.jsonl", queries)
    write_rows(path / "games.jsonl", games)
    (path / "uci.jsonl").write_text("", encoding="utf-8")
    for name, rows in parts.items():
        write_rows(path / f"{name}.jsonl", rows)
    refresh_manifest(path)


def refresh_manifest(path, **changes):
    filenames = ["plan.json", "queries.jsonl", "games.jsonl", "uci.jsonl", "train.jsonl", "validation.jsonl"]
    manifest = {"schema_version": 2, "status": "complete", "error": None,
                "static_control_status": "disabled",
                "plan_sha256": schema.sha256(path / "plan.json"),
                "files": {name: schema.sha256(path / name) for name in filenames},
                "partitions": {name: {"file": f"{name}.jsonl", "sha256": schema.sha256(path / f"{name}.jsonl"),
                                      "count": len((path / f"{name}.jsonl").read_text().splitlines())}
                               for name in schema.PARTITIONS}}
    manifest.update(changes)
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


class IdentityTests(unittest.TestCase):
    def test_current_input_and_full_state_have_distinct_identities(self):
        board = chess.Board()
        altered = board.copy()
        altered.turn = not altered.turn
        altered.castling_rights = 0
        altered.halfmove_clock = 41
        self.assertEqual(schema.feature_hash(board), schema.feature_hash(altered))
        self.assertEqual(schema.feature_hash(board), schema.feature_hash(board.mirror()))
        self.assertNotEqual(schema.state_hash(board.fen(), []), schema.state_hash(altered.fen(), []))
        self.assertNotEqual(schema.state_hash(board.fen(), []), schema.state_hash(board.fen(), ["e2e4"]))
        self.assertEqual(len(schema.state_hash(board.fen(), [])), 64)

    def test_replay_is_strict_and_preserves_final_outcome(self):
        board = schema.board_from(chess.STARTING_FEN, ["f2f3", "e7e5", "g2g4", "d8h4"])
        self.assertTrue(board.is_checkmate())
        for initial, moves in ((chess.STARTING_FEN, ["e2e5"]),
                               (chess.STARTING_FEN, ["0000"]),
                               (chess.STARTING_FEN, ["f2f3", "e7e5", "g2g4", "d8h4", "a2a3"]),
                               ("8/8/8/8/8/8/8/8 w - - 0 1", []),
                               (chess.STARTING_FEN.rsplit(" ", 2)[0], [])):
            with self.subTest(initial=initial, moves=moves), self.assertRaises(ValueError):
                schema.board_from(initial, moves)

    def test_family_split_is_stratified_and_order_independent(self):
        source = seeds()
        first = schema.split_families(source, 0.25, 123)
        self.assertEqual(first, schema.split_families(list(reversed(source)), 0.25, 123))
        for kind in schema.KINDS:
            self.assertEqual({first[row["family_id"]] for row in source if row["kind"] == kind},
                             {"train", "validation"})
        duplicate = {**source[0], "seed_id": "same-family"}
        self.assertEqual(first, schema.split_families(source + [duplicate], 0.25, 123))

    def test_family_split_rejects_leaky_or_insufficient_sources(self):
        source = seeds()
        mirror = schema.board_from(source[0]["initial_fen"], source[0]["history"]).mirror()
        leaky = {"seed_id": "mirror", "family_id": "other", "kind": "selfplay_start",
                 "initial_fen": mirror.fen(), "history": []}
        for data, fraction in ((source + [leaky], 0.2), (source[:3], 0.2), (source, float("nan")),
                               (source, True), (source, 0), (source, 1),
                               (source + [{**source[0], "kind": "synthetic_endgame"}], 0.2)):
            with self.subTest(fraction=fraction), self.assertRaises(ValueError):
                schema.split_families(data, fraction, 123)

    def test_write_is_exclusive(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "new.json"
            schema.write_json(path, {"a": 1})
            self.assertEqual(json.loads(path.read_text()), {"a": 1})
            with self.assertRaises(FileExistsError):
                schema.write_json(path, {})


class QueryTests(unittest.TestCase):
    def test_valid_cp_and_black_target_keep_root_perspective(self):
        board = schema.board_from(chess.STARTING_FEN, ["e2e4"])
        current = query(board, value=-17)
        self.assertEqual(schema.validate_query(board, current, 100), [])
        sample = schema.make_sample(board, current, family_id="a", game_id="g", ply=0,
                                    initial_fen=chess.STARTING_FEN, history=["e2e4"], partition="train")
        self.assertEqual(sample["search_stm_cp"], -17)
        self.assertEqual(sample["target_white_cp"], 17)
        self.assertNotIn("classical_stm_cp", sample)

    def test_invalid_search_metadata_is_excluded(self):
        board = chess.Board()
        good = query(board)
        cases = [("incomplete", {"completed": False}),
                 ("score_kind", {"score": {"kind": "mate", "stm": 3}}),
                 ("score_kind", {"score": None}),
                 ("score_value", {"score": {"kind": "cp", "stm": True}}),
                 ("bound", {"lowerbound": True}),
                 ("bound", {"upperbound": "false"}),
                 ("depth", {"depth": 0}), ("depth", {"depth": True}),
                 ("nodes", {"nodes": 101}), ("nodes", {"nodes": 0}),
                 ("requested_nodes", {"requested_nodes": 99}),
                 ("illegal_pv", {"pv_uci": ["e2e5"]}), ("illegal_pv", {"pv_uci": ["a2a3", "e2e4"]}),
                 ("illegal_pv", {"pv_uci": []}), ("illegal_bestmove", {"bestmove_uci": "e2e4"})]
        for reason, change in cases:
            with self.subTest(change=change):
                self.assertIn(reason, schema.validate_query(board, {**good, **change}, 100))

    def test_interrupted_empty_response_has_one_clear_reason(self):
        self.assertEqual(schema.validate_query(chess.Board(), {"completed": False}, 100), ["incomplete"])

    def test_quiet_policy_treats_tactics_as_exclusions(self):
        capture = chess.Board("7k/8/8/8/8/8/1r6/K7 w - - 0 1")
        current = {**query(capture), "pv_uci": ["a1b2"], "bestmove_uci": "a1b2"}
        self.assertIn("capture", schema.validate_query(capture, current, 100))
        self.assertNotIn("capture", schema.validate_query(capture, current, 100, quiet=False))
        checked = chess.Board("7k/8/8/8/8/8/r7/K7 w - - 0 1")
        self.assertIn("in_check", schema.validate_query(checked, query(checked), 100))
        promotion = chess.Board("7k/P7/8/8/8/8/8/K7 w - - 0 1")
        current = {**query(promotion), "pv_uci": ["a7a8q"], "bestmove_uci": "a7a8q"}
        self.assertIn("promotion", schema.validate_query(promotion, current, 100))
        dead = chess.Board("7k/8/8/8/8/8/8/K7 w - - 0 1")
        self.assertIn("terminal", schema.validate_query(dead, query(dead), 100))

    def test_sample_rejects_wrong_replay_and_invalid_query(self):
        board = chess.Board()
        for current, history in ((query(board), ["e2e4"]),
                                 ({**query(board), "completed": False}, [])):
            with self.assertRaises(ValueError):
                schema.make_sample(board, current, family_id="a", game_id="g", ply=0,
                                   initial_fen=chess.STARTING_FEN, history=history, partition="train")


class DatasetTests(unittest.TestCase):
    def test_loads_replayed_and_hash_bound_dataset(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            make_dataset(path)
            loaded = schema.load_dataset(path)
            self.assertEqual(len(loaded["train"]), 2)
            self.assertEqual(len(loaded["validation"]), 2)
            self.assertTrue(all(type(row["target_white_cp"]) is int for row in loaded["train"]))

    def test_partial_requires_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            make_dataset(path)
            refresh_manifest(path, status="budget_exhausted")
            with self.assertRaises(ValueError):
                schema.load_dataset(path)
            self.assertEqual(len(schema.load_dataset(path, allow_partial=True)["train"]), 2)

    def test_empty_failed_attempt_is_available_only_for_explicit_forensics(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            make_dataset(path)
            for name in ("games.jsonl", "queries.jsonl", "train.jsonl", "validation.jsonl"):
                (path / name).write_text("", encoding="utf-8")
            refresh_manifest(path, status="error", error="Teacher startup failed")
            with self.assertRaises(ValueError):
                schema.load_dataset(path)
            loaded = schema.load_dataset(path, allow_partial=True)
            self.assertEqual(loaded["train"], [])
            self.assertEqual(loaded["validation"], [])

    def test_checksum_and_path_mutation_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            make_dataset(path)
            with (path / "train.jsonl").open("a", encoding="utf-8") as stream:
                stream.write("{}\n")
            with self.assertRaises(ValueError):
                schema.load_dataset(path)
            refresh_manifest(path)
            manifest = json.loads((path / "manifest.json").read_text())
            manifest["files"]["../outside.json"] = "0" * 64
            (path / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                schema.load_dataset(path)

    def test_rehashed_target_query_and_game_tampering_is_rejected(self):
        mutations = [
            ("train.jsonl", lambda rows: rows[0].update(target_white_cp=999)),
            ("train.jsonl", lambda rows: rows[0].update(classical_stm_cp=1)),
            ("train.jsonl", lambda rows: rows.append(copy.deepcopy(rows[0]))),
            ("queries.jsonl", lambda rows: rows[0].update(history=[])),
            ("queries.jsonl", lambda rows: rows[0].update(completed=False)),
            ("queries.jsonl", lambda rows: rows[0].update(lowerbound=True)),
            ("queries.jsonl", lambda rows: rows[0].update(excluded_reasons=["unfavorable_score"], sampled=False)),
            ("games.jsonl", lambda rows: rows[0].update(result="1/2-1/2")),
            ("games.jsonl", lambda rows: rows[0].update(query_ids=[])),
            ("games.jsonl", lambda rows: rows[0].update(partition="elsewhere")),
            ("games.jsonl", lambda rows: rows.pop()),
        ]
        for filename, mutate in mutations:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary)
                make_dataset(path)
                rows = [json.loads(line) for line in (path / filename).read_text().splitlines()]
                mutate(rows)
                write_rows(path / filename, rows)
                refresh_manifest(path)
                with self.assertRaises(ValueError):
                    schema.load_dataset(path)

    def test_static_control_int_and_side_perspective_are_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            make_dataset(path)
            plan = json.loads((path / "plan.json").read_text())
            plan["config"]["static_control"] = True
            (path / "plan.json").write_text(json.dumps(plan))
            for partition in schema.PARTITIONS:
                rows = [json.loads(line) for line in (path / f"{partition}.jsonl").read_text().splitlines()]
                for row in rows:
                    row["classical_static_stm_cp"] = 15
                    row["classical_static_white_cp"] = 15 if chess.Board(row["fen"]).turn else -15
                write_rows(path / f"{partition}.jsonl", rows)
            refresh_manifest(path, static_control_status="complete")
            schema.load_dataset(path)
            rows = [json.loads(line) for line in (path / "train.jsonl").read_text().splitlines()]
            rows[0]["classical_static_white_cp"] *= -1
            write_rows(path / "train.jsonl", rows)
            refresh_manifest(path, static_control_status="complete")
            with self.assertRaises(ValueError):
                schema.load_dataset(path)

    def test_changed_trajectory_cannot_hide_behind_rehashed_search_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            make_dataset(path)
            games = [json.loads(line) for line in (path / "games.jsonl").read_text().splitlines()]
            first = games[0]
            board = schema.board_from(first["initial_fen"], first["initial_history"])
            alternate = next(move for move in board.legal_moves if move.uci() != first["moves_uci"][-1])
            board.push(alternate)
            first["moves_uci"][-1] = alternate.uci()
            first["final_fen"] = board.fen(en_passant="fen")
            write_rows(path / "games.jsonl", games)
            refresh_manifest(path)
            with self.assertRaises(ValueError):
                schema.load_dataset(path)

    def test_rehashed_boolean_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            make_dataset(path)
            rows = [json.loads(line) for line in (path / "queries.jsonl").read_text().splitlines()]
            first = rows[0]
            partition = first["partition"]
            sample_rows = [json.loads(line) for line in (path / f"{partition}.jsonl").read_text().splitlines()]
            current = next(row for row in sample_rows if row["query_id"] == first["query_id"])
            self.assertEqual(current["search_stm_cp"], 1)
            current["search_stm_cp"] = True  # bool compares equal to 1 in ordinary Python equality.
            write_rows(path / f"{partition}.jsonl", sample_rows)
            refresh_manifest(path)
            with self.assertRaises(ValueError):
                schema.load_dataset(path)


if __name__ == "__main__":
    unittest.main()
