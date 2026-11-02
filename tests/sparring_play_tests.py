"""Adaptive practice evidence, candidate safeguards and late full-strength play."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import chess
import chess.engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sparring_play as policy

LINE = "e4 e5 Nf3 Nc6 Bc4 Bc5 d3 Nf6 Nc3 d6 O-O O-O h3 h6 a3 a6 Be3 Be6 Qd2 Qd7 Rae1 Rae8 Kh1 Kh8 Nh2 Nh7 f4 f5".split()


def quality_game(losses):
    board, rows = chess.Board(), []
    for index, loss in enumerate(losses):
        prefix = [move.uci() for move in board.move_stack]
        fen = board.fen(en_passant="fen")
        human = board.parse_san(LINE[2 * index])
        board.push(human)
        row = {"before_fen": board.fen(en_passant="fen"), "history_uci": [move.uci() for move in board.move_stack],
               "opponent_observation": {"valid": True, "forced": False, "ply": len(prefix) + 1,
                                        "history_before": prefix, "before_fen": fen, "played_uci": human.uci(),
                                        "loss_cp": loss, "depth": 6}}
        bot = board.parse_san(LINE[2 * index + 1])
        row["selected_uci"] = bot.uci()
        rows.append(row)
        board.push(bot)
    if 2 * len(losses) < len(LINE):
        board.push_san(LINE[2 * len(losses)])
    return board, rows


def packet(board, uci, cp=100, depth=6, **extra):
    return {"depth": depth, "score": chess.engine.PovScore(chess.engine.Cp(cp), board.turn),
            "pv": [chess.Move.from_uci(uci)], **extra}


class ProfileTests(unittest.TestCase):
    def test_no_evidence_or_one_blunder_does_not_create_a_weak_opponent(self):
        initial = policy.derive_profile(chess.Board(), [])
        self.assertEqual(initial["status"], "calibrating")
        self.assertEqual(initial["target_loss_cp"], 0)
        self.assertEqual(initial["confidence"], "low")
        board, rows = quality_game([2000])
        one = policy.derive_profile(board, rows)
        self.assertEqual(one["target_loss_cp"], 0)
        self.assertLessEqual(one["smoothed_loss_cp"] - initial["smoothed_loss_cp"], policy.MAX_PROFILE_STEP_CP)

    def test_repeated_errors_increase_concessions_and_accurate_moves_reduce_them(self):
        board, rows = quality_game([250] * 6)
        weak = policy.derive_profile(board, rows)
        self.assertGreater(weak["target_loss_cp"], 60)
        self.assertEqual(weak["status"], "adapting")
        board, rows = quality_game([250] * 6 + [0] * 7)
        improving = policy.derive_profile(board, rows)
        self.assertLess(improving["target_loss_cp"], weak["target_loss_cp"])
        self.assertEqual(len(improving["recent_observations"]), 12)
        board, rows = quality_game([0] * 6)
        strong = policy.derive_profile(board, rows)
        self.assertEqual(strong["target_loss_cp"], 0)
        self.assertGreater(strong["level"], weak["level"])

    def test_duplicates_dictionary_undo_and_new_game_reconstruct_only_actual_history(self):
        board, rows = quality_game([200] * 6)
        original = policy.derive_profile(board, rows)
        self.assertEqual(original, policy.derive_profile(board, rows + rows))
        self.assertEqual(original, policy.derive_profile(board, dict(enumerate(rows))))
        board.pop()
        board.pop()
        board.pop()
        undone = policy.derive_profile(board, rows)
        self.assertEqual(undone["observations"], 5)
        self.assertEqual(policy.derive_profile(chess.Board(), rows)["observations"], 0)

    def test_invalid_forced_stale_wrong_fen_and_conflicting_evidence_is_unknown(self):
        board, rows = quality_game([200])
        for change in ({"valid": False}, {"forced": True}, {"depth": 3}, {"loss_cp": float("nan")},
                       {"loss_cp": True}, {"before_fen": "wrong"}, {"ply": 2}, {"played_uci": "d2d4"}):
            modified = copy.deepcopy(rows)
            modified[0]["opponent_observation"].update(change)
            self.assertEqual(policy.derive_profile(board, modified)["observations"], 0, change)
        changed_reply = copy.deepcopy(rows)
        changed_reply[0]["selected_uci"] = "c7c5"
        self.assertEqual(policy.derive_profile(board, changed_reply)["observations"], 0)
        conflict = copy.deepcopy(rows[0])
        conflict["opponent_observation"]["loss_cp"] = 50
        self.assertEqual(policy.derive_profile(board, [*rows, conflict])["observations"], 0)

    def test_one_new_observation_has_bounded_effect_even_after_long_history(self):
        for count in range(1, 14):
            board, rows = quality_game([0] * (count - 1) + [300])
            before_board, before_rows = quality_game([0] * (count - 1))
            before = policy.derive_profile(before_board, before_rows)["smoothed_loss_cp"]
            current = policy.derive_profile(board, rows)["smoothed_loss_cp"]
            self.assertLessEqual(abs(current - before), policy.MAX_PROFILE_STEP_CP + .001)

    def test_provisional_observation_only_matches_the_actual_latest_human_move(self):
        board, rows = quality_game([200])
        board.pop()
        board.pop()
        pending = {"opponent_observation": rows[0]["opponent_observation"]}
        self.assertEqual(policy.derive_profile(board, [pending])["observations"], 1)
        board.pop()
        self.assertEqual(policy.derive_profile(board, [pending])["observations"], 0)

    def test_profile_exposes_skill_target_and_applies_ramp_exactly_once(self):
        board, rows = quality_game([250] * 6)
        sequence = ["g8h8", "g1h1", "h8g8", "h1g1"]
        index = 0
        while len(board.move_stack) < 120:
            board.push_uci(sequence[index % 4])
            index += 1
        legacy = policy.derive_profile(board, rows, policy="regicide-sparring-play-v1")
        self.assertEqual(legacy["phase"], "ramp")
        self.assertAlmostEqual(legacy["target_loss_cp"], legacy["skill_target_loss_cp"] / 2, places=2)
        self.assertEqual(legacy["policy"], "regicide-sparring-play-v1")
        previous = policy.derive_profile(board, rows, policy="regicide-sparring-play-v2")
        self.assertEqual(previous["policy"], "regicide-sparring-play-v2")
        self.assertAlmostEqual(previous["target_loss_cp"], previous["skill_target_loss_cp"] * .3, places=2)
        thirty = policy.derive_profile(board, rows, policy="regicide-sparring-play-v3")
        self.assertEqual(thirty["policy"], "regicide-sparring-play-v3")
        self.assertAlmostEqual(thirty["target_loss_cp"], thirty["skill_target_loss_cp"] * .225, places=2)
        profile = policy.derive_profile(board, rows, policy="regicide-sparring-play-v4")
        self.assertEqual(profile["policy"], "regicide-sparring-play-v4")
        self.assertAlmostEqual(profile["target_loss_cp"], profile["skill_target_loss_cp"] * .18, places=2)
        current = policy.derive_profile(board, rows)
        self.assertEqual(current["policy"], "regicide-sparring-play-v5")
        self.assertEqual(current["phase"], "finish")
        self.assertEqual(current["target_loss_cp"], 0)
        self.assertEqual(policy._target(board, profile, 300)["target_cp"], 300)
        self.assertNotEqual(policy._target(board, profile, 300)["target_cp"], profile["target_loss_cp"])


class ScheduleAndNaturalnessTests(unittest.TestCase):
    def test_twenty_moves_then_ramp_and_full_at_each_colors_fiftieth_move(self):
        board = chess.Board()
        sequence = ["g1f3", "g8f6", "f3g1", "f6g8"]
        for ply in range(141):
            expected = "finish" if ply >= 98 else "ramp" if ply >= 40 else "calibration" if ply < 6 else "sparring"
            self.assertEqual(policy.phase(board), expected, ply)
            legacy = "finish" if ply >= 140 else "ramp" if ply >= 100 else "calibration" if ply < 6 else "sparring"
            self.assertEqual(policy.phase(board, policy="regicide-sparring-play-v1"), legacy, ply)
            previous = "finish" if ply >= 138 else "ramp" if ply >= 80 else "calibration" if ply < 6 else "sparring"
            self.assertEqual(policy.phase(board, policy="regicide-sparring-play-v2"), previous, ply)
            thirty = "finish" if ply >= 138 else "ramp" if ply >= 60 else "calibration" if ply < 6 else "sparring"
            self.assertEqual(policy.phase(board, policy="regicide-sparring-play-v3"), thirty, ply)
            twenty = "finish" if ply >= 138 else "ramp" if ply >= 40 else "calibration" if ply < 6 else "sparring"
            self.assertEqual(policy.phase(board, policy="regicide-sparring-play-v4"), twenty, ply)
            if ply in (38, 39, 40, 41, 96, 97, 98, 99):
                fullmove = ply // 2 + 1
                expected_fraction = {20: 0, 21: 1 / 30, 49: 29 / 30, 50: 1}[fullmove]
                self.assertEqual(board.fullmove_number, fullmove)
                self.assertEqual(board.turn, chess.WHITE if ply % 2 == 0 else chess.BLACK)
                self.assertAlmostEqual(policy._ramp_fraction(board), expected_fraction, msg=(fullmove, board.turn))
                self.assertAlmostEqual(policy._phase_factor(board), 1 - expected_fraction, msg=(fullmove, board.turn))
            if ply in (38, 39, 40, 41, 136, 137, 138, 139):
                old_fraction = {20: 0, 21: 1 / 50, 69: 49 / 50, 70: 1}[board.fullmove_number]
                self.assertAlmostEqual(policy._ramp_fraction(board, policy="regicide-sparring-play-v4"), old_fraction)
                self.assertAlmostEqual(policy._phase_factor(board, policy="regicide-sparring-play-v4"), 1 - old_fraction)
            if ply in (58, 59, 60, 61, 136, 137, 138, 139):
                old_fraction = {30: 0, 31: 1 / 40, 69: 39 / 40, 70: 1}[board.fullmove_number]
                self.assertAlmostEqual(policy._ramp_fraction(board, policy="regicide-sparring-play-v3"), old_fraction)
                self.assertAlmostEqual(policy._phase_factor(board, policy="regicide-sparring-play-v3"), 1 - old_fraction)
            if ply in (80, 81):
                self.assertAlmostEqual(policy._ramp_fraction(board, policy="regicide-sparring-play-v2"), 1 / 30)
            if ply in (136, 137):
                self.assertAlmostEqual(policy._ramp_fraction(board, policy="regicide-sparring-play-v2"), 29 / 30)
            if ply in (138, 139):
                self.assertEqual(policy._ramp_fraction(board), 1)
            if ply < 140:
                board.push_uci(sequence[ply % 4])
        sparse = chess.Board("7k/8/8/8/8/8/8/KQ6 w - - 0 1")
        self.assertEqual(policy.phase(sparse), "calibration")

    def test_both_colors_gain_exactly_one_thirtieth_each_full_move(self):
        board = chess.Board()
        by_color = {chess.WHITE: [], chess.BLACK: []}
        for ply in range(140):
            if 20 <= board.fullmove_number <= 50:
                fraction = policy._ramp_fraction(board)
                concession = policy._phase_factor(board)
                self.assertAlmostEqual(fraction, (board.fullmove_number - 20) / 30)
                self.assertAlmostEqual(concession, 1 - fraction)
                by_color[board.turn].append((fraction, concession))
            if ply < 139:
                board.push_uci(("g1f3", "g8f6", "f3g1", "f6g8")[ply % 4])
        self.assertEqual(by_color[chess.WHITE], by_color[chess.BLACK])
        for color, values in by_color.items():
            self.assertEqual(len(values), 31, color)
            self.assertEqual(values[0], (0, 1))
            self.assertEqual(values[-1], (1, 0))
            for before, after in zip(values, values[1:]):
                self.assertAlmostEqual(after[0] - before[0], 1 / 30, msg=color)
                self.assertAlmostEqual(before[1] - after[1], 1 / 30, msg=color)

    def test_budget_and_phase_target_are_bounded_and_decline_smoothly(self):
        self.assertEqual(policy.extra_budget(599), 0)
        self.assertEqual(policy.extra_budget(600), 1200)
        self.assertEqual(policy.extra_budget(1000), 2000)
        self.assertEqual(policy.extra_budget(5000), 2000)
        self.assertEqual(policy.extra_budget(1000, ply=97), 2000)
        self.assertEqual(policy.extra_budget(1000, ply=98), 500)
        self.assertEqual(policy.extra_budget(1000, ply=99), 500)
        self.assertEqual(policy.extra_budget(1000, analysis_only=True), 500)
        self.assertEqual(policy.extra_budget(599, ply=98), 0)
        board = chess.Board()
        profile = {"observations": 12, "target_loss_cp": 90}
        targets = []
        for ply in range(141):
            if ply in (39, 40, 60, 80, 96, 97, 98):
                targets.append(policy._target(board, profile, 300)["target_cp"])
            if ply < 140:
                board.push_uci(["g1f3", "g8f6", "f3g1", "f6g8"][ply % 4])
        self.assertEqual(targets[:2], [30, 39])
        self.assertEqual(targets[-1], 300)
        self.assertTrue(all(a <= b for a, b in zip(targets, targets[1:])))
        self.assertFalse(policy._target(board, profile, 300)["active"])

    def test_obvious_piece_donation_is_screened_but_normal_development_is_allowed(self):
        board = chess.Board("6k1/8/8/2p5/8/2P5/8/R2Q2K1 w - - 0 1")
        rejected = policy.naturalness(board, chess.Move.from_uci("d1d4"))
        self.assertFalse(rejected["admitted"])
        self.assertEqual(rejected["reason"], "obvious_material_donation")
        self.assertEqual(rejected["vulnerable_captures"][0]["net_material_cp"], -800)
        start = chess.Board()
        developed = policy.naturalness(start, chess.Move.from_uci("g1f3"))
        self.assertTrue(developed["admitted"])
        self.assertGreater(developed["style_points"], 0)
        self.assertEqual(start.fen(), chess.STARTING_FEN)

    def test_position_band_is_independent_of_profile_and_does_not_keep_half_the_advantage(self):
        board, _ = quality_game([200] * 3)
        profile = {"observations": 12, "target_loss_cp": 90}
        for cp in (-180, -30, 50, 300):
            target = policy._target(board, profile, cp)
            self.assertEqual((target["lower_cp"], target["upper_cp"]), (-100, 100))
            self.assertEqual(target["target_cp"], 30)
            self.assertEqual(target, policy._target(board, {"observations": 0}, cp))

    def test_immediate_opponent_mate_is_not_a_sparring_concession(self):
        board = chess.Board()
        board.push_san("f3")
        board.push_san("e5")
        result = policy.naturalness(board, chess.Move.from_uci("g2g4"))
        self.assertFalse(result["admitted"])
        self.assertEqual(result["reason"], "immediate_mate_reply")

    def test_delayed_exchange_exception_does_not_allow_an_immediate_giveaway(self):
        board = chess.Board("q5k1/8/8/8/r7/5B2/8/Q1B3K1 w - - 0 1")
        move = chess.Move.from_uci("a1a4")
        plan = {"active": True, "stage": 3, "allow_material_return": True, "return_cap_cp": 400}
        self.assertFalse(policy.naturalness(board, move)["admitted"])
        allowed = policy.naturalness(board, move, pacing=plan)
        self.assertTrue(allowed["admitted"])
        self.assertTrue(allowed["material_return_exception"])
        self.assertEqual(allowed["vulnerable_captures"][0]["net_material_cp"], -400)
        self.assertFalse(policy.naturalness(board, move, pacing=plan,
            resource_guard={"active": False, "material_concessions_blocked": True})["admitted"])
        for change in ({"stage": 1}, {"stage": 2}, {"active": False}, {"return_cap_cp": 399}):
            self.assertFalse(policy.naturalness(board, move, pacing={**plan, **change})["admitted"])
        pawn = chess.Board("6k1/8/8/1b6/p7/8/8/Q5K1 w - - 0 1")
        self.assertFalse(policy.naturalness(pawn, move, pacing={**plan, "return_cap_cp": 900})["admitted"])
        hanging = chess.Board("6k1/8/8/2p5/8/2P5/8/R2Q2K1 w - - 0 1")
        self.assertFalse(policy.naturalness(hanging, chess.Move.from_uci("d1d4"), pacing={**plan, "return_cap_cp": 900})["admitted"])

    def test_delayed_exchange_cannot_leave_only_fragile_mating_resources(self):
        board = chess.Board("q5k1/8/8/8/r7/4B3/8/Q1B3K1 w - - 0 1")
        guard = policy.sparring_pacing.resource_guard(board)
        self.assertTrue(board.is_valid())
        self.assertFalse(guard["active"])
        self.assertFalse(guard["material_concessions_blocked"])
        plan = {"active": True, "stage": 3, "allow_material_return": True, "return_cap_cp": 400}
        result = policy.naturalness(board, chess.Move.from_uci("a1a4"), pacing=plan, resource_guard=guard)
        self.assertFalse(result["admitted"])
        self.assertEqual(result["reason"], "material_return_consumes_critical_resources")
        after = result["vulnerable_captures"][0]["resources_after_reply"]
        self.assertEqual(after["reason"], "fragile_pawnless_mating_resources")
        # Opposite-colored bishops retain the resource heuristic. Entering
        # caution alone does not prohibit this compensated exchange.
        board = chess.Board("q5k1/8/8/8/r7/5B2/8/Q1B3K1 w - - 0 1")
        result = policy.naturalness(board, chess.Move.from_uci("a1a4"), pacing=plan,
                                    resource_guard=policy.sparring_pacing.resource_guard(board))
        self.assertTrue(result["admitted"])
        after = result["vulnerable_captures"][0]["resources_after_reply"]
        self.assertFalse(after["active"])
        self.assertTrue(after["material_concessions_blocked"])


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.board, self.rows = quality_game([250] * 6)
        self.legal = [move for move in sorted(self.board.legal_moves, key=lambda m: m.uci())
                      if policy.naturalness(self.board, move)["admitted"]]
        self.base = self.legal[0]
        self.alternative = self.legal[1]
        self.base_info = packet(self.board, self.base.uci(), cp=150)
        self.packets = {depth: packet(self.board, self.base.uci(), cp=150, depth=depth) for depth in (4, 5, 6)}
        self.calls = []
        self.closed = []

    def choose(self, *, cp=85, wrong_depth=False, malformed=False, mate=False, failing=False,
               reliability=None, forced=False, budget=1000, rows=None):
        def search(amount, *, root_move=None, root_moves=None, **options):
            self.calls.append((amount, root_move, root_moves, options))
            self.assertFalse(options["publish"])
            self.assertTrue(options["fresh"])
            if failing:
                raise TimeoutError("controlled")
            if options.get("position") is not None:
                after = options["position"]
                self.assertIsNone(root_move)
                self.assertIsNone(root_moves)
                selected = after.peek()
                move = sorted(after.legal_moves, key=lambda m: m.uci())[0]
                value = cp if selected == self.alternative or cp < 0 else 149
                info = packet(after, move.uci(), -value, options["depth"])
                return chess.engine.BestMove(move, None), info, {options["depth"]: info}
            move = root_move or (self.alternative if self.alternative in root_moves else root_moves[0])
            depth = 3 if wrong_depth and root_move else options["depth"]
            value = cp if move == self.alternative or cp < 0 else 149
            info = packet(self.board, move.uci(), value, depth)
            if mate and root_move:
                info["score"] = chess.engine.PovScore(chess.engine.Mate(-2), self.board.turn)
            if malformed and root_move:
                info["upperbound"] = True
            return chess.engine.BestMove(move, None), info, {depth: info}
        invalid = {"valid": False, "forced": False, "reason": "test_unknown", "loss_cp": None}
        with patch.object(policy.boss_play, "observe_human_move", return_value=(invalid, False)):
            if reliability is not None:
                with patch.object(policy.evaluation_stability, "assess", return_value={"reliability": reliability}):
                    return policy.choose(self.board, self.base, self.base_info, self.packets, search,
                                         lambda: self.closed.append(True), decisions=self.rows if rows is None else rows,
                                         seed="seed", movetime_ms=budget, forced=forced)
            return policy.choose(self.board, self.base, self.base_info, self.packets, search,
                                 lambda: self.closed.append(True), decisions=self.rows if rows is None else rows,
                                 seed="seed", movetime_ms=budget, forced=forced)

    def test_verified_weak_profile_changes_the_move_without_changing_the_board(self):
        saved = self.board.copy(stack=True)
        result, discarded = self.choose()
        self.assertFalse(discarded)
        self.assertEqual(result["reason"], "accepted_sparring_alternative")
        self.assertEqual(result["selected_uci"], self.alternative.uci())
        self.assertEqual(result["loss_cp"], 65)
        self.assertEqual(result["compared_depth"], 6)
        self.assertEqual(result["baseline_budget_ms"], 1000)
        self.assertLessEqual(result["extra_budget_ms"], 2000)
        self.assertTrue(result["naturalness"]["admitted"])
        self.assertEqual(self.board.fen(), saved.fen())
        self.assertEqual(self.board.move_stack, saved.move_stack)
        self.assertTrue(all(row["line"]["depth"] == 3 for row in result["discovery"]))
        self.assertEqual(result["candidate_compared"]["depth"], 6)
        self.assertTrue(any(call[1] is not None and call[3]["depth"] == 6 for call in self.calls))

    def test_accurate_or_uncalibrated_profile_keeps_baseline(self):
        for losses in ([0] * 6, [250] * 2):
            board, rows = quality_game(losses)
            self.board, self.rows = board, rows
            self.base = sorted(board.legal_moves, key=lambda m: m.uci())[0]
            self.base_info = packet(board, self.base.uci())
            self.packets = {d: packet(board, self.base.uci(), depth=d) for d in (4, 5, 6)}
            result, _ = self.choose()
            self.assertEqual(result["selected_uci"], self.base.uci())
        self.assertEqual(self.calls, [])

    def test_unstable_reference_is_rechecked_without_relabeling_it_stable(self):
        for status in ("volatile", "provisional"):
            result, _ = self.choose(reliability=status)
            self.assertEqual(result["selected_uci"], self.alternative.uci())
            self.assertEqual(result["reliability"]["reliability"], status)
            self.assertEqual(result["comparison_basis"], "rechecked_exact_depth")
            self.assertTrue(result["reference_revalidation"]["valid"])

    def test_mate_bounded_shallow_and_below_band_candidates_keep_baseline(self):
        for options in ({"wrong_depth": True}, {"malformed": True}, {"mate": True}, {"cp": -200}):
            result, _ = self.choose(**options)
            self.assertEqual(result["selected_uci"], self.base.uci(), options)
        self.base_info["score"] = chess.engine.PovScore(chess.engine.Mate(3), self.board.turn)
        result, _ = self.choose()
        self.assertEqual(result["reason"], "baseline_unreliable_or_mate")

    def test_forced_short_budget_finish_and_probe_failure_keep_full_baseline(self):
        for options, reason in (({"forced": True}, "user_forced_game"), ({"budget": 500}, "short_budget"),
                                ({"failing": True}, "candidate_search_failed")):
            result, discarded = self.choose(**options)
            self.assertEqual(result["selected_uci"], self.base.uci())
            self.assertEqual(result["reason"], reason)
            self.assertEqual(discarded, options.get("failing", False))
            self.assertEqual(result["profile"]["status"], "holding")
            self.assertEqual(result["band_control"]["status"], "unavailable")
        with patch.object(policy, "phase", return_value="finish"):
            result, _ = self.choose()
            self.assertEqual(result["reason"], "finish_full_strength")

    def test_selection_seed_is_reproducible_and_pgn_metadata_is_explicit(self):
        a, _ = self.choose()
        b, _ = self.choose()
        self.assertEqual(a["selected_uci"], b["selected_uci"])
        self.assertEqual(a["loss_cp"], b["loss_cp"])
        headers = policy.pgn_headers({"enabled": True, "policy": policy.POLICY, "seed": "ab" * 16})
        self.assertEqual(headers["SparringMode"], "1")
        self.assertEqual(headers["SparringSeed"], "ab" * 16)
        old = policy.pgn_headers({"enabled": True, "policy": "regicide-sparring-play-v1"})
        self.assertEqual(old["SparringPolicy"], "regicide-sparring-play-v1")
        self.assertEqual(policy.pgn_headers({"enabled": 1, "policy": policy.POLICY}), {})

    def test_winning_resources_guard_keeps_full_baseline_without_changing_phase(self):
        guard = {"active": True, "reason": "test_latched_material_guard", "nonpawn_cp_total": 2400}
        with patch.object(policy.sparring_pacing, "resource_guard", return_value=guard):
            result, discarded = self.choose()
        self.assertFalse(discarded)
        self.assertEqual(result["selected_uci"], self.base.uci())
        self.assertEqual(result["reason"], "winning_resources_guard")
        self.assertEqual(result["phase"], "sparring")
        self.assertEqual(result["profile"]["status"], "holding")
        self.assertEqual(result["resource_guard"], guard)
        self.assertEqual(self.calls, [])

    def test_resource_caution_still_allows_natural_positional_adjustment(self):
        guard = {"active": False, "material_concessions_blocked": True, "reason": "material_caution"}
        with patch.object(policy.sparring_pacing, "resource_guard", return_value=guard):
            result, discarded = self.choose()
        self.assertFalse(discarded)
        self.assertEqual(result["selected_uci"], self.alternative.uci())
        self.assertEqual(result["reason"], "accepted_sparring_alternative")
        self.assertFalse(result["naturalness"]["material_return_exception"])

    def test_finish_and_resource_guard_observe_quality_once_without_candidates(self):
        previous = self.board.copy(stack=True)
        played = previous.pop()
        observation = {"valid": True, "forced": False, "ply": len(self.board.move_stack),
                       "history_before": [m.uci() for m in previous.move_stack],
                       "played_uci": played.uci(), "before_fen": previous.fen(en_passant="fen"),
                       "loss_cp": 25, "depth": 6, "elapsed_ms": 20}
        for stage, guard, reason in (("finish", False, "finish_full_strength"),
                                     ("sparring", True, "winning_resources_guard")):
            def no_candidate_search(*args, **kwargs):
                self.fail("rating-only mode must not search alternatives")
            with patch.object(policy, "phase", return_value=stage), patch.object(
                    policy.sparring_pacing, "resource_guard", return_value={"active": guard}), patch.object(
                    policy.boss_play, "observe_human_move", return_value=(observation, False)) as observe:
                result, discarded = policy.choose(self.board, self.base, self.base_info, self.packets,
                    no_candidate_search, lambda: None, decisions=self.rows, seed="rating-only", movetime_ms=1000)
            self.assertFalse(discarded)
            self.assertEqual(result["selected_uci"], self.base.uci())
            self.assertEqual(result["reason"], reason)
            self.assertTrue(result["analysis_only"])
            self.assertEqual(result["quality_budget_ms"], 500)
            self.assertEqual(result["extra_budget_ms"], 500)
            self.assertEqual(result["candidate_budget_ms"], 0)
            self.assertEqual(result["discovery"], [])
            self.assertEqual(result["probes"], [])
            self.assertEqual(result["profile"]["observations"], 7)
            self.assertEqual(result["profile"]["status"], "holding")
            observe.assert_called_once()
            self.assertEqual(observe.call_args.kwargs["budget_ms"], 500)


class PositionFeedbackTests(unittest.TestCase):
    def archived_position(self):
        board = chess.Board()
        for san in "Nc3 e5 Nf3 Nc6 e4 Nf6 Bb5 Nd4 O-O Nxb5 Nxb5 c6 Nc3 Qe7 d4 h6".split():
            board.push_san(san)
        return board

    def run_case(self, board, base_uci, cp, candidates, *, depth=5, mate=None, deeper=None,
                 reference_cp=None, stable=True, rows=(), pacing=None, child_cp=None, child_change=None,
                 clock=None, exhaust_before_child=False):
        base_move = chess.Move.from_uci(base_uci)
        info = packet(board, base_uci, cp, depth)
        if mate is not None:
            value, pv = mate
            info.update(score=chess.engine.PovScore(chess.engine.Mate(value), board.turn),
                        pv=[chess.Move.from_uci(uci) for uci in pv])
        packets = {d: {**info, "depth": d} for d in range(max(1, depth - 2), depth + 1)}
        calls = []
        def search(budget, *, root_move=None, root_moves=None, **options):
            calls.append((root_move, options["depth"]))
            if options.get("position") is not None:
                after = options["position"]
                if clock is not None:
                    clock[0] += budget / 1000
                selected = after.peek().uci()
                value = candidates.get(selected, cp if cp is not None else 300) if child_cp is None else child_cp
                reply = sorted(after.legal_moves, key=lambda move: move.uci())[0]
                final = packet(after, reply.uci(), -value, options["depth"])
                if child_change:
                    final.update(child_change(after, reply, options["depth"]))
                return chess.engine.BestMove(reply, None), final, {final["depth"]: final}
            if clock is not None:
                # A deterministic clock charges exactly the granted search
                # budget; unlike a sleep this tests reserve accounting itself.
                clock[0] += budget / 1000
            move = root_move or next((chess.Move.from_uci(uci) for uci in candidates
                                      if chess.Move.from_uci(uci) in root_moves), root_moves[0])
            value = candidates.get(move.uci(), cp if cp is not None else 300)
            if move == base_move and reference_cp is not None:
                value = reference_cp
            searched = options["depth"]
            common = packet(board, move.uci(), value, searched)
            final = packet(board, move.uci(), deeper[move.uci()], searched + 1) if deeper and move.uci() in deeper and searched >= 4 else common
            if exhaust_before_child and clock is not None and root_move and searched >= policy.MIN_DEPTH:
                clock[0] = 1.95  # Only 50ms of the 2s deadline survives a simulated stall.
            return chess.engine.BestMove(move, None), final, {searched: common, final["depth"]: final}
        unknown = {"valid": False, "forced": False, "reason": "test_unknown", "loss_cp": None}
        with patch.object(policy.sparring_pacing, "derive", return_value=pacing or {"active": False}), patch.object(
                policy.boss_play, "observe_human_move", return_value=(unknown, False)), patch.object(
                policy.evaluation_stability, "assess", return_value={"reliability": "stable" if stable else "volatile"}):
            result, discarded = policy.choose(board, base_move, info, packets, search, lambda: None,
                                               decisions=rows, seed="position-feedback", movetime_ms=1000)
        self.assertFalse(discarded)
        return result, calls

    def test_paced_pressure_relief_is_not_reported_as_strict_band_recovery(self):
        board = self.archived_position()
        plan = {"active": True, "stage": 1, "target_cp": 460, "upper_cp": 500,
                "allow_material_return": False, "return_cap_cp": 0}
        result, _ = self.run_case(board, "d4e5", 500, {"c1e3": 460, "d1d3": 32}, pacing=plan)
        self.assertEqual(result["selected_uci"], "c1e3")
        self.assertEqual(result["reason"], "accepted_paced_recovery")
        control = result["band_control"]
        self.assertEqual(control["nominal_target_cp"], 30)
        self.assertEqual(control["nominal_upper_cp"], 100)
        self.assertEqual(control["target_cp"], 460)
        self.assertEqual(control["pacing_status"], "met")
        self.assertEqual(control["status"], "unmet")
        self.assertEqual(control["deviation_cp"], 360)
        self.assertEqual(control["strict_band_status"], "unmet")
        self.assertFalse(control["recovered"])
        self.assertFalse(control["pre40_inside"])
        result, calls = self.run_case(board, "d4e5", 200, {}, pacing=plan)
        self.assertEqual(result["reason"], "paced_baseline_preserved")
        self.assertEqual(result["band_control"]["status"], "unmet")
        self.assertEqual(calls, [])

    def test_last_paced_stage_returns_to_real_band_and_keeps_final_safety_floor(self):
        board = self.archived_position()
        plan = {"active": True, "stage": 4, "target_cp": 30, "upper_cp": 100,
                "allow_material_return": True, "return_cap_cp": 400}
        result, _ = self.run_case(board, "d4e5", 500, {"c1e3": 32, "d1d3": -120}, pacing=plan)
        self.assertEqual(result["selected_uci"], "c1e3")
        self.assertEqual(result["band_control"]["status"], "met")
        self.assertTrue(result["band_control"]["recovered"])
        self.assertTrue(result["band_control"]["pre40_inside"])

    def test_archived_203_to_92_correction_does_not_wait_for_skill_calibration(self):
        board = self.archived_position()
        result, _ = self.run_case(board, "d4e5", 203, {"c1e3": 92})
        self.assertEqual(result["profile"]["observations"], 0)
        self.assertEqual(result["selected_uci"], "c1e3")
        self.assertEqual(result["loss_cp"], 111)
        self.assertEqual(result["band_control"]["status"], "met")
        self.assertTrue(result["band_control"]["recovered"])
        self.assertEqual(result["selected_cp_own"], 92)

    def test_actual_center_target_beats_edge_and_negative_in_band_candidates(self):
        board = self.archived_position()
        result, _ = self.run_case(board, "d4e5", 203, {"c1e3": 99, "d1d3": 32, "a2a4": -86})
        self.assertEqual(result["selected_uci"], "d1d3")
        self.assertEqual(result["band_control"]["target_cp"], 30)
        self.assertEqual(result["selected_cp_own"], 32)

    def test_outside_progress_is_not_mislabeled_recovery(self):
        board = self.archived_position()
        result, _ = self.run_case(board, "d4e5", 410, {"c1e3": 221})
        self.assertEqual(result["selected_uci"], "c1e3")
        self.assertEqual(result["band_control"]["status"], "unmet")
        self.assertEqual(result["band_control"]["deviation_cp"], 121)
        self.assertFalse(result["band_control"]["recovered"])

    def test_black_uses_own_pov_and_losing_side_remains_strongest(self):
        board = chess.Board()
        board.push_san("e4")
        result, _ = self.run_case(board, "g8f6", 203, {"e7e5": 40})
        self.assertEqual(result["selected_cp_own"], 40)
        self.assertEqual(result["selected_cp_white"], -40)
        result, calls = self.run_case(board, "g8f6", -180, {"e7e5": -50})
        self.assertEqual(result["selected_uci"], "g8f6")
        self.assertEqual(result["reason"], "position_below_band_strongest")
        self.assertEqual(result["band_control"]["status"], "unmet")
        self.assertEqual(calls, [])

    def test_deeper_outside_evaluation_and_reference_conflict_prevent_false_recovery(self):
        board = self.archived_position()
        result, _ = self.run_case(board, "d4e5", 203, {"c1e3": 92}, deeper={"c1e3": 221})
        self.assertEqual(result["band_control"]["status"], "unmet")
        self.assertFalse(result["band_control"]["recovered"])
        result, _ = self.run_case(board, "d4e5", 203, {"c1e3": 92}, stable=False, reference_cp=350)
        self.assertEqual(result["selected_uci"], "d4e5")
        self.assertEqual(result["reason"], "reference_revalidation_failed")
        self.assertEqual(result["band_control"]["status"], "unavailable")

    def test_shallow_winning_mate_can_choose_finite_cp_at_separate_depth(self):
        board = chess.Board()
        for san in "f3 e5 g4".split():
            board.push_san(san)
        result, calls = self.run_case(board, "d8h4", None, {"d7d5": 35}, depth=1, mate=(1, ["d8h4"]))
        self.assertEqual(result["selected_uci"], "d7d5")
        self.assertEqual(result["comparison_mode"], "absolute_target_from_winning_mate")
        self.assertIsNone(result["loss_cp"])
        self.assertEqual(result["actual_reference_depth"], 1)
        self.assertEqual(result["verification_depth"], 4)
        self.assertEqual(result["candidate_compared"]["depth"], 4)
        self.assertEqual(result["baseline"]["mate"], 1)
        self.assertTrue(result["band_control"]["recovered"])

    def test_negative_mate_stays_full_and_uncorrected_positive_mate_is_unmet(self):
        board = chess.Board()
        board.push_san("f3")
        board.push_san("e5")
        result, calls = self.run_case(board, "g2g4", None, {"e2e4": 0}, depth=1,
                                      mate=(-1, ["g2g4", "d8h4"]))
        self.assertEqual(result["selected_uci"], "g2g4")
        self.assertEqual(result["reason"], "mate_reference_full_strength")
        self.assertEqual(result["band_control"]["status"], "unmet")
        self.assertEqual(calls, [])
        board.push_san("g4")
        result, _ = self.run_case(board, "d8h4", None, {}, depth=1, mate=(1, ["d8h4"]))
        self.assertEqual(result["band_control"]["status"], "unmet")
        self.assertFalse(result["band_control"]["recovered"])

    def test_postmove_check_records_both_color_scores_without_replacing_root_evidence(self):
        for black in (False, True):
            board = self.archived_position() if not black else chess.Board()
            if black:
                board.push_san("e4")
            baseline, alternative = ("g8f6", "e7e5") if black else ("d4e5", "c1e3")
            saved = board.copy(stack=True)
            result, calls = self.run_case(board, baseline, 203, {alternative: 40}, child_cp=65)
            receipt = result["postmove_verification"]
            self.assertEqual(result["verification_policy"], policy.POSTMOVE_POLICY)
            self.assertTrue(receipt["valid"])
            self.assertEqual(receipt["required_depth"], 4)
            self.assertEqual(receipt["cp_own"], 65)
            self.assertEqual(receipt["cp_white"], -65 if black else 65)
            self.assertEqual(result["selected_cp_own"], 40)  # Keep original-root evidence.
            self.assertEqual(result["band_control"]["selected_postmove_cp"], 65)
            after = saved.copy(stack=True)
            after.push_uci(alternative)
            self.assertEqual(receipt["after_fen"], after.fen(en_passant="fen"))
            self.assertEqual(board.fen(), saved.fen())
            self.assertEqual(board.move_stack, saved.move_stack)

    def test_postmove_floor_or_large_disagreement_rejects_concession(self):
        for child_cp, reason in ((-101, "postmove_below_band_floor"),
                                 (113, "postmove_score_disagreement")):
            with self.subTest(child_cp=child_cp):
                result, _ = self.run_case(self.archived_position(), "d4e5", 203, {"c1e3": 32}, child_cp=child_cp)
                self.assertEqual(result["selected_uci"], "d4e5")
                self.assertEqual(result["reason"], reason)
                self.assertFalse(result["postmove_verification"]["valid"])
                self.assertIsNone(result["band_control"]["selected_postmove_cp"])
                self.assertEqual(result["band_control"]["status"], "unavailable")

    def test_postmove_outside_band_cannot_claim_recovery_even_with_small_disagreement(self):
        result, _ = self.run_case(self.archived_position(), "d4e5", 203, {"c1e3": 92}, child_cp=120)
        self.assertEqual(result["selected_uci"], "c1e3")
        self.assertTrue(result["postmove_verification"]["valid"])
        self.assertEqual(result["reason"], "closest_verified_outside_band")
        self.assertEqual(result["band_control"]["status"], "unmet")
        self.assertEqual(result["band_control"]["strict_deviation_cp"], 20)
        self.assertFalse(result["band_control"]["recovered"])

    def test_postmove_mate_bound_illegal_or_shallow_score_preserves_baseline(self):
        changes = [lambda after, reply, depth: {"score": chess.engine.PovScore(chess.engine.Mate(2), after.turn)},
                   lambda after, reply, depth: {"upperbound": True},
                   lambda after, reply, depth: {"lowerbound": True},
                   lambda after, reply, depth: {"multipv": 2},
                   lambda after, reply, depth: {"pv": [chess.Move.null()]},
                   lambda after, reply, depth: {"pv": []},
                   lambda after, reply, depth: {"depth": 3},
                   lambda after, reply, depth: {"score": chess.engine.PovScore(chess.engine.Cp(32), not after.turn)}]
        for change in changes:
            with self.subTest(change=change):
                result, _ = self.run_case(self.archived_position(), "d4e5", 203, {"c1e3": 32}, child_change=change)
                self.assertEqual(result["selected_uci"], "d4e5")
                self.assertEqual(result["reason"], "postmove_unreliable")

    def test_reserved_postmove_search_fits_total_budget_when_searches_consume_their_limits(self):
        clock = [0.0]
        with patch.object(policy.time, "monotonic", side_effect=lambda: clock[0]):
            result, _ = self.run_case(self.archived_position(), "d4e5", 203, {"c1e3": 32}, clock=clock)
        self.assertEqual(result["selected_uci"], "c1e3")
        self.assertEqual(result["postmove_reserved_ms"], 600)
        self.assertGreaterEqual(result["postmove_verification"]["budget_ms"], policy.POSTMOVE_MIN_MS)
        self.assertLessEqual(result["postmove_verification"]["budget_ms"], policy.POSTMOVE_MAX_MS)
        self.assertLessEqual(result["extra_elapsed_ms"], result["extra_budget_ms"] + .001)
        self.assertGreaterEqual(result["extra_elapsed_ms"], result["postmove_verification"]["elapsed_ms"])

    def test_final_verification_budget_exhaustion_returns_baseline_without_retry(self):
        clock = [0.0]
        with patch.object(policy.time, "monotonic", side_effect=lambda: clock[0]):
            result, calls = self.run_case(self.archived_position(), "d4e5", 203, {"c1e3": 32},
                                          clock=clock, exhaust_before_child=True)
        self.assertEqual(result["selected_uci"], "d4e5")
        self.assertEqual(result["reason"], "postmove_verification_budget")
        self.assertIsNone(result["postmove_verification"])
        self.assertEqual(len([call for call in calls if call[0] is not None and call[1] >= 4]), 1)

    def test_mate_distance_and_finite_cp_with_mating_pv_are_rejected(self):
        board = chess.Board()
        for san in "f3 e5 g4".split():
            board.push_san(san)
        move = chess.Move.from_uci("d8h4")
        info = packet(board, move.uci(), 30, 4)
        probe = policy._absolute_probe(board, move, {4: info}, info, move, 4, 100, 1)
        self.assertFalse(probe["admitted"])
        info["score"] = chess.engine.PovScore(chess.engine.Mate(3), board.turn)
        self.assertIsNone(policy._mate_reference(board, info, move))


class PostmoveEvidenceTests(unittest.TestCase):
    def verify(self, *, white=True, value=40, mutation=None, packet_mutation=None, elapsed=0):
        board = chess.Board()
        if not white:
            board.push_san("e4")
        move = board.parse_san("d4" if white else "e5")
        candidate = {"candidate_compared": {"cp": 40}, "candidate_final": {"cp": 40}}
        calls, clock = [], [0.0]
        def search(budget, **options):
            calls.append(options)
            self.assertFalse(options["publish"])
            self.assertTrue(options["fresh"])
            self.assertNotIn("root_move", options)
            self.assertNotIn("root_moves", options)
            after = options["position"]
            reply = next(iter(after.legal_moves))
            final = packet(after, reply.uci(), -value, options["depth"])
            if mutation:
                final.update(mutation(after, reply))
            same_depth = copy.deepcopy(final)
            if packet_mutation:
                same_depth.update(packet_mutation)
            clock[0] += elapsed / 1000
            return chess.engine.BestMove(reply, None), final, {final["depth"]: same_depth}
        with patch.object(policy.time, "monotonic", side_effect=lambda: clock[0]):
            result = policy._postmove_verification(board, move, candidate, search,
                        depth=6, budget_ms=200, floor_cp=-100)
        return result, calls

    def test_exact_unrestricted_reply_evidence_in_both_colors(self):
        for white in (True, False):
            result, calls = self.verify(white=white)
            self.assertTrue(result["valid"])
            self.assertEqual(result["cp_own"], 40)
            self.assertEqual(result["cp_white"], 40 if white else -40)
            self.assertEqual(result["line"]["depth"], 6)
            self.assertEqual(len(calls), 1)

    def test_mismatched_final_packet_cannot_borrow_another_score(self):
        result, _ = self.verify(packet_mutation={"pv": []})
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "postmove_unreliable")

    def test_disagreement_limit_is_explicit_and_inclusive(self):
        result, _ = self.verify(value=120)
        self.assertTrue(result["valid"])
        self.assertEqual(result["disagreement_cp"], 80)
        result, _ = self.verify(value=121)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "postmove_score_disagreement")

    def test_search_overrun_is_not_a_verified_result(self):
        result, _ = self.verify(elapsed=221)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "postmove_verification_timeout")

    def test_secondary_rank_cannot_be_a_policy_score_or_mate_reference(self):
        board = chess.Board()
        move = board.parse_san("e4")
        self.assertIsNone(policy._score_line(board, packet(board, move.uci(), multipv=2), move))
        for san in "f3 e5 g4".split():
            board.push_san(san)
        mate = board.parse_san("Qh4#")
        info = packet(board, mate.uci(), score=chess.engine.PovScore(chess.engine.Mate(1), board.turn), multipv=2)
        self.assertIsNone(policy._mate_reference(board, info, mate))


if __name__ == "__main__":
    unittest.main()
