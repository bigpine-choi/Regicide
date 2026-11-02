"""Independent reporting checks; synthetic scores are not performance data."""
import copy
import json
from pathlib import Path
import random
import sys
import tempfile
import unittest

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import summarize_sparring_match as report


def game(sans="e4 e5 Nf3 Nc6", color="white", number=1):
    board, rows, history = chess.Board(), [], []
    for san in sans.split():
        before = board.copy(stack=True)
        move = board.parse_san(san)
        ply = len(history) + 1
        actor = "sparring" if board.turn == (color == "white") else "stockfish"
        line = {"uci": move.uci(), "pv_uci": [move.uci()], "depth": 5, "cp": 100,
                "cp_white": 100 if board.turn else -100}
        decision = {"policy": report.POLICY, "ply": ply, "before_fen": before.fen(en_passant="fen"),
                    "history_uci": list(history), "selected_uci": move.uci(), "baseline_uci": move.uci(),
                    "baseline": line, "loss_cp": 0, "reason": "baseline_preserved", "phase": report._phase(ply - 1),
                    "reliability": {"reliability": "stable"}, "target_loss_cp": 0,
                    "profile": {"smoothed_loss_cp": 35, "skill_target_loss_cp": 0, "target_loss_cp": 0},
                    "extra_elapsed_ms": 50, "baseline_elapsed_ms": 999}
        board.push(move)
        rows.append({"ply": ply, "actor": actor, "uci": move.uci(), "before_fen": before.fen(en_passant="fen"),
                     "after_fen": board.fen(en_passant="fen"), "elapsed_ms": 1100,
                     "search_elapsed_ms": 1090, "relay_elapsed_ms": 10,
                     "baseline_budget_ms": 1000, "advertised_extra_budget_ms": 2000,
                     "sparring_decision": decision if actor == "sparring" else None})
        history.append(move.uci())
    outcome = board.outcome(claim_draw=True)
    return {"game": number, "pair": 1, "stockfish_elo": 1320, "sparring_color": color,
            "sparring_seed": "a" * 32, "result": outcome.result() if outcome else "*",
            "termination": outcome.termination.name.lower() if outcome else "ply_cap",
            "completed": bool(outcome), "legal_result": bool(outcome), "capped": not bool(outcome), "error": None,
            "initial_fen": chess.STARTING_FEN, "final_fen": board.fen(en_passant="fen"),
            "uci_moves": history, "moves": rows, "plies": len(history)}


def match(*games):
    return {"schema": report.MATCH_SCHEMA,
            "config": {"ratings": [1320], "colors": ["white", "black"], "sparring_ms": 1000,
                       "stockfish_ms": 1000, "threads": 1, "hash_mb": 16, "max_plies": 220,
                       "cap_policy": "incomplete", "classical": True},
            "games": list(games), "schedule": [{"game": index + 1} for index in range(max(2, len(games)))],
            "status": "completed", "sources_unchanged": True, "catalog_unchanged": True, "binaries_unchanged": True}


