"""Progressive pressure, retained advantages, and rules-grounded draw choices."""
from functools import lru_cache
import unittest
from unittest.mock import patch

import chess

import sparring_play_tests as native_support
import sparring_referee_tests as referee_support
from ramp_guard_tests import schedule_board

policy = native_support.policy


@lru_cache(maxsize=None)
def live(ply):
    return referee_support.long_board(ply)


@lru_cache(maxsize=None)
def repetition_scene(cycles, prefix_ply=30):
    """A live 15-move prefix followed by a real reversible four-ply cycle."""
    original = live(prefix_ply)
    for first in original.legal_moves:
        piece = original.piece_at(first.from_square)
        if piece.piece_type == chess.PAWN or original.is_capture(first) or original.is_castling(first):
            continue
        one = original.copy(stack=True)
        one.push(first)
        for second in one.legal_moves:
            piece = one.piece_at(second.from_square)
            if piece.piece_type == chess.PAWN or one.is_capture(second) or one.is_castling(second):
                continue
            two = one.copy(stack=True)
            two.push(second)
            back_first = chess.Move(first.to_square, first.from_square)
            if back_first not in two.legal_moves:
                continue
            two.push(back_first)
            back_second = chess.Move(second.to_square, second.from_square)
            if back_second not in two.legal_moves:
                continue
            restored = two.copy(stack=True)
            restored.push(back_second)
            if restored.fen().split()[:4] != original.fen().split()[:4]:
                continue
            cycle = [first, second, back_first, back_second]
            board = original.copy(stack=True)
            try:
                for _ in range(cycles - 1):
                    for move in cycle:
                        board.push(move)
                prefix = 2 if cycles > 1 else 3
                for move in cycle[:prefix]:
                    if move not in board.legal_moves:
                        raise ValueError("cycle changed")
                    board.push(move)
            except ValueError:
                continue
            repeat = cycle[prefix]
            if repeat not in board.legal_moves:
                continue
            alternatives = [move for move in board.legal_moves
                            if not policy.draw_risk(board, move)["draw_or_claimable"]]
            if alternatives:
                return board, repeat, next(move for move in alternatives if move != repeat)
    raise AssertionError("no reversible cycle with a non-drawing alternative")


