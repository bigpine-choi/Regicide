"""Synthetic auditor checks only: this file never starts an engine."""
from collections import Counter
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import audit_dynamic_play_records as audit
import dynamic_play as dynamic
import opening_variety as opening


def middle():
    board = chess.Board()
    for san in 'e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Be7 Re1 b5 Bb3 d6 c3 O-O'.split():
        board.push_san(san)
    return board


def packet(board, pv, cp=20, depth=6):
    return {'depth': depth, 'score': chess.engine.PovScore(chess.engine.Cp(cp), board.turn),
            'pv': [chess.Move.from_uci(uci) for uci in pv.split()]}


def request(board, base_pv, other_pv=None, seed='a', label='dynamic', loss=5):
    module = dynamic if label == 'dynamic' else opening
    baseline = packet(board, base_pv)
    base = baseline['pv'][0]
    plan = module.plan(board, 1000, seed)
    probes = []
    if other_pv:
        candidate = packet(board, other_pv, 20 - loss)
        move = candidate['pv'][0]
        order = [move.uci(), base.uci()]
        plan['candidate_order'] = order
        if 'opening_plan' in plan:
            plan['opening_plan']['candidate_order'] = order
        probes.append(module.assess(board, base, baseline, move.uci(), {6: candidate},
                                    candidate, move, budget_ms=100))
    decision = module.receipt(board, plan, base, baseline, probes, seed)
    selected = decision['selected_uci']
    return {'selected_uci': selected, 'selected_san': board.san(chess.Move.from_uci(selected)),
            'decision': decision, 'engine': {'evaluation': 'classical_activity_v1',
                'movetime_ms': 1000, 'dynamic_play_enabled': label == 'dynamic', 'binary_sha256': 'synthetic'}}


def check(board, value, seed='a', label='dynamic'):
    stats = Counter()
    audit.audit_decision(board, seed, label, value, 1000, stats, 'synthetic')
    return stats