class SummaryTests(unittest.TestCase):
    def test_replayed_results_are_colored_correctly_and_caps_are_excluded(self):
        white = game("f3 e5 g4 Qh4#", "white", 1)
        black = game("f3 e5 g4 Qh4#", "black", 2)
        capped = game(number=3)
        result = report.summarize_matches([match(white, black, capped)])
        self.assertEqual(result["outcomes"]["wins"], 1)
        self.assertEqual(result["outcomes"]["losses"], 1)
        self.assertEqual(result["outcomes"]["legal_completed"], 2)
        self.assertEqual(result["outcomes"]["categories"]["capped"], 1)
        self.assertEqual(result["complete_legal_color_pairs"], 0)  # Third record is not another color pair.

    def test_same_depth_candidate_white_sign_and_actual_concession(self):
        value = game(color="black")
        row = value["moves"][1]
        decision = row["sparring_decision"]
        decision["baseline_uci"] = "a7a5"
        decision["baseline"] = {"uci": "a7a5", "pv_uci": ["a7a5"], "depth": 5, "cp": 100, "cp_white": -100}
        decision["candidate_compared"] = {"uci": "e7e5", "pv_uci": ["e7e5"], "depth": 5, "cp": 70, "cp_white": -70}
        decision["loss_cp"] = 30
        result = report.summarize_matches([match(value)])["all_moves"]
        self.assertEqual(result["verified_positive_loss_concessions"], 1)
        self.assertEqual(result["selected_root_same_depth"]["white_cp"]["max"], -70)
        self.assertEqual(result["baseline_minus_clamped_loss"], result["selected_root_same_depth"])
        decision["candidate_compared"]["depth"] = 4
        bad = report.summarize_matches([match(value)])["all_moves"]
        self.assertEqual(bad["verified_positive_loss_concessions"], 0)
        self.assertEqual(bad["selected_root_same_depth"]["known_exact_cp"], 1)

    def test_claimable_draw_scores_half_but_cap_draw_is_excluded(self):
        draw = game("Nf3 Nf6 Ng1 Ng8 Nf3 Nf6 Ng1", number=1)
        self.assertEqual(draw["termination"], "threefold_repetition")
        capped = game(number=2)
        capped.update(result="1/2-1/2", termination="ply_cap_draw")
        result = report.summarize_matches([match(draw, capped)])["outcomes"]
        self.assertEqual(result["draws"], 1)
        self.assertEqual(result["legal_completed"], 1)
        self.assertEqual(result["score_fraction_completed_only"], .5)
        self.assertEqual(result["categories"]["capped"], 1)

    def test_zero_clamped_loss_does_not_hide_better_candidate_score(self):
        value = game("e4", "white")
        row = value["moves"][0]["sparring_decision"]
        row.update(baseline_uci="d2d4", baseline={"uci": "d2d4", "pv_uci": ["d2d4"], "cp": 100, "depth": 5},
                   candidate_compared={"uci": "e2e4", "pv_uci": ["e2e4"], "cp": 105, "depth": 5})
        result = report.summarize_matches([match(value)])["all_moves"]
        self.assertEqual(result["selected_vs_clamped_loss_disagreements"], 1)
        self.assertEqual(result["verified_positive_loss_concessions"], 0)
        self.assertEqual(result["selected_root_same_depth"]["white_cp"]["mean"], 105)

    def test_missing_mate_and_wrong_pv_are_unknown_not_zero(self):
        for line in (None, {"cp": float("nan")}, {"uci": "e2e4", "pv_uci": ["e2e5"], "depth": 5, "cp": 0},
                     {"uci": "e2e4", "pv_uci": ["e2e4"], "depth": 5, "cp": 0, "mate": 2}):
            value = game("e4")
            value["moves"][0]["sparring_decision"]["baseline"] = line
            metric = report.summarize_matches([match(value)])["all_moves"]["baseline_before_selection"]
            self.assertEqual(metric["known_exact_cp"], 0)
            self.assertIsNone(metric["close_fraction_of_known"])
            self.assertEqual(metric["unknown_or_inexact"], 1)

    def test_stockfish_next_root_is_separate_and_pv_can_differ_from_selected(self):
        value = game("e4 e5")
        turn = value["moves"][1]
        turn["stockfish_score_packet"] = {"cp_white": -120, "mate_white": None, "depth": 6,
            "bound": "exact", "pv_legal": True, "score_pov_valid": True, "score_pov": "white",
            "before_fen": turn["before_fen"], "pv_uci": ["c7c5"], "is_selected_move_score": False}
        metric = report.summarize_matches([match(value)])["all_moves"]
        self.assertEqual(metric["stockfish_next_root_auxiliary"]["close_fraction_of_known"], 1)
        self.assertEqual(metric["stockfish_exact_root_differs_from_weakened_selected_move"], 1)
        self.assertEqual(metric["baseline_before_selection"]["white_cp"]["mean"], 100)
        for key, bad in (("bound", "upper"), ("pv_legal", False), ("score_pov_valid", False), ("before_fen", "wrong")):
            copy_value = copy.deepcopy(value)
            copy_value["moves"][1]["stockfish_score_packet"][key] = bad
            result = report.summarize_matches([match(copy_value)])["all_moves"]["stockfish_next_root_auxiliary"]
            self.assertEqual(result["known_exact_cp"], 0)

    def test_invalid_records_cannot_count_as_completed(self):
        for mutate in (lambda g: g.update(result="1-0"),
                       lambda g: g["moves"][0].update(actor="stockfish"),
                       lambda g: g["moves"][0].update(after_fen="wrong")):
            value = game("f3 e5 g4 Qh4#")
            mutate(value)
            result = report.summarize_matches([match(value)])
            self.assertEqual(result["outcomes"]["categories"], {"invalid_record": 1})
            self.assertEqual(result["all_moves"]["sparring_moves"], 0)

    def test_time_illegal_and_generic_failures_are_separated(self):
        values = []
        for number, error in enumerate(("TimeoutError: stopped", "ValueError: illegal move", "RuntimeError: transport"), 1):
            value = game(number=number)
            value.update(capped=False, error=error, termination="engine_error")
            values.append(value)
        result = report.summarize_matches([match(*values)])
        self.assertEqual(result["outcomes"]["categories"], {"timeout": 1, "illegal_move": 1, "engine_error": 1})

    def test_observation_and_control_trends_are_local_to_each_game(self):
        value = game("e4 e5 Nf3 Nc6 Bc4 Bc5", color="black")
        rows = [turn for turn in value["moves"] if turn["actor"] == "sparring"]
        board = chess.Board()
        for turn in value["moves"]:
            if turn["actor"] == "sparring":
                previous = board.copy(stack=True)
                played = previous.pop()
                turn["sparring_decision"]["opponent_observation"] = {
                    "valid": True, "forced": False, "history_before": [move.uci() for move in previous.move_stack],
                    "played_uci": played.uci(), "ply": len(board.move_stack), "before_fen": previous.fen(en_passant="fen"),
                    "depth": 5, "loss_cp": 100, "reason": "measured"}
            board.push_uci(turn["uci"])
        for row, target in zip(rows, [0, 40, 20]):
            row["sparring_decision"]["target_loss_cp"] = target
        result = report.summarize_matches([match(value)])
        self.assertEqual(result["all_moves"]["valid_quality_observations"], 3)
        trend = result["games"][0]["trends"]["control_target_cp"]
        self.assertEqual((trend["increases"], trend["decreases"]), (1, 1))
        rows[0]["sparring_decision"]["opponent_observation"]["history_before"] = ["d2d4"]
        self.assertEqual(report.summarize_matches([match(value)])["all_moves"]["valid_quality_observations"], 2)

    def test_fullmove_window_and_finish_boundary_use_pre_move_ply(self):
        board, sans = chess.Board(), []
        rng = random.Random(515)
        for _ in range(142):
            legal = list(board.legal_moves)
            rng.shuffle(legal)
            for move in legal:
                after = board.copy(stack=True)
                after.push(move)
                if after.outcome(claim_draw=True) is None:
                    sans.append(board.san(move))
                    board.push(move)
                    break
            else:
                self.fail("fixture unexpectedly cannot continue")
        value = game(" ".join(sans))
        result = report.summarize_matches([match(value)])
        self.assertTrue(result["games"][0]["reached_50_full_moves"])
        self.assertTrue(result["games"][0]["reached_finish_phase"])
        self.assertEqual(result["first_50_full_moves"]["sparring_moves"], 50)
        self.assertEqual(result["all_moves"]["phase_counts"]["ramp"], 20)
        self.assertEqual(result["all_moves"]["phase_counts"]["finish"], 1)

    def test_level_behavior_and_sources_are_reported_separately(self):
        first, second = match(game()), match(game())
        second["config"]["ratings"] = [2500]
        second["games"][0]["stockfish_elo"] = 2500
        second["games"][0]["moves"][0]["sparring_decision"]["baseline"]["cp"] = 300
        second["games"][0]["moves"][0]["sparring_decision"]["baseline"]["cp_white"] = 300
        second["catalog_unchanged"] = False
        result = report.summarize_matches([first, second])
        metrics = [row["first_50_full_moves"]["baseline_before_selection"] for row in result["by_level"]]
        self.assertEqual([row["close_positions"] for row in metrics], [2, 1])
        self.assertEqual(len(result["warnings"]), 1)

    def test_different_conditions_and_duplicate_ids_are_rejected(self):
        first, second = match(game()), match(game())
        second["config"]["stockfish_ms"] = 2000
        with self.assertRaises(ValueError):
            report.summarize_matches([first, second])
        with self.assertRaises(ValueError):
            report.summarize_matches([match(game(), game())])

    def test_loader_checks_summary_bytes_and_refuses_external_records(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            record = game()
            record["record_file"] = "game-001.json"
            document = match(record)
            document["games"] = [{key: value for key, value in record.items() if key not in ("moves", "uci_moves")}]
            (root / "game-001.json").write_text(json.dumps(record), encoding="utf-8")
            path = root / "match.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            self.assertEqual(report.load_match(path)["games"][0], record)
            document["games"][0]["result"] = "1-0"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(ValueError):
                report.load_match(path)
            document["games"][0]["record_file"] = "../game.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(ValueError):
                report.load_match(path)


if __name__ == "__main__":
    unittest.main()