class PressureTests(unittest.TestCase):
    def probe(self, move, cp, pressure=0, final=None):
        return {"admitted": True, "uci": move.uci(),
                "candidate_compared": {"cp": cp}, "candidate_final": {"cp": cp if final is None else final},
                "loss_cp": 300 - cp, "naturalness": {"style_points": 0, "test_pressure": pressure}}

    def choose(self, board, baseline, probes, cp=300, target=None):
        control = policy._target(board, {}, cp)
        if target is not None:
            control["target_cp"] = target
        with patch.object(policy, "_candidate_pressure", side_effect=lambda _board, _baseline, natural:
                          {"points": natural["test_pressure"], "weight": policy._ramp_strength(board)}):
            return policy._choose_verified(board, baseline, {"cp": cp}, probes, control, "pressure-test")

    def test_small_advantage_is_retained_and_caps_shrink_on_both_colors(self):
        previous = None
        for fullmove in range(16, 51):
            caps = []
            for offset in (0, 1):
                guard = policy.ramp_guard(schedule_board((fullmove - 1) * 2 + offset), 20)
                self.assertGreater(guard["floor_cp"], 18)
                self.assertLessEqual(guard["max_loss_cp"], 2)
                caps.append(guard["max_loss_cp"])
            self.assertEqual(*caps)
            if previous is not None:
                self.assertLess(caps[0], previous)
            previous = caps[0]
        self.assertEqual(previous, 0)

    def test_draw_rule_checks_do_not_mutate_history(self):
        board, repeat, alternative = repetition_scene(2)
        before = (board.fen(), list(board.move_stack))
        self.assertTrue(policy.draw_risk(board, repeat)["draw_or_claimable"])
        self.assertFalse(policy.draw_risk(board, alternative)["draw_or_claimable"])
        self.assertEqual((board.fen(), board.move_stack), before)

    def test_winning_avoids_claimable_repetition_with_equal_verified_alternative(self):
        for prefix_ply in (30, 31):
            board, repeat, alternative = repetition_scene(2, prefix_ply)
            self.assertLess(board.fullmove_number, 21)
            chosen = self.choose(board, repeat, [self.probe(alternative, 300)])
            self.assertEqual(chosen["uci"], alternative.uci())

    def test_twofold_is_only_a_tiebreak_and_not_a_false_draw(self):
        board, repeat, alternative = repetition_scene(1)
        risk = policy.draw_risk(board, repeat)
        self.assertTrue(risk["twofold"])
        self.assertFalse(risk["draw_or_claimable"])
        chosen = self.choose(board, repeat, [self.probe(alternative, 300)])
        self.assertEqual(chosen["uci"], alternative.uci())

    def test_losing_keeps_a_good_draw_and_all_draws_keep_legal_fallback(self):
        board, repeat, alternative = repetition_scene(2)
        self.assertEqual(self.choose(board, repeat, [self.probe(alternative, -100, 12)], cp=-100)["uci"], repeat.uci())
        self.assertEqual(self.choose(board, repeat, [], cp=300)["uci"], repeat.uci())

    def test_pressure_can_break_only_a_five_cp_quality_tie(self):
        board = live(58).copy(stack=True)
        baseline, alternative = list(board.legal_moves)[:2]
        self.assertEqual(self.choose(board, baseline, [self.probe(alternative, 297, 12)], target=300)["uci"], alternative.uci())
        self.assertEqual(self.choose(board, baseline, [self.probe(alternative, 294, 12)], target=300)["uci"], baseline.uci())
        self.assertEqual(self.choose(board, baseline, [self.probe(alternative, 297, 12, final=294)], target=300)["uci"], baseline.uci())

    def test_pressure_cannot_admit_a_candidate_below_the_ramp_floor(self):
        board = live(58).copy(stack=True)
        baseline, alternative = list(board.legal_moves)[:2]
        chosen = self.choose(board, baseline, [self.probe(alternative, 200, 10000)])
        self.assertEqual(chosen["uci"], baseline.uci())

    def test_pawn_move_count_does_not_generate_pressure_points(self):
        sparse = chess.Board("6k1/8/8/8/8/8/8/1R4K1 b - - 0 21")
        pawns = chess.Board("6k1/p7/8/8/8/8/8/1R4K1 b - - 0 21")
        self.assertNotEqual(sparse.legal_moves.count(), pawns.legal_moves.count())
        self.assertEqual(policy._pressure_profile(sparse), policy._pressure_profile(pawns))

    def test_fifty_move_claim_and_stalemate_are_rule_based(self):
        board = chess.Board("7k/8/8/8/8/8/8/KR6 w - - 99 30")
        risk = policy.draw_risk(board, chess.Move.from_uci("b1b2"))
        self.assertTrue(risk["draw_or_claimable"])
        self.assertEqual(risk["reason"], "fifty_moves")
        board = chess.Board("7k/8/5KQ1/8/8/8/8/8 w - - 0 30")
        risk = policy.draw_risk(board, chess.Move.from_uci("g6f7"))
        self.assertTrue(risk["draw_or_claimable"])
        self.assertEqual(risk["reason"], "stalemate")

    def test_same_seed_and_history_repeat_the_same_selection(self):
        board = live(58).copy(stack=True)
        baseline, alternative = list(board.legal_moves)[:2]
        for _ in range(3):
            chosen = self.choose(board, baseline, [self.probe(alternative, 297, 2)], target=300)
            self.assertEqual(chosen["uci"], alternative.uci())


class RefereeDrawTests(unittest.TestCase):
    setUp = referee_support.SparringRefereeTests.setUp
    peer = referee_support.SparringRefereeTests.peer
    screen = referee_support.SparringRefereeTests.screen

    def run_scene(self, cp):
        board, repeat, alternative = repetition_scene(2)
        sign = 1 if board.turn else -1
        def plan(position, limit, options, index):
            allowed = options.get("root_moves")
            if allowed:
                return {"packets": [referee_support.packet(position, move, cp * sign, rank=rank)
                        for rank, move in enumerate(allowed, 1)], "best": allowed[0]}
            move = next(iter(position.legal_moves))
            return {"packets": [referee_support.packet(position, move, cp * sign)], "best": move}
        service, factory = self.peer(referee_support.FakeFactory(plan))
        return self.screen(service, board, repeat, alternative), repeat, alternative

    def test_reference_avoids_winning_claimable_draw(self):
        result, _, alternative = self.run_scene(300)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["selected_uci"], alternative.uci())

    def test_reference_keeps_losing_draw_defense(self):
        result, repeat, _ = self.run_scene(-100)
        self.assertEqual(result["selected_uci"], repeat.uci())


if __name__ == "__main__":
    unittest.main()
