"""Speculative boss choices and bounded same-position human move evidence."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

import chess
import chess.engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import boss_play as policy
import boss_openings


def after(*moves):
    board = chess.Board()
    for move in moves:
        board.push_uci(move)
    return board


def packet(board, uci, cp=0, depth=4, **extra):
    return {'depth': depth, 'score': chess.engine.PovScore(chess.engine.Cp(cp), board.turn),
            'pv': [chess.Move.from_uci(uci)], **extra}


def seed_for(board, uci):
    for number in range(512):
        seed = f'{number:032x}'
        if policy.plan(board, 1000, seed)['candidate_uci'] == uci:
            return seed
    raise AssertionError('candidate never proposed')


class BossPolicyTests(unittest.TestCase):
    def setUp(self):
        self.board = after('d2d4')
        self.seed = seed_for(self.board, 'e7e5')
        self.base_move = chess.Move.from_uci('g8f6')
        self.base = packet(self.board, 'g8f6', 25)

    def assessed(self, cp=-150, depth=4, **kwargs):
        candidate = packet(self.board, 'e7e5', cp, depth)
        return policy.assess(self.board, self.base_move, self.base, 'e7e5',
                             {depth: candidate}, candidate, chess.Move.from_uci('e7e5'), **kwargs)

    def test_immediate_gambit_priority_full_budget_and_bounded_single_probe(self):
        choices = {policy.plan(self.board, 1000, f'{n:032x}')['candidate_uci'] for n in range(128)}
        self.assertEqual(choices, {'e7e5'})
        plan = policy.plan(self.board, 1000, self.seed)
        self.assertEqual(plan['baseline_budget_ms'], 1000)
        self.assertEqual(plan['extra_budget_ms'], 1000)
        self.assertEqual(plan['candidate_order'], ['e7e5'])
        self.assertEqual(plan['line_id'], 'englund')
        self.assertTrue(plan['line_complete'])
        self.assertEqual(policy.plan(self.board, 5000, self.seed)['extra_budget_ms'], 1000)

    def test_speculative_openings_record_cost_without_cp_floor_or_loss_cap(self):
        accepted = self.assessed(-150)
        self.assertTrue(accepted['admitted'])
        self.assertEqual(accepted['loss_cp'], 175)
        self.assertTrue(self.assessed(-1000)['admitted'])
        base = packet(self.board, 'g8f6', 100)
        candidate = packet(self.board, 'e7e5', -201)
        self.assertTrue(policy.assess(self.board, self.base_move, base, 'e7e5', {4: candidate}, candidate,
                                      chess.Move.from_uci('e7e5'))['admitted'])
        behind = packet(self.board, 'g8f6', -600)
        self.assertIsNone(policy.baseline_guard(self.board, self.base_move, behind))

    def test_mates_bounds_wrong_depth_wrong_pov_conflicts_are_not_evidence(self):
        good = packet(self.board, 'e7e5', -150)
        bad = [dict(good, lowerbound=True), dict(good, upperbound=True), dict(good, depth=3),
               dict(good, score=chess.engine.PovScore(chess.engine.Mate(1), self.board.turn)),
               dict(good, score=chess.engine.PovScore(chess.engine.Cp(-150), not self.board.turn)),
               dict(good, pv=[chess.Move.from_uci('e7e4')])]
        for candidate in bad:
            with self.subTest(candidate=candidate):
                result = policy.assess(self.board, self.base_move, self.base, 'e7e5',
                                       {candidate['depth']: candidate}, candidate, chess.Move.from_uci('e7e5'))
                self.assertFalse(result['admitted'])

    def test_signature_completes_book_and_keeps_the_gambit_attack_active(self):
        plan = policy.plan(self.board, 1000, self.seed)
        row = policy.receipt(self.board, plan, self.base_move, self.base, [self.assessed()], self.seed)
        self.board.push_uci('e7e5')
        self.assertEqual(policy.phase(self.board, [row], engine_color=chess.BLACK), 'initiative')
        self.board.push_uci('d4e5')
        following = policy.plan(self.board, 1000, self.seed, decisions=[row])
        self.assertTrue(following['eligible'])
        self.assertEqual(following['mode'], 'initiative')
        self.assertIsNone(following['candidate_uci'])
        self.assertTrue(following['initiative_context']['accepted'])
        self.assertEqual(following['initiative_context']['material_debt_cp'], 100)

    def test_preparation_can_switch_to_a_matching_immediate_gambit(self):
        board = after('e2e4', 'e7e5')
        row = {'ply': 1, 'line_id': 'evans', 'candidate_uci': 'e2e4', 'selected_uci': 'e2e4', 'line_complete': False}
        for n in range(20):
            plan = policy.plan(board, 1000, f'{n:032x}', decisions=[row])
            self.assertEqual(plan['line_id'], 'kings')
            self.assertEqual(plan['candidate_uci'], 'f2f4')
        diverted = after('e2e4', 'c7c5')
        self.assertEqual(policy.plan(diverted, 1000, self.seed, decisions=[row])['mode'], 'opening')
        self.assertEqual(policy.plan(diverted, 1000, self.seed, decisions=[row])['line_id'], 'smith-morra')

    def test_guards_and_black_white_score_direction(self):
        for board, budget, forced in [(self.board, 100, False), (self.board, 1000, True),
                                      (after('g1f3', 'g8f6', 'f3g1', 'f6g8'), 1000, False),
                                      (chess.Board('7k/8/8/8/8/8/8/KQ6 w - - 0 1'), 1000, False)]:
            self.assertFalse(policy.plan(board, budget, self.seed, forced=forced)['eligible'])
        self.assertEqual(self.assessed()['candidate_compared']['cp_white'], 150)
        self.assertEqual(policy.phase(chess.Board(), engine_color=chess.BLACK), 'opening')

    def test_invalid_evidence_keeps_baseline_and_no_commitment(self):
        plan = policy.plan(self.board, 1000, self.seed)
        failed = self.assessed(candidate_error='TimeoutError')
        row = policy.receipt(self.board, plan, self.base_move, self.base, [failed], self.seed)
        self.assertEqual(row['selected_uci'], 'g8f6')
        self.assertIsNone(row['line_id'])
        self.assertEqual(policy.commitment([row]), (None, False))

    def test_opening_frequency_is_separate_from_traps_and_probe_failure(self):
        plan = policy.plan(self.board, 1000, self.seed)
        original = dict(plan)
        closed = {'attempt_trap': False, 'probability': .1}
        selected, gate = policy.apply_frequency(plan, closed, self.base_move)
        self.assertTrue(selected['eligible'])
        self.assertTrue(selected['opening_frequency_exempt'])
        self.assertFalse(selected['frequency_gate_applied'])
        self.assertFalse(selected['conservative_setup'])
        self.assertFalse(gate['attempt_trap'])
        self.assertTrue(gate['opening_exempt'])
        self.assertEqual(plan, original)
        self.assertEqual(closed, {'attempt_trap': False, 'probability': .1})
        failed, _ = policy.apply_frequency(plan, closed, self.base_move, probe_failed=True)
        self.assertFalse(failed['eligible'])
        self.assertEqual(failed['reason'], 'quality_probe_failed')
        trap = {**plan, 'mode': 'trap_hunt', 'candidate_uci': None}
        held, _ = policy.apply_frequency(trap, closed, self.base_move)
        self.assertFalse(held['eligible'])
        self.assertTrue(held['frequency_gate_applied'])
        self.assertEqual(held['reason'], 'opponent_frequency_hold')

    def test_preparation_cost_limit_is_independent_of_an_open_trap_gate(self):
        board = chess.Board()
        plan = policy.plan(board, 1000, seed_for(board, 'e2e4'))
        selected, gate = policy.apply_frequency(plan, {'attempt_trap': True}, chess.Move.from_uci('b1c3'))
        self.assertTrue(selected['eligible'])
        self.assertFalse(selected['line_complete'])
        self.assertTrue(selected['conservative_setup'])
        self.assertTrue(gate['opening_exempt'])

    def test_native_baseline_signature_records_authorship_and_completion(self):
        plan = policy.plan(self.board, 1000, self.seed)
        move = chess.Move.from_uci('e7e5')
        info = packet(self.board, 'e7e5', -150)
        row = policy.receipt(self.board, plan, move, info, seed=self.seed)
        self.assertEqual(row['selected_uci'], row['candidate_uci'])
        self.assertEqual(row['reason'], 'authored_move_is_baseline')
        self.assertEqual(row['line_id'], 'englund')
        self.assertTrue(row['line_complete'])
        self.assertEqual(policy.commitment([row]), ('englund', True))

    def test_memo_and_pgn_identity(self):
        args = (self.board, 1000, 'binary', 'evaluation')
        key = policy.memo_key(*args, seed=self.seed, catalog_id='englund')
        self.assertNotEqual(key, policy.memo_key(*args, seed=self.seed, catalog_id='albin'))
        meta = {'enabled': True, 'policy': policy.POLICY, 'seed': self.seed}
        self.assertEqual(policy.pgn_headers(meta)['BossMode'], '1')
        legacy = policy.pgn_headers({**meta, 'policy': 'regicide-boss-play-v3'})
        self.assertEqual(legacy['BossPolicy'], 'regicide-boss-play-v3')
        for changed in ({'enabled': 1}, {'seed': 'bad'}, {'policy': 'unknown'}):
            self.assertEqual(policy.pgn_headers({**meta, **changed}), {})


class HumanObservationTests(unittest.TestCase):
    def observe(self, *, best='e2e4', actual_cp=-100, depth=4, flags=None, failure=None):
        board = after('d2d4')
        calls = []
        def search(budget, *, root_move=None, position=None, **options):
            calls.append((budget, root_move, position, options))
            self.assertEqual(position.move_stack, [])
            self.assertEqual(position.turn, chess.WHITE)
            self.assertFalse(options['publish'])
            self.assertTrue(options['fresh'])
            if failure:
                raise failure
            uci = root_move.uci() if root_move else best
            info = packet(position, uci, actual_cp if root_move else 100,
                          depth if root_move else 4, **(flags or {}))
            return chess.engine.BestMove(chess.Move.from_uci(uci), None), info, {info['depth']: info}
        result, discarded = policy.observe_human_move(board, search, lambda: None)
        self.assertEqual([move.uci() for move in board.move_stack], ['d2d4'])
        return result, discarded, calls

    def test_actual_human_loss_is_from_two_same_pre_move_searches(self):
        row, discarded, calls = self.observe()
        self.assertFalse(discarded)
        self.assertTrue(row['valid'])
        self.assertEqual(row['loss_cp'], 200)
        self.assertEqual(row['ply'], 1)
        self.assertEqual(row['history_before'], [])
        self.assertEqual(calls[1][1].uci(), 'd2d4')
        self.assertEqual(calls[1][3]['depth'], 4)
        self.assertLessEqual(calls[0][0], 225 * .45)
        self.assertLessEqual(calls[1][0], 225)
        self.assertLessEqual(row['elapsed_ms'], 225)

    def test_matching_reference_needs_only_one_search(self):
        row, discarded, calls = self.observe(best='d2d4')
        self.assertTrue(row['valid'])
        self.assertEqual(row['loss_cp'], 0)
        self.assertEqual(len(calls), 1)

    def test_incomplete_conflicting_or_failed_quality_is_unknown(self):
        for options in ({'depth': 3}, {'actual_cp': 150}, {'flags': {'upperbound': True}},
                        {'failure': TimeoutError('controlled')}):
            with self.subTest(options=options):
                row, _, _ = self.observe(**options)
                self.assertFalse(row['valid'])
                self.assertIsNone(row['loss_cp'])


class PoisonedPieceSelectionTests(unittest.TestCase):
    def setUp(self):
        self.board = after('e2e4', 'e7e5', 'g1f3', 'd7d6', 'f1c4', 'c8g4', 'b1c3', 'g7g6')
        self.offer = {'uci': 'f3e5', 'capture_uci': 'g4d1', 'offered_piece': 'queen',
                      'offered_cp': 900, 'capturing_piece': 'bishop', 'newly_exposed': True}
        self.plan = policy.plan(self.board, 1000, '00' * 16)
        self.calls = []

    def run_selection(self, *, baseline='f3e5', candidate_cp=90, baseline_cp=100, branch_error=None, prove=True):
        base_move = chess.Move.from_uci(baseline)
        base = packet(self.board, baseline, baseline_cp)
        def search(budget, *, root_move=None, position=None, **options):
            self.calls.append({'budget': budget, 'root_move': root_move, 'position': position, **options})
            if position is None:
                info = packet(self.board, root_move.uci(), candidate_cp)
                return chess.engine.BestMove(root_move, None), info, {4: info}
            if branch_error:
                raise branch_error
            expected = self.board.copy(stack=True)
            expected.push_uci('f3e5')
            expected.push_uci('g4d1')
            self.assertEqual(position.fen(), expected.fen())
            self.assertEqual(position.move_stack, expected.move_stack)
            self.assertFalse(options['publish'])
            if prove:
                info = {'depth': 4, 'score': chess.engine.PovScore(chess.engine.Mate(2), chess.WHITE),
                        'pv': [chess.Move.from_uci(m) for m in ('c4f7', 'e8e7', 'c3d5')]}
            else:
                info = packet(position, 'c4f7', 20)
            return chess.engine.BestMove(info['pv'][0], None), info, {4: info}
        with patch('poisoned_piece.enumerate_offers', return_value=[self.offer]):
            return policy.explore_traps(self.board, self.plan, base_move, base, {4: base}, '00' * 16,
                                        search, lambda: None)

    def test_baseline_poisoned_queen_is_annotated_without_changing_best_move(self):
        result, discard = self.run_selection()
        self.assertFalse(discard)
        self.assertEqual(result['selected_uci'], 'f3e5')
        self.assertEqual(result['loss_cp'], 0)
        self.assertEqual(result['reason'], 'verified_poisoned_piece')
        self.assertTrue(result['trap_evidence']['verified'])
        self.assertEqual(len(self.calls), 1)
        self.assertIsNotNone(self.calls[0]['position'])

    def test_speculative_offer_requires_its_own_common_depth_and_capture_proof(self):
        result, discard = self.run_selection(baseline='e1g1', candidate_cp=90)
        self.assertFalse(discard)
        self.assertEqual(result['selected_uci'], 'f3e5')
        self.assertEqual(result['loss_cp'], 10)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0]['depth'], 4)
        self.assertTrue(all(call['publish'] is False for call in self.calls))

    def test_losing_refusal_vetoes_a_tempting_trap_before_capture_proof(self):
        result, discard = self.run_selection(baseline='e1g1', candidate_cp=-400)
        self.assertFalse(discard)
        self.assertEqual(result['selected_uci'], 'e1g1')
        self.assertIsNone(result['trap_evidence'])
        self.assertEqual(result['loss_cp'], 0)
        self.assertEqual(result['trap_attempts'][0]['loss_cp'], 500)
        self.assertEqual(result['trap_attempts'][0]['reason'], 'trap_outside_quality_floor')
        self.assertEqual(result['risk_profile'], 'quality_gated')
        self.assertEqual(len(self.calls), 1)

    def test_no_proof_or_failed_probe_keeps_full_baseline(self):
        result, discard = self.run_selection(baseline='e1g1', prove=False)
        self.assertFalse(discard)
        self.assertEqual(result['selected_uci'], 'e1g1')
        self.assertIsNone(result['trap_evidence'])
        result, discard = self.run_selection(baseline='e1g1', branch_error=TimeoutError('controlled'))
        self.assertTrue(discard)
        self.assertEqual(result['selected_uci'], 'e1g1')
        self.assertIsNone(result['trap_evidence'])

    def test_trap_quality_boundary_and_nonlosing_floor(self):
        for cp, admitted in [(65, True), (64, False)]:
            with self.subTest(cp=cp):
                result, discard = self.run_selection(baseline='e1g1', candidate_cp=cp)
                self.assertFalse(discard)
                self.assertEqual(result['selected_uci'], 'f3e5' if admitted else 'e1g1')
        result, discard = self.run_selection(baseline='e1g1', baseline_cp=-50, candidate_cp=-20)
        self.assertFalse(discard)
        self.assertEqual(result['selected_uci'], 'e1g1')
        self.assertEqual(result['trap_attempts'][0]['reason'], 'trap_outside_quality_floor')


if __name__ == '__main__':
    unittest.main(verbosity=2)
