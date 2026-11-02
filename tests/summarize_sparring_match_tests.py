"""Independent reporting checks; synthetic scores are not performance data."""
import copy
import contextlib
import io
import json
from pathlib import Path
import random
import sys
import tempfile
import unittest

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import summarize_sparring_match as report


def game(sans="e4 e5 Nf3 Nc6", color="white", number=1, policy="regicide-sparring-play-v2"):
    board, rows, history = chess.Board(), [], []
    for san in sans.split():
        before = board.copy(stack=True)
        move = board.parse_san(san)
        ply = len(history) + 1
        actor = "sparring" if board.turn == (color == "white") else "stockfish"
        line = {"uci": move.uci(), "pv_uci": [move.uci()], "depth": 5, "cp": 100,
                "cp_white": 100 if board.turn else -100}
        decision = {"policy": policy, "ply": ply, "before_fen": before.fen(en_passant="fen"),
                    "history_uci": list(history), "selected_uci": move.uci(), "baseline_uci": move.uci(),
                    "baseline": line, "loss_cp": 0, "reason": "baseline_preserved", "phase": report._phase(ply - 1, policy),
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


def long_sans():
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
            raise AssertionError("fixture unexpectedly cannot continue")
    return sans


def add_stockfish_scores(value, cp=0):
    for turn in value["moves"]:
        if turn["actor"] == "stockfish":
            turn["stockfish_score_packet"] = {"cp_white": cp, "mate_white": None, "depth": 6,
                "bound": "exact", "pv_legal": True, "score_pov_valid": True, "score_pov": "white",
                "before_fen": turn["before_fen"], "pv_uci": [turn["uci"]], "is_selected_move_score": False}


def winning_mate_choice():
    value = game("f3 e5 g4 Nc6", color="black")
    turn = value["moves"][-1]
    baseline = {"uci": "d8h4", "pv_uci": ["d8h4"], "depth": 1, "cp": None, "cp_white": None,
                "mate": 1, "mate_white": -1, "winner": "black", "pov": "black",
                "source_fen": turn["before_fen"], "score_bound": "exact"}
    candidate = {"uci": "b8c6", "pv_uci": ["b8c6"], "depth": 4, "cp": 20, "cp_white": -20}
    turn["sparring_decision"].update(baseline_uci="d8h4", baseline=baseline,
        reference_mate=copy.deepcopy(baseline), candidate_compared=candidate,
        candidate_final={**candidate, "depth": 5, "cp": 10, "cp_white": -10},
        loss_cp=None, actual_reference_depth=1, verification_depth=4, compared_depth=4,
        comparison_mode="absolute_target_from_winning_mate", comparison_basis="exact_winning_mate_reference")
    return value


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

    def test_winning_mate_reference_can_choose_independent_finite_target_without_imputing_cp_loss(self):
        value = winning_mate_choice()
        result = report.summarize_matches([match(value)])
        metrics = result["all_moves"]
        self.assertEqual(metrics["different_from_baseline"], 1)
        self.assertEqual(metrics["verified_winning_mate_avoidance"], 1)
        self.assertEqual(metrics["verified_positive_loss_concessions"], 0)
        self.assertEqual(metrics["concession_loss_cp"]["n"], 0)
        self.assertEqual(metrics["baseline_before_selection"]["known_exact_mate"], 1)
        self.assertEqual(metrics["selected_root_same_depth"]["known_exact_cp"], 2)
        sample = report.inspect_game(value)["samples"][-1]
        self.assertIsNone(sample["baseline_cp_white"])
        self.assertIsNone(sample["loss_cp"])
        self.assertIsNone(sample["baseline_minus_loss_cp_white"])
        self.assertEqual(sample["baseline_mate_white"], -1)
        self.assertEqual(sample["selected_cp_white"], -20)
        band = result["games"][0]["continuous_first40_band"]
        self.assertEqual(band["baseline_before_selection"]["outside_mate_count"], 1)
        self.assertEqual(band["selected_root_same_depth"]["inside_count"], 2)
        self.assertEqual(band["selected_root_same_depth"]["outside_mate_count"], 0)

    def test_incoherent_mate_reference_or_finite_verification_is_not_trusted(self):
        mutations = (
            lambda d: d["baseline"].update(winner="white"),
            lambda d: d["baseline"].update(pov="white"),
            lambda d: d["baseline"].update(source_fen="wrong"),
            lambda d: d["baseline"].update(pv_uci=["d8h5"]),
            lambda d: d["baseline"].update(mate=-1, mate_white=1),
            lambda d: d["baseline"].update(mate=2, mate_white=-2),
            lambda d: d["baseline"].update(score_bound="lower"),
            lambda d: d.update(loss_cp=0),
            lambda d: d.update(verification_depth=3, compared_depth=3),
            lambda d: d.update(actual_reference_depth=4),
            lambda d: d["candidate_compared"].update(depth=3),
            lambda d: d["candidate_final"].update(depth=3),
            lambda d: d["candidate_final"].update(depth=4, cp=21, cp_white=-21),
            lambda d: d.update(comparison_mode="unsupported"),
        )
        for mutate in mutations:
            value = winning_mate_choice()
            decision = value["moves"][-1]["sparring_decision"]
            mutate(decision)
            # Keep reference copies coherent so raw proof checks are exercised.
            decision["reference_mate"] = copy.deepcopy(decision["baseline"])
            result = report.summarize_matches([match(value)])
            self.assertEqual(result["all_moves"]["verified_winning_mate_avoidance"], 0)
            self.assertEqual(result["games"][0]["continuous_first40_band"]["selected_root_same_depth"]["unknown_count"], 1)

    def test_verified_outside_mate_avoidance_remains_outside_not_unknown(self):
        value = winning_mate_choice()
        decision = value["moves"][-1]["sparring_decision"]
        decision["candidate_compared"].update(cp=150, cp_white=-150)
        decision["candidate_final"].update(cp=160, cp_white=-160)
        result = report.summarize_matches([match(value)])
        self.assertEqual(result["all_moves"]["verified_winning_mate_avoidance"], 1)
        self.assertEqual(result["all_moves"]["winning_mate_avoidance_inside_report_band"], 0)
        band = result["games"][0]["continuous_first40_band"]["selected_root_same_depth"]
        self.assertEqual((band["outside_count"], band["unknown_count"]), (1, 0))
        self.assertEqual(band["sequence"][-1]["cp_white"], -150)

    def test_known_selected_and_opponent_mates_are_outside_without_cp_conversion(self):
        own = game("f3 e5 g4 Qh4#", color="black")
        decision = own["moves"][-1]["sparring_decision"]
        decision["baseline"] = winning_mate_choice()["moves"][-1]["sparring_decision"]["baseline"]
        decision["loss_cp"] = None
        result = report.summarize_matches([match(own)])
        selected = result["games"][0]["continuous_first40_band"]["selected_root_same_depth"]
        self.assertEqual(selected["outside_mate_count"], 1)
        self.assertEqual(selected["sequence"][-1]["classification"], "outside")
        self.assertIsNone(selected["sequence"][-1]["cp_white"])
        self.assertEqual(result["all_moves"]["verified_winning_mate_avoidance"], 0)
        opponent = game("f3 e5 g4 Qh4#", color="white")
        add_stockfish_scores(opponent)
        opponent["moves"][-1]["stockfish_score_packet"].update(cp_white=None, mate_white=-1, winner="black")
        result = report.summarize_matches([match(opponent)])
        sf = result["games"][0]["continuous_first40_band"]["stockfish_next_root_auxiliary"]
        self.assertEqual(sf["outside_mate_count"], 1)
        self.assertEqual(sf["sequence"][-1]["classification"], "outside")
        self.assertIsNone(sf["sequence"][-1]["cp_white"])

    def test_stockfish_next_root_is_separate_and_pv_can_differ_from_selected(self):
        value = game("e4 e5")
        turn = value["moves"][1]
        turn["stockfish_score_packet"] = {"cp_white": -120, "mate_white": None, "depth": 6,
            "bound": "exact", "pv_legal": True, "score_pov_valid": True, "score_pov": "white",
            "before_fen": turn["before_fen"], "pv_uci": ["c7c5"], "is_selected_move_score": False}
        metric = report.summarize_matches([match(value)])["all_moves"]
        self.assertEqual(metric["stockfish_next_root_auxiliary"]["close_fraction_of_known"], 0)
        historical = report.summarize_matches([match(value)], close_cp=150)["all_moves"]
        self.assertEqual(historical["stockfish_next_root_auxiliary"]["close_fraction_of_known"], 1)
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
        sans = long_sans()
        value = game(" ".join(sans))
        result = report.summarize_matches([match(value)])
        self.assertTrue(result["games"][0]["reached_target_full_moves"])
        self.assertTrue(result["games"][0]["reached_finish_phase"])
        self.assertEqual(result["first_40_full_moves"]["sparring_moves"], 40)
        self.assertEqual(result["all_moves"]["phase_counts"]["ramp"], 29)
        self.assertEqual(result["all_moves"]["phase_counts"]["finish"], 2)
        self.assertEqual(result["games"][0]["phase_activation"]["finish"]["first_ply"], 139)
        self.assertTrue(result["games"][0]["continuous_first40_band"]["selected_root_same_depth"]["sustained_through_target_full_moves"])
        for color, plies, ramp, finish in (("white", 79, False, False), ("white", 80, False, False),
                                          ("black", 81, False, False), ("black", 82, True, False),
                                          ("white", 137, True, False), ("white", 138, True, False),
                                          ("white", 139, True, True), ("black", 139, True, False),
                                          ("black", 140, True, True)):
            current = game(" ".join(sans[:plies]), color=color)
            row = report.summarize_matches([match(current)])["games"][0]
            self.assertEqual(row["reached_ramp_phase"], ramp, (color, plies))
            self.assertEqual(row["reached_finish_phase"], finish, (color, plies))
            self.assertEqual(row["board_reached_finish_threshold"], plies >= 138)
            if finish:
                self.assertEqual(row["phase_activation"]["finish"]["first_ply"], 139 if color == "white" else 140)

    def test_inclusive_one_point_threshold_and_historical_override_are_local(self):
        values = []
        for number, cp in enumerate((-101, -100, 100, 101), 1):
            value = game("e4", number=number)
            value["moves"][0]["sparring_decision"]["baseline"].update(cp=cp, cp_white=cp)
            value["moves"][0]["sparring_decision"]["band_control"] = {"status": "met", "pre50_inside": True, "selected_cp": 0}
            values.append(value)
        source = match(*values)
        first = report.summarize_matches([source])
        self.assertEqual(first["schema"], "regicide.sparring-match-summary.v2")
        self.assertEqual(first["close_cp"], 100)
        self.assertEqual(first["all_moves"]["selected_root_same_depth"]["close_positions"], 2)
        statuses = [row["continuous_first40_band"]["selected_root_same_depth"]["sequence"][0]["classification"]
                    for row in first["games"]]
        self.assertEqual(statuses, ["outside", "inside", "inside", "outside"])
        historical = report.summarize_matches([source], close_cp=150)
        self.assertEqual(historical["all_moves"]["selected_root_same_depth"]["close_positions"], 4)
        self.assertIn("<=150", historical["definitions"]["close"])
        self.assertEqual(report.summarize_matches([source]), first)  # No global threshold mutation.
        for invalid in (True, 99, 101, 100.0):
            with self.assertRaises(ValueError):
                report.summarize_matches([source], close_cp=invalid)

    def test_unknown_breaks_outside_runs_and_is_not_a_recovery(self):
        sequence = [{"ply": 2 * index + 1, "evaluation_record_ply": 2 * index + 1, "cp_white": cp}
                    for index, cp in enumerate((101, 120, 100, -101, None, -100, 101, 0))]
        metric = report._band_sequence(sequence, False, 100)
        self.assertEqual((metric["inside_count"], metric["outside_count"], metric["unknown_count"]), (3, 4, 1))
        self.assertEqual(metric["first_exit_ply"], 1)
        self.assertEqual(metric["longest_consecutive_outside"], 2)
        self.assertEqual(metric["recovery_count"], 2)
        self.assertEqual([row["own_turns"] for row in metric["recoveries"]], [2, 1])
        self.assertEqual([row["recovery_ply"] for row in metric["recoveries"]], [5, 15])
        self.assertFalse(metric["sustained_through_target_full_moves"])

    def test_short_prefix_mate_unknown_and_cross_game_runs_never_become_full_success(self):
        values = []
        for number, cp in enumerate((101, 101, 0), 1):
            value = game("e4", number=number)
            value["moves"][0]["sparring_decision"]["baseline"].update(cp=cp, cp_white=cp)
            values.append(value)
        result = report.summarize_matches([match(*values)])
        total = result["continuous_first40_band"]["selected_root_same_depth"]
        self.assertEqual(total["longest_consecutive_outside_in_one_game"], 1)
        self.assertEqual(total["recovery_count"], 0)
        self.assertEqual(total["partial_prefix_all_inside_games"], 1)
        self.assertEqual(total["sustained_through_target_full_moves_games"], 0)
        value = game(" ".join(long_sans()[:100]))
        value["moves"][0]["sparring_decision"]["baseline"]["mate"] = 2
        result = report.summarize_matches([match(value)])
        band = result["games"][0]["continuous_first40_band"]["selected_root_same_depth"]
        self.assertEqual((band["inside_count"], band["unknown_count"]), (39, 1))
        self.assertFalse(band["sustained_through_target_full_moves"])
        self.assertEqual(result["all_moves"]["selected_root_same_depth"]["close_fraction_of_known"], 1)

    def test_stockfish_sequence_follows_each_own_move_and_includes_black_fortieth_reply(self):
        sans = long_sans()
        for length in (80, 81):
            value = game(" ".join(sans[:length]), color="black")
            add_stockfish_scores(value)
            value["moves"][0]["stockfish_score_packet"]["cp_white"] = 999  # Initial root is not after our move.
            result = report.summarize_matches([match(value)])
            band = result["games"][0]["continuous_first40_band"]["stockfish_next_root_auxiliary"]
            self.assertEqual(band["sample_count"], 40)
            self.assertEqual(band["outside_count"], 0)
            self.assertEqual(band["sequence"][0]["evaluation_record_ply"], 3)
            self.assertEqual(band["sequence"][-1]["evaluation_record_ply"], 81)
            self.assertEqual(band["unknown_count"], 1 if length == 80 else 0)
            self.assertEqual(band["sustained_through_target_full_moves"], length == 81)
        row = value["moves"][2]["stockfish_score_packet"]
        row["mate_white"] = 1
        result = report.summarize_matches([match(value)])
        band = result["games"][0]["continuous_first40_band"]["stockfish_next_root_auxiliary"]
        self.assertEqual(band["unknown_count"], 1)
        self.assertFalse(band["sustained_through_target_full_moves"])

    def test_old_policy_is_supported_preserved_and_never_pooled_with_new(self):
        old = match(game(policy="regicide-sparring-play-v1"))
        old["policy"] = "regicide-sparring-play-v1"
        old["source"] = {"commit": "historical", "dirty": False}
        result = report.summarize_matches([old])
        self.assertEqual(result["policy"], "regicide-sparring-play-v1")
        self.assertEqual(result["input_runs"][0]["source"]["commit"], "historical")
        self.assertEqual(result["all_moves"]["valid_decisions"], 2)
        with self.assertRaisesRegex(ValueError, "different sparring policies"):
            report.summarize_matches([old, match(game())])
        bad = copy.deepcopy(old)
        bad["games"][0]["moves"][0]["sparring_decision"]["policy"] = report.POLICY
        with self.assertRaisesRegex(ValueError, "different sparring policies"):
            report.summarize_matches([bad])

    def test_v1_phase_schedule_and_explicit_fifty_move_reporting_are_preserved(self):
        sans = long_sans()
        for length, finish in ((140, False), (141, False), (142, True)):
            old = game(" ".join(sans[:length]), color="black", policy="regicide-sparring-play-v1")
            result = report.summarize_matches([match(old)], target_fullmoves=50)
            self.assertEqual(result["policy_phase_root_plies"], {"ramp": 100, "finish": 140})
            self.assertEqual(result["first_50_full_moves"]["sparring_moves"], 50)
            self.assertEqual(result["games"][0]["reached_finish_phase"], finish)
            self.assertTrue(result["games"][0]["continuous_first50_band"]["selected_root_same_depth"]["sustained_through_target_full_moves"])
            self.assertEqual(result["games"][0]["issues"], [])
            comparison = report.summarize_matches([match(old)], target_fullmoves=40)
            self.assertEqual(comparison["first_40_full_moves"]["sparring_moves"], 40)
            self.assertEqual(comparison["games"][0]["reached_finish_phase"], finish)
        for invalid in (True, 0, 41, 40.0):
            with self.assertRaises(ValueError):
                report.summarize_matches([match(old)], target_fullmoves=invalid)

    def test_v3_thirty_move_window_and_both_color_phase_boundaries(self):
        sans = long_sans()
        for color in ("white", "black"):
            ramp_ply, finish_ply = (61, 139) if color == "white" else (62, 140)
            for length in (59, 60, 61, 62, 138, 139, 140, 142):
                value = game(" ".join(sans[:length]), color=color, policy="regicide-sparring-play-v3")
                result = report.summarize_matches([match(value)])
                row = result["games"][0]
                self.assertEqual(result["target_fullmoves"], 30)
                self.assertEqual(result["policy_phase_root_plies"], {"ramp": 60, "finish": 138})
                self.assertEqual(row["reached_target_full_moves"], length >= 60)
                self.assertEqual(row["board_reached_ramp_threshold"], length >= 60)
                self.assertEqual(row["reached_ramp_phase"], length >= ramp_ply)
                self.assertEqual(row["reached_finish_phase"], length >= finish_ply)
                self.assertEqual(row["issues"], [])
                if length >= ramp_ply:
                    self.assertEqual(row["phase_activation"]["ramp"]["first_ply"], ramp_ply)
                if length >= finish_ply:
                    self.assertEqual(row["phase_activation"]["finish"]["first_ply"], finish_ply)
                if length >= 60:
                    self.assertEqual(result["first_30_full_moves"]["sparring_moves"], 30)
                    self.assertTrue(row["continuous_first30_band"]["selected_root_same_depth"]["sustained_through_target_full_moves"])
            self.assertEqual(result["all_moves"]["phase_counts"]["ramp"], 39)

    def test_v3_reporting_window_override_does_not_rewrite_policy_or_pool_v2(self):
        current = match(game(" ".join(long_sans()), policy="regicide-sparring-play-v3"))
        historical = match(game(policy="regicide-sparring-play-v2"))
        self.assertEqual(report.summarize_matches([historical])["target_fullmoves"], 40)
        result = report.summarize_matches([current], target_fullmoves=40)
        self.assertEqual(result["first_40_full_moves"]["sparring_moves"], 40)
        self.assertEqual(result["policy_phase_root_plies"], {"ramp": 60, "finish": 138})
        with self.assertRaisesRegex(ValueError, "different sparring policies"):
            report.summarize_matches([historical, current])

    def test_v4_twenty_move_window_and_both_color_phase_boundaries(self):
        sans = long_sans()
        for color in ("white", "black"):
            ramp_ply, finish_ply = (41, 139) if color == "white" else (42, 140)
            for length in (39, 40, 41, 42, 138, 139, 140, 142):
                value = game(" ".join(sans[:length]), color=color, policy="regicide-sparring-play-v4")
                result = report.summarize_matches([match(value)])
                row = result["games"][0]
                self.assertEqual(result["target_fullmoves"], 20)
                self.assertEqual(result["policy_phase_root_plies"], {"ramp": 40, "finish": 138})
                self.assertEqual(row["reached_target_full_moves"], length >= 40)
                self.assertEqual(row["board_reached_ramp_threshold"], length >= 40)
                self.assertEqual(row["reached_ramp_phase"], length >= ramp_ply)
                self.assertEqual(row["reached_finish_phase"], length >= finish_ply)
                self.assertEqual(row["issues"], [])
                if length >= ramp_ply:
                    self.assertEqual(row["phase_activation"]["ramp"]["first_ply"], ramp_ply)
                if length >= finish_ply:
                    self.assertEqual(row["phase_activation"]["finish"]["first_ply"], finish_ply)
                if length >= 40:
                    self.assertEqual(result["first_20_full_moves"]["sparring_moves"], 20)
                    self.assertTrue(row["continuous_first20_band"]["selected_root_same_depth"]["sustained_through_target_full_moves"])
            self.assertEqual(result["all_moves"]["phase_counts"]["ramp"], 49)

    def test_v4_reporting_window_override_preserves_policy_and_refuses_mixed_history(self):
        current = match(game(" ".join(long_sans()), policy="regicide-sparring-play-v4"))
        for window in (20, 30, 40, 50):
            result = report.summarize_matches([current], target_fullmoves=window)
            self.assertEqual(result[f"first_{window}_full_moves"]["sparring_moves"], window)
            self.assertEqual(result["policy"], "regicide-sparring-play-v4")
            self.assertEqual(result["policy_phase_root_plies"], {"ramp": 40, "finish": 138})
        for historical_policy, old_window in (("regicide-sparring-play-v1", 40),
                                              ("regicide-sparring-play-v2", 40),
                                              ("regicide-sparring-play-v3", 30)):
            historical = match(game(policy=historical_policy))
            self.assertEqual(report.summarize_matches([historical])["target_fullmoves"], old_window)
            with self.assertRaisesRegex(ValueError, "different sparring policies"):
                report.summarize_matches([historical, current])

    def test_winning_mate_exception_remains_readable_for_v2_v3_and_v4(self):
        for policy in ("regicide-sparring-play-v2", "regicide-sparring-play-v3", "regicide-sparring-play-v4"):
            value = winning_mate_choice()
            for row in value["moves"]:
                if row["sparring_decision"] is not None:
                    row["sparring_decision"]["policy"] = policy
            result = report.summarize_matches([match(value)])
            self.assertEqual(result["all_moves"]["verified_winning_mate_avoidance"], 1)

    def test_pacing_receipts_do_not_relax_strict_band_or_bridge_missing_recovery(self):
        value = game("e4 e5 Nf3 Nc6 Bc4 Bc5 d3 d6 Nc3 Nf6", color="black")
        own = [row["sparring_decision"] for row in value["moves"] if row["actor"] == "sparring"]
        for stage, (decision, cp) in enumerate(zip(own, (300, 250, 200, 150, 50))):
            decision["baseline"].update(cp=cp, cp_white=-cp)
            decision["pacing"] = {"active": True, "event_ply": 1, "stage": stage, "upper_cp": 400,
                                  "reason": "staged_major_loss_recovery"}
            decision["band_control"] = {"pacing_status": "met", "status": "met", "pre40_inside": True}
            decision["resource_guard"] = {"active": stage >= 3, "material_concessions_blocked": stage >= 2,
                                           "trigger_ply": 7 if stage >= 3 else None, "reason": "synthetic"}
        result = report.summarize_matches([match(value)])
        self.assertEqual(result["all_moves"]["strict_band_while_pacing"], {"outside": 4, "inside": 1})
        self.assertEqual(result["all_moves"]["resource_guard_active_moves"], 2)
        self.assertEqual(result["all_moves"]["material_return_blocked_moves"], 3)
        pacing = result["games"][0]["pacing"]
        self.assertEqual(pacing["episode_count"], 1)
        self.assertEqual(pacing["resource_guard_trigger_plies"], [7])
        self.assertEqual(pacing["episodes"][0]["first_verified_restoration"], {"ply": 10, "stage": 4})
        self.assertTrue(pacing["episodes"][0]["restoration_within_stages_2_to_4"])
        self.assertFalse(result["games"][0]["continuous_first40_band"]["selected_root_same_depth"]["sustained_through_target_full_moves"])
        self.assertFalse(result["games"][0]["reached_finish_phase"], "resource guard does not rewrite phase")
        missing = copy.deepcopy(value)
        missing["moves"][7]["sparring_decision"]["baseline"] = None
        result = report.summarize_matches([match(missing)])
        self.assertIsNone(result["games"][0]["pacing"]["episodes"][0]["first_verified_restoration"])
        gap = copy.deepcopy(value)
        gap["moves"][7]["sparring_decision"]["pacing"] = None
        result = report.summarize_matches([match(gap)])
        self.assertIsNone(result["games"][0]["pacing"]["episodes"][0]["first_verified_restoration"])
        temporary = copy.deepcopy(value)
        temporary["moves"][5]["sparring_decision"]["baseline"].update(cp=50, cp_white=-50)
        temporary["moves"][9]["sparring_decision"]["baseline"].update(cp=150, cp_white=-150)
        episode = report.summarize_matches([match(temporary)])["games"][0]["pacing"]["episodes"][0]
        self.assertEqual(episode["first_verified_restoration"], {"ply": 6, "stage": 2})
        self.assertEqual(episode["outcome"], "outside_at_stage4", "temporary recovery does not hide the final outside sample")

    def test_level_behavior_and_sources_are_reported_separately(self):
        first, second = match(game()), match(game())
        second["config"]["ratings"] = [2500]
        second["games"][0]["stockfish_elo"] = 2500
        second["games"][0]["moves"][0]["sparring_decision"]["baseline"]["cp"] = 300
        second["games"][0]["moves"][0]["sparring_decision"]["baseline"]["cp_white"] = 300
        second["catalog_unchanged"] = False
        result = report.summarize_matches([first, second])
        metrics = [row["first_40_full_moves"]["baseline_before_selection"] for row in result["by_level"]]
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

    def test_cli_can_rerender_historical_threshold_without_changing_default(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            record = game("e4", policy="regicide-sparring-play-v1")
            record["moves"][0]["sparring_decision"]["baseline"].update(cp=120, cp_white=120)
            record["record_file"] = "game-001.json"
            document = match(record)
            document["games"] = [{key: value for key, value in record.items() if key not in ("moves", "uci_moves")}]
            (root / "game-001.json").write_text(json.dumps(record), encoding="utf-8")
            source = root / "match.json"
            source.write_text(json.dumps(document), encoding="utf-8")
            for threshold in (150, 100):
                output = root / f"summary-{threshold}.json"
                options = [str(source), "--output", str(output)]
                if threshold == 150:
                    options.extend(["--close-cp", "150"])
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(report.main(options), 0)
                result = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(result["close_cp"], threshold)
                self.assertEqual(result["policy"], "regicide-sparring-play-v1")
                self.assertEqual(result["all_moves"]["selected_root_same_depth"]["close_positions"], int(threshold == 150))

    def test_cli_accepts_explicit_twenty_move_window_and_defaults_v4_to_twenty(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            record = game(policy="regicide-sparring-play-v4")
            record["record_file"] = "game-001.json"
            document = match(record)
            document["games"] = [{key: value for key, value in record.items() if key not in ("moves", "uci_moves")}]
            (root / "game-001.json").write_text(json.dumps(record), encoding="utf-8")
            source = root / "match.json"
            source.write_text(json.dumps(document), encoding="utf-8")
            for explicit in (False, True):
                output = root / f"summary-{explicit}.json"
                options = [str(source), "--output", str(output)]
                if explicit:
                    options.extend(["--target-fullmoves", "20"])
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(report.main(options), 0)
                result = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(result["target_fullmoves"], 20)
                self.assertEqual(result["policy_phase_root_plies"], {"ramp": 40, "finish": 138})
if __name__ == "__main__":
    unittest.main()
