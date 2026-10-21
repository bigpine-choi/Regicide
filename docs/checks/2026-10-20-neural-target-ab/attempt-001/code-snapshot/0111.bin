"""Offline A/B schedule, immutable inputs and evidence corruption tests."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run_neural_target_ab_matches as driver


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def write(self, name, data):
        path = self.root / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def model_file(self):
        path = self.root / "m.nnue"
        model = driver.precision.quantize(driver.precision.base.bootstrap_float(), 32)
        driver.precision.save_model(model, path)
        return path, driver.precision.model_checksum(model)

    def model_rows(self):
        path, checksum = self.model_file()
        return [{"arm": arm, "seed": seed, "model_id": f"{arm}-{seed}",
                 "path": str(path), "sha256": driver.digest(path), "checksum": checksum}
                for arm in ("A", "B") for seed in driver.SEEDS]

    def test_model_pairs_and_rehashed_invalid_model_rejected(self):
        rows = self.model_rows()
        path = self.write("models.json", {"models": rows})
        self.assertEqual(len(driver.load_models(path)), 6)
        changed = copy.deepcopy(rows)
        changed[0]["checksum"] += 1
        with self.assertRaisesRegex(ValueError, "checksum"):
            driver.load_models(self.write("wrong-checksum.json", changed))
        model_path = Path(rows[0]["path"])
        model_path.write_text("REGICIDE_NNUE 2 768 16 4064 256 32\nchecksum 0\n", encoding="ascii")
        for row in rows:
            row["sha256"] = driver.digest(model_path)
        with self.assertRaises(ValueError):
            driver.load_models(self.write("rehashed-corrupt.json", rows))

    def test_missing_duplicate_and_unplanned_models_rejected(self):
        rows = self.model_rows()
        variants = [rows[:-1], rows[:-1] + [rows[0]],
                    [{**rows[0], "seed": True}] + rows[1:]]
        for index, variant in enumerate(variants):
            with self.subTest(index=index), self.assertRaises(ValueError):
                driver.load_models(self.write(f"bad{index}.json", variant))

    def openings(self):
        moves = list(chess.Board().legal_moves)[:16]
        rows = []
        for index, move in enumerate(moves):
            board = chess.Board()
            board.push(move)
            rows.append({"family_id": f"family-{index}", "initial_fen": chess.STARTING_FEN,
                         "history": [move.uci()], "fen_after_opening": board.fen(en_passant="fen")})
        return rows

    def test_opening_duplicate_family_position_history_and_fen_rejected(self):
        rows = self.openings()
        self.assertEqual(len(driver.load_openings(self.write("bank.json", {"match_openings_100": rows}))), 16)
        for label, alter in (
            ("family", lambda data: data[1].update(family_id=data[0]["family_id"])),
            ("position", lambda data: data.__setitem__(1, {**data[0], "family_id": "another"})),
            ("illegal", lambda data: data[1].update(history=["a1a8"])),
            ("fen", lambda data: data[1].update(fen_after_opening=chess.STARTING_FEN)),
        ):
            changed = copy.deepcopy(rows)
            alter(changed)
            with self.subTest(label=label), self.assertRaises(ValueError):
                driver.load_openings(self.write(label + ".json", {"match_openings_100": changed}))

    def test_96_schedule_rotates_seeds_and_swaps_every_pair(self):
        openings = [{"family_id": f"f{index}"} for index in range(16)]
        schedule = driver.make_schedule(openings)
        self.assertEqual(len(schedule), 96)
        for index in range(0, 96, 2):
            first, second = schedule[index:index + 2]
            self.assertEqual(first["seed"], second["seed"])
            self.assertEqual(first["family_id"], second["family_id"])
            self.assertNotEqual(first["candidate_white"], second["candidate_white"])
        self.assertEqual([schedule[index]["seed"] for index in (0, 2, 4)], list(driver.SEEDS))
        self.assertEqual([schedule[index]["seed"] for index in (6, 8, 10)], list(driver.SEEDS[1:] + driver.SEEDS[:1]))
        self.assertEqual(len({(row["family_id"], row["seed"], row["candidate_white"]) for row in schedule}), 96)

    def test_frozen_input_and_manifest_tamper_rejected(self):
        bound = self.root / "source.txt"
        bound.write_text("before", encoding="utf-8")
        manifest = self.write("freeze.json", {"test": "immutable"})
        expected = driver.digest(manifest)
        files = {str(bound): driver.digest(bound)}
        self.assertEqual(driver.assert_inputs(files, manifest, expected)["files"], files)
        bound.write_text("after", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Frozen input changed"):
            driver.assert_inputs(files, manifest, expected)
        manifest.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "freeze changed"):
            driver.assert_inputs({}, manifest, expected)

    def fixture(self):
        board = chess.Board()
        prefix = ["f2f3", "e7e5"]
        for text in prefix:
            board.push_uci(text)
        opening = {"family_id": "fools-mate-public", "name": "fools-mate-public",
                   "moves": prefix, "initial_fen": chess.STARTING_FEN,
                   "fen_after_opening": board.fen(en_passant="fen")}
        scheduled = driver.make_schedule([opening])
        expected = scheduled[0]
        record = driver.comparison.new_record(1, 1, opening, expected["candidate_white"])
        models = {(arm, seed): {"arm": arm, "seed": seed, "path": f"/{arm}-{seed}.nnue",
                              "sha256": "a" * 64, "checksum": seed}
                  for arm in ("A", "B") for seed in driver.SEEDS}
        record.update(family_id=opening["family_id"], seed=expected["seed"],
                      models={role: models[arm, expected["seed"]] for role, arm in zip(driver.ROLES, ("A", "B"))},
                      requested_options={role: {**driver.COMMON, "NeuralModelFile": models[arm, expected["seed"]]["path"]}
                                         for role, arm in zip(driver.ROLES, ("A", "B"))})
        for text in ("g2g4", "d8h4"):
            role = record["white" if board.turn else "black"]
            record["search_moves"].append({"ply": len(record["uci_moves"]) + 1,
                "role": role, "color": "white" if board.turn else "black", "move": text,
                "fen_before": board.fen(en_passant="fen"), "elapsed_seconds": .08,
                "engine_time_seconds": .078, "nodes": 100, "depth": 3, "seldepth": 3,
                "pv": [text], "score": {"pov": "mover", "cp": 10, "mate": None,
                                          "lowerbound": False, "upperbound": False}})
            board.push_uci(text)
            record["uci_moves"].append(text)
        record.update(plies=len(record["uci_moves"]), final_fen=board.fen(en_passant="fen"),
                      completed=True, result="0-1", termination="checkmate", candidate_points=0.)
        return record, [opening], scheduled, models

    def test_independent_replay_rejects_illegal_pv_score_result_model_and_incomplete(self):
        record, openings, schedule, models = self.fixture()
        replay = driver.audit_records([record], openings, schedule, models)
        self.assertEqual(replay["completed_games"], 1)
        self.assertFalse(replay["all_96_completed"])
        for label, alter in (
            ("pv", lambda data: data["search_moves"][0].update(pv=["g2g4", "a8a1"])),
            ("score", lambda data: data["search_moves"][0]["score"].update(cp=True)),
            ("result", lambda data: data.update(candidate_points=1)),
            ("model", lambda data: data["models"]["candidate"].update(arm="A")),
            ("unfinished", lambda data: data.update(completed=False)),
            ("color", lambda data: data.update(white="baseline", black="candidate")),
        ):
            changed = copy.deepcopy(record)
            alter(changed)
            with self.subTest(label=label), self.assertRaises(ValueError):
                driver.audit_records([changed], openings, schedule, models)

    def test_unfinished_remains_star_and_does_not_enter_scores_or_screen(self):
        record, openings, schedule, models = self.fixture()
        record.update(completed=False, result="*", termination="budget_exhausted", candidate_points=None)
        driver.audit_records([record], openings, schedule, models)
        summary = driver.summarize([record], openings)
        self.assertEqual(summary["completed_games"], 0)
        self.assertEqual(summary["incomplete_games"], 1)
        self.assertEqual(summary["B_points"], 0)
        self.assertIsNone(summary["B_score"])
        self.assertIsNone(summary["opening_family_bootstrap95"])
        self.assertFalse(summary["screen_score_condition"])

    def test_score_bootstrap_preserves_six_games_in_each_family(self):
        openings = [{"family_id": f"f{index}"} for index in range(16)]
        schedule = driver.make_schedule(openings)
        records = [{**item, "completed": True, "candidate_points": 1. if int(item["family_id"][1:]) < 8 else 0.,
                    "search_moves": []} for item in schedule]
        summary = driver.summarize(records, openings)
        self.assertEqual(summary["B_wins"], 48)
        self.assertEqual(summary["B_losses"], 48)
        self.assertEqual(summary["B_score"], .5)
        interval = summary["opening_family_bootstrap95"]["interval"]
        self.assertEqual(interval, [.25, .75])
        self.assertFalse(summary["screen_score_condition"])
        for record in records:
            if record["game"] <= 5:
                record["candidate_points"] = .5
        self.assertEqual(driver.summarize(records, openings)["B_points"], 45.5)

    def test_expired_hard_budget_fails_without_engine_launch(self):
        budget = driver.Budget(-1)
        with self.assertRaises(driver.BudgetExpired):
            budget.check()

    def test_model_acknowledgement_must_be_exact_and_unique(self):
        models = {(arm, seed): {"path": f"/{arm}-{seed}.nnue", "checksum": seed}
                  for arm in ("A", "B") for seed in driver.SEEDS}
        raw = driver.RawLog(self.root / "raw.jsonl")
        try:
            def fake(role, copies):
                def configure(options):
                    for _ in range(copies):
                        raw.line(role, "recv", f"info string neural model checksum {driver.SEEDS[0]}")
                return SimpleNamespace(options={}, configure=configure, ping=lambda: None,
                    transport=SimpleNamespace(get_pid=lambda: 123 if role == "baseline" else 456))
            good = {role: fake(role, 1) for role in driver.ROLES}
            options, acks = driver.configure_pair(good, raw, models, driver.SEEDS[0])
            self.assertEqual(acks["baseline"]["pid"], 123)
            self.assertEqual(acks["candidate"]["pid"], 456)
            self.assertEqual({key for key in options["baseline"] if options["baseline"][key]
                              != options["candidate"][key]}, {"NeuralModelFile"})
            for copies in (0, 2):
                bad = {role: fake(role, copies) for role in driver.ROLES}
                with self.subTest(copies=copies), self.assertRaisesRegex(ValueError, "acknowledgement"):
                    driver.configure_pair(bad, raw, models, driver.SEEDS[0])
        finally:
            raw.stream.close()


if __name__ == "__main__":
    unittest.main()