class AuditSyntheticTests(unittest.TestCase):
    def test_opening_preserves_v3_draw_and_needs_no_style(self):
        board = chess.Board()
        # Find an input that distinguishes the old dynamic hash from v3.
        for index in range(64):
            seed = str(index)
            value = request(board, 'e2e4 e7e5', 'd2d4 d7d5', seed)
            decision = value['decision']
            old_draw = audit.selected_from_weights(board, seed, dynamic.POLICY, decision['selection_weights'])
            if old_draw != value['selected_uci'] and value['selected_uci'] != 'e2e4':
                break
        else:
            self.fail('Synthetic setup did not distinguish policies')
        stats = check(board, value, seed)
        self.assertEqual(stats['preserved_opening_receipts'], 1)
        self.assertEqual(stats['independent_style_geometry_lines'], 0)
        self.assertEqual(stats['selected_alternatives'], 1)

    def test_opening_nested_selection_tampering_is_rejected(self):
        board = chess.Board()
        value = request(board, 'e2e4 e7e5', 'd2d4 d7d5')
        value['decision']['opening_receipt']['selected_uci'] = 'g1f3'
        with self.assertRaisesRegex(ValueError, 'opening receipt changed selected_uci'):
            check(board, value)

    def test_opening_style_filter_is_rejected(self):
        board = chess.Board()
        value = request(board, 'e2e4 e7e5', 'd2d4 d7d5')
        value['decision']['style_exclusions'] = {'d2d4': 'less_dynamic_after_reply'}
        with self.assertRaisesRegex(ValueError, 'style filtering'):
            check(board, value)

    def test_middle_geometry_and_quality_are_recomputed(self):
        board = middle()
        stats = check(board, request(board, 'h2h3 c6a5', 'd2d4 e5d4'))
        self.assertEqual(stats['independent_style_geometry_lines'], 2)
        self.assertEqual(stats['nonnegative_style_pool_alternatives'], 1)
        self.assertEqual(stats['losses_recomputed_from_cp'], 1)

    def test_middle_negative_style_pool_is_rejected(self):
        board = middle()
        value = request(board, 'd2d4 e5d4', 'h2h3 c6a5')
        decision = value['decision']
        self.assertEqual(decision['selection_pool'], ['d2d4'])
        decision['selection_pool'] = ['d2d4', 'h2h3']
        decision['selection_weights'] = {'d2d4': 3, 'h2h3': 1}
        selected = audit.selected_from_weights(board, 'a', dynamic.POLICY, decision['selection_weights'])
        decision['selected_uci'] = value['selected_uci'] = selected
        value['selected_san'] = board.san(chess.Move.from_uci(selected))
        if selected == 'h2h3':
            probe = decision['probes'][0]
            for name in ('candidate_compared', 'candidate_final', 'candidate_final_raw', 'loss_cp'):
                decision[name] = probe[name]
        with self.assertRaisesRegex(ValueError, 'negative style gain'):
            check(board, value)

    def test_style_geometry_tampering_is_rejected(self):
        board = middle()
        value = request(board, 'h2h3 c6a5', 'd2d4 e5d4')
        value['decision']['style_evidence']['d2d4']['after_opponent_reply']['mobility'] += 1
        with self.assertRaisesRegex(ValueError, 'style geometry'):
            check(board, value)

    def test_loss_tampering_is_rejected(self):
        board = middle()
        value = request(board, 'h2h3 c6a5', 'd2d4 e5d4')
        value['decision']['probes'][0]['loss_cp'] = 0
        with self.assertRaisesRegex(ValueError, 'stored loss'):
            check(board, value)

    def test_unequal_depth_admission_is_rejected(self):
        board = middle()
        value = request(board, 'h2h3 c6a5', 'd2d4 e5d4')
        value['decision']['probes'][0]['candidate_compared']['depth'] = 5
        with self.assertRaisesRegex(ValueError, 'unequal admission depth'):
            check(board, value)

    def test_admission_overoptimistic_score_is_rejected(self):
        board = middle()
        value = request(board, 'h2h3 c6a5', 'd2d4 e5d4')
        probe = value['decision']['probes'][0]
        probe['candidate_compared'].update(cp=36, cp_white=36)
        probe['loss_cp'] = 0
        with self.assertRaisesRegex(ValueError, 'original CP bounds'):
            check(board, value)

    def test_black_pov_and_illegal_pv_are_rejected(self):
        board = chess.Board()
        board.push_uci('e2e4')
        value = request(board, 'e7e5 g1f3', 'c7c5 g1f3', label='legacy')
        check(board, value, label='legacy')
        line = value['decision']['baseline']
        line['cp_white'] = line['cp']
        with self.assertRaisesRegex(ValueError, 'sign mismatch'):
            check(board, value, label='legacy')
        line['cp_white'] = -line['cp']
        line['pv_uci'][1] = 'e2e5'
        with self.assertRaisesRegex(ValueError, 'illegal PV move'):
            check(board, value, label='legacy')

    def test_complete_synthetic_matrix_and_duplicate_rejection(self):
        fixture = ROOT / 'tests/data/dynamic_play_positions_v1.json'
        bank = json.loads(fixture.read_text(encoding='utf-8'))
        rows = []
        for position in bank['positions']:
            board = chess.Board(position['initial_fen'])
            for uci in position['history_uci']:
                board.push_uci(uci)
            move = sorted(board.legal_moves, key=lambda item: item.uci())[0]
            after = board.copy(stack=True)
            after.push(move)
            reply = sorted(after.legal_moves, key=lambda item: item.uci())[0]
            pv = move.uci() + ' ' + reply.uci()
            for seed in bank['conditions']['seeds']:
                new = request(board, pv, seed=seed)
                if len(board.move_stack) < 16:
                    old = request(board, pv, seed=seed, label='legacy')
                else:
                    old = copy.deepcopy(new)
                    old['engine']['dynamic_play_enabled'] = False
                    old['decision'] = None
                rows.append({'id': position['id'], 'seed': seed, 'fen': position['fen'],
                             'history_uci': position['history_uci'], 'legacy': old, 'dynamic': new})
        with tempfile.TemporaryDirectory(prefix='regicide-audit-synthetic-') as temporary:
            folder = Path(temporary)
            (folder / 'manifest.json').write_text(json.dumps({'fixture_sha256': audit.digest(fixture),
                                                            'binary_sha256': 'synthetic'}), encoding='utf-8')
            records = folder / 'records.jsonl'
            records.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
            result = audit.audit(records, fixture)
            self.assertEqual(result['pairs'], 32)
            self.assertEqual(result['counts']['requests'], 64)
            self.assertEqual(result['counts']['preserved_opening_receipts'],
                             sum(len(row['history_uci']) < 16 for row in bank['positions'])
                             * len(bank['conditions']['seeds']))
            rows[-1] = copy.deepcopy(rows[0])
            records.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'duplicate pair'):
                audit.audit(records, fixture)


if __name__ == '__main__':
    unittest.main(verbosity=2)
